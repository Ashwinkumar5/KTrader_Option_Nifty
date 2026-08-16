from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.models import MarketTick
from app.marketdata.events import (
    FeedHealthSnapshot,
    MaterializedOptionChainFrame,
    RefreshProvenance,
)
from app.optionchain.state import OptionChainState
from app.workers.market_data_worker import run_market_data_worker
from app.workers.market_data_worker import _monitor_worker_ticks
from process_watch_dog.strategy_catalog import StrategyCatalog
from tests.test_market_data_feed_handler import (
    _ChainStore,
    _LiveStore,
    _SmokeFeedHandler,
    _TickStore,
    _worker_settings,
)


class RemoteMarketDataWorkerTests(unittest.TestCase):
    def test_every_configured_strategy_family_uses_publisher_frame_without_broker_calls(
        self,
    ) -> None:
        catalog = StrategyCatalog.load(
            Path(__file__).resolve().parents[1]
            / "config"
            / "strategy_config.json"
        )
        cases = tuple(
            (profile, strategy)
            for profile in catalog.watchdog_enabled_profiles()
            for strategy in catalog.enabled_strategies(profile)
        )
        for profile, strategy in cases:
            with self.subTest(profile=profile, strategy=strategy):
                handler = _RemoteFrameHandler()
                chain_store = _ChainStore()

                asyncio.run(
                    run_market_data_worker(
                        settings=replace(
                            _worker_settings(),
                            strategy_profile=profile,
                        ),
                        feed_handler=handler,
                        tick_store=_TickStore(),
                        chain_store=chain_store,
                        live_store=_LiveStore(),
                        max_ticks=1,
                        enabled_strategies=(strategy,),
                    )
                )

                self.assertEqual(handler.frame_requests, 1)
                self.assertEqual(handler.broker_calls, [])
                self.assertNotIn("subscribe", handler.calls)
                self.assertNotIn("unsubscribe", handler.calls)
                self.assertEqual(len(chain_store.snapshots), 1)
                self.assertIs(chain_store.snapshots[0], handler.frame.snapshot)

    def test_remote_frame_failure_surfaces_without_waiting_for_another_tick(
        self,
    ) -> None:
        handler = _FailingRemoteFrameHandler()

        async def exercise() -> None:
            with self.assertRaisesRegex(RuntimeError, "remote frame failed"):
                await asyncio.wait_for(
                    run_market_data_worker(
                        settings=_worker_settings(),
                        feed_handler=handler,
                        tick_store=_TickStore(),
                        chain_store=_ChainStore(),
                        live_store=_LiveStore(),
                        enabled_strategies=("DERIVATIVES_QUANT",),
                    ),
                    timeout=1.0,
                )

        asyncio.run(exercise())
        self.assertEqual(handler.calls[-1], "close")

    def test_worker_monitor_does_not_prefetch_before_tick_body_finishes(
        self,
    ) -> None:
        acknowledgements: list[str] = []
        acknowledged = asyncio.Event()

        async def source():
            try:
                yield "tick"
            finally:
                acknowledgements.append("ack")
                acknowledged.set()
            await asyncio.Event().wait()

        async def exercise() -> None:
            monitored = _monitor_worker_ticks(
                source(),
                snapshot_tasks={},
            )
            observed = await anext(monitored)
            self.assertEqual(observed, "tick")
            await asyncio.sleep(0)
            self.assertEqual(acknowledgements, [])
            next_tick = asyncio.create_task(anext(monitored))
            await asyncio.wait_for(acknowledged.wait(), timeout=0.1)
            self.assertEqual(acknowledgements, ["ack"])
            next_tick.cancel()
            await asyncio.gather(next_tick, return_exceptions=True)
            await monitored.aclose()

        asyncio.run(exercise())

    def test_max_ticks_closes_tick_barrier_before_settling_frame(self) -> None:
        handler = _BarrierRemoteFrameHandler()

        asyncio.run(
            asyncio.wait_for(
                run_market_data_worker(
                    settings=_worker_settings(),
                    feed_handler=handler,
                    tick_store=_TickStore(),
                    chain_store=_ChainStore(),
                    live_store=_LiveStore(),
                    max_ticks=1,
                    enabled_strategies=("DERIVATIVES_QUANT",),
                ),
                timeout=1.0,
            )
        )

        self.assertTrue(handler.tick_acknowledged.is_set())
        self.assertEqual(handler.frame_requests, 1)

    def test_worker_heartbeat_is_wired_without_broker_access(self) -> None:
        handler = _RemoteFrameHandler()
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "dq.heartbeat"

            asyncio.run(
                run_market_data_worker(
                    settings=_worker_settings(),
                    feed_handler=handler,
                    tick_store=_TickStore(),
                    chain_store=_ChainStore(),
                    live_store=_LiveStore(),
                    max_ticks=1,
                    enabled_strategies=("DERIVATIVES_QUANT",),
                    heartbeat_file=heartbeat,
                    heartbeat_stall_timeout_seconds=3,
                )
            )

            self.assertTrue(heartbeat.is_file())
        self.assertEqual(handler.broker_calls, [])


class _RemoteFrameHandler(_SmokeFeedHandler):
    is_remote_subscriber = True

    def __init__(self) -> None:
        super().__init__()
        self.frame_requests = 0
        self.broker_calls: list[str] = []
        self.frame = self._frame()

    async def subscribe(self, tokens) -> None:
        self.broker_calls.append("subscribe")
        raise AssertionError("Remote strategy must not subscribe at broker")

    async def unsubscribe(self, tokens) -> None:
        self.broker_calls.append("unsubscribe")
        raise AssertionError("Remote strategy must not unsubscribe at broker")

    async def refresh_option_quotes(self, *, state, contracts):
        self.broker_calls.append("quotes")
        raise AssertionError("Remote strategy must not refresh quotes")

    async def refresh_option_greeks(self, **_kwargs):
        self.broker_calls.append("greeks")
        raise AssertionError("Remote strategy must not refresh Greeks")

    async def next_materialized_frame(self, **_kwargs):
        self.frame_requests += 1
        return self.frame

    def _frame(self) -> MaterializedOptionChainFrame:
        state = OptionChainState(master=self.master)
        state.update_tick(
            MarketTick(
                token=self.spot,
                exchange_timestamp=self.at,
                received_at=self.at,
                ltp=Decimal("24500"),
            )
        )
        values = {
            "call_option": (
                Decimal("125"),
                Decimal("124.9"),
                Decimal("125.1"),
                1000,
                100,
            ),
            "put_option": (
                Decimal("95"),
                Decimal("94.9"),
                Decimal("95.1"),
                900,
                80,
            ),
        }
        for contract in (self.call_contract, self.put_contract):
            ltp, bid, ask, oi, volume = values[contract.token.token]
            state.update_tick(
                MarketTick(
                    token=contract.token,
                    exchange_timestamp=self.at,
                    received_at=self.at,
                    ltp=ltp,
                    bid=bid,
                    ask=ask,
                    oi=oi,
                    volume=volume,
                )
            )
        market = state.build_underlying_market_snapshot(
            underlying="NIFTY",
            captured_at=self.at,
        )
        snapshot = state.build_snapshot(
            underlying="NIFTY",
            expiry=self.call_contract.expiry,
            spot_price=Decimal("24500"),
            each_side=0,
            captured_at=self.at,
            market=market,
        )
        tokens = tuple(
            quote.contract.token.token for quote in snapshot.quotes
        )
        provenance = RefreshProvenance(
            status="ok",
            requested_at=self.at,
            responded_at=self.at,
            attempts=1,
            row_count=2,
            normalized_tokens=tokens,
            exchange_tokens=(("NFO", tokens),),
            mode="FULL",
            broker_status=True,
        )
        disabled_greeks = RefreshProvenance(
            status="disabled",
            requested_at=None,
            responded_at=None,
            attempts=0,
            row_count=0,
        )
        return MaterializedOptionChainFrame(
            handler_epoch="remote-test",
            event_id="remote-test-frame",
            published_at=self.at,
            snapshot=snapshot,
            scheduled_for=self.at,
            frame_started_at=self.at,
            trigger_tick_received_at=self.at,
            spot_observed_at=self.at,
            window_each_side=0,
            source_interval_ms=5000,
            quote_refresh=provenance,
            greeks_refresh=disabled_greeks,
            feed_health=FeedHealthSnapshot(status="HEALTHY"),
        )


class _FailingRemoteFrameHandler(_RemoteFrameHandler):
    async def ticks(self):
        self.calls.append("ticks")
        yield MarketTick(
            token=self.spot,
            exchange_timestamp=self.at,
            received_at=self.at,
            ltp=Decimal("24500"),
        )
        await asyncio.Event().wait()

    async def next_materialized_frame(self, **_kwargs):
        self.frame_requests += 1
        raise RuntimeError("remote frame failed")


class _BarrierRemoteFrameHandler(_RemoteFrameHandler):
    def __init__(self) -> None:
        super().__init__()
        self.tick_acknowledged = asyncio.Event()

    async def ticks(self):
        self.calls.append("ticks")
        try:
            yield MarketTick(
                token=self.spot,
                exchange_timestamp=self.at,
                received_at=self.at,
                ltp=Decimal("24500"),
            )
        finally:
            self.tick_acknowledged.set()

    async def next_materialized_frame(self, **_kwargs):
        self.frame_requests += 1
        await self.tick_acknowledged.wait()
        return self.frame


if __name__ == "__main__":
    unittest.main()
