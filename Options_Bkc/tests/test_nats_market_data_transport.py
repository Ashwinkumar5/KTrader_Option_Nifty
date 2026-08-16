from __future__ import annotations

import asyncio
import socket
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

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
)
from process_watch_dog.strategy_catalog import StrategyCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATS_SERVER = (
    PROJECT_ROOT
    / ".runtime"
    / "nats-server-v2.14.3"
    / "nats-server-v2.14.3-windows-amd64"
    / "nats-server.exe"
)


@unittest.skipUnless(
    NATS_SERVER.is_file(),
    "run scripts/install_nats_server.ps1 for the real Core-NATS smoke",
)
class RealNatsMarketDataTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_enabled_strategy_subscribers_receive_identical_ordered_events_and_disconnect_fails_closed(
        self,
    ) -> None:
        port = _available_port()
        server = await asyncio.create_subprocess_exec(
            str(NATS_SERVER),
            "-a",
            "127.0.0.1",
            "-p",
            str(port),
            "-n",
            "ktrader-test",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        publisher = None
        subscribers: list[NatsMarketDataFeedHandler] = []
        tick_streams = []
        pending_ticks: list[asyncio.Task[MarketTick]] = []
        try:
            await _wait_for_port(port)
            url = f"nats://127.0.0.1:{port}"
            bootstrap = _bootstrap()
            publisher = NatsMarketDataPublisher(
                nats_url=url,
                subject_prefix="ktrader.test.marketdata.v1",
                queue_capacity=32,
                connect_timeout_seconds=2.0,
            )
            await publisher.start(bootstrap)

            expected_subscriber_count = _enabled_strategy_subscriber_count()
            for _ in range(expected_subscriber_count):
                subscriber = NatsMarketDataFeedHandler(
                    nats_url=url,
                    subject_prefix="ktrader.test.marketdata.v1",
                    queue_capacity=32,
                    bootstrap_timeout_seconds=2.0,
                    consumer_interval_ms=5_000,
                    max_tick_lag_seconds=3.0,
                    max_frame_lag_seconds=3.0,
                )
                await subscriber.prepare()
                subscribers.append(subscriber)

            captured_at = datetime.now(UTC)
            tick = MarketTick(
                token=bootstrap.spot_tokens[0],
                exchange_timestamp=captured_at,
                received_at=captured_at,
                ltp=Decimal("24570.65"),
                open_price=Decimal("24557.00"),
                high_price=Decimal("24580.00"),
                low_price=Decimal("24540.00"),
                close_price=Decimal("24550.00"),
                volume=100,
                raw={"best_5_buy_data": [], "best_5_sell_data": []},
            )
            frame = _frame(captured_at)
            self.assertTrue(
                publisher.publish(
                    RawMarketTickEvent(
                        handler_epoch=bootstrap.handler_epoch,
                        event_id="tick-1",
                        published_at=captured_at,
                        tick=tick,
                    )
                )
            )
            self.assertTrue(publisher.publish(frame))
            await publisher.flush()

            received_ticks = []
            received_frames = []
            for subscriber in subscribers:
                stream = subscriber.ticks()
                tick_streams.append(stream)
                received_ticks.append(
                    await asyncio.wait_for(anext(stream), timeout=2.0)
                )
                # Resuming the generator represents the worker completing its
                # tick body; only then may the ordered later frame be released.
                pending_ticks.append(asyncio.create_task(anext(stream)))
                received_frames.append(
                    await asyncio.wait_for(
                        subscriber.next_materialized_frame(
                            underlying="NIFTY"
                        ),
                        timeout=2.0,
                    )
                )

            self.assertEqual(
                received_ticks,
                [tick] * expected_subscriber_count,
            )
            self.assertEqual(
                [item.event_id for item in received_frames],
                ["frame-1"] * expected_subscriber_count,
            )
            self.assertTrue(
                all(item.snapshot == frame.snapshot for item in received_frames)
            )

            server.terminate()
            await asyncio.wait_for(server.wait(), timeout=5.0)
            with self.assertRaises(MarketDataTransportFatalError):
                await asyncio.wait_for(pending_ticks[0], timeout=5.0)
        finally:
            for task in pending_ticks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending_ticks, return_exceptions=True)
            for stream in tick_streams:
                await stream.aclose()
            await asyncio.gather(
                *(subscriber.close() for subscriber in subscribers),
                return_exceptions=True,
            )
            if publisher is not None:
                await asyncio.gather(
                    publisher.close(),
                    return_exceptions=True,
                )
            if server.returncode is None:
                server.terminate()
                await asyncio.wait_for(server.wait(), timeout=5.0)


def _bootstrap() -> MarketDataBootstrap:
    now = datetime.now(UTC)
    spot = InstrumentToken(
        exchange=Exchange.NSE,
        token="99926000",
        symbol="NIFTY",
        trading_symbol="Nifty 50",
        kind=InstrumentKind.INDEX,
    )
    return MarketDataBootstrap(
        handler_epoch="real-nats-test-epoch",
        generated_at=now,
        source_interval_ms=5_000,
        option_window_each_side=0,
        selected_expiries=(("NIFTY", date(2026, 8, 13)),),
        spot_tokens=(spot,),
        option_contracts=(),
        future_contracts=(),
        reference_tokens=(),
    )


def _frame(captured_at: datetime) -> MaterializedOptionChainFrame:
    refresh = RefreshProvenance(
        status="ok",
        requested_at=captured_at,
        responded_at=captured_at,
        attempts=1,
        row_count=0,
    )
    return MaterializedOptionChainFrame(
        handler_epoch="real-nats-test-epoch",
        event_id="frame-1",
        published_at=captured_at,
        snapshot=OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 8, 13),
            spot_price=Decimal("24570.65"),
            atm_strike=Decimal("24550"),
            captured_at=captured_at,
            quotes=(),
        ),
        scheduled_for=captured_at,
        frame_started_at=captured_at,
        trigger_tick_received_at=captured_at,
        spot_observed_at=captured_at,
        window_each_side=0,
        source_interval_ms=5_000,
        quote_refresh=refresh,
        greeks_refresh=refresh,
        feed_health=FeedHealthSnapshot(status="HEALTHY"),
    )


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


async def _wait_for_port(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5.0
    last_error: BaseException | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        del reader
        return
    raise AssertionError("NATS server did not become ready") from last_error


def _enabled_strategy_subscriber_count() -> int:
    catalog = StrategyCatalog.load(
        PROJECT_ROOT / "config" / "strategy_config.json"
    )
    return sum(
        len(catalog.enabled_strategies(profile))
        for profile in catalog.watchdog_enabled_profiles()
    )


if __name__ == "__main__":
    unittest.main()
