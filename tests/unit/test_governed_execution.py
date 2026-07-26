"""Unit tests for the transport-agnostic governed-execution use case."""

from __future__ import annotations

from typing import Any

import pytest

from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.application.governed_execution import GovernedExecutionService
from openagent_control.domain.errors import (
    MissingSubjectTokenError,
    PolicyEngineUnavailableError,
    TokenExchangeError,
    UpstreamError,
)
from openagent_control.domain.models import (
    AgentStatus,
    Decision,
    ExecutionReceipt,
    PolicyDecision,
    RegisteredAgent,
    RiskTier,
    ToolCallRequest,
)

_AGENT = "spiffe://corp.net/ns/finance/agent/invoice-bot"
_PAYLOAD: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "read_query", "arguments": {"table": "invoices"}},
}


class FakePolicyEngine:
    def __init__(self, decision: PolicyDecision | None = None, error: Exception | None = None):
        self._decision = decision
        self._error = error

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        if self._error:
            raise self._error
        assert self._decision is not None
        return self._decision


class RecordingExporter:
    def __init__(self) -> None:
        self.receipts: list[ExecutionReceipt] = []

    async def export(self, receipt: ExecutionReceipt) -> None:
        self.receipts.append(receipt)


class FakeUpstream:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.credentials: list[str] = []

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        if self._error:
            raise self._error
        self.credentials.append(credential)
        return {"jsonrpc": "2.0", "id": request.request_id, "result": "ok"}


class FakeTokenExchange:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    async def exchange(self, subject_token: str, audience: str) -> str:
        if self._error:
            raise self._error
        return f"obo::{subject_token}::{audience}"


def _registered(status: AgentStatus = AgentStatus.ACTIVE) -> RegisteredAgent:
    return RegisteredAgent(
        spiffe_id=_AGENT,
        display_name="Invoice Bot",
        purpose="demo",
        owner="alice@corp.net",
        risk_tier=RiskTier.MEDIUM,
        status=status,
        granted_tools=["read_query"],
    )


class FakeRegistry:
    def __init__(self, agents: dict[str, RegisteredAgent] | None = None) -> None:
        self._agents = agents if agents is not None else {_AGENT: _registered()}

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        return self._agents.get(spiffe_id)


def _service(
    policy: FakePolicyEngine,
    upstream: FakeUpstream | None = None,
    exporter: RecordingExporter | None = None,
    registry: FakeRegistry | None = None,
    token_exchange: FakeTokenExchange | None = None,
) -> tuple[GovernedExecutionService, FakeUpstream, RecordingExporter]:
    upstream = upstream or FakeUpstream()
    exporter = exporter or RecordingExporter()
    service = GovernedExecutionService(
        identity_provider=HeaderIdentityProvider(),
        agent_registry=registry or FakeRegistry(),
        policy_engine=policy,
        ledger=Ed25519ChainLedger(),
        audit_exporter=exporter,
        token_exchange=token_exchange or FakeTokenExchange(),
        mcp_upstream=upstream,
        delegated_audience="test-audience",
    )
    return service, upstream, exporter


@pytest.mark.asyncio
async def test_autonomous_call_without_a_bearer_token_falls_back_to_a_placeholder() -> None:
    """Only reachable in identity_mode="header" (ADR-0005), where the caller
    presents no token there would be anything to exchange."""
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW))
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["result"] == "ok"
    assert upstream.credentials == [f"autonomous::{_AGENT}"]
    assert exporter.receipts[0].decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_autonomous_call_exchanges_the_agents_own_token() -> None:
    """A real upstream validates the credential's audience, so an autonomous
    agent must be given a brokered token, never a placeholder string."""
    service, upstream, _ = _service(FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)))

    headers = {"x-spiffe-id": _AGENT, "authorization": "Bearer agent-own-token"}
    result = await service.execute(headers, _PAYLOAD)

    assert result["result"] == "ok"
    assert upstream.credentials == ["obo::agent-own-token::test-audience"]


@pytest.mark.asyncio
async def test_autonomous_call_ignores_a_non_bearer_authorization_scheme() -> None:
    service, upstream, _ = _service(FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)))

    headers = {"x-spiffe-id": _AGENT, "authorization": "Basic dXNlcjpwYXNz"}
    await service.execute(headers, _PAYLOAD)

    assert upstream.credentials == [f"autonomous::{_AGENT}"]


@pytest.mark.asyncio
async def test_delegated_call_exchanges_subject_token() -> None:
    service, upstream, _ = _service(FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)))

    headers = {
        "x-spiffe-id": _AGENT,
        "x-human-sponsor": "alice@corp.net",
        "x-subject-token": "alice-oidc-token",
    }
    result = await service.execute(headers, _PAYLOAD)

    assert result["result"] == "ok"
    assert upstream.credentials == ["obo::alice-oidc-token::test-audience"]


@pytest.mark.asyncio
async def test_delegated_call_without_subject_token_is_rejected_before_upstream() -> None:
    service, upstream, _ = _service(FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)))

    headers = {"x-spiffe-id": _AGENT, "x-human-sponsor": "alice@corp.net"}
    with pytest.raises(MissingSubjectTokenError):
        await service.execute(headers, _PAYLOAD)

    assert upstream.credentials == []


@pytest.mark.asyncio
async def test_policy_engine_outage_fails_closed_and_is_audited() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(error=PolicyEngineUnavailableError("connection refused"))
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["error"]["code"] == -32000
    assert "fail-closed" in result["error"]["message"]
    assert upstream.credentials == []
    assert exporter.receipts[0].decision is Decision.DENY


@pytest.mark.asyncio
async def test_denied_call_returns_stop_instruction() -> None:
    service, _, _ = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.DENY, reason="velocity_limit"))
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["error"]["data"]["instruction"] == "Stop execution and request user approval."


@pytest.mark.asyncio
async def test_unregistered_agent_is_denied_and_receipted() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        registry=FakeRegistry(agents={}),
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "not registered" in result["error"]["message"]
    assert upstream.credentials == []
    assert exporter.receipts[0].decision is Decision.DENY
    assert "not registered" in exporter.receipts[0].reason


@pytest.mark.asyncio
async def test_suspended_agent_is_denied_and_receipted() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        registry=FakeRegistry(agents={_AGENT: _registered(status=AgentStatus.SUSPENDED)}),
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "suspended" in result["error"]["message"]
    assert upstream.credentials == []
    assert exporter.receipts[0].decision is Decision.DENY


@pytest.mark.asyncio
async def test_token_exchange_failure_returns_semantic_error() -> None:
    service, upstream, _ = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        token_exchange=FakeTokenExchange(error=TokenExchangeError("IdP said no")),
    )

    headers = {
        "x-spiffe-id": _AGENT,
        "x-human-sponsor": "alice@corp.net",
        "x-subject-token": "alice-oidc-token",
    }
    result = await service.execute(headers, _PAYLOAD)

    assert result["error"]["code"] == -32004
    assert upstream.credentials == []


@pytest.mark.asyncio
async def test_upstream_failure_after_allow_returns_semantic_error() -> None:
    service, _, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        upstream=FakeUpstream(error=UpstreamError("503 from target")),
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["error"]["code"] == -32002
    assert "Do not retry" in result["error"]["data"]["instruction"]
    assert exporter.receipts[0].decision is Decision.ALLOW
