"""Ports: the seams adapters plug into. No implementation, no I/O.

Each port corresponds to a row in docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.
Adapters implement these as structural (Protocol) types, so swapping an
implementation never requires changing the port or its callers.
"""

from __future__ import annotations

from typing import Any, Protocol

from openagent_control.domain.models import (
    AgentIdentity,
    ExecutionReceipt,
    PolicyDecision,
    ToolCallRequest,
)


class PolicyEngine(Protocol):
    """Deterministic authorization over a tool call. See ADR-0002."""

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision: ...


class IdentityProvider(Protocol):
    """Resolves the calling agent's workload identity. See ADR-0005."""

    async def identify(self, raw_headers: dict[str, str]) -> AgentIdentity: ...


class Ledger(Protocol):
    """Appends a hash-chained, signed audit receipt. See ADR-0003."""

    async def record(
        self, agent: AgentIdentity, request: ToolCallRequest, decision: PolicyDecision
    ) -> ExecutionReceipt: ...


class TokenExchange(Protocol):
    """RFC 8693 token exchange for delegated (on-behalf-of) access. See ADR-0004."""

    async def exchange(self, subject_token: str, audience: str) -> str:
        """Returns a short-lived, audience-scoped access token."""
        ...


class CredentialBroker(Protocol):
    """Converts an ALLOW decision into a scoped, short-lived credential."""

    async def issue(self, agent: AgentIdentity, request: ToolCallRequest) -> str: ...


class MCPUpstream(Protocol):
    """Forwards an approved tools/call to a downstream MCP server."""

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]: ...


class AuditExporter(Protocol):
    """Ships a signed receipt to an external sink (SIEM)."""

    async def export(self, receipt: ExecutionReceipt) -> None: ...


class ApprovalChannel(Protocol):
    """Requests a human decision for a paused workflow. Not implemented in v1."""

    async def request_approval(
        self, agent: AgentIdentity, request: ToolCallRequest, reason: str
    ) -> bool: ...
