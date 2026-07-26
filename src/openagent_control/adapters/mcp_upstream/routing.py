"""Routes one gateway across several downstream MCP servers. See ADR-0016.

`Settings.mcp_upstream_url` is a single string, so one gateway process could
front exactly one MCP server. A real agent talks to several (a finance server,
a CRM server, an internal tools server), which forced one gateway deployment
per upstream — every one of them a separate registry, policy bundle, and
audit chain for what is logically one fleet.

This adapter implements the same `MCPUpstream` port over N named upstreams:

- **`tools/list`** fans out to every upstream concurrently and returns the
  merged listing, so an agent points at one URL and discovers everything it is
  allowed to reach. The merge doubles as the routing table.
- **`tools/call`** looks the tool up in that table and forwards to the one
  upstream that advertised it.

Two deliberate behaviours worth knowing before deploying it:

**A partly-unreachable fleet still lists.** If some upstreams fail during the
fan-out, the listing returns what the reachable ones advertised rather than
failing the whole call — a CRM outage should not blind an agent to the finance
tools it could still use. Only an all-upstreams-failed fan-out raises. The
trade-off is that a tool can silently vanish from a listing during an outage;
a `tools/call` for it then reports which upstream is unreachable, rather than
"unknown tool".

**Name collisions resolve to the first upstream that advertised them**, in
configured order, and the loser is dropped from the merged listing entirely.
Renaming a colliding tool would be worse: the agent would call a name its
upstream has never heard of. Configured order is therefore load-bearing —
put the authoritative upstream first, and prefer distinct tool names across
servers.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import ToolCallRequest
from openagent_control.domain.ports import MCPUpstream


class RoutingMCPUpstream:
    def __init__(self, upstreams: dict[str, MCPUpstream], cache_ttl_seconds: float = 300.0) -> None:
        if not upstreams:
            raise ValueError("RoutingMCPUpstream requires at least one upstream")
        self._upstreams = upstreams
        self._cache_ttl = cache_ttl_seconds
        self._routes: dict[str, str] = {}
        self._routes_expire_at = 0.0
        # Serialises refreshes so a burst of calls for an unrouted tool produces
        # one fan-out, not one per caller.
        self._lock = asyncio.Lock()

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        if request.method == "tools/list":
            return await self._list(request, credential)
        return await self._call(request, credential)

    async def _list(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        tools, _ = await self._refresh(request, credential)
        return {"jsonrpc": "2.0", "id": request.request_id, "result": {"tools": tools}}

    async def _call(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        tool_name = request.tool_name or ""
        name = await self._route_for(tool_name, request, credential)
        if name is None:
            raise UpstreamError(
                f"no configured upstream advertises a tool named '{tool_name}' "
                f"(searched: {', '.join(self._upstreams)})"
            )
        return await self._upstreams[name].forward(request, credential)

    async def _route_for(
        self, tool_name: str, request: ToolCallRequest, credential: str
    ) -> str | None:
        if time.monotonic() < self._routes_expire_at and tool_name in self._routes:
            return self._routes[tool_name]
        async with self._lock:
            # Another caller may have refreshed while this one waited.
            if time.monotonic() < self._routes_expire_at and tool_name in self._routes:
                return self._routes[tool_name]
            await self._refresh(request, credential)
        return self._routes.get(tool_name)

    async def _refresh(
        self, request: ToolCallRequest, credential: str
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Fans out a tools/list to every upstream and rebuilds the route table."""
        listing = ToolCallRequest(
            method="tools/list",
            agent=request.agent,
            registration=request.registration,
            request_id=request.request_id,
        )
        results = await asyncio.gather(
            *(up.forward(listing, credential) for up in self._upstreams.values()),
            return_exceptions=True,
        )

        merged: list[dict[str, Any]] = []
        routes: dict[str, str] = {}
        failures: list[str] = []
        for name, result in zip(self._upstreams, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"{name}: {result}")
                continue
            for tool in _tools_of(result):
                tool_name = tool.get("name")
                if not isinstance(tool_name, str) or tool_name in routes:
                    continue  # first upstream in configured order wins
                routes[tool_name] = name
                merged.append(tool)

        if failures and len(failures) == len(self._upstreams):
            raise UpstreamError(f"every configured upstream failed — {'; '.join(failures)}")

        self._routes = routes
        self._routes_expire_at = time.monotonic() + self._cache_ttl
        return merged, routes

    async def aclose(self) -> None:
        for upstream in self._upstreams.values():
            close = getattr(upstream, "aclose", None)
            if close is not None:
                await close()


def _tools_of(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Pulls the tool array out of one upstream's tools/list JSON-RPC response.

    Tolerates an upstream answering with a JSON-RPC `error` (or any other
    unexpected shape) by contributing nothing, rather than raising: the
    fan-out's whole point is that one misbehaving server does not take the
    listing down with it.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return []
    tools = result.get("tools")
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, dict)]
