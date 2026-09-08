"""
Tiny TTL cache in front of SorobanContractClient.get_policy_state().

Every /relay call needs a fresh-enough policy snapshot for the local
pre-check, but re-reading the chain on every single request adds latency
this hot path doesn't need. The cache window is intentionally short
(config: policy_cache_ttl_seconds, default 2s) and only ever affects the
fast pre-check — check_and_record_spend() always hits the chain directly
and is never served from this cache.
"""
from __future__ import annotations

import asyncio
import time

from app.services.soroban_client import PolicyState, SorobanContractClient


class PolicyStateCache:
    def __init__(self, client: SorobanContractClient, ttl_seconds: float):
        self._client = client
        self._ttl = ttl_seconds
        self._value: PolicyState | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, *, force_refresh: bool = False) -> PolicyState:
        now = time.monotonic()
        if not force_refresh and self._value is not None and (now - self._fetched_at) < self._ttl:
            return self._value

        async with self._lock:
            # Re-check after acquiring the lock in case another request already refreshed it.
            now = time.monotonic()
            if not force_refresh and self._value is not None and (now - self._fetched_at) < self._ttl:
                return self._value
            self._value = await self._client.get_policy_state()
            self._fetched_at = now
            return self._value

    def invalidate(self) -> None:
        self._value = None
