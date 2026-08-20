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
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncGenerator, Callable, Optional, Dict, Any, List

# Check dependencies
try:
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse, StreamingResponse
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
    control_center = ControlCenterRuntime(config.control_center)
    control_center.initialize()
    app.state.control_center = control_center

    def emit_routing_event(
        *,
        request_id: str,
        event_kind: str,
        transport: str,
        requested_model: str,
        **values: Any,
    ) -> bool:
        if control_center.telemetry is None:
            return False
        try:
            return control_center.record(
                RoutingEvent.create(
                    request_id=request_id,
                    event_kind=event_kind,
                    traffic_class="production",
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
        judge_observer: Optional[Callable[[JudgeOutcome], None]] = None,
    ) -> RoutingDecision:
        available_models = list(config.llms.keys())
        if payload.model in available_models:
            _safe_log(f"[Route] explicit model={payload.model}")
            return RoutingDecision(
                selected_model=payload.model,
                session_key=None,
                cache_status="not_applicable",
                rejudge_reason=None,
                judge_outcome=None,
            )

        policy = parse_auto_policy(payload.model, config.session_routing)
        if policy is None:
            raise HTTPException(status_code=404, detail=f"Model '{payload.model}' not found")

        routing_context = build_routing_context(
            messages, config.router.routing_context_chars
        )
        judge_outcomes: List[JudgeOutcome] = []

        def observe_judge(outcome: JudgeOutcome) -> None:
            judge_outcomes.append(outcome)
            if judge_observer is not None:
                judge_observer(outcome)

        if route_cache is None:
            selected = await router.select_model(
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
            fallback_hash_chars=config.session_routing.fallback_hash_chars,
        )
        user_turns = count_user_turns(messages)
        modality = detect_modality(messages)
        selected, cache_hit, rejudge_reason = await route_cache.get_or_select_detailed(
            session_key,
            user_turns=user_turns,
            policy=policy,
            modality=modality,
            allowed_models=available_models,
            selector=lambda: router.select_model(
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
        return {
            "status": "ok",
            "strategy": config.router.strategy,
            "llms": list(config.llms.keys()),
            "session_cache_entries": route_cache.size if route_cache else 0,
            "commit": os.getenv("LLMROUTER_COMMIT_SHA", "unknown"),
        }

    app.add_api_route("/admin/api/status", admin_api_status, methods=["GET"], include_in_schema=False)

    @app.get("/v1/models")
    async def list_models():
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "description": llm.description}
                for name, llm in config.llms.items()
            ] + [
                {"id": "auto", "object": "model", "description": "Session-aware auto router"},
                {"id": "auto:once", "object": "model", "description": "Judge once per session TTL"},
            ] + [
                {
                    "id": f"auto:{interval}",
                    "object": "model",
                    "description": f"Rejudge every {interval} new user turns",
                }
                for interval in config.session_routing.allowed_rejudge_intervals
            ]
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: ChatRequest, http_request: Request):
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
            if config.media.enabled:
                # Use together API key as fallback
                together_key = config.api_keys.get("together")
                try:
                    processed_text, media_desc = await process_multimodal_content(
                        raw_content, config.media, fallback_key=together_key
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
                http_request.headers.get(config.session_routing.trusted_session_header),
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

                    stream_gen = await backend.call(
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
                        if not config.show_model_prefix:
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
                    if route_cache and config.session_routing.rejudge_on_backend_error:
                        route_cache.invalidate(session_key)
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
                result = await backend.call(
                    selected_model, messages, payload.max_tokens,
                    payload.temperature, stream=False,
                    tools=payload.tools, tool_choice=payload.tool_choice
                )
            except Exception as error:
                if route_cache and config.session_routing.rejudge_on_backend_error:
                    route_cache.invalidate(session_key)
                record_completed("error", error_category=_error_category(error))
                raise

            # Add model prefix
            if config.show_model_prefix and result.get("choices"):
                message = result["choices"][0].get("message", {})
                content = message.get("content")
                if content and not _message_has_tool_calls(message):
                    # Remove any existing prefix
                    content = re.sub(r'^\[[\w\-\.]+\]\s*', '', content)
                    message["content"] = f"[{selected_model}] {content}"

            result["model"] = selected_model
            record_completed("success", usage=result.get("usage"))
            return result

    @app.get("/")
    async def root():
        return {
            "name": "OpenClaw Router",
            "version": "1.0.0",
            "strategy": config.router.strategy,
            "llms": list(config.llms.keys()),
            "endpoints": {
                "chat": "POST /v1/chat/completions",
                "models": "GET /v1/models",
                "health": "GET /health"
            }
        }

    @app.get("/routers")
    async def list_routers():
        """List available routing strategies"""
        return {
            "available_routers": router.get_available_routers(),
            "current": config.router.strategy
        }

    @app.websocket("/v1/chat/ws")
    async def chat_websocket(websocket: WebSocket):
        """WebSocket endpoint for real-time streaming"""
        if not _authorized(
            websocket.headers.get("authorization"), config.security.inbound_api_key
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
                if config.media.enabled:
                    together_key = config.api_keys.get("together")
                    processed_text, _ = await process_multimodal_content(
                        raw_content, config.media, fallback_key=together_key
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
                websocket.headers.get(config.session_routing.trusted_session_header),
                judge_observer=record_ws_judge,
            )
            selected_model = decision.selected_model
            session_key = decision.session_key

            # Call LLM backend in streaming mode
            prefix_sent = False
            content_buffer = ""
            buffered_chunks = []

            stream_gen = await backend.call(
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
                if not config.show_model_prefix:
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
            if route_cache and config.session_routing.rejudge_on_backend_error:
                route_cache.invalidate(session_key)
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
