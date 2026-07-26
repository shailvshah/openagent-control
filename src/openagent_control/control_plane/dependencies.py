"""Control-plane dependency wiring. See docs/adr/0014.

Deliberately a separate, thinner container from gateway/dependencies.py's
Container: this service never constructs GovernedExecutionService,
PolicyEngine, MCPUpstream, or TokenExchange — a compromise here has no path
to those at all, because the process never imports them.

Requires OAC_DATABASE_URL unconditionally, unlike the gateway (which has a
zero-dependency file/in-memory mode): this service's entire purpose is
operating on a real deployment's persisted data.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from openagent_control.config import Settings
from openagent_control.domain.ports import (
    AgentDirectory,
    AgentRegistry,
    OperatorIdentity,
    ReceiptQuery,
)


@dataclass
class ControlPlaneContainer:
    agent_registry: AgentRegistry
    """Same underlying object as agent_directory (PostgresAgentRegistry
    implements both protocols) — exposed separately for the single-lookup
    read path, so routes don't need to list every agent to find one."""
    agent_directory: AgentDirectory
    receipt_query: ReceiptQuery
    operator_auth: OperatorIdentity
    public_key: Ed25519PublicKey
    db_engine: AsyncEngine

    async def aclose(self) -> None:
        await self.db_engine.dispose()


def _public_key(settings: Settings) -> Ed25519PublicKey:
    """Fetches the same public key the gateway's Signer would expose, but
    discards everything else: this function never returns anything capable of
    .sign() — see ADR-0014's security-boundary reasoning. Fails fast (same
    posture as gateway/dependencies.py's _signer) if Vault is unreachable."""
    if settings.signing_key_mode == "vault-transit":
        from openagent_control.adapters.ledger.vault_signer import VaultTransitSigner

        signer = VaultTransitSigner(
            vault_url=settings.vault_url,
            token=settings.vault_token,
            key_name=settings.vault_transit_key_name,
        )
    else:
        from openagent_control.adapters.ledger.signing import ReceiptSigner

        signer = ReceiptSigner()  # type: ignore[assignment]
    return signer.public_key()


def _operator_auth(settings: Settings) -> OperatorIdentity:
    if settings.control_plane_operator_auth_mode == "oidc-jwks":
        from openagent_control.adapters.operator_identity.oidc import OidcOperatorAuth

        return OidcOperatorAuth(
            discovery_url=settings.control_plane_oidc_discovery_url,
            audience=settings.control_plane_oidc_audience,
            role_claim=settings.control_plane_oidc_role_claim,
            required_role=settings.control_plane_oidc_required_role,
            issuer=settings.control_plane_oidc_issuer,
        )
    from openagent_control.adapters.operator_identity.api_key import ApiKeyOperatorAuth

    return ApiKeyOperatorAuth(settings.control_plane_api_key)


def build_control_plane_container(settings: Settings) -> ControlPlaneContainer:
    if not settings.database_url:
        raise RuntimeError(
            "OAC_DATABASE_URL is required to run the control plane — its entire "
            "purpose is operating on a real deployment's persisted registry and "
            "receipts, unlike the gateway's zero-dependency dev mode."
        )
    try:
        from openagent_control.adapters.db.session import make_engine, make_session_factory
        from openagent_control.adapters.ledger.postgres_query import PostgresReceiptQuery
        from openagent_control.adapters.registry.postgres import PostgresAgentRegistry
    except ImportError as exc:
        raise RuntimeError(
            "OAC_DATABASE_URL is configured but the persistence dependencies are "
            "not installed — install with: pip install 'openagent-control[persistence]'"
        ) from exc

    db_engine = make_engine(settings.database_url)
    session_factory = make_session_factory(db_engine)
    public_key = _public_key(settings)
    registry = PostgresAgentRegistry(session_factory)

    return ControlPlaneContainer(
        agent_registry=registry,
        agent_directory=registry,
        receipt_query=PostgresReceiptQuery(session_factory, public_key),
        operator_auth=_operator_auth(settings),
        public_key=public_key,
        db_engine=db_engine,
    )


def get_container(request: Request) -> ControlPlaneContainer:
    container: ControlPlaneContainer = request.app.state.container
    return container
