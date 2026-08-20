import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
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
};
type Health = {
  status: string;
  database: { status: string; schema_version: number | null };
  telemetry?: { status: string; dropped_events: number; database_errors: number; queue_depth: number };
};

class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, body?.error?.message || `Request failed (${response.status})`);
  }
  return body as T;
}

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
          <div className="eyebrow">LOCAL ADMINISTRATION</div>
          <h1>LLMRouter Control Center</h1>
          <p>Use the administrator token configured on this router. The token is never stored by the browser.</p>
          <label htmlFor="admin-token">Administrator token</label>
          <input
            id="admin-token"
            type="password"
            autoComplete="current-password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            required
          />
          {loginError && <div className="error" role="alert">{loginError}</div>}
          <button type="submit">Sign in</button>
        </form>
      </main>
    );
  }

  return (
    <main className="shell">
      <header>
        <div>
          <div className="eyebrow">ROUTING OBSERVABILITY</div>
          <h1>Control Center</h1>
          <p>{runtime ? `${runtime.strategy} · schema v${runtime.schema_version ?? "?"} · ${runtime.commit.slice(0, 12)}` : "Loading runtime…"}</p>
        </div>
        <div className="actions">
          <button className="secondary" onClick={() => setTimezone(timezone === "local" ? "utc" : "local")}>{timezone === "local" ? "Local time" : "UTC"}</button>
          <button className="secondary" onClick={() => void load()}>Refresh</button>
          <button className="secondary" onClick={() => void logout()}>Sign out</button>
        </div>
      </header>

      {error && <div className="error" role="alert">{error}</div>}
      <nav aria-label="Metric window">
        {(["1h", "24h", "7d"] as const).map((item) => (
          <button key={item} className={windowKey === item ? "active" : "secondary"} onClick={() => setWindowKey(item)}>{item}</button>
        ))}
      </nav>

      {loading && !overview ? <div className="state">Loading telemetry…</div> : summary && (
        <>
          <section className="cards" aria-label="Overview metrics">
            <Metric label="Outer requests" value={String(summary.request_count)} />
            <Metric label="Judge calls" value={`${summary.judge_calls} · ${summary.judge_amplification.toFixed(2)}×`} />
            <Metric label="Cache hit" value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`} />
            <Metric label="Success" value={`${(summary.success_rate * 100).toFixed(1)}%`} />
            <Metric label="Total latency" value={`${fmtNumber(summary.total_latency_ms.p50, " ms")} / ${fmtNumber(summary.total_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="Judge latency" value={`${fmtNumber(summary.judge_latency_ms.p50, " ms")} / ${fmtNumber(summary.judge_latency_ms.p95, " ms")}`} hint="p50 / p95" />
            <Metric label="Telemetry" value={health?.telemetry?.status || health?.database.status || "unknown"} hint={`queue ${health?.telemetry?.queue_depth ?? 0}`} />
            <Metric label="Dropped events" value={String(health?.telemetry?.dropped_events ?? 0)} hint={`${health?.telemetry?.database_errors ?? 0} database errors`} />
          </section>
          <section className="panel distribution">
            <div className="panel-title"><h2>Model selection</h2><span>{windowKey}</span></div>
            {summary.model_distribution.length === 0 ? <div className="state">No routed requests in this window.</div> : summary.model_distribution.map((item) => (
              <div className="bar-row" key={item.model || "unknown"}>
                <span>{item.model || "unknown"}</span>
                <div className="bar-track"><div className="bar" style={{ width: `${(item.count / modelMax) * 100}%` }} /></div>
                <strong>{item.count}</strong>
              </div>
            ))}
          </section>
        </>
      )}

      <section className="panel">
        <div className="panel-title"><h2>Requests</h2><span>Structured metadata only</span></div>
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
            {requests.items.map((item) => <tr key={item.event_id}>
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
        <div className="pagination"><button className="secondary" disabled={!requests || page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</button><span>Page {page} · {requests?.total ?? 0} requests</span><button className="secondary" disabled={!requests || page * requests.page_size >= requests.total} onClick={() => setPage((value) => value + 1)}>Next</button></div>
      </section>
    </main>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <article className="metric"><span>{label}</span><strong>{value}</strong>{hint && <small>{hint}</small>}</article>;
}

const root = document.getElementById("root");
if (root) createRoot(root).render(<App />);
