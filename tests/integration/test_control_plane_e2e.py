"""The loop this whole feature exists to close (ADR-0009 -> ADR-0014):

    create an agent via the control-plane API
        -> the real gateway (real OPA, real Postgres) now allows it
    suspend the agent via the control-plane API
        -> the real gateway now denies it, receipted

Before this feature, ADR-0009 was explicit that suspending an agent meant
hand-editing a database row directly — there was no HTTP surface that could
do it. This test proves the operator surface actually closes that gap, using
real components throughout (real OPA process, real Postgres), not fakes.

Opt-in, same convention as test_diagnostics_backends.py:

    OAC_TEST_DATABASE_URL=postgresql+asyncpg://oac:oac@localhost:5432/oac \\
    poetry run pytest tests/integration/test_control_plane_e2e.py
"""

from __future__ import annotations

import os
import uuid

import pytest
from examples.enterprise_scenario.harness import run_gateway, run_opa
from fastapi.testclient import TestClient

from openagent_control.config import Settings
from openagent_control.control_plane.app import create_app

DATABASE_URL = os.environ.get("OAC_TEST_DATABASE_URL", "")
_API_KEY = "e2e-test-control-plane-key"
_AUTH_HEADER = {"Authorization": f"Bearer {_API_KEY}"}

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="set OAC_TEST_DATABASE_URL")


def _tool_call_payload(request_id: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "read_query", "arguments": {"table": "invoices"}},
    }


def test_create_allow_suspend_deny_loop() -> None:
    spiffe_id = f"spiffe://corp.net/ns/finance/agent/e2e-{uuid.uuid4().hex[:8]}"

    # All control-plane requests must run through the same TestClient context
    # (one portal/event loop) — a real asyncpg engine's connections are bound
    # to whichever loop first used them, and calling a bare (non-context-
    # managed) TestClient instance can hand different calls to different
    # loops, which asyncpg rejects outright.
    with (
        TestClient(
            create_app(
                Settings(
                    database_url=DATABASE_URL,
                    control_plane_operator_auth_mode="api-key",
                    control_plane_api_key=_API_KEY,
                )
            )
        ) as control_plane,
        run_opa() as opa_url,
        run_gateway(
            Settings(
                database_url=DATABASE_URL,
                opa_url=opa_url,
                identity_mode="header",
            )
        ) as gateway_url,
    ):
        import httpx

        # 1. Before registration: the registry gate denies (ADR-0008), never
        # even reaching the policy engine.
        first_call = httpx.post(
            f"{gateway_url}/mcp/v1",
            json=_tool_call_payload(1),
            headers={"x-spiffe-id": spiffe_id},
            timeout=10.0,
        ).json()
        assert "error" in first_call
        assert "not registered" in first_call["error"]["message"]

        # 2. Register the agent through the control-plane API — the surface
        # ADR-0009 said didn't exist yet.
        create_response = control_plane.post(
            "/api/v1/agents",
            headers=_AUTH_HEADER,
            json={
                "spiffe_id": spiffe_id,
                "display_name": "E2E Test Bot",
                "purpose": "control-plane e2e test",
                "owner": "test-harness@corp.net",
                "risk_tier": "low",
                "granted_tools": ["read_query"],
            },
        )
        assert create_response.status_code == 201

        # 3. The real gateway, with no restart or cache to wait out (no Redis
        # configured here), now allows the same call.
        second_call = httpx.post(
            f"{gateway_url}/mcp/v1",
            json=_tool_call_payload(2),
            headers={"x-spiffe-id": spiffe_id},
            timeout=10.0,
        ).json()
        assert "error" not in second_call or second_call["error"]["code"] != -32000

        # 4. Suspend the agent through the control-plane API.
        suspend_response = control_plane.post(
            f"/api/v1/agents/{spiffe_id}/suspend", headers=_AUTH_HEADER
        )
        assert suspend_response.status_code == 200
        assert suspend_response.json()["status"] == "suspended"

        # 5. The real gateway now denies it again.
        third_call = httpx.post(
            f"{gateway_url}/mcp/v1",
            json=_tool_call_payload(3),
            headers={"x-spiffe-id": spiffe_id},
            timeout=10.0,
        ).json()
        assert "error" in third_call
        assert "suspended" in third_call["error"]["message"]

        # 6. The control plane's own receipt search shows exactly this
        # history — dogfooding the receipt-search endpoint this feature also
        # adds.
        #
        # Deliberately not asserting verify-chain here: with the default
        # signing_key_mode="in-process", the gateway and the control plane
        # each generate their own independent random key at startup, so the
        # control plane's public key can never verify signatures the
        # gateway's process produced — cross-process signature verification
        # is only meaningful with signing_key_mode="vault-transit"
        # (ADR-0013), where both processes read the same key from Vault.
        # verify_chain()'s own correctness (given a shared signer) is covered
        # in test_ledger_postgres_query.py.
        receipts_response = control_plane.get(
            "/api/v1/receipts", headers=_AUTH_HEADER, params={"spiffe_id": spiffe_id, "limit": 10}
        )
        assert receipts_response.status_code == 200
        decisions = [r["decision"] for r in receipts_response.json()]
        # newest first: suspended-deny, allow, orphan-deny
        assert decisions == ["DENY", "ALLOW", "DENY"]
