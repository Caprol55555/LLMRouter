"""Shared fixtures for Control Center tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from openclaw_router.config import ControlCenterConfig
from openclaw_router.control_center.migrations import Database, migrate


@pytest.fixture
def temp_data_dir() -> Path:
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory(prefix="llmrouter_control_center_") as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def enabled_control_center_config(temp_data_dir: Path) -> ControlCenterConfig:
    """Return an enabled Control Center config pointing at a temporary data dir."""
    return ControlCenterConfig(enabled=True, data_dir=str(temp_data_dir))


@pytest.fixture
def disabled_control_center_config() -> ControlCenterConfig:
    """Return the default disabled Control Center config."""
    return ControlCenterConfig()


@pytest.fixture
def migrated_database(temp_data_dir: Path) -> Path:
    """Return a freshly migrated database path and clean up connections."""
    db_path = temp_data_dir / "control-center.db"
    migrate(str(db_path))
    yield db_path
    Database(str(db_path)).close()
