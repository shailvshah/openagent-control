"""One gateway fronting two real MCP servers (ADR-0016), driven by the real
MCP SDK client.

The unit tests for `RoutingMCPUpstream` route between fakes, which proves the
routing decision but not that a merged listing and a routed call survive two
real Streamable HTTP handshakes, two real OAuth resource servers, and one
brokered credential. That is what this file proves: a finance server and a CRM
server, each protected by real token validation, behind one gateway URL.
"""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.authorization_server import (
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    run_authorization_server,
)
from examples.enterprise_scenario.crm_server import run_crm_server
from examples.enterprise_scenario.harness import (
    AGENT_CLIENT_ID,
    GATEWAY_AUDIENCE,
    build_settings,
    run_gateway,
    run_opa,
)
from examples.enterprise_scenario.mcp_server import run_mcp_server
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)


@contextlib.asynccontextmanager
async def connect(url: str, headers: dict[str, str]) -> AsyncIterator[ClientSession]:
    async with (
        httpx.AsyncClient(headers=headers, timeout=15.0) as http_client,
        streamable_http_client(url, http_client=http_client) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


def _write_registry(path: Path, issuer: str) -> None:
    """Grants one tool from each upstream, so a passing listing proves the
    merge really spanned both servers rather than one answering twice."""
    path.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "spiffe_id": f"oidc://{issuer}/{AGENT_CLIENT_ID}",
                        "display_name": "Finance Invoice Service",
                        "purpose": "Read invoices and look up the customer's CRM account.",
                        "owner": "alice@corp.net",
                        "risk_tier": "medium",
                        "status": "active",
                        "granted_tools": ["read_query", "lookup_account"],
                    }
                ]
            }
        )
    )


class Stack:
    def __init__(self, gateway_url: str, agent_token: str) -> None:
        self.mcp_url = f"{gateway_url}/mcp/"
        self.headers = {"Authorization": f"Bearer {agent_token}"}


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    registry = tmp_path_factory.mktemp("registry") / "agents.yaml"
    with (
        run_authorization_server(GATEWAY_AUDIENCE) as auth,
        run_opa() as opa_url,
        run_mcp_server(auth.issuer + "/keys", auth.issuer) as finance_url,
        run_crm_server(auth.issuer + "/keys", auth.issuer) as crm_url,
    ):
        _write_registry(registry, auth.issuer)
        settings = build_settings(
            auth_discovery_url=auth.discovery_url,
            auth_token_url=auth.token_url,
            opa_url=opa_url,
            # Deliberately a dead URL: mcp_upstreams must take precedence, and
            # a passing suite therefore proves routing is what served every
            # call, not a single-upstream fallback that happened to work.
            mcp_url="http://127.0.0.1:1/unused",
            registry_path=registry,
            delegated_audience=mcp.AUDIENCE,
            client_id=GATEWAY_CLIENT_ID,
            client_secret=GATEWAY_CLIENT_SECRET,
        )
        settings.mcp_upstreams = {"finance": finance_url, "crm": crm_url}
        with run_gateway(settings) as gateway_url:
            token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, None)
            yield Stack(gateway_url, token)


@pytest.mark.asyncio
async def test_listing_merges_tools_from_both_real_upstreams(stack: Stack) -> None:
    async with connect(stack.mcp_url, stack.headers) as session:
        listing = await session.list_tools()

        assert {t.name for t in listing.tools} == {"read_query", "lookup_account"}


@pytest.mark.asyncio
async def test_a_call_reaches_the_finance_server(stack: Stack) -> None:
    async with connect(stack.mcp_url, stack.headers) as session:
        result = await session.call_tool("read_query", {"quarter": "Q3"})

        assert result.isError is not True
        assert result.structuredContent is not None
        rows = result.structuredContent["rows"]
        assert [r["invoice_id"] for r in rows] == ["INV-1001", "INV-1002", "INV-1003"]


@pytest.mark.asyncio
async def test_a_call_reaches_the_crm_server_through_the_same_gateway(stack: Stack) -> None:
    """The same agent, same URL, same brokered credential — routed to a
    different real server purely because that server advertised the tool."""
    async with connect(stack.mcp_url, stack.headers) as session:
        result = await session.call_tool("lookup_account", {"customer": "ACME Corp"})

        assert result.isError is not True
        assert result.structuredContent is not None
        assert result.structuredContent["account_id"] == "ACC-1"
        assert result.structuredContent["tier"] == "enterprise"
