"""Control Center SQLite migration runner.

Uses only the Python standard library `sqlite3`.
Migrations are applied in strict integer order inside a transaction.
The migration history table records a content checksum for drift detection.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

MIGRATIONS_TABLE = "schema_migrations"
MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""

MIGRATIONS: List["Migration"] = []


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: Tuple[str, ...]

    def __post_init__(self):
        # Validate on construction so a registry built from constants still
        # fails fast if it is somehow malformed.
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("migration version must be an int")
        if self.version < 0:
            raise ValueError("migration version must be non-negative")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("migration name must be a non-empty string")
        if not isinstance(self.statements, tuple):
            raise ValueError("migration statements must be a tuple")
        if not self.statements:
            raise ValueError("migration statements must not be empty")
        if not all(isinstance(s, str) and s.strip() for s in self.statements):
            raise ValueError("each migration statement must be a non-empty string")

    @property
    def sql(self) -> str:
        """Stable, canonical SQL representation for checksums and logging."""
        return ";\n".join(self.statements) + ";"

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class MigrationError(Exception):
    """Raised when migration cannot be applied safely."""

    def __init__(self, message: str):
        super().__init__(message)
        self.__suppress_context__ = True


class ChecksumMismatchError(MigrationError):
    """Raised when a previously applied migration has changed."""


class UnknownSchemaError(MigrationError):
    """Raised when the database contains a migration version this code does not know."""


class RegistryValidationError(MigrationError):
    """Raised when the migration registry itself is invalid."""


def _validate_registry(migrations: Iterable[Migration]) -> List[Migration]:
    """Validate and return migrations sorted by ascending version.

    Runs before any database write so that an invalid registry cannot partially
    modify the database or leave a successful migration record.
    """
    migrations = list(migrations)
    seen_versions = set()
    for migration in migrations:
        if not isinstance(migration, Migration):
            raise RegistryValidationError("registry must contain only Migration instances")
        if migration.version in seen_versions:
            raise RegistryValidationError(f"duplicate migration version {migration.version}")
        seen_versions.add(migration.version)

    if not migrations:
        raise RegistryValidationError("migration registry must not be empty")

    if 0 not in seen_versions:
        raise RegistryValidationError("registry must include bootstrap version 0")

    return sorted(migrations, key=lambda m: m.version)


class Database:
    """Thin wrapper around a SQLite connection with the required pragmas."""

    BUSY_TIMEOUT_MS = 5000

    def __init__(self, path: str):
        self.path = path
        self._connection: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            self._connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def __enter__(self) -> sqlite3.Connection:
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _ensure_data_dir(path: str) -> None:
    """Create the parent directory for the database if it does not exist."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def _applied_migrations(cursor: sqlite3.Cursor) -> dict:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (MIGRATIONS_TABLE,),
    )
    if not cursor.fetchone():
        return {}
    cursor.execute(
        f"SELECT version, checksum FROM {MIGRATIONS_TABLE} ORDER BY version ASC"
    )
    return {row[0]: row[1] for row in cursor.fetchall()}


def _record_migration(cursor: sqlite3.Cursor, migration: Migration) -> None:
    applied_at = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
        (migration.version, migration.name, migration.checksum, applied_at),
    )


def _max_applied_version(cursor: sqlite3.Cursor) -> Optional[int]:
    cursor.execute(f"SELECT MAX(version) FROM {MIGRATIONS_TABLE}")
    row = cursor.fetchone()
    return row[0] if row and row[0] is not None else None


def _all_applied_versions(cursor: sqlite3.Cursor) -> set:
    cursor.execute(f"SELECT version FROM {MIGRATIONS_TABLE}")
    return {row[0] for row in cursor.fetchall()}


def migrate(db_path: str, migrations: Optional[Iterable[Migration]] = None) -> int:
    """Apply all pending migrations and return the current schema version.

    Raises:
        MigrationError: if the database is unusable, a checksum mismatch is
            detected, or the database contains an unknown schema version.
    """
    registry = _validate_registry(migrations if migrations is not None else MIGRATIONS)

    _ensure_data_dir(db_path)
    db = Database(db_path)
    conn = db.connect()
    cursor = conn.cursor()
    try:
        # Fail closed if the database contains any version not in the registry.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (MIGRATIONS_TABLE,),
        )
        if cursor.fetchone():
            applied_versions = _all_applied_versions(cursor)
            known_versions = {m.version for m in registry}
            unknown = applied_versions - known_versions
            if unknown:
                raise UnknownSchemaError(
                    "database schema is newer than this version of the application"
                )

        applied = _applied_migrations(cursor)

        for migration in registry:
            existing_checksum = applied.get(migration.version)
            if existing_checksum is not None:
                if existing_checksum != migration.checksum:
                    raise ChecksumMismatchError(
                        f"Migration {migration.version} checksum drift detected"
                    )
                continue

            cursor.execute("BEGIN IMMEDIATE")
            try:
                # Bootstrap: the very first migration (version 0) is responsible
                # for creating the tracking table itself. It runs in the same
                # transaction as its record so that a failure between DDL and
                # INSERT cannot leave the database half-initialized.
                if migration.version == 0:
                    for statement in migration.statements:
                        cursor.execute(statement)
                else:
                    for statement in migration.statements:
                        cursor.execute(statement)
                _record_migration(cursor, migration)
                cursor.execute("COMMIT")
            except Exception as exc:
                try:
                    cursor.execute("ROLLBACK")
                except Exception:
                    pass
                raise MigrationError(
                    f"Failed to apply migration {migration.version}: {type(exc).__name__}"
                ) from None

        return _max_applied_version(cursor)
    finally:
        db.close()


def get_schema_version(db_path: str) -> Optional[int]:
    """Return the highest recorded schema version, or None if migrations table is absent."""
    if not os.path.exists(db_path):
        return None
    db = Database(db_path)
    try:
        conn = db.connect()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (MIGRATIONS_TABLE,),
        )
        if not cursor.fetchone():
            return None
        return _max_applied_version(cursor)
    finally:
        db.close()


# M0: Establish the migration tracking table itself. No business tables.
MIGRATIONS.append(
    Migration(
        version=0,
        name="create_schema_migrations_table",
        statements=(MIGRATIONS_DDL,),
    )
)


MIGRATIONS.append(
    Migration(
        version=1,
        name="create_privacy_safe_routing_telemetry",
        statements=(
            """
            CREATE TABLE routing_events (
                event_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_kind TEXT NOT NULL CHECK (
                    event_kind IN ('request_started', 'judge_completed', 'request_completed')
                ),
                traffic_class TEXT NOT NULL CHECK (
                    traffic_class IN ('production', 'admin_test', 'deployment_smoke')
                ),
                transport TEXT NOT NULL CHECK (transport IN ('http', 'websocket')),
                requested_model TEXT NOT NULL,
                route_policy TEXT,
                cache_status TEXT,
                rejudge_reason TEXT,
                judge_status TEXT,
                selected_model TEXT,
                final_status TEXT,
                fallback INTEGER NOT NULL DEFAULT 0 CHECK (fallback IN (0, 1)),
                error_category TEXT,
                judge_latency_ms REAL,
                first_byte_latency_ms REAL,
                total_latency_ms REAL,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                config_version_id INTEGER,
                session_hash_prefix TEXT
            )
            """,
            "CREATE INDEX idx_routing_events_request ON routing_events(request_id, occurred_at)",
            "CREATE INDEX idx_routing_events_time ON routing_events(occurred_at)",
            "CREATE INDEX idx_routing_events_model ON routing_events(selected_model, occurred_at)",
            """
            CREATE TABLE routing_aggregates_hourly (
                bucket_start TEXT NOT NULL,
                traffic_class TEXT NOT NULL,
                requested_model TEXT NOT NULL,
                selected_model TEXT NOT NULL DEFAULT '',
                final_status TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                judge_call_count INTEGER NOT NULL DEFAULT 0,
                cache_hit_count INTEGER NOT NULL DEFAULT 0,
                fallback_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                total_latency_sum_ms REAL NOT NULL DEFAULT 0,
                total_latency_sample_count INTEGER NOT NULL DEFAULT 0,
                judge_latency_sum_ms REAL NOT NULL DEFAULT 0,
                judge_latency_sample_count INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                token_sample_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (
                    bucket_start,
                    traffic_class,
                    requested_model,
                    selected_model,
                    final_status
                )
            )
            """,
            "CREATE INDEX idx_routing_aggregates_hourly_time ON routing_aggregates_hourly(bucket_start)",
        ),
    )
)


MIGRATIONS.append(
    Migration(
        version=2,
        name="create_configuration_versions_drafts_and_audit",
        statements=(
            """
            CREATE TABLE configuration_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_number INTEGER NOT NULL UNIQUE CHECK (version_number >= 1),
                parent_version_id INTEGER REFERENCES configuration_versions(version_id),
                source TEXT NOT NULL CHECK (source IN ('yaml_baseline', 'draft')),
                snapshot_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                release_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER configuration_versions_immutable_update
            BEFORE UPDATE ON configuration_versions
            BEGIN
                SELECT RAISE(ABORT, 'configuration versions are immutable');
            END
            """,
            """
            CREATE TRIGGER configuration_versions_immutable_delete
            BEFORE DELETE ON configuration_versions
            BEGIN
                SELECT RAISE(ABORT, 'configuration versions are immutable');
            END
            """,
            """
            CREATE TABLE configuration_state (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                active_version_id INTEGER NOT NULL REFERENCES configuration_versions(version_id),
                initialized_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE configuration_drafts (
                draft_id TEXT PRIMARY KEY,
                base_version_id INTEGER NOT NULL REFERENCES configuration_versions(version_id),
                finalized_version_id INTEGER REFERENCES configuration_versions(version_id),
                status TEXT NOT NULL CHECK (status IN ('editing', 'ready', 'finalized')),
                revision INTEGER NOT NULL CHECK (revision >= 1),
                snapshot_json TEXT NOT NULL,
                checksum TEXT NOT NULL,
                validation_json TEXT NOT NULL DEFAULT '[]',
                release_notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_configuration_drafts_updated ON configuration_drafts(updated_at DESC)",
            """
            CREATE TABLE admin_audit_events (
                audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'denied')),
                subject_type TEXT,
                subject_id TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX idx_admin_audit_events_time ON admin_audit_events(occurred_at DESC, audit_id DESC)",
        ),
    )
)

MIGRATIONS.append(
    Migration(
        version=3,
        name="add_named_active_drafts_and_model_catalog",
        statements=(
            "ALTER TABLE configuration_drafts ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE configuration_drafts ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))",
            "CREATE INDEX idx_configuration_drafts_active ON configuration_drafts(is_active, updated_at DESC)",
            "CREATE TABLE configuration_model_catalog (model_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)",
        ),
    )
)

MIGRATIONS.append(
    Migration(
        version=4,
        name="persist_admin_credentials",
        statements=(
            """
            CREATE TABLE admin_credentials (
                credential_id INTEGER PRIMARY KEY CHECK (credential_id = 1),
                password_digest TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ),
    )
)
