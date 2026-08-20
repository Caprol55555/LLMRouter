from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import httpx
from fastapi.testclient import TestClient

from openclaw_router.config import (
    ControlCenterConfig,
    LLMConfig,
    OpenClawConfig,
    RouterConfig,
    SessionRoutingConfig,
)
from openclaw_router.control_center.migrations import Database, migrate
from openclaw_router.control_center.queries import TelemetryQueryService
from openclaw_router.control_center.telemetry import RoutingEvent, TelemetryService
from openclaw_router.server import create_app


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload


class FakeStreamResponse:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class TelemetryAsyncClient:
    judge_calls = 0
    backend_calls = 0
    judge_delay = 0.0
    judge_mode = "success"
    stream_lines = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        if json["model"] == "judge-model":
            type(self).judge_calls += 1
            if type(self).judge_delay:
                await asyncio.sleep(type(self).judge_delay)
            if type(self).judge_mode == "timeout":
                raise httpx.TimeoutException("private timeout detail")
            if type(self).judge_mode == "http_error":
                return FakeResponse({}, status_code=503, text="private upstream body")
            if type(self).judge_mode == "parse_error":
                return FakeResponse(
                    {"choices": [{"message": {"content": "not-json private content"}}]}
                )
            if type(self).judge_mode == "out_of_allowlist":
                return FakeResponse(
                    {"choices": [{"message": {"content": '{"model":"secret-model"}'}}]}
                )
            return FakeResponse(
                {"choices": [{"message": {"content": '{"model":"qwen"}'}}]}
            )
        type(self).backend_calls += 1
        return FakeResponse(
            {
                "id": "chatcmpl-telemetry",
                "object": "chat.completion",
                "model": json["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        )

    def stream(self, method, url, headers=None, json=None, timeout=None):
        type(self).backend_calls += 1
        return FakeStreamResponse(type(self).stream_lines)


@pytest.fixture(autouse=True)
def reset_fake_client():
    TelemetryAsyncClient.judge_calls = 0
    TelemetryAsyncClient.backend_calls = 0
    TelemetryAsyncClient.judge_delay = 0.0
    TelemetryAsyncClient.judge_mode = "success"
    TelemetryAsyncClient.stream_lines = []


def build_config(
    data_dir: Path,
    *,
    queue_capacity=2048,
    batch_size=100,
    judge_auth_mode="none",
):
    return OpenClawConfig(
        show_model_prefix=False,
        router=RouterConfig(
            strategy="llm",
            provider="test",
            model="judge-model",
            base_url="https://judge.test/v1",
            auth_mode=judge_auth_mode,
            default_model="glm",
        ),
        llms={
            name: LLMConfig(
                name=name,
                provider="test",
                model_id=name,
                base_url="https://backend.test/v1",
                auth_mode="none",
                description=name,
            )
            for name in ("glm", "qwen")
        },
        session_routing=SessionRoutingConfig(
            enabled=True,
            ttl_seconds=1800,
            rejudge_every_user_turns=2,
            allowed_rejudge_intervals=[1, 2, 3],
            max_entries=100,
        ),
        control_center=ControlCenterConfig(
            enabled=True,
            data_dir=str(data_dir),
            telemetry_queue_capacity=queue_capacity,
            telemetry_batch_size=batch_size,
            telemetry_flush_interval_seconds=0.05,
        ),
    )


def _request(model="auto", user="telemetry-session", stream=False):
    return {
        "model": model,
        "user": user,
        "stream": stream,
        "messages": [{"role": "user", "content": "private prompt text"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "private_tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }


def _run_request(app, payload, headers=None):
    with patch("openclaw_router.routers.httpx.AsyncClient", TelemetryAsyncClient), patch(
        "openclaw_router.server.httpx.AsyncClient", TelemetryAsyncClient
    ):
        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", json=payload, headers=headers or {})
            assert app.state.control_center.telemetry.flush(timeout=2.0)
            return response


def test_event_schema_excludes_sensitive_content():
    names = {field.name for field in fields(RoutingEvent)}
    forbidden = {
        "prompt",
        "messages",
        "headers",
        "authorization",
        "api_key",
        "cookie",
        "tool_calls",
        "tool_arguments",
        "response_body",
    }
    assert names.isdisjoint(forbidden)
    with pytest.raises(TypeError, match="unsupported routing event field"):
        RoutingEvent.create(
            request_id="request",
            event_kind="request_started",
            traffic_class="production",
            transport="http",
            requested_model="auto",
            prompt="must not be accepted",
        )


def test_migration_v1_query_and_hourly_aggregate(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    assert migrate(str(db_path)) == 1
    service = TelemetryService(
        ControlCenterConfig(
            enabled=True,
            data_dir=str(temp_data_dir),
            telemetry_flush_interval_seconds=0.05,
        ),
        str(db_path),
    )
    request_id = "aggregate-request"
    assert service.submit(
        RoutingEvent.create(
            request_id=request_id,
            event_kind="request_completed",
            traffic_class="production",
            transport="http",
            requested_model="auto",
            selected_model="qwen",
            final_status="success",
            cache_status="hit",
            judge_status="not_called",
            total_latency_ms=12.5,
            prompt_tokens=3,
            completion_tokens=2,
            total_tokens=5,
        )
    )
    assert service.flush(timeout=2.0)
    assert service.stop(timeout=2.0)

    queries = TelemetryQueryService(str(db_path))
    events = queries.list_events(request_id=request_id)
    aggregates = queries.list_hourly_aggregates()
    assert len(events) == 1
    assert events[0]["selected_model"] == "qwen"
    assert len(aggregates) == 1
    assert aggregates[0]["request_count"] == 1
    assert aggregates[0]["cache_hit_count"] == 1
    assert aggregates[0]["judge_call_count"] == 0
    assert aggregates[0]["total_tokens"] == 5


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"telemetry_queue_capacity": 0}, "queue_capacity"),
        ({"telemetry_batch_size": 0}, "batch_size"),
        (
            {"telemetry_queue_capacity": 2, "telemetry_batch_size": 3},
            "batch_size",
        ),
        ({"telemetry_flush_interval_seconds": 0.01}, "flush_interval"),
        ({"telemetry_retention_days": 0}, "retention_days"),
        (
            {"telemetry_retention_days": 30, "telemetry_aggregate_retention_days": 7},
            "aggregate_retention_days",
        ),
    ],
)
def test_telemetry_config_bounds_rejected(temp_data_dir: Path, values, message):
    config = ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir), **values)
    with pytest.raises(ValueError, match=message):
        config.validate()


def test_auto_request_records_one_start_judge_and_final(temp_data_dir: Path):
    app = create_app(config=build_config(temp_data_dir))
    response = _run_request(
        app,
        _request(),
        headers={"Authorization": "Bearer secret-not-stored", "Cookie": "private=cookie"},
    )
    assert response.status_code == 200
    events = app.state.control_center.queries.list_events(limit=20)
    kinds = [event["event_kind"] for event in events]
    assert kinds.count("request_started") == 1
    assert kinds.count("judge_completed") == 1
    assert kinds.count("request_completed") == 1
    assert len({event["request_id"] for event in events}) == 1
    final = next(event for event in events if event["event_kind"] == "request_completed")
    assert final["selected_model"] == "qwen"
    assert final["judge_status"] == "success"
    assert final["prompt_tokens"] == 3
    assert final["completion_tokens"] == 2
    assert final["total_tokens"] == 5
    stored = json.dumps(events)
    assert "private prompt text" not in stored
    assert "private_tool" not in stored
    assert "secret-not-stored" not in stored
    assert "private=cookie" not in stored


def test_unknown_client_model_is_not_persisted_verbatim(temp_data_dir: Path):
    app = create_app(config=build_config(temp_data_dir))
    response = _run_request(app, _request("sk-private-model-secret"))
    assert response.status_code == 404
    events = app.state.control_center.queries.list_events(limit=10)
    assert len(events) == 2
    assert {event["event_kind"] for event in events} == {
        "request_started",
        "request_completed",
    }
    assert {event["requested_model"] for event in events} == {"invalid"}
    assert "sk-private-model-secret" not in json.dumps(events)


@pytest.mark.parametrize(
    "judge_mode",
    ["http_error", "timeout", "parse_error", "out_of_allowlist"],
)
def test_judge_failures_are_structured_and_sanitized(temp_data_dir: Path, judge_mode):
    TelemetryAsyncClient.judge_mode = judge_mode
    app = create_app(config=build_config(temp_data_dir))
    response = _run_request(app, _request("auto"))
    assert response.status_code == 200
    events = app.state.control_center.queries.list_events(limit=10)
    judge = next(event for event in events if event["event_kind"] == "judge_completed")
    final = next(event for event in events if event["event_kind"] == "request_completed")
    assert judge["judge_status"] == judge_mode
    assert final["judge_status"] == judge_mode
    assert judge["fallback"] == 1
    assert final["fallback"] == 1
    stored = json.dumps(events)
    assert "private upstream body" not in stored
    assert "private timeout detail" not in stored
    assert "not-json private content" not in stored
    assert "secret-model" not in stored


def test_missing_judge_key_records_no_actual_judge_event(temp_data_dir: Path):
    app = create_app(
        config=build_config(temp_data_dir, judge_auth_mode="bearer")
    )
    response = _run_request(app, _request("auto"))
    assert response.status_code == 200
    events = app.state.control_center.queries.list_events(limit=10)
    assert all(event["event_kind"] != "judge_completed" for event in events)
    final = next(event for event in events if event["event_kind"] == "request_completed")
    assert final["judge_status"] == "no_api_key"
    assert final["fallback"] == 1
    assert TelemetryAsyncClient.judge_calls == 0


def test_explicit_and_cache_hit_do_not_add_judge_calls(temp_data_dir: Path):
    app = create_app(config=build_config(temp_data_dir))
    with patch("openclaw_router.routers.httpx.AsyncClient", TelemetryAsyncClient), patch(
        "openclaw_router.server.httpx.AsyncClient", TelemetryAsyncClient
    ):
        with TestClient(app) as client:
            assert client.post("/v1/chat/completions", json=_request("glm")).status_code == 200
            assert client.post("/v1/chat/completions", json=_request("auto:once")).status_code == 200
            assert client.post("/v1/chat/completions", json=_request("auto:once")).status_code == 200
            assert app.state.control_center.telemetry.flush(timeout=2.0)

    events = app.state.control_center.queries.list_events(limit=50)
    assert sum(event["event_kind"] == "judge_completed" for event in events) == 1
    finals = [event for event in events if event["event_kind"] == "request_completed"]
    assert sorted(event["cache_status"] for event in finals) == ["hit", "miss", "not_applicable"]
    assert TelemetryAsyncClient.judge_calls == 1


def test_five_concurrent_requests_preserve_singleflight_counts(temp_data_dir: Path):
    TelemetryAsyncClient.judge_delay = 0.05
    app = create_app(config=build_config(temp_data_dir))
    with patch("openclaw_router.routers.httpx.AsyncClient", TelemetryAsyncClient), patch(
        "openclaw_router.server.httpx.AsyncClient", TelemetryAsyncClient
    ):
        with TestClient(app) as client:
            with ThreadPoolExecutor(max_workers=5) as executor:
                responses = list(
                    executor.map(
                        lambda _: client.post("/v1/chat/completions", json=_request("auto:once")),
                        range(5),
                    )
                )
            assert all(response.status_code == 200 for response in responses)
            assert app.state.control_center.telemetry.flush(timeout=2.0)

    events = app.state.control_center.queries.list_events(limit=50)
    assert sum(event["event_kind"] == "request_started" for event in events) == 5
    assert sum(event["event_kind"] == "judge_completed" for event in events) == 1
    finals = [event for event in events if event["event_kind"] == "request_completed"]
    assert len(finals) == 5
    assert sum(event["cache_status"] == "miss" for event in finals) == 1
    assert sum(event["cache_status"] == "hit" for event in finals) == 4
    assert TelemetryAsyncClient.judge_calls == 1


def test_queue_full_is_non_blocking_and_counted(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))
    config = ControlCenterConfig(
        enabled=True,
        data_dir=str(temp_data_dir),
        telemetry_queue_capacity=1,
        telemetry_batch_size=1,
    )
    service = TelemetryService(config, str(db_path))
    service.start = lambda: None
    event = RoutingEvent.create(
        request_id="queue",
        event_kind="request_started",
        traffic_class="production",
        transport="http",
        requested_model="auto",
    )
    started = time.perf_counter()
    assert service.submit(event) is True
    assert service.submit(event) is False
    assert time.perf_counter() - started < 0.05
    snapshot = service.snapshot()
    assert snapshot.dropped_events == 1
    assert snapshot.last_error_category == "queue_full"


def test_writer_failure_does_not_delay_or_fail_inference(temp_data_dir: Path):
    app = create_app(config=build_config(temp_data_dir))
    service = app.state.control_center.telemetry

    def fail_write(connection, batch):
        raise sqlite3.OperationalError("database is locked and secret-value")

    service._write_batch = fail_write
    started = time.perf_counter()
    response = _run_request(app, _request("glm"))
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 1.0
    snapshot = service.snapshot()
    assert snapshot.database_errors >= 1
    assert snapshot.dropped_events >= 1
    assert snapshot.last_error_category == "OperationalError"


def test_retention_deletes_expired_raw_and_aggregate_rows(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))
    old_time = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    database = Database(str(db_path))
    connection = database.connect()
    try:
        connection.execute(
            "INSERT INTO routing_events (event_id, request_id, occurred_at, event_kind, "
            "traffic_class, transport, requested_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old-event", "old-request", old_time, "request_started", "production", "http", "auto"),
        )
        connection.execute(
            "INSERT INTO routing_aggregates_hourly (bucket_start, traffic_class, requested_model, "
            "selected_model, final_status) VALUES (?, ?, ?, ?, ?)",
            (old_time, "production", "auto", "qwen", "success"),
        )
    finally:
        database.close()

    service = TelemetryService(
        ControlCenterConfig(
            enabled=True,
            data_dir=str(temp_data_dir),
            telemetry_retention_days=7,
            telemetry_aggregate_retention_days=90,
            telemetry_flush_interval_seconds=0.05,
        ),
        str(db_path),
    )
    assert service.submit(
        RoutingEvent.create(
            request_id="new-request",
            event_kind="request_started",
            traffic_class="production",
            transport="http",
            requested_model="auto",
        )
    )
    assert service.flush(timeout=2.0)
    assert service.stop(timeout=2.0)
    queries = TelemetryQueryService(str(db_path))
    assert queries.list_events(request_id="old-request") == []
    assert queries.list_hourly_aggregates() == []


def test_streaming_records_first_byte_and_reliable_usage(temp_data_dir: Path):
    TelemetryAsyncClient.stream_lines = [
        'data: {"id":"1","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}',
        'data: {"id":"1","choices":[],"usage":{"prompt_tokens":7,"completion_tokens":4,"total_tokens":11}}',
        "data: [DONE]",
    ]
    app = create_app(config=build_config(temp_data_dir))
    response = _run_request(app, _request("glm", stream=True))
    assert response.status_code == 200
    final = next(
        event
        for event in app.state.control_center.queries.list_events(limit=10)
        if event["event_kind"] == "request_completed"
    )
    assert final["final_status"] == "success"
    assert final["first_byte_latency_ms"] is not None
    assert final["first_byte_latency_ms"] >= 0
    assert final["prompt_tokens"] == 7
    assert final["completion_tokens"] == 4
    assert final["total_tokens"] == 11


def test_streaming_error_records_exactly_one_sanitized_final(temp_data_dir: Path):
    class FailingStreamResponse(FakeStreamResponse):
        async def aiter_lines(self):
            yield 'data: {"id":"1","choices":[{"index":0,"delta":{"content":"ok"}}]}'
            raise RuntimeError("private stream failure")

    class FailingStreamingClient(TelemetryAsyncClient):
        def stream(self, method, url, headers=None, json=None, timeout=None):
            return FailingStreamResponse([])

    app = create_app(config=build_config(temp_data_dir))
    with patch("openclaw_router.server.httpx.AsyncClient", FailingStreamingClient):
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions", json=_request("glm", stream=True)
            )
            assert response.status_code == 200
            assert app.state.control_center.telemetry.flush(timeout=2.0)

    events = app.state.control_center.queries.list_events(limit=10)
    assert sum(event["event_kind"] == "request_started" for event in events) == 1
    assert sum(event["event_kind"] == "request_completed" for event in events) == 1
    final = next(event for event in events if event["event_kind"] == "request_completed")
    assert final["final_status"] == "error"
    assert final["error_category"] == "RuntimeError"
    assert final["first_byte_latency_ms"] is not None
    assert "private stream failure" not in json.dumps(events)


def test_websocket_records_one_correlated_request(temp_data_dir: Path):
    TelemetryAsyncClient.stream_lines = [
        'data: {"id":"ws","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}',
        'data: {"id":"ws","choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}',
        "data: [DONE]",
    ]
    app = create_app(config=build_config(temp_data_dir))
    with patch("openclaw_router.routers.httpx.AsyncClient", TelemetryAsyncClient), patch(
        "openclaw_router.server.httpx.AsyncClient", TelemetryAsyncClient
    ):
        with TestClient(app) as client:
            with client.websocket_connect("/v1/chat/ws") as websocket:
                websocket.send_json(_request("auto:once", user="ws-session", stream=True))
                while True:
                    message = websocket.receive_text()
                    if "[DONE]" in message:
                        break
            assert app.state.control_center.telemetry.flush(timeout=2.0)

    events = app.state.control_center.queries.list_events(limit=10)
    assert sum(event["event_kind"] == "request_started" for event in events) == 1
    assert sum(event["event_kind"] == "judge_completed" for event in events) == 1
    final = next(event for event in events if event["event_kind"] == "request_completed")
    assert final["transport"] == "websocket"
    assert final["final_status"] == "success"
    assert final["first_byte_latency_ms"] is not None
    assert final["total_tokens"] == 3
    assert len({event["request_id"] for event in events}) == 1


def test_external_header_cannot_forge_traffic_class(temp_data_dir: Path):
    for traffic_class in ("production", "admin_test", "deployment_smoke"):
        event = RoutingEvent.create(
            request_id=traffic_class,
            event_kind="request_started",
            traffic_class=traffic_class,
            transport="http",
            requested_model="auto",
        )
        assert event.traffic_class == traffic_class

    app = create_app(config=build_config(temp_data_dir))
    response = _run_request(
        app,
        _request("glm"),
        headers={"X-LLMRouter-Traffic-Class": "admin_test"},
    )
    assert response.status_code == 200
    events = app.state.control_center.queries.list_events(limit=10)
    assert {event["traffic_class"] for event in events} == {"production"}


def test_shutdown_is_time_bounded_when_writer_is_stuck(temp_data_dir: Path):
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))
    service = TelemetryService(
        ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir)), str(db_path)
    )
    entered = threading.Event()
    release = threading.Event()
    original_write = service._write_batch

    def blocking_write(connection, batch):
        entered.set()
        release.wait(timeout=2.0)
        return original_write(connection, batch)

    service._write_batch = blocking_write
    assert service.submit(
        RoutingEvent.create(
            request_id="shutdown",
            event_kind="request_started",
            traffic_class="production",
            transport="http",
            requested_model="auto",
        )
    )
    assert entered.wait(timeout=1.0)
    started = time.perf_counter()
    assert service.stop(timeout=0.05) is False
    assert time.perf_counter() - started < 0.25
    release.set()
    assert service.stop(timeout=2.0) is True
