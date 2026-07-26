"""HTTP MCP upstream adapter — forwards an approved tools/call to a downstream MCP server.

Real target adapters (MCP servers) point this same
implementation at a different `upstream_url`; no new adapter code is needed unless a
target requires a non-standard request shape.
"""

from __future__ import annotations

from typing import Any

import httpx

from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import ToolCallRequest


class HttpMCPUpstream:
    def __init__(self, upstream_url: str, client: httpx.AsyncClient | None = None) -> None:
        self._upstream_url = upstream_url
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "method": request.method,
            "params": {"name": request.tool_name, "arguments": request.arguments},
        }
        headers = {"Authorization": f"Bearer {credential}"}
        try:
            response = await self._client.post(self._upstream_url, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(str(exc)) from exc
        result: dict[str, Any] = response.json()
        return result

    async def aclose(self) -> None:
        await self._client.aclose()
