"""Dependency wiring: the only place adapters are chosen for ports.

See docs/adr/0006-hexagonal-architecture-for-the-control-plane.md — swapping an
adapter (e.g. OPA -> Cedar, header identity -> JWT-SVID, stub -> Okta/Entra token
exchange, file/in-memory -> Postgres, uncached -> Redis-cached) means changing
settings consumed here, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from fastapi import Request

from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
from openagent_control.adapters.identity.header import HeaderIdentityProvider
from openagent_control.adapters.identity.jwt_svid import JwtSvidIdentityProvider
from openagent_control.adapters.identity.oidc_jwks import OidcJwksIdentityProvider
from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
from openagent_control.adapters.ledger.signing import ReceiptSigner, Signer
from openagent_control.adapters.mcp_upstream.http import HttpMCPUpstream
from openagent_control.adapters.mcp_upstream.routing import RoutingMCPUpstream
from openagent_control.adapters.mcp_upstream.streamable_http import StreamableHttpMCPUpstream
from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.adapters.registry.file import FileAgentRegistry
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
from openagent_control.resources import example_registry

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncEngine


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
    decision_mode: Literal["enforce", "observe"] = "enforce"
    db_engine: AsyncEngine | None = None
    redis_client: Redis | None = None
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
            decision_mode=self.decision_mode,
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
    if settings.identity_mode == "oidc-jwks":
        return OidcJwksIdentityProvider(
            discovery_url=settings.oidc_discovery_url,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
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


def resolve_registry_path(settings: Settings) -> Path:
    """The registry file to read, failing fast if it is missing.

    Startup is the right place for this error. Left to the first request, a
    missing registry surfaces as a 500 per call while /healthz keeps returning
    200 — a green signal over a gateway that cannot serve anything.
    """
    if not settings.registry_path:
        return example_registry()
    path = Path(settings.registry_path)
    if not path.is_file():
        raise RuntimeError(
            f"OAC_REGISTRY_PATH points at '{path}', which does not exist. "
            "Create one with: openagent-control init <dir>"
        )
    return path


def _one_upstream(settings: Settings, url: str) -> MCPUpstream:
    if settings.mcp_upstream_mode == "raw-jsonrpc":
        return HttpMCPUpstream(upstream_url=url)
    return StreamableHttpMCPUpstream(upstream_url=url)


def _mcp_upstream(settings: Settings) -> MCPUpstream:
    """One upstream, or several behind a routing adapter (ADR-0016).

    `mcp_upstreams` deliberately takes precedence over `mcp_upstream_url`
    rather than merging with it: silently folding the single-upstream default
    (`http://localhost:8080`) into a configured fleet would add a phantom
    member to every multi-upstream deployment.
    """
    if not settings.mcp_upstreams:
        return _one_upstream(settings, settings.mcp_upstream_url)
    return RoutingMCPUpstream(
        {name: _one_upstream(settings, url) for name, url in settings.mcp_upstreams.items()},
        cache_ttl_seconds=settings.mcp_route_cache_ttl_seconds,
    )


def _require_persistence(feature: str) -> NoReturn:
    """Raises a clear startup error if the optional persistence extra is missing."""
    raise RuntimeError(
        f"{feature} is configured but the persistence dependencies are not "
        "installed — install with: pip install 'openagent-control[persistence]' "
        "(or poetry install --extras persistence)"
    )


def _signer(settings: Settings) -> Signer:
    """Which key custody backs receipt signing — orthogonal to which ledger
    backend stores the receipts (ADR-0013). Fetches the public key from Vault
    at construction time, same startup-fail-fast posture as
    OidcJwksIdentityProvider (ADR-0010): an unreachable Vault or a missing
    transit key becomes a startup failure, not a per-request one."""
    if settings.signing_key_mode == "vault-transit":
        from openagent_control.adapters.ledger.vault_signer import VaultTransitSigner

        return VaultTransitSigner(
            vault_url=settings.vault_url,
            token=settings.vault_token,
            key_name=settings.vault_transit_key_name,
        )
    return ReceiptSigner()


def build_container(settings: Settings) -> Container:
    # The Postgres/Redis stack is imported lazily inside these branches: eager
    # imports cost ~43MB RSS and ~150ms startup even when persistence is unused
    # (measured; see ADR-0009).
    signer = _signer(settings)
    db_engine: AsyncEngine | None = None
    if settings.database_url:
        try:
            from openagent_control.adapters.db.session import make_engine, make_session_factory
            from openagent_control.adapters.ledger.postgres import PostgresLedger
            from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
        except ImportError:
            _require_persistence("OAC_DATABASE_URL")
        db_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(db_engine)
        agent_registry: AgentRegistry = PostgresAgentRegistry(session_factory)
        ledger: Ledger = PostgresLedger(session_factory, signer)
    else:
        agent_registry = FileAgentRegistry(resolve_registry_path(settings))
        ledger = Ed25519ChainLedger(signer)

    token_exchange = _token_exchange(settings)

    redis_client: Redis | None = None
    if settings.redis_url:
        try:
            from redis.asyncio import Redis as RedisClient

            from openagent_control.adapters.registry.caching import CachingAgentRegistry
            from openagent_control.adapters.token_exchange.caching import CachingTokenExchange
        except ImportError:
            _require_persistence("OAC_REDIS_URL")
        # decode_responses=True so cached tokens/JSON come back as str, matching
        # what the caching adapters and pydantic expect.
        redis_client = RedisClient.from_url(settings.redis_url, decode_responses=True)
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
        mcp_upstream=_mcp_upstream(settings),
        delegated_audience=settings.delegated_audience,
        decision_mode=settings.decision_mode,
        db_engine=db_engine,
        redis_client=redis_client,
    )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container
