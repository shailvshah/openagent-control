"""The shipped Rego policy, evaluated by a real `opa` process (ADR-0002).

Every other policy test mocks OPA's HTTP response, which proves how the adapter
parses a decision but nothing about the decision itself — the rules in
`resources/policies/mcp_authz.rego` had no automated coverage at all. This file
covers the rules, against the real engine that will evaluate them in production.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Any

import pytest
from examples.enterprise_scenario.harness import run_opa

from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.domain.models import (
    AgentIdentity,
    Decision,
    PolicyDecision,
    RegisteredAgent,
    RiskTier,
    ToolCallRequest,
)

pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)

_SPIFFE = "spiffe://corp.net/ns/sales/agent/lead-qualifier"


@pytest.fixture(scope="module")
def opa_url() -> Iterator[str]:
    with run_opa() as url:
        yield url


async def _decide(
    opa_url: str, method: str, tool: str | None, arguments: dict[str, Any], granted: list[str]
) -> PolicyDecision:
    engine = OPAPolicyEngine(opa_url=opa_url)
    try:
        return await engine.evaluate(
            ToolCallRequest(
                method=method,
                tool_name=tool,
                arguments=arguments,
                agent=AgentIdentity(spiffe_id=_SPIFFE),
                registration=RegisteredAgent(
                    spiffe_id=_SPIFFE,
                    display_name="Lead Qualifier",
                    purpose="test",
                    owner="bob@corp.net",
                    risk_tier=RiskTier.LOW,
                    granted_tools=granted,
                ),
            )
        )
    finally:
        await engine.aclose()


@pytest.mark.asyncio
async def test_discovery_is_always_allowed(opa_url: str) -> None:
    decision = await _decide(opa_url, "tools/list", None, {}, granted=[])

    assert decision.decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_a_granted_tool_with_no_guardrail_rule_is_allowed(opa_url: str) -> None:
    """The registry grant is the allowlist (ADR-0008). Requiring a matching
    Rego rule per tool meant a registry grant silently did nothing — the
    regression this asserts against, since it made every new tool and every new
    upstream (ADR-0016) a two-system change."""
    decision = await _decide(
        opa_url, "tools/call", "lookup_account", {"customer": "ACME"}, granted=["lookup_account"]
    )

    assert decision.decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_an_ungranted_tool_is_denied_with_the_capability_reason(opa_url: str) -> None:
    decision = await _decide(
        opa_url, "tools/call", "delete_everything", {}, granted=["lookup_account"]
    )

    assert decision.decision is Decision.DENY
    assert decision.reason == "Capability not granted for this agent identity"


@pytest.mark.asyncio
async def test_arguments_within_the_guardrail_are_allowed(opa_url: str) -> None:
    decision = await _decide(
        opa_url,
        "tools/call",
        "salesforce_update_account",
        {"account": "ACME", "credit_limit": 9_000},
        granted=["salesforce_update_account"],
    )

    assert decision.decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_arguments_beyond_the_guardrail_are_denied(opa_url: str) -> None:
    """The security-critical half of the rewrite: guardrails must still narrow
    a grant, not merely fail open now that unlisted tools are permitted."""
    decision = await _decide(
        opa_url,
        "tools/call",
        "salesforce_update_account",
        {"account": "ACME", "credit_limit": 50_000},
        granted=["salesforce_update_account"],
    )

    assert decision.decision is Decision.DENY
    assert decision.reason == "Tool arguments exceed authorized thresholds"


@pytest.mark.asyncio
async def test_a_guarded_tool_called_with_no_arguments_is_allowed(opa_url: str) -> None:
    """An absent `credit_limit` must not be read as an unbounded one."""
    decision = await _decide(
        opa_url,
        "tools/call",
        "salesforce_update_account",
        {},
        granted=["salesforce_update_account"],
    )

    assert decision.decision is Decision.ALLOW
