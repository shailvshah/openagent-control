from __future__ import annotations

import httpx
import pytest

from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.domain.models import AgentIdentity, Decision, ToolCallRequest


def _call() -> ToolCallRequest:
    return ToolCallRequest(
        method="tools/call",
        tool_name="read_query",
        arguments={},
        agent=AgentIdentity(spiffe_id="spiffe://corp.net/ns/finance/agent/invoice-bot"),
    )


def _engine(result: dict[str, object]) -> OPAPolicyEngine:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": result})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OPAPolicyEngine(opa_url="http://opa.test/v1/data/openagent/authz", client=client)


@pytest.mark.asyncio
async def test_allow_decision_maps_from_opa_result() -> None:
    engine = _engine({"allow": True})

    decision = await engine.evaluate(_call())

    assert decision.decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_deny_decision_carries_reason() -> None:
    engine = _engine({"allow": False, "reason": "velocity_limit"})

    decision = await engine.evaluate(_call())

    assert decision.decision is Decision.DENY
    assert decision.reason == "velocity_limit"


@pytest.mark.asyncio
async def test_deny_decision_defaults_reason_when_missing() -> None:
    engine = _engine({"allow": False})

    decision = await engine.evaluate(_call())

    assert decision.reason == "Denied by policy"
