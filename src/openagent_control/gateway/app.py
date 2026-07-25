from __future__ import annotations

from fastapi import FastAPI

from openagent_control.config import Settings
from openagent_control.gateway.dependencies import build_container
from openagent_control.gateway.routes.mcp import router as mcp_router


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="OpenAgent-Control Gateway")
    app.state.container = build_container(settings or Settings())
    app.include_router(mcp_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
