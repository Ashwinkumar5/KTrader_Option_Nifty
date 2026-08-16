from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.domain.models import InstrumentToken, MarketTick


@dataclass(frozen=True)
class BrokerSession:
    access_token: str
    refresh_token: str | None
    feed_token: str
    raw: dict[str, object]


class BrokerClient(Protocol):
    async def login(self) -> BrokerSession:
        """Create or refresh broker session."""

    async def instrument_master(self) -> Sequence[dict[str, object]]:
        """Return raw instrument master rows from the broker."""

    async def market_quote(
        self,
        *,
        mode: str,
        exchange_tokens: dict[str, list[str]],
    ) -> dict[str, object]:
        """Return broker quote data for grouped exchange tokens."""

    async def ltp_data(
        self,
        *,
        exchange: str,
        trading_symbol: str,
        symbol_token: str,
    ) -> dict[str, object]:
        """Return last traded price for one instrument."""

    async def historical_oi(self, params: dict[str, object]) -> dict[str, object]:
        """Return broker historical OI data."""

    async def historical_candles(
        self,
        params: dict[str, object],
    ) -> dict[str, object]:
        """Return broker historical OHLC candle data."""

    async def option_greeks(self, params: dict[str, object]) -> dict[str, object]:
        """Return broker option IV and Greeks data."""

    async def put_call_ratio(self) -> dict[str, object]:
        """Return broker-level put-call ratio data."""

    async def oi_buildup(self, params: dict[str, object]) -> dict[str, object]:
        """Return broker OI buildup data."""


class MarketDataFeed(Protocol):
    async def connect(self) -> None:
        """Open feed connection."""

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        """Subscribe to market-data tokens."""

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        """Unsubscribe from market-data tokens."""

    async def close(self) -> None:
        """Close the feed connection and release background resources."""

    def ticks(self) -> AsyncIterator[MarketTick]:
        """Yield normalized ticks."""

    def health_snapshot(self) -> dict[str, object]:
        """Return bounded-queue and connection health for fail-closed gating."""
