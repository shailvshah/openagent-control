"""Unit tests for the MCP Streamable HTTP upstream adapter.

Runs against a real MCP server built with the official SDK rather than a mock
transport: the whole reason this adapter exists is that the previous one only
worked against something that wasn't an MCP server, and a hand-written fake
would reproduce exactly that mistake.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

from openagent_control.adapters.mcp_upstream.streamable_http import StreamableHttpMCPUpstream
from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import AgentIdentity, ToolCallRequest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    port = _free_port()
    server: FastMCP = FastMCP("test-upstream", host="127.0.0.1", port=port)

    @server.tool()
    def read_query(quarter: str) -> dict[str, Any]:
        """Read invoice rows for a quarter."""
        return {"quarter": quarter, "rows": 3}

    @server.tool()
    def always_fails() -> str:
        """Raises, to exercise the error path."""
        raise ValueError("upstream blew up")

    config = uvicorn.Config(
        server.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"
    )
    http = uvicorn.Server(config)
    thread = threading.Thread(target=http.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            if httpx.post(url, timeout=1.0).status_code < 500:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    else:  # pragma: no cover - only on a broken environment
        raise RuntimeError("MCP test server did not start")

    try:
        yield url
    finally:
        http.should_exit = True
        thread.join(timeout=10)


def _request(
    method: str, tool: str | None = None, arguments: dict[str, Any] | None = None
) -> ToolCallRequest:
    return ToolCallRequest(
        method=method,
        tool_name=tool,
        arguments=arguments or {},
        agent=AgentIdentity(spiffe_id="oidc://issuer/agent"),
        registration=None,
        request_id=7,
    )


@pytest.mark.asyncio
async def test_forwards_a_tool_call_over_the_real_transport(mcp_url: str) -> None:
    upstream = StreamableHttpMCPUpstream(mcp_url)

    response = await upstream.forward(_request("tools/call", "read_query", {"quarter": "Q3"}), "t")

    assert response["id"] == 7
    assert response["result"]["structuredContent"] == {"quarter": "Q3", "rows": 3}


@pytest.mark.asyncio
async def test_lists_tools(mcp_url: str) -> None:
    upstream = StreamableHttpMCPUpstream(mcp_url)

    response = await upstream.forward(_request("tools/list"), "t")

    names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"read_query", "always_fails"} <= names


@pytest.mark.asyncio
async def test_upstream_tool_error_surfaces_a_readable_message(mcp_url: str) -> None:
    """The SDK reports failures inside an anyio task group; an agent must get
    the actual cause, not 'unhandled errors in a TaskGroup'."""
    upstream = StreamableHttpMCPUpstream(mcp_url)

    with pytest.raises(UpstreamError) as exc_info:
        await upstream.forward(_request("tools/call", "always_fails"), "t")

    assert "TaskGroup" not in str(exc_info.value)
    assert "upstream blew up" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unreachable_upstream_raises_upstream_error() -> None:
    upstream = StreamableHttpMCPUpstream(f"http://127.0.0.1:{_free_port()}/mcp", timeout=2.0)

    with pytest.raises(UpstreamError):
        await upstream.forward(_request("tools/call", "read_query", {"quarter": "Q3"}), "t")
