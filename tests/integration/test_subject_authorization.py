"""The user's own entitlements deciding a call, evaluated by real OPA (ADR-0019).

The point of separating approval from authorization is that a rule can be
written against the *user's* roles and scopes, not just the agent's grants. A
unit test with a mocked policy engine cannot show that — it would only prove
this project agrees with its own assumptions about Rego. These run a real `opa`
process over a real policy that gates on `input.subject`.

The absent-subject case is the one that matters most. `input.subject` is null
for an autonomous call, and in Rego `not "x" in null.roles` is *undefined*
rather than true — so a naively written entitlement check silently fails to
fire and an autonomous agent sails past a rule meant to constrain it. That is
tested here explicitly.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest
from examples.enterprise_scenario.harness import free_port, wait_for

from openagent_control.adapters.policy.opa import OPAPolicyEngine
from openagent_control.domain.models import (
    AgentIdentity,
    Decision,
    PolicyDecision,
    RegisteredAgent,
    RiskTier,
    SubjectIdentity,
    ToolCallRequest,
)

pytestmark = pytest.mark.skipif(
    shutil.which("opa") is None, reason="requires the real `opa` binary (brew install opa)"
)

_ISSUER = "https://idp.corp.net/realms/corp"

# A policy of the shape docs/adr/0019 and the shipped Rego's commented example
# recommend: the registry grant is necessary, the user's entitlement is also
# necessary, and "no subject at all" is spelled out rather than left undefined.
_POLICY = """
package openagent.authz

import rego.v1

default allow := false

allow if {
	input.method == "tools/list"
}

allow if {
	input.method == "tools/call"
	input.agent.status == "active"
	granted(input.params.name)
	not guardrail_violation(input.params.name, input.params.arguments)
}

granted(tool) if {
	some t in input.agent.granted_tools
	t.name == tool
}

reason := "Capability not granted for this agent identity" if {
	input.method == "tools/call"
	not granted(input.params.name)
}

reason := "Acting user is not entitled to this tool" if {
	input.method == "tools/call"
	granted(input.params.name)
	guardrail_violation(input.params.name, input.params.arguments)
}

# An autonomous call has no user authority to act under.
guardrail_violation("update_record", _) if {
	input.subject == null
}

guardrail_violation("update_record", _) if {
	input.subject != null
	not "finance-approver" in input.subject.roles
}

guardrail_violation("read_query", _) if {
	input.subject != null
	not "invoices:read" in input.subject.scopes
}
"""


@pytest.fixture(scope="module")
def opa_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    policy_dir = tmp_path_factory.mktemp("policy")
    (policy_dir / "subject_authz.rego").write_text(_POLICY)
    port = free_port()
    process = subprocess.Popen(
        ["opa", "run", "--server", "--addr", f"127.0.0.1:{port}", str(policy_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}/v1/data/openagent/authz"
    finally:
        process.terminate()
        process.wait(timeout=10)


def _registration(tools: list[Any]) -> RegisteredAgent:
    return RegisteredAgent(
        spiffe_id=f"oidc://{_ISSUER}/invoice-bot",
        display_name="Invoice Bot",
        purpose="test",
        owner="alice@corp.net",
        risk_tier=RiskTier.MEDIUM,
        granted_tools=tools,
    )


async def _decide(
    opa_url: str,
    tool: str,
    *,
    subject: SubjectIdentity | None,
    granted: list[Any],
) -> PolicyDecision:
    engine = OPAPolicyEngine(opa_url=opa_url)
    try:
        return await engine.evaluate(
            ToolCallRequest(
                method="tools/call",
                tool_name=tool,
                arguments={},
                agent=AgentIdentity(
                    spiffe_id=f"oidc://{_ISSUER}/invoice-bot",
                    human_sponsor=f"{_ISSUER}#dana",
                    client_id="invoice-bot",
                ),
                registration=_registration(granted),
                subject=subject,
            )
        )
    finally:
        await engine.aclose()


def _user(roles: list[str], scopes: list[str] | None = None) -> SubjectIdentity:
    return SubjectIdentity(
        subject_id=f"{_ISSUER}#dana",
        issuer=_ISSUER,
        roles=roles,
        scopes=scopes or [],
    )


@pytest.mark.asyncio
async def test_an_entitled_user_is_allowed(opa_url: str) -> None:
    """Agent grant AND user entitlement — the intersection delegation implies."""
    decision = await _decide(
        opa_url, "update_record", subject=_user(["finance-approver"]), granted=["update_record"]
    )

    assert decision.decision is Decision.ALLOW


@pytest.mark.asyncio
async def test_an_unentitled_user_is_denied_even_though_the_agent_is_granted(
    opa_url: str,
) -> None:
    """The whole model in one test: the agent may call this tool, the human may
    not, so the call is refused. Approval is not entitlement."""
    decision = await _decide(
        opa_url, "update_record", subject=_user(["viewer"]), granted=["update_record"]
    )

    assert decision.decision is Decision.DENY
    assert decision.reason == "Acting user is not entitled to this tool"


@pytest.mark.asyncio
async def test_an_entitled_user_is_still_denied_a_tool_the_agent_lacks(opa_url: str) -> None:
    """The converse: a powerful user does not widen the agent's own grant."""
    decision = await _decide(
        opa_url, "update_record", subject=_user(["finance-approver"]), granted=["read_query"]
    )

    assert decision.decision is Decision.DENY
    assert decision.reason == "Capability not granted for this agent identity"


@pytest.mark.asyncio
async def test_an_autonomous_call_has_no_user_authority_to_draw_on(opa_url: str) -> None:
    """With `input.subject` null, a rule written as `not "x" in input.subject.roles`
    would be *undefined* and quietly not fire. The policy says so explicitly,
    and this asserts the deny actually happens."""
    decision = await _decide(opa_url, "update_record", subject=None, granted=["update_record"])

    assert decision.decision is Decision.DENY
    assert decision.reason == "Acting user is not entitled to this tool"


@pytest.mark.asyncio
async def test_a_users_oauth_scope_can_gate_a_call(opa_url: str) -> None:
    """Scopes are the better lever when the entitlement is already modelled in
    the IdP on the user's own token (RFC 9068 `scope`)."""
    allowed = await _decide(
        opa_url, "read_query", subject=_user([], ["invoices:read"]), granted=["read_query"]
    )
    denied = await _decide(
        opa_url, "read_query", subject=_user([], ["profile"]), granted=["read_query"]
    )

    assert allowed.decision is Decision.ALLOW
    assert denied.decision is Decision.DENY
