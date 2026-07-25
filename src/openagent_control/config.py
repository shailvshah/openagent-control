from __future__ import annotations

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAC_")

    opa_url: str = "http://localhost:8181/v1/data/openagent/authz"
    mcp_upstream_url: str = "http://localhost:8080"
    registry_path: str = "registry/agents.yaml"
    """Master Agent Registry file (ADR-0008)."""

    delegated_audience: str = "openagent-control-mcp-upstream"
    """OAuth audience requested in token exchanges for delegated (OBO) calls.
    For Entra this maps to the scope parameter (e.g. "api://<app-id>/.default")."""

    identity_mode: Literal["header", "jwt-svid"] = "header"
    """"header" trusts X-Spiffe-ID (dev / behind an attesting mesh only, ADR-0005);
    "jwt-svid" cryptographically validates a SPIFFE JWT-SVID bearer token."""
    jwt_svid_public_key_path: str = ""
    """PEM public key (SPIRE trust-bundle key) for identity_mode="jwt-svid"."""
    jwt_svid_audience: str = "openagent-control"

    token_exchange_mode: Literal["stub", "rfc8693", "entra"] = "stub"
    """"rfc8693" = Okta-compatible RFC 8693 exchange; "entra" = Microsoft OBO flow."""
    token_exchange_url: str = ""
    token_exchange_client_id: str = ""
    token_exchange_client_secret: str = ""
