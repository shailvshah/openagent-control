from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, status

from openagent_control.config import Settings
from openagent_control.diagnostics import run_all
from openagent_control.gateway.dependencies import build_container
from openagent_control.gateway.mcp_server import build_mcp_asgi_app
from openagent_control.gateway.routes.mcp import router as mcp_router


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    container = build_container(app_settings)
    mcp_asgi_app, mcp_session_manager = build_mcp_asgi_app(container)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_session_manager.run():
            yield
        await app.state.container.aclose()

    app = FastAPI(title="OpenAgent-Control Gateway", lifespan=lifespan)
    app.state.container = container
    # /mcp/v1 (raw JSON-RPC over HTTPS, not real MCP transport) must be
    # registered before the /mcp mount below — Starlette resolves routes in
    # registration order, so the exact-path route here always wins over the
    # mount for that one path. See docs/adr/0015.
    app.include_router(mcp_router)
    app.mount("/mcp", mcp_asgi_app)

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
