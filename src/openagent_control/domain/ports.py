"""Ports: the seams adapters plug into. No implementation, no I/O.

Each port corresponds to a row in docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.
Adapters implement these as structural (Protocol) types, so swapping an
implementation never requires changing the port or its callers. Adapters must raise
the domain errors in openagent_control.domain.errors, never their own infrastructure
exceptions, across these boundaries.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from openagent_control.domain.models import (
    AgentIdentity,
    ExecutionReceipt,
    PolicyDecision,
    RegisteredAgent,
    ToolCallRequest,
)


@runtime_checkable
class PolicyEngine(Protocol):
    """Deterministic authorization over a tool call. See ADR-0002.

    Raises PolicyEngineUnavailableError when no decision can be obtained; callers
    must treat that as a deny (fail closed).
    """

    async def evaluate(self, request: ToolCallRequest) -> PolicyDecision: ...


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolves the calling agent's workload identity. See ADR-0005.

    Raises IdentityError when identity cannot be established.
    """

    async def identify(self, raw_headers: dict[str, str]) -> AgentIdentity: ...


@runtime_checkable
class AgentRegistry(Protocol):
    """Source of truth for agent facts (ownership, status, granted tools).

    See ADR-0008. Returns None for unknown SPIFFE IDs — the caller decides how
    to treat orphans (the gateway denies and receipts them).
    """

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None: ...


@runtime_checkable
class Ledger(Protocol):
    """Appends a hash-chained, signed audit receipt. See ADR-0003."""

    async def record(
        self,
        agent: AgentIdentity,
        request: ToolCallRequest,
        decision: PolicyDecision,
        *,
        enforced: bool = True,
    ) -> ExecutionReceipt: ...


@runtime_checkable
class TokenExchange(Protocol):
    """RFC 8693 token exchange for delegated (on-behalf-of) access. See ADR-0004."""

    async def exchange(self, subject_token: str, audience: str) -> str:
        """Returns a short-lived, audience-scoped access token."""
        ...


@runtime_checkable
class CredentialBroker(Protocol):
    """Converts an ALLOW decision into a scoped, short-lived credential."""

    async def issue(self, agent: AgentIdentity, request: ToolCallRequest) -> str: ...


@runtime_checkable
class MCPUpstream(Protocol):
    """Forwards an approved tools/call to a downstream MCP server.

    Raises UpstreamError when the downstream call fails.
    """

    async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]: ...


@runtime_checkable
class AuditExporter(Protocol):
    """Ships a signed receipt to an external sink (SIEM)."""

    async def export(self, receipt: ExecutionReceipt) -> None: ...


@runtime_checkable
class ApprovalChannel(Protocol):
    """Requests a human decision for a paused workflow. Not implemented in v1."""

    async def request_approval(
        self, agent: AgentIdentity, request: ToolCallRequest, reason: str
    ) -> bool: ...
