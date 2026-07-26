"""Governed tool factory: LangChain tools whose every call goes through the gateway.

Each tool POSTs a JSON-RPC tools/call to openagent-control's /mcp/v1 endpoint.
The gateway attests identity, evaluates OPA policy, records a signed audit
receipt, and only then forwards to the upstream MCP server. Policy denials come
back as semantic error payloads which we surface to the model as tool output --
so the agent reads "stop and request approval" instead of retry-looping on a 403.

Authentication: `fetch_agent_token` performs a real OAuth 2.0 client-credentials
grant against the authorization server in the compose stack, which is how a
service principal obtains its access token against Okta or Entra ID too. The
older `X-Spiffe-ID` header path remains available for `OAC_IDENTITY_MODE=header`
deployments, but it is a dev stub (ADR-0005) and is not what `make up` runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from langchain.tools import tool

DEFAULT_GATEWAY_URL = "http://localhost:8000/mcp/v1"
DEFAULT_TOKEN_URL = "http://localhost:8090/oauth2/v1/token"
AGENT_SPIFFE_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


def fetch_agent_token(
    token_url: str = DEFAULT_TOKEN_URL,
    client_id: str = "finance-invoice-svc",
    client_secret: str = "scenario-only-not-a-real-secret",
    audience: str = "api://openagent-control-gateway",
) -> str:
    """Obtains the agent's own access token via the client-credentials grant."""
    response = httpx.post(
        token_url,
        data={"grant_type": "client_credentials", "audience": audience},
        auth=(client_id, client_secret),
        timeout=10.0,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def make_governed_tool(
    name: str,
    description: str,
    gateway_url: str = DEFAULT_GATEWAY_URL,
    spiffe_id: str = AGENT_SPIFFE_ID,
    agent_token: str | None = None,
) -> Callable[..., Any]:
    """Returns a LangChain tool that executes `name` through the governance gateway."""

    headers = (
        {"Authorization": f"Bearer {agent_token}"} if agent_token else {"X-Spiffe-ID": spiffe_id}
    )

    def _call_gateway(**arguments: Any) -> str:
        response = httpx.post(
            gateway_url,
            headers=headers,
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


def demo_tools(
    gateway_url: str = DEFAULT_GATEWAY_URL, agent_token: str | None = None
) -> list[Any]:
    return [
        make_governed_tool(
            "read_query",
            "Run a read-only query against the finance database.",
            gateway_url,
            agent_token=agent_token,
        ),
        make_governed_tool(
            "salesforce_update_account",
            "Update a customer account record in Salesforce.",
            gateway_url,
            agent_token=agent_token,
        ),
    ]
