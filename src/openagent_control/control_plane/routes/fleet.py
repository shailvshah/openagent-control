"""Fleet health summary for the control plane. See docs/adr/0014."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends

from openagent_control.control_plane.auth import get_operator_subject
from openagent_control.control_plane.dependencies import ControlPlaneContainer, get_container
from openagent_control.domain.models import Decision, FleetActivity, FleetSummary

router = APIRouter(prefix="/api/v1/fleet", tags=["fleet"])

_Container = Annotated[ControlPlaneContainer, Depends(get_container)]
_Operator = Annotated[str, Depends(get_operator_subject)]


@router.get("/summary")
async def fleet_summary(container: _Container, _operator: _Operator) -> FleetSummary:
    agents = await container.agent_directory.list_agents()
    agents_by_status = Counter(agent.status.value for agent in agents)

    since = datetime.now(UTC) - timedelta(hours=24)
    # limit=1000: a summary count, not a hard cap on real traffic — fine at
    # this project's expected volume; a fleet doing more than that in a day
    # should read this as "at least 1000," not a false total.
    recent_receipts = await container.receipt_query.search(since=since, limit=1000)
    receipts_by_decision = Counter(receipt.decision.value for receipt in recent_receipts)

    latest = await container.receipt_query.search(limit=1)

    return FleetSummary(
        agents_by_status=dict(agents_by_status),
        receipts_last_24h_by_decision=dict(receipts_by_decision),
        last_receipt_timestamp=latest[0].timestamp if latest else None,
    )


@router.get("/activity")
async def fleet_activity(
    container: _Container, _operator: _Operator, hours: int = 24, limit: int = 2000
) -> FleetActivity:
    """What the fleet actually did, aggregated for a dashboard.

    Grouped by agent and by denial reason — not by tool, which receipts
    deliberately do not carry: ADR-0003 stores a payload hash rather than the
    payload, so the arguments of a call are provable but not readable. "Which
    tool was denied most" is therefore not answerable from the ledger alone,
    and inventing it here would mean weakening that property.

    `limit` bounds the scan rather than the truth: a fleet busier than this
    should read the counts as "at least", which `truncated` says explicitly
    instead of silently under-reporting.
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    receipts = await container.receipt_query.search(since=since, limit=limit)

    calls_by_agent = Counter(r.spiffe_id for r in receipts)
    denials = [r for r in receipts if r.decision is Decision.DENY]
    return FleetActivity(
        window_hours=hours,
        total_calls=len(receipts),
        calls_by_agent=dict(calls_by_agent.most_common(20)),
        denials_by_agent=dict(Counter(r.spiffe_id for r in denials).most_common(20)),
        denials_by_reason=dict(Counter(r.reason for r in denials).most_common(20)),
        shadowed_denials=sum(1 for r in denials if not r.enforced),
        truncated=len(receipts) >= limit,
    )
