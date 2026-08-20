from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from openclaw_router.config import ControlCenterConfig, LLMConfig, OpenClawConfig, RouterConfig
from openclaw_router.control_center.auth import AdminAuthService
from openclaw_router.control_center.telemetry import RoutingEvent
from openclaw_router.server import create_app


ORIGIN = {"Origin": "http://localhost", "Host": "localhost"}


def build_config(data_dir: Path, *, enabled=True, attempts=5):
    return OpenClawConfig(
        router=RouterConfig(strategy="random"),
        llms={
            "glm": LLMConfig(
                name="glm",
                provider="test",
                model_id="glm",
                base_url="https://example.test/v1",
                description="General model",
            )
        },
        control_center=ControlCenterConfig(
            enabled=enabled,
            data_dir=str(data_dir),
            admin_login_max_attempts=attempts,
        ),
    )


def login(client: TestClient, token="admin-secret"):
    return client.post("/admin/api/login", headers=ORIGIN, json={"token": token})


def test_admin_session_expiry_and_csrf_recovery():
    now = [100.0]
    auth = AdminAuthService(
        "admin-secret",
        session_ttl_seconds=300,
        login_window_seconds=60,
        login_max_attempts=5,
        clock=lambda: now[0],
    )
    session, result = auth.login("admin-secret", "client")
    assert result == "ok"
    assert session is not None
    assert auth.verify(session.session_token)
    assert auth.csrf_for_session(session.session_token) == session.csrf_token
    assert auth.verify(session.session_token, session.csrf_token)
    now[0] = 401.0
    assert auth.verify(session.session_token) is False
    assert auth.csrf_for_session(session.session_token) is None
    assert auth.active_session_count() == 0
    assert "admin-secret" not in repr(vars(auth))


def test_admin_auth_state_is_capacity_bounded():
    auth = AdminAuthService(
        "admin-secret",
        session_ttl_seconds=300,
        login_window_seconds=60,
        login_max_attempts=5,
        max_active_sessions=2,
        max_tracked_clients=3,
    )

    sessions = []
    for client_key in ("one", "two", "three"):
        session, result = auth.login("admin-secret", client_key)
        assert result == "ok"
        assert session is not None
        sessions.append(session)
    assert auth.active_session_count() == 2
    assert auth.verify(sessions[0].session_token) is False
    assert auth.verify(sessions[-1].session_token) is True

    for client_key in ("a", "b", "c", "d"):
        session, result = auth.login("wrong", client_key)
        assert session is None
        assert result == "invalid_credentials"
    assert len(auth._failures) == 3
    assert "a" not in auth._failures


def test_disabled_admin_routes_remain_404_without_io(temp_data_dir: Path):
    app = create_app(config=build_config(temp_data_dir, enabled=False))
    client = TestClient(app)
    assert login(client).status_code == 404
    assert client.get("/admin/api/status").status_code == 404
    assert not (temp_data_dir / "control-center.db").exists()


def test_missing_admin_token_fails_closed(temp_data_dir: Path, monkeypatch):
    monkeypatch.delenv("LLMROUTER_ADMIN_TOKEN", raising=False)
    client = TestClient(create_app(config=build_config(temp_data_dir)))
    assert login(client).status_code == 503
    assert client.get("/admin/api/overview").status_code == 401


def test_login_cookie_origin_rate_limit_and_uniform_failure(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    client = TestClient(create_app(config=build_config(temp_data_dir, attempts=2)))
    assert client.post("/admin/api/login", json={"token": "admin-secret"}).status_code == 403
    assert client.post(
        "/admin/api/login",
        headers={"Origin": "http://localhost:9999", "Host": "localhost"},
        json={"token": "admin-secret"},
    ).status_code == 403
    first = login(client, "wrong-one")
    second = login(client, "wrong-two")
    limited = login(client, "admin-secret")
    assert first.status_code == second.status_code == 401
    assert first.json() == second.json()
    assert limited.status_code == 429
    assert limited.json()["error"]["message"] == first.json()["error"]["message"]

    fresh = TestClient(create_app(config=build_config(temp_data_dir)))
    success = login(fresh)
    assert success.status_code == 200
    assert "admin-secret" not in success.text
    cookie = success.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" not in cookie
    assert "path=/admin" in cookie
    assert success.json()["csrf_token"]


def test_login_rejects_extra_fields_and_oversized_body(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    client = TestClient(create_app(config=build_config(temp_data_dir)))
    extra = client.post(
        "/admin/api/login",
        headers=ORIGIN,
        json={"token": "admin-secret", "unexpected": True},
    )
    assert extra.status_code == 401
    assert extra.json()["error"]["code"] == "login_failed"
    assert "admin-secret" not in extra.text

    oversized = client.post(
        "/admin/api/login",
        headers={**ORIGIN, "Content-Type": "application/json"},
        content=b'{"token":"' + (b"a" * 8192) + b'"}',
    )
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"


def test_https_login_sets_secure_cookie(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    client = TestClient(
        create_app(config=build_config(temp_data_dir)),
        base_url="https://localhost",
    )
    response = client.post(
        "/admin/api/login",
        headers={"Origin": "https://localhost", "Host": "localhost"},
        json={"token": "admin-secret"},
    )
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()


def test_inbound_key_cannot_authenticate_admin(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    config.security.inbound_api_key = "ordinary-router-key"
    client = TestClient(create_app(config=config))
    response = login(client, "ordinary-router-key")
    assert response.status_code == 401


def test_authenticated_read_apis_logout_and_security_headers(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    app = create_app(config=build_config(temp_data_dir))
    with TestClient(app) as client:
        auth = login(client)
        csrf = auth.json()["csrf_token"]
        for path in (
            "/admin/api/session",
            "/admin/api/status",
            "/admin/api/overview",
            "/admin/api/requests",
            "/admin/api/health",
            "/admin/api/runtime",
        ):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["permissions-policy"] == (
                "camera=(), microphone=(), geolocation=()"
            )
            assert "access-control-allow-origin" not in response.headers

        recovered_csrf = client.get("/admin/api/session").json()["csrf_token"]
        assert recovered_csrf == csrf

        assert client.post("/admin/api/logout", headers=ORIGIN).status_code == 401
        assert client.post(
            "/admin/api/logout",
            headers={**ORIGIN, "X-CSRF-Token": "wrong"},
        ).status_code == 401
        assert client.post(
            "/admin/api/logout",
            headers={**ORIGIN, "X-CSRF-Token": recovered_csrf},
        ).status_code == 200
        assert client.get("/admin/api/overview").status_code == 401


def test_all_management_data_routes_default_to_unauthorized(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    client = TestClient(create_app(config=build_config(temp_data_dir)))
    for path in (
        "/admin/api/session",
        "/admin/api/status",
        "/admin/api/overview",
        "/admin/api/requests",
        "/admin/api/health",
        "/admin/api/runtime",
    ):
        response = client.get(path)
        assert response.status_code == 401, (path, response.text)
        assert response.json()["error"]["code"] == "admin_unauthorized"
        assert response.headers["cache-control"] == "no-store"


def test_overview_and_paginated_requests_match_sqlite(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    app = create_app(config=build_config(temp_data_dir))
    runtime = app.state.control_center
    for index, model in enumerate(("glm", "glm", "qwen")):
        runtime.record(
            RoutingEvent.create(
                request_id=f"request-{index}",
                event_kind="request_completed",
                traffic_class="production",
                transport="http",
                requested_model="auto",
                selected_model=model,
                final_status="success" if index < 2 else "error",
                cache_status="hit" if index == 1 else "miss",
                judge_status="not_called" if index == 1 else "success",
                total_latency_ms=10.0 + index,
                total_tokens=5,
            )
        )
    assert runtime.telemetry.flush(timeout=2.0)
    with TestClient(app) as client:
        assert login(client).status_code == 200
        overview = client.get("/admin/api/overview").json()["windows"]["24h"]
        assert overview["request_count"] == 3
        assert overview["judge_calls"] == 2
        assert overview["cache_hit_rate"] == 1 / 3
        page = client.get("/admin/api/requests?page=1&page_size=2").json()
        assert page["total"] == 3
        assert len(page["items"]) == 2
        filtered = client.get("/admin/api/requests?selected_model=qwen").json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["final_status"] == "error"
        assert client.get("/admin/api/requests?since=not-a-time").status_code == 422
        assert client.get("/admin/api/requests?since=2026-08-20T00:00:00").status_code == 422


def test_dashboard_serves_built_spa_with_cache_policy(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    dashboard = Path(__file__).parents[2] / "control_center_ui" / "dist"
    assert (dashboard / "index.html").is_file(), "run npm run build before backend tests"
    monkeypatch.setenv("LLMROUTER_DASHBOARD_DIR", str(dashboard))
    client = TestClient(create_app(config=build_config(temp_data_dir)))
    index = client.get("/dashboard")
    assert index.status_code == 200
    assert "LLMRouter Control Center" in index.text
    assert index.headers["cache-control"] == "no-store"
    asset_name = next((dashboard / "assets").iterdir()).name
    asset = client.get(f"/dashboard/assets/{asset_name}")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
