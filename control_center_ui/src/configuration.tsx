import { useEffect, useMemo, useState } from "react";

import { ApiError, api } from "./api";

type ValidationIssue = { code: string; path: string; message: string };
type ManagedSnapshot = {
  router: {
    judge_model: string | null;
    default_model: string | null;
    allowed_models: string[];
    judge_timeout_seconds: number;
    judge_max_tokens: number;
    routing_context_chars: number;
    judge_system_prompt: string;
  };
  session_routing: {
    enabled: boolean;
    ttl_seconds: number;
    rejudge_every_user_turns: number;
    allowed_rejudge_intervals: number[];
    max_entries: number;
    rejudge_on_modality_change: boolean;
    rejudge_on_backend_error: boolean;
  };
  llms: Record<
    string,
    { model: string; description: string; max_tokens: number; context_limit: number }
  >;
};
type Version = {
  version_id: number;
  version_number: number;
  parent_version_id: number | null;
  source: string;
  release_notes: string;
  created_at: string;
  is_active: boolean;
  publish_state: "active" | "pending";
  snapshot?: ManagedSnapshot;
};
type Draft = {
  draft_id: string;
  base_version_id: number;
  finalized_version_id: number | null;
  status: "editing" | "ready" | "finalized";
  revision: number;
  validation_issues: ValidationIssue[];
  release_notes: string;
  created_at: string;
  updated_at: string;
  snapshot: ManagedSnapshot;
};
type DraftSummary = Omit<Draft, "snapshot">;
type ConfigurationSummary = {
  active: Version & { snapshot: ManagedSnapshot };
  drafts: DraftSummary[];
  read_only: {
    serve: { host: string; port: number };
    router: Record<string, unknown>;
    security: {
      require_inbound_auth: boolean;
      forbidden_upstream_models: string[];
      forbidden_upstream_prefixes: string[];
    };
    models: Record<
      string,
      {
        provider: string;
        provider_type: string;
        base_url: string;
        auth_mode: string;
        chat_path: string;
        credential: {
          source: string;
          name: string | null;
          configured: boolean;
          valid_name?: boolean;
        };
      }
    >;
  };
};
type VersionPage = { items: Version[]; page: number; page_size: number; total: number };
type AuditPage = {
  items: Array<{
    audit_id: number;
    occurred_at: string;
    action: string;
    outcome: string;
    subject_type: string | null;
    subject_id: string | null;
    summary: Record<string, unknown>;
  }>;
  total: number;
};
type LabResult = {
  result: {
    selected_model: string;
    judge_status: string;
    used_default: boolean;
    judge_latency_ms: number | null;
    traffic_class: string;
    persisted: boolean;
  };
  comparison?: LabResult["result"];
};
type DiscoveryResult = {
  status: string;
  models: string[];
  reason?: string;
  combo_internal_recursion_checked?: boolean;
};
type DraftDiff = {
  changes: Array<{ path: string; before: unknown; after: unknown }>;
};

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function scalar(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string") return JSON.stringify(value);
  return String(value);
}

function toYaml(value: unknown, indent = 0): string {
  const padding = " ".repeat(indent);
  if (Array.isArray(value)) {
    if (!value.length) return `${padding}[]`;
    return value.map((item) => `${padding}- ${scalar(item)}`).join("\n");
  }
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => {
        if (item && typeof item === "object") {
          return `${padding}${key}:\n${toYaml(item, indent + 2)}`;
        }
        if (typeof item === "string" && item.includes("\n")) {
          const block = item
            .split("\n")
            .map((line) => `${" ".repeat(indent + 2)}${line}`)
            .join("\n");
          return `${padding}${key}: |-\n${block}`;
        }
        return `${padding}${key}: ${scalar(item)}`;
      })
      .join("\n");
  }
  return `${padding}${scalar(value)}`;
}

function formatValue(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function ConfigurationPage({
  csrf,
  onUnauthorized,
}: {
  csrf: string;
  onUnauthorized: () => void;
}) {
  const [summary, setSummary] = useState<ConfigurationSummary | null>(null);
  const [versions, setVersions] = useState<VersionPage | null>(null);
  const [audit, setAudit] = useState<AuditPage | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editable, setEditable] = useState<ManagedSnapshot | null>(null);
  const [releaseNotes, setReleaseNotes] = useState("");
  const [diff, setDiff] = useState<DraftDiff | null>(null);
  const [loading, setLoading] = useState(true);
  const [draftLoading, setDraftLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [labText, setLabText] = useState("");
  const [labResult, setLabResult] = useState<LabResult | null>(null);
  const [labVersionId, setLabVersionId] = useState("");
  const [discovery, setDiscovery] = useState<DiscoveryResult | null>(null);

  function handleError(reason: unknown) {
    const apiError = reason as ApiError;
    if (apiError.status === 401) onUnauthorized();
    else setError(apiError.message || "Configuration request failed");
  }

  async function loadPage(preferredDraftId?: string) {
    setLoading(true);
    setError("");
    try {
      const [nextSummary, nextVersions, nextAudit] = await Promise.all([
        api<ConfigurationSummary>("/admin/api/configuration"),
        api<VersionPage>("/admin/api/configuration/versions?page=1&page_size=25"),
        api<AuditPage>("/admin/api/audit?page=1&page_size=20"),
      ]);
      setSummary(nextSummary);
      setVersions(nextVersions);
      setAudit(nextAudit);
      const candidate = preferredDraftId || selectedId;
      const nextSelected = nextSummary.drafts.some((item) => item.draft_id === candidate)
        ? candidate
        : nextSummary.drafts[0]?.draft_id || "";
      setSelectedId(nextSelected);
      if (!nextSelected) {
        setDraft(null);
        setEditable(null);
        setDiff(null);
      }
    } catch (reason) {
      handleError(reason);
    } finally {
      setLoading(false);
    }
  }

  async function loadDraft(draftId: string) {
    if (!draftId) return;
    setDraftLoading(true);
    setError("");
    try {
      const [nextDraft, nextDiff] = await Promise.all([
        api<Draft>(`/admin/api/configuration/drafts/${encodeURIComponent(draftId)}`),
        api<DraftDiff>(
          `/admin/api/configuration/drafts/${encodeURIComponent(draftId)}/diff`,
        ),
      ]);
      setDraft(nextDraft);
      setEditable(clone(nextDraft.snapshot));
      setReleaseNotes(nextDraft.release_notes);
      setDiff(nextDiff);
    } catch (reason) {
      handleError(reason);
    } finally {
      setDraftLoading(false);
    }
  }

  useEffect(() => {
    void loadPage();
  }, []);

  useEffect(() => {
    if (selectedId) void loadDraft(selectedId);
  }, [selectedId]);

  const dirty = useMemo(
    () =>
      Boolean(
        draft &&
          editable &&
          (JSON.stringify(draft.snapshot) !== JSON.stringify(editable) ||
            draft.release_notes !== releaseNotes),
      ),
    [draft, editable, releaseNotes],
  );

  function mutate(update: (snapshot: ManagedSnapshot) => void) {
    setEditable((current) => {
      if (!current) return current;
      const next = clone(current);
      update(next);
      return next;
    });
    setNotice("");
  }

  function writeHeaders() {
    return {
      "Content-Type": "application/json",
      Origin: window.location.origin,
      "X-CSRF-Token": csrf,
    };
  }

  async function saveDraft(): Promise<Draft | null> {
    if (!draft || !editable || draft.status === "finalized") return draft;
    const nextDraft = await api<Draft>(
      `/admin/api/configuration/drafts/${encodeURIComponent(draft.draft_id)}`,
      {
        method: "PUT",
        headers: writeHeaders(),
        body: JSON.stringify({
          revision: draft.revision,
          snapshot: editable,
          release_notes: releaseNotes,
        }),
      },
    );
    setDraft(nextDraft);
    setEditable(clone(nextDraft.snapshot));
    setReleaseNotes(nextDraft.release_notes);
    setDiff(
      await api<DraftDiff>(
        `/admin/api/configuration/drafts/${encodeURIComponent(nextDraft.draft_id)}/diff`,
      ),
    );
    return nextDraft;
  }

  async function perform(action: () => Promise<void>) {
    setActionBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (reason) {
      handleError(reason);
    } finally {
      setActionBusy(false);
    }
  }

  async function createDraft() {
    await perform(async () => {
      const created = await api<Draft>("/admin/api/configuration/drafts", {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify({ release_notes: "" }),
      });
      setNotice("Draft created from the active configuration.");
      await loadPage(created.draft_id);
    });
  }

  async function validateDraft() {
    await perform(async () => {
      const current = dirty ? await saveDraft() : draft;
      if (!current) return;
      const validated = await api<Draft>(
        `/admin/api/configuration/drafts/${encodeURIComponent(current.draft_id)}/validate`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({ revision: current.revision }),
        },
      );
      setDraft(validated);
      setEditable(clone(validated.snapshot));
      setReleaseNotes(validated.release_notes);
      setNotice(
        validated.status === "ready"
          ? "Validation passed. The draft can be finalized as a pending version."
          : "Validation completed with issues.",
      );
      await loadPage(validated.draft_id);
    });
  }

  async function finalizeDraft() {
    if (!draft) return;
    await perform(async () => {
      const version = await api<Version>(
        `/admin/api/configuration/drafts/${encodeURIComponent(draft.draft_id)}/finalize`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({ revision: draft.revision }),
        },
      );
      setNotice(
        `Version ${version.version_number} is pending. Runtime configuration was not changed.`,
      );
      await loadPage(draft.draft_id);
      await loadDraft(draft.draft_id);
    });
  }

  async function activateVersion(version: Version) {
    if (!summary || version.is_active) return;
    await perform(async () => {
      const result = await api<Version & { cache_cleared: number; cache_clear_reason: string }>(
        `/admin/api/configuration/versions/${version.version_id}/activate`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({ expected_active_version_id: summary.active.version_id }),
        },
      );
      setNotice(`Activated v${result.version_number}; cleared ${result.cache_cleared} cached routes.`);
      await loadPage();
    });
  }

  async function rollbackVersion(version: Version) {
    if (!summary || version.is_active || !window.confirm(`Rollback to v${version.version_number}?`)) return;
    await perform(async () => {
      const result = await api<Version & { cache_cleared: number }>(
        `/admin/api/configuration/versions/${version.version_id}/rollback`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({
            expected_active_version_id: summary.active.version_id,
            release_notes: `Rollback to version ${version.version_number}`,
          }),
        },
      );
      setNotice(`Rollback created and activated v${result.version_number}.`);
      await loadPage();
    });
  }

  async function runDiscovery() {
    await perform(async () => {
      setDiscovery(await api<DiscoveryResult>("/admin/api/discovery/models"));
    });
  }

  async function runLab() {
    if (!labText.trim()) return;
    await perform(async () => {
      const body: Record<string, unknown> = { text: labText };
      if (labVersionId) body.version_id = Number(labVersionId);
      if (draft?.status !== "finalized" && draft?.draft_id) body.draft_id = draft.draft_id;
      setLabResult(await api<LabResult>("/admin/api/route-lab/evaluate", {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify(body),
      }));
    });
  }

  async function deleteDraft() {
    if (!draft || !window.confirm("Delete this editable draft?")) return;
    await perform(async () => {
      await api<{ status: string }>(
        `/admin/api/configuration/drafts/${encodeURIComponent(draft.draft_id)}?revision=${draft.revision}`,
        { method: "DELETE", headers: writeHeaders() },
      );
      setSelectedId("");
      setDraft(null);
      setEditable(null);
      setNotice("Draft deleted.");
      await loadPage();
    });
  }

  if (loading && !summary) {
    return <div className="state">Loading configuration…</div>;
  }

  if (!summary) {
    return error
      ? <div className="error" role="alert">{error}</div>
      : <div className="state">Configuration data is unavailable.</div>;
  }

  const active = summary.active;
  const aliases = Object.keys(active.snapshot.llms).sort();
  const editableDraft = draft?.status !== "finalized";

  return (
    <div className="configuration-page">
      {error && <div className="error" role="alert">{error}</div>}
      {notice && <div className="notice" role="status">{notice}</div>}

      <section className="cards configuration-summary" aria-label="Configuration summary">
        <Metric label="Active version" value={`v${active.version_number}`} hint={active.source} />
        <Metric label="Runtime state" value="Active" hint="Atomic activation enabled" />
        <Metric label="Drafts" value={String(summary.drafts.length)} hint="maximum 100 listed" />
        <Metric label="Pending versions" value={String(versions?.items.filter((item) => item.publish_state === "pending").length || 0)} />
      </section>

      <section className="panel">
        <div className="panel-title configuration-title">
          <div>
            <h2>Configuration drafts</h2>
            <span>Drafts are validated before atomic activation; rollback creates a new immutable version.</span>
          </div>
          <button onClick={() => void createDraft()} disabled={actionBusy}>Create draft</button>
        </div>
        <label className="field compact-field" htmlFor="draft-selector">
          <span>Selected draft</span>
          <select
            id="draft-selector"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            <option value="">No draft selected</option>
            {summary.drafts.map((item) => (
              <option key={item.draft_id} value={item.draft_id}>
                {item.status} · revision {item.revision} · {item.draft_id.slice(0, 10)}
              </option>
            ))}
          </select>
        </label>
        {!summary.drafts.length && <div className="state">No drafts. Create one from the active version.</div>}
      </section>

      {draftLoading && <div className="state">Loading draft…</div>}
      {draft && editable && !draftLoading && (
        <>
          <section className="panel">
            <div className="panel-title configuration-title">
              <div>
                <h2>Draft editor</h2>
                <span>
                  {draft.status} · revision {draft.revision}
                  {dirty ? " · unsaved changes" : " · saved"}
                </span>
              </div>
              <div className="actions">
                <button
                  className="secondary"
                  onClick={() => void perform(async () => {
                    await saveDraft();
                    setNotice("Draft saved. Validate it before finalization.");
                    await loadPage(draft.draft_id);
                  })}
                  disabled={actionBusy || !editableDraft || !dirty}
                >
                  Save draft
                </button>
                <button
                  className="secondary"
                  onClick={() => void validateDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  Validate
                </button>
                <button
                  onClick={() => void finalizeDraft()}
                  disabled={
                    actionBusy ||
                    !editableDraft ||
                    dirty ||
                    draft.status !== "ready" ||
                    !releaseNotes.trim()
                  }
                >
                  Finalize pending version
                </button>
                <button
                  className="danger"
                  onClick={() => void deleteDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  Delete draft
                </button>
              </div>
            </div>

            {draft.validation_issues.length > 0 && (
              <div className="validation" role="alert">
                <strong>Validation issues</strong>
                <ul>
                  {draft.validation_issues.map((issue) => (
                    <li key={`${issue.path}:${issue.code}`}>
                      <code>{issue.path}</code> — {issue.message}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <fieldset disabled={!editableDraft || actionBusy}>
              <legend>Router</legend>
              <div className="form-grid">
                <Field label="Judge model" htmlFor="judge-model">
                  <input
                    id="judge-model"
                    value={editable.router.judge_model || ""}
                    onChange={(event) => mutate((snapshot) => { snapshot.router.judge_model = event.target.value || null; })}
                  />
                </Field>
                <Field label="Default model" htmlFor="default-model">
                  <select
                    id="default-model"
                    value={editable.router.default_model || ""}
                    onChange={(event) => mutate((snapshot) => { snapshot.router.default_model = event.target.value || null; })}
                  >
                    <option value="">None</option>
                    {aliases.map((alias) => <option key={alias} value={alias}>{alias}</option>)}
                  </select>
                </Field>
                <NumberField label="Judge timeout seconds" id="judge-timeout" value={editable.router.judge_timeout_seconds} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_timeout_seconds = value; })} step="0.1" />
                <NumberField label="Judge token budget" id="judge-tokens" value={editable.router.judge_max_tokens} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_max_tokens = value; })} />
                <NumberField label="Routing context characters" id="routing-context" value={editable.router.routing_context_chars} onChange={(value) => mutate((snapshot) => { snapshot.router.routing_context_chars = value; })} />
              </div>
              <div className="checkbox-group" aria-label="Allowed models">
                <span>Allowed models</span>
                {aliases.map((alias) => (
                  <label key={alias}>
                    <input
                      type="checkbox"
                      checked={editable.router.allowed_models.includes(alias)}
                      onChange={(event) => mutate((snapshot) => {
                        snapshot.router.allowed_models = event.target.checked
                          ? [...snapshot.router.allowed_models, alias].sort()
                          : snapshot.router.allowed_models.filter((item) => item !== alias);
                      })}
                    />
                    {alias}
                  </label>
                ))}
              </div>
              <Field label="Judge system prompt" htmlFor="judge-prompt">
                <textarea
                  id="judge-prompt"
                  rows={8}
                  value={editable.router.judge_system_prompt}
                  onChange={(event) => mutate((snapshot) => { snapshot.router.judge_system_prompt = event.target.value; })}
                />
              </Field>
            </fieldset>

            <fieldset disabled={!editableDraft || actionBusy}>
              <legend>Session routing</legend>
              <div className="checkbox-group">
                <label><input type="checkbox" checked={editable.session_routing.enabled} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.enabled = event.target.checked; })} />Enabled</label>
                <label><input type="checkbox" checked={editable.session_routing.rejudge_on_modality_change} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.rejudge_on_modality_change = event.target.checked; })} />Rejudge on modality change</label>
                <label><input type="checkbox" checked={editable.session_routing.rejudge_on_backend_error} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.rejudge_on_backend_error = event.target.checked; })} />Rejudge on backend error</label>
              </div>
              <div className="form-grid">
                <NumberField label="TTL seconds" id="session-ttl" value={editable.session_routing.ttl_seconds} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.ttl_seconds = value; })} />
                <NumberField label="Rejudge every user turns" id="session-rejudge" value={editable.session_routing.rejudge_every_user_turns} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.rejudge_every_user_turns = value; })} />
                <NumberField label="Maximum cache entries" id="session-max" value={editable.session_routing.max_entries} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.max_entries = value; })} />
                <Field label="Allowed rejudge intervals" htmlFor="session-intervals">
                  <input
                    id="session-intervals"
                    value={editable.session_routing.allowed_rejudge_intervals.join(", ")}
                    onChange={(event) => mutate((snapshot) => {
                      snapshot.session_routing.allowed_rejudge_intervals = event.target.value
                        .split(",")
                        .map((item) => Number(item.trim()))
                        .filter((item) => Number.isInteger(item) && item > 0);
                    })}
                  />
                </Field>
              </div>
            </fieldset>

            <fieldset disabled={!editableDraft || actionBusy}>
              <legend>Configured backends</legend>
              <div className="backend-grid">
                {aliases.map((alias) => (
                  <article className="backend-card" key={alias}>
                    <h3>{alias}</h3>
                    <Field label={`${alias} upstream model`} htmlFor={`model-${alias}`}>
                      <input id={`model-${alias}`} value={editable.llms[alias].model} onChange={(event) => mutate((snapshot) => { snapshot.llms[alias].model = event.target.value; })} />
                    </Field>
                    <Field label={`${alias} description`} htmlFor={`description-${alias}`}>
                      <textarea id={`description-${alias}`} rows={3} value={editable.llms[alias].description} onChange={(event) => mutate((snapshot) => { snapshot.llms[alias].description = event.target.value; })} />
                    </Field>
                    <div className="form-grid">
                      <NumberField label={`${alias} max tokens`} id={`max-tokens-${alias}`} value={editable.llms[alias].max_tokens} onChange={(value) => mutate((snapshot) => { snapshot.llms[alias].max_tokens = value; })} />
                      <NumberField label={`${alias} context limit`} id={`context-${alias}`} value={editable.llms[alias].context_limit} onChange={(value) => mutate((snapshot) => { snapshot.llms[alias].context_limit = value; })} />
                    </div>
                    <small>
                      {summary.read_only.models[alias]?.provider || "unknown provider"} · credential {summary.read_only.models[alias]?.credential.configured ? "configured" : "missing"}
                    </small>
                  </article>
                ))}
              </div>
            </fieldset>

            <Field label="Release notes" htmlFor="release-notes">
              <textarea
                id="release-notes"
                rows={4}
                maxLength={2000}
                value={releaseNotes}
                disabled={!editableDraft || actionBusy}
                onChange={(event) => setReleaseNotes(event.target.value)}
              />
            </Field>
          </section>

          <div className="configuration-columns">
            <section className="panel">
              <div className="panel-title"><h2>Stable diff</h2><span>{diff?.changes.length || 0} changes</span></div>
              {!diff?.changes.length ? <div className="state">No saved differences from the base version.</div> : (
                <div className="diff-list">
                  {diff.changes.map((change) => (
                    <article key={change.path}>
                      <code>{change.path}</code>
                      <div><span>Before</span><pre>{formatValue(change.before)}</pre></div>
                      <div><span>After</span><pre>{formatValue(change.after)}</pre></div>
                    </article>
                  ))}
                </div>
              )}
            </section>
            <section className="panel">
              <div className="panel-title"><h2>Managed YAML</h2><span>Read only</span></div>
              <pre className="yaml-view" aria-label="Managed configuration YAML">{toYaml(editable)}</pre>
            </section>
          </div>
        </>
      )}

      <div className="configuration-columns">
        <section className="panel">
          <div className="panel-title"><h2>Version history</h2><span>Immutable snapshots</span></div>
          {!versions?.items.length ? <div className="state">No configuration versions.</div> : (
            <div className="table-wrap"><table className="compact-table"><thead><tr><th>Version</th><th>State</th><th>Source</th><th>Created</th><th>Release notes</th><th>Actions</th></tr></thead><tbody>
              {versions.items.map((item) => <tr key={item.version_id}>
                <td>v{item.version_number}</td>
                <td><span className={`status ${item.publish_state}`}>{item.publish_state}</span></td>
                <td>{item.source}</td>
                <td>{new Date(item.created_at).toLocaleString()}</td>
                <td>{item.release_notes || "—"}</td>
                <td><div className="actions compact-actions">
                  {!item.is_active && <button className="secondary" disabled={actionBusy} onClick={() => void activateVersion(item)}>Activate</button>}
                  {!item.is_active && <button className="danger" disabled={actionBusy} onClick={() => void rollbackVersion(item)}>Rollback</button>}
                </div></td>
              </tr>)}
            </tbody></table></div>
          )}
        </section>
        <section className="panel">
          <div className="panel-title"><h2>Recent audit</h2><span>No request or prompt bodies</span></div>
          {!audit?.items.length ? <div className="state">No recent management events.</div> : (
            <div className="audit-list">
              {audit.items.map((item) => <article key={item.audit_id}>
                <div><strong>{item.action}</strong><span className={`status ${item.outcome}`}>{item.outcome}</span></div>
                <small>{new Date(item.occurred_at).toLocaleString()} · {item.subject_type || "system"}</small>
                <code>{JSON.stringify(item.summary)}</code>
              </article>)}
            </div>
          )}
        </section>
      </div>

      <div className="configuration-columns">
        <section className="panel">
          <div className="panel-title"><h2>Route Lab</h2><span>Admin test only; input is not persisted</span></div>
          <Field label="Task text" htmlFor="route-lab-text">
            <textarea id="route-lab-text" rows={5} value={labText} onChange={(event) => setLabText(event.target.value)} placeholder="Enter a temporary routing task" />
          </Field>
          <div className="form-grid">
            <Field label="Version ID (optional)" htmlFor="route-lab-version">
              <input id="route-lab-version" value={labVersionId} onChange={(event) => setLabVersionId(event.target.value)} inputMode="numeric" />
            </Field>
            <div className="actions"><button onClick={() => void runLab()} disabled={actionBusy || !labText.trim()}>Evaluate route</button></div>
          </div>
          {labResult && <div className="notice">Selected <strong>{labResult.result.selected_model}</strong> · {labResult.result.judge_status} · {labResult.result.judge_latency_ms == null ? "no judge" : `${labResult.result.judge_latency_ms.toFixed(1)} ms`} · persisted: no</div>}
        </section>
        <section className="panel">
          <div className="panel-title"><h2>Upstream model discovery</h2><span>Read only; Combo recursion is not verified</span></div>
          <button className="secondary" onClick={() => void runDiscovery()} disabled={actionBusy}>Check configured router models</button>
          {discovery && <div className={discovery.status === "ok" ? "notice" : "validation"}>
            <strong>{discovery.status}</strong>
            {discovery.models.length > 0 ? <ul>{discovery.models.map((model) => <li key={model}><code>{model}</code></li>)}</ul> : <p>{discovery.reason || "No models returned."}</p>}
            <small>Model ID existence does not prove Combo internal recursion safety.</small>
          </div>}
        </section>
      </div>
    </div>
  );
}

function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: React.ReactNode;
}) {
  return <label className="field" htmlFor={htmlFor}><span>{label}</span>{children}</label>;
}

function NumberField({
  label,
  id,
  value,
  onChange,
  step = "1",
}: {
  label: string;
  id: string;
  value: number;
  onChange: (value: number) => void;
  step?: string;
}) {
  return <Field label={label} htmlFor={id}><input id={id} type="number" step={step} value={value} onChange={(event) => onChange(event.currentTarget.valueAsNumber)} /></Field>;
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <article className="metric"><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</article>;
}
