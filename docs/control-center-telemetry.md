# Control Center Routing Telemetry

> Phase: 1
> Date: 2026-08-20

## Scope

Phase 1 records privacy-safe routing decisions without adding a management HTTP
API. Production `/v1/chat/completions` and `/v1/chat/ws` traffic is classified
internally as `production`. The event model also supports `admin_test` and
`deployment_smoke` for later internal callers; ordinary clients cannot select a
traffic class through a header or request field.

## Correlation and counting

Every accepted outer chat request receives a random request ID and produces:

1. one `request_started` event;
2. zero or one `judge_completed` event, only when an upstream judge HTTP call
   was actually attempted;
3. one `request_completed` event for success, error, or disconnect.

Session cache hits and explicit model requests produce no judge event. During
single-flight routing, only the request that executes the selector records the
judge call; waiters are recorded as cache hits. The final event is the source
for hourly request aggregates, so start/judge events do not inflate outer
request counts.

## Stored fields

Events may contain only:

- random event/request IDs and UTC timestamp;
- traffic class and HTTP/WebSocket transport;
- requested policy, cache status, and structured rejudge reason;
- structured judge status, selected model, fallback flag, and final status;
- judge, first-byte, and total latency;
- prompt/completion/total token counts only when reliably returned upstream;
- future configuration version ID;
- at most 12 hexadecimal characters from the already SHA-256-derived session
  cache key.

The schema and event constructor do not accept prompt text, messages, response
bodies, tool calls or arguments, headers, Authorization, API keys, cookies,
environment variables, file paths, SQL, or raw exception messages. Error
categories use bounded status/class labels only.

## Write path and failure isolation

The request path calls `put_nowait` on a bounded queue. A lazily started daemon
thread owns the SQLite write connection, drains bounded batches, and commits
raw events plus hourly aggregate updates in one transaction.

- Queue full: discard the new event and increment `dropped_events`.
- SQLite error/lock/unwritable storage: roll back the batch, count its events as
  dropped, increment `database_errors`, retain only the exception class name,
  close the failed connection, and retry on a later batch.
- Shutdown: attempt flush and thread stop inside one caller-supplied time
  budget. Timeout cannot hold application shutdown indefinitely.

No telemetry failure is raised into routing or backend response handling.
Control Center disabled/degraded state bypasses event construction and performs
no telemetry I/O.

## Aggregation and retention

Only `request_completed` events update `routing_aggregates_hourly`. Dimensions
are UTC hour, traffic class, requested policy, selected model, and final status.
Counters include outer requests, actual judge calls, cache hits, fallback,
errors, latency samples, and reliable token samples.

Defaults:

- queue capacity: 2048 events;
- batch size: 100 events;
- flush interval: 1 second;
- raw event retention: 7 days;
- hourly aggregate retention: 90 days.

Cleanup runs in the writer transaction at most once per hour. All limits are
validated at startup and are bounded. The internal query service caps result
sizes and opens SQLite with `mode=ro` plus `PRAGMA query_only=ON`.

## Deferred interfaces

Phase 1 exposes no telemetry list, overview, export, or filtering endpoint.
Authenticated read-only management APIs and Dashboard views belong to phase 2.
