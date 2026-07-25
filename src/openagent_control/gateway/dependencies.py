"""Dependency wiring: the only place adapters are chosen for ports.

See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md — swapping an
adapter (e.g. OPA -> Cedar, header identity -> JWT-SVID, stub -> Okta/Entra token
exchange, file/in-memory -> Postgres, uncached -> Redis-cached) means changing
settings consumed here, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import redis.asyncio as redis_asyncio
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.db.session import make_engine, make_session_factory
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.postgres import PostgresLedger
from openagent_control.adapters.ledger.signing import ReceiptSigner
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.adapters.registry.caching import CachingAgentRegistry
from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
from openagent_control.adapters.token_exchange.caching import CachingTokenExchange
from openagent_control.adapters.token_exchange.entra_obo import EntraOnBehalfOfTokenExchange
from openagent_control.adapters.token_exchange.rfc8693 import Rfc8693TokenExchange
from openagent_control.adapters.token_exchange.stub import StubTokenExchange
from openagent_control.application.governed_execution import GovernedExecutionService
from openagent_control.config import Settings
from openagent_control.domain.ports import (
    AgentRegistry,
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
    agent_registry: AgentRegistry
    policy_engine: PolicyEngine
    ledger: Ledger
    audit_exporter: AuditExporter
    token_exchange: TokenExchange
    mcp_upstream: MCPUpstream
    delegated_audience: str = "openagent-control-mcp-upstream"
    db_engine: AsyncEngine | None = None
    redis_client: redis_asyncio.Redis | None = None
    governed_execution: GovernedExecutionService = field(init=False)

    def __post_init__(self) -> None:
        self.governed_execution = GovernedExecutionService(
            identity_provider=self.identity_provider,
            agent_registry=self.agent_registry,
            policy_engine=self.policy_engine,
            ledger=self.ledger,
            audit_exporter=self.audit_exporter,
            token_exchange=self.token_exchange,
            mcp_upstream=self.mcp_upstream,
            delegated_audience=self.delegated_audience,
        )

    async def aclose(self) -> None:
        """Releases adapter resources (HTTP pools, DB engine, Redis client)."""
        for adapter in (self.policy_engine, self.mcp_upstream, self.token_exchange):
            closer = getattr(adapter, "aclose", None)
            if closer is not None:
                await closer()
        if self.db_engine is not None:
            await self.db_engine.dispose()
        if self.redis_client is not None:
            await self.redis_client.aclose()


def _identity_provider(settings: Settings) -> IdentityProvider:
    if settings.identity_mode == "jwt-svid":
        return JwtSvidIdentityProvider(
            public_key_path=settings.jwt_svid_public_key_path,
            audience=settings.jwt_svid_audience,
        )
    return HeaderIdentityProvider()


def _token_exchange(settings: Settings) -> TokenExchange:
    if settings.token_exchange_mode == "rfc8693":
        return Rfc8693TokenExchange(
            token_url=settings.token_exchange_url,
            client_id=settings.token_exchange_client_id,
            client_secret=settings.token_exchange_client_secret,
        )
    if settings.token_exchange_mode == "entra":
        return EntraOnBehalfOfTokenExchange(
            token_url=settings.token_exchange_url,
            client_id=settings.token_exchange_client_id,
            client_secret=settings.token_exchange_client_secret,
        )
    return StubTokenExchange()


def build_container(settings: Settings) -> Container:
    db_engine: AsyncEngine | None = None
    if settings.database_url:
        db_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(db_engine)
        agent_registry: AgentRegistry = PostgresAgentRegistry(session_factory)
        ledger: Ledger = PostgresLedger(session_factory, ReceiptSigner())
    else:
        agent_registry = FileAgentRegistry(settings.registry_path)
        ledger = Ed25519ChainLedger()

    token_exchange = _token_exchange(settings)

    redis_client: redis_asyncio.Redis | None = None
    if settings.redis_url:
        redis_client = redis_asyncio.Redis.from_url(settings.redis_url)
        agent_registry = CachingAgentRegistry(
            agent_registry, redis_client, settings.registry_cache_ttl_seconds
        )
        token_exchange = CachingTokenExchange(
            token_exchange,
            redis_client,
            settings.token_cache_max_ttl_seconds,
            settings.token_cache_safety_margin_seconds,
        )

    return Container(
        identity_provider=_identity_provider(settings),
        agent_registry=agent_registry,
        policy_engine=OPAPolicyEngine(opa_url=settings.opa_url),
        ledger=ledger,
        audit_exporter=StdoutAuditExporter(),
        token_exchange=token_exchange,
        mcp_upstream=HttpMCPUpstream(upstream_url=settings.mcp_upstream_url),
        delegated_audience=settings.delegated_audience,
        db_engine=db_engine,
        redis_client=redis_client,
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
