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
