from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from app.broker.angleone.data_map import SmartApiWebSocketMode
from app.broker.interfaces import BrokerSession
from app.core.config import Settings
from app.domain.models import InstrumentToken, MarketTick
from app.marketdata.normalizer import normalize_tick


_FEED_FAILED = object()


class AngleOneWebSocketFeed:
    def __init__(
        self,
        *,
        settings: Settings,
        session: BrokerSession,
        token_lookup: dict[str, InstrumentToken],
    ) -> None:
        self._settings = settings
        self._session = session
        self._token_lookup = token_lookup
        self._queue_capacity = settings.market_data_queue_capacity
        self._queue_pressure_threshold = max(
            1,
            int(
                self._queue_capacity
                * settings.market_data_queue_pressure_ratio
            ),
        )
        self._queue: asyncio.Queue[MarketTick | object] = asyncio.Queue(
            maxsize=self._queue_capacity
        )
        self._socket: Any | None = None
        self._thread: Thread | None = None
        self._received_events = 0
        self._enqueued_events = 0
        self._dropped_events = 0
        self._queue_pressure_events = 0
        self._queue_high_watermark = 0
        self._last_received_at: datetime | None = None
        self._last_error: str | None = None

    async def connect(self) -> None:
        try:
            from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        except ImportError as exc:
            raise RuntimeError("WebSocket feed requires smartapi-python from requirements.txt.") from exc

        loop = asyncio.get_running_loop()
        socket = SmartWebSocketV2(
            self._session.access_token,
            self._settings.angleone_api_key,
            self._settings.angleone_client_code,
            self._session.feed_token,
        )
        socket.on_data = lambda _wsapp, payload: self._on_data(loop, payload)
        socket.on_error = lambda _wsapp, error: self._on_error(loop, error)
        self._socket = socket
        self._socket_open = Event()
        socket.on_open = lambda wsapp: self._socket_open.set()
        socket.on_close = lambda wsapp: self._socket_open.clear()
        self._thread = Thread(target=socket.connect, name="angleone-websocket", daemon=True)
        self._thread.start()
        await self._wait_for_socket_open()

    async def _wait_for_socket_open(self) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._socket_open.wait), timeout=20)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("WebSocket did not open within 20 seconds") from exc
        except Exception as exc:
            raise RuntimeError("WebSocket failed to open") from exc

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        socket = self._require_socket()
        token_list = _token_list(tokens)
        if not token_list:
            return
        socket.subscribe(str(uuid4()), self._websocket_mode().code, token_list)

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        socket = self._require_socket()
        token_list = _token_list(tokens)
        if not token_list:
            return
        socket.unsubscribe(str(uuid4()), self._websocket_mode().code, token_list)

    async def close(self) -> None:
        """Close the SDK socket and wait briefly for its daemon thread."""

        socket = self._socket
        thread = self._thread
        self._socket = None
        self._thread = None
        if socket is not None:
            await asyncio.to_thread(socket.close_connection)
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 2.0)

    async def ticks(self) -> AsyncIterator[MarketTick]:
        while True:
            if self._last_error is not None:
                raise RuntimeError(
                    f"Angle One WebSocket failed: {self._last_error}"
                )
            item = await self._queue.get()
            if item is _FEED_FAILED:
                raise RuntimeError(
                    f"Angle One WebSocket failed: {self._last_error}"
                )
            if isinstance(item, MarketTick):
                yield item

    def _on_data(self, loop: asyncio.AbstractEventLoop, payload: dict[str, object]) -> None:
        token_id = str(payload.get("token") or payload.get("symbolToken") or payload.get("symbol_token") or "")
        token = self._token_lookup.get(token_id)
        if token is None:
            return
        tick = normalize_tick(token=token, payload=payload)
        loop.call_soon_threadsafe(self._enqueue_tick, tick)

    def _enqueue_tick(self, tick: MarketTick) -> None:
        self._received_events += 1
        self._last_received_at = tick.received_at
        try:
            self._queue.put_nowait(tick)
        except asyncio.QueueFull:
            # A missing event invalidates sequence-dependent velocity and book
            # persistence. Latch the loss and let the worker force NO_TRADE.
            self._dropped_events += 1
            return
        self._enqueued_events += 1
        depth = self._queue.qsize()
        self._queue_high_watermark = max(
            self._queue_high_watermark,
            depth,
        )
        if depth >= self._queue_pressure_threshold:
            self._queue_pressure_events += 1

    def _on_error(
        self,
        loop: asyncio.AbstractEventLoop,
        error: object,
    ) -> None:
        loop.call_soon_threadsafe(self._set_error, error)

    def _set_error(self, error: object) -> None:
        self._last_error = f"{type(error).__name__}: {error}"
        try:
            self._queue.put_nowait(_FEED_FAILED)
        except asyncio.QueueFull:
            # The consumer will see _last_error before requesting the next tick.
            pass

    def health_snapshot(self) -> dict[str, object]:
        depth = self._queue.qsize()
        if self._last_error is not None:
            status = "FAILED"
            reason = "websocket_error"
        elif self._dropped_events:
            status = "DATA_LOSS"
            reason = "market_data_queue_overflow"
        elif depth >= self._queue_pressure_threshold:
            status = "PRESSURE"
            reason = "market_data_queue_pressure"
        else:
            status = "HEALTHY"
            reason = None
        return {
            "status": status,
            "reason": reason,
            "queue_depth": depth,
            "queue_capacity": self._queue_capacity,
            "queue_pressure_threshold": self._queue_pressure_threshold,
            "queue_high_watermark": self._queue_high_watermark,
            "received_events": self._received_events,
            "enqueued_events": self._enqueued_events,
            "dropped_events": self._dropped_events,
            "queue_pressure_events": self._queue_pressure_events,
            "last_received_at": self._last_received_at,
            "last_error": self._last_error,
        }

    def _websocket_mode(self) -> SmartApiWebSocketMode:
        return SmartApiWebSocketMode(self._settings.market_data_ws_mode.upper())

    def _require_socket(self) -> Any:
        if self._socket is None:
            raise RuntimeError("WebSocket is not connected.")
        return self._socket


def _token_list(tokens: Iterable[InstrumentToken]) -> list[dict[str, object]]:
    grouped: dict[str, list[str]] = {}
    for token in tokens:
        grouped.setdefault(token.exchange.value, []).append(token.token)
    return [
        {"exchangeType": _websocket_exchange_type(exchange), "tokens": sorted(set(token_ids))}
        for exchange, token_ids in grouped.items()
    ]


def _websocket_exchange_type(exchange: str) -> int:
    return {"NSE": 1, "NFO": 2}[exchange]
