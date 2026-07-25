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
    UpstreamError,
)
from openagent_control.domain.models import (
    Decision,
    ExecutionReceipt,
    PolicyDecision,
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
    async def exchange(self, subject_token: str, audience: str) -> str:
        return f"obo::{subject_token}::{audience}"


def _service(
    policy: FakePolicyEngine,
    upstream: FakeUpstream | None = None,
    exporter: RecordingExporter | None = None,
) -> tuple[GovernedExecutionService, FakeUpstream, RecordingExporter]:
    upstream = upstream or FakeUpstream()
    exporter = exporter or RecordingExporter()
    service = GovernedExecutionService(
        identity_provider=HeaderIdentityProvider(),
        policy_engine=policy,
        ledger=Ed25519ChainLedger(),
        audit_exporter=exporter,
        token_exchange=FakeTokenExchange(),
        mcp_upstream=upstream,
        delegated_audience="test-audience",
    )
    return service, upstream, exporter


@pytest.mark.asyncio
async def test_allowed_autonomous_call_uses_workload_credential() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW))
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["result"] == "ok"
    assert upstream.credentials == [f"autonomous::{_AGENT}"]
    assert exporter.receipts[0].decision is Decision.ALLOW


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
async def test_upstream_failure_after_allow_returns_semantic_error() -> None:
    service, _, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        upstream=FakeUpstream(error=UpstreamError("503 from target")),
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["error"]["code"] == -32002
    assert "Do not retry" in result["error"]["data"]["instruction"]
    assert exporter.receipts[0].decision is Decision.ALLOW
