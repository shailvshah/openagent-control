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
