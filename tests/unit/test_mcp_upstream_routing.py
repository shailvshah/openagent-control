"""RoutingMCPUpstream: one gateway across several downstream MCP servers (ADR-0016).

The fakes here stand in for whole downstream *servers*, not for the transport —
the transport itself is verified against real MCP servers elsewhere
(test_github_mcp_conformance.py, test_mcp_ingress_streamable_http.py). What
needs proving here is the routing decision: which upstream a call goes to, and
what happens when one of them is down.
"""

from __future__ import annotations

from typing import Any

import pytest

from openagent_control.adapters.mcp_upstream.routing import RoutingMCPUpstream
from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import AgentIdentity, ToolCallRequest

AGENT = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")


class FakeUpstream:
    """A downstream MCP server advertising `tools`, recording what it was asked."""

    def __init__(self, tools: list[str], fail: bool = False) -> None:
        self._tools = tools
        self._fail = fail
        self.calls: list[str] = []
        self.closed = False

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        if self._fail:
            raise UpstreamError("connection refused")
        if request.method == "tools/list":
            self.calls.append("tools/list")
            return {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {"tools": [{"name": t, "description": t} for t in self._tools]},
            }
        self.calls.append(request.tool_name or "")
        return {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "result": {"content": [{"type": "text", "text": f"ran {request.tool_name}"}]},
        }

    async def aclose(self) -> None:
        self.closed = True


def _call(tool: str) -> ToolCallRequest:
    return ToolCallRequest(method="tools/call", tool_name=tool, agent=AGENT, request_id="1")


def _list() -> ToolCallRequest:
    return ToolCallRequest(method="tools/list", agent=AGENT, request_id="1")


async def test_list_merges_every_upstreams_catalogue() -> None:
    router = RoutingMCPUpstream(
        {"finance": FakeUpstream(["read_query"]), "crm": FakeUpstream(["update_account"])}
    )

    response = await router.forward(_list(), "cred")

    assert {t["name"] for t in response["result"]["tools"]} == {"read_query", "update_account"}


async def test_call_routes_to_the_upstream_that_advertised_the_tool() -> None:
    finance = FakeUpstream(["read_query"])
    crm = FakeUpstream(["update_account"])
    router = RoutingMCPUpstream({"finance": finance, "crm": crm})

    await router.forward(_call("update_account"), "cred")

    assert "update_account" in crm.calls
    assert "update_account" not in finance.calls


async def test_call_discovers_routes_without_an_explicit_list_first() -> None:
    """A drop-in agent may call a tool it already knows about, never having
    issued tools/list through this gateway — routing must still resolve."""
    crm = FakeUpstream(["update_account"])
    router = RoutingMCPUpstream({"crm": crm})

    response = await router.forward(_call("update_account"), "cred")

    assert response["result"]["content"][0]["text"] == "ran update_account"


async def test_unknown_tool_names_the_upstreams_that_were_searched() -> None:
    router = RoutingMCPUpstream({"finance": FakeUpstream(["read_query"])})

    with pytest.raises(UpstreamError, match="no configured upstream advertises"):
        await router.forward(_call("delete_everything"), "cred")


async def test_one_unreachable_upstream_still_lists_the_reachable_ones() -> None:
    """A CRM outage must not blind the agent to the finance tools it can use."""
    router = RoutingMCPUpstream(
        {"finance": FakeUpstream(["read_query"]), "crm": FakeUpstream([], fail=True)}
    )

    response = await router.forward(_list(), "cred")

    assert [t["name"] for t in response["result"]["tools"]] == ["read_query"]


async def test_every_upstream_failing_is_an_error_not_an_empty_listing() -> None:
    """An empty catalogue and a total outage mean very different things to an
    agent; reporting the outage as "no tools" would be a silent failure."""
    router = RoutingMCPUpstream(
        {"a": FakeUpstream([], fail=True), "b": FakeUpstream([], fail=True)}
    )

    with pytest.raises(UpstreamError, match="every configured upstream failed"):
        await router.forward(_list(), "cred")


async def test_colliding_tool_names_resolve_to_the_first_configured_upstream() -> None:
    primary = FakeUpstream(["read_query"])
    shadow = FakeUpstream(["read_query"])
    router = RoutingMCPUpstream({"primary": primary, "shadow": shadow})

    response = await router.forward(_list(), "cred")
    await router.forward(_call("read_query"), "cred")

    # Listed once, not twice — a duplicate entry would be ambiguous to the agent.
    assert [t["name"] for t in response["result"]["tools"]] == ["read_query"]
    assert "read_query" in primary.calls
    assert "read_query" not in shadow.calls


async def test_routes_are_cached_across_calls() -> None:
    finance = FakeUpstream(["read_query"])
    router = RoutingMCPUpstream({"finance": finance})

    await router.forward(_call("read_query"), "cred")
    await router.forward(_call("read_query"), "cred")

    assert finance.calls.count("tools/list") == 1


async def test_expired_routes_trigger_one_more_fanout() -> None:
    finance = FakeUpstream(["read_query"])
    router = RoutingMCPUpstream({"finance": finance}, cache_ttl_seconds=0.0)

    await router.forward(_call("read_query"), "cred")
    await router.forward(_call("read_query"), "cred")

    assert finance.calls.count("tools/list") == 2


async def test_aclose_closes_every_upstream() -> None:
    finance, crm = FakeUpstream([]), FakeUpstream([])
    await RoutingMCPUpstream({"finance": finance, "crm": crm}).aclose()

    assert finance.closed and crm.closed


def test_an_empty_upstream_map_is_rejected_at_construction() -> None:
    """Silently accepting it would produce a gateway that denies every
    tools/call at runtime with a confusing "no upstream advertises" message."""
    with pytest.raises(ValueError, match="at least one upstream"):
        RoutingMCPUpstream({})
