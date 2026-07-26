"""Domain models: framework-agnostic, no I/O.

See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentIdentity(BaseModel):
    """A workload identity for an agent, per ADR-0005."""

    spiffe_id: str
    human_sponsor: str | None = None
    """OIDC subject of the human this agent is acting on behalf of, if any."""


class AgentStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RegisteredAgent(BaseModel):
    """An agent's registry record: the facts the enterprise holds about it.

    Per ADR-0008 this — not policy code — is the source of truth for what an
    agent is, who owns it, and which tools it has been granted. `owner` is the
    human accountable for the agent's existence; the per-request `human_sponsor`
    on AgentIdentity is who it acts for right now.
    """

    spiffe_id: str
    display_name: str
    purpose: str
    owner: str
    risk_tier: RiskTier
    status: AgentStatus = AgentStatus.ACTIVE
    granted_tools: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status_changed_at: datetime | None = None
    """When `status` last changed — e.g. when an agent was suspended. Distinct
    from `updated_at` (any field change) so compliance reporting and a future
    kill-switch feature can answer "when was this agent revoked" precisely."""


class ToolCallRequest(BaseModel):
    """A single MCP-shaped tool invocation attempt, per ADR-0004."""

    method: str
    """MCP JSON-RPC method, e.g. "tools/list" or "tools/call"."""
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent: AgentIdentity
    registration: RegisteredAgent | None = None
    """The agent's registry record, attached by the gateway before policy
    evaluation so the policy engine reasons over registry facts (ADR-0008)."""
    request_id: str | int | None = None


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


class PolicyDecision(BaseModel):
    decision: Decision
    reason: str = ""


class ExecutionReceipt(BaseModel):
    """An audit record for one policy decision, per ADR-0003."""

    sequence_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    spiffe_id: str
    decision: Decision
    reason: str = ""
    payload_hash: str
    previous_hash: str
    signature: str | None = None
    """Hex-encoded Ed25519 signature; set once the receipt has been signed."""
    enforced: bool = True
    """False only for a shadow-mode (decision_mode="observe") DENY: the policy
    engine's real decision, recorded and signed exactly as it would be, but not
    what actually happened — the call was forwarded anyway. Registry-gate
    denials (orphaned/suspended agents, ADR-0008) and fail-closed denials
    (policy engine unreachable) are never softened by shadow mode and are
    always enforced=True; see ADR-0012."""


class AuthorizationOutcome(BaseModel):
    """The result of deciding and receipting one tool call, before (or without)
    executing it — the native-SDK pattern of ADR-0001.

    `allowed` is the *effective* answer, not the policy's raw one: in shadow
    mode (ADR-0012) a DENY is recorded and signed exactly as it would be, but
    the call proceeds anyway, so `allowed` is True while `decision` says DENY
    and `shadowed` says why. A caller that gates on `decision` instead of
    `allowed` would silently re-enforce what shadow mode exists to suspend.
    """

    allowed: bool
    decision: PolicyDecision
    receipt: ExecutionReceipt
    call: ToolCallRequest
    shadowed: bool = False


class AgentPatch(BaseModel):
    """Partial update to a RegisteredAgent's mutable facts, per ADR-0014.

    `spiffe_id` is the primary key and never patchable; `status` changes go
    through AgentDirectory.set_status, not this, so status_changed_at is
    always updated deliberately rather than as a side effect of an edit."""

    display_name: str | None = None
    purpose: str | None = None
    owner: str | None = None
    risk_tier: RiskTier | None = None
    granted_tools: list[str] | None = None


class ChainVerificationResult(BaseModel):
    """Result of walking the full receipt chain and checking every hash link
    and signature, per ADR-0014. O(n) over the receipt table — see
    ReceiptQuery.verify_chain."""

    valid: bool
    receipts_checked: int
    first_broken_sequence_id: str | None = None


class FleetSummary(BaseModel):
    """A dashboard landing-page summary, per ADR-0014."""

    agents_by_status: dict[str, int]
    receipts_last_24h_by_decision: dict[str, int]
    last_receipt_timestamp: datetime | None = None


class FleetActivity(BaseModel):
    """Aggregated fleet behaviour over a window, per ADR-0018.

    Grouped by agent and denial reason, never by tool: receipts carry a payload
    hash rather than the payload (ADR-0003), so what a call *was* is provable
    but not readable. `truncated` is part of the answer rather than a footnote —
    a count silently capped by a scan limit is worse than one labelled as a
    lower bound.
    """

    window_hours: int
    total_calls: int
    calls_by_agent: dict[str, int]
    denials_by_agent: dict[str, int]
    denials_by_reason: dict[str, int]
    shadowed_denials: int = 0
    truncated: bool = False
