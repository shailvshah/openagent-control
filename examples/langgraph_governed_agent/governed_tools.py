"""Governed tool factory: LangChain tools whose every call goes through the gateway.

Each tool POSTs a JSON-RPC tools/call to openagent-control's /mcp/v1 endpoint.
The gateway attests identity, evaluates OPA policy, records a signed audit
receipt, and only then forwards to the upstream MCP server. Policy denials come
back as semantic error payloads which we surface to the model as tool output --
so the agent reads "stop and request approval" instead of retry-looping on a 403.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from langchain.tools import tool

DEFAULT_GATEWAY_URL = "http://localhost:8000/mcp/v1"
AGENT_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


def make_governed_tool(
    name: str,
    description: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    spiffe_id: str = AGENT_SPIFFE_ID,
) -> Callable[..., Any]:
    """Returns a LangChain tool that executes `name` through the governance gateway."""

    def _call_gateway(**arguments: Any) -> str:
        response = httpx.post(
            gateway_url,
            headers={"X-Spiffe-ID": spiffe_id},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            timeout=15.0,
        )
        body = response.json()
        if "error" in body:
            error = body["error"]
            instruction = error.get("data", {}).get("instruction", "")
            return f"BLOCKED: {error['message']}. {instruction}"
        return json.dumps(body.get("result", body))

    _call_gateway.__doc__ = description
    return tool(name, description=description)(_call_gateway)


def demo_tools(gateway_url: str = DEFAULT_GATEWAY_URL) -> list[Any]:
    return [
        make_governed_tool(
            "read_query",
            "Run a read-only query against the finance database.",
            gateway_url,
        ),
        make_governed_tool(
            "salesforce_update_account",
            "Update a customer account record in Salesforce.",
            gateway_url,
        ),
    ]
