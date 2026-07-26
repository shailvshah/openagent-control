from __future__ import annotations

import pytest
from loguru import logger

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.domain.models import Decision, ExecutionReceipt


@pytest.mark.asyncio
async def test_export_logs_receipt() -> None:
    """Attaches a temporary sink rather than capturing stderr: loguru's global
    default sink is created once, at import time, well before this test's
    capture fixtures could attach to it — a stream-capture-based assertion
    would be checking a stale target. A sink added inside the test always
    points at something live for exactly this test, which is the pattern
    loguru's own docs recommend for testing."""
    receipt = ExecutionReceipt(
        sequence_id="seq-1",
        spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot",
        decision=Decision.ALLOW,
        payload_hash="a" * 64,
        previous_hash="0" * 64,
    )
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")

    try:
        await StdoutAuditExporter().export(receipt)
    finally:
        logger.remove(sink_id)

    assert any("audit_receipt" in message for message in messages)
