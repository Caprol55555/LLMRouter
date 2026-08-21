"""Read-only query service for Control Center telemetry.

Phase 1 intentionally exposes no HTTP route for these methods.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class TelemetryQueryService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def list_events(
        self,
        *,
        limit: int = 100,
        request_id: Optional[str] = None,
        event_kind: Optional[str] = None,
        traffic_class: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        clauses = []
        values: List[Any] = []
        if request_id:
            clauses.append("request_id = ?")
            values.append(request_id)
        if event_kind:
            clauses.append("event_kind = ?")
            values.append(event_kind)
        if traffic_class:
            clauses.append("traffic_class = ?")
            values.append(traffic_class)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM routing_events"
            + where
            + " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        )
        values.append(bounded_limit)
        return self._rows(sql, values)

    def list_hourly_aggregates(self, *, limit: int = 168) -> List[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 1000))
        return self._rows(
            "SELECT * FROM routing_aggregates_hourly "
            "ORDER BY bucket_start DESC LIMIT ?",
            [bounded_limit],
        )

    def request_page(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        since: Optional[str] = None,
        until: Optional[str] = None,
        traffic_class: Optional[str] = None,
        selected_model: Optional[str] = None,
        final_status: Optional[str] = None,
        config_version_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        bounded_page = max(1, min(int(page), 100000))
        bounded_size = max(1, min(int(page_size), 100))
        clauses = ["event_kind = 'request_completed'"]
        values: List[Any] = []
        for column, value in (
            ("occurred_at >= ?", since),
            ("occurred_at <= ?", until),
            ("traffic_class = ?", traffic_class),
            ("selected_model = ?", selected_model),
            ("final_status = ?", final_status),
        ):
            if value is not None:
                clauses.append(column)
                values.append(value)
        if config_version_id is not None:
            clauses.append("config_version_id = ?")
            values.append(int(config_version_id))
        where = " WHERE " + " AND ".join(clauses)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM routing_events" + where,
                    values,
                ).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM routing_events"
                + where
                + " ORDER BY occurred_at DESC, event_id DESC LIMIT ? OFFSET ?",
                values + [bounded_size, (bounded_page - 1) * bounded_size],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": bounded_page,
            "page_size": bounded_size,
            "total": total,
        }

    def overview(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        windows = {}
        for label, delta in (
            ("1h", timedelta(hours=1)),
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
        ):
            windows[label] = self._window_summary((current - delta).isoformat())
        return {"generated_at": current.isoformat(), "windows": windows}

    def count_events(self, event_kind: Optional[str] = None) -> int:
        with closing(self._connect()) as connection:
            if event_kind:
                row = connection.execute(
                    "SELECT COUNT(*) FROM routing_events WHERE event_kind = ?",
                    (event_kind,),
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) FROM routing_events").fetchone()
            return int(row[0])

    def _rows(self, sql: str, values: List[Any]) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, values).fetchall()]

    def _window_summary(self, since: str) -> Dict[str, Any]:
        where = "event_kind = 'request_completed' AND occurred_at >= ?"
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT COUNT(*) AS request_count, "
                "SUM(CASE WHEN judge_status NOT IN ('not_called', 'no_api_key') THEN 1 ELSE 0 END) AS judge_calls, "
                "SUM(CASE WHEN cache_status = 'hit' THEN 1 ELSE 0 END) AS cache_hits, "
                "SUM(CASE WHEN fallback = 1 THEN 1 ELSE 0 END) AS fallback_count, "
                "SUM(CASE WHEN final_status = 'success' THEN 1 ELSE 0 END) AS success_count, "
                "SUM(CASE WHEN final_status != 'success' THEN 1 ELSE 0 END) AS error_count "
                "FROM routing_events WHERE "
                + where,
                (since,),
            ).fetchone()
            model_rows = connection.execute(
                "SELECT COALESCE(selected_model, '') AS model, COUNT(*) AS count "
                "FROM routing_events WHERE "
                + where
                + " GROUP BY selected_model ORDER BY count DESC, model ASC",
                (since,),
            ).fetchall()
            request_count = int(row["request_count"] or 0)
            judge_calls = int(row["judge_calls"] or 0)
            cache_hits = int(row["cache_hits"] or 0)
            success_count = int(row["success_count"] or 0)
            return {
                "request_count": request_count,
                "judge_calls": judge_calls,
                "judge_amplification": judge_calls / request_count if request_count else 0.0,
                "cache_hit_rate": cache_hits / request_count if request_count else 0.0,
                "success_rate": success_count / request_count if request_count else 0.0,
                "fallback_count": int(row["fallback_count"] or 0),
                "error_count": int(row["error_count"] or 0),
                "total_latency_ms": self._percentiles(
                    connection, since, "total_latency_ms"
                ),
                "judge_latency_ms": self._percentiles(
                    connection, since, "judge_latency_ms"
                ),
                "model_distribution": [dict(item) for item in model_rows],
            }

    @staticmethod
    def _percentiles(connection: sqlite3.Connection, since: str, column: str) -> Dict[str, Any]:
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM routing_events WHERE event_kind = 'request_completed' "
                f"AND occurred_at >= ? AND {column} IS NOT NULL",
                (since,),
            ).fetchone()[0]
        )
        if count == 0:
            return {"p50": None, "p95": None, "sample_count": 0}

        def value_at(percentile: float) -> float:
            offset = max(0, min(count - 1, int((count - 1) * percentile)))
            return float(
                connection.execute(
                    f"SELECT {column} FROM routing_events "
                    f"WHERE event_kind = 'request_completed' AND occurred_at >= ? "
                    f"AND {column} IS NOT NULL ORDER BY {column} LIMIT 1 OFFSET ?",
                    (since, offset),
                ).fetchone()[0]
            )

        return {"p50": value_at(0.50), "p95": value_at(0.95), "sample_count": count}

    def _connect(self) -> sqlite3.Connection:
        database_uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA query_only = ON")
        return connection
