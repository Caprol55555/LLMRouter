"""Control Center phase 0 foundation tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from openclaw_router.config import ControlCenterConfig, OpenClawConfig, RouterConfig
from openclaw_router.control_center import migrations
from openclaw_router.control_center.migrations import (
    Database,
    ChecksumMismatchError,
    Migration,
    MigrationError,
    RegistryValidationError,
    UnknownSchemaError,
    MIGRATIONS,
    migrate,
    MIGRATIONS_TABLE,
)
from openclaw_router.control_center.runtime import ControlCenterRuntime, ControlCenterState
from openclaw_router.server import create_app


def test_config_defaults_disable_control_center():
    config = OpenClawConfig()
    assert config.control_center.enabled is False
    assert config.control_center.data_dir == "/data"
    assert config.control_center.db_path == "/data/control-center.db"


def test_db_filename_is_constant():
    """The database filename cannot be overridden by __init__ or YAML."""
    config = ControlCenterConfig(enabled=True, data_dir="/var/lib/llmrouter")
    assert config.DB_FILENAME == "control-center.db"
    assert config.db_path == "/var/lib/llmrouter/control-center.db"

    # Passing DB_FILENAME to the constructor must not change the constant.
    with pytest.raises(TypeError):
        ControlCenterConfig(enabled=True, data_dir="/data", DB_FILENAME="evil.db")


def test_db_path_remains_inside_resolved_data_dir():
    config = ControlCenterConfig(enabled=True, data_dir="/data")
    assert config.db_path == "/data/control-center.db"
    config.validate()
    assert config.db_path == "/data/control-center.db"


def test_old_yaml_without_control_center_loads():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("serve:\n  host: 0.0.0.0\n  port: 8000\nrouter:\n  strategy: random\n")
        path = f.name
    try:
        config = OpenClawConfig.from_yaml(path)
        assert config.control_center.enabled is False
        assert config.control_center.data_dir == "/data"
    finally:
        os.unlink(path)


def test_yaml_parses_enabled_and_absolute_data_dir():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(
            "serve:\n  host: 0.0.0.0\n  port: 8000\n"
            "control_center:\n  enabled: true\n  data_dir: /var/lib/llmrouter\n"
        )
        path = f.name
    try:
        config = OpenClawConfig.from_yaml(path)
        assert config.control_center.enabled is True
        assert config.control_center.data_dir == "/var/lib/llmrouter"
    finally:
        os.unlink(path)


def test_relative_data_dir_is_rejected():
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir="data")
    with pytest.raises(ValueError, match="absolute path"):
        config.validate()


def test_empty_data_dir_is_rejected():
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir="  ")
    with pytest.raises(ValueError, match="non-empty"):
        config.validate()


def test_disabled_control_center_does_not_create_data_dir_or_database(
    temp_data_dir: Path,
):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=False, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    assert app.state.control_center.state == ControlCenterState.DISABLED
    assert not (temp_data_dir / "control-center.db").exists()


def test_disabled_status_returns_404_no_store(temp_data_dir: Path):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=False, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    client = TestClient(app)
    response = client.get("/admin/api/status")
    assert response.status_code == 404
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["error"]["code"] == "control_center_disabled"


def test_enabled_database_created_only_in_data_dir(temp_data_dir: Path):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    assert app.state.control_center.state == ControlCenterState.OK
    assert (temp_data_dir / "control-center.db").exists()
    assert list(temp_data_dir.iterdir()) == [temp_data_dir / "control-center.db"]


def test_migration_first_run_succeeds(migrated_database: Path):
    assert migrated_database.exists()
    db = Database(str(migrated_database))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA user_version")
        cursor.execute("SELECT version, name, checksum, applied_at FROM schema_migrations")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 0
        assert rows[0][1] == "create_schema_migrations_table"
        assert rows[0][2]
        assert rows[0][3]
    finally:
        db.close()


def test_migration_is_idempotent(migrated_database: Path):
    migrate(str(migrated_database))
    migrate(str(migrated_database))
    db = Database(str(migrated_database))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM schema_migrations")
        assert cursor.fetchone()[0] == 1
    finally:
        db.close()


def test_schema_version_matches(migrated_database: Path):
    version = migrations.get_schema_version(str(migrated_database))
    assert version == 0


def test_pragmas_are_applied(migrated_database: Path):
    db = Database(str(migrated_database))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        assert cursor.fetchone()[0].lower() == "wal"
        cursor.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone()[0] == Database.BUSY_TIMEOUT_MS
    finally:
        db.close()


def test_checksum_drift_is_rejected(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))

    original = MIGRATIONS[0]
    try:
        MIGRATIONS[0] = Migration(
            version=original.version,
            name=original.name,
            statements=original.statements + ("-- tampered",),
        )
        with pytest.raises(ChecksumMismatchError):
            migrate(str(db_path))
    finally:
        MIGRATIONS[0] = original


def test_migrations_run_in_version_order(temp_data_dir: Path):
    """Unordered registry is sorted; v2 depends on v1 actually running first."""
    db_path = temp_data_dir / "control-center.db"

    v0 = Migration(
        version=0,
        name="bootstrap",
        statements=(migrations.MIGRATIONS_DDL,),
    )
    v1 = Migration(
        version=1,
        name="create_business_table",
        statements=("CREATE TABLE business (id INTEGER PRIMARY KEY, value TEXT)",),
    )
    v2 = Migration(
        version=2,
        name="add_business_index",
        statements=("CREATE INDEX idx_business_value ON business(value)",),
    )

    # Supply in reverse order.
    migrate(str(db_path), migrations=[v2, v1, v0])

    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT version FROM schema_migrations ORDER BY version")
        versions = [row[0] for row in cursor.fetchall()]
        assert versions == [0, 1, 2]

        # v2's index can only exist if v1's table was created first.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_business_value'"
        )
        assert cursor.fetchone() is not None
    finally:
        db.close()


def test_duplicate_versions_rejected(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    registry = [
        Migration(version=0, name="bootstrap", statements=("SELECT 1",)),
        Migration(version=1, name="one", statements=("SELECT 1",)),
        Migration(version=1, name="one_again", statements=("SELECT 2",)),
    ]
    with pytest.raises(RegistryValidationError, match="duplicate migration version 1"):
        migrate(str(db_path), registry)
    assert not db_path.exists()


@pytest.mark.parametrize(
    "bad_version",
    [
        "1",  # string
        1.5,  # float
        True,  # bool
        -1,  # negative
    ],
)
def test_invalid_versions_rejected(bad_version):
    with pytest.raises((ValueError, RegistryValidationError)):
        migrate(
            "/tmp/should_not_be_created.db",
            migrations=[Migration(version=bad_version, name="bad", statements=("SELECT 1",))],
        )


def test_invalid_registry_leaves_no_database(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    registry = [
        Migration(version=0, name="bootstrap", statements=("SELECT 1",)),
        Migration(version=0, name="dup", statements=("SELECT 2",)),
    ]
    with pytest.raises(RegistryValidationError):
        migrate(str(db_path), registry)
    assert not db_path.exists()


def test_future_schema_rejected_and_degraded(temp_data_dir: Path):
    """A database with an unknown future version must fail closed."""
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))

    # Simulate a future deployment having applied version 7.
    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"INSERT INTO {MIGRATIONS_TABLE} (version, name, checksum, applied_at) VALUES (?, ?, ?, ?)",
            (7, "future_migration", "sha256future", "2026-08-20T00:00:00+00:00"),
        )
    finally:
        db.close()

    with pytest.raises(UnknownSchemaError):
        migrate(str(db_path))

    # The unknown record must not be touched.
    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT version, checksum FROM {MIGRATIONS_TABLE} WHERE version = 7")
        row = cursor.fetchone()
        assert row == (7, "sha256future")
    finally:
        db.close()

    # Runtime must enter DEGRADED and status returns 503 with schema_version null.
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))

    app = create_app(config=config)
    runtime = app.state.control_center
    assert runtime.state == ControlCenterState.DEGRADED
    assert runtime.schema_version is None
    client = TestClient(app)
    response = client.get("/admin/api/status")
    assert response.status_code == 503
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["schema_version"] is None
    assert str(temp_data_dir) not in response.text
    assert "sha256future" not in response.text


def test_failed_migration_not_recorded_and_rolls_back(temp_data_dir: Path):
    """A real SQL failure rolls back the whole migration transaction."""
    db_path = temp_data_dir / "control-center.db"

    registry = [
        Migration(
            version=0,
            name="bootstrap",
            statements=(migrations.MIGRATIONS_DDL,),
        ),
        Migration(
            version=1,
            name="create_then_explode",
            statements=(
                "CREATE TABLE business (id INTEGER PRIMARY KEY)",
                "THIS IS INVALID SQL",
            ),
        ),
    ]

    with pytest.raises(MigrationError):
        migrate(str(db_path), registry)

    # Business table and the failed migration 1 record must not exist.
    # Version 0 bootstrap is a separate successful migration and may remain.
    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='business'"
        )
        assert cursor.fetchone() is None
        cursor.execute(f"SELECT version FROM {MIGRATIONS_TABLE} WHERE version = 1")
        assert cursor.fetchone() is None
    finally:
        db.close()


def test_migration_with_semicolon_literal(temp_data_dir: Path):
    """Statement content containing a semicolon must not be split."""
    db_path = temp_data_dir / "control-center.db"

    registry = [
        Migration(
            version=0,
            name="bootstrap",
            statements=(migrations.MIGRATIONS_DDL,),
        ),
        Migration(
            version=1,
            name="insert_semicolon_value",
            statements=(
                "CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)",
                "INSERT INTO values_table (value) VALUES ('a;b')",
            ),
        ),
    ]

    migrate(str(db_path), registry)

    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM values_table")
        assert cursor.fetchone()[0] == "a;b"
    finally:
        db.close()


def test_version_zero_atomic_bootstrap_failure(temp_data_dir: Path):
    """Failure after tracking table DDL but before record insert rolls back everything."""
    db_path = temp_data_dir / "control-center.db"

    # Use a valid version 0 migration with the real DDL.
    failing = Migration(
        version=0,
        name="failing_bootstrap",
        statements=(migrations.MIGRATIONS_DDL,),
    )

    original_record = migrations._record_migration
    record_calls = []

    def explode_record(cursor, migration):
        record_calls.append(migration.version)
        raise sqlite3.OperationalError("simulated record failure")

    try:
        migrations._record_migration = explode_record
        with pytest.raises(MigrationError):
            migrate(str(db_path), migrations=[failing])
    finally:
        migrations._record_migration = original_record

    # Verify the patch was called with version 0.
    assert record_calls == [0], "record_migration must be called for version 0"

    # The database should be either absent or have no migrations table.
    if db_path.exists():
        db = Database(str(db_path))
        conn = db.connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (MIGRATIONS_TABLE,),
            )
            assert cursor.fetchone() is None, "schema_migrations must not exist after rollback"
        finally:
            db.close()


def test_list_statements_rejected():
    """Migration statements must be a tuple, not a list."""
    with pytest.raises(ValueError, match="must be a tuple"):
        Migration(version=1, name="bad", statements=["SELECT 1"])


def test_str_statements_rejected():
    """A bare string is iterable but must not be accepted as statements."""
    with pytest.raises(ValueError, match="must be a tuple"):
        Migration(version=1, name="bad", statements="SELECT 1")


def test_set_statements_rejected():
    """A set is iterable but must not be accepted as statements."""
    with pytest.raises(ValueError, match="must be a tuple"):
        Migration(version=1, name="bad", statements={"SELECT 1"})


def test_tuple_statements_accepted():
    """A proper tuple of SQL strings must work."""
    m = Migration(version=1, name="good", statements=("SELECT 1",))
    assert m.statements == ("SELECT 1",)
    assert m.checksum


def test_statements_tuple_immutability():
    """The tuple itself cannot be reassigned (frozen dataclass)."""
    m = Migration(version=1, name="good", statements=("SELECT 1",))
    with pytest.raises(Exception):  # FrozenInstanceError
        m.statements = ("SELECT 2",)


def test_registry_without_version_zero_rejected(temp_data_dir: Path):
    """A registry missing version 0 must fail before any database write."""
    db_path = temp_data_dir / "control-center.db"
    registry = [
        Migration(version=1, name="one", statements=("SELECT 1",)),
    ]
    with pytest.raises(RegistryValidationError, match="bootstrap version 0"):
        migrate(str(db_path), registry)
    # No database or directory should be created.
    assert not db_path.exists()


def test_registry_with_only_version_zero_passes(temp_data_dir: Path):
    """A registry with only version 0 must pass validation."""
    db_path = temp_data_dir / "control-center.db"
    registry = [
        Migration(version=0, name="bootstrap", statements=(migrations.MIGRATIONS_DDL,)),
    ]
    version = migrate(str(db_path), registry)
    assert version == 0
    assert db_path.exists()


def test_unknown_intermediate_version_fails_closed(temp_data_dir: Path):
    """All unknown applied versions must fail closed, not just the max."""
    db_path = temp_data_dir / "control-center.db"

    # Apply v0, v1, v2.
    v0 = Migration(version=0, name="bootstrap", statements=(migrations.MIGRATIONS_DDL,))
    v1 = Migration(
        version=1,
        name="create_business",
        statements=("CREATE TABLE business (id INTEGER PRIMARY KEY)",),
    )
    v2 = Migration(
        version=2,
        name="add_index",
        statements=("CREATE INDEX idx_business ON business(id)",),
    )
    migrate(str(db_path), migrations=[v0, v1, v2])

    # Now use a registry that only knows v0 and v2 (missing v1).
    incomplete_registry = [v0, v2]
    with pytest.raises(UnknownSchemaError):
        migrate(str(db_path), incomplete_registry)

    # Verify v1 record remains untouched.
    db = Database(str(db_path))
    conn = db.connect()
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT version FROM {MIGRATIONS_TABLE} WHERE version = 1")
        assert cursor.fetchone() is not None, "v1 record must remain"
    finally:
        db.close()

    # Runtime must enter DEGRADED.
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    runtime = app.state.control_center
    assert runtime.state == ControlCenterState.DEGRADED
    assert runtime.schema_version is None
    client = TestClient(app)
    response = client.get("/admin/api/status")
    assert response.status_code == 503
    assert response.json()["database"]["schema_version"] is None


def test_healthy_status_returns_200_and_no_store(temp_data_dir: Path):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    client = TestClient(app)
    response = client.get("/admin/api/status")
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["status"] == "ok"
    assert body["enabled"] is True
    assert body["database"]["status"] == "ok"
    assert body["database"]["schema_version"] == 0
    assert body["commit"] == os.getenv("LLMROUTER_COMMIT_SHA", "unknown")


def test_status_response_does_not_expose_paths_or_secrets(temp_data_dir: Path):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    app = create_app(config=config)
    client = TestClient(app)
    response = client.get("/admin/api/status")
    text = response.text
    assert str(temp_data_dir) not in text
    assert "control-center.db" not in text
    assert "BEGIN" not in text


def test_degraded_status_returns_503_and_no_store(temp_data_dir: Path):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    runtime = ControlCenterRuntime(config.control_center)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure")

    with patch.object(migrations, "migrate", fail):
        runtime.initialize()

    app = create_app(config=config)
    app.state.control_center = runtime
    client = TestClient(app)
    response = client.get("/admin/api/status")
    assert response.status_code == 503
    assert response.headers.get("cache-control") == "no-store"
    body = response.json()
    assert body["status"] == "degraded"
    assert body["enabled"] is True
    assert body["database"]["status"] == "unavailable"
    assert body["database"]["schema_version"] is None


def test_existing_health_and_v1_models_still_work_when_control_center_degraded(
    temp_data_dir: Path,
):
    config = OpenClawConfig()
    config.control_center = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))
    config.router = RouterConfig(strategy="random")
    app = create_app(config=config)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("simulated failure")

    with patch.object(migrations, "migrate", fail):
        runtime = ControlCenterRuntime(config.control_center)
        runtime.initialize()
        app.state.control_center = runtime

    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models").status_code == 200


def test_existing_health_contract_unchanged():
    config = OpenClawConfig()
    app = create_app(config=config)
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert "status" in body
    assert body["status"] == "ok"


def test_existing_regression_tests_still_pass_with_control_center_disabled():
    """Smoke-level: create_app with default config still serves /v1/models."""
    config = OpenClawConfig()
    app = create_app(config=config)
    client = TestClient(app)
    response = client.get("/v1/models")
    assert response.status_code == 200
