"""Day-1 demo: the gateway authenticating agents via real OIDC access tokens
(the shape Okta and Microsoft Entra ID actually issue) instead of a trusted
header or a SPIFFE JWT-SVID.

Fully offline: a mock IdP (mock_idp.py) serves a discovery document and JWKS
on a local port and mints RS256 tokens; no real Okta org or Entra tenant is
needed to see the identity validation, registry gate, and audit chain work.

Scenario:
1. A token shaped like an Entra ID delegated OBO token (`azp` = the calling
   service's client ID) for an agent that IS registered -> ALLOWED, forwarded.
2. A token shaped like an Okta client-credentials token (`cid` claim) for a
   client ID that is NOT in the Agent Registry -> DENIED as an orphan, but the
   denial is still receipted (ADR-0008's zero-orphaned-agents guarantee).
3. A token with a valid signature but the WRONG audience -> rejected at the
   identity layer itself (never reaches policy), proving the confused-deputy
   check from the enterprise-idp-integration skill actually runs.

Run:  poetry run python -m examples.oidc_identity_demo.demo
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from examples.oidc_identity_demo.mock_idp import run_mock_idp
from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.models import Decision, PolicyDecision, ToolCallRequest
from openagent_control.gateway.app import create_app
from openagent_control.gateway.dependencies import Container

_AUDIENCE = "oac-gateway"
_REGISTERED_CLIENT_ID = "finance-invoice-svc"
_ORPHAN_CLIENT_ID = "shadow-service-principal"


class _RegistryGrantPolicy:
    """Stands in for OPA here: allows exactly what the (already-attached)
    registry record grants -- the same rule policies/mcp_authz.rego encodes,
    kept inline so this demo needs no external OPA process."""

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        granted = request.registration.granted_tools if request.registration else []
        if request.tool_name in granted:
            return PolicyDecision(decision=Decision.ALLOW)
        return PolicyDecision(decision=Decision.DENY, reason="Capability not granted")


class _EchoMCPUpstream:
    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request.request_id, "result": "Q3 invoices: 2 rows"}


def _write_registry(path: Path, issuer: str) -> None:
    path.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "spiffe_id": f"oidc://{issuer}/{_REGISTERED_CLIENT_ID}",
                        "display_name": "Finance Invoice Service",
                        "purpose": "demo",
                        "owner": "alice@corp.net",
                        "risk_tier": "medium",
                        "status": "active",
                        "granted_tools": ["read_query"],
                    }
                ]
            }
        )
    )


def _call(client: TestClient, token: str, tool_name: str) -> dict[str, Any]:
    response = client.post(
        "/mcp/v1",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        },
    )
    body: dict[str, Any] = response.json()
    return {"status": response.status_code, "body": body}


def main() -> None:
    with run_mock_idp() as idp, tempfile.TemporaryDirectory() as tmp:
        registry_path = Path(tmp) / "agents.yaml"
        _write_registry(registry_path, idp.issuer)

        app = create_app()
        app.state.container = Container(
            identity_provider=OidcJwksIdentityProvider(idp.discovery_url, audience=_AUDIENCE),
            agent_registry=FileAgentRegistry(registry_path),
            policy_engine=_RegistryGrantPolicy(),
            ledger=Ed25519ChainLedger(),
            audit_exporter=StdoutAuditExporter(),
            token_exchange=StubTokenExchange(),
            mcp_upstream=_EchoMCPUpstream(),
        )
        client = TestClient(app, raise_server_exceptions=False)

        print("=" * 72)
        print("1. Registered agent, Entra-style delegated token (azp claim) -> ALLOW")
        registered_token = idp.mint_token(_AUDIENCE, {"azp": _REGISTERED_CLIENT_ID})
        print(_call(client, registered_token, "read_query"))

        print()
        print("2. Unregistered client, Okta-style token (cid claim) -> DENY (orphan)")
        orphan_token = idp.mint_token(_AUDIENCE, {"cid": _ORPHAN_CLIENT_ID})
        print(_call(client, orphan_token, "read_query"))

        print()
        print("3. Valid signature, WRONG audience -> rejected at identity layer (401)")
        wrong_audience_token = idp.mint_token("some-other-app", {"azp": _REGISTERED_CLIENT_ID})
        print(_call(client, wrong_audience_token, "read_query"))
        print("=" * 72)
        print(
            "Every decision above -- including the orphan denial -- produced a "
            "signed, hash-chained audit receipt (see the audit_receipt log lines above)."
        )


if __name__ == "__main__":
    main()
