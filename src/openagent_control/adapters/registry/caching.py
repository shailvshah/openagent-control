"""Redis-caching decorator over any AgentRegistry. See docs/adr/0009.

Wraps — does not replace — the file or Postgres registry, so caching composes
with either backend unchanged. Short TTL (default 30s) bounds how stale a status
read can be; there is no invalidate-on-write yet, which is a deliberate,
documented trade-off (see the ADR) until an admin/kill-switch surface exists.
"""

from __future__ import annotations

from redis.asyncio import Redis

from openagent_control.domain.models import RegisteredAgent
from openagent_control.domain.ports import AgentRegistry

_KEY_PREFIX = "oac:registry:"


class CachingAgentRegistry:
    def __init__(self, inner: AgentRegistry, redis: Redis, ttl_seconds: int) -> None:
        self._inner = inner
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def lookup(self, spiffe_id: str) -> RegisteredAgent | None:
        key = _KEY_PREFIX + spiffe_id
        cached = await self._redis.get(key)
        if cached is not None:
            # Empty string is the cached-negative marker (agent confirmed absent).
            return RegisteredAgent.model_validate_json(cached) if cached else None

        agent = await self._inner.lookup(spiffe_id)
        await self._redis.set(key, agent.model_dump_json() if agent else "", ex=self._ttl_seconds)
        return agent
