from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.broker.angleone.feed import AngleOneWebSocketFeed
from app.broker.interfaces import BrokerSession
from app.domain.models import (
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
)
from app.marketdata.runtime_metrics import MarketDataRuntimeMetrics
from app.workers.market_data_worker import _feed_health_error


class RuntimePerformanceSafeguardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = InstrumentToken(
            exchange=Exchange.NSE,
            token="99926000",
            symbol="NIFTY",
            trading_symbol="NIFTY",
            kind=InstrumentKind.INDEX,
        )
        self.now = datetime(2026, 7, 27, 3, 45, tzinfo=UTC)

    def test_queue_overflow_is_latched_as_data_loss(self) -> None:
        settings = SimpleNamespace(
            market_data_queue_capacity=2,
            market_data_queue_pressure_ratio=0.50,
        )
        feed = AngleOneWebSocketFeed(
            settings=settings,
            session=BrokerSession("access", None, "feed", {}),
            token_lookup={self.token.token: self.token},
        )
        tick = self._tick()

        feed._enqueue_tick(tick)
        feed._enqueue_tick(tick)
        feed._enqueue_tick(tick)

        health = feed.health_snapshot()
        self.assertEqual(health["status"], "DATA_LOSS")
        self.assertEqual(health["dropped_events"], 1)
        self.assertEqual(health["queue_high_watermark"], 2)
        self.assertIn(
            "feed_unhealthy=DATA_LOSS",
            _feed_health_error(health),
        )

    def test_runtime_metrics_use_bounded_samples_and_report_p95(self) -> None:
        metrics = MarketDataRuntimeMetrics(sample_capacity=2)
        for delay_ms in (1, 5, 10):
            tick = self._tick(receipt_delay_ms=delay_ms)
            metrics.observe_tick(
                tick,
                processing_started_at=(
                    tick.received_at + timedelta(milliseconds=2)
                ),
            )

        snapshot = metrics.snapshot(
            feed_health={"status": "HEALTHY"},
            recorder_health={"status": "HEALTHY"},
        )

        exchange_latency = snapshot["exchange_to_receipt_ms"]
        self.assertEqual(exchange_latency["samples"], 2)
        self.assertEqual(exchange_latency["latest"], 10.0)
        self.assertEqual(exchange_latency["p95"], 10.0)
        self.assertEqual(snapshot["feed"]["status"], "HEALTHY")

    def test_websocket_error_unblocks_tick_consumer(self) -> None:
        settings = SimpleNamespace(
            market_data_queue_capacity=2,
            market_data_queue_pressure_ratio=0.50,
        )
        feed = AngleOneWebSocketFeed(
            settings=settings,
            session=BrokerSession("access", None, "feed", {}),
            token_lookup={self.token.token: self.token},
        )
        feed._set_error(RuntimeError("connection lost"))

        async def consume() -> None:
            await anext(feed.ticks())

        with self.assertRaisesRegex(
            RuntimeError,
            "WebSocket failed",
        ):
            asyncio.run(consume())

    def test_websocket_close_stops_socket_and_joins_thread(self) -> None:
        settings = SimpleNamespace(
            market_data_queue_capacity=2,
            market_data_queue_pressure_ratio=0.50,
        )
        feed = AngleOneWebSocketFeed(
            settings=settings,
            session=BrokerSession("access", None, "feed", {}),
            token_lookup={self.token.token: self.token},
        )
        socket = _Socket()
        thread = _Thread()
        feed._socket = socket
        feed._thread = thread

        asyncio.run(feed.close())

        self.assertTrue(socket.closed)
        self.assertEqual(thread.join_timeouts, [2.0])
        self.assertIsNone(feed._socket)
        self.assertIsNone(feed._thread)

    def _tick(self, *, receipt_delay_ms: int = 0) -> MarketTick:
        return MarketTick(
            token=self.token,
            exchange_timestamp=self.now,
            received_at=(
                self.now + timedelta(milliseconds=receipt_delay_ms)
            ),
            ltp=None,
        )


class _Socket:
    def __init__(self) -> None:
        self.closed = False

    def close_connection(self) -> None:
        self.closed = True


class _Thread:
    def __init__(self) -> None:
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)


if __name__ == "__main__":
    unittest.main()
