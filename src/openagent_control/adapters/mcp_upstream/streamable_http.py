"""MCP Streamable HTTP upstream adapter, built on the official MCP Python SDK.

`HttpMCPUpstream` POSTs a bare JSON-RPC body, which is *not* the MCP transport.
A real MCP server rejects that with `406 Not Acceptable` (the client must accept
both `application/json` and `text/event-stream`) and then `400 Missing session
ID` (Streamable HTTP requires an `initialize` handshake first). Rather than
re-implement handshake, session management and SSE framing here, this adapter
delegates all of it to `mcp.ClientSession` + `mcp.client.streamable_http` — the
reference implementation maintained alongside the specification.

Credential handling follows the MCP authorization spec (2025-06-18): the
brokered credential is sent as `Authorization: Bearer`, and it is a token the
gateway obtained for the *upstream's own* audience — never the agent's token.
The spec explicitly forbids token passthrough, and audience-scoped brokering
(ADR-0004) is precisely what satisfies that requirement.

Session lifecycle: one MCP session per tool call. That costs an extra
initialize round trip compared with pooling sessions, and is a deliberate v1
tradeoff — a pooled session is stateful, must be rebuilt when the upstream
restarts, and needs its own concurrency control. See ADR-0011.
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import ToolCallRequest


class StreamableHttpMCPUpstream:
    def __init__(self, upstream_url: str, timeout: float = 30.0) -> None:
        self._upstream_url = upstream_url
        self._timeout = timeout

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {credential}"}
        try:
            async with (
                httpx.AsyncClient(headers=headers, timeout=self._timeout) as http_client,
                streamable_http_client(self._upstream_url, http_client=http_client) as streams,
                ClientSession(streams[0], streams[1]) as session,
            ):
                await session.initialize()
                return await self._dispatch(session, request)
        # No `except UpstreamError: raise` here: an UpstreamError raised inside
        # the session is re-wrapped by anyio's task group before it reaches this
        # frame, so that branch would be unreachable. `_describe` unwraps it.
        except Exception as exc:  # noqa: BLE001 — SDK surfaces many error types
            raise UpstreamError(_describe(exc)) from exc

    async def _dispatch(self, session: ClientSession, request: ToolCallRequest) -> dict[str, Any]:
        if request.method == "tools/list":
            listing = await session.list_tools()
            return _jsonrpc_result(request, listing.model_dump(mode="json"))

        result = await session.call_tool(request.tool_name or "", request.arguments or {})
        payload = result.model_dump(mode="json")
        if result.isError:
            raise UpstreamError(_error_text(payload))
        return _jsonrpc_result(request, payload)


def _describe(exc: BaseException) -> str:
    """Renders an SDK failure into something an agent can act on.

    The SDK runs the session in an anyio task group, so failures surface as
    `ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)` — which
    tells the agent (and the on-call engineer) nothing. Unwrap to the real
    causes, since this message is what the denial payload carries back.
    """
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe(inner) for inner in exc.exceptions)
    # Our own error raised inside the session's task group gets wrapped on the
    # way out; don't re-label it with its own class name.
    if isinstance(exc, UpstreamError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _jsonrpc_result(request: ToolCallRequest, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request.request_id, "result": payload}


def _error_text(payload: dict[str, Any]) -> str:
    """Flattens an MCP error result's content blocks into one message."""
    blocks = payload.get("content") or []
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    return "; ".join(t for t in texts if t) or "upstream reported an error"
