"""Redis cache layer: prompt -> result, with single-flight and graceful degradation.

Every Redis error is swallowed (logged); callers behave as on a cache miss.
Key = hash(normalized query + user_id + role + catalog version), so users never share entries.
"""
import asyncio
import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.core.config import settings
from app.core.logger import logger


class CacheService:
    def __init__(self, redis_client=None, default_ttl: Optional[int] = None, backoff_seconds: float = 0.0):
        self._client = redis_client
        self.default_ttl = default_ttl if default_ttl is not None else settings.CACHE_TTL_SECONDS
        self._backoff = backoff_seconds  # skip Redis this long after a failure (0 = always try)
        self._down_until = 0.0
        self._inflight: Dict[str, "asyncio.Future"] = {}

    def _redis(self):
        if self._client is None:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(
                settings.REDIS_URL, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1,
            )
        return self._client

    @staticmethod
    def make_key(query: str, user_id: str, role: str, catalog_version: str) -> str:
        normalized = " ".join((query or "").lower().split())
        raw = "\x1f".join([normalized, str(user_id), str(role), str(catalog_version)])
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"nl2sql:q:{catalog_version}:{digest}"

    def _available(self) -> bool:
        return time.monotonic() >= self._down_until

    def _mark_down(self, err: Exception):
        self._down_until = time.monotonic() + self._backoff
        logger.warning(f"Cache unavailable, continuing without it: {err}")

    async def get(self, key: str) -> Optional[Any]:
        if not self._available():
            return None
        try:
            raw = await self._redis().get(key)
        except Exception as e:
            self._mark_down(e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        if not self._available():
            return
        try:
            await self._redis().set(key, json.dumps(value, default=str), ex=ttl or self.default_ttl)
        except Exception as e:
            self._mark_down(e)

    async def get_or_compute(self, key: str, compute: Callable[[], Awaitable[Any]],
                             ttl: Optional[int] = None) -> Tuple[Any, bool]:
        """Return (value, hit); hit is True when this call did no computation (cache or a concurrent caller's result).

        Concurrent callers for one key share a single cache lookup and a single `compute`
        (per process). The in-flight marker is registered before any await, so there is no race window.
        """
        pending = self._inflight.get(key)
        if pending is not None:
            value, _ = await asyncio.shield(pending)
            return value, True  # served by another caller's computation: this call did no work
        fut: "asyncio.Future" = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            cached = await self.get(key)
            if cached is not None:
                res = (cached, True)
            else:
                value = await compute()
                await self.set(key, value, ttl)
                res = (value, False)
            fut.set_result(res)
            return res
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # mark retrieved so no "never retrieved" warning
            raise
        finally:
            self._inflight.pop(key, None)

    async def single_flight(self, key: str, compute: Callable[[], Awaitable[Any]],
                            ttl: Optional[int] = None) -> Any:
        return (await self.get_or_compute(key, compute, ttl))[0]


cache_service = CacheService()
