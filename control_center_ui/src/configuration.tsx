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
  name: string;
  is_active: boolean;
  snapshot: ManagedSnapshot;
};
type DraftSummary = Omit<Draft, "snapshot">;
type ConfigurationSummary = {
  active: Version & { snapshot: ManagedSnapshot };
  drafts: DraftSummary[];
  active_drafts?: DraftSummary[];
  model_catalog?: string[];
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
  view = "configuration",
}: {
  csrf: string;
  onUnauthorized: () => void;
  view?: "configuration" | "activity" | "route-lab";
}) {
  const [summary, setSummary] = useState<ConfigurationSummary | null>(null);
  const [versions, setVersions] = useState<VersionPage | null>(null);
  const [audit, setAudit] = useState<AuditPage | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftName, setDraftName] = useState("");
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
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [selectedDiscoveredModels, setSelectedDiscoveredModels] = useState<string[]>([]);
  const [newAlias, setNewAlias] = useState("");

  function handleError(reason: unknown) {
    const apiError = reason as ApiError;
    if (apiError.status === 401) onUnauthorized();
    else setError(apiError.message || "配置请求失败");
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
      setSelectedDiscoveredModels(nextSummary.model_catalog || []);
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
      setDraftName(nextDraft.name || "");
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
    () => Boolean(
      draft && editable && (
        JSON.stringify(draft.snapshot) !== JSON.stringify(editable) ||
        draft.release_notes !== releaseNotes ||
        (draft.name || "") !== draftName
      ),
    ),
    [draft, editable, releaseNotes, draftName],
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
          name: draftName,
        }),
      },
    );
      setDraft(nextDraft);
      setDraftName(nextDraft.name || "");
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
        body: JSON.stringify({ release_notes: "", name: "未命名草稿" }),
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
      setDraftName(validated.name || "");
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

  async function saveModelCatalog() {
    await perform(async () => {
      await api("/admin/api/configuration/model-catalog", {
        method: "PUT",
        headers: writeHeaders(),
        body: JSON.stringify({ models: selectedDiscoveredModels }),
      });
      setModelMenuOpen(false);
      setNotice("模型清单已保存。");
    });
  }

  function addDraftModel() {
    const alias = newAlias.trim();
    if (!alias || !editable || editable.llms[alias]) return;
    mutate((snapshot) => {
      snapshot.llms[alias] = { model: alias, description: "", max_tokens: 4096, context_limit: 32768 };
      snapshot.router.allowed_models = [...new Set([...snapshot.router.allowed_models, alias])].sort();
    });
    setNewAlias("");
  }

  function removeDraftModel(alias: string) {
    if (!editable || Object.keys(editable.llms).length <= 1) return;
    mutate((snapshot) => {
      delete snapshot.llms[alias];
      snapshot.router.allowed_models = snapshot.router.allowed_models.filter((item) => item !== alias);
      if (snapshot.router.default_model === alias) snapshot.router.default_model = null;
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
    return <div className="state">正在加载配置…</div>;
  }

  if (!summary) {
    return error
      ? <div className="error" role="alert">{error}</div>
      : <div className="state">配置数据不可用。</div>;
  }

  const active = summary.active;
  const aliases = Object.keys(editable?.llms || active.snapshot.llms).sort();
  const editableDraft = draft?.status !== "finalized";

  return (
    <div className="configuration-page">
      {error && <div className="error" role="alert">{error}</div>}
      {notice && <div className="notice" role="status">{notice}</div>}

      {view === "configuration" && <section className="cards configuration-summary" aria-label="配置摘要">
        <Metric label="当前版本" value={`v${active.version_number}`} hint={active.source} />
        <Metric label="运行状态" value="已启用" hint="支持原子启用" />
        <Metric label="草稿数量" value={String(summary.drafts.length)} hint="最多显示 100 条" />
        <Metric label="待发布版本" value={String(versions?.items.filter((item) => item.publish_state === "pending").length || 0)} />
      </section>}

      {view === "configuration" && <section className="panel">
        <div className="panel-title configuration-title">
          <div>
            <h2>配置草稿</h2>
          <span>草稿需先校验，再进行原子启用；回滚会生成新的不可变版本。</span>
          </div>
          <button onClick={() => void createDraft()} disabled={actionBusy}>新建草稿</button>
        </div>
        <label className="field compact-field" htmlFor="draft-selector">
          <span>当前草稿</span>
          <select
            id="draft-selector"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            <option value="">未选择草稿</option>
            {summary.drafts.map((item) => (
              <option key={item.draft_id} value={item.draft_id}>
                {item.name || "未命名草稿"} · {item.status} · 修订 {item.revision}{item.is_active ? " · 已启用" : ""}
              </option>
            ))}
          </select>
        </label>
        {!summary.drafts.length && <div className="state">暂无草稿，请从当前版本新建。</div>}
      </section>}

      {view === "configuration" && <section className="panel">
        <div className="panel-title"><h2>模型清单</h2><span>发现后勾选可用于草稿的模型</span></div>
        <button className="secondary" onClick={() => { setModelMenuOpen(true); void runDiscovery(); }} disabled={actionBusy}>发现可用模型</button>
        {modelMenuOpen && <div className="model-menu"><input placeholder="模糊搜索模型" value={modelSearch} onChange={(event) => setModelSearch(event.target.value)} />{(discovery?.models || summary.model_catalog || []).filter((model) => model.toLowerCase().includes(modelSearch.toLowerCase())).map((model) => <label key={model}><input type="checkbox" checked={selectedDiscoveredModels.includes(model)} onChange={(event) => setSelectedDiscoveredModels((current) => event.target.checked ? [...new Set([...current, model])] : current.filter((item) => item !== model))} />{model}</label>)}<div className="actions"><button onClick={() => void saveModelCatalog()}>确认</button><button className="secondary" onClick={() => setModelMenuOpen(false)}>取消</button></div></div>}
      </section>}

      {draftLoading && <div className="state">正在加载草稿…</div>}
      {view === "configuration" && draft && editable && !draftLoading && (
        <>
          <section className="panel">
            <div className="panel-title configuration-title">
              <div>
                <h2>草稿编辑器</h2>
                <span>
                  {draft.status} · 修订 {draft.revision}
                  {dirty ? " · 未保存" : " · 已保存"}
                </span>
              </div>
              <label className="field compact-field"><span>草稿名称</span><input value={draftName} onChange={(event) => setDraftName(event.target.value)} /></label>
              <div className="actions">
                <button className="secondary" disabled={actionBusy} onClick={() => void perform(async () => { const updated = await api<Draft>(`/admin/api/configuration/drafts/${encodeURIComponent(draft.draft_id)}/activation`, { method: "POST", headers: writeHeaders(), body: JSON.stringify({ active: !draft.is_active }) }); setDraft(updated); await loadPage(draft.draft_id); })}>{draft.is_active ? "停用" : "启用"}</button>
                <button
                  className="secondary"
                  onClick={() => void perform(async () => {
                    await saveDraft();
                    setNotice("草稿已保存，请先校验再发布。");
                    await loadPage(draft.draft_id);
                  })}
                  disabled={actionBusy || !editableDraft || !dirty}
                >
                  保存草稿
                </button>
                <button
                  className="secondary"
                  onClick={() => void validateDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  校验
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
                  生成待发布版本
                </button>
                <button
                  className="danger"
                  onClick={() => void deleteDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  删除草稿
                </button>
              </div>
            </div>

            {draft.validation_issues.length > 0 && (
              <div className="validation" role="alert">
                <strong>校验问题</strong>
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
              <legend>路由器</legend>
              <div className="form-grid">
                <Field label="判断模型" htmlFor="judge-model">
                  <input
                    id="judge-model"
                    value={editable.router.judge_model || ""}
                    onChange={(event) => mutate((snapshot) => { snapshot.router.judge_model = event.target.value || null; })}
                  />
                </Field>
                <Field label="默认模型" htmlFor="default-model">
                  <select
                    id="default-model"
                    value={editable.router.default_model || ""}
                    onChange={(event) => mutate((snapshot) => { snapshot.router.default_model = event.target.value || null; })}
                  >
                    <option value="">无</option>
                    {aliases.map((alias) => <option key={alias} value={alias}>{alias}</option>)}
                  </select>
                </Field>
                <NumberField label="Judge timeout seconds" id="judge-timeout" value={editable.router.judge_timeout_seconds} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_timeout_seconds = value; })} step="0.1" />
                <NumberField label="Judge token budget" id="judge-tokens" value={editable.router.judge_max_tokens} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_max_tokens = value; })} />
                <NumberField label="Routing context characters" id="routing-context" value={editable.router.routing_context_chars} onChange={(value) => mutate((snapshot) => { snapshot.router.routing_context_chars = value; })} />
              </div>
              <div className="checkbox-group" aria-label="可用模型">
                <span>可用模型</span>
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
              <Field label="判断系统提示词" htmlFor="judge-prompt">
                <textarea
                  id="judge-prompt"
                  rows={8}
                  value={editable.router.judge_system_prompt}
                  onChange={(event) => mutate((snapshot) => { snapshot.router.judge_system_prompt = event.target.value; })}
                />
              </Field>
            </fieldset>

            <fieldset disabled={!editableDraft || actionBusy}>
              <legend>会话路由</legend>
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
              <legend>已配置模型</legend>
              <div className="backend-grid">
                {aliases.map((alias) => (
                  <article className="backend-card" key={alias}>
                    <h3>{alias} <button type="button" className="danger compact-remove" onClick={() => removeDraftModel(alias)}>删除</button></h3>
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
                      {summary.read_only.models[alias]?.provider || "未知提供方"} · 凭据 {summary.read_only.models[alias]?.credential.configured ? "已配置" : "缺失"}
                    </small>
                  </article>
                ))}
              </div>
              <div className="actions model-add-row"><input value={newAlias} onChange={(event) => setNewAlias(event.target.value)} placeholder="输入模型别名" /><button type="button" className="secondary" onClick={addDraftModel}>添加模型</button></div>
            </fieldset>

            <Field label="发布说明" htmlFor="release-notes">
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
              <div className="panel-title"><h2>稳定差异</h2><span>{diff?.changes.length || 0} 项变更</span></div>
              {!diff?.changes.length ? <div className="state">相对基础版本没有已保存的差异。</div> : (
                <div className="diff-list">
                  {diff.changes.map((change) => (
                    <article key={change.path}>
                      <code>{change.path}</code>
                      <div><span>修改前</span><pre>{formatValue(change.before)}</pre></div>
                      <div><span>修改后</span><pre>{formatValue(change.after)}</pre></div>
                    </article>
                  ))}
                </div>
              )}
            </section>
            <section className="panel">
              <div className="panel-title"><h2>托管配置 YAML</h2><span>只读</span></div>
              <pre className="yaml-view" aria-label="托管配置 YAML">{toYaml(editable)}</pre>
            </section>
          </div>
        </>
      )}

      {view === "activity" && <div className="configuration-columns">
        <section className="panel">
          <div className="panel-title"><h2>版本历史</h2><span>不可变快照</span></div>
          {!versions?.items.length ? <div className="state">暂无配置版本。</div> : (
            <div className="table-wrap"><table className="compact-table"><thead><tr><th>版本</th><th>状态</th><th>来源</th><th>创建时间</th><th>发布说明</th><th>操作</th></tr></thead><tbody>
              {versions.items.map((item) => <tr key={item.version_id}>
                <td>v{item.version_number}</td>
                <td><span className={`status ${item.publish_state}`}>{item.publish_state}</span></td>
                <td>{item.source}</td>
                <td>{new Date(item.created_at).toLocaleString()}</td>
                <td>{item.release_notes || "—"}</td>
                <td><div className="actions compact-actions">
                  {!item.is_active && <button className="secondary" disabled={actionBusy} onClick={() => void activateVersion(item)}>启用</button>}
                  {!item.is_active && <button className="danger" disabled={actionBusy} onClick={() => void rollbackVersion(item)}>回滚</button>}
                </div></td>
              </tr>)}
            </tbody></table></div>
          )}
        </section>
        <section className="panel">
          <div className="panel-title"><h2>最近审计</h2><span>不记录请求或提示词正文</span></div>
          {!audit?.items.length ? <div className="state">暂无管理事件。</div> : (
            <div className="audit-list">
              {audit.items.map((item) => <article key={item.audit_id}>
                <div><strong>{item.action}</strong><span className={`status ${item.outcome}`}>{item.outcome}</span></div>
                <small>{new Date(item.occurred_at).toLocaleString()} · {item.subject_type || "system"}</small>
                <code>{JSON.stringify(item.summary)}</code>
              </article>)}
            </div>
          )}
        </section>
      </div>}

      {view === "route-lab" && <div className="configuration-columns">
        <section className="panel">
          <div className="panel-title"><h2>Route Lab</h2><span>仅用于管理员测试，不会保存输入</span></div>
          <Field label="任务文本" htmlFor="route-lab-text">
            <textarea id="route-lab-text" rows={5} value={labText} onChange={(event) => setLabText(event.target.value)} placeholder="输入临时路由任务" />
          </Field>
          <div className="form-grid">
            <Field label="版本 ID（可选）" htmlFor="route-lab-version">
              <input id="route-lab-version" value={labVersionId} onChange={(event) => setLabVersionId(event.target.value)} inputMode="numeric" />
            </Field>
            <div className="actions"><button onClick={() => void runLab()} disabled={actionBusy || !labText.trim()}>评估路由</button></div>
          </div>
          {labResult && <div className="notice">已选择 <strong>{labResult.result.selected_model}</strong> · {labResult.result.judge_status} · {labResult.result.judge_latency_ms == null ? "未使用判断模型" : `${labResult.result.judge_latency_ms.toFixed(1)} ms`} · 不会持久化</div>}
        </section>
      </div>}
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
