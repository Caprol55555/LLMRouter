"""Control Center status endpoint handler."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from .runtime import ControlCenterRuntime, ControlCenterState


async def admin_api_status(request: Request) -> JSONResponse:
    """Handle GET /admin/api/status.

    - Disabled: 404 with a stable, non-sensitive error code.
    - Healthy: 200 with status, enabled, database, and commit SHA.
    - Degraded: 503 with degraded status and unavailable database.

    All responses include Cache-Control: no-store.
    """
    runtime: ControlCenterRuntime = request.app.state.control_center

    if not runtime.enabled:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "control_center_disabled", "message": "Control Center is not enabled"}},
            headers={"Cache-Control": "no-store"},
        )

    headers = {"Cache-Control": "no-store"}
    if runtime.state == ControlCenterState.OK:
        return JSONResponse(content=runtime.status_payload(), status_code=200, headers=headers)

    return JSONResponse(
        content=runtime.status_payload(),
        status_code=503,
        headers=headers,
    )
