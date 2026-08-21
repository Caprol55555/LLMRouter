# ADR-001: Control Center SQLite and Runtime State

> Status: accepted  
> Phase: 0  
> Date: 2026-08-20

## Context

LLMRouter needs a small, durable store for Control Center state. The store will
later hold routing telemetry, configuration versions, and audit records. Phase 0
only establishes the migration runner and the `/admin/api/status` endpoint.

## Decision

Use Python standard library `sqlite3` with a single file at
`<data_dir>/control-center.db`.

### Why SQLite

- Already present in the Python runtime; no extra dependency.
- Sufficient for a single-node, single-process management plane.
- WAL mode gives acceptable concurrency between a single writer and readers.
- Matches the "lightweight" requirement in the development plan.

### Why standard library only

- `requirements-server.txt` intentionally stays small and free of ORMs.
- No Alembic/SQLAlchemy/aiosqlite keeps the production image minimal.
- Synchronous `sqlite3` is acceptable because Control Center I/O is off the
  inference hot path. Phase 1 confines writes to one background worker and
  opens query connections in SQLite read-only/query-only mode.

## Configuration

```yaml
control_center:
  enabled: false
  data_dir: /data
```

- `enabled` defaults to `false`.
- `data_dir` defaults to `/data` and must be an absolute path.
- The database filename is fixed to `control-center.db`; it is declared as a
  `ClassVar[str]` on `ControlCenterConfig` so it cannot be overridden by the
  constructor, YAML, or dataclass serialization.
- `db_path` is always `<data_dir>/control-center.db`; startup validation rejects
  any resolved path that would escape `data_dir`.

## Database pragmas

Every connection enables:

- `PRAGMA foreign_keys = ON`
- `PRAGMA busy_timeout = 5000`
- `PRAGMA journal_mode = WAL`

## Migration design

Migrations are Python dataclass instances declared in
`openclaw_router/control_center/migrations.py`:

```python
@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
```

- Versions are non-negative integers. `bool`, `str`, `float`, and negative
  values are rejected. Duplicate versions are rejected.
- `name` must be a non-empty string.
- `statements` must be a `tuple[str, ...]` — not a list, set, string, or other
  iterable. The frozen dataclass rejects non-tuple containers at construction
  time so that checksums cannot change via external mutation of the container.
- The tuple must be non-empty, and each element must be a non-empty string.
- The runner executes each statement with `cursor.execute`; it does **not**
  split SQL on semicolons. This keeps string literals containing `;` safe and
  avoids the ambiguity of parsing SQL without a real tokenizer.
- The runner validates and sorts the registry before any database write. An
  invalid registry raises a stable `MigrationError` subclass and leaves the
  database untouched. The registry must include exactly one version 0 (the
  bootstrap migration that creates `schema_migrations`); missing or duplicate
  version 0 is rejected before any directory or database is created.
- The runner applies migrations in ascending version order.
- Each migration runs inside `BEGIN IMMEDIATE ... COMMIT`.
- `schema_migrations` stores `version`, `name`, `checksum`, and `applied_at`.
- `checksum` is SHA-256 of the canonical SQL representation (statements joined
  by `;\n` and terminated with `;`); drift in an already-applied migration
  causes `ChecksumMismatchError`.
- Re-running `migrate()` is a stable no-op.
- A failed migration rolls back the active transaction and is not recorded.

### Version 0 bootstrap

Version 0 is special: it creates the `schema_migrations` tracking table itself.
The DDL and the version-0 record are written in the same explicit transaction.
If a failure occurs between the `CREATE TABLE` and the `INSERT`, the entire
transaction rolls back, leaving no half-initialized tracking table. This avoids
a separate out-of-transaction `_create_migrations_table()` step.

### Unknown / future schema versions

If the database contains any `schema_migrations` row whose version is not
present in the current code registry, `migrate()` raises
`UnknownSchemaError` before applying or modifying anything. This is not
limited to the maximum applied version — an intermediate version removed from
the registry also triggers fail-closed. No unknown row is altered.
`ControlCenterRuntime` catches the error, enters `DEGRADED`, and
`/admin/api/status` returns HTTP 503 with `database.schema_version: null`. This
prevents an older code version from misreporting the schema as version 0 or
running against an incompatible newer schema.

## Runtime state

`ControlCenterRuntime` holds:

- `config`: the parsed `ControlCenterConfig`
- `state`: `DISABLED`, `OK`, or `DEGRADED`
- `schema_version`: current schema version when OK, otherwise `None`
- `last_error`: sanitized error message
- phase 1 telemetry writer and read-only query service references when healthy

Initialization is failure-isolated: an exception during migration is caught, the
state is set to `DEGRADED`, and the main FastAPI application continues to serve
`/health` and `/v1/*`.

## Security and privacy

- Status responses never include absolute paths, SQL, configuration bodies,
  environment variables, or raw exceptions.
- All `/admin/api/status` responses include `Cache-Control: no-store`.
- Logs only expose the exception class name, not message or traceback.
- No secrets are stored or required in phase 0.
- Phase 1 routing events contain only bounded structural labels, timings,
  counters, token usage reported by upstream, and a short prefix of an already
  SHA-256-derived session key. Prompt/message/tool content, headers, cookies,
  API keys, and exception messages are excluded.

## Read-only root filesystem

- The application image does not need to write to `/app`, the source tree, or
  the configuration directory.
- `/data` is created in the image and owned by the `llmrouter` user.
- When Control Center is disabled, the image can still run with `--read-only`
  because no write path is exercised.
- When enabled, administrators mount a writable volume at `/data`.

## Consequences

- Positive: minimal dependency footprint, small image, simple migration model.
- Positive: phase 0 behavior is fully isolated from inference.
- Negative: telemetry can be dropped during overload or database failure. This
  is deliberate: phase 1 uses a bounded in-memory queue, a single batch writer,
  retention limits, and explicit dropped/error counters to protect inference.

## Alternatives considered

- PostgreSQL/MySQL: rejected; overkill for single-node deployment and adds ops
  burden.
- SQLAlchemy + Alembic: rejected; adds dependencies not needed for a handful of
  tables and a single writer.
- aiosqlite: rejected; current phase has no async SQLite requirement.
