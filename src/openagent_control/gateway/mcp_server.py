"""Real MCP ingress transport for the gateway. See docs/adr/0015.

ADR-0011 fixed the gateway's *outgoing* MCP transport (a bare JSON-RPC POST is
not the MCP transport; any real MCP server rejects it). That fix was never
applied to the *incoming* side: `POST /mcp/v1` (gateway/routes/mcp.py) still
parses a raw JSON body with no handshake, no session, no SSE framing — a real
MCP client (the SDK's own client, LangChain's MCP adapter, Claude, etc.) would
be rejected the same way a real MCP server used to reject this project's old
outgoing adapter.

This module exposes the gateway itself as a real MCP server, delegating the
entire transport to the official SDK (`mcp.server.lowlevel.Server` +
`mcp.server.streamable_http_manager.StreamableHTTPSessionManager`), the same
"delegate to the reference implementation" approach ADR-0011 took for the
outgoing side. `/mcp/v1` is kept, unchanged, for callers that don't need real
MCP semantics.

GovernedExecutionService itself does not change: every call here still goes
through `container.governed_execution.execute(headers, payload)`, exactly as
`gateway/routes/mcp.py` does — this module only translates between the SDK's
typed request/response objects and that same (headers, JSON-RPC dict) shape.

Stateless mode (`StreamableHTTPSessionManager(stateless=True)`): this gateway
already runs multi-replica (ADR-0009, Postgres-backed ledger/registry), and
the SDK's default session-tracking mode would silently introduce a session-
affinity requirement (sticky LB routing) this project has never needed
anywhere else. One session per tool call was already accepted on the
*outgoing* side for the same correctness-first reasoning (ADR-0011);
stateless mode is the ingress-side equivalent trade-off.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError

from openagent_control.domain.errors import IdentityError, MissingSubjectTokenError
from openagent_control.gateway.dependencies import Container

# Matches Starlette/httpx's expected raw-ASGI-callable shape exactly (a plain
# `Callable[[dict, Callable, Callable], Awaitable]` alias fails mypy strict
# against both `app.mount(...)` and `httpx.ASGITransport(app=...)`, which are
# typed against MutableMapping/receive-returns-MutableMapping).
ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ],
    Awaitable[None],
]

_AUTH_ERROR_CODE = -32001
_DELEGATION_ERROR_CODE = -32003


def _extract_headers(server: Server[Any, Any]) -> dict[str, str]:
    """Pulls the real per-call HTTP headers via the SDK's own request
    context — see the module docstring in docs/adr/0015 for how this reaches
    the actual Starlette Request for this specific call, not just the
    session's initial connection."""
    request = server.request_context.request
    if request is None:
        return {}
    return dict(request.headers)


def _payload(method: str, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def build_mcp_asgi_app(container: Container) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    server: Server[Any, Any] = Server("openagent-control-gateway")

    @server.list_tools()  # type: ignore[untyped-decorator, no-untyped-call]
    async def list_tools() -> types.ListToolsResult:
        headers = _extract_headers(server)
        response = await _execute(container, headers, "tools/list", {})
        if "error" in response:
            raise _mcp_error(response["error"])
        return types.ListToolsResult.model_validate(response["result"])

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        headers = _extract_headers(server)
        response = await _execute(
            container, headers, "tools/call", {"name": name, "arguments": arguments}
        )
        if "error" not in response:
            return types.CallToolResult.model_validate(response["result"])

        # Every error case — policy denial, identity failure, credential-
        # broker failure, upstream error — becomes an in-band isError=True
        # result here, never a raised exception. Verified against the real
        # SDK, not assumed: Server.call_tool()'s own wrapper catches *any*
        # exception raised from inside this handler (including McpError) and
        # converts it to CallToolResult(isError=True) regardless — so trying
        # to raise a protocol-level error for e.g. a missing bearer token from
        # inside call_tool is dead code, never observable by a real client.
        # list_tools (below) does not have this behavior — errors raised
        # there do propagate as real client-side exceptions.
        error = response["error"]
        instruction = (error.get("data") or {}).get("instruction", "")
        text = error.get("message", "tool call failed")
        if instruction:
            text = f"{text}\n\n{instruction}"
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=True
        )

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def asgi_app(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
        send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
    ) -> None:
        await session_manager.handle_request(scope, receive, send)

    return asgi_app, session_manager


async def _execute(
    container: Container, headers: dict[str, str], method: str, params: dict[str, Any]
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    payload = _payload(method, request_id, params)
    try:
        return await container.governed_execution.execute(headers, payload)
    except IdentityError as exc:
        return _error_response(request_id, _AUTH_ERROR_CODE, f"Identity error: {exc}")
    except MissingSubjectTokenError as exc:
        return _error_response(request_id, _DELEGATION_ERROR_CODE, f"Delegation error: {exc}")


def _error_response(request_id: str, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _mcp_error(error: dict[str, Any]) -> McpError:
    return McpError(
        types.ErrorData(code=error.get("code", -32000), message=error.get("message", "error"))
    )
