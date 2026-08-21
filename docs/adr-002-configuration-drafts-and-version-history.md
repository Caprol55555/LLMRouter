# ADR-002: Configuration Drafts and Immutable Version History

> Status: accepted
> Phase: 3
> Date: 2026-08-20

## Context

Operators need to edit and validate routing configuration before production
activation. Stage 3 must establish durable drafts and version history while
preserving the current YAML-backed inference behavior until stage 4 introduces
atomic runtime snapshots.

## Decision

Persist a closed, secret-free managed configuration snapshot in SQLite. Import
the YAML projection as immutable version 1 only when the database does not yet
contain an active configuration pointer.

The managed snapshot contains:

- router judge/default/allowed models, timeout, token budget, context size, and
  system prompt;
- session routing enablement, TTL, rejudge policy, bounded cache size, and
  invalidation triggers;
- each already-configured backend alias's upstream model ID, description,
  maximum output tokens, and context limit.

It does not contain provider credentials, credential values, base URLs,
listener settings, filesystem paths, Docker settings, or arbitrary provider
definitions.

## Storage model

Migration version 2 creates:

- `configuration_versions`: immutable numbered snapshots and release notes;
- `configuration_state`: the singleton active version pointer;
- `configuration_drafts`: mutable optimistic-revision workspaces;
- `admin_audit_events`: bounded sanitized management audit summaries.

SQLite triggers reject `UPDATE` and `DELETE` on `configuration_versions`.
Draft finalization inserts a new version with source `draft`, records its parent
version, and marks the draft finalized. The active pointer is not modified.

## Lifecycle

```text
active immutable version
→ create editable draft at revision 1
→ save normalized snapshot and increment revision
→ validate structure and routing semantics
→ ready draft with release notes
→ finalize immutable pending version
```

Any save returns a draft to `editing`. A stale revision receives a conflict.
Finalized drafts cannot be updated or deleted. Invalid drafts remain editable
and cannot be finalized.

`publish_state` is derived for API display: the pointer target is `active` and
all other immutable versions are `pending`. Stage 3 has no endpoint or UI
control that changes the pointer.

## Validation and privacy

- The root, router, session, and per-backend objects use exact key sets.
- Backend aliases must exactly match aliases already configured by the server;
  the management plane cannot add providers or outbound destinations.
- Editable strings have explicit length limits and reject environment
  substitutions and high-confidence secret formats.
- Credential metadata returns only source, a syntactically validated
  environment variable name where applicable, and configured/not-configured
  state. Environment values are never read into API responses or snapshots.
- Upstream model IDs reject `auto`, `auto:*`, `lr/*`, and server-configured
  forbidden models or prefixes.
- The default model must be allowed; token budgets cannot exceed context limits;
  rejudge values must satisfy the configured interval set.
- Audit summaries use bounded scalar fields and never store configuration
  bodies, prompts, request content, credentials, cookies, or tokens.

## Stable diff

Snapshots are normalized before storage. Object keys and set-like arrays are
canonicalized, and persisted JSON is serialized with sorted keys. Diff output
is path-sorted, so mapping order and allowlist ordering do not create noise.

## Runtime isolation

The configuration repository belongs to `ControlCenterRuntime` and remains off
the inference hot path. Creating, editing, validating, deleting, or finalizing
a draft does not mutate `OpenClawConfig`, `OpenClawRouter`, `LLMBackend`, or the
session route cache.

If configuration initialization fails after telemetry startup, the telemetry
writer is stopped before the runtime enters `DEGRADED`. `/health` and `/v1/*`
remain available, while authenticated configuration APIs return a sanitized
unavailable response.

## Consequences

- Positive: stage 4 receives a durable, validated, immutable input for atomic
  activation without retrofitting draft semantics.
- Positive: YAML remains a deterministic first-start and disaster-recovery
  baseline without repeatedly overwriting database state.
- Positive: secret and outbound-destination boundaries are enforced in both the
  data model and HTTP API.
- Negative: pending versions cannot be activated or rolled back in stage 3;
  this is deliberate and visible in the UI.
