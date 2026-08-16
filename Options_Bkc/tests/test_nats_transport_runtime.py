from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
)
from app.marketdata.events import (
    FeedHealthSnapshot,
    MarketDataBootstrap,
    MaterializedOptionChainFrame,
    RawMarketTickEvent,
    RefreshProvenance,
)
from app.marketdata.nats_transport import (
    MarketDataTransportFatalError,
    NatsMarketDataFeedHandler,
    NatsMarketDataPublisher,
    _QueuedFrame,
    _QueuedTick,
)
from app.marketdata.serde import (
    encode_market_data_bootstrap,
    encode_market_data_event,
)


_NOW = datetime.now(UTC)


def _spot_token() -> InstrumentToken:
    return InstrumentToken(
        exchange=Exchange.NSE,
        token="99926000",
        symbol="NIFTY",
        trading_symbol="NIFTY",
        kind=InstrumentKind.INDEX,
    )


def _bootstrap() -> MarketDataBootstrap:
    return MarketDataBootstrap(
        handler_epoch="test-epoch",
        generated_at=_NOW,
        source_interval_ms=5_000,
        option_window_each_side=0,
        selected_expiries=(("NIFTY", date(2026, 8, 13)),),
        spot_tokens=(),
        option_contracts=(),
        future_contracts=(),
        reference_tokens=(),
    )


def _frame(offset_seconds: int) -> MaterializedOptionChainFrame:
    scheduled_for = _NOW + timedelta(seconds=offset_seconds)
    provenance = RefreshProvenance(
        status="success",
        requested_at=scheduled_for,
        responded_at=scheduled_for,
        attempts=1,
        row_count=0,
    )
    return MaterializedOptionChainFrame(
        handler_epoch="test-epoch",
        event_id=f"frame-{offset_seconds}",
        # Producer schedule may be synthetic in this unit test; transport
        # publication time is the current wall clock and must stay fresh.
        published_at=_NOW,
        snapshot=OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 8, 13),
            spot_price=Decimal("24500"),
            atm_strike=Decimal("24500"),
            captured_at=scheduled_for,
            quotes=(),
        ),
        scheduled_for=scheduled_for,
        frame_started_at=scheduled_for,
        trigger_tick_received_at=scheduled_for,
        spot_observed_at=scheduled_for,
        window_each_side=0,
        source_interval_ms=5_000,
        quote_refresh=provenance,
        greeks_refresh=provenance,
        feed_health=FeedHealthSnapshot(status="HEALTHY"),
    )


class _FakeSubscription:
    async def unsubscribe(self) -> None:
        return None


class _FakeMessage:
    def __init__(self, data: bytes, reply: str = "") -> None:
        self.data = data
        self.reply = reply


class _FakeClient:
    def __init__(self, bootstrap: MarketDataBootstrap) -> None:
        self.bootstrap = bootstrap
        self.published: list[tuple[str, bytes]] = []
        self.request_attempts = 0

    async def subscribe(self, _subject: str, **_kwargs: object) -> _FakeSubscription:
        return _FakeSubscription()

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, payload))

    async def request(
        self,
        _subject: str,
        _payload: bytes,
        *,
        timeout: float,
    ) -> _FakeMessage:
        del timeout
        self.request_attempts += 1
        if self.request_attempts < 3:
            raise TimeoutError("publisher not ready")
        return _FakeMessage(encode_market_data_bootstrap(self.bootstrap))

    async def flush(self, timeout: float = 10.0) -> None:
        del timeout

    async def drain(self) -> None:
        return None

    async def close(self) -> None:
        return None


class NatsTransportRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_retries_startup_and_publishes_admission_order(self) -> None:
        client = _FakeClient(_bootstrap())
        attempts: list[dict[str, object]] = []

        def factory(**options: object) -> _FakeClient:
            attempts.append(options)
            if len(attempts) < 3:
                raise OSError("NATS not listening yet")
            return client

        publisher = NatsMarketDataPublisher(
            client_factory=factory,
            connect_timeout_seconds=0.5,
        )
        await publisher.start(_bootstrap())
        self.assertTrue(publisher.publish_encoded(b"first"))
        self.assertTrue(publisher.publish_encoded(b"second"))
        await publisher.flush()

        self.assertGreaterEqual(len(attempts), 3)
        self.assertTrue(all(options["allow_reconnect"] is False for options in attempts))
        self.assertEqual(
            [payload for _subject, payload in client.published],
            [b"first", b"second"],
        )
        await publisher.close()

    async def test_subscriber_retries_connection_then_bootstrap(self) -> None:
        bootstrap = _bootstrap()
        client = _FakeClient(bootstrap)
        server_available = False
        connection_attempts = 0

        def factory(**_options: object) -> _FakeClient:
            nonlocal connection_attempts
            connection_attempts += 1
            if not server_available:
                raise ConnectionRefusedError("subscriber started before NATS")
            return client

        handler = NatsMarketDataFeedHandler(
            client_factory=factory,
            bootstrap_timeout_seconds=1.0,
            connect_timeout_seconds=0.05,
            consumer_interval_ms=15_000,
        )
        prepare_task = asyncio.create_task(handler.prepare())
        await asyncio.sleep(0.12)
        self.assertFalse(prepare_task.done())
        server_available = True

        runtime = await asyncio.wait_for(prepare_task, timeout=1.0)
        self.assertIsNotNone(runtime)
        self.assertGreaterEqual(connection_attempts, 2)
        self.assertEqual(client.request_attempts, 3)
        await handler.close()

    async def test_five_second_frames_downsample_to_fifteen_without_reuse(self) -> None:
        handler = NatsMarketDataFeedHandler(consumer_interval_ms=15_000)
        handler._fatal_event = asyncio.Event()
        handler._bootstrap = _bootstrap()
        handler._handler_epoch = "test-epoch"
        handler._frame_queues = {"NIFTY": asyncio.Queue(maxsize=16)}

        for offset in (0, 5, 10, 15):
            handler._dispatch_frame(_frame(offset))

        first = await handler.next_materialized_frame(underlying="NIFTY")
        second = await handler.next_materialized_frame(underlying="NIFTY")
        self.assertEqual(first.scheduled_for, _NOW)
        self.assertEqual(second.scheduled_for, _NOW + timedelta(seconds=15))
        self.assertEqual(handler._frame_queues["NIFTY"].qsize(), 0)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                handler.next_materialized_frame(underlying="NIFTY"),
                timeout=0.02,
            )
        handler._dispatch_frame(_frame(30))
        after_cancel = await asyncio.wait_for(
            handler.next_materialized_frame(underlying="NIFTY"),
            timeout=0.1,
        )
        self.assertEqual(
            after_cancel.scheduled_for,
            _NOW + timedelta(seconds=30),
        )
        await handler.close()

    async def test_stale_tick_publication_fails_closed_without_replay(self) -> None:
        handler = NatsMarketDataFeedHandler(max_tick_lag_seconds=0.05)
        handler._fatal_event = asyncio.Event()
        handler._bootstrap = _bootstrap()
        handler._handler_epoch = "test-epoch"
        handler._dispatcher_task = asyncio.create_task(handler._run_dispatcher())
        observed_at = datetime.now(UTC) - timedelta(seconds=1)
        token = _spot_token()
        event = RawMarketTickEvent(
            handler_epoch="test-epoch",
            event_id="stale-tick",
            published_at=observed_at,
            tick=MarketTick(
                token=token,
                exchange_timestamp=observed_at,
                received_at=observed_at,
                ltp=Decimal("24500"),
            ),
        )
        await handler._on_event_message(
            _FakeMessage(encode_market_data_event(event))
        )

        with self.assertRaises(MarketDataTransportFatalError):
            await asyncio.wait_for(anext(handler._yield_ticks()), timeout=0.1)
        self.assertEqual(handler._ticks.qsize(), 0)
        await handler.close()

    async def test_stale_tick_and_frame_queue_residence_fails_closed(self) -> None:
        loop = asyncio.get_running_loop()
        now = datetime.now(UTC)
        handler = NatsMarketDataFeedHandler(
            consumer_interval_ms=5_000,
            max_tick_lag_seconds=0.05,
        )
        handler._fatal_event = asyncio.Event()
        handler._ticks.put_nowait(
            _QueuedTick(
                tick=MarketTick(
                    token=_spot_token(),
                    exchange_timestamp=now,
                    received_at=now,
                ),
                published_at=now,
                enqueued_monotonic=loop.time() - 1.0,
                sequence=1,
            )
        )

        with self.assertRaises(MarketDataTransportFatalError):
            await anext(handler._yield_ticks())
        await handler.close()

        frame_handler = NatsMarketDataFeedHandler(
            consumer_interval_ms=5_000,
            max_frame_lag_seconds=0.05,
        )
        frame_handler._fatal_event = asyncio.Event()
        frame_handler._bootstrap = _bootstrap()
        frame_handler._handler_epoch = "test-epoch"
        frame_handler._frame_queues = {
            "NIFTY": asyncio.Queue(maxsize=2)
        }
        frame_handler._frame_queues["NIFTY"].put_nowait(
            _QueuedFrame(
                frame=replace(_frame(0), published_at=datetime.now(UTC)),
                enqueued_monotonic=loop.time() - 1.0,
                required_tick_sequence=0,
            )
        )

        with self.assertRaises(MarketDataTransportFatalError):
            await frame_handler.next_materialized_frame(underlying="NIFTY")
        await frame_handler.close()

    async def test_frame_cannot_overtake_an_earlier_tick(self) -> None:
        handler = NatsMarketDataFeedHandler(
            consumer_interval_ms=5_000,
            max_tick_lag_seconds=5.0,
            max_frame_lag_seconds=5.0,
        )
        handler._fatal_event = asyncio.Event()
        handler._bootstrap = _bootstrap()
        handler._handler_epoch = "test-epoch"
        handler._frame_queues = {"NIFTY": asyncio.Queue(maxsize=2)}
        handler._dispatcher_task = asyncio.create_task(
            handler._run_dispatcher()
        )
        now = datetime.now(UTC)
        tick_event = RawMarketTickEvent(
            handler_epoch="test-epoch",
            event_id="tick-before-frame",
            published_at=now,
            tick=MarketTick(
                token=_spot_token(),
                exchange_timestamp=now,
                received_at=now,
                ltp=Decimal("24500"),
            ),
        )
        await handler._on_event_message(
            _FakeMessage(encode_market_data_event(tick_event))
        )
        await handler._on_event_message(
            _FakeMessage(
                encode_market_data_event(
                    replace(_frame(0), published_at=now)
                )
            )
        )

        frame_waiter = asyncio.create_task(
            handler.next_materialized_frame(underlying="NIFTY")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(frame_waiter.done())

        ticks = handler._yield_ticks()
        observed = await anext(ticks)
        self.assertEqual(observed.ltp, Decimal("24500"))
        self.assertFalse(frame_waiter.done())
        # Requesting the next tick resumes the generator after the worker body,
        # acknowledging that the prior tick's state/microstructure work ended.
        next_tick = asyncio.create_task(anext(ticks))
        frame = await asyncio.wait_for(frame_waiter, timeout=0.1)
        self.assertEqual(frame.event_id, "frame-0")
        next_tick.cancel()
        await asyncio.gather(next_tick, return_exceptions=True)
        await ticks.aclose()
        await handler.close()


if __name__ == "__main__":
    unittest.main()
