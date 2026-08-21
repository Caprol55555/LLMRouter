import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, api } from "./api";
import { ConfigurationPage } from "./configuration";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "./components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "./components/ui/tabs";
import { Toast } from "./components/ui/toast";
import "./styles.css";

type WindowSummary = {
  request_count: number;
  judge_calls: number;
  judge_amplification: number;
  cache_hit_rate: number;
  success_rate: number;
  fallback_count: number;
  error_count: number;
  total_latency_ms: { p50: number | null; p95: number | null; sample_count: number };
  judge_latency_ms: { p50: number | null; p95: number | null; sample_count: number };
  model_distribution: Array<{ model: string; count: number }>;
};

type Overview = { generated_at: string; windows: Record<"1h" | "24h" | "7d", WindowSummary> };
type RequestItem = {
  event_id: string;
  request_id: string;
  occurred_at: string;
  traffic_class: string;
  requested_model: string;
  cache_status: string | null;
  rejudge_reason: string | null;
  judge_status: string | null;
  selected_model: string | null;
  final_status: string | null;
  fallback: number;
  error_category: string | null;
  judge_latency_ms: number | null;
  first_byte_latency_ms: number | null;
  total_latency_ms: number | null;
  total_tokens: number | null;
  config_version_id: number | null;
};
type RequestPage = { items: RequestItem[]; page: number; page_size: number; total: number };
type Runtime = {
  strategy: string;
  models: Array<{ name: string; description: string }>;
  session_cache_entries: number;
  commit: string;
  schema_version: number | null;
  config_version_id: number | null;
  config_version_number: number | null;
  deployment_time?: string;
};
type Health = {
  status: string;
  database: { status: string; schema_version: number | null };
  telemetry?: { status: string; dropped_events: number; database_errors: number; queue_depth: number };
};

function translateValue(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    success: "成功",
    error: "错误",
    disconnected: "已断开",
    hit: "命中",
    miss: "未命中",
    bypass: "未使用缓存",
    not_called: "未调用",
    called: "已调用",
    used: "已使用",
    fallback: "已回退",
    rejudge: "重新判断",
    backend_error: "上游错误",
    pending: "待发布",
    active: "已启用",
    editing: "编辑中",
    ready: "已校验",
    finalized: "已生成版本",
    production: "生产流量",
    admin_test: "管理员测试",
    deployment_smoke: "部署冒烟",
    success_rate: "成功率",
    ok: "正常",
    healthy: "正常",
    degraded: "降级",
    failed: "失败",
    unknown: "未知",
  };
  return value && labels[value] ? labels[value] : value || "—";
}

function fmtNumber(value: number | null, suffix = "") {
  return value == null ? "—" : `${value.toFixed(value >= 100 ? 0 : 1)}${suffix}`;
}

function fmtDate(value: string | undefined) {
  return value ? new Date(value).toLocaleString("zh-CN") : "未知";
}

function windowLabel(value: "1h" | "24h" | "7d") {
  return value === "1h" ? "最近 1 小时" : value === "24h" ? "最近 24 小时" : "最近 7 天";
}

export function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [csrf, setCsrf] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [requests, setRequests] = useState<RequestPage | null>(null);
  const [windowKey, setWindowKey] = useState<"1h" | "24h" | "7d">("24h");
  const [page, setPage] = useState(1);
  const [trafficClass, setTrafficClass] = useState("");
  const [selectedModel, setSelectedModel] = useState("");
  const [finalStatus, setFinalStatus] = useState("");
  const [requestWindow, setRequestWindow] = useState<"1h" | "24h" | "7d">("24h");
  const [section, setSection] = useState<"overview" | "activity" | "configuration" | "route-lab">("overview");
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ current: "", next: "", confirm: "" });
  const [passwordNotice, setPasswordNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const query = new URLSearchParams({ page: String(page), page_size: "25" });
    const windowMs = { "1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000 }[requestWindow];
    query.set("since", new Date(Date.now() - windowMs).toISOString());
    if (trafficClass) query.set("traffic_class", trafficClass);
    if (selectedModel) query.set("selected_model", selectedModel);
    if (finalStatus) query.set("final_status", finalStatus);
    try {
      const [session, nextOverview, nextRuntime, nextHealth, nextRequests] = await Promise.all([
        api<{ csrf_token: string }>("/admin/api/session"),
        api<Overview>("/admin/api/overview"),
        api<Runtime>("/admin/api/runtime"),
        api<Health>("/admin/api/health"),
        api<RequestPage>(`/admin/api/requests?${query}`),
      ]);
      setCsrf(session.csrf_token);
      setOverview(nextOverview);
      setRuntime(nextRuntime);
      setHealth(nextHealth);
      setRequests(nextRequests);
      setAuthenticated(true);
    } catch (reason) {
      const apiError = reason as ApiError;
      if (apiError.status === 401) setAuthenticated(false);
      else setError(apiError.message);
    } finally {
      setLoading(false);
    }
  }, [page, requestWindow, trafficClass, selectedModel, finalStatus]);

  useEffect(() => {
    void load();
  }, [load]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setLoginError("");
    try {
      const result = await api<{ csrf_token: string }>("/admin/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: window.location.origin },
        body: JSON.stringify({ token }),
      });
      setCsrf(result.csrf_token);
      setToken("");
      setAuthenticated(true);
      await load();
    } catch (reason) {
      setLoginError((reason as Error).message);
    }
  }

  async function logout() {
    const session = csrf
      ? { csrf_token: csrf }
      : await api<{ csrf_token: string }>("/admin/api/session").catch(() => null);
    if (session?.csrf_token) await api("/admin/api/logout", {
      method: "POST",
      headers: { Origin: window.location.origin, "X-CSRF-Token": session.csrf_token },
    }).catch(() => undefined);
    setCsrf("");
    setAuthenticated(false);
    setOverview(null);
    setRequests(null);
    setSection("overview");
  }

  async function changePassword(event: FormEvent) {
    event.preventDefault();
    setPasswordNotice("");
    if (passwordForm.next.length < 8 || passwordForm.next !== passwordForm.confirm) {
      setPasswordNotice("新密码至少 8 位，且两次输入必须一致。");
      return;
    }
    try {
      await api("/admin/api/password", {
        method: "POST",
        headers: { "Content-Type": "application/json", Origin: window.location.origin, "X-CSRF-Token": csrf },
        body: JSON.stringify({ current_password: passwordForm.current, new_password: passwordForm.next }),
      });
      setPasswordOpen(false);
      setPasswordForm({ current: "", next: "", confirm: "" });
      setPasswordNotice("密码已修改，请使用新密码重新登录。");
      await logout();
    } catch (reason) {
      setPasswordNotice((reason as Error).message);
    }
  }

  const summary = overview?.windows[windowKey];
  const modelMax = useMemo(
    () => Math.max(1, ...(summary?.model_distribution.map((item) => item.count) || [1])),
    [summary],
  );

  if (authenticated === false) {
    return (
      <main className="login-shell">
        <form className="login-card" onSubmit={login}>
          <div className="eyebrow">本地管理</div>
          <h1>LLMRouter 管理中心</h1>
          <p>请输入路由器上配置的管理员密码。浏览器不会保存密码。</p>
          <label htmlFor="admin-token">管理员密码</label>
          <input
            id="admin-token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            required
          />
          {loginError && <div className="error" role="alert">{loginError}</div>}
          <button type="submit">登录</button>
        </form>
      </main>
    );
  }

  return (
    <main className="shell">
      <header>
        <div>
          <div className="eyebrow">路由可观测性</div>
          <h1>LLMRouter 管理中心</h1>
        </div>
        <div className="actions">
          <button className="secondary" onClick={() => void load()}>刷新</button>
          <button className="secondary" onClick={() => setPasswordOpen(true)}>修改密码</button>
          <button className="secondary" onClick={() => void logout()}>退出登录</button>
        </div>
      </header>
      <div className="deployment-banner">最后更新部署时间：<strong>{fmtDate(runtime?.deployment_time)}</strong></div>

      {error && <Toast message={error} tone="error" onClose={() => setError("")} />}
      <Tabs value={section} onValueChange={(value) => setSection(value as typeof section)}>
        <TabsList className="primary-nav" aria-label="管理中心页面">
          <TabsTrigger value="overview">概览</TabsTrigger><TabsTrigger value="activity">活动记录</TabsTrigger><TabsTrigger value="configuration">配置</TabsTrigger><TabsTrigger value="route-lab">路由测试</TabsTrigger>
        </TabsList>
      </Tabs>

      {section === "configuration" ? (
        <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="configuration" />
      ) : section === "route-lab" ? (
        <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="route-lab" />
      ) : section === "activity" ? (
        <>
          <RequestsPanel requests={requests} runtime={runtime} loading={loading} requestWindow={requestWindow} setRequestWindow={setRequestWindow} trafficClass={trafficClass} setTrafficClass={setTrafficClass} selectedModel={selectedModel} setSelectedModel={setSelectedModel} finalStatus={finalStatus} setFinalStatus={setFinalStatus} page={page} setPage={setPage} />
          <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="activity" />
        </>
      ) : (
        <>
          <Tabs value={windowKey} onValueChange={(value) => setWindowKey(value as typeof windowKey)}>
            <TabsList className="range-tabs" aria-label="统计时间范围"><TabsTrigger value="1h">1 小时</TabsTrigger><TabsTrigger value="24h">24 小时</TabsTrigger><TabsTrigger value="7d">7 天</TabsTrigger></TabsList>
          </Tabs>
          {loading && !overview ? <div className="state">正在加载遥测数据…</div> : summary && (
            <>
          <section className="overview-metrics-section" aria-label="概览指标"><div className="section-heading"><div><span className="eyebrow">运行指标</span><h2>{windowKey === "1h" ? "最近 1 小时" : windowKey === "24h" ? "最近 24 小时" : "最近 7 天"}</h2></div><div className="hero-status"><span className={`health-dot ${health?.status === "ok" ? "ok" : "warn"}`} />{health?.status === "ok" ? "运行正常" : "需要关注"}</div></div><div className="cards overview-metrics">
            <Metric label="外部请求" value={String(summary.request_count)} />
            <Metric label="判断调用" value={`${summary.judge_calls} · ${summary.judge_amplification.toFixed(2)}×`} />
            <Metric label="缓存命中" value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`} />
            <Metric label="成功率" value={`${(summary.success_rate * 100).toFixed(1)}%`} />
            <Metric label="总延迟" value={`${fmtNumber(summary.total_latency_ms.p50, " ms")} / ${fmtNumber(summary.total_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="判断延迟" value={`${fmtNumber(summary.judge_latency_ms.p50, " ms")} / ${fmtNumber(summary.judge_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="遥测状态" value={translateValue(health?.telemetry?.status || health?.database.status)} hint={`队列 ${health?.telemetry?.queue_depth ?? 0}`} />
            <Metric label="丢弃事件" value={String(health?.telemetry?.dropped_events ?? 0)} hint={`${health?.telemetry?.database_errors ?? 0} 个数据库错误`} />
          </div></section>
          <section className="overview-analysis">
            <article className="panel analysis-panel"><div className="panel-title"><h2>请求健康度</h2><span>成功与异常</span></div><div className="analysis-bars"><AnalysisBar label="成功请求" value={summary.success_rate * 100} display={`${(summary.success_rate * 100).toFixed(1)}%`} tone="good" /><AnalysisBar label="回退请求" value={summary.request_count ? (summary.fallback_count / summary.request_count) * 100 : 0} display={`${summary.fallback_count} 次`} tone="warn" /><AnalysisBar label="错误请求" value={summary.request_count ? (summary.error_count / summary.request_count) * 100 : 0} display={`${summary.error_count} 次`} tone="bad" /></div></article>
            <article className="panel analysis-panel"><div className="panel-title"><h2>延迟概览</h2><span>毫秒</span></div><div className="latency-grid"><LatencyStat label="总延迟 p50" value={fmtNumber(summary.total_latency_ms.p50)} /><LatencyStat label="总延迟 p95" value={fmtNumber(summary.total_latency_ms.p95)} /><LatencyStat label="判断 p50" value={fmtNumber(summary.judge_latency_ms.p50)} /><LatencyStat label="判断 p95" value={fmtNumber(summary.judge_latency_ms.p95)} /></div></article>
          </section>
          <section className="panel distribution">
            <div className="panel-title"><h2>模型分布</h2><span>{windowLabel(windowKey)}</span></div>
            {summary.model_distribution.length === 0 ? <div className="state">此时间范围内没有路由请求。</div> : summary.model_distribution.map((item) => (
              <div className="bar-row" key={item.model || "unknown"}>
                <span>{item.model || "未知"}</span>
                <div className="bar-track"><div className="bar" style={{ width: `${(item.count / modelMax) * 100}%` }} /></div>
                <strong>{item.count}</strong>
              </div>
            ))}
          </section>
            </>
          )}

        </>
      )}
      {passwordNotice && !passwordOpen && <Toast message={passwordNotice} onClose={() => setPasswordNotice("")} />}
      <Dialog open={passwordOpen} onOpenChange={setPasswordOpen}><DialogContent><DialogHeader><DialogTitle>修改密码</DialogTitle><DialogDescription>修改后需要使用新密码重新登录。</DialogDescription></DialogHeader><form className="modal-form" onSubmit={changePassword}><label className="field"><span>当前密码</span><input type="password" value={passwordForm.current} onChange={(e) => setPasswordForm({ ...passwordForm, current: e.target.value })} required /></label><label className="field"><span>新密码</span><input type="password" minLength={8} value={passwordForm.next} onChange={(e) => setPasswordForm({ ...passwordForm, next: e.target.value })} required /></label><label className="field"><span>确认新密码</span><input type="password" minLength={8} value={passwordForm.confirm} onChange={(e) => setPasswordForm({ ...passwordForm, confirm: e.target.value })} required /></label>{passwordNotice && <div className="error">{passwordNotice}</div>}<div className="dialog-actions"><button type="submit">保存密码</button></div></form></DialogContent></Dialog>
    </main>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <article className="metric"><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</article>;
}

function AnalysisBar({ label, value, display, tone }: { label: string; value: number; display: string; tone: "good" | "warn" | "bad" }) {
  return <div className="analysis-bar"><div><span>{label}</span><strong>{display}</strong></div><div className="analysis-track"><div className={`analysis-fill ${tone}`} style={{ width: `${Math.min(100, Math.max(0, value))}%` }} /></div></div>;
}

function LatencyStat({ label, value }: { label: string; value: string }) {
  return <div className="latency-stat"><span>{label}</span><strong>{value}</strong></div>;
}

function RequestsPanel({
  requests, runtime, loading, requestWindow, setRequestWindow, trafficClass, setTrafficClass,
  selectedModel, setSelectedModel, finalStatus, setFinalStatus, page, setPage,
}: {
  requests: RequestPage | null;
  runtime: Runtime | null;
  loading: boolean;
  requestWindow: "1h" | "24h" | "7d";
  setRequestWindow: (value: "1h" | "24h" | "7d") => void;
  trafficClass: string;
  setTrafficClass: (value: string) => void;
  selectedModel: string;
  setSelectedModel: (value: string) => void;
  finalStatus: string;
  setFinalStatus: (value: string) => void;
  page: number;
  setPage: (value: number | ((value: number) => number)) => void;
}) {
  return <section className="panel"><div className="panel-title"><h2>请求记录</h2><span>仅展示结构化元数据</span></div><div className="filters"><Select value={requestWindow} onValueChange={(value) => { setPage(1); setRequestWindow(value as "1h" | "24h" | "7d"); }}><SelectTrigger aria-label="请求时间范围"><SelectValue placeholder="时间范围" /></SelectTrigger><SelectContent><SelectItem value="1h">最近 1 小时</SelectItem><SelectItem value="24h">最近 24 小时</SelectItem><SelectItem value="7d">最近 7 天</SelectItem></SelectContent></Select><Select value={trafficClass || "all"} onValueChange={(value) => { setPage(1); setTrafficClass(value === "all" ? "" : value); }}><SelectTrigger aria-label="流量类型"><SelectValue placeholder="流量类型" /></SelectTrigger><SelectContent><SelectItem value="all">全部流量</SelectItem><SelectItem value="production">生产流量</SelectItem><SelectItem value="admin_test">管理员测试</SelectItem><SelectItem value="deployment_smoke">部署冒烟</SelectItem></SelectContent></Select><Select value={selectedModel || "all"} onValueChange={(value) => { setPage(1); setSelectedModel(value === "all" ? "" : value); }}><SelectTrigger aria-label="已选模型"><SelectValue placeholder="模型" /></SelectTrigger><SelectContent><SelectItem value="all">全部模型</SelectItem>{runtime?.models.map((model) => <SelectItem key={model.name} value={model.name}>{model.name}</SelectItem>)}</SelectContent></Select><Select value={finalStatus || "all"} onValueChange={(value) => { setPage(1); setFinalStatus(value === "all" ? "" : value); }}><SelectTrigger aria-label="最终状态"><SelectValue placeholder="最终状态" /></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem><SelectItem value="success">成功</SelectItem><SelectItem value="error">错误</SelectItem><SelectItem value="disconnected">断开</SelectItem></SelectContent></Select></div>{loading && !requests ? <div className="state">正在加载请求记录…</div> : !requests?.items.length ? <div className="state">没有符合条件的请求。</div> : <div className="table-wrap"><table><thead><tr><th>时间</th><th>请求</th><th>策略</th><th>缓存 / 判断</th><th>模型</th><th>状态</th><th>延迟</th><th>令牌</th></tr></thead><tbody>{requests.items.map((item) => <tr key={item.event_id}><td>{new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "medium" }).format(new Date(item.occurred_at))}</td><td><code>{item.request_id.slice(0, 12)}</code><small>{translateValue(item.traffic_class)}</small></td><td>{item.requested_model}<small>{translateValue(item.rejudge_reason)}</small></td><td>{translateValue(item.cache_status)}<small>{translateValue(item.judge_status)}</small></td><td>{item.selected_model || "—"}</td><td><span className={`status ${item.final_status}`}>{translateValue(item.final_status)}</span></td><td>{fmtNumber(item.total_latency_ms, " ms")}</td><td>{item.total_tokens ?? "—"}</td></tr>)}</tbody></table></div>}<div className="pagination"><button className="secondary" disabled={!requests || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>上一页</button><span>第 {page} 页 · {requests?.total ?? 0} 条</span><button className="secondary" disabled={!requests || page * requests.page_size >= requests.total} onClick={() => setPage((value) => value + 1)}>下一页</button></div></section>;
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
