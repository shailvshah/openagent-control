"""Integration tests over the full real stack — no fakes, no in-process shortcuts.

Runs a real OPA server, a real OAuth 2.0 authorization server, a real MCP server
backed by real SQLite, and the real gateway under uvicorn, then asserts the
properties the enterprise scenario exists to demonstrate. Without these, the
demo is a script that can rot silently.

Skipped when the `opa` binary is unavailable: the policy engine here is the real
one, and substituting a fake would defeat the purpose of the test.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.authorization_server import (
    AGENT_CLIENT_SECRET,
    GATEWAY_CLIENT_ID,
    GATEWAY_CLIENT_SECRET,
    AuthorizationServer,
    run_authorization_server,
)
from examples.enterprise_scenario.harness import (
    AGENT_CLIENT_ID,
    GATEWAY_AUDIENCE,
    HUMAN_SPONSOR,
    build_settings,
    run_gateway,
    run_opa,
    write_registry,
)
from examples.enterprise_scenario.mcp_server import run_mcp_server

pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)


class Stack:
    def __init__(self, auth: AuthorizationServer, gateway_url: str, mcp_url: str, registry: Path):
        self.auth = auth
        self.gateway_url = f"{gateway_url}/mcp/v1"
        self.mcp_url = mcp_url
        self.registry = registry
        self.agent_token = auth.mint_agent_token(GATEWAY_AUDIENCE, AGENT_CLIENT_ID, HUMAN_SPONSOR)
        self.sponsor_token = auth.mint_sponsor_token(GATEWAY_AUDIENCE, HUMAN_SPONSOR)

    def call(self, tool: str, arguments: dict[str, Any], **kwargs: Any) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {kwargs.get('agent_token', self.agent_token)}",
            "X-Subject-Token": self.sponsor_token,
        }
        return httpx.post(
            self.gateway_url,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            timeout=15.0,
        )


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    registry = tmp_path_factory.mktemp("registry") / "agents.yaml"
    with (
        run_authorization_server(GATEWAY_AUDIENCE) as auth,
        run_opa() as opa_url,
        run_mcp_server(auth.issuer + "/keys", auth.issuer) as mcp_url,
    ):
        write_registry(registry, auth.issuer)
        settings = build_settings(
            auth_discovery_url=auth.discovery_url,
            auth_token_url=auth.token_url,
            opa_url=opa_url,
            mcp_url=mcp_url,
            registry_path=registry,
            delegated_audience=mcp.AUDIENCE,
            client_id=GATEWAY_CLIENT_ID,
            client_secret=GATEWAY_CLIENT_SECRET,
        )
        with run_gateway(settings) as gateway_url:
            yield Stack(auth, gateway_url, mcp_url, registry)


def test_granted_call_returns_real_rows_via_a_brokered_credential(stack: Stack) -> None:
    body = stack.call("read_query", {"quarter": "Q3"}).json()

    result = body["result"]
    assert [row["invoice_id"] for row in result["rows"]] == ["INV-1001", "INV-1002", "INV-1003"]
    # Proves the MCP server served this off the brokered token's own delegation
    # claims, not off anything the agent asserted.
    assert result["_served_for"] == HUMAN_SPONSOR
    assert result["_via_actor"] == GATEWAY_CLIENT_ID


def test_ungranted_capability_is_denied_before_reaching_the_upstream(stack: Stack) -> None:
    body = stack.call("update_record", {"invoice_id": "INV-1001", "status": "written_off"}).json()

    assert "Capability not granted" in body["error"]["message"]
    # The invoice must be untouched: the denial has to stop the call, not just log it.
    rows = stack.call("read_query", {"quarter": "Q3"}).json()["result"]["rows"]
    assert next(r for r in rows if r["invoice_id"] == "INV-1001")["status"] == "open"


def test_bypassing_the_gateway_is_refused_by_the_upstream(stack: Stack) -> None:
    """The gateway must be load-bearing: the agent's own valid token is useless here."""
    direct = httpx.post(
        stack.mcp_url,
        headers={"Authorization": f"Bearer {stack.agent_token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
        },
        timeout=10.0,
    )

    assert direct.status_code == 401
    assert "Audience" in direct.json()["error"]["message"]


def test_unauthenticated_upstream_call_is_refused(stack: Stack) -> None:
    direct = httpx.post(
        stack.mcp_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
        },
        timeout=10.0,
    )

    assert direct.status_code == 401


def test_token_exchange_rejects_an_unauthenticated_client(stack: Stack) -> None:
    """Without client authentication the gateway's exchange endpoint is an open
    credential-minting oracle."""
    response = httpx.post(
        stack.auth.token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": stack.sponsor_token,
            "audience": mcp.AUDIENCE,
        },
        timeout=10.0,
    )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"


def test_autonomous_agent_gets_a_real_brokered_credential(stack: Stack) -> None:
    """No human sponsor: the gateway exchanges the agent's own token. The upstream
    validates audience and scope, so a placeholder credential would be refused."""
    token = httpx.post(
        stack.auth.token_url,
        data={"grant_type": "client_credentials"},
        auth=(AGENT_CLIENT_ID, AGENT_CLIENT_SECRET),
        timeout=10.0,
    ).json()["access_token"]

    response = httpx.post(
        stack.gateway_url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
        },
        timeout=15.0,
    )

    result = response.json()["result"]
    assert len(result["rows"]) == 3
    assert result["_served_for"] == AGENT_CLIENT_ID
    assert result["_via_actor"] == GATEWAY_CLIENT_ID


def test_agent_cannot_perform_token_exchange_itself(stack: Stack) -> None:
    """If the agent could exchange tokens it would mint its own downstream
    credentials, defeating the control plane entirely."""
    response = httpx.post(
        stack.auth.token_url,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": stack.sponsor_token,
            "audience": mcp.AUDIENCE,
        },
        auth=(AGENT_CLIENT_ID, AGENT_CLIENT_SECRET),
        timeout=10.0,
    )

    assert response.status_code == 403
    assert response.json()["error"] == "unauthorized_client"


def test_suspending_the_agent_takes_effect_without_a_restart(stack: Stack) -> None:
    write_registry(stack.registry, stack.auth.issuer, status="suspended")
    try:
        body = stack.call("read_query", {"quarter": "Q3"}).json()
        assert "suspended" in body["error"]["message"]
    finally:
        write_registry(stack.registry, stack.auth.issuer, status="active")
