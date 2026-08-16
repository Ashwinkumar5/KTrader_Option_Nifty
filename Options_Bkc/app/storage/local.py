from __future__ import annotations

import asyncio
from pathlib import Path

import orjson

from app.domain.models import AnalyticsSnapshot, MarketTick, OptionChainSnapshot
from app.storage.serialization import to_ist_iso, to_jsonable


class JsonlTickStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def save_tick(self, tick: MarketTick) -> None:
        serialized = orjson.dumps(
            to_jsonable({
                **to_jsonable(tick),
                "exchange_timestamp_ist": to_ist_iso(tick.exchange_timestamp),
                "received_at_ist": to_ist_iso(tick.received_at),
            }),
            option=orjson.OPT_APPEND_NEWLINE,
        )
        await asyncio.to_thread(self._append, serialized)

    def _append(self, serialized: bytes) -> None:
        with self._path.open("ab") as handle:
            handle.write(serialized)


class NullTickStore:
    """No-op store used when the schema-v4 tape is the raw-tick authority."""

    async def save_tick(self, tick: MarketTick) -> None:
        return None


class JsonlChainSnapshotStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def save_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        payload = {
            **to_jsonable(snapshot),
            "captured_at_ist": to_ist_iso(snapshot.captured_at),
        }
        serialized = orjson.dumps(
            payload,
            option=orjson.OPT_APPEND_NEWLINE,
        )
        await asyncio.to_thread(self._append, serialized)

    def _append(self, serialized: bytes) -> None:
        with self._path.open("ab") as handle:
            handle.write(serialized)


class NullChainSnapshotStore:
    """No-op store when the replay tape already owns complete chain frames."""

    async def save_chain_snapshot(
        self,
        snapshot: OptionChainSnapshot,
    ) -> None:
        return None


class InMemoryLiveStateStore:
    def __init__(self) -> None:
        self.latest_chain_snapshot: OptionChainSnapshot | None = None
        self.latest_analytics_snapshot: AnalyticsSnapshot | None = None

    async def publish_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        self.latest_chain_snapshot = snapshot

    async def publish_analytics_snapshot(self, snapshot: AnalyticsSnapshot) -> None:
        self.latest_analytics_snapshot = snapshot
