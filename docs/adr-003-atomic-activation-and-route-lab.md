# ADR-003: Atomic Activation, Rollback, and Route Lab

> Status: accepted
> Phase: 4-5
> Date: 2026-08-20

## Decision

Configuration activation builds a complete candidate runtime bundle before
changing the SQLite active pointer. The pointer update uses an expected active
version ID under `BEGIN IMMEDIATE`; stale administrators receive a conflict.
Once the transaction succeeds, the process swaps one immutable bundle reference
containing the config, router, backend, and session cache. Requests capture that
bundle once and cannot observe a mixed configuration during streaming or tool
calls.

Routing-semantic changes create a fresh cache and report the number of entries
discarded. Description-only changes reuse the existing cache. A rollback copies
the selected historical snapshot into a new immutable version, activates that
new version, and records the target and parent in audit metadata.

## Route Lab

Route Lab accepts temporary task text, an immutable version or draft, and an
optional comparison version. It disables session caching, labels the operation
`admin_test`, and never stores the task text. Discovery calls only the fixed
server-configured router `/models` endpoint and returns model IDs without
credentials or arbitrary URL support. The UI explicitly distinguishes model
existence from Combo-internal recursion safety.

## Failure behavior

Candidate construction and validation happen before the database write. If
either fails, the active pointer and runtime bundle remain unchanged. If the
database transaction fails, no in-memory swap occurs. Management responses are
sanitized and all writes require the existing administrator session, Origin,
and CSRF checks.
