"""End-to-end enterprise scenario -- every component real, nothing stubbed.

    LangGraph agent
        -> HTTP -> openagent-control gateway (uvicorn, real process port)
             -> OIDC/JWKS identity validation      (real RS256 + live JWKS)
             -> Agent Registry lookup              (real registry adapter)
             -> OPA policy evaluation              (real `opa run --server`)
             -> Ed25519 hash-chained receipt       (real signing)
             -> RFC 8693 token exchange            (real authorization server)
        -> HTTP -> MCP server                      (real JSON-RPC + real SQLite)
             -> validates the brokered token's signature, issuer, audience, scope

The only thing standing in for a vendor is that the authorization server runs on
localhost rather than in an Okta org or Entra tenant. Every protocol exchange,
signature, and policy decision is genuine.

Scenarios
  1. Delegated read       -- granted capability, brokered credential, real rows.
  2. Policy denial        -- ungranted capability; never reaches the MCP server.
  3. Gateway bypass       -- the agent calls the MCP server directly and is
                             refused, proving the gateway is load-bearing rather
                             than decorative.
  4. Registry kill-switch -- the agent is suspended in the registry; the next
                             call is denied, with no policy change and no restart.

Run:  poetry install --with examples
      poetry run python -m examples.enterprise_scenario.scenario
Requires the `opa` binary on PATH (brew install opa).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.agent import run_agent
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
    REPO_ROOT,
    build_settings,
    run_gateway,
    run_opa,
    write_registry,
)
from examples.enterprise_scenario.mcp_server import run_mcp_server


def _print_conversation(messages: list[Any]) -> None:
    for message in messages:
        role = message.__class__.__name__
        for call in getattr(message, "tool_calls", []) or []:
            print(f"    [{role}] -> tool_call {call['name']}({call['args']})")
        content = message.content or ""
        if content:
            print(f"    [{role}] {content}")


def _banner(number: int, title: str) -> None:
    print()
    print("=" * 78)
    print(f"{number}. {title}")
    print("=" * 78)


def main() -> None:
    registry_path = REPO_ROOT / "examples" / "enterprise_scenario" / ".scenario-registry.yaml"

    with run_authorization_server(GATEWAY_AUDIENCE) as auth, run_opa() as opa_url:
        with run_mcp_server(auth.issuer + "/keys", auth.issuer) as mcp_url:
            write_registry(registry_path, auth.issuer)
            settings = build_settings(
                auth_discovery_url=auth.discovery_url,
                auth_token_url=auth.token_url,
                opa_url=opa_url,
                mcp_url=mcp_url,
                registry_path=registry_path,
                delegated_audience=mcp.AUDIENCE,
                client_id=GATEWAY_CLIENT_ID,
                client_secret=GATEWAY_CLIENT_SECRET,
            )
            with run_gateway(settings) as gateway_url:
                _run_scenarios(auth, gateway_url, mcp_url, registry_path)

    registry_path.unlink(missing_ok=True)


def _run_scenarios(
    auth: AuthorizationServer, gateway_url: str, mcp_url: str, registry_path: Path
) -> None:
    mcp_endpoint = f"{gateway_url}/mcp/v1"
    agent_token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, HUMAN_SPONSOR)
    sponsor_token = auth.mint_sponsor_token(GATEWAY_AUDIENCE, HUMAN_SPONSOR)
    read_call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
    }

    _banner(1, "Delegated read: granted capability -> brokered credential -> real SQL")
    print(f"    agent   : oidc://{auth.issuer}/{AGENT_CLIENT_ID}")
    print(f"    sponsor : {HUMAN_SPONSOR}")
    _print_conversation(
        run_agent(
            mcp_endpoint,
            agent_token,
            sponsor_token,
            "Summarize the Q3 invoices, then write off INV-1001.",
        )
    )
    print(
        "\n    Those rows came from a real SQLite table, served only after the MCP\n"
        "    server verified a token it had never seen before -- minted by the\n"
        "    authorization server for ITS audience, scoped to invoices:read, and\n"
        "    carrying act.sub = the gateway. The agent never held that token."
    )

    _banner(2, "Policy denial: update_record is not a granted capability")
    print("    (the second tool call above -- BLOCKED before any upstream call)")
    print("    Real OPA evaluated policies/mcp_authz.rego against the registry facts.")

    _banner(3, "Gateway bypass: the agent calls the MCP server directly")
    direct = httpx.post(
        mcp_url, headers={"Authorization": f"Bearer {agent_token}"}, json=read_call, timeout=10.0
    )
    print(f"    HTTP {direct.status_code}  {json.dumps(direct.json())}")
    print(
        "\n    The agent's own token is cryptographically valid -- it is the very one\n"
        "    the gateway just accepted. It is refused here because its audience is\n"
        "    the gateway, not this API. There is no path to the data that skips\n"
        "    governance: the gateway is load-bearing, not decorative."
    )

    _banner(4, "Registry kill-switch: suspend the agent, no policy change, no restart")
    write_registry(registry_path, auth.issuer, status="suspended")
    suspended = httpx.post(
        mcp_endpoint,
        headers={"Authorization": f"Bearer {agent_token}", "X-Subject-Token": sponsor_token},
        json=read_call,
        timeout=10.0,
    )
    print(f"    {json.dumps(suspended.json()['error']['message'])}")

    print()
    print("=" * 78)
    print(
        "Every decision above -- the allow, the policy denial, and the suspension --\n"
        "produced an Ed25519-signed receipt chained to the previous one, printed as\n"
        "audit_receipt log lines by this process."
    )
    print("=" * 78)


if __name__ == "__main__":
    main()
