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
    SubjectIdentity,
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
        self.received: ToolCallRequest | None = None

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
        self.received = request
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


class FakeSubjectVerifier:
    """Records the token it was asked to verify, so a test can assert
    verification actually ran (rather than only that the end result looked
    right for reasons that could be coincidental)."""

    def __init__(self, identity: SubjectIdentity) -> None:
        self._identity = identity
        self.verified_tokens: list[str] = []

    async def verify(self, subject_token: str) -> SubjectIdentity:
        self.verified_tokens.append(subject_token)
        return self._identity


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
    decision_mode: str = "enforce",
    subject_verifier: FakeSubjectVerifier | None = None,
    subject_binding: str = "strict",
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
        decision_mode=decision_mode,  # type: ignore[arg-type]
        subject_verifier=subject_verifier,
        subject_binding=subject_binding,  # type: ignore[arg-type]
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
async def test_subject_verification_runs_without_an_agent_side_sponsor_claim() -> None:
    """ADR-0020: a stable, autonomous agent identity (no `human_sponsor` on its
    own token) still gets its caller's subject verified and exposed to policy
    when a subject token is attached per request. Before this was fixed,
    verification was gated on `agent.human_sponsor` — which this call
    deliberately doesn't have — so it silently never ran at all; `call.subject`
    stayed `None` regardless of a real subject token being present."""
    identity = SubjectIdentity(
        subject_id="https://idp.corp.net#dana",
        issuer="https://idp.corp.net",
        roles=["finance-approver"],
    )
    verifier = FakeSubjectVerifier(identity)
    policy = FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW))
    service, _, _ = _service(policy, subject_verifier=verifier, subject_binding="off")

    # No x-human-sponsor at all -- an autonomous agent token, per ADR-0020.
    headers = {"x-spiffe-id": _AGENT, "x-subject-token": "dana-oidc-token"}
    await service.execute(headers, _PAYLOAD)

    assert verifier.verified_tokens == ["dana-oidc-token"]
    assert policy.received is not None
    assert policy.received.subject is identity


@pytest.mark.asyncio
async def test_subject_verification_is_skipped_with_no_subject_token_at_all() -> None:
    """The other half of the same fix: a subject verifier being configured
    must not force every autonomous call (no subject_token at all) through
    verification -- that would reject ordinary autonomous traffic outright."""
    verifier = FakeSubjectVerifier(
        SubjectIdentity(subject_id="x#y", issuer="x", roles=["finance-approver"])
    )
    policy = FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW))
    service, _, _ = _service(policy, subject_verifier=verifier)

    await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert verifier.verified_tokens == []
    assert policy.received is not None
    assert policy.received.subject is None


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


@pytest.mark.asyncio
async def test_observe_mode_forwards_a_policy_deny_but_receipts_it_unenforced() -> None:
    """ADR-0012: the call goes through, but the audit trail records exactly what
    enforce mode would have blocked."""
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.DENY, reason="not granted")),
        decision_mode="observe",
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["result"] == "ok"  # forwarded, not blocked
    assert upstream.credentials == [f"autonomous::{_AGENT}"]
    receipt = exporter.receipts[0]
    assert receipt.decision is Decision.DENY
    assert receipt.reason == "not granted"
    assert receipt.enforced is False


@pytest.mark.asyncio
async def test_observe_mode_still_enforces_an_allow_normally() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)), decision_mode="observe"
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["result"] == "ok"
    assert upstream.credentials == [f"autonomous::{_AGENT}"]
    assert exporter.receipts[0].enforced is True


@pytest.mark.asyncio
async def test_observe_mode_does_not_soften_an_orphaned_agent_denial() -> None:
    """The registry gate (ADR-0008) is a hard security boundary, not a policy
    call shadow mode exists to tune — it must never be bypassed by observe mode."""
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        registry=FakeRegistry(agents={}),
        decision_mode="observe",
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "not registered" in result["error"]["message"]
    assert upstream.credentials == []
    assert exporter.receipts[0].enforced is True


@pytest.mark.asyncio
async def test_observe_mode_does_not_soften_a_suspended_agent_denial() -> None:
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        registry=FakeRegistry(agents={_AGENT: _registered(status=AgentStatus.SUSPENDED)}),
        decision_mode="observe",
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "suspended" in result["error"]["message"]
    assert upstream.credentials == []
    assert exporter.receipts[0].enforced is True


@pytest.mark.asyncio
async def test_observe_mode_does_not_soften_a_fail_closed_denial() -> None:
    """A policy-engine outage is an infrastructure failure, not the kind of
    signal shadow mode exists to observe — it must still block."""
    service, upstream, exporter = _service(
        FakePolicyEngine(error=PolicyEngineUnavailableError("connection refused")),
        decision_mode="observe",
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "error" in result
    assert upstream.credentials == []
    assert exporter.receipts[0].enforced is True


@pytest.mark.asyncio
async def test_enforce_mode_never_shadows_a_deny() -> None:
    """decision_mode="enforce" (the default) behaves exactly as before ADR-0012."""
    service, upstream, exporter = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.DENY, reason="nope"))
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert "error" in result
    assert upstream.credentials == []
    assert exporter.receipts[0].enforced is True


class ListingUpstream:
    """An upstream advertising a full catalogue, as a real shared MCP server does."""

    def __init__(self, tools: list[str]) -> None:
        self._tools = tools

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "result": {"tools": [{"name": t, "description": t} for t in self._tools]},
        }


_LIST_PAYLOAD: dict[str, Any] = {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}


@pytest.mark.asyncio
async def test_tools_list_is_projected_down_to_the_registrys_grants() -> None:
    """Advertising the upstream's whole catalogue would have the agent discover
    tools it can only be denied for calling — see ADR-0016."""
    upstream = ListingUpstream(["read_query", "update_record", "delete_everything"])
    service, _, _ = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        upstream=upstream,  # type: ignore[arg-type]
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _LIST_PAYLOAD)

    assert [t["name"] for t in result["result"]["tools"]] == ["read_query"]


@pytest.mark.asyncio
async def test_listing_projection_preserves_the_rest_of_the_result() -> None:
    """Only `tools` is filtered; pagination cursors and any other fields the
    upstream returned must survive, or a paging client silently breaks."""

    class PagedUpstream:
        async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": {"tools": [{"name": "read_query"}], "nextCursor": "abc"},
            }

    service, _, _ = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        upstream=PagedUpstream(),  # type: ignore[arg-type]
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _LIST_PAYLOAD)

    assert result["result"]["nextCursor"] == "abc"


@pytest.mark.asyncio
async def test_a_tools_call_result_is_never_touched_by_the_projection() -> None:
    service, _, _ = _service(FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)))

    result = await service.execute({"x-spiffe-id": _AGENT}, _PAYLOAD)

    assert result["result"] == "ok"


@pytest.mark.asyncio
async def test_an_upstream_error_response_passes_through_the_projection_untouched() -> None:
    """A JSON-RPC error carries no `result`; rewriting it into an empty tool
    list would turn an upstream failure into a silent "you have no tools"."""

    class ErroringUpstream:
        async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "error": {"code": -32603, "message": "upstream exploded"},
            }

    service, _, _ = _service(
        FakePolicyEngine(PolicyDecision(decision=Decision.ALLOW)),
        upstream=ErroringUpstream(),  # type: ignore[arg-type]
    )

    result = await service.execute({"x-spiffe-id": _AGENT}, _LIST_PAYLOAD)

    assert result["error"]["message"] == "upstream exploded"
