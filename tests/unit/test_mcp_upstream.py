from __future__ import annotations

import httpx
import pytest

from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import AgentIdentity, ToolCallRequest


def _call() -> ToolCallRequest:
    return ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={"table": "invoices"},
        agent=AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot"),
        request_id=1,
    )


@pytest.mark.asyncio
async def test_forward_sends_jsonrpc_payload_with_bearer_credential() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = HttpMCPUpstream(upstream_url="http://upstream.test", client=client)

    result = await upstream.forward(_call(), credential="ephemeral-token")

    assert result["result"] == "ok"
    assert seen["auth"] == "Bearer ephemeral-token"


@pytest.mark.asyncio
async def test_upstream_http_failure_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    upstream = HttpMCPUpstream(upstream_url="http://upstream.test", client=client)

    with pytest.raises(UpstreamError):
        await upstream.forward(_call(), credential="ephemeral-token")


@pytest.mark.asyncio
async def test_aclose_releases_client() -> None:
    upstream = HttpMCPUpstream(upstream_url="http://upstream.test")

    await upstream.aclose()
