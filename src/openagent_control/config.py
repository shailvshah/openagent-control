from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAC_")

    opa_url: str = "http://localhost:8181/v1/data/openagent/authz"
    mcp_upstream_url: str = "http://localhost:8080"
