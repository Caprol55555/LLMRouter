# Control Center Administrator Access and Dashboard

> Phase: 2
> Date: 2026-08-20

## Access boundary

The Dashboard is served at `/dashboard` only when Control Center is enabled.
Management data remains under `/admin/api/*` and requires a dedicated
administrator session. The ordinary `/v1/*` bearer key is never accepted as an
administrator credential.

`LLMROUTER_ADMIN_TOKEN` is read from the process environment. The application
keeps only its SHA-256 digest and compares candidate digests with
`hmac.compare_digest`. The token is not written to SQLite, configuration,
responses, logs, frontend storage, or build output. When Control Center is
disabled, no administrator token is required.

## Browser session

Successful login creates random session and CSRF tokens. The raw session token
is sent only in an HttpOnly, `SameSite=Strict` cookie scoped to `/admin`. The
server stores a digest of the session token and an in-memory CSRF record with a
bounded lifetime. Sessions are process-local, expire automatically, and are
revoked on logout. Active sessions and tracked login-failure clients also have
fixed capacity limits; the oldest records are evicted before memory can grow
without bound.

`GET /admin/api/session` lets same-origin JavaScript recover the existing CSRF
token after reload or in a new tab. Cross-origin callers cannot read the
response because no CORS policy is enabled. Administrator write requests must
match the exact loopback Origin host and port and provide the CSRF header.

Login failures use a uniform message. A bounded per-client failure window
returns HTTP 429 after the configured threshold. Request bodies are size
limited and never echoed.

## Read-only API

- `GET /admin/api/status`: sanitized Control Center state.
- `GET /admin/api/session`: session/CSRF recovery.
- `GET /admin/api/overview`: 1 hour, 24 hour, and 7 day routing summaries.
- `GET /admin/api/requests`: bounded pagination and structured filters.
- `GET /admin/api/health`: database and telemetry writer self-monitoring.
- `GET /admin/api/runtime`: strategy, configured model labels, cache size,
  schema version, and commit SHA.

Phase 2 exposes no configuration mutation, export, prompt/message view, Route
Lab, deployment, rollback, file access, command execution, or arbitrary URL
fetching.

## Response security

Management API responses use `Cache-Control: no-store`. Dashboard HTML also
uses `no-store`; fingerprinted assets use one-year immutable caching. Dashboard
and API responses include CSP, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, and frame
denial. No broad CORS middleware is installed. The server dependency set also
sets a Starlette floor that includes the patched `FileResponse` Range handling.

## Frontend delivery

The frontend uses React, TypeScript, and Vite. It displays only structured
telemetry, with loading, unauthorized, empty, and error states; bounded request
pagination; traffic/model/status/time filters; local/UTC time display; and a
narrow-screen layout. Session identifiers remain HttpOnly and the CSRF token is
kept only in React memory; reloads and new tabs recover it from the authenticated
session endpoint instead of browser storage.

GitHub Actions runs component tests and builds the frontend before Python tests.
Docker uses a separate Node builder stage and copies only static assets into the
Python runtime stage; the production image contains no Node runtime or frontend
package manager.
