"""Privacy-safe, failure-isolated routing telemetry.

The inference path only performs a non-blocking enqueue. A single background
writer owns SQLite writes, batches events, updates hourly aggregates, and
enforces bounded retention. Telemetry failure must never fail a user request.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..config import ControlCenterConfig
from .migrations import Database

logger = logging.getLogger(__name__)

EVENT_KINDS = frozenset({"request_started", "judge_completed", "request_completed"})
TRAFFIC_CLASSES = frozenset({"production", "admin_test", "deployment_smoke"})
TRANSPORTS = frozenset({"http", "websocket"})
SAFE_TEXT_LIMIT = 128


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _bounded_label(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip()[:SAFE_TEXT_LIMIT]


@dataclass(frozen=True)
class RoutingEvent:
    """A structured event with an intentionally narrow, non-content schema."""

    event_id: str
    request_id: str
    occurred_at: str
    event_kind: str
    traffic_class: str
    transport: str
    requested_model: str
    route_policy: Optional[str] = None
    cache_status: Optional[str] = None
    rejudge_reason: Optional[str] = None
    judge_status: Optional[str] = None
    selected_model: Optional[str] = None
    final_status: Optional[str] = None
    fallback: bool = False
    error_category: Optional[str] = None
    judge_latency_ms: Optional[float] = None
    first_byte_latency_ms: Optional[float] = None
    total_latency_ms: Optional[float] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    config_version_id: Optional[int] = None
    session_hash_prefix: Optional[str] = None

    def __post_init__(self) -> None:
        if self.event_kind not in EVENT_KINDS:
            raise ValueError("invalid routing event kind")
        if self.traffic_class not in TRAFFIC_CLASSES:
            raise ValueError("invalid routing traffic class")
        if self.transport not in TRANSPORTS:
            raise ValueError("invalid routing transport")
        if not self.event_id or not self.request_id or not self.requested_model:
            raise ValueError("routing event identifiers must not be empty")

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        event_kind: str,
        traffic_class: str,
        transport: str,
        requested_model: str,
        occurred_at: Optional[datetime] = None,
        **values: Any,
    ) -> "RoutingEvent":
        safe_values: Dict[str, Any] = {}
        text_fields = {
            "route_policy",
            "cache_status",
            "rejudge_reason",
            "judge_status",
            "selected_model",
            "final_status",
            "error_category",
            "session_hash_prefix",
        }
        allowed = {item.name for item in fields(cls)}
        for key, value in values.items():
            if key not in allowed:
                raise TypeError(f"unsupported routing event field: {key}")
            safe_values[key] = _bounded_label(value) if key in text_fields else value
        return cls(
            event_id=uuid.uuid4().hex,
            request_id=request_id,
            occurred_at=_utc_text(occurred_at or utc_now()),
            event_kind=event_kind,
            traffic_class=traffic_class,
            transport=transport,
            requested_model=_bounded_label(requested_model) or "unknown",
            **safe_values,
        )

    def sqlite_row(self) -> tuple:
        return (
            self.event_id,
            self.request_id,
            self.occurred_at,
            self.event_kind,
            self.traffic_class,
            self.transport,
            self.requested_model,
            self.route_policy,
            self.cache_status,
            self.rejudge_reason,
            self.judge_status,
            self.selected_model,
            self.final_status,
            int(self.fallback),
            self.error_category,
            self.judge_latency_ms,
            self.first_byte_latency_ms,
            self.total_latency_ms,
            self.prompt_tokens,
            self.completion_tokens,
            self.total_tokens,
            self.config_version_id,
            self.session_hash_prefix,
        )


@dataclass(frozen=True)
class TelemetrySnapshot:
    dropped_events: int
    database_errors: int
    written_events: int
    queue_depth: int
    writer_alive: bool
    last_error_category: Optional[str]


_STOP = object()


class TelemetryService:
    """Bounded asynchronous telemetry pipeline with a single SQLite writer."""

    INSERT_SQL = """
        INSERT INTO routing_events (
            event_id, request_id, occurred_at, event_kind, traffic_class,
            transport, requested_model, route_policy, cache_status,
            rejudge_reason, judge_status, selected_model, final_status,
            fallback, error_category, judge_latency_ms, first_byte_latency_ms,
            total_latency_ms, prompt_tokens, completion_tokens, total_tokens,
            config_version_id, session_hash_prefix
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    AGGREGATE_SQL = """
        INSERT INTO routing_aggregates_hourly (
            bucket_start, traffic_class, requested_model, selected_model,
            final_status, request_count, judge_call_count, cache_hit_count,
            fallback_count, error_count, total_latency_sum_ms,
            total_latency_sample_count, judge_latency_sum_ms,
            judge_latency_sample_count, prompt_tokens, completion_tokens,
            total_tokens, token_sample_count
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (
            bucket_start, traffic_class, requested_model, selected_model, final_status
        ) DO UPDATE SET
            request_count = request_count + 1,
            judge_call_count = judge_call_count + excluded.judge_call_count,
            cache_hit_count = cache_hit_count + excluded.cache_hit_count,
            fallback_count = fallback_count + excluded.fallback_count,
            error_count = error_count + excluded.error_count,
            total_latency_sum_ms = total_latency_sum_ms + excluded.total_latency_sum_ms,
            total_latency_sample_count = total_latency_sample_count + excluded.total_latency_sample_count,
            judge_latency_sum_ms = judge_latency_sum_ms + excluded.judge_latency_sum_ms,
            judge_latency_sample_count = judge_latency_sample_count + excluded.judge_latency_sample_count,
            prompt_tokens = prompt_tokens + excluded.prompt_tokens,
            completion_tokens = completion_tokens + excluded.completion_tokens,
            total_tokens = total_tokens + excluded.total_tokens,
            token_sample_count = token_sample_count + excluded.token_sample_count
    """

    def __init__(self, config: ControlCenterConfig, db_path: str):
        self.config = config
        self.db_path = db_path
        self._queue: "queue.Queue[object]" = queue.Queue(
            maxsize=config.telemetry_queue_capacity
        )
        self._start_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped_events = 0
        self._database_errors = 0
        self._written_events = 0
        self._last_error_category: Optional[str] = None
        self._last_cleanup: Optional[float] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="llmrouter-telemetry-writer",
                daemon=True,
            )
            self._thread.start()

    def submit(self, event: RoutingEvent) -> bool:
        """Submit without blocking the inference path."""
        try:
            self.start()
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self._increment(dropped=1, error="queue_full")
            return False
        except Exception as exc:  # pragma: no cover - defensive isolation
            self._increment(dropped=1, error=type(exc).__name__)
            return False

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 2.0) -> bool:
        thread = self._thread
        if thread is None:
            return True
        self._stop_event.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def snapshot(self) -> TelemetrySnapshot:
        with self._stats_lock:
            return TelemetrySnapshot(
                dropped_events=self._dropped_events,
                database_errors=self._database_errors,
                written_events=self._written_events,
                queue_depth=self._queue.qsize(),
                writer_alive=bool(self._thread and self._thread.is_alive()),
                last_error_category=self._last_error_category,
            )

    def _increment(
        self,
        *,
        dropped: int = 0,
        database_errors: int = 0,
        written: int = 0,
        error: Optional[str] = None,
    ) -> None:
        with self._stats_lock:
            self._dropped_events += dropped
            self._database_errors += database_errors
            self._written_events += written
            if error:
                self._last_error_category = _bounded_label(error)

    def _writer_loop(self) -> None:
        database: Optional[Database] = None
        connection = None
        try:
            while not self._stop_event.is_set() or not self._queue.empty():
                batch: List[RoutingEvent] = []
                try:
                    item = self._queue.get(timeout=self.config.telemetry_flush_interval_seconds)
                except queue.Empty:
                    continue

                if item is _STOP:
                    self._queue.task_done()
                    if self._stop_event.is_set() and self._queue.empty():
                        break
                    continue
                if isinstance(item, RoutingEvent):
                    batch.append(item)

                while len(batch) < self.config.telemetry_batch_size:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is _STOP:
                        self._queue.task_done()
                        continue
                    if isinstance(item, RoutingEvent):
                        batch.append(item)

                if not batch:
                    continue
                try:
                    if connection is None:
                        database = Database(self.db_path)
                        connection = database.connect()
                    self._write_batch(connection, batch)
                    self._increment(written=len(batch))
                except Exception as exc:
                    self._increment(
                        dropped=len(batch),
                        database_errors=1,
                        error=type(exc).__name__,
                    )
                    logger.error("Telemetry writer failure: %s", type(exc).__name__)
                    if database is not None:
                        database.close()
                    database = None
                    connection = None
                finally:
                    for _ in batch:
                        self._queue.task_done()
        finally:
            if database is not None:
                database.close()

    def _write_batch(self, connection, batch: Iterable[RoutingEvent]) -> None:
        events = list(batch)
        cursor = connection.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        try:
            cursor.executemany(self.INSERT_SQL, [event.sqlite_row() for event in events])
            for event in events:
                if event.event_kind == "request_completed":
                    cursor.execute(self.AGGREGATE_SQL, self._aggregate_row(event))
            now = time.monotonic()
            if self._last_cleanup is None or now - self._last_cleanup >= 3600:
                self._cleanup(cursor)
                self._last_cleanup = now
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise

    def _aggregate_row(self, event: RoutingEvent) -> tuple:
        occurred = datetime.fromisoformat(event.occurred_at).astimezone(timezone.utc)
        bucket = occurred.replace(minute=0, second=0, microsecond=0).isoformat()
        token_sample = int(event.total_tokens is not None)
        return (
            bucket,
            event.traffic_class,
            event.requested_model,
            event.selected_model or "",
            event.final_status or "unknown",
            int(event.judge_status not in (None, "not_called", "no_api_key")),
            int(event.cache_status == "hit"),
            int(event.fallback),
            int((event.final_status or "") != "success"),
            float(event.total_latency_ms or 0.0),
            int(event.total_latency_ms is not None),
            float(event.judge_latency_ms or 0.0),
            int(event.judge_latency_ms is not None),
            int(event.prompt_tokens or 0),
            int(event.completion_tokens or 0),
            int(event.total_tokens or 0),
            token_sample,
        )

    def _cleanup(self, cursor) -> None:
        raw_cutoff = _utc_text(utc_now() - timedelta(days=self.config.telemetry_retention_days))
        aggregate_cutoff = _utc_text(
            utc_now() - timedelta(days=self.config.telemetry_aggregate_retention_days)
        )
        cursor.execute("DELETE FROM routing_events WHERE occurred_at < ?", (raw_cutoff,))
        cursor.execute(
            "DELETE FROM routing_aggregates_hourly WHERE bucket_start < ?",
            (aggregate_cutoff,),
        )
