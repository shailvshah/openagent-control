from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAC_")

    opa_url: str = "http://localhost:8181/v1/data/openagent/authz"
    mcp_upstream_url: str = "http://localhost:8080"

    mcp_upstream_mode: Literal["streamable-http", "raw-jsonrpc"] = "streamable-http"
    """"streamable-http" speaks the real MCP transport via the official SDK
    (initialize handshake, session ids, SSE) and is what any genuine MCP server
    requires — see ADR-0011. "raw-jsonrpc" POSTs a bare JSON-RPC body, which
    only suits a plain internal HTTP endpoint that is not actually an MCP
    server; a real one answers it with 406 Not Acceptable."""
    registry_path: str = "registry/agents.yaml"
    """Master Agent Registry file (ADR-0008)."""

    delegated_audience: str = "openagent-control-mcp-upstream"
    """OAuth audience requested in token exchanges for delegated (OBO) calls.
    For Entra this maps to the scope parameter (e.g. "api://<app-id>/.default")."""

    identity_mode: Literal["header", "jwt-svid", "oidc-jwks"] = "header"
    """"header" trusts X-Spiffe-ID (dev / behind an attesting mesh only, ADR-0005);
    "jwt-svid" cryptographically validates a SPIFFE JWT-SVID bearer token;
    "oidc-jwks" validates an access token issued by an OIDC provider (Okta,
    Microsoft Entra ID, or any OIDC-compliant IdP) against its published JWKS —
    see ADR-0010."""
    jwt_svid_public_key_path: str = ""
    """PEM public key (SPIRE trust-bundle key) for identity_mode="jwt-svid"."""
    jwt_svid_audience: str = "openagent-control"

    oidc_discovery_url: str = ""
    """OIDC discovery document URL for identity_mode="oidc-jwks", e.g.
    https://{okta-domain}/oauth2/default/.well-known/oauth-authorization-server
    or https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration."""
    oidc_audience: str = ""
    """Expected `aud` claim (your registered application's client ID / App ID URI)."""
    oidc_issuer: str = ""
    """Expected `iss` claim; empty = use the discovery document's own `issuer`."""

    token_exchange_mode: Literal["stub", "rfc8693", "entra"] = "stub"
    """"rfc8693" = Okta-compatible RFC 8693 exchange; "entra" = Microsoft OBO flow."""
    token_exchange_url: str = ""
    token_exchange_client_id: str = ""
    token_exchange_client_secret: str = ""

    database_url: str = ""
    """Async SQLAlchemy URL (e.g. postgresql+asyncpg://user:pass@host/db). Empty =
    use the in-process ledger (ADR-0003) and file registry (ADR-0008) instead of
    Postgres (ADR-0009)."""

    redis_url: str = ""
    """Empty = caching disabled; registry lookups and token exchange call
    straight through to their adapter on every request (ADR-0009)."""
    registry_cache_ttl_seconds: int = 30
    token_cache_max_ttl_seconds: int = 300
    token_cache_safety_margin_seconds: int = 30
