from __future__ import annotations

from fastapi.testclient import TestClient

from openagent_control.config import Settings
from openagent_control.gateway.app import create_app


def test_healthz() -> None:
    client = TestClient(create_app(Settings()))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_default_settings_have_local_dev_urls() -> None:
    settings = Settings()

    assert settings.opa_url.startswith("http://")
    assert settings.mcp_upstream_url.startswith("http://")
