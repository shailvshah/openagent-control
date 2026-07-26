"""The gateway's real MCP ingress transport (ADR-0015), verified against the
**real MCP SDK client** — the same reference implementation ADR-0011 already
trusts for the outgoing (upstream) side, now proving the mirror-image
direction: a genuine MCP client connecting INTO the gateway.

Reuses the full real stack from test_enterprise_scenario.py (real
authorization server, real OPA, real downstream MCP server, real gateway
under uvicorn) so the ALLOW path proves an actual brokered credential reaching
an actual downstream tool — not just that the transport handshake succeeds.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.authorization_server import (
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    AuthorizationServer,
    run_authorization_server,
)
from examples.enterprise_scenario.harness import (
    AGENT_CLIENT_ID,
    GATEWAY_AUDIENCE,
    HUMAN_SPONSOR,
    build_settings,
    run_gateway,
    run_opa,
    write_registry,
)
from examples.enterprise_scenario.mcp_server import run_mcp_server
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent

from openagent_control.config import Settings


def _text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)


@contextlib.asynccontextmanager
async def connect(url: str, headers: dict[str, str] | None = None) -> AsyncIterator[ClientSession]:
    """A real MCP client session against `url` — real handshake included."""
    async with (
        httpx.AsyncClient(headers=headers or {}, timeout=15.0) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


class Stack:
    def __init__(self, auth: AuthorizationServer, gateway_url: str, registry: Path):
        self.auth = auth
        self.mcp_ingress_url = f"{gateway_url}/mcp/"
        self.registry = registry
        self.agent_token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, HUMAN_SPONSOR)
        self.sponsor_token = auth.mint_sponsor_token(GATEWAY_AUDIENCE, HUMAN_SPONSOR)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.agent_token}",
            "X-Subject-Token": self.sponsor_token,
        }


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    registry = tmp_path_factory.mktemp("registry") / "agents.yaml"
    with (
        run_authorization_server(GATEWAY_AUDIENCE) as auth,
        run_opa() as opa_url,
        run_mcp_server(auth.issuer + "/keys", auth.issuer) as mcp_url,
    ):
        write_registry(registry, auth.issuer)
        settings = build_settings(
            auth_discovery_url=auth.discovery_url,
            auth_token_url=auth.token_url,
            opa_url=opa_url,
            mcp_url=mcp_url,
            registry_path=registry,
            delegated_audience=mcp.AUDIENCE,
            client_id=GATEWAY_CLIENT_ID,
            client_secret=GATEWAY_CLIENT_SECRET,
        )
        with run_gateway(settings) as gateway_url:
            yield Stack(auth, gateway_url, registry)


@pytest.mark.asyncio
async def test_real_mcp_client_handshake_and_granted_call_returns_real_rows(stack: Stack) -> None:
    async with connect(stack.mcp_ingress_url, stack.headers()) as session:
        result = await session.call_tool("read_query", {"quarter": "Q3"})

        assert result.isError is not True
        assert result.structuredContent is not None
        rows = result.structuredContent["rows"]
        assert [r["invoice_id"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]
        # Proves this reached a real downstream server off a real brokered
        # credential's own delegation claims.
        assert result.structuredContent["_served_for"] == HUMAN_SPONSOR
        assert result.structuredContent["_via_actor"] == GATEWAY_CLIENT_ID


@pytest.mark.asyncio
async def test_real_mcp_client_list_tools_shows_only_granted_tools(stack: Stack) -> None:
    """The listing is projected down to the registry's grants (ADR-0016), so a
    drop-in agent never discovers a tool it would only be denied for calling.

    The real upstream advertises `update_record`; this agent's registry record
    grants only `read_query`, and the real MCP client must see just that.
    """
    async with connect(stack.mcp_ingress_url, stack.headers()) as session:
        listing = await session.list_tools()

        assert {t.name for t in listing.tools} == {"read_query"}


@pytest.mark.asyncio
async def test_ungranted_capability_returns_an_in_band_tool_error(stack: Stack) -> None:
    """A policy denial is a failed tool call, not a broken MCP session — the
    real client must see isError=True with the reason, not an exception."""
    async with connect(stack.mcp_ingress_url, stack.headers()) as session:
        result = await session.call_tool(
            "update_record", {"invoice_id": "INV-1001", "status": "written_off"}
        )

        assert result.isError is True
        assert "Capability not granted" in _text(result)


def test_mcp_v1_raw_jsonrpc_path_still_works_alongside_the_new_mount(stack: Stack) -> None:
    """Regression check: mounting the real transport at /mcp must not swallow
    or break the pre-existing raw-JSON-RPC /mcp/v1 path (ADR-0011's
    `raw-jsonrpc` mode, still documented as the non-MCP-transport option)."""
    gateway_url = stack.mcp_ingress_url.removesuffix("/mcp/")
    response = httpx.post(
        f"{gateway_url}/mcp/v1",
        headers=stack.headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
        },
        timeout=15.0,
    )

    assert response.status_code == 200
    rows = response.json()["result"]["structuredContent"]["rows"]
    assert [r["invoice_id"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]


@pytest.mark.asyncio
async def test_no_bearer_token_fails_the_real_clients_list_tools_call() -> None:
    """An identity failure is a session-level protocol error for list_tools —
    it must surface as a real McpError to the client, not a silent empty list.

    Identity is checked before policy evaluation, so no OPA/registry/auth
    server is needed here — a bare gateway with default settings
    (identity_mode="header") is enough to exercise the missing-identity path.
    """
    with run_gateway(Settings()) as gateway_url:
        async with connect(f"{gateway_url}/mcp/") as session:
            with pytest.raises(McpError, match="Identity error"):
                await session.list_tools()
