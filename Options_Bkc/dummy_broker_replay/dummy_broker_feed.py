from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Iterator

from app.domain.models import InstrumentToken, MarketTick

from .serde import parse_market_tick


class RecordedMarketDataFeed:
    """Subscription-aware decoder for recorded WebSocket market events."""

    def __init__(
        self,
        records: Iterable[dict[str, object]] = (),
    ) -> None:
        self._records = records
        self._connected = False
        self._subscriptions: set[str] = set()

    async def connect(self) -> None:
        self._connected = True

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        self._subscriptions.update(token.token for token in tokens)

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        self._subscriptions.difference_update(token.token for token in tokens)

    async def close(self) -> None:
        self._connected = False
        self._subscriptions.clear()

    def decode_market_event(
        self,
        record: dict[str, object],
    ) -> MarketTick | None:
        if record.get("record_type") != "market_event":
            return None
        tick = parse_market_tick(record)
        if self._subscriptions and tick.token.token not in self._subscriptions:
            return None
        return tick

    async def ticks(self) -> AsyncIterator[MarketTick]:
        if not self._connected:
            raise RuntimeError("Replay feed is not connected")
        for record in self._records:
            tick = self.decode_market_event(record)
            if tick is not None:
                yield tick
