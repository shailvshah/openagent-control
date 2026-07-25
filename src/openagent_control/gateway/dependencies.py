"""Dependency wiring: the only place adapters are chosen for ports.

See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md — swapping an
adapter (e.g. OPA -> Cedar, header identity -> real SPIRE) means changing this file
and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Request

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.application.governed_execution import GovernedExecutionService
from openagent_control.config import Settings
from openagent_control.domain.ports import (
    AuditExporter,
    IdentityProvider,
    Ledger,
    MCPUpstream,
    PolicyEngine,
    TokenExchange,
)


@dataclass
class Container:
    """Concrete adapters wired to each port, built once at app startup."""

    identity_provider: IdentityProvider
    policy_engine: PolicyEngine
    ledger: Ledger
    audit_exporter: AuditExporter
    token_exchange: TokenExchange
    mcp_upstream: MCPUpstream
    delegated_audience: str = "openagent-control-mcp-upstream"
    governed_execution: GovernedExecutionService = field(init=False)

    def __post_init__(self) -> None:
        self.governed_execution = GovernedExecutionService(
            identity_provider=self.identity_provider,
            policy_engine=self.policy_engine,
            ledger=self.ledger,
            audit_exporter=self.audit_exporter,
            token_exchange=self.token_exchange,
            mcp_upstream=self.mcp_upstream,
            delegated_audience=self.delegated_audience,
        )

    async def aclose(self) -> None:
        """Releases adapter resources (HTTP connection pools) on shutdown."""
        for adapter in (self.policy_engine, self.mcp_upstream):
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                await closer()


def build_container(settings: Settings) -> Container:
    return Container(
        identity_provider=HeaderIdentityProvider(),
        policy_engine=OPAPolicyEngine(opa_url=settings.opa_url),
        ledger=Ed25519ChainLedger(),
        audit_exporter=StdoutAuditExporter(),
        token_exchange=StubTokenExchange(),
        mcp_upstream=HttpMCPUpstream(upstream_url=settings.mcp_upstream_url),
        delegated_audience=settings.delegated_audience,
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
