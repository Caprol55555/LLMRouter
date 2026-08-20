"""Control Center runtime lifecycle and state.

This module may import configuration but must remain independent from the
inference routing modules (`routers.py`, `session_routing.py`, `server.py`).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..config import ControlCenterConfig
from . import migrations

logger = logging.getLogger(__name__)


class ControlCenterState(str, Enum):
    DISABLED = "disabled"
    OK = "ok"
    DEGRADED = "degraded"


@dataclass
class ControlCenterRuntime:
    """Thin runtime handle for the Control Center control plane."""

    config: ControlCenterConfig
    state: ControlCenterState = ControlCenterState.DISABLED
    schema_version: Optional[int] = None
    last_error: Optional[str] = None

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

        try:
            self.config.validate()
            self.schema_version = migrations.migrate(self.config.db_path)
            self.state = ControlCenterState.OK
            self.last_error = None
        except Exception as exc:  # pragma: no cover - failure isolation path
            self.state = ControlCenterState.DEGRADED
            self.schema_version = None
            self.last_error = "Control Center database initialization failed"
            logger.error("Control Center initialization failed: %s", type(exc).__name__)

    def status_payload(self) -> dict:
        """Return a sanitized status payload without paths or secrets."""
        commit_sha = os.getenv("LLMROUTER_COMMIT_SHA", "unknown")
        return {
            "status": "ok" if self.state == ControlCenterState.OK else "degraded",
            "enabled": self.enabled,
            "database": {
                "status": "ok" if self.state == ControlCenterState.OK else "unavailable",
                "schema_version": self.schema_version,
            },
            "commit": commit_sha,
        }
