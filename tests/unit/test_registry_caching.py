"""CachingAgentRegistry, exercised against a minimal in-memory fake Redis client
(get/set with TTL only — the subset the decorator uses) rather than a running
Redis server.
"""

from __future__ import annotations

import pytest

from openagent_control.adapters.registry.caching import CachingAgentRegistry
from openagent_control.domain.models import AgentStatus, RegisteredAgent, RiskTier

_AGENT_ID = "spiffe://corp.net/ns/finance/agent/invoice-bot"


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex


class CountingRegistry:
    def __init__(self, agent: RegisteredAgent | None) -> None:
        self._agent = agent
        self.calls = 0

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        self.calls += 1
        return self._agent


def _agent() -> RegisteredAgent:
    return RegisteredAgent(
        spiffe_id=_AGENT_ID,
        display_name="Invoice Bot",
        purpose="demo",
        owner="alice@corp.net",
        risk_tier=RiskTier.MEDIUM,
        status=AgentStatus.ACTIVE,
        granted_tools=["read_query"],
    )


@pytest.mark.asyncio
async def test_cache_miss_calls_inner_and_populates_cache() -> None:
    redis = FakeRedis()
    inner = CountingRegistry(_agent())
    cache = CachingAgentRegistry(inner, redis, ttl_seconds=30)  # type: ignore[arg-type]

    result = await cache.lookup(_AGENT_ID)

    assert result is not None and result.spiffe_id == _AGENT_ID
    assert inner.calls == 1
    assert redis.ttls["oac:registry:" + _AGENT_ID] == 30


@pytest.mark.asyncio
async def test_cache_hit_skips_inner() -> None:
    redis = FakeRedis()
    inner = CountingRegistry(_agent())
    cache = CachingAgentRegistry(inner, redis, ttl_seconds=30)  # type: ignore[arg-type]

    await cache.lookup(_AGENT_ID)
    await cache.lookup(_AGENT_ID)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_unknown_agent_is_negatively_cached() -> None:
    redis = FakeRedis()
    inner = CountingRegistry(None)
    cache = CachingAgentRegistry(inner, redis, ttl_seconds=30)  # type: ignore[arg-type]

    first = await cache.lookup("spiffe://corp.net/ns/x/agent/ghost")
    second = await cache.lookup("spiffe://corp.net/ns/x/agent/ghost")

    assert first is None
    assert second is None
    assert inner.calls == 1
