from __future__ import annotations

import httpx
import pytest

from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.domain.errors import PolicyEngineUnavailableError
from openagent_control.domain.models import AgentIdentity, Decision, ToolCallRequest, ToolGrant


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


@pytest.mark.asyncio
async def test_registry_facts_are_included_in_opa_input() -> None:
    from openagent_control.domain.models import RegisteredAgent, RiskTier

    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["input"] = json.loads(request.content)["input"]
        return httpx.Response(200, json={"result": {"allow": True}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = OPAPolicyEngine(opa_url="http://opa.test/v1/data/openagent/authz", client=client)
    call = _call()
    call.registration = RegisteredAgent(
        spiffe_id=call.agent.spiffe_id,
        display_name="Invoice Bot",
        purpose="demo",
        owner="alice@corp.net",
        risk_tier=RiskTier.MEDIUM,
        granted_tools=[ToolGrant(name="read_query")],
    )

    await engine.evaluate(call)

    agent_facts = seen["input"]["agent"]  # type: ignore[index]
    assert [t["name"] for t in agent_facts["granted_tools"]] == ["read_query"]
    assert agent_facts["status"] == "active"


@pytest.mark.asyncio
async def test_unreachable_opa_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = OPAPolicyEngine(opa_url="http://opa.test/v1/data/openagent/authz", client=client)

    with pytest.raises(PolicyEngineUnavailableError):
        await engine.evaluate(_call())


@pytest.mark.asyncio
async def test_opa_server_error_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    engine = OPAPolicyEngine(opa_url="http://opa.test/v1/data/openagent/authz", client=client)

    with pytest.raises(PolicyEngineUnavailableError):
        await engine.evaluate(_call())


@pytest.mark.asyncio
async def test_aclose_releases_client() -> None:
    engine = _engine({"allow": True})

    await engine.aclose()
