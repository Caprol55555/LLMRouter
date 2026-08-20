import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConfigurationPage } from "./configuration";

const snapshot = {
  router: {
    judge_model: "judge-upstream",
    default_model: "glm",
    allowed_models: ["glm", "qwen"],
    judge_timeout_seconds: 10,
    judge_max_tokens: 128,
    routing_context_chars: 4000,
    judge_system_prompt: "Choose a backend.",
  },
  session_routing: {
    enabled: true,
    ttl_seconds: 600,
    rejudge_every_user_turns: 0,
    allowed_rejudge_intervals: [1, 3, 5],
    max_entries: 1000,
    rejudge_on_modality_change: true,
    rejudge_on_backend_error: true,
  },
  llms: {
    glm: { model: "glm-upstream", description: "General", max_tokens: 4096, context_limit: 32768 },
    qwen: { model: "qwen-upstream", description: "Structured", max_tokens: 4096, context_limit: 32768 },
  },
};

const active = {
  version_id: 1,
  version_number: 1,
  parent_version_id: null,
  source: "yaml_baseline",
  release_notes: "Initial YAML baseline",
  created_at: "2026-08-20T00:00:00+00:00",
  is_active: true,
  publish_state: "active",
  snapshot,
};

const readOnly = {
  serve: { host: "127.0.0.1", port: 8000 },
  router: { strategy: "llm" },
  security: {
    require_inbound_auth: true,
    forbidden_upstream_models: [],
    forbidden_upstream_prefixes: ["lr/"],
  },
  models: {
    glm: {
      provider: "nine_router",
      provider_type: "openai",
      base_url: "http://router.internal/v1",
      auth_mode: "bearer",
      chat_path: "/chat/completions",
      credential: { source: "model_env", name: "GLM_KEY", configured: true },
    },
    qwen: {
      provider: "nine_router",
      provider_type: "openai",
      base_url: "http://router.internal/v1",
      auth_mode: "bearer",
      chat_path: "/chat/completions",
      credential: { source: "model_env", name: "QWEN_KEY", configured: true },
    },
  },
};

function draft(status: "editing" | "ready" | "finalized" = "editing", issues: unknown[] = []) {
  return {
    draft_id: "draft-test-id",
    base_version_id: 1,
    finalized_version_id: status === "finalized" ? 2 : null,
    status,
    revision: 1,
    validation_issues: issues,
    release_notes: status === "ready" ? "Prefer qwen" : "",
    created_at: "2026-08-20T00:01:00+00:00",
    updated_at: "2026-08-20T00:01:00+00:00",
    snapshot: JSON.parse(JSON.stringify(snapshot)),
  };
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  }));
}

function details(input: RequestInfo | URL, init?: RequestInit) {
  const url = new URL(typeof input === "string" ? input : input.toString(), "http://localhost");
  return { path: url.pathname, method: init?.method || "GET", init };
}

function pageSummary(drafts: ReturnType<typeof draft>[] = []) {
  return {
    active,
    read_only: readOnly,
    drafts: drafts.map(({ snapshot: _snapshot, ...item }) => item),
  };
}

function commonGet(path: string, drafts: ReturnType<typeof draft>[] = []) {
  if (path === "/admin/api/configuration") return response(pageSummary(drafts));
  if (path === "/admin/api/configuration/versions") {
    return response({ items: [active], page: 1, page_size: 25, total: 1 });
  }
  if (path === "/admin/api/audit") return response({ items: [], page: 1, page_size: 20, total: 0 });
  if (path.endsWith("/diff")) return response({ changes: [] });
  if (path === "/admin/api/configuration/drafts/draft-test-id") return response(drafts[0]);
  return response({ error: { message: `Unexpected request ${path}` } }, 500);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Configuration page", () => {
  it("renders loading and empty states", async () => {
    const pending = vi.fn(() => new Promise<Response>(() => undefined));
    vi.stubGlobal("fetch", pending);
    const view = render(<ConfigurationPage csrf="csrf-value" onUnauthorized={vi.fn()} />);
    expect(screen.getByText("Loading configuration…")).toBeTruthy();
    view.unmount();

    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => commonGet(details(input).path)));
    render(<ConfigurationPage csrf="csrf-value" onUnauthorized={vi.fn()} />);
    expect(await screen.findByText("No drafts. Create one from the active version.")).toBeTruthy();
    expect(screen.getByText("No recent management events.")).toBeTruthy();
  });

  it("renders a sanitized error and revokes unauthorized state", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response({ error: { message: "Configuration storage is unavailable" } }, 503)));
    const unauthorized = vi.fn();
    const view = render(<ConfigurationPage csrf="csrf-value" onUnauthorized={unauthorized} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Configuration storage is unavailable");
    view.unmount();

    vi.stubGlobal("fetch", vi.fn(() => response({ error: { message: "Administrator session is required" } }, 401)));
    render(<ConfigurationPage csrf="csrf-value" onUnauthorized={unauthorized} />);
    await waitFor(() => expect(unauthorized).toHaveBeenCalled());
  });

  it("creates, edits, saves, and validates a draft with protected writes", async () => {
    let current = draft();
    let exists = false;
    const writes: Array<{ path: string; method: string; init?: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = details(input, init);
      if (request.method === "POST" && request.path === "/admin/api/configuration/drafts") {
        current = draft();
        exists = true;
        writes.push(request);
        return response(current, 201);
      }
      if (request.method === "PUT") {
        writes.push(request);
        const body = JSON.parse(String(init?.body));
        current = { ...current, revision: 2, snapshot: body.snapshot, release_notes: body.release_notes };
        return response(current);
      }
      if (request.method === "POST" && request.path.endsWith("/validate")) {
        writes.push(request);
        current = { ...current, status: "ready", validation_issues: [] };
        return response(current);
      }
      return commonGet(request.path, exists ? [current] : []);
    }));

    render(<ConfigurationPage csrf="csrf-value" onUnauthorized={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Create draft" }));
    const defaultModel = await screen.findByLabelText("Default model");
    fireEvent.change(defaultModel, { target: { value: "qwen" } });
    fireEvent.change(screen.getByLabelText("Release notes"), { target: { value: "Prefer qwen" } });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));

    expect(await screen.findByText(/Validation passed/)).toBeTruthy();
    expect(writes.map((item) => item.method)).toEqual(["POST", "PUT", "POST"]);
    for (const write of writes) {
      const headers = new Headers(write.init?.headers);
      expect(headers.get("X-CSRF-Token")).toBe("csrf-value");
      expect(headers.get("Origin")).toBe(window.location.origin);
    }
    expect(current.snapshot.router.default_model).toBe("qwen");
    expect(screen.getByRole("button", { name: "Finalize pending version" }).hasAttribute("disabled")).toBe(false);
    expect(screen.getByLabelText("Managed configuration YAML").textContent).toContain("default_model: \"qwen\"");
  });

  it("shows validation issues and blocks finalization", async () => {
    const invalid = draft("editing", [{
      code: "recursive_upstream_model",
      path: "/llms/glm/model",
      message: "Router-internal model IDs cannot be used as upstream candidates",
    }]);
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => commonGet(details(input).path, [invalid])));
    render(<ConfigurationPage csrf="csrf-value" onUnauthorized={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain(
      "Router-internal model IDs cannot be used as upstream candidates",
    );
    expect(screen.getByRole("button", { name: "Finalize pending version" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByLabelText("Judge model")).toBeTruthy();
    expect(screen.getByLabelText("TTL seconds")).toBeTruthy();
  });

  it("finalizes to a pending version and exposes activation controls", async () => {
    let current = draft("ready");
    const pending = { ...active, version_id: 2, version_number: 2, is_active: false, publish_state: "pending", source: "draft" };
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const request = details(input, init);
      if (request.method === "POST" && request.path.endsWith("/finalize")) {
        current = { ...current, status: "finalized", finalized_version_id: 2 };
        return response(pending, 201);
      }
      if (request.path === "/admin/api/configuration/versions") {
        return response({ items: [pending, active], page: 1, page_size: 25, total: 2 });
      }
      return commonGet(request.path, [current]);
    }));
    render(<ConfigurationPage csrf="csrf-value" onUnauthorized={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Finalize pending version" }));
    expect(await screen.findByText(/Version 2 is pending/)).toBeTruthy();
    expect(screen.getByText("pending")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /publish/i })).toBeNull();
    expect(screen.getByRole("button", { name: /activate/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /rollback/i })).toBeTruthy();
  });
});
