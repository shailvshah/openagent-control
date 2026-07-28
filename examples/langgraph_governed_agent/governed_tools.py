"""Governed tool factory: LangChain tools whose every call goes through the gateway.

Each tool proxies a `tools/call` through `openagent_control.sdk.GovernedClient`
(ADR-0017) rather than hand-rolling the JSON-RPC envelope. The gateway attests
identity, evaluates OPA policy, records a signed audit receipt, and only then
forwards to the upstream MCP server. A denial comes back as `ToolCallFailed`,
already carrying the gateway's own agent-readable instruction; returning it as
the tool's *output* (not raising) is what makes the model read "stop and
request approval" and halt, instead of retry-looping on a crashed graph node.

Not `sdk.langchain.proxied_tools()`: that discovers tools from the gateway's
own `tools/list`, which since ADR-0016 is filtered to what this agent is
granted — an ungranted tool never appears in it at all. This demo's whole
point is showing that denial happen in-band, so `salesforce_update_account`
(which invoice-bot is deliberately NOT granted) is wired in by name below,
via the SDK's lower-level `call_tool`, the way you would for a tool you know
about out-of-band rather than one you want to auto-discover.

Authentication: `fetch_agent_token` performs a real OAuth 2.0 client-credentials
grant against the authorization server in the compose stack, which is how a
service principal obtains its access token against Okta or Entra ID too. The
older `X-Spiffe-ID` header path remains available for `OAC_IDENTITY_MODE=header`
deployments, but it is a dev stub (ADR-0005) and is not what `make up` runs.
"""

from __future__ import annotations

from typing import Any

import httpx
from langchain.tools import tool

from openagent_control.sdk import GovernedClient
from openagent_control.sdk.client import ToolCallFailed

DEFAULT_GATEWAY_URL = "http://localhost:8000"
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


def make_governed_tool(oac: GovernedClient, name: str, description: str, **args: type) -> Any:
    """Returns a LangChain tool that executes `name` through the gateway via the SDK.

    `args` is an explicit {name: type} spec, not `**kwargs` on the wrapped
    function. LangChain's `@tool` builds a call's argument schema by
    introspecting the function it wraps, and a bare `**kwargs` function gives
    it nothing to introspect — it then invokes with no arguments at all. This
    project hit the identical bug in `sdk.langchain.proxied_tools()`
    (ADR-0017) and fixed it there by deriving the schema from the MCP tool's
    own `inputSchema`. There is no `inputSchema` to read here: the whole point
    of wiring `salesforce_update_account` in below is reaching a tool this
    agent isn't even granted, so it never appears in `tools/list` at all — the
    caller states the shape explicitly instead.
    """
    from pydantic import create_model

    args_schema = create_model(f"{name}Args", **{arg: (typ, ...) for arg, typ in args.items()})

    def _call(**arguments: Any) -> str:
        try:
            result = oac.call_tool(name, arguments)
        except ToolCallFailed as exc:
            return str(exc)
        return str(result)

    _call.__name__ = name
    return tool(name, description=description, args_schema=args_schema)(_call)


def demo_tools(
    gateway_url: str = DEFAULT_GATEWAY_URL,
    agent_token: str | None = None,
    spiffe_id: str = AGENT_SPIFFE_ID,
) -> list[Any]:
    oac = GovernedClient(
        gateway_url,
        token=agent_token,
        spiffe_id=None if agent_token else spiffe_id,
    )
    return [
        make_governed_tool(
            oac,
            "read_query",
            "Run a read-only query against the finance database.",
            quarter=str,
        ),
        make_governed_tool(
            oac,
            "salesforce_update_account",
            "Update a customer account record in Salesforce.",
            account=str,
            credit_limit=float,
        ),
    ]
