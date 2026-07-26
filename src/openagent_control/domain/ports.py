"""Ports: the seams adapters plug into. No implementation, no I/O.

Each port corresponds to a row in docs/adr/0006-hexagonal-architecture-for-the-control-plane.md.
Adapters implement these as structural (Protocol) types, so swapping an
implementation never requires changing the port or its callers. Adapters must raise
the domain errors in openagent_control.domain.errors, never their own infrastructure
exceptions, across these boundaries.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from openagent_control.domain.models import (
    AgentIdentity,
    AgentPatch,
    AgentStatus,
    ChainVerificationResult,
    Decision,
    ExecutionReceipt,
    PolicyDecision,
    RegisteredAgent,
    SubjectIdentity,
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
class SubjectVerifier(Protocol):
    """Verifies the human subject token on a delegated call. See ADR-0019.

    Separate from IdentityProvider (which answers "what workload is calling")
    and from OperatorIdentity (which answers "may this human operate the
    control plane"). This one answers "who is this call actually running as,
    and what are they entitled to" — the authorization principal behind a
    delegated call, as opposed to the sponsor, which is only an approval.

    Raises IdentityError when the token is absent, invalid, or does not belong
    to the sponsor the agent claimed.
    """

    async def verify(self, subject_token: str) -> SubjectIdentity: ...


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


@runtime_checkable
class AgentDirectory(Protocol):
    """Registry CRUD for the control plane, per ADR-0014.

    Deliberately separate from AgentRegistry (the gateway's single-method,
    hot-path port): bloating that port with list/create/verify would force
    every existing adapter — including CachingAgentRegistry, which only wraps
    another AgentRegistry — to grow methods that make no sense for it.

    Every mutating method takes `operator_subject` (from OperatorIdentity) and
    writes an oac.operator_actions row in the same transaction as its agents
    write — see ADR-0014's "every mutation is itself audited."
    """

    async def list_agents(self, *, status: AgentStatus | None = None) -> list[RegisteredAgent]: ...
    async def create(self, agent: RegisteredAgent, *, operator_subject: str) -> RegisteredAgent: ...
    async def update(
        self, spiffe_id: str, patch: AgentPatch, *, operator_subject: str
    ) -> RegisteredAgent: ...
    async def set_status(
        self, spiffe_id: str, status: AgentStatus, *, operator_subject: str
    ) -> RegisteredAgent: ...


@runtime_checkable
class ReceiptQuery(Protocol):
    """Read-only access to the audit ledger for the control plane, per
    ADR-0014. Deliberately separate from Ledger (write-only, append-only):
    a component holding a ReceiptQuery can never write a receipt, by
    construction — see ADR-0014's security-boundary reasoning.
    """

    async def search(
        self,
        *,
        spiffe_id: str | None = None,
        decision: Decision | None = None,
        enforced: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExecutionReceipt]: ...

    async def get(self, sequence_id: str) -> ExecutionReceipt | None: ...

    async def verify_chain(self) -> ChainVerificationResult:
        """Walks every receipt, recomputing hash links and signatures.

        O(n) over the whole table — fine at this project's expected receipt
        volume, but must never be called from a hot path.
        """
        ...


@runtime_checkable
class OperatorIdentity(Protocol):
    """Resolves the calling human operator's identity for the control plane,
    per ADR-0014 — distinct from IdentityProvider, which resolves a workload's
    identity for the gateway. Raises IdentityError when the request cannot be
    authenticated as an authorized operator.
    """

    async def identify(self, raw_headers: dict[str, str]) -> str:
        """Returns an opaque operator-subject string for audit logging."""
        ...
