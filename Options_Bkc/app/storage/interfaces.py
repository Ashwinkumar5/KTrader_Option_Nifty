from __future__ import annotations

from typing import Protocol

from app.domain.models import AnalyticsSnapshot, MarketTick, OptionChainSnapshot


class TickStore(Protocol):
    async def save_tick(self, tick: MarketTick) -> None:
        """Persist one normalized tick."""


class ChainSnapshotStore(Protocol):
    async def save_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        """Persist an option-chain snapshot."""


class LiveStateStore(Protocol):
    async def publish_chain_snapshot(self, snapshot: OptionChainSnapshot) -> None:
        """Publish latest chain to live cache."""

    async def publish_analytics_snapshot(self, snapshot: AnalyticsSnapshot) -> None:
        """Publish latest analytics to live cache."""
