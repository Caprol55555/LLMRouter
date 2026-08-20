# Control Center Operations Runbook

## SQLite checks

With an authenticated administrator session, request
`GET /admin/api/maintenance/integrity`. A healthy response has
`status: "ok"`, `integrity_check: ["ok"]`, and no foreign-key violations.
The endpoint is read-only and does not run `VACUUM`, migrations, or cleanup.

## Backup and restore

The Control Center database is sensitive operational data. Stop the server or
flush telemetry first, then use SQLite's online backup command against the
configured `/data/control-center.db` file. Store backups outside the container
with restrictive permissions and verify them on a disposable copy before a
restore. Restore by replacing the database while the server is stopped, then
start the server and run the integrity endpoint before enabling administration.

Do not expose backup paths, Docker sockets, shell commands, or arbitrary file
writes through the admin UI. The application only provides read-only integrity
checks; backup transport and retention remain an operator responsibility.

CI also builds the server image and runs a read-only-root smoke with `/data`
mounted writable. That smoke checks `/health`, database creation, and that the
runtime image cannot import `torch`, `transformers`, or `gradio`.

## Failure recovery

- If Control Center initialization fails, inference endpoints remain available
  and the runtime reports `degraded`.
- If activation candidate construction or SQLite commit fails, the previous
  runtime bundle and active pointer remain in use.
- If telemetry cannot write, events are dropped with bounded counters and
  inference continues.
- A rollback creates and activates a new immutable version; historical rows are
  never edited.

## Resource and dependency boundaries

The server image installs only `requirements-server.txt` and builds static UI
assets in a disposable Node builder stage. The runtime image contains no Node,
Torch, Transformers, Gradio, or ComfyUI dependencies. The Control Center
database is written only under the configured data directory.
