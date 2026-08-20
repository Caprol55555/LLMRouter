"""Read-only query service for Control Center telemetry.

Phase 1 intentionally exposes no HTTP route for these methods.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
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
