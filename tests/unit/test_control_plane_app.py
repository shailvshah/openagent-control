"""Route-level tests for the control-plane app against an in-memory SQLite
database (schema-translated, see adapters/db/session.py) — same approach as
gateway/app.py's test_app.py, but exercising real routes end to end through a
TestClient rather than unit-testing adapters in isolation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from openagent_control.adapters.db.tables import Base
from openagent_control.config import Settings
from openagent_control.control_plane.app import create_app

_API_KEY = "test-control-plane-key"
_AUTH_HEADER = {"Authorization": f"Bearer {_API_KEY}"}


@pytest.fixture
async def client() -> AsyncIterator[TestClient]:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        control_plane_operator_auth_mode="api-key",
        control_plane_api_key=_API_KEY,
    )
    app = create_app(settings)
    # The app's own engine is created inside create_app(); reach into the
    # container to create the schema before any request runs, same pattern
    # test_ledger_postgres.py uses for its own in-memory fixture.
    async with app.state.container.db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with TestClient(app) as test_client:
        yield test_client


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_unauthenticated_request_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/agents")

    assert response.status_code == 401


def test_wrong_api_key_is_rejected(client: TestClient) -> None:
    response = client.get("/api/v1/agents", headers={"Authorization": "Bearer wrong-key"})

    assert response.status_code == 401


def test_create_list_and_get_agent(client: TestClient) -> None:
    create_response = client.post(
        "/api/v1/agents",
        headers=_AUTH_HEADER,
        json={
            "spiffe_id": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "display_name": "Invoice Bot",
            "purpose": "demo",
            "owner": "alice@corp.net",
            "risk_tier": "medium",
            "granted_tools": ["read_query"],
        },
    )
    assert create_response.status_code == 201

    list_response = client.get("/api/v1/agents", headers=_AUTH_HEADER)
    assert [a["spiffe_id"] for a in list_response.json()] == [
        "spiffe://corp.net/ns/finance/agent/invoice-bot"
    ]

    get_response = client.get(
        "/api/v1/agents/spiffe://corp.net/ns/finance/agent/invoice-bot", headers=_AUTH_HEADER
    )
    assert get_response.status_code == 200
    assert get_response.json()["owner"] == "alice@corp.net"


def test_get_unknown_agent_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/agents/spiffe://corp.net/ns/x/agent/ghost", headers=_AUTH_HEADER)

    assert response.status_code == 404


def test_update_agent_patches_fields(client: TestClient) -> None:
    client.post(
        "/api/v1/agents",
        headers=_AUTH_HEADER,
        json={
            "spiffe_id": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "display_name": "Invoice Bot",
            "purpose": "demo",
            "owner": "alice@corp.net",
            "risk_tier": "medium",
        },
    )

    response = client.patch(
        "/api/v1/agents/spiffe://corp.net/ns/finance/agent/invoice-bot",
        headers=_AUTH_HEADER,
        json={"owner": "bob@corp.net"},
    )

    assert response.status_code == 200
    assert response.json()["owner"] == "bob@corp.net"
    assert response.json()["display_name"] == "Invoice Bot"


def test_suspend_and_activate_agent(client: TestClient) -> None:
    client.post(
        "/api/v1/agents",
        headers=_AUTH_HEADER,
        json={
            "spiffe_id": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "display_name": "Invoice Bot",
            "purpose": "demo",
            "owner": "alice@corp.net",
            "risk_tier": "medium",
        },
    )

    suspended = client.post(
        "/api/v1/agents/spiffe://corp.net/ns/finance/agent/invoice-bot/suspend",
        headers=_AUTH_HEADER,
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    activated = client.post(
        "/api/v1/agents/spiffe://corp.net/ns/finance/agent/invoice-bot/activate",
        headers=_AUTH_HEADER,
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"


def test_suspend_unknown_agent_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/v1/agents/spiffe://corp.net/ns/x/agent/ghost/suspend", headers=_AUTH_HEADER
    )

    assert response.status_code == 404


def test_receipts_search_and_verify_chain_on_an_empty_ledger(client: TestClient) -> None:
    search_response = client.get("/api/v1/receipts", headers=_AUTH_HEADER)
    assert search_response.status_code == 200
    assert search_response.json() == []

    verify_response = client.get("/api/v1/receipts/verify-chain", headers=_AUTH_HEADER)
    assert verify_response.status_code == 200
    assert verify_response.json() == {
        "valid": True,
        "receipts_checked": 0,
        "first_broken_sequence_id": None,
    }


def test_get_unknown_receipt_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/receipts/no-such-id", headers=_AUTH_HEADER)

    assert response.status_code == 404


def test_fleet_summary_reflects_registered_agents(client: TestClient) -> None:
    client.post(
        "/api/v1/agents",
        headers=_AUTH_HEADER,
        json={
            "spiffe_id": "spiffe://corp.net/ns/finance/agent/invoice-bot",
            "display_name": "Invoice Bot",
            "purpose": "demo",
            "owner": "alice@corp.net",
            "risk_tier": "medium",
        },
    )

    response = client.get("/api/v1/fleet/summary", headers=_AUTH_HEADER)

    assert response.status_code == 200
    body = response.json()
    assert body["agents_by_status"] == {"active": 1}
    assert body["receipts_last_24h_by_decision"] == {}
    assert body["last_receipt_timestamp"] is None


def test_build_control_plane_container_requires_database_url() -> None:
    from openagent_control.control_plane.dependencies import build_control_plane_container

    with pytest.raises(RuntimeError, match="OAC_DATABASE_URL is required"):
        build_control_plane_container(Settings(database_url=""))


def test_readyz_reports_ready_when_dependencies_are_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openagent_control.diagnostics import Check

    async def all_ok(_settings: object) -> list[Check]:
        return [Check("database", True, "at head")]

    monkeypatch.setattr("openagent_control.control_plane.app.run_all", all_ok)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readyz_returns_503_when_a_dependency_is_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openagent_control.diagnostics import Check

    async def one_bad(_settings: object) -> list[Check]:
        return [Check("database", False, "un-migrated")]

    monkeypatch.setattr("openagent_control.control_plane.app.run_all", one_bad)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not ready"


def test_operator_auth_mode_oidc_selects_oidc_operator_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the mode-selection branch is under test here — not
    OidcOperatorAuth's own construction, which is covered against a real
    local IdP in test_operator_identity.py. Faking the class avoids a real
    network call to a discovery endpoint that doesn't exist."""

    class _FakeOidcOperatorAuth:
        def __init__(self, **_kwargs: object) -> None:
            self.built = True

    monkeypatch.setattr(
        "openagent_control.adapters.operator_identity.oidc.OidcOperatorAuth",
        _FakeOidcOperatorAuth,
    )
    from openagent_control.control_plane.dependencies import _operator_auth

    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        control_plane_operator_auth_mode="oidc-jwks",
        control_plane_oidc_discovery_url="http://example.invalid/.well-known/openid-configuration",
    )

    auth = _operator_auth(settings)

    assert isinstance(auth, _FakeOidcOperatorAuth)
