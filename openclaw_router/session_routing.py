"""Bounded, privacy-preserving sticky routing for OpenAI-compatible sessions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .config import SessionRoutingConfig


@dataclass(frozen=True)
class AutoPolicy:
    model_id: str
    rejudge_every_user_turns: int

    @property
    def signature(self) -> str:
        return f"{self.model_id}:{self.rejudge_every_user_turns}"


@dataclass
class SessionRouteEntry:
    model: str
    judged_user_turns: int
    expires_at: float
    last_seen: float
    policy_signature: str
    modality: str


def parse_auto_policy(model_id: str, config: SessionRoutingConfig) -> Optional[AutoPolicy]:
    normalized = (model_id or "").strip().lower()
    if normalized == "auto":
        return AutoPolicy("auto", config.rejudge_every_user_turns)
    if normalized == "auto:once":
        return AutoPolicy("auto:once", 0)
    if not normalized.startswith("auto:"):
        return None

    raw_interval = normalized.split(":", 1)[1]
    if not raw_interval.isdigit():
        return None
    interval = int(raw_interval)
    if interval not in config.allowed_rejudge_intervals:
        return None
    return AutoPolicy(normalized, interval)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def count_user_turns(messages: List[Dict[str, Any]]) -> int:
    return sum(1 for message in messages if message.get("role") == "user")


def detect_modality(messages: List[Dict[str, Any]]) -> str:
    modalities = {"text"}
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).lower()
            if "image" in item_type:
                modalities.add("image")
            elif "audio" in item_type:
                modalities.add("audio")
            elif "video" in item_type:
                modalities.add("video")
    return "+".join(sorted(modalities))


def derive_session_key(
    messages: List[Dict[str, Any]],
    *,
    user: Optional[str],
    header_value: Optional[str],
    fallback_hash_chars: int,
) -> str:
    if header_value and header_value.strip():
        source = f"header:{header_value.strip()}"
    elif user and user.strip():
        source = f"user:{user.strip()}"
    else:
        stable_messages = []
        first_user_added = False
        for message in messages:
            role = str(message.get("role", ""))
            if role == "system" or (role == "user" and not first_user_added):
                stable_messages.append(
                    {"role": role, "content": _content_text(message.get("content"))}
                )
                if role == "user":
                    first_user_added = True
            if first_user_added and role != "system":
                break
        source = "fallback:" + json.dumps(
            stable_messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )[:fallback_hash_chars]
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class SessionRouteCache:
    def __init__(self, config: SessionRoutingConfig, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._clock = clock
        self._entries: "OrderedDict[str, SessionRouteEntry]" = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_key: str) -> asyncio.Lock:
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    def _can_reuse(
        self,
        entry: SessionRouteEntry,
        *,
        now: float,
        user_turns: int,
        policy: AutoPolicy,
        modality: str,
        allowed_models: List[str],
    ) -> bool:
        if entry.expires_at <= now or entry.model not in allowed_models:
            return False
        if entry.policy_signature != policy.signature:
            return False
        if self.config.rejudge_on_modality_change and entry.modality != modality:
            return False
        interval = policy.rejudge_every_user_turns
        if interval > 0 and user_turns - entry.judged_user_turns >= interval:
            return False
        return True

    async def get_or_select(
        self,
        session_key: str,
        *,
        user_turns: int,
        policy: AutoPolicy,
        modality: str,
        allowed_models: List[str],
        selector: Callable[[], Awaitable[str]],
    ) -> Tuple[str, bool]:
        async with self._lock_for(session_key):
            now = self._clock()
            entry = self._entries.get(session_key)
            if entry and self._can_reuse(
                entry,
                now=now,
                user_turns=user_turns,
                policy=policy,
                modality=modality,
                allowed_models=allowed_models,
            ):
                entry.last_seen = now
                self._entries.move_to_end(session_key)
                return entry.model, True

            selected = await selector()
            self._entries[session_key] = SessionRouteEntry(
                model=selected,
                judged_user_turns=user_turns,
                expires_at=now + self.config.ttl_seconds,
                last_seen=now,
                policy_signature=policy.signature,
                modality=modality,
            )
            self._entries.move_to_end(session_key)
            while len(self._entries) > self.config.max_entries:
                old_key, _ = self._entries.popitem(last=False)
                self._locks.pop(old_key, None)
            return selected, False

    def invalidate(self, session_key: Optional[str]) -> None:
        if not session_key:
            return
        self._entries.pop(session_key, None)

    @property
    def size(self) -> int:
        return len(self._entries)
