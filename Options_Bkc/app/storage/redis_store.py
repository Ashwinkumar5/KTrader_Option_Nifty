from __future__ import annotations

import json
from typing import Optional

try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # ImportError or package not installed
    aioredis = None  # type: ignore

from app.domain.models import AnalyticsSnapshot, MarketTick, OptionChainSnapshot
from app.storage.serialization import to_jsonable


def _require_aioredis() -> None:
    if aioredis is None:
        raise RuntimeError(
            "The 'redis' Python package is required to use Redis stores.\n"
            "Install it in your environment: python -m pip install 'redis>=5.0'"
        )


class RedisTickStore:
    def __init__(self, redis_url: str, key: str = "ticks") -> None:
        _require_aioredis()
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._key = key

    async def save_tick(self, tick: MarketTick) -> None:
        payload = json.dumps(to_jsonable(tick), separators=(",", ":"))
        await self._redis.rpush(self._key, payload)


class RedisChainSnapshotStore:
    def __init__(self, redis_url: str, key: str = "chain_snapshots") -> None:
        _require_aioredis()
        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._key = key

    async def save_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        payload = json.dumps(to_jsonable(snapshot), separators=(",", ":"))
        await self._redis.rpush(self._key, payload)


class RedisLiveStateStore:
    def __init__(self, redis_url: str) -> None:
        _require_aioredis()
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def publish_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        key = f"live:chain:{snapshot.underlying}"
        await self._redis.set(key, json.dumps(to_jsonable(snapshot), separators=(",", ":")))
        # publish a simple index message so subscribers can react
        await self._redis.publish(
            "channel:chain",
            json.dumps({"underlying": snapshot.underlying, "captured_at": snapshot.captured_at.isoformat()}),
        )

    async def publish_analytics_snapshot(self, snapshot: AnalyticsSnapshot) -> None:
        key = f"live:analytics:{snapshot.underlying}"
        await self._redis.set(key, json.dumps(to_jsonable(snapshot), separators=(",", ":")))
        await self._redis.publish(
            "channel:analytics",
            json.dumps({"underlying": snapshot.underlying, "captured_at": snapshot.captured_at.isoformat()}),
        )
