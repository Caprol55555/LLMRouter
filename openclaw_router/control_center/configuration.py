"""Immutable configuration versions and editable Control Center drafts.

Only the explicitly managed routing subset is persisted. Secrets, credential
values, filesystem paths, listener settings, and outbound URLs remain outside
the editable snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import LLMConfig, OpenClawConfig


SNAPSHOT_KEYS = {"router", "session_routing", "llms"}
ROUTER_KEYS = {
    "judge_model",
    "default_model",
    "allowed_models",
    "judge_timeout_seconds",
    "judge_max_tokens",
    "routing_context_chars",
    "judge_system_prompt",
}
SESSION_KEYS = {
    "enabled",
    "ttl_seconds",
    "rejudge_every_user_turns",
    "allowed_rejudge_intervals",
    "max_entries",
    "rejudge_on_modality_change",
    "rejudge_on_backend_error",
}
LLM_KEYS = {"model", "description", "max_tokens", "context_limit"}
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    re.compile(r"\bAuthorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
)
AUDIT_RETENTION_ROWS = 10_000


class ConfigurationError(Exception):
    """Base class for sanitized configuration service failures."""


class ConfigurationNotFound(ConfigurationError):
    pass


class ConfigurationConflict(ConfigurationError):
    pass


class SnapshotStructureError(ConfigurationError):
    pass


class DraftValidationError(ConfigurationError):
    def __init__(self, issues: Sequence["ValidationIssue"]):
        super().__init__("Draft validation failed")
        self.issues = list(issues)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(snapshot_json: str) -> str:
    return hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()


def _expect_dict(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotStructureError(f"{path} must be an object")
    return value


def _expect_exact_keys(value: Dict[str, Any], expected: set[str], path: str) -> None:
    unknown = sorted(set(value) - expected)
    missing = sorted(expected - set(value))
    if unknown:
        raise SnapshotStructureError(f"{path} contains unknown fields: {', '.join(unknown)}")
    if missing:
        raise SnapshotStructureError(f"{path} is missing fields: {', '.join(missing)}")


def _expect_string(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int,
    preserve_whitespace: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SnapshotStructureError(f"{path} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not preserve_whitespace:
        normalized = normalized.strip()
    if not minimum <= len(normalized) <= maximum:
        raise SnapshotStructureError(
            f"{path} length must be between {minimum} and {maximum} characters"
        )
    _reject_sensitive_text(normalized, path)
    return normalized


def _expect_optional_string(value: Any, path: str, *, maximum: int) -> Optional[str]:
    if value is None:
        return None
    normalized = _expect_string(value, path, maximum=maximum)
    return normalized or None


def _expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotStructureError(f"{path} must be a boolean")
    return value


def _expect_int(value: Any, path: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotStructureError(f"{path} must be an integer")
    if not minimum <= value <= maximum:
        raise SnapshotStructureError(f"{path} must be between {minimum} and {maximum}")
    return value


def _expect_number(value: Any, path: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotStructureError(f"{path} must be a number")
    normalized = float(value)
    if not minimum <= normalized <= maximum:
        raise SnapshotStructureError(f"{path} must be between {minimum} and {maximum}")
    return normalized


def _reject_sensitive_text(value: str, path: str) -> None:
    if "${" in value:
        raise SnapshotStructureError(
            f"{path} cannot contain environment references; credential references are read-only"
        )
    if any(pattern.search(value) for pattern in SENSITIVE_VALUE_PATTERNS):
        raise SnapshotStructureError(f"{path} appears to contain a secret")


def _expect_unique_strings(value: Any, path: str, *, maximum_items: int) -> List[str]:
    if not isinstance(value, list):
        raise SnapshotStructureError(f"{path} must be an array")
    if len(value) > maximum_items:
        raise SnapshotStructureError(f"{path} contains too many entries")
    normalized = [
        _expect_string(item, f"{path}[{index}]", minimum=1, maximum=128)
        for index, item in enumerate(value)
    ]
    if len(set(normalized)) != len(normalized):
        raise SnapshotStructureError(f"{path} must not contain duplicate entries")
    return sorted(normalized)


def _expect_unique_ints(value: Any, path: str, *, maximum_items: int) -> List[int]:
    if not isinstance(value, list):
        raise SnapshotStructureError(f"{path} must be an array")
    if len(value) > maximum_items:
        raise SnapshotStructureError(f"{path} contains too many entries")
    normalized = [
        _expect_int(item, f"{path}[{index}]", minimum=1, maximum=1000)
        for index, item in enumerate(value)
    ]
    if len(set(normalized)) != len(normalized):
        raise SnapshotStructureError(f"{path} must not contain duplicate entries")
    return sorted(normalized)


def build_managed_snapshot(config: OpenClawConfig) -> Dict[str, Any]:
    """Project a runtime config into the editable, secret-free subset."""
    return {
        "router": {
            "judge_model": config.router.model,
            "default_model": config.router.default_model,
            "allowed_models": sorted(config.llms),
            "judge_timeout_seconds": float(config.router.judge_timeout_seconds),
            "judge_max_tokens": int(config.router.judge_max_tokens),
            "routing_context_chars": int(config.router.routing_context_chars),
            "judge_system_prompt": config.router.judge_system_prompt,
        },
        "session_routing": {
            "enabled": bool(config.session_routing.enabled),
            "ttl_seconds": int(config.session_routing.ttl_seconds),
            "rejudge_every_user_turns": int(
                config.session_routing.rejudge_every_user_turns
            ),
            "allowed_rejudge_intervals": list(
                config.session_routing.allowed_rejudge_intervals
            ),
            "max_entries": int(config.session_routing.max_entries),
            "rejudge_on_modality_change": bool(
                config.session_routing.rejudge_on_modality_change
            ),
            "rejudge_on_backend_error": bool(
                config.session_routing.rejudge_on_backend_error
            ),
        },
        "llms": {
            name: {
                "model": llm.model_id,
                "description": llm.description,
                "max_tokens": int(llm.max_tokens),
                "context_limit": int(llm.context_limit),
            }
            for name, llm in sorted(config.llms.items())
        },
    }


def apply_managed_snapshot(config: OpenClawConfig, snapshot: Any) -> OpenClawConfig:
    """Apply the editable snapshot to a cloned application configuration.

    Read-only provider, credential, listener, and filesystem settings remain
    owned by the original configuration object. The caller is responsible for
    cloning it before invoking this helper.
    """
    normalized = normalize_snapshot(snapshot, config.llms)
    router = normalized["router"]
    config.router.model = router["judge_model"]
    config.router.default_model = router["default_model"]
    config.router.judge_timeout_seconds = router["judge_timeout_seconds"]
    config.router.judge_max_tokens = router["judge_max_tokens"]
    config.router.routing_context_chars = router["routing_context_chars"]
    config.router.judge_system_prompt = router["judge_system_prompt"]

    session = normalized["session_routing"]
    for key, value in session.items():
        setattr(config.session_routing, key, value)

    existing = next(iter(config.llms.values()), None)
    for alias in list(config.llms):
        if alias not in normalized["llms"]:
            del config.llms[alias]
    for alias, values in normalized["llms"].items():
        llm = config.llms.get(alias)
        if llm is None:
            if existing is None:
                raise SnapshotStructureError("At least one configured backend is required")
            llm = LLMConfig(
                name=alias,
                provider=existing.provider,
                model_id=values["model"],
                base_url=existing.base_url,
                provider_type=existing.provider_type,
                auth_mode=existing.auth_mode,
                chat_path=existing.chat_path,
                local=existing.local,
                api_key=existing.api_key,
                api_key_env=existing.api_key_env,
                description=values["description"],
                max_tokens=values["max_tokens"],
                context_limit=values["context_limit"],
            )
            config.llms[alias] = llm
        llm.model_id = values["model"]
        llm.description = values["description"]
        llm.max_tokens = values["max_tokens"]
        llm.context_limit = values["context_limit"]
    return config


def normalize_snapshot(snapshot: Any, configured_aliases: Iterable[str]) -> Dict[str, Any]:
    """Enforce the closed schema and return a deterministic representation."""
    aliases = sorted(set(configured_aliases))
    root = _expect_dict(snapshot, "snapshot")
    _expect_exact_keys(root, SNAPSHOT_KEYS, "snapshot")

    router = _expect_dict(root["router"], "snapshot.router")
    _expect_exact_keys(router, ROUTER_KEYS, "snapshot.router")
    session = _expect_dict(root["session_routing"], "snapshot.session_routing")
    _expect_exact_keys(session, SESSION_KEYS, "snapshot.session_routing")
    llms = _expect_dict(root["llms"], "snapshot.llms")
    if not llms and aliases:
        raise SnapshotStructureError("snapshot.llms must contain at least one model")

    normalized_llms: Dict[str, Dict[str, Any]] = {}
    for alias in sorted(llms):
        item = _expect_dict(llms[alias], f"snapshot.llms.{alias}")
        _expect_exact_keys(item, LLM_KEYS, f"snapshot.llms.{alias}")
        normalized_llms[alias] = {
            "model": _expect_string(
                item["model"], f"snapshot.llms.{alias}.model", minimum=1, maximum=256
            ),
            "description": _expect_string(
                item["description"],
                f"snapshot.llms.{alias}.description",
                maximum=2000,
                preserve_whitespace=True,
            ),
            "max_tokens": _expect_int(
                item["max_tokens"],
                f"snapshot.llms.{alias}.max_tokens",
                minimum=1,
                maximum=1_000_000,
            ),
            "context_limit": _expect_int(
                item["context_limit"],
                f"snapshot.llms.{alias}.context_limit",
                minimum=1,
                maximum=10_000_000,
            ),
        }

    return {
        "router": {
            "judge_model": _expect_optional_string(
                router["judge_model"], "snapshot.router.judge_model", maximum=256
            ),
            "default_model": _expect_optional_string(
                router["default_model"], "snapshot.router.default_model", maximum=128
            ),
            "allowed_models": _expect_unique_strings(
                router["allowed_models"],
                "snapshot.router.allowed_models",
                maximum_items=max(1, len(llms), len(aliases)),
            ),
            "judge_timeout_seconds": _expect_number(
                router["judge_timeout_seconds"],
                "snapshot.router.judge_timeout_seconds",
                minimum=0.1,
                maximum=120.0,
            ),
            "judge_max_tokens": _expect_int(
                router["judge_max_tokens"],
                "snapshot.router.judge_max_tokens",
                minimum=1,
                maximum=4096,
            ),
            "routing_context_chars": _expect_int(
                router["routing_context_chars"],
                "snapshot.router.routing_context_chars",
                minimum=100,
                maximum=100_000,
            ),
            "judge_system_prompt": _expect_string(
                router["judge_system_prompt"],
                "snapshot.router.judge_system_prompt",
                minimum=1,
                maximum=20_000,
                preserve_whitespace=True,
            ),
        },
        "session_routing": {
            "enabled": _expect_bool(
                session["enabled"], "snapshot.session_routing.enabled"
            ),
            "ttl_seconds": _expect_int(
                session["ttl_seconds"],
                "snapshot.session_routing.ttl_seconds",
                minimum=30,
                maximum=86_400,
            ),
            "rejudge_every_user_turns": _expect_int(
                session["rejudge_every_user_turns"],
                "snapshot.session_routing.rejudge_every_user_turns",
                minimum=0,
                maximum=1000,
            ),
            "allowed_rejudge_intervals": _expect_unique_ints(
                session["allowed_rejudge_intervals"],
                "snapshot.session_routing.allowed_rejudge_intervals",
                maximum_items=32,
            ),
            "max_entries": _expect_int(
                session["max_entries"],
                "snapshot.session_routing.max_entries",
                minimum=1,
                maximum=1_000_000,
            ),
            "rejudge_on_modality_change": _expect_bool(
                session["rejudge_on_modality_change"],
                "snapshot.session_routing.rejudge_on_modality_change",
            ),
            "rejudge_on_backend_error": _expect_bool(
                session["rejudge_on_backend_error"],
                "snapshot.session_routing.rejudge_on_backend_error",
            ),
        },
        "llms": normalized_llms,
    }


def _is_reserved_upstream(model_id: str) -> bool:
    normalized = model_id.strip().lower()
    return normalized == "auto" or normalized.startswith("auto:") or normalized.startswith("lr/")


def validate_snapshot(
    snapshot: Dict[str, Any],
    *,
    configured_aliases: Iterable[str],
    forbidden_models: Iterable[str],
    forbidden_prefixes: Iterable[str],
) -> List[ValidationIssue]:
    aliases = set(snapshot.get("llms", {}))
    issues: List[ValidationIssue] = []
    router = snapshot["router"]
    allowed = set(router["allowed_models"])

    if aliases and not allowed:
        issues.append(
            ValidationIssue(
                "allowed_models_empty",
                "/router/allowed_models",
                "At least one configured backend must be allowed",
            )
        )
    for alias in sorted(allowed - aliases):
        issues.append(
            ValidationIssue(
                "unknown_model_alias",
                "/router/allowed_models",
                f"Model alias '{alias}' is not configured by the server",
            )
        )
    for alias in sorted(allowed):
        if _is_reserved_upstream(alias):
            issues.append(
                ValidationIssue(
                    "reserved_model_alias",
                    "/router/allowed_models",
                    f"Model alias '{alias}' is reserved for router-internal use",
                )
            )

    default_model = router["default_model"]
    if default_model and default_model not in allowed:
        issues.append(
            ValidationIssue(
                "default_model_not_allowed",
                "/router/default_model",
                "The default model must be in allowed_models",
            )
        )

    forbidden_model_set = {str(value).strip() for value in forbidden_models if str(value).strip()}
    forbidden_prefix_tuple = tuple(
        str(value).strip() for value in forbidden_prefixes if str(value).strip()
    )

    def validate_upstream(model_id: Optional[str], path: str) -> None:
        if not model_id:
            return
        if _is_reserved_upstream(model_id):
            issues.append(
                ValidationIssue(
                    "recursive_upstream_model",
                    path,
                    "Router-internal model IDs cannot be used as upstream candidates",
                )
            )
        elif model_id in forbidden_model_set or (
            forbidden_prefix_tuple and model_id.startswith(forbidden_prefix_tuple)
        ):
            issues.append(
                ValidationIssue(
                    "forbidden_upstream_model",
                    path,
                    "The upstream model is blocked by the server recursion guard",
                )
            )

    validate_upstream(router["judge_model"], "/router/judge_model")
    for alias, llm in snapshot["llms"].items():
        validate_upstream(llm["model"], f"/llms/{alias}/model")
        if llm["max_tokens"] > llm["context_limit"]:
            issues.append(
                ValidationIssue(
                    "token_budget_exceeds_context",
                    f"/llms/{alias}/max_tokens",
                    "max_tokens cannot exceed context_limit",
                )
            )

    session = snapshot["session_routing"]
    rejudge = session["rejudge_every_user_turns"]
    if rejudge and rejudge not in session["allowed_rejudge_intervals"]:
        issues.append(
            ValidationIssue(
                "rejudge_interval_not_allowed",
                "/session_routing/rejudge_every_user_turns",
                "The rejudge interval must be zero or appear in allowed_rejudge_intervals",
            )
        )
    return sorted(issues, key=lambda item: (item.path, item.code))


def _stable_diff(before: Any, after: Any, path: str = "") -> List[Dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: List[Dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{key}"
            if key not in before:
                result.append({"path": child, "before": None, "after": after[key]})
            elif key not in after:
                result.append({"path": child, "before": before[key], "after": None})
            else:
                result.extend(_stable_diff(before[key], after[key], child))
        return result
    if before != after:
        return [{"path": path or "/", "before": before, "after": after}]
    return []


class ConfigurationService:
    """SQLite-backed immutable version and draft repository."""

    def __init__(self, config: OpenClawConfig, db_path: str):
        self._config = config
        self.db_path = db_path
        self._aliases = tuple(sorted(config.llms))
        self._forbidden_models = tuple(config.security.forbidden_upstream_models)
        self._forbidden_prefixes = tuple(config.security.forbidden_upstream_prefixes)
        self._baseline = normalize_snapshot(build_managed_snapshot(config), self._aliases)

    def initialize_baseline(self) -> int:
        now = _utc_now()
        snapshot_json = _canonical_json(self._baseline)
        checksum = _checksum(snapshot_json)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                state = connection.execute(
                    "SELECT active_version_id FROM configuration_state WHERE singleton_id = 1"
                ).fetchone()
                if state is not None:
                    active = connection.execute(
                        "SELECT version_id FROM configuration_versions WHERE version_id = ?",
                        (state["active_version_id"],),
                    ).fetchone()
                    if active is None:
                        raise ConfigurationError("Active configuration version is missing")
                    connection.execute("COMMIT")
                    return int(active["version_id"])

                count = int(
                    connection.execute("SELECT COUNT(*) FROM configuration_versions").fetchone()[0]
                )
                if count:
                    raise ConfigurationError("Configuration state is incomplete")
                cursor = connection.execute(
                    "INSERT INTO configuration_versions "
                    "(version_number, parent_version_id, source, snapshot_json, checksum, release_notes, created_at) "
                    "VALUES (1, NULL, 'yaml_baseline', ?, ?, ?, ?)",
                    (
                        snapshot_json,
                        checksum,
                        "Sanitized YAML baseline import",
                        now,
                    ),
                )
                version_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO configuration_state "
                    "(singleton_id, active_version_id, initialized_at, updated_at) VALUES (1, ?, ?, ?)",
                    (version_id, now, now),
                )
                self._insert_audit(
                    connection,
                    action="baseline_imported",
                    outcome="success",
                    subject_type="configuration_version",
                    subject_id=str(version_id),
                    summary={"version_number": 1},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
                return version_id
            except Exception:
                self._rollback(connection)
                raise

    def active_configuration(self) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT v.* FROM configuration_state s "
                "JOIN configuration_versions v ON v.version_id = s.active_version_id "
                "WHERE s.singleton_id = 1"
            ).fetchone()
        if row is None:
            raise ConfigurationError("Active configuration is unavailable")
        return self._version_row(row, include_snapshot=True, active_version_id=int(row["version_id"]))

    def read_only_metadata(self) -> Dict[str, Any]:
        models: Dict[str, Any] = {}
        for alias, llm in sorted(self._config.llms.items()):
            env_name = llm.api_key_env if llm.api_key_env and ENV_NAME.fullmatch(llm.api_key_env) else None
            provider_value = self._config.api_keys.get(llm.provider)
            if llm.api_key:
                credential = {"source": "inline_legacy", "name": None, "configured": True}
            elif llm.api_key_env:
                credential = {
                    "source": "model_env",
                    "name": env_name,
                    "configured": bool(env_name and os.getenv(env_name)),
                    "valid_name": env_name is not None,
                }
            elif provider_value:
                credential = {"source": "provider_config", "name": None, "configured": True}
            else:
                fallback = f"{llm.provider.upper()}_API_KEY"
                credential = {
                    "source": "provider_env",
                    "name": fallback,
                    "configured": bool(os.getenv(fallback)),
                    "valid_name": bool(ENV_NAME.fullmatch(fallback)),
                }
            models[alias] = {
                "provider": llm.provider,
                "provider_type": llm.provider_type,
                "base_url": llm.base_url,
                "auth_mode": llm.auth_mode,
                "chat_path": llm.chat_path,
                "credential": credential,
            }
        return {
            "serve": {"host": self._config.host, "port": self._config.port},
            "router": {
                "strategy": self._config.router.strategy,
                "provider": self._config.router.provider,
                "base_url": self._config.router.base_url,
                "auth_mode": self._config.router.auth_mode,
                "chat_path": self._config.router.chat_path,
            },
            "security": {
                "require_inbound_auth": self._config.security.require_inbound_auth,
                "forbidden_upstream_models": list(self._forbidden_models),
                "forbidden_upstream_prefixes": list(self._forbidden_prefixes),
            },
            "models": models,
        }

    def list_versions(self, *, page: int = 1, page_size: int = 25) -> Dict[str, Any]:
        page = max(1, min(int(page), 100_000))
        page_size = max(1, min(int(page_size), 100))
        with closing(self._connect()) as connection:
            active_id = self._active_version_id(connection)
            total = int(connection.execute("SELECT COUNT(*) FROM configuration_versions").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM configuration_versions ORDER BY version_number DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [self._version_row(row, False, active_id) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def get_version(self, version_id: int) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            active_id = self._active_version_id(connection)
            row = connection.execute(
                "SELECT * FROM configuration_versions WHERE version_id = ?",
                (int(version_id),),
            ).fetchone()
        if row is None:
            raise ConfigurationNotFound("Configuration version was not found")
        return self._version_row(row, True, active_id)

    def activate_version(
        self,
        version_id: int,
        *,
        expected_active_version_id: int,
    ) -> Dict[str, Any]:
        """Move the active pointer with an optimistic version check."""
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                active_id = self._active_version_id(connection)
                if active_id != int(expected_active_version_id):
                    raise ConfigurationConflict("Active configuration version changed")
                row = connection.execute(
                    "SELECT * FROM configuration_versions WHERE version_id = ?",
                    (int(version_id),),
                ).fetchone()
                if row is None:
                    raise ConfigurationNotFound("Configuration version was not found")
                if int(row["version_id"]) == active_id:
                    raise ConfigurationConflict("Configuration version is already active")
                connection.execute(
                    "UPDATE configuration_state SET active_version_id = ?, updated_at = ? "
                    "WHERE singleton_id = 1",
                    (int(version_id), now),
                )
                self._insert_audit(
                    connection,
                    action="version_activated",
                    outcome="success",
                    subject_type="configuration_version",
                    subject_id=str(version_id),
                    summary={
                        "from_version_id": active_id,
                        "to_version_id": int(version_id),
                    },
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_version(int(version_id))

    def rollback_version(
        self,
        target_version_id: int,
        *,
        expected_active_version_id: int,
        release_notes: str = "",
    ) -> Dict[str, Any]:
        """Create a new immutable snapshot from history and activate it."""
        notes = self._normalize_release_notes(release_notes)
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                active_id = self._active_version_id(connection)
                if active_id != int(expected_active_version_id):
                    raise ConfigurationConflict("Active configuration version changed")
                target = connection.execute(
                    "SELECT * FROM configuration_versions WHERE version_id = ?",
                    (int(target_version_id),),
                ).fetchone()
                if target is None:
                    raise ConfigurationNotFound("Rollback target version was not found")
                if int(target["version_id"]) == active_id:
                    raise ConfigurationConflict("Rollback target is already active")
                next_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version_number), 0) + 1 FROM configuration_versions"
                    ).fetchone()[0]
                )
                if not notes:
                    notes = f"Rollback to version {int(target['version_number'])}"
                cursor = connection.execute(
                    "INSERT INTO configuration_versions "
                    "(version_number, parent_version_id, source, snapshot_json, checksum, release_notes, created_at) "
                    "VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                    (
                        next_number,
                        active_id,
                        target["snapshot_json"],
                        target["checksum"],
                        notes,
                        now,
                    ),
                )
                new_version_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE configuration_state SET active_version_id = ?, updated_at = ? "
                    "WHERE singleton_id = 1",
                    (new_version_id, now),
                )
                self._insert_audit(
                    connection,
                    action="version_rolled_back",
                    outcome="success",
                    subject_type="configuration_version",
                    subject_id=str(new_version_id),
                    summary={
                        "from_version_id": active_id,
                        "target_version_id": int(target_version_id),
                        "new_version_id": new_version_id,
                    },
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_version(new_version_id)

    def list_drafts(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM configuration_drafts ORDER BY updated_at DESC, draft_id ASC LIMIT 100"
            ).fetchall()
        return [self._draft_row(row, include_snapshot=False) for row in rows]

    def list_active_drafts(self) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM configuration_drafts WHERE is_active = 1 ORDER BY updated_at DESC, draft_id ASC"
            ).fetchall()
        return [self._draft_row(row, include_snapshot=False) for row in rows]

    def get_draft(self, draft_id: str) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            row = self._draft(connection, draft_id)
        return self._draft_row(row, include_snapshot=True)

    def create_draft(
        self,
        *,
        base_version_id: Optional[int] = None,
        release_notes: str = "",
        name: str = "",
    ) -> Dict[str, Any]:
        notes = self._normalize_release_notes(release_notes)
        name = self._normalize_draft_name(name)
        now = _utc_now()
        draft_id = secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                base_id = int(base_version_id) if base_version_id is not None else self._active_version_id(connection)
                base = connection.execute(
                    "SELECT snapshot_json, checksum FROM configuration_versions WHERE version_id = ?",
                    (base_id,),
                ).fetchone()
                if base is None:
                    raise ConfigurationNotFound("Base configuration version was not found")
                connection.execute(
                    "INSERT INTO configuration_drafts "
                    "(draft_id, base_version_id, finalized_version_id, status, revision, snapshot_json, checksum, "
                    "validation_json, release_notes, created_at, updated_at, name, is_active) "
                    "VALUES (?, ?, NULL, 'editing', 1, ?, ?, '[]', ?, ?, ?, ?, 0)",
                    (draft_id, base_id, base["snapshot_json"], base["checksum"], notes, now, now, name),
                )
                self._insert_audit(
                    connection,
                    action="draft_created",
                    outcome="success",
                    subject_type="configuration_draft",
                    subject_id=draft_id,
                    summary={"base_version_id": base_id},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_draft(draft_id)

    def update_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        snapshot: Any,
        release_notes: str,
        name: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized = normalize_snapshot(snapshot, self._aliases)
        notes = self._normalize_release_notes(release_notes)
        issues = self._validate(normalized)
        snapshot_json = _canonical_json(normalized)
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._draft(connection, draft_id)
                self._require_editable_revision(row, expected_revision)
                next_revision = int(row["revision"]) + 1
                next_name = row["name"] if name is None else self._normalize_draft_name(name)
                connection.execute(
                    "UPDATE configuration_drafts SET status = 'editing', revision = ?, snapshot_json = ?, "
                    "checksum = ?, validation_json = ?, release_notes = ?, name = ?, updated_at = ? WHERE draft_id = ?",
                    (
                        next_revision,
                        snapshot_json,
                        _checksum(snapshot_json),
                        _canonical_json([issue.as_dict() for issue in issues]),
                        notes,
                        next_name,
                        now,
                        draft_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    action="draft_updated",
                    outcome="success",
                    subject_type="configuration_draft",
                    subject_id=draft_id,
                    summary={"revision": next_revision, "issue_count": len(issues)},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_draft(draft_id)

    def set_draft_active(self, draft_id: str, *, active: bool) -> Dict[str, Any]:
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._draft(connection, draft_id)
                connection.execute(
                    "UPDATE configuration_drafts SET is_active = ?, updated_at = ? WHERE draft_id = ?",
                    (1 if active else 0, now, draft_id),
                )
                self._insert_audit(
                    connection,
                    action="draft_activation_changed",
                    outcome="success",
                    subject_type="configuration_draft",
                    subject_id=draft_id,
                    summary={"active": bool(active)},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_draft(draft_id)

    def replace_model_catalog(self, models: Sequence[str]) -> List[str]:
        normalized = sorted({str(model).strip() for model in models if str(model).strip()})
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM configuration_model_catalog")
            connection.executemany(
                "INSERT OR IGNORE INTO configuration_model_catalog (model_id, created_at) VALUES (?, ?)",
                [(model, now) for model in normalized],
            )
            rows = connection.execute("SELECT model_id FROM configuration_model_catalog ORDER BY model_id").fetchall()
        return [str(row["model_id"]) for row in rows]

    def model_catalog(self) -> List[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT model_id FROM configuration_model_catalog ORDER BY model_id").fetchall()
        return [str(row["model_id"]) for row in rows]

    def validate_draft(self, draft_id: str, *, expected_revision: int) -> Dict[str, Any]:
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._draft(connection, draft_id)
                self._require_editable_revision(row, expected_revision)
                snapshot = json.loads(row["snapshot_json"])
                issues = self._validate(snapshot)
                status = "ready" if not issues else "editing"
                connection.execute(
                    "UPDATE configuration_drafts SET status = ?, validation_json = ?, updated_at = ? "
                    "WHERE draft_id = ?",
                    (
                        status,
                        _canonical_json([issue.as_dict() for issue in issues]),
                        now,
                        draft_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    action="draft_validated",
                    outcome="success" if not issues else "failure",
                    subject_type="configuration_draft",
                    subject_id=draft_id,
                    summary={"revision": int(row["revision"]), "issue_count": len(issues)},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_draft(draft_id)

    def finalize_draft(self, draft_id: str, *, expected_revision: int) -> Dict[str, Any]:
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._draft(connection, draft_id)
                self._require_editable_revision(row, expected_revision)
                if row["status"] != "ready":
                    raise ConfigurationConflict("Draft must be validated before finalization")
                if not str(row["release_notes"] or "").strip():
                    raise ConfigurationConflict("Release notes are required before finalization")
                snapshot = json.loads(row["snapshot_json"])
                issues = self._validate(snapshot)
                if issues:
                    raise DraftValidationError(issues)
                next_number = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version_number), 0) + 1 FROM configuration_versions"
                    ).fetchone()[0]
                )
                cursor = connection.execute(
                    "INSERT INTO configuration_versions "
                    "(version_number, parent_version_id, source, snapshot_json, checksum, release_notes, created_at) "
                    "VALUES (?, ?, 'draft', ?, ?, ?, ?)",
                    (
                        next_number,
                        int(row["base_version_id"]),
                        row["snapshot_json"],
                        row["checksum"],
                        row["release_notes"],
                        now,
                    ),
                )
                version_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE configuration_drafts SET status = 'finalized', finalized_version_id = ?, "
                    "updated_at = ? WHERE draft_id = ?",
                    (version_id, now, draft_id),
                )
                self._insert_audit(
                    connection,
                    action="draft_finalized",
                    outcome="success",
                    subject_type="configuration_version",
                    subject_id=str(version_id),
                    summary={"draft_id": draft_id, "version_number": next_number},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise
        return self.get_version(version_id)

    def delete_draft(self, draft_id: str, *, expected_revision: int) -> None:
        now = _utc_now()
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._draft(connection, draft_id)
                self._require_editable_revision(row, expected_revision)
                connection.execute(
                    "DELETE FROM configuration_drafts WHERE draft_id = ?", (draft_id,)
                )
                self._insert_audit(
                    connection,
                    action="draft_deleted",
                    outcome="success",
                    subject_type="configuration_draft",
                    subject_id=draft_id,
                    summary={"revision": int(row["revision"])},
                    occurred_at=now,
                )
                connection.execute("COMMIT")
            except Exception:
                self._rollback(connection)
                raise

    def draft_diff(self, draft_id: str) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            row = self._draft(connection, draft_id)
            base = connection.execute(
                "SELECT snapshot_json FROM configuration_versions WHERE version_id = ?",
                (int(row["base_version_id"]),),
            ).fetchone()
        if base is None:
            raise ConfigurationError("Draft base version is unavailable")
        before = json.loads(base["snapshot_json"])
        after = json.loads(row["snapshot_json"])
        return {
            "draft_id": draft_id,
            "base_version_id": int(row["base_version_id"]),
            "revision": int(row["revision"]),
            "changes": _stable_diff(before, after),
        }

    def record_audit(
        self,
        *,
        action: str,
        outcome: str,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        with closing(self._connect()) as connection:
            self._insert_audit(
                connection,
                action=action,
                outcome=outcome,
                subject_type=subject_type,
                subject_id=subject_id,
                summary=summary or {},
                occurred_at=_utc_now(),
            )

    def list_audit(self, *, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        page = max(1, min(int(page), 100_000))
        page_size = max(1, min(int(page_size), 100))
        with closing(self._connect()) as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM admin_audit_events").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM admin_audit_events ORDER BY occurred_at DESC, audit_id DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [
                {
                    "audit_id": int(row["audit_id"]),
                    "occurred_at": row["occurred_at"],
                    "action": row["action"],
                    "outcome": row["outcome"],
                    "subject_type": row["subject_type"],
                    "subject_id": row["subject_id"],
                    "summary": json.loads(row["summary_json"]),
                }
                for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def _validate(self, snapshot: Dict[str, Any]) -> List[ValidationIssue]:
        return validate_snapshot(
            snapshot,
            configured_aliases=self._aliases,
            forbidden_models=self._forbidden_models,
            forbidden_prefixes=self._forbidden_prefixes,
        )

    def _connect(self) -> sqlite3.Connection:
        database_uri = Path(self.db_path).resolve().as_uri() + "?mode=rw"
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass

    @staticmethod
    def _active_version_id(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT active_version_id FROM configuration_state WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise ConfigurationError("Active configuration is unavailable")
        return int(row["active_version_id"])

    @staticmethod
    def _draft(connection: sqlite3.Connection, draft_id: str) -> sqlite3.Row:
        if not isinstance(draft_id, str) or not 1 <= len(draft_id) <= 128:
            raise ConfigurationNotFound("Configuration draft was not found")
        row = connection.execute(
            "SELECT * FROM configuration_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise ConfigurationNotFound("Configuration draft was not found")
        return row

    @staticmethod
    def _require_editable_revision(row: sqlite3.Row, expected_revision: int) -> None:
        if row["status"] == "finalized":
            raise ConfigurationConflict("Finalized drafts are immutable")
        if int(row["revision"]) != int(expected_revision):
            raise ConfigurationConflict("Draft revision does not match")

    @staticmethod
    def _normalize_release_notes(value: Any) -> str:
        if not isinstance(value, str):
            raise SnapshotStructureError("release_notes must be a string")
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(normalized) > 2000:
            raise SnapshotStructureError("release_notes must not exceed 2000 characters")
        _reject_sensitive_text(normalized, "release_notes")
        return normalized

    @staticmethod
    def _normalize_draft_name(value: Any) -> str:
        if not isinstance(value, str):
            raise SnapshotStructureError("name must be a string")
        normalized = value.strip()
        if len(normalized) > 120:
            raise SnapshotStructureError("name must not exceed 120 characters")
        return normalized

    @staticmethod
    def _version_row(
        row: sqlite3.Row, include_snapshot: bool, active_version_id: int
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "version_id": int(row["version_id"]),
            "version_number": int(row["version_number"]),
            "parent_version_id": (
                int(row["parent_version_id"]) if row["parent_version_id"] is not None else None
            ),
            "source": row["source"],
            "checksum": row["checksum"],
            "release_notes": row["release_notes"],
            "created_at": row["created_at"],
            "is_active": int(row["version_id"]) == active_version_id,
            "publish_state": (
                "active" if int(row["version_id"]) == active_version_id else "pending"
            ),
        }
        if include_snapshot:
            item["snapshot"] = json.loads(row["snapshot_json"])
        return item

    @staticmethod
    def _draft_row(row: sqlite3.Row, *, include_snapshot: bool) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "draft_id": row["draft_id"],
            "base_version_id": int(row["base_version_id"]),
            "finalized_version_id": (
                int(row["finalized_version_id"])
                if row["finalized_version_id"] is not None
                else None
            ),
            "status": row["status"],
            "revision": int(row["revision"]),
            "checksum": row["checksum"],
            "validation_issues": json.loads(row["validation_json"]),
            "release_notes": row["release_notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "name": row["name"] if "name" in row.keys() else "",
            "is_active": bool(row["is_active"]) if "is_active" in row.keys() else False,
        }
        if include_snapshot:
            item["snapshot"] = json.loads(row["snapshot_json"])
        return item

    @staticmethod
    def _sanitize_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(summary, dict) or len(summary) > 20:
            raise ConfigurationError("Audit summary is invalid")
        result: Dict[str, Any] = {}
        for key, value in summary.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
                raise ConfigurationError("Audit summary key is invalid")
            if value is None or isinstance(value, (bool, int, float)):
                result[key] = value
            elif isinstance(value, str) and len(value) <= 256:
                _reject_sensitive_text(value, f"audit.{key}")
                result[key] = value
            else:
                raise ConfigurationError("Audit summary value is invalid")
        return result

    @classmethod
    def _insert_audit(
        cls,
        connection: sqlite3.Connection,
        *,
        action: str,
        outcome: str,
        subject_type: Optional[str],
        subject_id: Optional[str],
        summary: Dict[str, Any],
        occurred_at: str,
    ) -> None:
        if outcome not in {"success", "failure", "denied"}:
            raise ConfigurationError("Audit outcome is invalid")
        action = _expect_string(action, "audit.action", minimum=1, maximum=64)
        subject_type = (
            _expect_string(subject_type, "audit.subject_type", minimum=1, maximum=64)
            if subject_type is not None
            else None
        )
        subject_id = (
            _expect_string(subject_id, "audit.subject_id", minimum=1, maximum=128)
            if subject_id is not None
            else None
        )
        connection.execute(
            "INSERT INTO admin_audit_events "
            "(occurred_at, action, outcome, subject_type, subject_id, summary_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                occurred_at,
                action,
                outcome,
                subject_type,
                subject_id,
                _canonical_json(cls._sanitize_summary(summary)),
            ),
        )
        connection.execute(
            "DELETE FROM admin_audit_events WHERE audit_id <= COALESCE(("
            "SELECT audit_id FROM admin_audit_events ORDER BY audit_id DESC LIMIT 1 OFFSET ?"
            "), 0)",
            (AUDIT_RETENTION_ROWS,),
        )
