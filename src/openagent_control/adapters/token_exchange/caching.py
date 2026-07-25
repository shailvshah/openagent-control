"""Redis-caching decorator over any TokenExchange. See docs/adr/0009.

Cache TTL is derived from the exchanged token's own `exp` claim (peeked via an
unverified JWT decode — we already trust this token because it came straight
from the IdP's response over TLS; we are not re-validating its signature, only
reading a cache-lifetime hint), minus a safety margin, capped at a configured
maximum. Opaque (non-JWT) tokens fall back to the safety-margin value as a short,
conservative TTL. Never caches past a token's real expiry.
"""

from __future__ import annotations

import hashlib
import time

import jwt
from redis.asyncio import Redis

from openagent_control.domain.ports import TokenExchange

_KEY_PREFIX = "oac:token-exchange:"


def _cache_key(subject_token: str, audience: str) -> str:
    digest = hashlib.sha256(f"{subject_token}:{audience}".encode()).hexdigest()
    return _KEY_PREFIX + digest


def _ttl_from_token(token: str, max_ttl: int, safety_margin: int) -> int:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        exp = int(claims["exp"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        return safety_margin

    remaining = exp - int(time.time()) - safety_margin
    return max(0, min(remaining, max_ttl))


class CachingTokenExchange:
    def __init__(
        self,
        inner: TokenExchange,
        redis: Redis,
        max_ttl_seconds: int,
        safety_margin_seconds: int,
    ) -> None:
        self._inner = inner
        self._redis = redis
        self._max_ttl_seconds = max_ttl_seconds
        self._safety_margin_seconds = safety_margin_seconds

    async def exchange(self, subject_token: str, audience: str) -> str:
        key = _cache_key(subject_token, audience)
        cached = await self._redis.get(key)
        if cached is not None:
            # A client without decode_responses=True hands back bytes; str() on
            # bytes would corrupt the credential into "b'...'", so decode
            # explicitly instead of coercing.
            return cached.decode() if isinstance(cached, bytes) else str(cached)

        token = await self._inner.exchange(subject_token, audience)
        ttl = _ttl_from_token(token, self._max_ttl_seconds, self._safety_margin_seconds)
        if ttl > 0:
            await self._redis.set(key, token, ex=ttl)
        return token

    async def aclose(self) -> None:
        """Forwards to the wrapped adapter; the decorator owns no resources of
        its own (the Redis client is shared and closed once by the Container)."""
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()
