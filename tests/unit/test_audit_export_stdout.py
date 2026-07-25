from __future__ import annotations

import logging

import pytest

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.domain.models import Decision, ExecutionReceipt


@pytest.mark.asyncio
async def test_export_logs_receipt(caplog: pytest.LogCaptureFixture) -> None:
    receipt = ExecutionReceipt(
        sequence_id="seq-1",
        spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot",
        decision=Decision.ALLOW,
        payload_hash="a" * 64,
        previous_hash="0" * 64,
    )

    with caplog.at_level(logging.INFO, logger="openagent_control.audit"):
        await StdoutAuditExporter().export(receipt)

    assert "audit_receipt" in caplog.text
