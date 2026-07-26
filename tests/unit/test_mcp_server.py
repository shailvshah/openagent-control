"""Unit tests for the real MCP ingress transport (ADR-0015).

In-process (no real network socket, no external services) against a fake
Container — same style as test_mcp_route.py's fakes for /mcp/v1 — but driven
through the real MCP SDK client over an in-memory ASGI transport
(`httpx.ASGITransport`), so the actual request/response translation logic in
mcp_server.py runs for real. Verification against a genuinely real stack
(real OPA, real auth server, real downstream MCP server, real gateway
process) lives in
tests/integration/test_mcp_ingress_streamable_http.py — this file is the fast,
dependency-free layer underneath it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.models import Decision, PolicyDecision, ToolCallRequest
from openagent_control.gateway.dependencies import Container
from openagent_control.gateway.mcp_server import build_mcp_asgi_app


class _FixedPolicyEngine:
    """Mirrors the real Rego policy's actual contract (resources/policies/
    mcp_authz.rego): "tools/list" is always allowed regardless of the
    configured decision — only tools/call is subject to it. A fake that
    denies tools/list too doesn't match real policy behavior, and produces a
    confusing failure: Server.call_tool()'s own wrapper calls the registered
    list_tools handler internally (to cache tool definitions for input
    validation) before ever running our call_tool handler, so denying
    tools/list breaks even a plain tools/call test in a way no real
    deployment would."""

    def __init__(self, decision: PolicyDecision) -> None:
        self._decision = decision

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        if request.method == "tools/list":
            return PolicyDecision(decision=Decision.ALLOW)
        return self._decision


class _EchoMCPUpstream:
    """Method-aware: Server.call_tool()'s own wrapper calls the registered
    list_tools handler internally (to cache tool definitions for input-schema
    validation) before ever calling our call_tool handler — so a fake that
    ignores request.method and always returns the same tools/call-shaped
    dict breaks even a plain tools/call test. Verified by hitting exactly
    this while writing this test, not assumed."""

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        if request.method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {"tools": [{"name": "read_query", "inputSchema": {"type": "object"}}]},
            }
        return {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
        }


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def _container(decision: PolicyDecision) -> Container:
    return Container(
        identity_provider=HeaderIdentityProvider(),
        agent_registry=FileAgentRegistry("registry/agents.yaml"),
        policy_engine=_FixedPolicyEngine(decision),
        ledger=Ed25519ChainLedger(),
        audit_exporter=StdoutAuditExporter(),
        token_exchange=StubTokenExchange(),
        mcp_upstream=_EchoMCPUpstream(),
    )


@contextlib.asynccontextmanager
async def _client(
    container: Container, headers: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    asgi_app, session_manager = build_mcp_asgi_app(container)
    async with session_manager.run():
        transport = httpx.ASGITransport(app=asgi_app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://testserver", headers=headers or {}
            ) as http_client,
            streamable_http_client("http://testserver/", http_client=http_client) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session


@pytest.mark.asyncio
async def test_allowed_call_returns_the_upstreams_result() -> None:
    container = _container(PolicyDecision(decision=Decision.ALLOW))

    async with _client(
        container, {"X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot"}
    ) as session:
        result = await session.call_tool("read_query", {})

        assert result.isError is not True
        assert _text(result) == "ok"


@pytest.mark.asyncio
async def test_denied_call_returns_an_in_band_tool_error_not_an_exception() -> None:
    container = _container(PolicyDecision(decision=Decision.DENY, reason="velocity_limit"))

    async with _client(
        container, {"X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot"}
    ) as session:
        result = await session.call_tool("read_query", {})

        assert result.isError is True
        assert "velocity_limit" in _text(result)
        assert "Stop execution" in _text(result)


@pytest.mark.asyncio
async def test_missing_identity_header_is_an_in_band_error_for_call_tool() -> None:
    container = _container(PolicyDecision(decision=Decision.ALLOW))

    async with _client(container) as session:  # no X-Spiffe-ID header at all
        result = await session.call_tool("read_query", {})

        assert result.isError is True
        assert "Identity error" in _text(result)


@pytest.mark.asyncio
async def test_missing_identity_header_raises_for_list_tools() -> None:
    """Unlike call_tool, list_tools has no per-item error shape — an identity
    failure must be a real client-side exception, verified against the SDK's
    actual behavior (not assumed): Server.call_tool()'s wrapper swallows any
    exception into isError=True, but Server.list_tools()'s does not."""
    container = _container(PolicyDecision(decision=Decision.ALLOW))

    async with _client(container) as session:
        with pytest.raises(McpError, match="Identity error"):
            await session.list_tools()


@pytest.mark.asyncio
async def test_orphaned_agent_denial_is_an_in_band_tool_error() -> None:
    """The registry gate (ADR-0008) denies before the policy engine runs at
    all — same in-band CallToolResult(isError=True) treatment as a policy
    DENY, since it's still a failed tool call, not an auth failure."""
    container = _container(PolicyDecision(decision=Decision.ALLOW))

    async with _client(container, {"X-Spiffe-ID": "spiffe://corp.net/ns/x/agent/ghost"}) as session:
        result = await session.call_tool("read_query", {})

        assert result.isError is True
        assert "not registered" in _text(result)
