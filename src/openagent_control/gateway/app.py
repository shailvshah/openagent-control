from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from openagent_control.config import Settings
from openagent_control.diagnostics import run_all
from openagent_control.gateway.dependencies import build_container
from openagent_control.gateway.routes.mcp import router as mcp_router


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.container.aclose()

    app_settings = settings or Settings()
    app = FastAPI(title="OpenAgent-Control Gateway", lifespan=lifespan)
    app.state.container = build_container(app_settings)
    app.include_router(mcp_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: the process is up. Deliberately shallow — a restart
        cannot fix a misconfigured dependency, so those belong in /readyz."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, object]:
        """Readiness: every dependency the gateway needs to serve a real call.

        Returns 503 when any check fails, so an orchestrator stops routing to a
        gateway that would 500 — the same checks `openagent-control doctor`
        runs, so the CLI and the load balancer cannot disagree.
        """
        checks = await run_all(app_settings)
        ready = all(check.ok for check in checks)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if ready else "not ready",
            "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        }

    return app


app = create_app()
