from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.signing import canonical_json
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
    unsigned = canonical_json(first.model_dump(mode="json", exclude={"signature"}))
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


@pytest.mark.asyncio
async def test_receipt_defaults_to_enforced() -> None:
    ledger = Ed25519ChainLedger()
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

    receipt = await ledger.record(agent, _call(), PolicyDecision(decision=Decision.ALLOW))

    assert receipt.enforced is True


@pytest.mark.asyncio
async def test_shadow_deny_is_signed_with_enforced_false() -> None:
    """ADR-0012: the receipt for an unenforced shadow-mode DENY is still
    genuinely signed, not a lesser record — only the `enforced` flag differs."""
    ledger = Ed25519ChainLedger()
    agent = AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot")

    receipt = await ledger.record(
        agent, _call(), PolicyDecision(decision=Decision.DENY, reason="not granted"), enforced=False
    )

    assert receipt.enforced is False
    public_key: Ed25519PublicKey = ledger.public_key()
    unsigned = canonical_json(receipt.model_dump(mode="json", exclude={"signature"}))
    public_key.verify(bytes.fromhex(receipt.signature or ""), unsigned)
