from __future__ import annotations

from pathlib import Path

import pytest

from openagent_control.adapters.registry.file import FileAgentRegistry
from openagent_control.domain.models import AgentStatus, RiskTier

_YAML = """
agents:
  - spiffe_id: spiffe://corp.net/ns/finance/agent/invoice-bot
    display_name: Invoice Bot
    purpose: demo
    owner: alice@corp.net
    risk_tier: medium
    status: active
    granted_tools: [read_query]
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
    assert agent.granted_tools == ["read_query"]


@pytest.mark.asyncio
async def test_lookup_returns_suspended_status(registry: FileAgentRegistry) -> None:
    agent = await registry.lookup("spiffe://corp.net/ns/x/agent/retired-bot")

    assert agent is not None
    assert agent.status is AgentStatus.SUSPENDED


@pytest.mark.asyncio
async def test_lookup_unknown_agent_returns_none(registry: FileAgentRegistry) -> None:
    assert await registry.lookup("spiffe://corp.net/ns/x/agent/ghost") is None


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
