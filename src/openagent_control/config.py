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
    registry_path: str = ""
    """Master Agent Registry file (ADR-0008). Empty = the empty starter registry
    bundled in the package, which denies every agent until you register one.

    This must not default to a relative path: a pip-installed gateway would
    then resolve it against the caller's working directory, start, report
    healthy, and fail every request with FileNotFoundError."""

    decision_mode: Literal["enforce", "observe"] = "enforce"
    """"observe" lets a policy DENY through instead of blocking it: the real
    decision is still recorded and signed (with enforced=false), but the call
    is forwarded upstream anyway. Exists so a first deployment can run against
    live traffic and see what a policy WOULD block before it actually blocks
    anything — see ADR-0012. Registry-gate denials (orphaned/suspended agents)
    and fail-closed denials (policy engine unreachable) are never softened by
    this setting; only an explicit OPA DENY is."""

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

    signing_key_mode: Literal["in-process", "vault-transit"] = "in-process"
    """"in-process" (default) generates an Ed25519 key in memory, lost on
    restart — receipts are signed, but the key isn't compliance-grade custody.
    "vault-transit" signs via HashiCorp Vault's Transit engine: the private key
    never leaves Vault, this process only ever sees signatures. See ADR-0013
    for why Vault specifically (AWS KMS and Azure Key Vault don't support
    Ed25519 asymmetric signing)."""
    vault_url: str = "http://localhost:8200"
    vault_token: str = ""
    vault_transit_key_name: str = "oac-receipt-signer"

    otel_enabled: bool = False
    """Off by default: instrumentation (spans through GovernedExecutionService)
    is always present via the no-op tracer, but nothing is exported until a
    collector endpoint is configured — an unreachable collector must not be
    able to affect request handling."""
    otel_exporter_endpoint: str = "http://localhost:4318/v1/traces"
    """OTLP/HTTP endpoint. Any local collector or vendor OTLP ingest works;
    this project is not tied to a specific backend."""
    otel_service_name: str = "openagent-control"

    control_plane_operator_auth_mode: Literal["api-key", "oidc-jwks"] = "api-key"
    """"api-key" validates a static bearer token (control_plane_api_key) — the
    low-ceremony default for direct API/script/CI use, same posture as
    identity_mode="header". "oidc-jwks" validates a real operator's OIDC
    access token against a required role/group claim. See ADR-0014."""
    control_plane_api_key: str = ""
    """Required when control_plane_operator_auth_mode="api-key"."""
    control_plane_oidc_discovery_url: str = ""
    control_plane_oidc_audience: str = ""
    control_plane_oidc_issuer: str = ""
    control_plane_oidc_role_claim: str = "roles"
    """Which claim carries the operator's roles/groups. Okta: a custom claim
    you configure (commonly "groups") — not present by default. Entra ID:
    "roles" (app roles; prefer this over "groups", which is subject to claim
    overage for users in many groups). Keycloak: "realm_access.roles" (a
    dotted path into a nested object) for realm roles."""
    control_plane_oidc_required_role: str = "oac-operator"
    """The role/group value control_plane_oidc_role_claim must contain."""
