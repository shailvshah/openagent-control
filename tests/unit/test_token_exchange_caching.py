"""CachingTokenExchange against a minimal in-memory fake Redis client."""

from __future__ import annotations

import datetime

import jwt
import pytest

from openagent_control.adapters.token_exchange.caching import CachingTokenExchange


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


class CountingTokenExchange:
    def __init__(self, token: str) -> None:
        self._token = token
        self.calls = 0
        self.closed = False

    async def exchange(self, subject_token: str, audience: str) -> str:
        self.calls += 1
        return self._token

    async def aclose(self) -> None:
        self.closed = True


def _jwt(expires_in: int) -> str:
    exp = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=expires_in)
    return jwt.encode({"sub": "x", "exp": exp}, "unused-secret", algorithm="HS256")


@pytest.mark.asyncio
async def test_cache_miss_calls_inner_and_ttl_derives_from_exp() -> None:
    redis = FakeRedis()
    token = _jwt(expires_in=300)
    inner = CountingTokenExchange(token)
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    result = await cache.exchange("subject-tok", "aud")

    assert result == token
    assert inner.calls == 1
    ((_key, ttl),) = redis.ttls.items()
    assert 260 <= ttl <= 270  # 300 - 30 safety margin, minus a little test latency


@pytest.mark.asyncio
async def test_cache_hit_skips_inner() -> None:
    redis = FakeRedis()
    inner = CountingTokenExchange(_jwt(expires_in=300))
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    await cache.exchange("subject-tok", "aud")
    await cache.exchange("subject-tok", "aud")

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_ttl_is_capped_at_max_ttl() -> None:
    redis = FakeRedis()
    inner = CountingTokenExchange(_jwt(expires_in=10_000))
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=60, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    await cache.exchange("subject-tok", "aud")

    assert list(redis.ttls.values()) == [60]


@pytest.mark.asyncio
async def test_opaque_token_falls_back_to_safety_margin_ttl() -> None:
    redis = FakeRedis()
    inner = CountingTokenExchange("opaque-not-a-jwt-token")
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=45  # type: ignore[arg-type]
    )

    await cache.exchange("subject-tok", "aud")

    assert list(redis.ttls.values()) == [45]


@pytest.mark.asyncio
async def test_already_expired_token_is_not_cached() -> None:
    redis = FakeRedis()
    token = _jwt(expires_in=-60)
    inner = CountingTokenExchange(token)
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    result = await cache.exchange("subject-tok", "aud")

    assert result == token
    assert redis.ttls == {}


@pytest.mark.asyncio
async def test_different_subject_or_audience_are_different_cache_keys() -> None:
    redis = FakeRedis()
    inner = CountingTokenExchange(_jwt(expires_in=300))
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    await cache.exchange("subject-a", "aud-1")
    await cache.exchange("subject-b", "aud-1")
    await cache.exchange("subject-a", "aud-2")

    assert inner.calls == 3


@pytest.mark.asyncio
async def test_aclose_forwards_to_inner() -> None:
    redis = FakeRedis()
    inner = CountingTokenExchange(_jwt(expires_in=300))
    cache = CachingTokenExchange(
        inner, redis, max_ttl_seconds=600, safety_margin_seconds=30  # type: ignore[arg-type]
    )

    await cache.aclose()

    assert inner.closed is True


class _NoCloseTokenExchange:
    async def exchange(self, subject_token: str, audience: str) -> str:
        return "token"


@pytest.mark.asyncio
async def test_aclose_tolerates_inner_without_aclose() -> None:
    redis = FakeRedis()
    cache = CachingTokenExchange(
        _NoCloseTokenExchange(),
        redis,  # type: ignore[arg-type]
        max_ttl_seconds=600,
        safety_margin_seconds=30,
    )

    await cache.aclose()
