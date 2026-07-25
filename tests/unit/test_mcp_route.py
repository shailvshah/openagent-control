from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.domain.models import Decision, PolicyDecision, ToolCallRequest
from openagent_control.gateway.app import create_app
from openagent_control.gateway.dependencies import Container


class _FixedPolicyEngine:
    def __init__(self, decision: PolicyDecision) -> None:
        self._decision = decision

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        return self._decision


class _EchoMCPUpstream:
    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "result": "ok",
            "credential": credential,
        }


def _client(decision: PolicyDecision) -> TestClient:
    app = create_app()
    app.state.container = Container(
        identity_provider=HeaderIdentityProvider(),
        policy_engine=_FixedPolicyEngine(decision),
        ledger=Ed25519ChainLedger(),
        audit_exporter=StdoutAuditExporter(),
        token_exchange=StubTokenExchange(),
        mcp_upstream=_EchoMCPUpstream(),
    )
    return TestClient(app, raise_server_exceptions=False)


def test_allowed_call_is_forwarded_upstream() -> None:
    client = _client(PolicyDecision(decision=Decision.ALLOW))

    response = client.post(
        "/mcp/v1",
        headers={"X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_query"}},
    )

    assert response.status_code == 200
    assert response.json()["result"] == "ok"


def test_denied_call_returns_semantic_error_not_forwarded() -> None:
    client = _client(PolicyDecision(decision=Decision.DENY, reason="velocity_limit"))

    response = client.post(
        "/mcp/v1",
        headers={"X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot"},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "read_query"}},
    )

    body = response.json()
    assert body["error"]["message"] == "Policy violation: velocity_limit"
    assert body["error"]["data"]["instruction"] == "Stop execution and request user approval."


def test_missing_identity_header_returns_401_jsonrpc_error() -> None:
    client = _client(PolicyDecision(decision=Decision.ALLOW))

    response = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32001


def test_delegated_call_without_subject_token_returns_401() -> None:
    client = _client(PolicyDecision(decision=Decision.ALLOW))

    response = client.post(
        "/mcp/v1",
        headers={
            "X-Spiffe-ID": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "X-Human-Sponsor": "alice@corp.net",
        },
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_query"}},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == -32003


def test_malformed_json_body_returns_parse_error() -> None:
    client = _client(PolicyDecision(decision=Decision.ALLOW))

    response = client.post(
        "/mcp/v1",
        headers={"Content-Type": "application/json"},
        content="{not json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700


def test_non_object_body_returns_invalid_request() -> None:
    client = _client(PolicyDecision(decision=Decision.ALLOW))

    response = client.post("/mcp/v1", json=[1, 2, 3])

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
