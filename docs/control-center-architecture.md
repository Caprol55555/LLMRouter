# LLMRouter Control Center Architecture Boundaries

> Phase: 0–2 (control-plane skeleton, telemetry, and read-only dashboard)
> Date: 2026-08-20

## Purpose

This document defines the isolation boundary between the existing LLMRouter
inference plane (`/v1/*`, `/health`, routing strategies, session routing) and the
new Control Center management plane.

## Deployment shape

The Control Center lives in the same Python package and the same Docker image as
the OpenClaw Router. It is **opt-in** via YAML:

```yaml
control_center:
  enabled: false
  data_dir: /data
```

When disabled, the server behaves exactly as before: no SQLite database, no data
directory creation, no extra runtime dependencies, no administrator token
required.

## Dependency direction

```text
inference plane (server.py, routers.py, session_routing.py, LLMBackend)
        ^
        | only thin wiring in server.py
        v
control plane (openclaw_router/control_center/)
        ^
        | reads config object
        v
openclaw_router.config.OpenClawConfig / ControlCenterConfig
```

Rules:

- `routers.py`, `session_routing.py`, and the LLM backend must not import from
  `control_center`; they expose generic routing detail/observer hooks only.
- `control_center` may import configuration dataclasses and the Python standard
  library only.
- `server.py` owns the thin integration: it registers
  `app.state.control_center`, mounts the authenticated management API and
  Dashboard assets, and submits narrow structured events through a non-blocking
  callback.
- The inference hot path never opens, queries, or waits for SQLite. Its only
  telemetry operation is bounded in-memory `put_nowait`.

## Default-off guarantee

When `control_center.enabled` is false or absent:

- No directory or file is created under `data_dir`.
- `LLMROUTER_ADMIN_TOKEN` is not required.
- `/health`, `/v1/*`, WebSocket, SSE, tool calls, and session routing semantics
  are unchanged.
- The Control Center runtime object is created but performs no I/O.

## Failure isolation

If `control_center.enabled` is true but database initialization or migrations
fail:

- The main FastAPI application still starts.
- `/health` and `/v1/*` remain available.
- An authenticated `/admin/api/status` request returns HTTP 503 with
  `status: degraded`.
- The error is logged without exposing absolute paths, SQL, environment
  variables, or raw exceptions.

## Data directory and read-only root filesystem

- The database filename is fixed to `control-center.db`; it is a class-level
  constant (`ClassVar[str]`) and cannot be overridden by YAML, the constructor,
  or dataclass serialization.
- `db_path` is always `<data_dir>/control-center.db`; it cannot escape
  `data_dir` through `..` or a custom filename.
- `data_dir` must be an absolute path; relative paths are rejected at startup.
- The application root filesystem remains read-only compatible.
- The Docker image creates `/data` and assigns it to the `llmrouter` user.
- Enabling Control Center only requires mounting a writable volume at `/data`.

## Status contract

Three states:

1. **Disabled** (`enabled: false`) → HTTP 404, stable error code
   `control_center_disabled`, `Cache-Control: no-store`, no database created.
2. **Healthy** (`enabled: true` and DB/migrations OK) → HTTP 200, `status: ok`,
   `database.status: ok`, current schema version, `LLMROUTER_COMMIT_SHA` or
   `unknown`, `Cache-Control: no-store`.
3. **Degraded** (`enabled: true` and DB/migrations failed) → HTTP 503,
   `status: degraded`, `database.status: unavailable`,
   `database.schema_version: null`, `Cache-Control: no-store`.

The response never contains the database path, configuration body, secrets, or
raw exceptions.

Phase 2 preserves the disabled 404 contract before authentication. When the
Control Center is enabled, all status and management data routes are mounted on
a protected router and require the dedicated administrator session. Login and
logout remain explicit public/session-lifecycle routes; logout additionally
requires exact loopback Origin matching and a CSRF token.

## What is intentionally absent through phase 2

No configuration drafts, snapshots, diff, publish, hot update, rollback,
`RuntimeSnapshot`, Route Lab, A/B testing, test sets, export, 9router
`/v1/models` calls, or phase 3–5 business tables. Phase 2 adds only an
authenticated read-only Dashboard and management API.

## Migration rules

- Migrations use Python standard library `sqlite3` only.
- Migrations are declared as `Migration(version: int, name: str, statements:
  tuple[str, ...])`. Each migration holds one or more complete SQL statements;
  the runner executes each statement with `cursor.execute`. It never splits a
  single SQL string on `;`, so string literals containing `;` are safe.
- `statements` must be a `tuple[str, ...]` — not a list, set, or string. The
  frozen dataclass rejects non-tuple containers at construction time.
- Migrations are strictly ordered by integer version. The registry is validated
  and sorted before any database write; an invalid registry (duplicate version,
  non-int version, empty name, empty statements, non-tuple statements, missing
  version 0, etc.) raises a stable `MigrationError` subclass and leaves the
  database untouched.
- Version `0` is reserved for bootstrapping the `schema_migrations` tracking
  table itself.
- Each migration runs inside a transaction (`BEGIN IMMEDIATE ... COMMIT`).
  Version 0 creates the tracking table and records itself in the same
  transaction, so a failure between DDL and the record insert rolls back both.
- `schema_migrations` records `version`, `name`, `checksum`, and `applied_at`
  (UTC ISO-8601).
- Re-running migrations is a stable no-op.
- Checksum drift of an already-applied migration is rejected.
- Failed migrations are rolled back and not recorded as successful.
- The registry must include exactly one version 0 (the bootstrap migration).
  Missing or duplicate version 0 is rejected before any database or directory
  write.
- If the database contains any applied version not present in the current
  registry, the runner fails closed with `UnknownSchemaError` (not just the
  max version). No unknown record is modified or deleted, the runtime enters
  DEGRADED, and `/admin/api/status` returns 503 with `schema_version: null`.

## Phase 1 telemetry extension

Phase 1 adds `routing_events`, `routing_aggregates_hourly`, a bounded queue, and
a single SQLite writer. Queue full, SQLite failures, and shutdown timeout only
degrade observability. They never fail `/v1/*`. The read-only query service is
internal and has no HTTP route until phase 2. See
`docs/control-center-telemetry.md` for event and retention contracts.
