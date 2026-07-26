"""Receipt search/verify for the control plane. See docs/adr/0014.

All routes here go through ReceiptQuery, never Ledger — this process holds
nothing capable of writing oac.execution_receipts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from openagent_control.control_plane.auth import get_operator_subject
from openagent_control.control_plane.dependencies import ControlPlaneContainer, get_container
from openagent_control.domain.models import ChainVerificationResult, Decision, ExecutionReceipt

router = APIRouter(prefix="/api/v1/receipts", tags=["receipts"])

_Container = Annotated[ControlPlaneContainer, Depends(get_container)]
_Operator = Annotated[str, Depends(get_operator_subject)]


@router.get("")
async def search_receipts(
    container: _Container,
    _operator: _Operator,
    spiffe_id: str | None = None,
    decision: Decision | None = None,
    enforced: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ExecutionReceipt]:
    return await container.receipt_query.search(
        spiffe_id=spiffe_id,
        decision=decision,
        enforced=enforced,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


@router.get("/verify-chain")
async def verify_chain(container: _Container, _operator: _Operator) -> ChainVerificationResult:
    """Walks the full receipt chain. O(n) over the whole table — a fleet
    integrity check, not something to call on every dashboard page load."""
    return await container.receipt_query.verify_chain()


@router.get("/{sequence_id}")
async def get_receipt(
    sequence_id: str, container: _Container, _operator: _Operator
) -> ExecutionReceipt:
    receipt = await container.receipt_query.get(sequence_id)
    if receipt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no receipt '{sequence_id}'")
    return receipt
