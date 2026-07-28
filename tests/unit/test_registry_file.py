from __future__ import annotations

import os
from pathlib import Path

import pytest

from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.domain.models import AgentStatus, RiskTier, ToolGrant

_YAML = """
agents:
  - spiffe_id: spiffe://corp.net/ns/finance/agent/invoice-bot
    display_name: Invoice Bot
    purpose: demo
    owner: alice@corp.net
    risk_tier: medium
    status: active
    granted_tools:
      - read_query
      - name: update_record
        required_roles: [finance-approver]
        risk_tier: high
  - spiffe_id: spiffe://corp.net/ns/x/agent/retired-bot
    display_name: Retired Bot
    purpose: demo
    owner: bob@corp.net
    risk_tier: high
    status: suspended
    granted_tools: []
"""


@pytest.fixture
def registry(tmp_path: Path) -> FileAgentRegistry:
    path = tmp_path / "agents.yaml"
    path.write_text(_YAML)
    return FileAgentRegistry(path)


@pytest.mark.asyncio
async def test_lookup_returns_registered_agent(registry: FileAgentRegistry) -> None:
    agent = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")

    assert agent is not None
    assert agent.owner == "alice@corp.net"
    assert agent.risk_tier is RiskTier.MEDIUM
    assert agent.tool_names == ["read_query", "update_record"]


@pytest.mark.asyncio
async def test_plain_string_and_object_grants_both_parse(registry: FileAgentRegistry) -> None:
    """A bare tool name and a per-grant object may coexist in the same list —
    see ADR-0021's backward-compatibility guarantee."""
    agent = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")

    assert agent is not None
    assert agent.granted_tools[0] == ToolGrant(name="read_query")
    assert agent.granted_tools[1] == ToolGrant(
        name="update_record", required_roles=["finance-approver"], risk_tier=RiskTier.HIGH
    )


@pytest.mark.asyncio
async def test_lookup_returns_suspended_status(registry: FileAgentRegistry) -> None:
    agent = await registry.lookup("spiffe://corp.net/ns/x/agent/retired-bot")

    assert agent is not None
    assert agent.status is AgentStatus.SUSPENDED


@pytest.mark.asyncio
async def test_lookup_unknown_agent_returns_none(registry: FileAgentRegistry) -> None:
    assert await registry.lookup("spiffe://corp.net/ns/x/agent/ghost") is None


@pytest.mark.asyncio
async def test_second_lookup_reuses_cached_parse(
    registry: FileAgentRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")

    def _fail(*args: object, **kwargs: object) -> str:
        raise AssertionError("re-read the registry file despite an unchanged mtime")

    monkeypatch.setattr(Path, "read_text", _fail)

    agent = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")
    assert agent is not None


@pytest.mark.asyncio
async def test_suspension_takes_effect_without_a_restart(tmp_path: Path) -> None:
    """Revocation must not require a redeploy — see ADR-0008."""
    path = tmp_path / "agents.yaml"
    path.write_text(_YAML)
    registry = FileAgentRegistry(path)

    before = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")
    assert before is not None and before.status is AgentStatus.ACTIVE

    path.write_text(_YAML.replace("status: active", "status: suspended"))
    # Force a distinct mtime: filesystem timestamp granularity can otherwise
    # make an immediate rewrite indistinguishable from the original.
    os.utime(path, (0, 0))

    after = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")
    assert after is not None and after.status is AgentStatus.SUSPENDED


@pytest.mark.asyncio
async def test_empty_file_yields_no_agents(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("")

    assert await FileAgentRegistry(path).lookup("spiffe://anything") is None


@pytest.mark.asyncio
async def test_repo_registry_file_is_valid() -> None:
    registry = FileAgentRegistry("registry/agents.yaml")

    agent = await registry.lookup("spiffe://corp.net/ns/finance/agent/invoice-bot")

    assert agent is not None and agent.status is AgentStatus.ACTIVE
