"""Read-only SQLite health checks for Control Center operations."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List


def integrity_report(db_path: str) -> Dict[str, Any]:
    """Run bounded SQLite consistency checks without mutating the database."""
    database_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True, timeout=5.0)) as connection:
        connection.execute("PRAGMA query_only = ON")
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
        foreign_keys: List[Dict[str, Any]] = [
            {
                "table": row[0],
                "rowid": row[1],
                "parent": row[2],
                "fkid": row[3],
            }
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        schema = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    return {
        "status": "ok" if integrity == ["ok"] and not foreign_keys else "degraded",
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "schema_version": int(schema or 0),
    }
