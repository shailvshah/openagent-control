"""Conformance tests against a real, independently-implemented IdP (Keycloak).

The scenario's own authorization server is code in this repo, so a bug in it
could be mirrored by a matching bug in the adapters that talk to it, and both
tests would still pass. Keycloak cannot share our bugs. These tests are what
justify claiming the OIDC and RFC 8693 adapters actually interoperate — and
they already caught one real defect (Keycloak's client-credentials tokens carry
a service-account `sub` distinct from the client id, which the identity adapter
originally mistook for a human sponsor).

Opt-in: set OAC_TEST_KEYCLOAK_URL to a realm base URL, e.g.
    http://localhost:8380/realms/oac
The realm must be provisioned as described in
examples/enterprise_scenario/keycloak/README.md.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import httpx
import pytest
from examples.enterprise_scenario import mcp_server as mcp
from examples.enterprise_scenario.harness import run_gateway, run_opa, write_registry

from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.adapters.mcp_upstream.streamable_http import StreamableHttpMCPUpstream
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.config import Settings
from openagent_control.domain.errors import UpstreamError
from openagent_control.domain.models import AgentIdentity, ToolCallRequest

REALM_URL = os.environ.get("OAC_TEST_KEYCLOAK_URL", "")
GATEWAY_CLIENT_ID = "openagent-control-gateway"
GATEWAY_SECRET = "gateway-secret"
AGENT_CLIENT_ID = "finance-invoice-svc"
AGENT_SECRET = "agent-secret"
MCP_AUDIENCE = "finance-mcp-api"

pytestmark = pytest.mark.skipif(
    not REALM_URL, reason="set OAC_TEST_KEYCLOAK_URL to run Keycloak conformance tests"
)

DISCOVERY_URL = f"{REALM_URL}/.well-known/openid-configuration"
TOKEN_URL = f"{REALM_URL}/protocol/openid-connect/token"


def agent_token() -> str:
    response = httpx.post(
        TOKEN_URL,
        auth=(AGENT_CLIENT_ID, AGENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=10.0,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


@pytest.mark.asyncio
async def test_identity_adapter_validates_a_real_keycloak_token() -> None:
    provider = OidcJwksIdentityProvider(DISCOVERY_URL, audience=GATEWAY_CLIENT_ID)

    identity = await provider.identify({"Authorization": f"Bearer {agent_token()}"})

    assert identity.spiffe_id == f"oidc://{REALM_URL}/{AGENT_CLIENT_ID}"
    # Keycloak service accounts have a UUID `sub` that is NOT a human. Treating
    # it as one makes the gateway demand a subject token for a machine call.
    assert identity.human_sponsor is None


@pytest.mark.asyncio
async def test_identity_adapter_rejects_a_token_for_another_audience() -> None:
    provider = OidcJwksIdentityProvider(DISCOVERY_URL, audience="some-other-api")

    with pytest.raises(Exception, match="[Aa]udience"):
        await provider.identify({"Authorization": f"Bearer {agent_token()}"})


@pytest.mark.asyncio
async def test_rfc8693_adapter_performs_a_real_keycloak_token_exchange() -> None:
    exchange = Rfc8693TokenExchange(TOKEN_URL, GATEWAY_CLIENT_ID, GATEWAY_SECRET)
    try:
        brokered = await exchange.exchange(agent_token(), MCP_AUDIENCE)
    finally:
        await exchange.aclose()

    # Validate the brokered token exactly as the MCP resource server would.
    verifier = mcp.JwksTokenVerifier(
        f"{REALM_URL}/protocol/openid-connect/certs", REALM_URL, audience=MCP_AUDIENCE
    )
    verified = await verifier.verify_token(brokered)

    assert verified is not None
    assert verified.claims is not None
    assert verified.claims["aud"] == MCP_AUDIENCE
    assert verified.client_id == GATEWAY_CLIENT_ID
    assert "invoices:read" in verified.scopes
    # The agent's own token must not be usable at the downstream API.
    assert await verifier.verify_token(agent_token()) is None


@pytest.fixture(scope="module")
def keycloak_gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """The real gateway, wired entirely to Keycloak."""
    registry = tmp_path_factory.mktemp("kc-registry") / "agents.yaml"
    certs = f"{REALM_URL}/protocol/openid-connect/certs"
    with (
        run_opa() as opa_url,
        mcp.run_mcp_server(certs, REALM_URL, audience=MCP_AUDIENCE) as mcp_url,
    ):
        write_registry(registry, REALM_URL)
        settings = Settings(
            opa_url=opa_url,
            mcp_upstream_url=mcp_url,
            registry_path=str(registry),
            identity_mode="oidc-jwks",
            oidc_discovery_url=DISCOVERY_URL,
            oidc_audience=GATEWAY_CLIENT_ID,
            token_exchange_mode="rfc8693",
            token_exchange_url=TOKEN_URL,
            token_exchange_client_id=GATEWAY_CLIENT_ID,
            token_exchange_client_secret=GATEWAY_SECRET,
            delegated_audience=MCP_AUDIENCE,
        )
        with run_gateway(settings) as gateway_url:
            yield f"{gateway_url}/mcp/v1", mcp_url


def test_full_stack_against_keycloak(keycloak_gateway: tuple[str, str]) -> None:
    """Agent -> gateway -> Keycloak identity + exchange -> MCP server -> SQL."""
    gateway_url, mcp_url = keycloak_gateway
    token = agent_token()

    response = httpx.post(
        gateway_url,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read_query", "arguments": {"quarter": "Q3"}},
        },
        timeout=20.0,
    )

    # A real MCP CallToolResult, over the real Streamable HTTP transport.
    assert len(response.json()["result"]["structuredContent"]["rows"]) == 3

    # And the same token cannot reach the MCP server directly.
    bypass = ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={"quarter": "Q3"},
        agent=AgentIdentity(spiffe_id="oidc://bypass/attempt"),
        registration=None,
        request_id=1,
    )
    with pytest.raises(UpstreamError, match="401"):
        asyncio.run(StreamableHttpMCPUpstream(mcp_url).forward(bypass, token))
