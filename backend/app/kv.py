"""Tiny key-value store: Redis when REDIS_URL is set, otherwise in-process memory.

On Hugging Face Spaces api + worker run in one container and only the worker uses locks/dedup keys,
so an in-memory store is enough (durable dedup lives in Postgres `alert_history`).
"""
from __future__ import annotations

import time

from .config import get_settings


class MemoryKV:
    def __init__(self):
        self._d: dict[str, tuple[str, float | None]] = {}

    def _alive(self, key: str) -> bool:
        item = self._d.get(key)
        if item is None:
            return False
        if item[1] is not None and item[1] < time.monotonic():
            del self._d[key]
            return False
        return True

    async def get(self, key: str) -> str | None:
        return self._d[key][0] if self._alive(key) else None

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and self._alive(key):
            return False
        self._d[key] = (str(value), time.monotonic() + ex if ex else None)
        return True

    async def delete(self, *keys: str) -> int:
        return sum(1 for k in keys if self._d.pop(k, None) is not None)

    async def ping(self) -> bool:
        return True

    async def flushdb(self) -> bool:
        self._d.clear()
        return True

    async def aclose(self) -> None:
        return None


def get_kv():
    url = get_settings().redis_url
    if url:
        import redis.asyncio as aioredis

        return aioredis.from_url(url, decode_responses=True)
    return MemoryKV()
