import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./main";

const emptyWindow = {
  request_count: 0,
  judge_calls: 0,
  judge_amplification: 0,
  cache_hit_rate: 0,
  success_rate: 0,
  fallback_count: 0,
  error_count: 0,
  total_latency_ms: { p50: null, p95: null, sample_count: 0 },
  judge_latency_ms: { p50: null, p95: null, sample_count: 0 },
  model_distribution: [],
};

function response(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

function requestPath(input: RequestInfo | URL) {
  const value = typeof input === "string" ? input : input.toString();
  return new URL(value, "http://localhost").pathname;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Control Center dashboard states", () => {
  it("shows a loading state while the initial API calls are pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
    render(<App />);
    expect(screen.getByText("正在加载遥测数据…")).toBeTruthy();
  });

  it("falls back to the accessible login form when the session is unauthorized", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response({ error: { message: "Administrator session is required" } }, 401)),
    );
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "LLMRouter 管理中心" }),
    ).toBeTruthy();
    expect(screen.getByLabelText("管理员密码")).toBeTruthy();
    expect(screen.getByRole("button", { name: "登录" })).toBeTruthy();
  });

  it("shows a uniform login error without echoing the submitted token", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return response({ error: { message: "Invalid administrator credentials" } }, 401);
      }
      return response({ error: { message: "Administrator session is required" } }, 401);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    const input = await screen.findByLabelText("管理员密码");
    fireEvent.change(input, { target: { value: "not-the-token" } });
    fireEvent.submit(screen.getByRole("button", { name: "登录" }).closest("form")!);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("管理员密码错误");
    expect(alert.textContent).not.toContain("not-the-token");
  });

  it("renders overview and request empty states from authenticated API data", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        switch (requestPath(input)) {
          case "/admin/api/session":
            return response({ csrf_token: "csrf-test-value" });
          case "/admin/api/overview":
            return response({
              generated_at: "2026-08-20T00:00:00+00:00",
              windows: { "1h": emptyWindow, "24h": emptyWindow, "7d": emptyWindow },
            });
          case "/admin/api/runtime":
            return response({
              strategy: "random",
              models: [],
              session_cache_entries: 0,
              commit: "test-commit",
              schema_version: 1,
            });
          case "/admin/api/health":
            return response({ status: "ok", database: { status: "ok", schema_version: 1 } });
          case "/admin/api/requests":
            return response({ items: [], page: 1, page_size: 25, total: 0 });
          default:
            return response({ error: { message: "unexpected request" } }, 500);
        }
      }),
    );
    render(<App />);
    expect(await screen.findByText("此时间范围内没有路由请求。")).toBeTruthy();
    expect(screen.getByRole("button", { name: "活动记录" })).toBeTruthy();
  });

  it("renders a sanitized API error state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response({ error: { message: "Telemetry is unavailable" } }, 503)),
    );
    render(<App />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("遥测数据不可用");
    });
  });
});
