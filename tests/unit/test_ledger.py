from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger, _canonical_json
from openagent_control.domain.models import AgentIdentity, Decision, PolicyDecision, ToolCallRequest


def _call() -> ToolCallRequest:
    return ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={"table": "invoices"},
        agent=AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot"),
    )


@pytest.mark.asyncio
async def test_receipts_chain_and_verify() -> None:
    ledger = Ed25519ChainLedger()
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

    first = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))
    second = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))

    assert first.previous_hash == "0" * 64
    assert second.previous_hash != first.previous_hash

    public_key: Ed25519PublicKey = ledger.public_key()
    unsigned = _canonical_json(first.model_dump(mode="json", exclude={"signature"}))
    public_key.verify(bytes.fromhex(first.signature or ""), unsigned)


@pytest.mark.asyncio
async def test_deny_decision_is_recorded_with_reason() -> None:
    ledger = Ed25519ChainLedger()
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/sales/agent/lead-qualifier")

    receipt = await ledger.record(
        agent, _call(), PolicyDecision(decision=Decision.DENY, reason="velocity_limit")
    )

    assert receipt.decision is Decision.DENY
    assert receipt.reason == "velocity_limit"
