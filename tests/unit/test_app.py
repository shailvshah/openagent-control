from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from openagent_control.config import Settings
from openagent_control.diagnostics import Check
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
    assert settings.delegated_audience


def test_lifespan_closes_adapter_clients_on_shutdown() -> None:
    app = create_app(Settings())

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    # exiting the context runs shutdown -> Container.aclose(); nothing to assert
    # beyond it not raising, since the pools are internal to httpx


def test_container_aclose_tolerates_adapters_without_aclose() -> None:
    import asyncio
    from typing import Any

    from openagent_control.adapters.audit_export.stdout import StdoutAuditExporter
    from openagent_control.adapters.identity.header import HeaderIdentityProvider
    from openagent_control.adapters.ledger.ed25519_chain import Ed25519ChainLedger
    from openagent_control.adapters.registry.file import FileAgentRegistry
    from openagent_control.adapters.token_exchange.stub import StubTokenExchange
    from openagent_control.domain.models import PolicyDecision, ToolCallRequest
    from openagent_control.gateway.dependencies import Container

    class _Bare:
        async def evaluate(self, request: ToolCallRequest) -> PolicyDecision:
            raise NotImplementedError

        async def forward(self, request: ToolCallRequest, credential: str) -> dict[str, Any]:
            raise NotImplementedError

    container = Container(
        identity_provider=HeaderIdentityProvider(),
        agent_registry=FileAgentRegistry("registry/agents.yaml"),
        policy_engine=_Bare(),
        ledger=Ed25519ChainLedger(),
        audit_exporter=StdoutAuditExporter(),
        token_exchange=StubTokenExchange(),
        mcp_upstream=_Bare(),
    )

    asyncio.run(container.aclose())


def test_readyz_returns_503_when_a_dependency_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """An orchestrator must stop routing to a gateway that would 500."""

    async def one_bad(_settings: object) -> list[Check]:
        return [Check("opa", False, "connection refused"), Check("redis", True, "ping ok")]

    monkeypatch.setattr("openagent_control.gateway.app.run_all", one_bad)
    client = TestClient(create_app(), raise_server_exceptions=False)

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not ready"
    assert {"name": "opa", "ok": False, "detail": "connection refused"} in body["checks"]


def test_readyz_returns_200_when_every_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def all_ok(_settings: object) -> list[Check]:
        return [Check("opa", True, "HTTP 200")]

    monkeypatch.setattr("openagent_control.gateway.app.run_all", all_ok)
    client = TestClient(create_app(), raise_server_exceptions=False)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
