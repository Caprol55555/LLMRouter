from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openclaw_router.config import (
    ControlCenterConfig,
    LLMConfig,
    OpenClawConfig,
    RouterConfig,
    SecurityConfig,
    SessionRoutingConfig,
)
from openclaw_router.control_center import configuration as configuration_module
from openclaw_router.control_center import runtime as runtime_module
from openclaw_router.control_center.configuration import (
    ConfigurationConflict,
    ConfigurationService,
    SnapshotStructureError,
)
from openclaw_router.server import create_app


ORIGIN = {"Origin": "http://localhost", "Host": "localhost"}


def build_config(data_dir: Path) -> OpenClawConfig:
    return OpenClawConfig(
        router=RouterConfig(
            strategy="llm",
            provider="nine_router",
            model="judge-model",
            base_url="http://router.internal/v1",
            default_model="glm",
        ),
        llms={
            "glm": LLMConfig(
                name="glm",
                provider="nine_router",
                model_id="glm-upstream",
                base_url="http://router.internal/v1",
                api_key="unit-test-credential",
                description="General model",
                max_tokens=4096,
                context_limit=32768,
            ),
            "qwen": LLMConfig(
                name="qwen",
                provider="nine_router",
                model_id="qwen-upstream",
                base_url="http://router.internal/v1",
                api_key_env="NINE_ROUTER_TEST_KEY",
                description="Structured output",
                max_tokens=4096,
                context_limit=32768,
            ),
        },
        session_routing=SessionRoutingConfig(enabled=True),
        security=SecurityConfig(
            forbidden_upstream_models=["blocked-model"],
            forbidden_upstream_prefixes=["blocked/"],
        ),
        control_center=ControlCenterConfig(enabled=True, data_dir=str(data_dir)),
    )


def login(client: TestClient, token: str = "admin-secret") -> str:
    response = client.post(
        "/admin/api/login", headers=ORIGIN, json={"token": token}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def write_headers(csrf: str) -> dict[str, str]:
    return {**ORIGIN, "X-CSRF-Token": csrf}


def test_yaml_baseline_is_secret_free_and_immutable(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service = app.state.control_center.configuration
    assert service is not None
    active = service.active_configuration()
    assert active["version_number"] == 1
    assert active["source"] == "yaml_baseline"
    serialized = json.dumps(active, sort_keys=True)
    assert "unit-test-credential" not in serialized
    assert "api_key" not in serialized
    assert "router.internal" not in serialized

    with closing(sqlite3.connect(config.control_center.db_path)) as connection:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE configuration_versions SET release_notes = 'changed' WHERE version_id = ?",
                (active["version_id"],),
            )
        connection.rollback()
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "DELETE FROM configuration_versions WHERE version_id = ?",
                (active["version_id"],),
            )
        connection.rollback()


def test_existing_active_baseline_is_not_reimported(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    original = build_config(temp_data_dir)
    first = create_app(config=original)
    first_snapshot = first.state.control_center.configuration.active_configuration()["snapshot"]

    changed = build_config(temp_data_dir)
    changed.llms["glm"].description = "Changed outside the database"
    second = create_app(config=changed)
    second_snapshot = second.state.control_center.configuration.active_configuration()["snapshot"]
    assert second_snapshot == first_snapshot
    assert second_snapshot["llms"]["glm"]["description"] == "General model"


def test_draft_lifecycle_creates_pending_version_without_runtime_change(
    temp_data_dir: Path, monkeypatch
):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service = app.state.control_center.configuration
    active = service.active_configuration()
    draft = service.create_draft(release_notes="Prefer the structured model")
    snapshot = draft["snapshot"]
    snapshot["router"]["default_model"] = "qwen"
    snapshot["llms"]["qwen"]["description"] = "Preferred structured output"
    updated = service.update_draft(
        draft["draft_id"],
        expected_revision=draft["revision"],
        snapshot=snapshot,
        release_notes="Prefer the structured model",
    )
    assert updated["status"] == "editing"
    validated = service.validate_draft(
        draft["draft_id"], expected_revision=updated["revision"]
    )
    assert validated["status"] == "ready"
    version = service.finalize_draft(
        draft["draft_id"], expected_revision=validated["revision"]
    )
    assert version["version_number"] == 2
    assert version["publish_state"] == "pending"
    assert service.active_configuration()["version_id"] == active["version_id"]
    assert app.state.router.config.router.default_model == "glm"


def test_schema_secret_and_environment_reference_rejections(temp_data_dir: Path):
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service = app.state.control_center.configuration
    draft = service.create_draft()

    unknown = draft["snapshot"]
    unknown["api_key"] = "not-allowed"
    with pytest.raises(SnapshotStructureError):
        service.update_draft(
            draft["draft_id"],
            expected_revision=draft["revision"],
            snapshot=unknown,
            release_notes="",
        )

    secret = service.get_draft(draft["draft_id"])["snapshot"]
    secret["llms"]["glm"]["description"] = "sk-abcdefghijklmnop1234"
    with pytest.raises(SnapshotStructureError):
        service.update_draft(
            draft["draft_id"],
            expected_revision=draft["revision"],
            snapshot=secret,
            release_notes="",
        )

    env_reference = service.get_draft(draft["draft_id"])["snapshot"]
    env_reference["router"]["judge_system_prompt"] = "Use ${ADMIN_SECRET}"
    with pytest.raises(SnapshotStructureError):
        service.update_draft(
            draft["draft_id"],
            expected_revision=draft["revision"],
            snapshot=env_reference,
            release_notes="",
        )
    assert service.get_draft(draft["draft_id"])["revision"] == 1


@pytest.mark.parametrize("model_id", ["auto", "auto:once", "lr/recursive", "blocked-model", "blocked/model"])
def test_recursive_or_forbidden_models_cannot_be_finalized(
    temp_data_dir: Path, model_id: str
):
    service = create_app(config=build_config(temp_data_dir)).state.control_center.configuration
    draft = service.create_draft(release_notes="Invalid candidate")
    snapshot = draft["snapshot"]
    snapshot["llms"]["glm"]["model"] = model_id
    updated = service.update_draft(
        draft["draft_id"],
        expected_revision=1,
        snapshot=snapshot,
        release_notes="Invalid candidate",
    )
    validated = service.validate_draft(
        draft["draft_id"], expected_revision=updated["revision"]
    )
    assert validated["status"] == "editing"
    assert validated["validation_issues"]
    with pytest.raises(ConfigurationConflict):
        service.finalize_draft(
            draft["draft_id"], expected_revision=validated["revision"]
        )


def test_diff_is_stable_and_ignores_mapping_and_set_order(temp_data_dir: Path):
    service = create_app(config=build_config(temp_data_dir)).state.control_center.configuration
    draft = service.create_draft()
    original = draft["snapshot"]
    reordered = {
        "llms": {key: original["llms"][key] for key in reversed(list(original["llms"]))},
        "session_routing": dict(reversed(list(original["session_routing"].items()))),
        "router": dict(reversed(list(original["router"].items()))),
    }
    reordered["router"]["allowed_models"] = list(
        reversed(reordered["router"]["allowed_models"])
    )
    updated = service.update_draft(
        draft["draft_id"],
        expected_revision=1,
        snapshot=reordered,
        release_notes="",
    )
    assert service.draft_diff(draft["draft_id"])["changes"] == []

    changed = updated["snapshot"]
    changed["router"]["default_model"] = "qwen"
    changed["session_routing"]["ttl_seconds"] = 900
    service.update_draft(
        draft["draft_id"],
        expected_revision=updated["revision"],
        snapshot=changed,
        release_notes="",
    )
    paths = [item["path"] for item in service.draft_diff(draft["draft_id"])["changes"]]
    assert paths == sorted(paths)


def test_optimistic_revision_and_finalized_draft_are_immutable(temp_data_dir: Path):
    service = create_app(config=build_config(temp_data_dir)).state.control_center.configuration
    draft = service.create_draft(release_notes="Ready")
    with pytest.raises(ConfigurationConflict):
        service.update_draft(
            draft["draft_id"],
            expected_revision=99,
            snapshot=draft["snapshot"],
            release_notes="Ready",
        )
    ready = service.validate_draft(draft["draft_id"], expected_revision=1)
    service.finalize_draft(draft["draft_id"], expected_revision=ready["revision"])
    with pytest.raises(ConfigurationConflict):
        service.delete_draft(draft["draft_id"], expected_revision=ready["revision"])


def test_activation_switches_runtime_and_rollback_creates_new_version(
    temp_data_dir: Path, monkeypatch
):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service = app.state.control_center.configuration
    active = service.active_configuration()
    draft = service.create_draft(release_notes="Use qwen by default")
    snapshot = draft["snapshot"]
    snapshot["router"]["default_model"] = "qwen"
    updated = service.update_draft(
        draft["draft_id"],
        expected_revision=draft["revision"],
        snapshot=snapshot,
        release_notes="Use qwen by default",
    )
    ready = service.validate_draft(
        draft["draft_id"], expected_revision=updated["revision"]
    )
    pending = service.finalize_draft(
        draft["draft_id"], expected_revision=ready["revision"]
    )

    client = TestClient(app)
    csrf = login(client)
    response = client.post(
        f"/admin/api/configuration/versions/{pending['version_id']}/activate",
        headers=write_headers(csrf),
        json={"expected_active_version_id": active["version_id"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["cache_clear_reason"] == "routing_semantics_changed"
    assert app.state.runtime_bundle.version_id == pending["version_id"]
    assert app.state.router.config.router.default_model == "qwen"
    assert service.active_configuration()["version_id"] == pending["version_id"]

    rolled_back = client.post(
        f"/admin/api/configuration/versions/{active['version_id']}/rollback",
        headers=write_headers(csrf),
        json={"expected_active_version_id": pending["version_id"]},
    )
    assert rolled_back.status_code == 201, rolled_back.text
    rollback_version = rolled_back.json()
    assert rollback_version["version_number"] == 3
    assert rollback_version["parent_version_id"] == pending["version_id"]
    assert app.state.router.config.router.default_model == "glm"
    assert service.active_configuration()["version_id"] == rollback_version["version_id"]


def test_activation_rejects_stale_expected_version(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service = app.state.control_center.configuration
    draft = service.create_draft(release_notes="Pending")
    ready = service.validate_draft(draft["draft_id"], expected_revision=draft["revision"])
    pending = service.finalize_draft(draft["draft_id"], expected_revision=ready["revision"])
    client = TestClient(app)
    csrf = login(client)
    response = client.post(
        f"/admin/api/configuration/versions/{pending['version_id']}/activate",
        headers=write_headers(csrf),
        json={"expected_active_version_id": 999},
    )
    assert response.status_code == 409
    assert service.active_configuration()["version_id"] == 1


def test_route_lab_uses_ephemeral_admin_test_and_does_not_persist_text(
    temp_data_dir: Path, monkeypatch
):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    config.router.strategy = "random"
    app = create_app(config=config)
    with TestClient(app) as client:
        csrf = login(client)
        response = client.post(
            "/admin/api/route-lab/evaluate",
            headers=write_headers(csrf),
            json={"text": "temporary route lab input"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()["result"]
        assert payload["traffic_class"] == "admin_test"
        assert payload["persisted"] is False
        assert app.state.control_center.telemetry.flush(timeout=2.0)
        admin_events = app.state.control_center.queries.list_events(traffic_class="admin_test")
        assert admin_events
        assert all(item["traffic_class"] == "admin_test" for item in admin_events)
        audit = client.get("/admin/api/audit", headers=ORIGIN)
        assert "temporary route lab input" not in audit.text


def test_maintenance_integrity_report_is_read_only_and_healthy(
    temp_data_dir: Path, monkeypatch
):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    client = TestClient(create_app(config=build_config(temp_data_dir)))
    login(client)
    report = client.get("/admin/api/maintenance/integrity", headers=ORIGIN)
    assert report.status_code == 200, report.text
    assert report.json()["status"] == "ok"
    assert report.json()["foreign_key_violations"] == []


def test_configuration_api_requires_session_origin_and_csrf(temp_data_dir: Path, monkeypatch):
    monkeypatch.setenv("LLMROUTER_ADMIN_TOKEN", "admin-secret")
    config = build_config(temp_data_dir)
    client = TestClient(create_app(config=config))
    assert client.get("/admin/api/configuration").status_code == 401
    csrf = login(client)
    summary = client.get("/admin/api/configuration")
    assert summary.status_code == 200
    assert summary.json()["active"]["version_number"] == 1
    assert "unit-test-credential" not in summary.text

    anonymous = TestClient(create_app(config=build_config(temp_data_dir)))
    assert anonymous.post(
        "/admin/api/configuration/routes",
        headers=ORIGIN,
        json={},
    ).status_code == 401
    assert client.post(
        "/admin/api/configuration/routes",
        headers={"Origin": "http://localhost:9999", "Host": "localhost"},
        json={},
    ).status_code == 403
    created = client.post(
        "/admin/api/configuration/routes",
        headers=write_headers(csrf),
        json={"release_notes": "API draft"},
    )
    assert created.status_code == 201
    draft = created.json()
    assert client.put(
        f"/admin/api/configuration/routes/{draft['draft_id']}",
        headers=write_headers(csrf),
        json={
            "revision": draft["revision"],
            "snapshot": draft["snapshot"],
            "release_notes": "API draft",
            "unknown": True,
        },
    ).status_code == 422
    assert config.router.default_model == "glm"


def test_audit_is_sanitized_and_capacity_bounded(temp_data_dir: Path, monkeypatch):
    config = build_config(temp_data_dir)
    app = create_app(config=config)
    service: ConfigurationService = app.state.control_center.configuration
    monkeypatch.setattr(configuration_module, "AUDIT_RETENTION_ROWS", 3)
    for index in range(5):
        service.record_audit(
            action="test_action",
            outcome="success",
            subject_type="test",
            subject_id=str(index),
            summary={"sequence": index},
        )
    audit = service.list_audit(page_size=100)
    assert audit["total"] == 3
    assert [item["summary"]["sequence"] for item in reversed(audit["items"])] == [2, 3, 4]
    serialized = json.dumps(audit)
    assert "unit-test-credential" not in serialized


def test_read_only_environment_reference_metadata_never_exposes_values(
    temp_data_dir: Path, monkeypatch
):
    monkeypatch.setenv("NINE_ROUTER_TEST_KEY", "environment-credential-value")
    config = build_config(temp_data_dir)
    service = create_app(config=config).state.control_center.configuration
    metadata = service.read_only_metadata()
    credential = metadata["models"]["qwen"]["credential"]
    assert credential == {
        "source": "model_env",
        "name": "NINE_ROUTER_TEST_KEY",
        "configured": True,
        "valid_name": True,
    }
    assert "environment-credential-value" not in json.dumps(metadata)


def test_configuration_initialization_failure_stops_telemetry(
    temp_data_dir: Path, monkeypatch
):
    stop_calls: list[float] = []
    original_stop = runtime_module.TelemetryService.stop

    def fail_baseline(_service):
        raise sqlite3.OperationalError("injected configuration failure")

    def recording_stop(service, timeout: float = 2.0):
        stop_calls.append(timeout)
        return original_stop(service, timeout=timeout)

    monkeypatch.setattr(
        runtime_module.ConfigurationService,
        "initialize_baseline",
        fail_baseline,
    )
    monkeypatch.setattr(runtime_module.TelemetryService, "stop", recording_stop)
    app = create_app(config=build_config(temp_data_dir))
    runtime = app.state.control_center
    assert runtime.state == runtime_module.ControlCenterState.DEGRADED
    assert runtime.telemetry is None
    assert runtime.configuration is None
    assert stop_calls == [0.5]
