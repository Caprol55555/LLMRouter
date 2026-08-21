import { useEffect, useMemo, useState } from "react";

import { ApiError, api } from "./api";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./components/ui/select";
import { Toast } from "./components/ui/toast";

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
  smart_routes: DraftSummary[];
  active_smart_routes: DraftSummary[];
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
  page: number;
  page_size: number;
  total: number;
};
type LabResult = {
  result: {
    selected_model: string;
    assistant_message?: string;
    judge_status: string;
    used_default: boolean;
    judge_latency_ms: number | null;
    elapsed_ms?: number;
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

function translateStatus(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    editing: "编辑中",
    ready: "已校验",
    finalized: "已生成版本",
    active: "已启用",
    pending: "待发布",
    success: "成功",
    failure: "失败",
    denied: "已拒绝",
  };
  return value && labels[value] ? labels[value] : value || "未知";
}

function translateSource(value: string): string {
  return value === "yaml_baseline" ? "配置文件基线" : value === "draft" ? "智能路由版本" : value;
}

function translateAuditAction(value: string): string {
  const labels: Record<string, string> = {
    baseline_imported: "导入配置基线",
    draft_created: "创建智能路由版本",
    draft_updated: "更新智能路由版本",
    draft_validated: "校验智能路由版本",
    draft_finalized: "生成配置版本",
    draft_deleted: "删除智能路由版本",
    draft_activation_changed: "修改智能路由启用状态",
    model_discovery: "发现可用模型",
    route_lab_evaluate: "执行路由测试",
    password_change: "修改密码",
    login: "登录",
    logout: "退出登录",
    integrity_check: "检查数据完整性",
    version_activated: "启用配置版本",
    version_rolled_back: "回滚配置版本",
  };
  return labels[value] || "其他管理操作";
}

function translateSubjectType(value: string | null): string {
  const labels: Record<string, string> = {
    system: "系统",
    configuration_version: "配置版本",
    configuration_draft: "智能路由版本",
  };
  return value && labels[value] ? labels[value] : "系统";
}

function formatAuditSummary(summary: Record<string, unknown>): string {
  const labels: Record<string, string> = {
    version_number: "版本号",
    from_version_id: "原版本",
    to_version_id: "目标版本",
    target_version_id: "回滚目标",
    new_version_id: "新版本",
    base_version_id: "基础版本",
    revision: "修订号",
    issue_count: "问题数",
    draft_id: "智能路由版本标识",
    active: "启用状态",
  };
  return Object.entries(summary)
    .map(([key, value]) => `${labels[key] || "其他信息"}：${typeof value === "boolean" ? (value ? "是" : "否") : String(value)}`)
    .join(" · ") || "无附加信息";
}

function translateValidationMessage(value: string): string {
  const replacements: Array<[string, string]> = [
    ["Router-internal model IDs cannot be used as upstream candidates", "不能将路由器内部模型 ID 用作上游候选模型"],
    ["At least one configured backend must be allowed", "至少需要启用一个已配置模型"],
    ["The default model must be in allowed_models", "默认模型必须包含在可用模型中"],
    ["The upstream model is blocked by the server recursion guard", "该上游模型被服务器递归保护规则阻止"],
    ["max_tokens cannot exceed context_limit", "最大令牌数不能超过上下文上限"],
  ];
  return replacements.reduce((result, [from, to]) => result.replace(from, to), value);
}

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
  const [versionPage, setVersionPage] = useState(1);
  const [auditPage, setAuditPage] = useState(1);
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
  const [labRouteId, setLabRouteId] = useState("");
  const [discovery, setDiscovery] = useState<DiscoveryResult | null>(null);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [selectedDiscoveredModels, setSelectedDiscoveredModels] = useState<string[]>([]);
  const [newModelId, setNewModelId] = useState("");
  const [labMessages, setLabMessages] = useState<Array<{ role: "user" | "assistant"; text: string; createdAt: string; meta?: string }>>([]);

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
        api<VersionPage>(`/admin/api/configuration/versions?page=${versionPage}&page_size=10`),
        api<AuditPage>(`/admin/api/audit?page=${auditPage}&page_size=10`),
      ]);
      setSummary(nextSummary);
      setVersions(nextVersions);
      setAudit(nextAudit);
      setSelectedDiscoveredModels(nextSummary.model_catalog || []);
      const routes = nextSummary.smart_routes;
      const candidate = preferredDraftId || selectedId;
      const nextSelected = routes.some((item) => item.draft_id === candidate)
        ? candidate
        : routes[0]?.draft_id || "";
      setSelectedId(nextSelected);
      if (nextSelected) setEditorOpen(true);
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
        api<Draft>(`/admin/api/configuration/routes/${encodeURIComponent(draftId)}`),
        api<DraftDiff>(
          `/admin/api/configuration/routes/${encodeURIComponent(draftId)}/diff`,
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

  useEffect(() => { void loadPage(); }, [versionPage, auditPage]);

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
      `/admin/api/configuration/routes/${encodeURIComponent(draft.draft_id)}`,
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
        `/admin/api/configuration/routes/${encodeURIComponent(nextDraft.draft_id)}/diff`,
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
      const created = await api<Draft>("/admin/api/configuration/routes", {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify({ release_notes: "", name: "未命名草稿" }),
      });
      setNotice("已从当前配置创建智能路由版本。");
      setEditorOpen(true);
      await loadPage(created.draft_id);
    });
  }

  async function validateDraft() {
    await perform(async () => {
      const current = dirty ? await saveDraft() : draft;
      if (!current) return;
      const validated = await api<Draft>(
        `/admin/api/configuration/routes/${encodeURIComponent(current.draft_id)}/validate`,
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
          ? "校验通过，智能路由版本可以生成配置版本。"
          : "校验完成，但仍存在问题。",
      );
      await loadPage(validated.draft_id);
    });
  }

  async function finalizeDraft() {
    if (!draft) return;
    await perform(async () => {
      const version = await api<Version>(
        `/admin/api/configuration/routes/${encodeURIComponent(draft.draft_id)}/finalize`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({ revision: draft.revision }),
        },
      );
      setNotice(
        `版本 ${version.version_number} 已进入待发布状态，运行时配置未改变。`,
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
      setNotice(`已启用 v${result.version_number}，清理缓存路由 ${result.cache_cleared} 条。`);
      await loadPage();
    });
  }

  async function rollbackVersion(version: Version) {
    if (!summary || version.is_active || !window.confirm(`确认回滚到 v${version.version_number}？`)) return;
    await perform(async () => {
      const result = await api<Version & { cache_cleared: number }>(
        `/admin/api/configuration/versions/${version.version_id}/rollback`,
        {
          method: "POST",
          headers: writeHeaders(),
          body: JSON.stringify({
            expected_active_version_id: summary.active.version_id,
            release_notes: `回滚到版本 ${version.version_number}`,
          }),
        },
      );
      setNotice(`已创建回滚版本并启用 v${result.version_number}。`);
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
      const saved = await api<{ models: string[] }>("/admin/api/configuration/model-catalog", {
        method: "PUT",
        headers: writeHeaders(),
        body: JSON.stringify({ models: selectedDiscoveredModels }),
      });
      setSummary((current) => current ? { ...current, model_catalog: saved.models } : current);
      setModelMenuOpen(false);
      setNotice("模型清单已保存。");
    });
  }

  function addDraftModel() {
    const modelId = newModelId.trim();
    if (!modelId || !editable) return;
    const baseAlias = modelId.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "模型";
    let alias = baseAlias;
    let suffix = 2;
    while (editable.llms[alias]) alias = `${baseAlias}-${suffix++}`;
    mutate((snapshot) => {
      snapshot.llms[alias] = { model: modelId, description: "", max_tokens: 4096, context_limit: 32768 };
      snapshot.router.allowed_models = [...new Set([...snapshot.router.allowed_models, alias])].sort();
    });
    setNewModelId("");
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
    const text = labText.trim();
    if (!text) return;
    const createdAt = new Date().toISOString();
    setLabMessages((current) => [...current, { role: "user", text, createdAt }]);
    await perform(async () => {
      const body: Record<string, unknown> = { text };
      if (labRouteId) body.route_id = labRouteId;
      const result = await api<LabResult>("/admin/api/route-lab/evaluate", {
        method: "POST",
        headers: writeHeaders(),
        body: JSON.stringify(body),
      });
      setLabText("");
      setLabResult(result);
      setLabMessages((current) => [...current, {
        role: "assistant",
        text: result.result.assistant_message || `已选择模型：${result.result.selected_model}`,
        createdAt: new Date().toISOString(),
        meta: `${result.result.elapsed_ms == null ? "—" : `${result.result.elapsed_ms.toFixed(1)} 毫秒`} · ${result.result.selected_model}`,
      }]);
    });
  }

  function clearLab() {
    setLabMessages([]);
    setLabResult(null);
    setLabText("");
  }

  async function deleteDraft() {
    if (!draft || !window.confirm("确认删除此智能路由版本？")) return;
    await perform(async () => {
      await api<{ status: string }>(
        `/admin/api/configuration/routes/${encodeURIComponent(draft.draft_id)}?revision=${draft.revision}`,
        { method: "DELETE", headers: writeHeaders() },
      );
      setSelectedId("");
      setDraft(null);
      setEditable(null);
      setNotice("智能路由版本已删除。");
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
      {error && <Toast message={error} tone="error" onClose={() => setError("")} />}
      {notice && <Toast message={notice} onClose={() => setNotice("")} />}

      {view === "configuration" && <section className="cards configuration-summary" aria-label="配置摘要">
        <Metric label="当前版本" value={`v${active.version_number}`} hint={translateSource(active.source)} />
        <Metric label="运行状态" value="已启用" hint="支持原子启用" />
        <Metric label="智能路由数量" value={String(summary.smart_routes.length)} hint="最多显示 100 条" />
        <Metric label="待发布版本" value={String(versions?.items.filter((item) => item.publish_state === "pending").length || 0)} />
      </section>}

      {view === "configuration" && <section className="panel">
        <div className="panel-title configuration-title">
          <div>
            <h2>智能路由版本管理</h2>
          <span>每个智能路由都可以独立命名、编辑和启用；版本生成后可在活动记录中启用或回滚。</span>
          </div>
          <button onClick={() => void createDraft()} disabled={actionBusy}>新建智能路由版本</button>
        </div>
        <div className="route-version-list" aria-label="智能路由版本列表">
          {!summary.smart_routes.length && <div className="state">暂无智能路由版本，请新建一个版本。</div>}
          {summary.smart_routes.map((item) => (
            <button
              type="button"
              key={item.draft_id}
              className={`route-version-item ${selectedId === item.draft_id ? "selected" : ""} ${item.is_active ? "enabled" : "disabled"}`}
              onClick={() => { setSelectedId(item.draft_id); setEditorOpen(true); }}
            >
              <span className="route-version-name">{item.name || "未命名智能路由"}</span>
              <span className="route-version-meta">{translateStatus(item.status)} · 修订 {item.revision}</span>
              <span className={`route-state ${item.is_active ? "enabled" : "disabled"}`}>{item.is_active ? "已启用" : "未启用"}</span>
            </button>
          ))}
        </div>
        {!!summary.active_smart_routes.length && <div className="active-route-list"><strong>已启用智能路由</strong><div>{summary.active_smart_routes.map((item) => <span className="route-tag" key={item.draft_id}>{item.name || "未命名智能路由"}</span>)}</div></div>}
      </section>}

      {view === "configuration" && <section className="panel">
        <div className="panel-title"><h2>模型清单</h2><span>选择可用于智能路由的上游模型</span></div>
        <div className="selected-catalog" aria-label="当前选中模型">
          <span>当前选中模型</span>
          {summary.model_catalog?.length ? summary.model_catalog.map((model) => <span className="catalog-chip" key={model}>{model}</span>) : <small>尚未配置可用模型</small>}
        </div>
        <button className="secondary" onClick={() => { setModelMenuOpen(true); void runDiscovery(); }} disabled={actionBusy}>发现可用模型</button>
        <Dialog open={modelMenuOpen} onOpenChange={setModelMenuOpen}><DialogContent className="model-menu-dialog"><DialogHeader><DialogTitle>发现可用模型</DialogTitle><DialogDescription>搜索并选择可用于智能路由的上游模型。</DialogDescription></DialogHeader><div className="model-menu">
          <div className="model-menu-toolbar">
            <div className="selected-models" aria-label="已勾选模型">
              <span>已勾选模型</span>
              {selectedDiscoveredModels.length ? selectedDiscoveredModels.map((model) => <button key={model} type="button" className="model-chip" onClick={() => setSelectedDiscoveredModels((current) => current.filter((item) => item !== model))}>{model} ×</button>) : <small>暂无</small>}
            </div>
            <input placeholder="模糊搜索模型" aria-label="模糊搜索模型" value={modelSearch} onChange={(event) => setModelSearch(event.target.value)} />
          </div>
          <div className="model-results">
            {(discovery?.models || summary.model_catalog || []).filter((model) => model.toLowerCase().includes(modelSearch.toLowerCase())).map((model) => <button key={model} type="button" className={`model-option ${selectedDiscoveredModels.includes(model) ? "selected" : ""}`} onClick={() => setSelectedDiscoveredModels((current) => current.includes(model) ? current.filter((item) => item !== model) : [...current, model])}>{selectedDiscoveredModels.includes(model) ? "已选择 · " : ""}{model}</button>)}
          </div>
          <div className="actions model-menu-actions"><button onClick={() => void saveModelCatalog()}>确认</button><button className="secondary" onClick={() => { setSelectedDiscoveredModels(summary.model_catalog || []); setModelSearch(""); setModelMenuOpen(false); }}>取消</button></div>
        </div></DialogContent></Dialog>
      </section>}

      {draftLoading && <div className="state">正在加载智能路由版本…</div>}
      {view === "configuration" && editorOpen && draft && editable && !draftLoading && (
        <Dialog open={editorOpen} onOpenChange={setEditorOpen}><DialogContent className="route-editor-dialog">
          <section className="panel">
            <div className="panel-title configuration-title">
              <div>
                <h2>智能路由版本编辑器</h2>
                <span>
                  {translateStatus(draft.status)} · 修订 {draft.revision}
                  {dirty ? " · 未保存" : " · 已保存"}
                </span>
              </div>
              <label className="field compact-field"><span>智能路由名称</span><input value={draftName} onChange={(event) => setDraftName(event.target.value)} /></label>
              <div className="actions">
                <button className="secondary" disabled={actionBusy} onClick={() => void perform(async () => { const updated = await api<Draft>(`/admin/api/configuration/routes/${encodeURIComponent(draft.draft_id)}/activation`, { method: "POST", headers: writeHeaders(), body: JSON.stringify({ active: !draft.is_active }) }); setDraft(updated); await loadPage(draft.draft_id); })}>{draft.is_active ? "停用" : "启用"}</button>
                <button
                  className="secondary"
                  onClick={() => void perform(async () => {
                    await saveDraft();
                    setNotice("智能路由版本已保存，请先校验再生成版本。");
                    await loadPage(draft.draft_id);
                  })}
                  disabled={actionBusy || !editableDraft || !dirty}
                >
                  保存版本
                </button>
                <button
                  className="secondary"
                  onClick={() => void validateDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  校验版本
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
                  生成配置版本
                </button>
                <button
                  className="danger"
                  onClick={() => void deleteDraft()}
                  disabled={actionBusy || !editableDraft}
                >
                  删除版本
                </button>
              </div>
            </div>

            {draft.validation_issues.length > 0 && (
              <div className="validation" role="alert">
                <strong>校验问题</strong>
                <ul>
                  {draft.validation_issues.map((issue) => (
                    <li key={`${issue.path}:${issue.code}`}>
                      <code>{issue.path}</code> — {translateValidationMessage(issue.message)}
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
                  <Select value={editable.router.default_model || "none"} onValueChange={(value) => mutate((snapshot) => { snapshot.router.default_model = value === "none" ? null : value; })}>
                    <SelectTrigger id="default-model"><SelectValue placeholder="选择默认模型" /></SelectTrigger>
                    <SelectContent><SelectItem value="none">无</SelectItem>{aliases.map((alias) => <SelectItem key={alias} value={alias}>{alias}</SelectItem>)}</SelectContent>
                  </Select>
                </Field>
                <NumberField label="判断超时时间（秒）" id="judge-timeout" value={editable.router.judge_timeout_seconds} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_timeout_seconds = value; })} step="0.1" />
                <NumberField label="判断令牌预算" id="judge-tokens" value={editable.router.judge_max_tokens} onChange={(value) => mutate((snapshot) => { snapshot.router.judge_max_tokens = value; })} />
                <NumberField label="路由上下文字符数" id="routing-context" value={editable.router.routing_context_chars} onChange={(value) => mutate((snapshot) => { snapshot.router.routing_context_chars = value; })} />
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
                <label><input type="checkbox" checked={editable.session_routing.enabled} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.enabled = event.target.checked; })} />启用会话路由</label>
                <label><input type="checkbox" checked={editable.session_routing.rejudge_on_modality_change} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.rejudge_on_modality_change = event.target.checked; })} />模态变化时重新判断</label>
                <label><input type="checkbox" checked={editable.session_routing.rejudge_on_backend_error} onChange={(event) => mutate((snapshot) => { snapshot.session_routing.rejudge_on_backend_error = event.target.checked; })} />上游错误时重新判断</label>
              </div>
              <div className="form-grid">
                <NumberField label="缓存有效期（秒）" id="session-ttl" value={editable.session_routing.ttl_seconds} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.ttl_seconds = value; })} />
                <NumberField label="每几轮用户消息重新判断" id="session-rejudge" value={editable.session_routing.rejudge_every_user_turns} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.rejudge_every_user_turns = value; })} />
                <NumberField label="最大缓存条目" id="session-max" value={editable.session_routing.max_entries} onChange={(value) => mutate((snapshot) => { snapshot.session_routing.max_entries = value; })} />
                <Field label="允许的重新判断间隔" htmlFor="session-intervals">
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
                    <Field label="上游模型" htmlFor={`model-${alias}`}>
                      <Select value={editable.llms[alias].model} onValueChange={(value) => mutate((snapshot) => { snapshot.llms[alias].model = value; })}>
                        <SelectTrigger id={`model-${alias}`}><SelectValue placeholder="选择上游模型" /></SelectTrigger>
                        <SelectContent>{summary.model_catalog?.map((model) => <SelectItem key={model} value={model}>{model}</SelectItem>)}</SelectContent>
                      </Select>
                    </Field>
                    <Field label="模型说明" htmlFor={`description-${alias}`}>
                      <textarea id={`description-${alias}`} rows={3} value={editable.llms[alias].description} onChange={(event) => mutate((snapshot) => { snapshot.llms[alias].description = event.target.value; })} />
                    </Field>
                    <div className="form-grid">
                      <NumberField label="最大令牌数" id={`max-tokens-${alias}`} value={editable.llms[alias].max_tokens} onChange={(value) => mutate((snapshot) => { snapshot.llms[alias].max_tokens = value; })} />
                      <NumberField label="上下文上限" id={`context-${alias}`} value={editable.llms[alias].context_limit} onChange={(value) => mutate((snapshot) => { snapshot.llms[alias].context_limit = value; })} />
                    </div>
                    <small>
                      {summary.read_only.models[alias]?.provider || "未知提供方"} · 凭据 {summary.read_only.models[alias]?.credential.configured ? "已配置" : "缺失"}
                    </small>
                  </article>
                ))}
              </div>
              <div className="actions model-add-row"><Select value={newModelId || "none"} onValueChange={(value) => setNewModelId(value === "none" ? "" : value)}><SelectTrigger aria-label="选择要添加的模型"><SelectValue placeholder="选择可用模型" /></SelectTrigger><SelectContent><SelectItem value="none">选择可用模型</SelectItem>{summary.model_catalog?.map((model) => <SelectItem key={model} value={model}>{model}</SelectItem>)}</SelectContent></Select><button type="button" className="secondary" onClick={addDraftModel} disabled={!newModelId}>添加模型</button></div>
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
        </DialogContent></Dialog>
      )}

      {view === "activity" && <div className="configuration-columns">
        <section className="panel">
          <div className="panel-title"><h2>版本历史</h2><span>不可变快照</span></div>
          {!versions?.items.length ? <div className="state">暂无配置版本。</div> : (
            <div className="table-wrap"><table className="compact-table"><thead><tr><th>版本</th><th>状态</th><th>来源</th><th>创建时间</th><th>发布说明</th><th>操作</th></tr></thead><tbody>
              {versions.items.map((item) => <tr key={item.version_id}>
                <td>v{item.version_number}</td>
                <td><span className={`status ${item.publish_state}`}>{translateStatus(item.publish_state)}{item.is_active ? " · 当前" : ""}</span></td>
                <td>{translateSource(item.source)}</td>
                <td>{new Date(item.created_at).toLocaleString()}</td>
                <td>{item.release_notes || "—"}</td>
                <td><div className="actions compact-actions">
                  {!item.is_active && <button className="secondary" disabled={actionBusy} onClick={() => void activateVersion(item)}>启用</button>}
                  {!item.is_active && <button className="danger" disabled={actionBusy} onClick={() => void rollbackVersion(item)}>回滚</button>}
                </div></td>
              </tr>)}
            </tbody></table></div>
          )}
          <div className="pagination"><button className="secondary" disabled={!versions || versionPage <= 1} onClick={() => setVersionPage((value) => Math.max(1, value - 1))}>上一页</button><span>第 {versionPage} 页 · {versions?.total ?? 0} 个版本</span><button className="secondary" disabled={!versions || versionPage * versions.page_size >= versions.total} onClick={() => setVersionPage((value) => value + 1)}>下一页</button></div>
        </section>
        <section className="panel">
          <div className="panel-title"><h2>操作审计</h2><span>不记录请求或提示词正文</span></div>
          {!audit?.items.length ? <div className="state">暂无管理事件。</div> : (
            <div className="audit-list">
              {audit.items.map((item) => <article key={item.audit_id}>
                <div><strong>{translateAuditAction(item.action)}</strong><span className={`status ${item.outcome}`}>{translateStatus(item.outcome)}</span></div>
                <small>{new Date(item.occurred_at).toLocaleString("zh-CN")} · {translateSubjectType(item.subject_type)}</small>
                <span className="audit-summary">{formatAuditSummary(item.summary)}</span>
              </article>)}
            </div>
          )}
          <div className="pagination"><button className="secondary" disabled={!audit || auditPage <= 1} onClick={() => setAuditPage((value) => Math.max(1, value - 1))}>上一页</button><span>第 {auditPage} 页 · {audit?.total ?? 0} 条操作</span><button className="secondary" disabled={!audit || auditPage * audit.page_size >= audit.total} onClick={() => setAuditPage((value) => value + 1)}>下一页</button></div>
        </section>
      </div>}

      {view === "route-lab" && <section className="panel route-chat-panel">
        <div className="panel-title route-lab-header"><h2>路由测试</h2><div className="route-lab-controls"><div className="route-lab-select"><span>已启用智能路由</span><Select value={labRouteId || "current"} onValueChange={(value) => setLabRouteId(value === "current" ? "" : value)}><SelectTrigger id="route-lab-route"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="current">当前运行配置</SelectItem>{summary.active_smart_routes.map((item) => <SelectItem key={item.draft_id} value={item.draft_id}>{item.name || "未命名智能路由"}</SelectItem>)}</SelectContent></Select></div></div></div>
        <div className="chat-thread" aria-live="polite">
          {!labMessages.length && <div className="chat-empty"><strong>开始测试智能路由</strong><span>输入一段任务文本，查看系统选择的模型和判断耗时。</span></div>}
          {labMessages.map((message, index) => <div key={`${message.role}-${index}`} className={`chat-message ${message.role}`}><div className="chat-bubble">{message.text}</div><small>{new Date(message.createdAt).toLocaleTimeString("zh-CN")}{message.meta ? ` · ${message.meta}` : ""}</small></div>)}
        </div>
        <form className="chat-composer" onSubmit={(event) => { event.preventDefault(); void runLab(); }}>
          <textarea id="route-lab-text" rows={3} value={labText} onChange={(event) => setLabText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder="输入要测试的任务，例如：帮我总结这份报告" />
          <div className="chat-actions"><button type="button" className="secondary" onClick={clearLab} disabled={!labMessages.length}>清除窗口</button><button type="submit" disabled={actionBusy || !labText.trim()}>发送</button></div>
        </form>
      </section>}
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
