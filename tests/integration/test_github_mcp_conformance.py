"""Conformance tests against GitHub's production MCP server.

Keycloak proves the identity adapters interoperate with an IdP we did not
write (see test_keycloak_conformance.py). This does the same one layer up, for
the MCP transport itself: `https://api.githubcopilot.com/mcp/` is a real,
third-party, production MCP server with OAuth. It cannot share our bugs, and it
is what caught the fact that the original upstream adapter did not speak MCP at
all — a real server answers a bare JSON-RPC POST with `406 Not Acceptable`.

Opt-in and read-only. Set a GitHub token with at least `read:user`:

    OAC_TEST_GITHUB_TOKEN=$(gh auth token) poetry run pytest \\
        tests/integration/test_github_mcp_conformance.py

These tests never call a mutating GitHub tool. They send a real credential to
a real external service, so they are skipped unless explicitly enabled.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from openagent_control.adapters.mcp_upstream.streamable_http import StreamableHttpMCPUpstream
from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import AgentIdentity, ToolCallRequest

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
TOKEN = os.environ.get("OAC_TEST_GITHUB_TOKEN", "")

pytestmark = pytest.mark.skipif(
    not TOKEN, reason="set OAC_TEST_GITHUB_TOKEN (read-only) to run GitHub MCP conformance tests"
)


def _request(method: str, tool: str | None = None) -> ToolCallRequest:
    return ToolCallRequest(
        method=method,
        tool_name=tool,
        arguments={},
        agent=AgentIdentity(spiffe_id="oidc://corp.net/finance-invoice-svc"),
        registration=None,
        request_id=1,
    )


def test_github_advertises_rfc9728_protected_resource_metadata() -> None:
    """The MCP auth spec requires a 401 to carry a WWW-Authenticate pointing at
    the resource metadata. This is the discovery path a compliant client walks."""
    unauthenticated = httpx.post(
        GITHUB_MCP_URL, headers={"Accept": "application/json, text/event-stream"}, timeout=30.0
    )

    assert unauthenticated.status_code == 401
    challenge = unauthenticated.headers["www-authenticate"]
    assert "resource_metadata=" in challenge

    metadata_url = challenge.split('resource_metadata="')[1].split('"')[0]
    metadata = httpx.get(metadata_url, timeout=30.0).json()
    assert metadata["resource"] == GITHUB_MCP_URL
    assert metadata["authorization_servers"]


@pytest.mark.asyncio
async def test_lists_tools_from_githubs_production_mcp_server() -> None:
    upstream = StreamableHttpMCPUpstream(GITHUB_MCP_URL, timeout=60.0)

    response = await upstream.forward(_request("tools/list"), TOKEN)

    names = {tool["name"] for tool in response["result"]["tools"]}
    # A stable, long-standing read-only tool on this server.
    assert "get_me" in names


@pytest.mark.asyncio
async def test_calls_a_read_only_tool_on_githubs_production_mcp_server() -> None:
    upstream = StreamableHttpMCPUpstream(GITHUB_MCP_URL, timeout=60.0)

    response = await upstream.forward(_request("tools/call", "get_me"), TOKEN)

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["login"]


@pytest.mark.asyncio
async def test_github_refuses_an_invalid_credential() -> None:
    upstream = StreamableHttpMCPUpstream(GITHUB_MCP_URL, timeout=60.0)

    with pytest.raises(UpstreamError):
        await upstream.forward(_request("tools/list"), "not-a-valid-github-token")
