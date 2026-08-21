"""
OpenClaw Router Server
======================
OpenAI-compatible API server with intelligent LLM routing.

Usage:
    llmrouter serve --config configs/openclaw_example.yaml

Or directly:
    python server.py --config config.yaml
"""

import json
import hmac
import os
import re
import sys
import copy
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional, Dict, Any, List
from urllib.parse import urlparse

# Check dependencies
try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
    from pydantic import BaseModel
    import httpx
    import uvicorn
except ImportError:
    print("Please install: pip install fastapi uvicorn httpx pydantic")
    sys.exit(1)

# Handle both relative and direct imports
try:
    from .config import OpenClawConfig, LLMConfig, MODELS_WITHOUT_SYSTEM_ROLE, MODEL_CONTEXT_LIMITS
    from .routers import JudgeOutcome, OpenClawRouter, _safe_log
    from .media import process_multimodal_content, MediaConfig
    from .session_routing import (
        SessionRouteCache,
        count_user_turns,
        derive_session_key,
        detect_modality,
        parse_auto_policy,
    )
    from .control_center.runtime import ControlCenterRuntime
    from .control_center.status import admin_api_status
    from .control_center.telemetry import RoutingEvent
    from .control_center.maintenance import integrity_report
    from .control_center.configuration import (
        ConfigurationConflict,
        ConfigurationError,
        ConfigurationNotFound,
        DraftValidationError,
        SnapshotStructureError,
        apply_managed_snapshot,
        normalize_snapshot,
        validate_snapshot,
    )
except ImportError:
    from config import OpenClawConfig, LLMConfig, MODELS_WITHOUT_SYSTEM_ROLE, MODEL_CONTEXT_LIMITS
    from routers import JudgeOutcome, OpenClawRouter, _safe_log
    from media import process_multimodal_content, MediaConfig
    from session_routing import (
        SessionRouteCache,
        count_user_turns,
        derive_session_key,
        detect_modality,
        parse_auto_policy,
    )
    from control_center.runtime import ControlCenterRuntime
    from control_center.status import admin_api_status
    from control_center.telemetry import RoutingEvent
    from control_center.maintenance import integrity_report
    from control_center.configuration import (
        ConfigurationConflict,
        ConfigurationError,
        ConfigurationNotFound,
        DraftValidationError,
        SnapshotStructureError,
        apply_managed_snapshot,
        normalize_snapshot,
        validate_snapshot,
    )


# ============================================================
# Request/Response Models
# ============================================================

class Message(BaseModel):
    role: str
    content: Optional[Any] = None  # Can be string or list (multimodal)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None


class ChatRequest(BaseModel):
    model: str = "auto"
    messages: List[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = 4096
    stream: Optional[bool] = False
    user: Optional[str] = None  # Optional user id (used for memory scoping if enabled)
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    stream_options: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class RoutingDecision:
    selected_model: str
    session_key: Optional[str]
    cache_status: str
    rejudge_reason: Optional[str]
    judge_outcome: Optional[JudgeOutcome]


@dataclass(frozen=True)
class RuntimeBundle:
    """Immutable request-level references for one active configuration."""

    config: OpenClawConfig
    router: OpenClawRouter
    backend: "LLMBackend"
    route_cache: Optional[SessionRouteCache]
    version_id: Optional[int] = None
    version_number: Optional[int] = None


def _safe_token_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_token_counts(usage: Any) -> Dict[str, Optional[int]]:
    if not isinstance(usage, dict):
        usage = {}
    return {
        "prompt_tokens": _safe_token_count(usage.get("prompt_tokens")),
        "completion_tokens": _safe_token_count(usage.get("completion_tokens")),
        "total_tokens": _safe_token_count(usage.get("total_tokens")),
    }


def _stream_usage(chunk: str) -> Optional[Dict[str, Optional[int]]]:
    if "[DONE]" in chunk:
        return None
    try:
        json_text = chunk[6:] if chunk.startswith("data: ") else chunk
        payload = json.loads(json_text.strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    return _usage_token_counts(usage)


def _error_category(error: BaseException) -> str:
    if isinstance(error, HTTPException):
        return f"http_{error.status_code}"
    if isinstance(error, WebSocketDisconnect):
        return "client_disconnect"
    return type(error).__name__[:128]


# ============================================================
# Message Processing
# ============================================================

def normalize_content(content: Any) -> str:
    """Convert multimodal content to plain string"""
    if isinstance(content, str):
        return content
    elif isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif "text" in part:
                    text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
        return "\n".join(text_parts)
    return str(content) if content else ""


def build_routing_context(messages: List[Dict[str, Any]], max_chars: int) -> str:
    """Build a bounded judge context from user turns only; never include tool output."""
    user_turns = [
        normalize_content(message.get("content"))
        for message in messages
        if message.get("role") == "user"
    ]
    selected_turns = user_turns[-2:] or ["general query"]
    context = "\n\n".join(
        f"[user_turn_{index + 1}]\n{text}" for index, text in enumerate(selected_turns)
    )
    return context[: max(256, max_chars)]


def _bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _authorized(authorization: Optional[str], expected: Optional[str]) -> bool:
    if not expected:
        return True
    token = _bearer_token(authorization)
    return bool(token) and hmac.compare_digest(token, expected)


def normalize_messages(messages: List[Dict], model_id: str = "") -> List[Dict]:
    """Normalize message format for compatibility"""
    normalized = []
    system_content = ""

    for msg in messages:
        role = msg.get("role", "user")
        content = normalize_content(msg.get("content", ""))
        normalized_msg = {"role": role, "content": content}

        if msg.get("tool_calls") is not None:
            normalized_msg["tool_calls"] = msg["tool_calls"]
        if msg.get("tool_call_id") is not None:
            normalized_msg["tool_call_id"] = msg["tool_call_id"]
        if msg.get("function_call") is not None:
            normalized_msg["function_call"] = msg["function_call"]

        if role == "system":
            system_content = content
        else:
            normalized.append(normalized_msg)

    # Handle models without system role support
    if system_content and model_id in MODELS_WITHOUT_SYSTEM_ROLE:
        if normalized and normalized[0]["role"] == "user":
            normalized[0]["content"] = f"[System Instructions]\n{system_content}\n\n[User Message]\n{normalized[0]['content']}"
        else:
            normalized.insert(0, {"role": "user", "content": f"[System Instructions]\n{system_content}"})
    elif system_content:
        normalized.insert(0, {"role": "system", "content": system_content})

    return normalized


def estimate_tokens(text: str) -> int:
    """Estimate token count (approx 4 chars = 1 token)"""
    return len(text) // 4


def adjust_max_tokens(messages: List[Dict], model_id: str, requested_max: int) -> int:
    """Adjust max_tokens based on context limit"""
    context_limit = MODEL_CONTEXT_LIMITS.get(model_id, 32768)

    input_text = " ".join(m.get("content", "") for m in messages)
    input_tokens = estimate_tokens(input_text)

    available = context_limit - input_tokens - 100
    if available < 100:
        available = 100

    result = min(requested_max, available)

    # NVIDIA API limits max_tokens to 1024
    if model_id in MODELS_WITHOUT_SYSTEM_ROLE:
        result = min(result, 1024)

    return result


def clean_response(result: Dict) -> Dict:
    """Clean response for OpenAI compatibility"""
    usage = _clean_usage(result.get("usage"))

    cleaned = {
        "id": result.get("id", ""),
        "object": result.get("object", "chat.completion"),
        "model": result.get("model", ""),
        "choices": [],
        "usage": usage
    }

    for choice in result.get("choices", []):
        cleaned_choice = {
            "index": choice.get("index", 0),
            "finish_reason": choice.get("finish_reason", "stop")
        }
        if "message" in choice:
            msg = choice["message"]
            cleaned_choice["message"] = {
                "role": msg.get("role", "assistant"),
                "content": msg.get("content")
            }
            if msg.get("tool_calls") is not None:
                cleaned_choice["message"]["tool_calls"] = msg["tool_calls"]
            if msg.get("function_call") is not None:
                cleaned_choice["message"]["function_call"] = msg["function_call"]
        cleaned["choices"].append(cleaned_choice)

    return cleaned


def _message_has_tool_calls(message: Optional[Dict[str, Any]]) -> bool:
    return bool(message and (message.get("tool_calls") or message.get("function_call")))


def _delta_has_tool_calls(delta: Optional[Dict[str, Any]]) -> bool:
    return bool(delta and (delta.get("tool_calls") or delta.get("function_call")))


def _clean_usage_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            cleaned_item = _clean_usage_value(item)
            if cleaned_item is not None:
                cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        cleaned = []
        for item in value:
            cleaned_item = _clean_usage_value(item)
            if cleaned_item is not None:
                cleaned.append(cleaned_item)
        return cleaned
    if value is None:
        return None
    return value


def _clean_usage(usage_raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not usage_raw:
        return {}
    if not isinstance(usage_raw, dict):
        return {}
    cleaned_usage = _clean_usage_value(usage_raw)
    return cleaned_usage if isinstance(cleaned_usage, dict) else {}


def _merge_stream_options(stream_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(stream_options or {})
    merged.setdefault("include_usage", True)
    return merged


def clean_streaming_chunk(chunk: Dict) -> Optional[Dict]:
    """Clean streaming chunk for OpenAI compatibility"""
    choices = chunk.get("choices", [])
    usage = _clean_usage(chunk.get("usage"))
    if not choices and not usage:
        return None

    cleaned = {
        "id": chunk.get("id", ""),
        "object": chunk.get("object", "chat.completion.chunk"),
        "choices": []
    }
    if "model" in chunk:
        cleaned["model"] = chunk["model"]
    if usage:
        cleaned["usage"] = usage

    for choice in choices:
        finish_reason = choice.get("finish_reason")
        cleaned_choice = {
            "index": choice.get("index", 0),
            "finish_reason": finish_reason
        }

        if "delta" in choice:
            delta = choice["delta"]
            if finish_reason == "stop":
                cleaned_choice["delta"] = {}
            else:
                cleaned_delta = {}
                if "role" in delta:
                    cleaned_delta["role"] = delta["role"]
                if "content" in delta:
                    cleaned_delta["content"] = delta["content"]
                if "tool_calls" in delta:
                    cleaned_delta["tool_calls"] = delta["tool_calls"]
                if "function_call" in delta:
                    cleaned_delta["function_call"] = delta["function_call"]
                cleaned_choice["delta"] = cleaned_delta
        else:
            cleaned_choice["delta"] = {}

        cleaned["choices"].append(cleaned_choice)

    return cleaned


LOCAL_PROVIDER_HINTS = {
    "sglang",
    "vllm",
    "llama.cpp",
    "llama_cpp",
    "lmstudio",
    "lm_studio",
    "huggingface_cli",
}


def _is_local_base_url(base_url: str) -> bool:
    if not base_url:
        return False
    lower = base_url.lower()
    return (
        "localhost" in lower
        or "127.0.0.1" in lower
        or lower.startswith("http://0.0.0.0")
    )


def _resolve_auth_mode(provider: str, base_url: str, auth_mode: str = "auto", local: Optional[bool] = None) -> str:
    mode = (auth_mode or "auto").strip().lower()
    if mode in ("none", "bearer"):
        return mode

    provider_norm = (provider or "").strip().lower()
    is_local = bool(local) if local is not None else _is_local_base_url(base_url)
    if provider_norm in LOCAL_PROVIDER_HINTS or is_local:
        return "none"
    return "bearer"


def _build_chat_url(base_url: str, chat_path: str) -> str:
    path = (chat_path or "/chat/completions").strip()
    if not path.startswith("/"):
        path = "/" + path
    return f"{(base_url or '').rstrip('/')}{path}"


# ============================================================
# LLM Backend
# ============================================================

class LLMBackend:
    """LLM API caller"""

    def __init__(self, config: OpenClawConfig):
        self.config = config

    async def call(self, llm_name: str, messages: List[Dict], max_tokens: int = 4096,
                   temperature: Optional[float] = None, stream: bool = False,
                   tools: Optional[List[Dict[str, Any]]] = None,
                   tool_choice: Optional[Any] = None,
                   stream_options: Optional[Dict[str, Any]] = None):
        """Call LLM API"""
        if llm_name not in self.config.llms:
            raise HTTPException(status_code=404, detail=f"LLM '{llm_name}' not found")

        llm_config = self.config.llms[llm_name]
        api_key = self.config.get_api_key(llm_config.provider, llm_config)

        if stream:
            return self._call_streaming(
                llm_config,
                messages,
                max_tokens,
                temperature,
                api_key,
                tools,
                tool_choice,
                stream_options,
            )
        else:
            return await self._call_sync(llm_config, messages, max_tokens, temperature, api_key, tools, tool_choice)

    async def _call_sync(self, llm: LLMConfig, messages: List[Dict], max_tokens: int,
                         temperature: Optional[float], api_key: Optional[str],
                         tools: Optional[List[Dict[str, Any]]] = None,
                         tool_choice: Optional[Any] = None) -> Dict:
        """Synchronous API call"""
        normalized = normalize_messages(messages, llm.model_id)
        adjusted_max = adjust_max_tokens(normalized, llm.model_id, max_tokens)
        auth_mode = _resolve_auth_mode(llm.provider, llm.base_url, llm.auth_mode, llm.local)
        chat_url = _build_chat_url(llm.base_url, llm.chat_path)


        async with httpx.AsyncClient() as client:
            headers = {"Content-Type": "application/json"}
            if auth_mode == "bearer" and api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            body = {
                "model": llm.model_id,
                "messages": normalized,
                "max_tokens": adjusted_max,
                # OpenAI-compatible gateways are not consistent about the
                # default when this field is omitted. 9router treats an
                # unspecified value as streaming, which cannot be decoded by
                # this synchronous response path.
                "stream": False,
            }
            if temperature is not None:
                body["temperature"] = temperature
            if tools is not None:
                body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice

            resp = await client.post(
                chat_url,
                headers=headers,
                json=body,
                timeout=120.0
            )

            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])

            result = resp.json()
            return clean_response(result)

    async def _call_streaming(self, llm: LLMConfig, messages: List[Dict], max_tokens: int,
                          temperature: Optional[float], api_key: Optional[str],
                          tools: Optional[List[Dict[str, Any]]] = None,
                          tool_choice: Optional[Any] = None,
                          stream_options: Optional[Dict[str, Any]] = None) -> AsyncGenerator:
        """Streaming API call"""
        normalized = normalize_messages(messages, llm.model_id)
        adjusted_max = adjust_max_tokens(normalized, llm.model_id, max_tokens)
        auth_mode = _resolve_auth_mode(llm.provider, llm.base_url, llm.auth_mode, llm.local)
        chat_url = _build_chat_url(llm.base_url, llm.chat_path)

        async with httpx.AsyncClient() as client:
            headers = {"Content-Type": "application/json"}
            if auth_mode == "bearer" and api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            body = {
                "model": llm.model_id,
                "messages": normalized,
                "max_tokens": adjusted_max,
                "stream": True,
                "stream_options": _merge_stream_options(stream_options),
            }
            if temperature is not None:
                body["temperature"] = temperature
            if tools is not None:
                body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice

            async with client.stream(
                "POST",
                chat_url,
                headers=headers,
                json=body,
                timeout=120.0
            ) as resp:
                if resp.status_code != 200:
                    error = await resp.aread()
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=error.decode(errors="replace")[:500],
                    )

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        yield line + "\n\n"


# ============================================================
# FastAPI App Factory
# ============================================================

def create_app(config: OpenClawConfig = None, config_path: str = None) -> FastAPI:
    """Create FastAPI application"""
    if config is None and config_path:
        config = OpenClawConfig.from_yaml(config_path)
    elif config is None:
        config = OpenClawConfig()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        runtime = getattr(application.state, "control_center", None)
        if runtime is not None:
            runtime.shutdown(timeout=2.0)

    app = FastAPI(
        title="OpenClaw Router",
        description="OpenAI-compatible API with intelligent LLM routing",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Initialize components
    router = OpenClawRouter(config)
    backend = LLMBackend(config)
    route_cache = (
        SessionRouteCache(config.session_routing)
        if config.session_routing.enabled
        else None
    )
    app.state.router = router
    app.state.backend = backend
    app.state.route_cache = route_cache

    # Initialize Control Center in a failure-isolated way. When disabled, this only
    # creates a lightweight runtime object and never touches the database or data_dir.
    control_center = ControlCenterRuntime(config.control_center, application_config=config)
    control_center.initialize()
    app.state.control_center = control_center
    runtime_lock = threading.RLock()
    initial_version_id: Optional[int] = None
    initial_version_number: Optional[int] = None
    if control_center.configuration is not None:
        try:
            active = control_center.configuration.active_configuration()
            initial_version_id = int(active["version_id"])
            initial_version_number = int(active["version_number"])
        except Exception:
            pass
    runtime_bundle = RuntimeBundle(
        config=config,
        router=router,
        backend=backend,
        route_cache=route_cache,
        version_id=initial_version_id,
        version_number=initial_version_number,
    )
    app.state.runtime_bundle = runtime_bundle

    def current_runtime() -> RuntimeBundle:
        with runtime_lock:
            return runtime_bundle

    def swap_runtime(candidate: RuntimeBundle) -> RuntimeBundle:
        nonlocal runtime_bundle
        with runtime_lock:
            previous = runtime_bundle
            runtime_bundle = candidate
            app.state.runtime_bundle = candidate
            app.state.router = candidate.router
            app.state.backend = candidate.backend
            app.state.route_cache = candidate.route_cache
            return previous

    def cache_affecting_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
        """Return whether a version change can alter sticky routing semantics."""
        before_router = dict(before.get("router", {}))
        after_router = dict(after.get("router", {}))
        before_session = before.get("session_routing", {})
        after_session = after.get("session_routing", {})
        before_llms = {
            alias: {key: value for key, value in values.items() if key != "description"}
            for alias, values in before.get("llms", {}).items()
        }
        after_llms = {
            alias: {key: value for key, value in values.items() if key != "description"}
            for alias, values in after.get("llms", {}).items()
        }
        return (
            before_router != after_router
            or before_session != after_session
            or before_llms != after_llms
        )

    def build_runtime_candidate(version: Dict[str, Any], current: RuntimeBundle) -> RuntimeBundle:
        candidate_config = copy.copy(current.config)
        for field_name in (
            "router",
            "llms",
            "api_keys",
            "memory",
            "session_routing",
            "security",
            "media",
            "control_center",
        ):
            setattr(candidate_config, field_name, copy.deepcopy(getattr(current.config, field_name)))
        # The key cycle is intentionally shared, while the lock belongs to the
        # candidate object so no deepcopy or cross-version lock sharing occurs.
        candidate_config._nvidia_key_cycle = current.config._nvidia_key_cycle
        candidate_config._nvidia_key_lock = threading.Lock()
        apply_managed_snapshot(candidate_config, version["snapshot"])
        candidate_router = OpenClawRouter(candidate_config)
        candidate_backend = LLMBackend(candidate_config)
        candidate_cache = (
            SessionRouteCache(candidate_config.session_routing)
            if candidate_config.session_routing.enabled
            else None
        )
        return RuntimeBundle(
            config=candidate_config,
            router=candidate_router,
            backend=candidate_backend,
            route_cache=candidate_cache,
            version_id=int(version["version_id"]),
            version_number=int(version["version_number"]),
        )

    def activate_runtime_version(
        version_id: int,
        *,
        expected_active_version_id: int,
    ) -> Dict[str, Any]:
        service = configuration_service()
        with runtime_lock:
            current = runtime_bundle
            current_version_id = current.version_id
            if current_version_id is not None and current_version_id != int(expected_active_version_id):
                raise ConfigurationConflict("Active runtime version changed")
            version = service.get_version(int(version_id))
            if version["publish_state"] == "active":
                raise ConfigurationConflict("Configuration version is already active")
            candidate = build_runtime_candidate(version, current)
            previous_cache_size = current.route_cache.size if current.route_cache else 0
            activated = service.activate_version(
                int(version_id),
                expected_active_version_id=int(expected_active_version_id),
            )
            old_snapshot = service.get_version(int(expected_active_version_id))["snapshot"]
            semantics_changed = cache_affecting_changed(old_snapshot, version["snapshot"])
            cleared = previous_cache_size if semantics_changed else 0
            if not semantics_changed and current.route_cache is not None and candidate.route_cache is not None:
                candidate = RuntimeBundle(
                    config=candidate.config,
                    router=candidate.router,
                    backend=candidate.backend,
                    route_cache=current.route_cache,
                    version_id=candidate.version_id,
                    version_number=candidate.version_number,
                )
            swap_runtime(candidate)
            activated["cache_cleared"] = cleared
            activated["cache_clear_reason"] = "routing_semantics_changed" if semantics_changed else "display_only"
            return activated

    def rollback_runtime_version(
        target_version_id: int,
        *,
        expected_active_version_id: int,
        release_notes: str,
    ) -> Dict[str, Any]:
        service = configuration_service()
        with runtime_lock:
            current = runtime_bundle
            if current.version_id is not None and current.version_id != int(expected_active_version_id):
                raise ConfigurationConflict("Active runtime version changed")
            target = service.get_version(int(target_version_id))
            candidate = build_runtime_candidate(target, current)
            previous_cache_size = current.route_cache.size if current.route_cache else 0
            activated = service.rollback_version(
                int(target_version_id),
                expected_active_version_id=int(expected_active_version_id),
                release_notes=release_notes,
            )
            old_snapshot = current.config and service.get_version(int(expected_active_version_id))["snapshot"]
            semantics_changed = cache_affecting_changed(old_snapshot, target["snapshot"])
            cleared = previous_cache_size if semantics_changed else 0
            if not semantics_changed and current.route_cache is not None and candidate.route_cache is not None:
                candidate = RuntimeBundle(
                    config=candidate.config,
                    router=candidate.router,
                    backend=candidate.backend,
                    route_cache=current.route_cache,
                    version_id=candidate.version_id,
                    version_number=candidate.version_number,
                )
            candidate = RuntimeBundle(
                config=candidate.config,
                router=candidate.router,
                backend=candidate.backend,
                route_cache=candidate.route_cache,
                version_id=int(activated["version_id"]),
                version_number=int(activated["version_number"]),
            )
            swap_runtime(candidate)
            activated["cache_cleared"] = cleared
            activated["cache_clear_reason"] = "routing_semantics_changed" if semantics_changed else "display_only"
            return activated
    dashboard_dir = Path(
        os.getenv(
            "LLMROUTER_DASHBOARD_DIR",
            str(Path(__file__).resolve().parent / "control_center" / "static"),
        )
    ).resolve()

    def emit_routing_event(
        *,
        request_id: str,
        event_kind: str,
        transport: str,
        requested_model: str,
        traffic_class: str = "production",
        **values: Any,
    ) -> bool:
        if control_center.telemetry is None:
            return False
        try:
            values.setdefault("config_version_id", current_runtime().version_id)
            return control_center.record(
                RoutingEvent.create(
                    request_id=request_id,
                    event_kind=event_kind,
                    traffic_class=traffic_class,
                    transport=transport,
                    requested_model=requested_model,
                    **values,
                )
            )
        except Exception:
            # Event construction and submission are both outside the inference
            # correctness boundary.
            return False

    def telemetry_model_label(requested_model: str) -> str:
        if requested_model in config.llms:
            return requested_model
        if parse_auto_policy(requested_model, config.session_routing) is not None:
            return requested_model
        return "invalid"

    def admin_error(status_code: int, code: str, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": code, "message": message}},
            headers={"Cache-Control": "no-store"},
        )

    class AdminAccessError(Exception):
        def __init__(self, status_code: int, code: str, message: str):
            self.status_code = status_code
            self.code = code
            self.message = message

    @app.exception_handler(AdminAccessError)
    async def handle_admin_access_error(_request: Request, exc: AdminAccessError):
        return admin_error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(ConfigurationError)
    async def handle_configuration_error(_request: Request, exc: ConfigurationError):
        if isinstance(exc, ConfigurationNotFound):
            return admin_error(404, "configuration_not_found", "Configuration resource was not found")
        if isinstance(exc, ConfigurationConflict):
            return admin_error(409, "configuration_conflict", str(exc))
        if isinstance(exc, DraftValidationError):
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "configuration_invalid",
                        "message": "Configuration validation failed",
                    },
                    "issues": [issue.as_dict() for issue in exc.issues],
                },
                headers={"Cache-Control": "no-store"},
            )
        if isinstance(exc, SnapshotStructureError):
            return admin_error(422, "configuration_structure_invalid", str(exc))
        return admin_error(503, "configuration_unavailable", "Configuration storage is unavailable")

    def local_origin_allowed(request: Request) -> bool:
        origin = request.headers.get("origin", "")
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
            and parsed.netloc.lower() == request.headers.get("host", "").lower()
        )

    def normalized_time_filter(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if len(value) > 128:
            raise ValueError("time filter is too long")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("time filter must include a timezone")
        return parsed.astimezone(timezone.utc).isoformat()

    def admin_session_valid(request: Request, *, require_csrf: bool = False) -> bool:
        auth = control_center.admin_auth
        if auth is None or not auth.configured:
            return False
        csrf_token = request.headers.get(auth.CSRF_HEADER) if require_csrf else None
        if require_csrf and not csrf_token:
            return False
        return auth.verify(
            request.cookies.get(auth.COOKIE_NAME),
            csrf_token=csrf_token,
        )

    def require_admin_session(request: Request) -> None:
        if not control_center.enabled:
            raise AdminAccessError(
                404,
                "control_center_disabled",
                "Control Center is disabled",
            )
        if not admin_session_valid(request):
            raise AdminAccessError(
                401,
                "admin_unauthorized",
                "Administrator session is required",
            )

    def require_admin_write(request: Request) -> None:
        auth = control_center.admin_auth
        if not local_origin_allowed(request):
            raise AdminAccessError(403, "invalid_origin", "Request origin is not allowed")
        if auth is None or not admin_session_valid(request, require_csrf=True):
            raise AdminAccessError(
                401,
                "admin_unauthorized",
                "Administrator session is required",
            )

    async def read_admin_json(request: Request, *, maximum_bytes: int = 262_144) -> Any:
        content_length = request.headers.get("content-length")
        if content_length and (
            not content_length.isdigit() or int(content_length) > maximum_bytes
        ):
            raise AdminAccessError(413, "request_too_large", "Request body is too large")
        raw_body = bytearray()
        async for chunk in request.stream():
            raw_body.extend(chunk)
            if len(raw_body) > maximum_bytes:
                raise AdminAccessError(413, "request_too_large", "Request body is too large")
        try:
            return json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise SnapshotStructureError("Request body must contain valid JSON") from None

    def strict_object(
        value: Any,
        *,
        allowed: set[str],
        required: set[str],
    ) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise SnapshotStructureError("Request body must be an object")
        unknown = sorted(set(value) - allowed)
        missing = sorted(required - set(value))
        if unknown:
            raise SnapshotStructureError(
                "Request body contains unknown fields: " + ", ".join(unknown)
            )
        if missing:
            raise SnapshotStructureError(
                "Request body is missing fields: " + ", ".join(missing)
            )
        return value

    def configuration_service():
        service = control_center.configuration
        if service is None:
            raise ConfigurationError("Configuration service is unavailable")
        return service

    def record_admin_audit(
        action: str,
        outcome: str,
        *,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        service = control_center.configuration
        if service is None:
            return
        try:
            service.record_audit(
                action=action,
                outcome=outcome,
                subject_type=subject_type,
                subject_id=subject_id,
                summary=summary,
            )
        except Exception:
            # Audit failure must not reveal details or change authentication behavior.
            return

    @app.middleware("http")
    async def admin_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/admin/api/") or request.url.path.startswith(
            "/dashboard"
        ):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=()"
            )
            if request.url.path.startswith("/admin/api/") or not request.url.path.startswith(
                "/dashboard/assets/"
            ):
                response.headers["Cache-Control"] = "no-store"
            else:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.middleware("http")
    async def require_v1_bearer(request: Request, call_next):
        if request.url.path.startswith("/v1/") and not _authorized(
            request.headers.get("authorization"), config.security.inbound_api_key
        ):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "authentication_error"}},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    async def choose_request_model(
        payload: ChatRequest,
        messages: List[Dict[str, Any]],
        session_header: Optional[str],
        bundle: RuntimeBundle,
        judge_observer: Optional[Callable[[JudgeOutcome], None]] = None,
    ) -> RoutingDecision:
        request_config = bundle.config
        request_router = bundle.router
        request_route_cache = bundle.route_cache
        available_models = list(request_config.llms.keys())
        if payload.model in available_models:
            _safe_log(f"[Route] explicit model={payload.model}")
            return RoutingDecision(
                selected_model=payload.model,
                session_key=None,
                cache_status="not_applicable",
                rejudge_reason=None,
                judge_outcome=None,
            )

        policy = parse_auto_policy(payload.model, request_config.session_routing)
        if policy is None:
            raise HTTPException(status_code=404, detail=f"Model '{payload.model}' not found")

        routing_context = build_routing_context(
            messages, request_config.router.routing_context_chars
        )
        judge_outcomes: List[JudgeOutcome] = []

        def observe_judge(outcome: JudgeOutcome) -> None:
            judge_outcomes.append(outcome)
            if judge_observer is not None:
                judge_observer(outcome)

        if request_route_cache is None:
            selected = await request_router.select_model(
                routing_context,
                user=payload.user,
                judge_observer=observe_judge,
            )
            _safe_log(f"[Route] auto model={selected} cache=disabled")
            return RoutingDecision(
                selected_model=selected,
                session_key=None,
                cache_status="disabled",
                rejudge_reason="cache_disabled",
                judge_outcome=judge_outcomes[-1] if judge_outcomes else None,
            )

        session_key = derive_session_key(
            messages,
            user=payload.user,
            header_value=session_header,
            fallback_hash_chars=request_config.session_routing.fallback_hash_chars,
        )
        user_turns = count_user_turns(messages)
        modality = detect_modality(messages)
        selected, cache_hit, rejudge_reason = await request_route_cache.get_or_select_detailed(
            session_key,
            user_turns=user_turns,
            policy=policy,
            modality=modality,
            allowed_models=available_models,
            selector=lambda: request_router.select_model(
                routing_context,
                user=payload.user,
                judge_observer=observe_judge,
            ),
        )
        _safe_log(
            f"[Route] session={session_key[:12]} model={selected} "
            f"cache={'hit' if cache_hit else 'miss'} turns={user_turns} policy={policy.model_id}"
        )
        return RoutingDecision(
            selected_model=selected,
            session_key=session_key,
            cache_status="hit" if cache_hit else "miss",
            rejudge_reason=rejudge_reason,
            judge_outcome=judge_outcomes[-1] if judge_outcomes else None,
        )

    @app.get("/health")
    async def health():
        current = current_runtime()
        return {
            "status": "ok",
            "strategy": current.config.router.strategy,
            "llms": list(current.config.llms.keys()),
            "session_cache_entries": current.route_cache.size if current.route_cache else 0,
            "commit": os.getenv("LLMROUTER_COMMIT_SHA", "unknown"),
        }

    protected_admin = APIRouter(
        prefix="/admin/api",
        dependencies=[Depends(require_admin_session)],
    )
    protected_admin_write = APIRouter(
        prefix="/admin/api",
        dependencies=[Depends(require_admin_session), Depends(require_admin_write)],
    )

    @app.post("/admin/api/login", include_in_schema=False)
    async def admin_login(request: Request):
        if not control_center.enabled:
            return await admin_api_status(request)
        auth = control_center.admin_auth
        if auth is None or not auth.configured:
            record_admin_audit("login", "failure", summary={"reason": "auth_unavailable"})
            return admin_error(503, "admin_auth_unavailable", "Administrator login is unavailable")
        if not local_origin_allowed(request):
            record_admin_audit("login", "denied", summary={"reason": "invalid_origin"})
            return admin_error(403, "invalid_origin", "Request origin is not allowed")
        content_length = request.headers.get("content-length")
        if content_length and (
            not content_length.isdigit() or int(content_length) > 8192
        ):
            record_admin_audit("login", "failure", summary={"reason": "request_too_large"})
            return admin_error(413, "request_too_large", "Login request is too large")
        try:
            raw_body = bytearray()
            async for chunk in request.stream():
                raw_body.extend(chunk)
                if len(raw_body) > 8192:
                    record_admin_audit(
                        "login", "failure", summary={"reason": "request_too_large"}
                    )
                    return admin_error(413, "request_too_large", "Login request is too large")
            body = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            body = {}
        candidate = (
            body.get("token", "")
            if isinstance(body, dict) and set(body) == {"token"}
            else ""
        )
        candidate = candidate if isinstance(candidate, str) else ""
        client_key = request.client.host if request.client else "unknown"
        session, result = auth.login(candidate, client_key)
        if session is None:
            record_admin_audit("login", "failure", summary={"reason": result})
            status_code = 429 if result == "rate_limited" else 401
            response = admin_error(
                status_code,
                "login_failed",
                "Invalid administrator credentials",
            )
            if status_code == 429:
                response.headers["Retry-After"] = str(auth.login_window_seconds)
            return response
        record_admin_audit("login", "success")
        response = JSONResponse(
            {
                "status": "ok",
                "csrf_token": session.csrf_token,
                "expires_in": auth.session_ttl_seconds,
            },
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            auth.COOKIE_NAME,
            session.session_token,
            max_age=auth.session_ttl_seconds,
            httponly=True,
            secure=urlparse(request.headers.get("origin", "")).scheme == "https",
            samesite="strict",
            path="/admin",
        )
        return response

    @protected_admin.get("/session", include_in_schema=False)
    async def admin_session(request: Request):
        auth = control_center.admin_auth
        csrf_token = (
            auth.csrf_for_session(request.cookies.get(auth.COOKIE_NAME))
            if auth is not None
            else None
        )
        if not csrf_token:
            return admin_error(401, "admin_unauthorized", "Administrator session is required")
        return JSONResponse(
            {"status": "ok", "csrf_token": csrf_token},
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.post("/logout", include_in_schema=False)
    async def admin_logout(request: Request):
        auth = control_center.admin_auth
        assert auth is not None
        auth.logout(request.cookies.get(auth.COOKIE_NAME))
        record_admin_audit("logout", "success")
        response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(auth.COOKIE_NAME, path="/admin", samesite="strict")
        return response

    @protected_admin_write.post("/password", include_in_schema=False)
    async def admin_change_password(request: Request):
        auth = control_center.admin_auth
        if auth is None:
            return admin_error(503, "admin_auth_unavailable", "Administrator authentication is unavailable")
        body = strict_object(
            await read_admin_json(request),
            allowed={"current_password", "new_password"},
            required={"current_password", "new_password"},
        )
        current_password = body["current_password"]
        new_password = body["new_password"]
        if not isinstance(current_password, str) or not isinstance(new_password, str):
            raise SnapshotStructureError("Password fields must be strings")
        if len(new_password) < 8:
            raise SnapshotStructureError("New password must be at least 8 characters")
        if not auth.change_token(current_password, new_password):
            record_admin_audit("password_change", "denied", summary={"reason": "invalid_current_password"})
            return admin_error(403, "password_change_denied", "Current password is incorrect")
        record_admin_audit("password_change", "success")
        response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(auth.COOKIE_NAME, path="/admin", samesite="strict")
        return response

    @protected_admin.get("/status", include_in_schema=False)
    async def protected_admin_status(request: Request):
        return await admin_api_status(request)

    @protected_admin.get("/overview", include_in_schema=False)
    async def admin_overview():
        if control_center.queries is None:
            return admin_error(503, "telemetry_unavailable", "Telemetry is unavailable")
        return JSONResponse(
            control_center.queries.overview(),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/requests", include_in_schema=False)
    async def admin_requests(
        page: int = 1,
        page_size: int = 50,
        since: Optional[str] = None,
        until: Optional[str] = None,
        traffic_class: Optional[str] = None,
        selected_model: Optional[str] = None,
        final_status: Optional[str] = None,
        config_version_id: Optional[int] = None,
    ):
        if control_center.queries is None:
            return admin_error(503, "telemetry_unavailable", "Telemetry is unavailable")
        for value in (traffic_class, selected_model, final_status):
            if value is not None and len(value) > 128:
                return admin_error(422, "invalid_filter", "A request filter is invalid")
        try:
            normalized_since = normalized_time_filter(since)
            normalized_until = normalized_time_filter(until)
        except (ValueError, TypeError):
            return admin_error(422, "invalid_filter", "A request filter is invalid")
        return JSONResponse(
            control_center.queries.request_page(
                page=page,
                page_size=page_size,
                since=normalized_since,
                until=normalized_until,
                traffic_class=traffic_class,
                selected_model=selected_model,
                final_status=final_status,
                config_version_id=config_version_id,
            ),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/health", include_in_schema=False)
    async def admin_health():
        return JSONResponse(
            control_center.status_payload(),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/runtime", include_in_schema=False)
    async def admin_runtime():
        current = current_runtime()
        active = (
            control_center.configuration.active_configuration()
            if control_center.configuration is not None
            else None
        )
        return JSONResponse(
            {
                "strategy": current.config.router.strategy,
                "models": [
                    {"name": name, "description": llm.description}
                    for name, llm in current.config.llms.items()
                ],
                "session_cache_entries": current.route_cache.size if current.route_cache else 0,
                "commit": os.getenv("LLMROUTER_COMMIT_SHA", "unknown"),
                "schema_version": control_center.schema_version,
                "config_version_id": active["version_id"] if active else None,
                "config_version_number": active["version_number"] if active else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/configuration", include_in_schema=False)
    async def admin_configuration():
        service = configuration_service()
        return JSONResponse(
            {
                "schema_version": 1,
                "active": service.active_configuration(),
                "read_only": service.read_only_metadata(),
                "smart_routes": service.list_drafts(),
                "active_smart_routes": service.list_active_drafts(),
                "model_catalog": service.model_catalog(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/configuration/versions", include_in_schema=False)
    async def admin_configuration_versions(page: int = 1, page_size: int = 25):
        return JSONResponse(
            configuration_service().list_versions(page=page, page_size=page_size),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get(
        "/configuration/versions/{version_id}", include_in_schema=False
    )
    async def admin_configuration_version(version_id: int):
        return JSONResponse(
            configuration_service().get_version(version_id),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/configuration/routes", include_in_schema=False)
    async def admin_configuration_drafts():
        return JSONResponse(
            {"items": configuration_service().list_drafts()},
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get(
        "/configuration/routes/{draft_id}", include_in_schema=False
    )
    async def admin_configuration_draft(draft_id: str):
        return JSONResponse(
            configuration_service().get_draft(draft_id),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get(
        "/configuration/routes/{draft_id}/diff", include_in_schema=False
    )
    async def admin_configuration_draft_diff(draft_id: str):
        return JSONResponse(
            configuration_service().draft_diff(draft_id),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/audit", include_in_schema=False)
    async def admin_audit(page: int = 1, page_size: int = 50):
        return JSONResponse(
            configuration_service().list_audit(page=page, page_size=page_size),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/maintenance/integrity", include_in_schema=False)
    async def admin_integrity():
        report = integrity_report(control_center.config.db_path)
        record_admin_audit(
            "integrity_check",
            "success" if report["status"] == "ok" else "failure",
            summary={"status": report["status"]},
        )
        return JSONResponse(report, headers={"Cache-Control": "no-store"})

    @protected_admin_write.post("/configuration/routes", include_in_schema=False)
    async def create_configuration_draft(request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"base_version_id", "release_notes", "name"},
            required=set(),
        )
        base_version_id = body.get("base_version_id")
        if base_version_id is not None and (
            isinstance(base_version_id, bool) or not isinstance(base_version_id, int)
        ):
            raise SnapshotStructureError("base_version_id must be an integer")
        return JSONResponse(
            configuration_service().create_draft(
                base_version_id=base_version_id,
                release_notes=body.get("release_notes", ""),
                name=body.get("name", ""),
            ),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.put(
        "/configuration/routes/{draft_id}", include_in_schema=False
    )
    async def update_configuration_draft(draft_id: str, request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"revision", "snapshot", "release_notes", "name"},
            required={"revision", "snapshot", "release_notes"},
        )
        revision = body["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise SnapshotStructureError("revision must be an integer")
        return JSONResponse(
            configuration_service().update_draft(
                draft_id,
                expected_revision=revision,
                snapshot=body["snapshot"],
                release_notes=body["release_notes"],
                name=body.get("name"),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.post(
        "/configuration/routes/{draft_id}/validate", include_in_schema=False
    )
    async def validate_configuration_draft(draft_id: str, request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"revision"},
            required={"revision"},
        )
        revision = body["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise SnapshotStructureError("revision must be an integer")
        return JSONResponse(
            configuration_service().validate_draft(
                draft_id, expected_revision=revision
            ),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.post(
        "/configuration/routes/{draft_id}/activation", include_in_schema=False
    )
    async def set_configuration_draft_activation(draft_id: str, request: Request):
        body = strict_object(await read_admin_json(request), allowed={"active"}, required={"active"})
        if not isinstance(body["active"], bool):
            raise SnapshotStructureError("active must be a boolean")
        return JSONResponse(
            configuration_service().set_draft_active(draft_id, active=body["active"]),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/configuration/model-catalog", include_in_schema=False)
    async def configuration_model_catalog():
        return JSONResponse({"models": configuration_service().model_catalog()}, headers={"Cache-Control": "no-store"})

    @protected_admin_write.put("/configuration/model-catalog", include_in_schema=False)
    async def update_configuration_model_catalog(request: Request):
        body = strict_object(await read_admin_json(request), allowed={"models"}, required={"models"})
        models = body["models"]
        if not isinstance(models, list) or any(not isinstance(model, str) for model in models):
            raise SnapshotStructureError("models must be an array of strings")
        return JSONResponse({"models": configuration_service().replace_model_catalog(models)}, headers={"Cache-Control": "no-store"})

    @protected_admin_write.post(
        "/configuration/routes/{draft_id}/finalize", include_in_schema=False
    )
    async def finalize_configuration_draft(draft_id: str, request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"revision"},
            required={"revision"},
        )
        revision = body["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise SnapshotStructureError("revision must be an integer")
        return JSONResponse(
            configuration_service().finalize_draft(
                draft_id, expected_revision=revision
            ),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.post(
        "/configuration/versions/{version_id}/activate", include_in_schema=False
    )
    async def activate_configuration_version(version_id: int, request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"expected_active_version_id"},
            required={"expected_active_version_id"},
        )
        expected = body["expected_active_version_id"]
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise SnapshotStructureError("expected_active_version_id must be an integer")
        return JSONResponse(
            activate_runtime_version(
                int(version_id), expected_active_version_id=expected
            ),
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin_write.post(
        "/configuration/versions/{version_id}/rollback", include_in_schema=False
    )
    async def rollback_configuration_version(version_id: int, request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"expected_active_version_id", "release_notes"},
            required={"expected_active_version_id"},
        )
        expected = body["expected_active_version_id"]
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise SnapshotStructureError("expected_active_version_id must be an integer")
        release_notes = body.get("release_notes", "")
        if not isinstance(release_notes, str):
            raise SnapshotStructureError("release_notes must be a string")
        return JSONResponse(
            rollback_runtime_version(
                int(version_id),
                expected_active_version_id=expected,
                release_notes=release_notes,
            ),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @protected_admin.get("/discovery/models", include_in_schema=False)
    async def discover_upstream_models():
        current = current_runtime()
        upstream = current.config.router
        base_url = (upstream.base_url or "").rstrip("/")
        if not base_url:
            return JSONResponse(
                {"status": "unavailable", "models": [], "reason": "router_base_url_missing"},
                headers={"Cache-Control": "no-store"},
            )
        api_key = current.config.get_api_key(upstream.provider or "openai")
        headers = {"Accept": "application/json"}
        if api_key and upstream.auth_mode != "none":
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{base_url}/models", headers=headers)
            if response.status_code != 200:
                record_admin_audit("model_discovery", "failure", summary={"reason": "upstream_status"})
                return JSONResponse(
                    {"status": "unavailable", "models": [], "reason": "upstream_status"},
                    headers={"Cache-Control": "no-store"},
                )
            body = response.json()
            raw_models = body.get("data", []) if isinstance(body, dict) else []
            models = sorted(
                {
                    str(item.get("id"))
                    for item in raw_models
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            )
            record_admin_audit("model_discovery", "success", summary={"model_count": len(models)})
            return JSONResponse(
                {"status": "ok", "models": models, "combo_internal_recursion_checked": False},
                headers={"Cache-Control": "no-store"},
            )
        except (httpx.HTTPError, ValueError, TypeError):
            record_admin_audit("model_discovery", "failure", summary={"reason": "upstream_unavailable"})
            return JSONResponse(
                {"status": "unavailable", "models": [], "reason": "upstream_unavailable"},
                headers={"Cache-Control": "no-store"},
            )

    @protected_admin_write.post("/route-lab/evaluate", include_in_schema=False)
    async def evaluate_route_lab(request: Request):
        body = strict_object(
            await read_admin_json(request),
            allowed={"text", "version_id", "route_id", "compare_version_id"},
            required={"text"},
        )
        text_value = body["text"]
        if not isinstance(text_value, str) or not text_value.strip() or len(text_value) > 20_000:
            raise SnapshotStructureError("text must be a non-empty string up to 20000 characters")
        version_id = body.get("version_id")
        draft_id = body.get("route_id")
        compare_version_id = body.get("compare_version_id")
        for name, value in (("version_id", version_id), ("compare_version_id", compare_version_id)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise SnapshotStructureError(f"{name} must be an integer")
        if draft_id is not None and (not isinstance(draft_id, str) or len(draft_id) > 128):
            raise SnapshotStructureError("route_id is invalid")

        current = current_runtime()
        service = configuration_service()

        def candidate_for(selected_version_id: Optional[int], selected_draft_id: Optional[str]) -> RuntimeBundle:
            if selected_draft_id:
                draft = service.get_draft(selected_draft_id)
                snapshot = normalize_snapshot(draft["snapshot"], current.config.llms)
                issues = validate_snapshot(
                    snapshot,
                    configured_aliases=current.config.llms,
                    forbidden_models=current.config.security.forbidden_upstream_models,
                    forbidden_prefixes=current.config.security.forbidden_upstream_prefixes,
                )
                if issues:
                    raise DraftValidationError(issues)
                version = {
                    "version_id": current.version_id,
                    "version_number": current.version_number,
                    "snapshot": snapshot,
                }
            elif selected_version_id is not None:
                version = service.get_version(selected_version_id)
            else:
                version = service.active_configuration()
            candidate = build_runtime_candidate(version, current)
            return RuntimeBundle(
                config=candidate.config,
                router=candidate.router,
                backend=candidate.backend,
                route_cache=None,
                version_id=candidate.version_id,
                version_number=candidate.version_number,
            )

        async def run_one(selected_version_id: Optional[int], selected_draft_id: Optional[str]) -> Dict[str, Any]:
            started = time.perf_counter()
            candidate = candidate_for(selected_version_id, selected_draft_id)
            lab_request_id = uuid.uuid4().hex
            emit_routing_event(
                request_id=lab_request_id,
                event_kind="request_started",
                transport="http",
                requested_model="auto",
                traffic_class="admin_test",
                route_policy="route_lab",
                config_version_id=candidate.version_id,
            )
            payload = ChatRequest(model="auto", messages=[Message(role="user", content=text_value)])
            decision = await choose_request_model(payload, [{"role": "user", "content": text_value}], None, candidate)
            outcome = decision.judge_outcome
            emit_routing_event(
                request_id=lab_request_id,
                event_kind="request_completed",
                transport="http",
                requested_model="auto",
                traffic_class="admin_test",
                route_policy="route_lab",
                judge_status=outcome.status if outcome else "not_called",
                selected_model=decision.selected_model,
                final_status="success",
                fallback=outcome.used_default if outcome else False,
                judge_latency_ms=outcome.latency_ms if outcome else None,
                total_latency_ms=(time.perf_counter() - started) * 1000,
                config_version_id=candidate.version_id,
            )
            return {
                "version_id": candidate.version_id,
                "version_number": candidate.version_number,
                "selected_model": decision.selected_model,
                "cache_status": "disabled",
                "judge_status": outcome.status if outcome else "not_called",
                "used_default": outcome.used_default if outcome else False,
                "judge_latency_ms": outcome.latency_ms if outcome else None,
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "traffic_class": "admin_test",
                "persisted": False,
            }

        result = {"result": await run_one(version_id, draft_id)}
        if compare_version_id is not None:
            result["comparison"] = await run_one(compare_version_id, None)
        record_admin_audit("route_lab_evaluate", "success", summary={"comparison": compare_version_id is not None})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @protected_admin_write.delete(
        "/configuration/routes/{draft_id}", include_in_schema=False
    )
    async def delete_configuration_draft(draft_id: str, revision: int):
        configuration_service().delete_draft(
            draft_id, expected_revision=revision
        )
        return JSONResponse(
            {"status": "ok"}, headers={"Cache-Control": "no-store"}
        )

    app.include_router(protected_admin)
    app.include_router(protected_admin_write)

    @app.get("/v1/models")
    async def list_models():
        current = current_runtime()
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "description": llm.description}
                for name, llm in current.config.llms.items()
            ] + [
                {"id": "auto", "object": "model", "description": "Session-aware auto router"},
                {"id": "auto:once", "object": "model", "description": "Judge once per session TTL"},
            ] + [
                {
                    "id": f"auto:{interval}",
                    "object": "model",
                    "description": f"Rejudge every {interval} new user turns",
                }
                for interval in current.config.session_routing.allowed_rejudge_intervals
            ]
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: ChatRequest, http_request: Request):
        bundle = current_runtime()
        request_config = bundle.config
        request_backend = bundle.backend
        request_route_cache = bundle.route_cache
        request_id = uuid.uuid4().hex
        request_started = time.perf_counter()
        decision: Optional[RoutingDecision] = None
        completion_recorded = False
        telemetry_model = telemetry_model_label(payload.model)

        emit_routing_event(
            request_id=request_id,
            event_kind="request_started",
            transport="http",
            requested_model=telemetry_model,
            route_policy=telemetry_model,
        )

        def record_completed(
            final_status: str,
            *,
            error_category: Optional[str] = None,
            first_byte_latency_ms: Optional[float] = None,
            usage: Any = None,
        ) -> None:
            nonlocal completion_recorded
            if completion_recorded:
                return
            completion_recorded = True
            outcome = decision.judge_outcome if decision else None
            usage_counts = _usage_token_counts(usage)
            emit_routing_event(
                request_id=request_id,
                event_kind="request_completed",
                transport="http",
                requested_model=telemetry_model,
                route_policy=telemetry_model,
                cache_status=decision.cache_status if decision else None,
                rejudge_reason=decision.rejudge_reason if decision else None,
                judge_status=outcome.status if outcome else "not_called",
                selected_model=decision.selected_model if decision else None,
                final_status=final_status,
                fallback=outcome.used_default if outcome else False,
                error_category=error_category,
                judge_latency_ms=outcome.latency_ms if outcome else None,
                first_byte_latency_ms=first_byte_latency_ms,
                total_latency_ms=(time.perf_counter() - request_started) * 1000,
                session_hash_prefix=(decision.session_key[:12] if decision and decision.session_key else None),
                **usage_counts,
            )

        def record_judge(outcome: JudgeOutcome) -> None:
            if not outcome.called:
                return
            emit_routing_event(
                request_id=request_id,
                event_kind="judge_completed",
                transport="http",
                requested_model=telemetry_model,
                route_policy=telemetry_model,
                judge_status=outcome.status,
                selected_model=outcome.selected_model,
                fallback=outcome.used_default,
                judge_latency_ms=outcome.latency_ms,
            )

        messages = []
        for message in payload.messages:
            message_payload = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls is not None:
                message_payload["tool_calls"] = message.tool_calls
            if message.tool_call_id is not None:
                message_payload["tool_call_id"] = message.tool_call_id
            if message.function_call is not None:
                message_payload["function_call"] = message.function_call
            messages.append(message_payload)

        # Extract user query for routing (with optional media understanding)
        user_query = ""
        media_description = None

        # Find and process the last user message
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                last_user_idx = i
                break

        if last_user_idx is not None:
            raw_content = messages[last_user_idx]["content"]

            # Process multimodal content if media is enabled
            # Supports both OpenAI format (list) and OpenClaw format (string with [media attached:...])
            if request_config.media.enabled:
                # Use together API key as fallback
                together_key = request_config.api_keys.get("together")
                try:
                    processed_text, media_desc = await process_multimodal_content(
                        raw_content, request_config.media, fallback_key=together_key
                    )
                except Exception as error:
                    record_completed("error", error_category=_error_category(error))
                    raise
                user_query = processed_text[:500]
                media_description = media_desc
                if media_desc:
                    _safe_log("[Media] Processed attached content for routing")
                    # IMPORTANT: Replace the message content with processed text
                    # so LLM sees the image description instead of [media attached: ...]
                    messages[last_user_idx]["content"] = processed_text
            else:
                user_query = normalize_content(raw_content)[:500]

        if not user_query:
            user_query = "general query"

        try:
            decision = await choose_request_model(
                payload,
                messages,
                http_request.headers.get(request_config.session_routing.trusted_session_header),
                bundle,
                judge_observer=record_judge,
            )
        except Exception as error:
            record_completed("error", error_category=_error_category(error))
            raise

        selected_model = decision.selected_model
        session_key = decision.session_key

        # Handle streaming
        if payload.stream:
            async def generate():
                prefix_sent = False
                content_buffer = ""
                buffered_chunks = []
                first_byte_latency_ms: Optional[float] = None
                latest_usage: Any = None
                stream_succeeded = False
                stream_error_category: Optional[str] = None

                def mark_first_byte() -> None:
                    nonlocal first_byte_latency_ms
                    if first_byte_latency_ms is None:
                        first_byte_latency_ms = (time.perf_counter() - request_started) * 1000

                def flush_buffered_prefix() -> Optional[str]:
                    nonlocal prefix_sent, content_buffer, buffered_chunks
                    if not buffered_chunks or prefix_sent:
                        return None

                    content_buffer = re.sub(r'^\[[\w\-\.]+\]\s*', '', content_buffer)
                    first = buffered_chunks[0]
                    try:
                        first_json = first[6:] if first.startswith("data: ") else first
                        first_data = json.loads(first_json.strip())
                        if first_data.get("choices") and first_data["choices"][0].get("delta"):
                            first_data["choices"][0]["delta"]["content"] = f"[{selected_model}] " + content_buffer
                            prefix_sent = True
                            buffered_chunks = []
                            return f"data: {json.dumps(first_data)}\n\n"
                    except:
                        pass
                    return None

                try:
                    prefix_disabled = False

                    stream_gen = await request_backend.call(
                        selected_model, messages, payload.max_tokens,
                        payload.temperature, stream=True,
                        tools=payload.tools,
                        tool_choice=payload.tool_choice,
                        stream_options=payload.stream_options,
                    )
                    async for chunk in stream_gen:
                        chunk_usage = _stream_usage(chunk)
                        if chunk_usage is not None:
                            latest_usage = chunk_usage
                        if not request_config.show_model_prefix:
                            mark_first_byte()
                            yield chunk
                            continue

                        if prefix_disabled:
                            if "[DONE]" in chunk:
                                mark_first_byte()
                                yield chunk
                                continue
                            try:
                                json_str = chunk[6:] if chunk.startswith("data: ") else chunk
                                data = json.loads(json_str.strip())
                                cleaned = clean_streaming_chunk(data)
                                if cleaned:
                                    mark_first_byte()
                                    yield f"data: {json.dumps(cleaned)}\n\n"
                                    continue
                            except:
                                pass
                            mark_first_byte()
                            yield chunk
                            continue

                        # Add model prefix to first content chunk
                        if "[DONE]" in chunk:
                            # Flush buffer before DONE
                            if buffered_chunks and not prefix_sent:
                                flushed_chunk = flush_buffered_prefix()
                                if flushed_chunk:
                                    mark_first_byte()
                                    yield flushed_chunk
                            mark_first_byte()
                            yield chunk
                        else:
                            try:
                                json_str = chunk[6:] if chunk.startswith("data: ") else chunk
                                data = json.loads(json_str.strip())
                                cleaned = clean_streaming_chunk(data)

                                if cleaned:
                                    if cleaned.get("usage") and not cleaned.get("choices"):
                                        if buffered_chunks and not prefix_sent:
                                            flushed_chunk = flush_buffered_prefix()
                                            if flushed_chunk:
                                                mark_first_byte()
                                                yield flushed_chunk
                                        mark_first_byte()
                                        yield f"data: {json.dumps(cleaned)}\n\n"
                                        continue

                                    choices = cleaned.get("choices", [])
                                    if choices and "delta" in choices[0]:
                                        delta = choices[0]["delta"]

                                        if _delta_has_tool_calls(delta):
                                            if buffered_chunks and not prefix_sent:
                                                for buffered_chunk in buffered_chunks:
                                                    try:
                                                        buffered_json = buffered_chunk[6:] if buffered_chunk.startswith("data: ") else buffered_chunk
                                                        buffered_data = json.loads(buffered_json.strip())
                                                        buffered_cleaned = clean_streaming_chunk(buffered_data)
                                                        if buffered_cleaned:
                                                            mark_first_byte()
                                                            yield f"data: {json.dumps(buffered_cleaned)}\n\n"
                                                        else:
                                                            mark_first_byte()
                                                            yield buffered_chunk
                                                    except:
                                                        mark_first_byte()
                                                        yield buffered_chunk
                                            buffered_chunks = []
                                            prefix_disabled = True
                                            mark_first_byte()
                                            yield f"data: {json.dumps(cleaned)}\n\n"
                                            continue

                                        content = delta.get("content", "")

                                        if not prefix_sent:
                                            content_buffer += content
                                            buffered_chunks.append(chunk)

                                            if len(content_buffer) > 30 or (content_buffer and not content_buffer.startswith("[")):
                                                content_buffer = re.sub(r'^\[[\w\-\.]+\]\s*', '', content_buffer)
                                                first = buffered_chunks[0]
                                                first_data = json.loads(first[6:] if first.startswith("data: ") else first)
                                                if first_data.get("choices") and first_data["choices"][0].get("delta"):
                                                    first_data["choices"][0]["delta"]["content"] = f"[{selected_model}] " + content_buffer
                                                    mark_first_byte()
                                                    yield f"data: {json.dumps(first_data)}\n\n"
                                                    prefix_sent = True
                                                    buffered_chunks = []
                                        else:
                                            mark_first_byte()
                                            yield f"data: {json.dumps(cleaned)}\n\n"
                                    else:
                                        if prefix_sent:
                                            mark_first_byte()
                                            yield f"data: {json.dumps(cleaned)}\n\n"
                            except:
                                mark_first_byte()
                                yield chunk
                    stream_succeeded = True
                except Exception as e:
                    stream_error_category = _error_category(e)
                    if request_route_cache and request_config.session_routing.rejudge_on_backend_error:
                        request_route_cache.invalidate(session_key)
                    _safe_log(f"[Stream Error] type={type(e).__name__} session={(session_key or '')[:12]}")
                    mark_first_byte()
                    yield f'data: {json.dumps({"error": str(e)})}\n\n'
                finally:
                    if stream_succeeded:
                        record_completed(
                            "success",
                            first_byte_latency_ms=first_byte_latency_ms,
                            usage=latest_usage,
                        )
                    else:
                        record_completed(
                            "error" if stream_error_category else "disconnected",
                            error_category=stream_error_category or "client_disconnect",
                            first_byte_latency_ms=first_byte_latency_ms,
                            usage=latest_usage,
                        )

            return StreamingResponse(generate(), media_type="text/event-stream")

        else:
            try:
                result = await request_backend.call(
                    selected_model, messages, payload.max_tokens,
                    payload.temperature, stream=False,
                    tools=payload.tools, tool_choice=payload.tool_choice
                )
            except Exception as error:
                if request_route_cache and request_config.session_routing.rejudge_on_backend_error:
                    request_route_cache.invalidate(session_key)
                record_completed("error", error_category=_error_category(error))
                raise

            # Add model prefix
            if request_config.show_model_prefix and result.get("choices"):
                message = result["choices"][0].get("message", {})
                content = message.get("content")
                if content and not _message_has_tool_calls(message):
                    # Remove any existing prefix
                    content = re.sub(r'^\[[\w\-\.]+\]\s*', '', content)
                    message["content"] = f"[{selected_model}] {content}"

            result["model"] = selected_model
            record_completed("success", usage=result.get("usage"))
            return result

    @app.get("/dashboard", include_in_schema=False)
    @app.get("/dashboard/{asset_path:path}", include_in_schema=False)
    async def dashboard(asset_path: str = ""):
        if not control_center.enabled:
            return admin_error(404, "control_center_disabled", "Control Center is disabled")
        index_path = dashboard_dir / "index.html"
        if not index_path.is_file():
            return admin_error(503, "dashboard_unavailable", "Dashboard assets are unavailable")
        requested = (dashboard_dir / asset_path).resolve() if asset_path else index_path
        try:
            requested.relative_to(dashboard_dir)
        except ValueError:
            return admin_error(404, "dashboard_asset_not_found", "Dashboard asset was not found")
        if asset_path.startswith("assets/"):
            if not requested.is_file():
                return admin_error(404, "dashboard_asset_not_found", "Dashboard asset was not found")
            return FileResponse(requested)
        return FileResponse(index_path, media_type="text/html")

    @app.get("/")
    async def root():
        current = current_runtime()
        return {
            "name": "OpenClaw Router",
            "version": "1.0.0",
            "strategy": current.config.router.strategy,
            "llms": list(current.config.llms.keys()),
            "endpoints": {
                "chat": "POST /v1/chat/completions",
                "models": "GET /v1/models",
                "health": "GET /health"
            }
        }

    @app.get("/routers")
    async def list_routers():
        """List available routing strategies"""
        current = current_runtime()
        return {
            "available_routers": current.router.get_available_routers(),
            "current": current.config.router.strategy
        }

    @app.websocket("/v1/chat/ws")
    async def chat_websocket(websocket: WebSocket):
        """WebSocket endpoint for real-time streaming"""
        bundle = current_runtime()
        request_config = bundle.config
        request_backend = bundle.backend
        request_route_cache = bundle.route_cache
        if not _authorized(
            websocket.headers.get("authorization"), request_config.security.inbound_api_key
        ):
            await websocket.close(code=4401, reason="Invalid API key")
            return
        await websocket.accept()
        session_key = None
        decision: Optional[RoutingDecision] = None
        request_id: Optional[str] = None
        request_started: Optional[float] = None
        completion_recorded = False
        latest_usage: Any = None
        first_byte_latency_ms: Optional[float] = None
        payload: Optional[ChatRequest] = None
        telemetry_model = "invalid"

        def mark_ws_first_byte() -> None:
            nonlocal first_byte_latency_ms
            if first_byte_latency_ms is None and request_started is not None:
                first_byte_latency_ms = (time.perf_counter() - request_started) * 1000

        async def send_ws_text(value: str) -> None:
            mark_ws_first_byte()
            await websocket.send_text(value)

        async def send_ws_json(value: Any) -> None:
            mark_ws_first_byte()
            await websocket.send_json(value)

        def record_ws_completed(
            final_status: str,
            *,
            error_category: Optional[str] = None,
        ) -> None:
            nonlocal completion_recorded
            if completion_recorded or request_id is None or request_started is None or payload is None:
                return
            completion_recorded = True
            outcome = decision.judge_outcome if decision else None
            emit_routing_event(
                request_id=request_id,
                event_kind="request_completed",
                transport="websocket",
                requested_model=telemetry_model,
                route_policy=telemetry_model,
                cache_status=decision.cache_status if decision else None,
                rejudge_reason=decision.rejudge_reason if decision else None,
                judge_status=outcome.status if outcome else "not_called",
                selected_model=decision.selected_model if decision else None,
                final_status=final_status,
                fallback=outcome.used_default if outcome else False,
                error_category=error_category,
                judge_latency_ms=outcome.latency_ms if outcome else None,
                first_byte_latency_ms=first_byte_latency_ms,
                total_latency_ms=(time.perf_counter() - request_started) * 1000,
                session_hash_prefix=(decision.session_key[:12] if decision and decision.session_key else None),
                **_usage_token_counts(latest_usage),
            )

        def record_ws_judge(outcome: JudgeOutcome) -> None:
            if not outcome.called or request_id is None or payload is None:
                return
            emit_routing_event(
                request_id=request_id,
                event_kind="judge_completed",
                transport="websocket",
                requested_model=telemetry_model,
                route_policy=telemetry_model,
                judge_status=outcome.status,
                selected_model=outcome.selected_model,
                fallback=outcome.used_default,
                judge_latency_ms=outcome.latency_ms,
            )

        try:
            # Receive request
            data = await websocket.receive_json()
            payload = ChatRequest(**data)
            telemetry_model = telemetry_model_label(payload.model)
            request_id = uuid.uuid4().hex
            request_started = time.perf_counter()
            emit_routing_event(
                request_id=request_id,
                event_kind="request_started",
                transport="websocket",
                requested_model=telemetry_model,
                route_policy=telemetry_model,
            )
            messages = []
            for message in payload.messages:
                item = {"role": message.role, "content": message.content}
                if message.tool_calls is not None:
                    item["tool_calls"] = message.tool_calls
                if message.tool_call_id is not None:
                    item["tool_call_id"] = message.tool_call_id
                if message.function_call is not None:
                    item["function_call"] = message.function_call
                messages.append(item)

            # Extract user query for routing
            user_query = ""
            last_user_idx = None
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    last_user_idx = i
                    break

            if last_user_idx is not None:
                raw_content = messages[last_user_idx]["content"]
                if request_config.media.enabled:
                    together_key = request_config.api_keys.get("together")
                    processed_text, _ = await process_multimodal_content(
                        raw_content, request_config.media, fallback_key=together_key
                    )
                    user_query = processed_text[:500]
                    messages[last_user_idx]["content"] = processed_text
                else:
                    user_query = normalize_content(raw_content)[:500]

            if not user_query:
                user_query = "general query"

            decision = await choose_request_model(
                payload,
                messages,
                websocket.headers.get(request_config.session_routing.trusted_session_header),
                bundle,
                judge_observer=record_ws_judge,
            )
            selected_model = decision.selected_model
            session_key = decision.session_key

            # Call LLM backend in streaming mode
            prefix_sent = False
            content_buffer = ""
            buffered_chunks = []

            stream_gen = await request_backend.call(
                selected_model, messages, payload.max_tokens,
                payload.temperature,
                stream=True,
                tools=payload.tools,
                tool_choice=payload.tool_choice,
                stream_options=payload.stream_options,
            )

            async for chunk in stream_gen:
                chunk_usage = _stream_usage(chunk)
                if chunk_usage is not None:
                    latest_usage = chunk_usage
                if not request_config.show_model_prefix:
                    await send_ws_text(chunk)
                    continue

                if "[DONE]" in chunk:
                    if buffered_chunks and not prefix_sent:
                        content_buffer = re.sub(r'^\[[\w\-\.]+\]\s*', '', content_buffer)
                        first = buffered_chunks[0]
                        try:
                            data_chunk = json.loads(first[6:]) if first.startswith("data: ") else {}
                            if data_chunk.get("choices") and data_chunk["choices"][0].get("delta"):
                                data_chunk["choices"][0]["delta"]["content"] = f"[{selected_model}] " + content_buffer
                                await send_ws_text(f"data: {json.dumps(data_chunk)}\n\n")
                        except:
                            pass
                    await send_ws_text(chunk)
                else:
                    try:
                        json_str = chunk[6:] if chunk.startswith("data: ") else chunk
                        data_chunk = json.loads(json_str.strip())
                        cleaned = clean_streaming_chunk(data_chunk)

                        if cleaned:
                            if cleaned.get("usage") and not cleaned.get("choices"):
                                if buffered_chunks and not prefix_sent:
                                    content_buffer = re.sub(r'^\[[\w\-\.]+\]\s*', '', content_buffer)
                                    first = buffered_chunks[0]
                                    try:
                                        first_data = json.loads(first[6:] if first.startswith("data: ") else first)
                                        if first_data.get("choices") and first_data["choices"][0].get("delta"):
                                            first_data["choices"][0]["delta"]["content"] = f"[{selected_model}] " + content_buffer
                                            await send_ws_text(f"data: {json.dumps(first_data)}\n\n")
                                            prefix_sent = True
                                            buffered_chunks = []
                                    except:
                                        pass
                                await send_ws_json(cleaned)
                                continue

                            choices = cleaned.get("choices", [])
                            if choices and "delta" in choices[0]:
                                content = choices[0]["delta"].get("content", "")

                                if not prefix_sent:
                                    content_buffer += content
                                    buffered_chunks.append(chunk)

                                    if len(content_buffer) > 30 or (content_buffer and not content_buffer.startswith("[")):
                                        content_buffer = re.sub(r'^\[[\w\-\.]+\]\s*', '', content_buffer)
                                        first = buffered_chunks[0]
                                        first_data = json.loads(first[6:] if first.startswith("data: ") else first)
                                        if first_data.get("choices") and first_data["choices"][0].get("delta"):
                                            first_data["choices"][0]["delta"]["content"] = f"[{selected_model}] " + content_buffer
                                            await send_ws_text(f"data: {json.dumps(first_data)}\n\n")
                                            prefix_sent = True
                                            buffered_chunks = []
                                else:
                                    await send_ws_json(cleaned)
                            else:
                                if prefix_sent:
                                    await send_ws_json(cleaned)
                    except:
                        await send_ws_text(chunk)

            record_ws_completed("success")

        except WebSocketDisconnect:
            _safe_log("[WS] Client disconnected")
            record_ws_completed("disconnected", error_category="client_disconnect")
        except Exception as e:
            if request_route_cache and request_config.session_routing.rejudge_on_backend_error:
                request_route_cache.invalidate(session_key)
            _safe_log(f"[WS Error] type={type(e).__name__}")
            record_ws_completed("error", error_category=_error_category(e))
            try:
                await send_ws_json({"error": str(e)})
            except:
                pass
        finally:
            if request_id is not None and not completion_recorded:
                record_ws_completed("disconnected", error_category="client_disconnect")
            try:
                await websocket.close()
            except:
                pass

    return app


def run_server(app: FastAPI = None, config_path: str = None, host: str = "0.0.0.0", port: int = 8000):
    """Run the server"""
    if app is None:
        app = create_app(config_path=config_path)

    print(f"""
============================================================
  OpenClaw Router
============================================================
  Server: http://{host}:{port}
  API:    http://{host}:{port}/v1/chat/completions
  Health: http://{host}:{port}/health
============================================================
""")

    uvicorn.run(app, host=host, port=port)


# ============================================================
# CLI Entry Point
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw Router Server")
    parser.add_argument("--config", "-c", help="Config file path")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", "-p", type=int, default=8000, help="Port to bind")
    args = parser.parse_args()

    run_server(config_path=args.config, host=args.host, port=args.port)
