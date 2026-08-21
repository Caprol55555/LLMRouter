"""Control Center runtime lifecycle and state.

This module may import configuration but must remain independent from the
inference routing modules (`routers.py`, `session_routing.py`, `server.py`).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..config import ControlCenterConfig, OpenClawConfig
from . import migrations
from .auth import AdminAuthService
from .configuration import ConfigurationService
from .queries import TelemetryQueryService
from .telemetry import RoutingEvent, TelemetryService

logger = logging.getLogger(__name__)


class ControlCenterState(str, Enum):
    DISABLED = "disabled"
    OK = "ok"
    DEGRADED = "degraded"


@dataclass
class ControlCenterRuntime:
    """Thin runtime handle for the Control Center control plane."""

    config: ControlCenterConfig
    application_config: Optional[OpenClawConfig] = None
    state: ControlCenterState = ControlCenterState.DISABLED
    schema_version: Optional[int] = None
    last_error: Optional[str] = None
    telemetry: Optional[TelemetryService] = None
    queries: Optional[TelemetryQueryService] = None
    admin_auth: Optional[AdminAuthService] = None
    configuration: Optional[ConfigurationService] = None

    @classmethod
    def disabled(cls) -> "ControlCenterRuntime":
        return cls(config=ControlCenterConfig())

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def initialize(self) -> None:
        """Initialize database and run migrations.

        Failures are captured in the runtime state and logged; they do not raise.
        """
        if not self.config.enabled:
            self.state = ControlCenterState.DISABLED
            return

        # Keep authentication available even when database initialization is
        # degraded, matching the control center's existing fail-open-to-status
        # behavior. A database-backed instance replaces this after migration.
        self.admin_auth = AdminAuthService(
            os.getenv("LLMROUTER_ADMIN_TOKEN"),
            session_ttl_seconds=self.config.admin_session_ttl_seconds,
            login_window_seconds=self.config.admin_login_window_seconds,
            login_max_attempts=self.config.admin_login_max_attempts,
        )
        try:
            self.config.validate()
            self.schema_version = migrations.migrate(self.config.db_path)
            self.admin_auth = AdminAuthService(
                os.getenv("LLMROUTER_ADMIN_TOKEN"),
                db_path=self.config.db_path,
                session_ttl_seconds=self.config.admin_session_ttl_seconds,
                login_window_seconds=self.config.admin_login_window_seconds,
                login_max_attempts=self.config.admin_login_max_attempts,
            )
            self.telemetry = TelemetryService(self.config, self.config.db_path)
            self.queries = TelemetryQueryService(self.config.db_path)
            if self.application_config is not None:
                self.configuration = ConfigurationService(
                    self.application_config,
                    self.config.db_path,
                )
                self.configuration.initialize_baseline()
            self.state = ControlCenterState.OK
            self.last_error = None
        except Exception as exc:  # pragma: no cover - failure isolation path
            telemetry = self.telemetry
            if telemetry is not None:
                try:
                    telemetry.stop(timeout=0.5)
                except Exception:
                    pass
            self.state = ControlCenterState.DEGRADED
            self.schema_version = None
            self.telemetry = None
            self.queries = None
            self.configuration = None
            self.last_error = "Control Center database initialization failed"
            logger.error("Control Center initialization failed: %s", type(exc).__name__)

    def status_payload(self) -> dict:
        """Return a sanitized status payload without paths or secrets."""
        commit_sha = os.getenv("LLMROUTER_COMMIT_SHA", "unknown")
        payload = {
            "status": "ok" if self.state == ControlCenterState.OK else "degraded",
            "enabled": self.enabled,
            "database": {
                "status": "ok" if self.state == ControlCenterState.OK else "unavailable",
                "schema_version": self.schema_version,
            },
            "commit": commit_sha,
        }
        if self.telemetry is not None:
            snapshot = self.telemetry.snapshot()
            payload["telemetry"] = {
                "status": "degraded" if snapshot.database_errors else "ok",
                "dropped_events": snapshot.dropped_events,
                "database_errors": snapshot.database_errors,
                "written_events": snapshot.written_events,
                "queue_depth": snapshot.queue_depth,
                "writer_alive": snapshot.writer_alive,
            }
        payload["admin_auth"] = {
            "configured": bool(self.admin_auth and self.admin_auth.configured),
            "active_sessions": self.admin_auth.active_session_count()
            if self.admin_auth
            else 0,
        }
        return payload

    def record(self, event: RoutingEvent) -> bool:
        if self.state != ControlCenterState.OK or self.telemetry is None:
            return False
        return self.telemetry.submit(event)

    def shutdown(self, timeout: float = 2.0) -> bool:
        if self.telemetry is None:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        flushed = self.telemetry.flush(timeout=max(0.0, deadline - time.monotonic()))
        stopped = self.telemetry.stop(timeout=max(0.0, deadline - time.monotonic()))
        return flushed and stopped
