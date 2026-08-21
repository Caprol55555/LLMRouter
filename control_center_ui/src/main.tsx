import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { ApiError, api } from "./api";
import { ConfigurationPage } from "./configuration";
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
};
type Health = {
  status: string;
  database: { status: string; schema_version: number | null };
  telemetry?: { status: string; dropped_events: number; database_errors: number; queue_depth: number };
};

function fmtNumber(value: number | null, suffix = "") {
  return value == null ? "—" : `${value.toFixed(value >= 100 ? 0 : 1)}${suffix}`;
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
  const [timezone, setTimezone] = useState<"local" | "utc">("local");
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
          <p>{runtime ? `${runtime.strategy} · 数据结构 v${runtime.schema_version ?? "?"} · ${runtime.commit.slice(0, 12)}` : "正在加载运行时…"}</p>
        </div>
        <div className="actions">
          <button className="secondary" onClick={() => setTimezone(timezone === "local" ? "utc" : "local")}>{timezone === "local" ? "本地时间" : "UTC"}</button>
          <button className="secondary" onClick={() => void load()}>刷新</button>
          <button className="secondary" onClick={() => setPasswordOpen(true)}>修改密码</button>
          <button className="secondary" onClick={() => void logout()}>退出登录</button>
        </div>
      </header>

      {error && <div className="error" role="alert">{error}</div>}
      <nav className="primary-nav" aria-label="管理中心页面">
        <button className={section === "overview" ? "active" : "secondary"} onClick={() => setSection("overview")}>概览</button>
        <button className={section === "activity" ? "active" : "secondary"} onClick={() => setSection("activity")}>活动记录</button>
        <button className={section === "configuration" ? "active" : "secondary"} onClick={() => setSection("configuration")}>配置</button>
        <button className={section === "route-lab" ? "active" : "secondary"} onClick={() => setSection("route-lab")}>Route Lab</button>
      </nav>

      {section === "configuration" ? (
        <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="configuration" />
      ) : section === "route-lab" ? (
        <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="route-lab" />
      ) : section === "activity" ? (
        <>
          <RequestsPanel requests={requests} runtime={runtime} loading={loading} timezone={timezone} requestWindow={requestWindow} setRequestWindow={setRequestWindow} trafficClass={trafficClass} setTrafficClass={setTrafficClass} selectedModel={selectedModel} setSelectedModel={setSelectedModel} finalStatus={finalStatus} setFinalStatus={setFinalStatus} page={page} setPage={setPage} />
          <ConfigurationPage csrf={csrf} onUnauthorized={() => setAuthenticated(false)} view="activity" />
        </>
      ) : (
        <>
          <nav aria-label="统计时间范围">
            {(["1h", "24h", "7d"] as const).map((item) => (
              <button key={item} className={windowKey === item ? "active" : "secondary"} onClick={() => setWindowKey(item)}>{item}</button>
            ))}
          </nav>
          {loading && !overview ? <div className="state">正在加载遥测数据…</div> : summary && (
            <>
          <section className="cards" aria-label="概览指标">
            <Metric label="外部请求" value={String(summary.request_count)} />
            <Metric label="判断调用" value={`${summary.judge_calls} · ${summary.judge_amplification.toFixed(2)}×`} />
            <Metric label="缓存命中" value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`} />
            <Metric label="成功率" value={`${(summary.success_rate * 100).toFixed(1)}%`} />
            <Metric label="总延迟" value={`${fmtNumber(summary.total_latency_ms.p50, " ms")} / ${fmtNumber(summary.total_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="判断延迟" value={`${fmtNumber(summary.judge_latency_ms.p50, " ms")} / ${fmtNumber(summary.judge_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="遥测状态" value={health?.telemetry?.status || health?.database.status || "未知"} hint={`队列 ${health?.telemetry?.queue_depth ?? 0}`} />
            <Metric label="丢弃事件" value={String(health?.telemetry?.dropped_events ?? 0)} hint={`${health?.telemetry?.database_errors ?? 0} 个数据库错误`} />
          </section>
          <section className="panel distribution">
            <div className="panel-title"><h2>模型分布</h2><span>{windowKey}</span></div>
            {summary.model_distribution.length === 0 ? <div className="state">此时间范围内没有路由请求。</div> : summary.model_distribution.map((item) => (
              <div className="bar-row" key={item.model || "unknown"}>
                <span>{item.model || "unknown"}</span>
                <div className="bar-track"><div className="bar" style={{ width: `${(item.count / modelMax) * 100}%` }} /></div>
                <strong>{item.count}</strong>
              </div>
            ))}
          </section>
            </>
          )}

          {false && <section className="panel">
        <div className="panel-title"><h2>请求</h2><span>仅展示结构化元数据</span></div>
        <div className="filters">
          <select aria-label="Request time window" value={requestWindow} onChange={(event) => { setPage(1); setRequestWindow(event.target.value as "1h" | "24h" | "7d"); }}>
            <option value="1h">Last hour</option><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option>
          </select>
          <select aria-label="Traffic class" value={trafficClass} onChange={(event) => { setPage(1); setTrafficClass(event.target.value); }}>
            <option value="">All traffic</option><option value="production">Production</option><option value="admin_test">Admin test</option><option value="deployment_smoke">Deployment smoke</option>
          </select>
          <select aria-label="Selected model" value={selectedModel} onChange={(event) => { setPage(1); setSelectedModel(event.target.value); }}>
            <option value="">All models</option>{runtime?.models.map((model) => <option key={model.name} value={model.name}>{model.name}</option>)}
          </select>
          <select aria-label="Final status" value={finalStatus} onChange={(event) => { setPage(1); setFinalStatus(event.target.value); }}>
            <option value="">All statuses</option><option value="success">Success</option><option value="error">Error</option><option value="disconnected">Disconnected</option>
          </select>
        </div>
        {!requests?.items.length ? <div className="state">No requests match the current filters.</div> : (
          <div className="table-wrap"><table><thead><tr><th>Time</th><th>Request</th><th>Policy</th><th>Cache / judge</th><th>Model</th><th>Status</th><th>Latency</th><th>Tokens</th></tr></thead><tbody>
            {requests?.items.map((item) => <tr key={item.event_id}>
              <td>{new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "medium", timeZone: timezone === "utc" ? "UTC" : undefined }).format(new Date(item.occurred_at))}{timezone === "utc" ? " UTC" : ""}</td>
              <td><code>{item.request_id.slice(0, 12)}</code><small>{item.traffic_class}</small></td>
              <td>{item.requested_model}<small>{item.rejudge_reason || "—"}</small></td>
              <td>{item.cache_status || "—"}<small>{item.judge_status || "—"}</small></td>
              <td>{item.selected_model || "—"}{item.fallback ? <small>fallback</small> : null}</td>
              <td><span className={`status ${item.final_status}`}>{item.final_status || "unknown"}</span><small>{item.error_category || "—"}</small></td>
              <td>{fmtNumber(item.total_latency_ms, " ms")}<small>TTFB {fmtNumber(item.first_byte_latency_ms, " ms")}</small></td>
              <td>{item.total_tokens ?? "—"}</td>
            </tr>)}
          </tbody></table></div>
        )}
        <div className="pagination"><button className="secondary" disabled={!requests || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button><span>Page {page} · {requests?.total ?? 0} requests</span><button className="secondary" disabled={!requests || page * (requests?.page_size ?? 0) >= (requests?.total ?? 0)} onClick={() => setPage((value) => value + 1)}>Next</button></div>
          </section>}
        </>
      )}
      {passwordNotice && <div className="notice" role="status">{passwordNotice}</div>}
      {passwordOpen && <div className="modal-backdrop"><form className="modal" onSubmit={changePassword}><div className="panel-title"><h2>修改密码</h2><button type="button" className="secondary" onClick={() => setPasswordOpen(false)}>关闭</button></div><label className="field"><span>当前密码</span><input type="password" value={passwordForm.current} onChange={(e) => setPasswordForm({ ...passwordForm, current: e.target.value })} required /></label><label className="field"><span>新密码</span><input type="password" minLength={8} value={passwordForm.next} onChange={(e) => setPasswordForm({ ...passwordForm, next: e.target.value })} required /></label><label className="field"><span>确认新密码</span><input type="password" minLength={8} value={passwordForm.confirm} onChange={(e) => setPasswordForm({ ...passwordForm, confirm: e.target.value })} required /></label>{passwordNotice && <div className="error">{passwordNotice}</div>}<button type="submit">保存密码</button></form></div>}
    </main>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <article className="metric"><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</article>;
}

function RequestsPanel({
  requests, runtime, loading, timezone, requestWindow, setRequestWindow, trafficClass, setTrafficClass,
  selectedModel, setSelectedModel, finalStatus, setFinalStatus, page, setPage,
}: {
  requests: RequestPage | null;
  runtime: Runtime | null;
  loading: boolean;
  timezone: "local" | "utc";
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
  return <section className="panel"><div className="panel-title"><h2>请求记录</h2><span>仅展示结构化元数据</span></div><div className="filters"><select aria-label="请求时间范围" value={requestWindow} onChange={(event) => { setPage(1); setRequestWindow(event.target.value as "1h" | "24h" | "7d"); }}><option value="1h">最近 1 小时</option><option value="24h">最近 24 小时</option><option value="7d">最近 7 天</option></select><select aria-label="流量类型" value={trafficClass} onChange={(event) => { setPage(1); setTrafficClass(event.target.value); }}><option value="">全部流量</option><option value="production">生产流量</option><option value="admin_test">管理员测试</option><option value="deployment_smoke">部署冒烟</option></select><select aria-label="已选模型" value={selectedModel} onChange={(event) => { setPage(1); setSelectedModel(event.target.value); }}><option value="">全部模型</option>{runtime?.models.map((model) => <option key={model.name} value={model.name}>{model.name}</option>)}</select><select aria-label="最终状态" value={finalStatus} onChange={(event) => { setPage(1); setFinalStatus(event.target.value); }}><option value="">全部状态</option><option value="success">成功</option><option value="error">错误</option><option value="disconnected">断开</option></select></div>{loading && !requests ? <div className="state">正在加载请求记录…</div> : !requests?.items.length ? <div className="state">没有符合条件的请求。</div> : <div className="table-wrap"><table><thead><tr><th>时间</th><th>请求</th><th>策略</th><th>缓存 / 判断</th><th>模型</th><th>状态</th><th>延迟</th><th>令牌</th></tr></thead><tbody>{requests.items.map((item) => <tr key={item.event_id}><td>{new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "medium", timeZone: timezone === "utc" ? "UTC" : undefined }).format(new Date(item.occurred_at))}</td><td><code>{item.request_id.slice(0, 12)}</code><small>{item.traffic_class}</small></td><td>{item.requested_model}<small>{item.rejudge_reason || "—"}</small></td><td>{item.cache_status || "—"}<small>{item.judge_status || "—"}</small></td><td>{item.selected_model || "—"}</td><td><span className={`status ${item.final_status}`}>{item.final_status || "未知"}</span></td><td>{fmtNumber(item.total_latency_ms, " ms")}</td><td>{item.total_tokens ?? "—"}</td></tr>)}</tbody></table></div>}<div className="pagination"><button className="secondary" disabled={!requests || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>上一页</button><span>第 {page} 页 · {requests?.total ?? 0} 条</span><button className="secondary" disabled={!requests || page * requests.page_size >= requests.total} onClick={() => setPage((value) => value + 1)}>下一页</button></div></section>;
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
