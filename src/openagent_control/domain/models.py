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


class ToolCallRequest(BaseModel):
    """A single MCP-shaped tool invocation attempt, per ADR-0004."""

    method: str
    """MCP JSON-RPC method, e.g. "tools/list" or "tools/call"."""
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent: AgentIdentity
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
