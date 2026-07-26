from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from openagent_control.config import Settings
from openagent_control.control_plane.dependencies import build_control_plane_container
from openagent_control.control_plane.routes.agents import router as agents_router
from openagent_control.control_plane.routes.fleet import router as fleet_router
from openagent_control.control_plane.routes.receipts import router as receipts_router
from openagent_control.diagnostics import run_all


def create_app(settings: Settings | None = None) -> FastAPI:
    """Unlike gateway/app.py, this module deliberately has no module-level
    `app = create_app()`: construction requires OAC_DATABASE_URL (see
    build_control_plane_container), and a bare module-level call would make
    merely importing this module (e.g. `from ... import create_app`) fail
    whenever the database isn't configured — including in tests that just
    want the function. `cli.py`'s serve-control-plane command instead points
    uvicorn at this function directly via `factory=True`, so construction
    (and its fail-fast behavior) happens at actual server startup, not import."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.container.aclose()

    app_settings = settings or Settings()
    app = FastAPI(title="OpenAgent-Control Control Plane", lifespan=lifespan)
    app.state.container = build_control_plane_container(app_settings)
    app.include_router(agents_router)
    app.include_router(receipts_router)
    app.include_router(fleet_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        """Same diagnostics.run_all() the gateway's /readyz and
        `openagent-control doctor` use — the CLI, the gateway, and the
        control plane cannot disagree about the state of shared dependencies."""
        checks = await run_all(app_settings)
        ready = all(check.ok for check in checks)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not ready",
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        }

    return app
