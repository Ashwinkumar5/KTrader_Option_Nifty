from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.broker.interfaces import BrokerSession
from app.core.config import load_settings
from app.domain.models import (
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionContract,
    OptionType,
)
from app.instruments.master import InstrumentMaster
from app.marketdata.feed_handler import (
    EmbeddedMarketDataFeedHandler,
    FeedHandlerRuntime,
    fetch_greeks,
)
from app.optionchain.state import OptionChainState
from app.workers.market_data_worker import run_market_data_worker


class MarketDataFeedHandlerTests(unittest.TestCase):
    def test_greeks_refresh_preserves_bounded_three_attempt_retry(self) -> None:
        class FlakyClient:
            def __init__(self) -> None:
                self.calls = 0

            async def option_greeks(self, _params):
                self.calls += 1
                if self.calls < 3:
                    raise TimeoutError("temporary broker failure")
                return {"status": True, "data": []}

        client = FlakyClient()
        sleeper = AsyncMock()
        with patch(
            "app.marketdata.feed_handler.asyncio.sleep",
            sleeper,
        ):
            payload, refresh = asyncio.run(
                fetch_greeks(
                    client=client,
                    underlying="NIFTY",
                    expiry=date(2026, 7, 30),
                )
            )

        self.assertEqual(payload, {"status": True, "data": []})
        self.assertEqual(refresh["status"], "ok")
        self.assertEqual(refresh["attempts"], 3)
        self.assertEqual(sleeper.await_args_list[0].args, (1.0,))
        self.assertEqual(sleeper.await_args_list[1].args, (2.0,))

    def test_prepares_and_starts_one_owned_broker_session(self) -> None:
        calls: list[str] = []
        client = _BrokerClient(calls)
        feed = _MarketDataFeed(calls)
        handler = EmbeddedMarketDataFeedHandler(
            settings=_settings(),
            client=client,
            feed=feed,
        )

        async def exercise():
            first = await handler.prepare()
            second = await handler.prepare()
            tokens = await handler.start(market_date=date(2026, 7, 29))
            repeated = await handler.start(market_date=date(2026, 7, 29))
            await handler.close()
            await handler.close()
            return first, second, tokens, repeated

        first, second, tokens, repeated = asyncio.run(exercise())

        self.assertIs(first, second)
        self.assertEqual(client.login_count, 1)
        self.assertEqual(
            calls,
            [
                "login",
                "instrument_master",
                "connect",
                "subscribe",
                "feed_close",
                "close",
            ],
        )
        self.assertEqual(
            tuple(token.token for token in tokens),
            ("spot", "future", "vix"),
        )
        self.assertEqual(tokens, repeated)
        self.assertEqual(feed.subscriptions, [("spot", "future", "vix")])

    def test_owns_quote_and_greeks_refresh_without_strategy_broker_calls(self) -> None:
        calls: list[str] = []
        client = _BrokerClient(calls)
        handler = EmbeddedMarketDataFeedHandler(
            settings=_settings(),
            client=client,
            feed=_MarketDataFeed(calls),
        )

        async def exercise():
            runtime = await handler.prepare()
            state = OptionChainState(master=runtime.master)
            contract = runtime.master.options[0]
            quote_refresh = await handler.refresh_option_quotes(
                state=state,
                contracts=(contract,),
            )
            greeks, greeks_refresh = await handler.refresh_option_greeks(
                underlying="NIFTY",
                expiry=contract.expiry,
                contracts=(contract,),
            )
            await handler.close()
            return state, contract, quote_refresh, greeks, greeks_refresh

        state, contract, quote_refresh, greeks, greeks_refresh = asyncio.run(
            exercise()
        )

        tick = state.latest_tick(contract.token.token)
        self.assertIsNotNone(tick)
        self.assertEqual(tick.ltp, Decimal("125.50"))
        self.assertEqual(quote_refresh["normalized_tokens"], ("option",))
        self.assertEqual(greeks_refresh["status"], "ok")
        self.assertEqual(greeks_refresh["normalized_tokens"], ("option",))
        self.assertEqual(greeks["option"].delta, Decimal("0.51"))
        self.assertIn("market_quote", calls)
        self.assertIn("option_greeks", calls)

    def test_reports_feed_health_and_delegates_rotation_operations(self) -> None:
        calls: list[str] = []
        feed = _MarketDataFeed(calls)
        handler = EmbeddedMarketDataFeedHandler(
            settings=_settings(),
            client=_BrokerClient(calls),
            feed=feed,
        )

        async def exercise():
            runtime = await handler.prepare()
            option = runtime.master.options[0].token
            await handler.subscribe((option,))
            await handler.unsubscribe((option,))
            health = handler.health_snapshot()
            await handler.close()
            return health

        health = asyncio.run(exercise())

        self.assertEqual(health["status"], "HEALTHY")
        self.assertEqual(feed.subscriptions, [("option",)])
        self.assertEqual(feed.unsubscriptions, [("option",)])

    def test_closed_handler_fails_closed(self) -> None:
        calls: list[str] = []
        handler = EmbeddedMarketDataFeedHandler(
            settings=_settings(),
            client=_BrokerClient(calls),
            feed=_MarketDataFeed(calls),
        )

        async def exercise():
            await handler.prepare()
            await handler.close()
            with self.assertRaisesRegex(RuntimeError, "is closed"):
                await handler.prepare()
            with self.assertRaisesRegex(RuntimeError, "is closed"):
                await handler.start(market_date=date(2026, 7, 29))

        asyncio.run(exercise())

    def test_worker_smoke_consumes_handler_without_direct_broker_ownership(self) -> None:
        handler = _SmokeFeedHandler()
        tick_store = _TickStore()
        chain_store = _ChainStore()
        live_store = _LiveStore()
        settings = _worker_settings()

        asyncio.run(
            run_market_data_worker(
                settings=settings,
                feed_handler=handler,
                tick_store=tick_store,
                chain_store=chain_store,
                live_store=live_store,
                max_ticks=1,
            )
        )

        self.assertEqual(handler.calls[0:3], ["prepare", "reference", "start"])
        self.assertIn("refresh_quotes", handler.calls)
        self.assertEqual(handler.calls[-1], "close")
        self.assertEqual(len(tick_store.ticks), 1)
        self.assertEqual(len(chain_store.snapshots), 1)
        self.assertEqual(len(live_store.snapshots), 1)
        snapshot = chain_store.snapshots[0]
        self.assertEqual(snapshot.spot_price, Decimal("24500"))
        self.assertEqual(
            {
                quote.contract.option_type: (
                    quote.ltp,
                    quote.bid,
                    quote.ask,
                    quote.oi,
                    quote.volume,
                )
                for quote in snapshot.quotes
            },
            {
                OptionType.CALL: (
                    Decimal("125"),
                    Decimal("124.9"),
                    Decimal("125.1"),
                    1000,
                    100,
                ),
                OptionType.PUT: (
                    Decimal("95"),
                    Decimal("94.9"),
                    Decimal("95.1"),
                    900,
                    80,
                ),
            },
        )

    def test_worker_closes_handler_when_start_fails(self) -> None:
        handler = _StartFailureFeedHandler()

        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            asyncio.run(
                run_market_data_worker(
                    settings=_worker_settings(),
                    feed_handler=handler,
                    tick_store=_TickStore(),
                    chain_store=_ChainStore(),
                    live_store=_LiveStore(),
                    max_ticks=1,
                )
            )

        self.assertEqual(handler.calls[-2:], ["start", "close"])

    def test_start_failure_finalizes_replay_recorder_as_failed(self) -> None:
        handler = _StartFailureFeedHandler()
        recorder = _Recorder()
        settings = replace(
            _worker_settings(),
            replay_capture_enabled=True,
        )

        with patch(
            "app.workers.market_data_worker.JsonlMicrostructureRecorder",
            return_value=recorder,
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                asyncio.run(
                    run_market_data_worker(
                        settings=settings,
                        feed_handler=handler,
                        tick_store=_TickStore(),
                        chain_store=_ChainStore(),
                        live_store=_LiveStore(),
                        max_ticks=1,
                    )
                )

        self.assertEqual(
            recorder.finished,
            {
                "processed_ticks": 0,
                "status": "failed",
                "error": "RuntimeError",
            },
        )

    def test_one_inflight_frame_uses_latest_spot_after_rest_wait(self) -> None:
        handler = _SlowFrameFeedHandler()
        tick_store = _ReleasingTickStore(handler)
        chain_store = _ChainStore()

        asyncio.run(
            run_market_data_worker(
                settings=_worker_settings(),
                feed_handler=handler,
                tick_store=tick_store,
                chain_store=chain_store,
                live_store=_LiveStore(),
                max_ticks=2,
            )
        )

        self.assertEqual(handler.refresh_count, 1)
        self.assertEqual(len(chain_store.snapshots), 1)
        self.assertEqual(
            chain_store.snapshots[0].spot_price,
            Decimal("24501"),
        )


def _settings():
    return replace(
        load_settings(),
        broker_name="angleone",
        broker_adapter_module="",
        default_underlyings=("NIFTY",),
    )


def _worker_settings():
    return replace(
        _settings(),
        strategy_config_path="config/strategy_config.json",
        strategy_profile="derivatives_only",
        storage_backend="jsonl",
        replay_capture_enabled=False,
        microstructure_enabled=False,
        option_greeks_enabled=False,
        signal_router_enabled=False,
        option_window_each_side=0,
    )


class _BrokerClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.login_count = 0

    async def login(self) -> BrokerSession:
        self.calls.append("login")
        self.login_count += 1
        return BrokerSession(
            access_token="access",
            refresh_token=None,
            feed_token="feed",
            raw={},
        )

    async def instrument_master(self):
        self.calls.append("instrument_master")
        return [
            {
                "exch_seg": "NSE",
                "token": "spot",
                "symbol": "Nifty 50",
                "name": "NIFTY",
            },
            {
                "exch_seg": "NSE",
                "token": "vix",
                "symbol": "India VIX",
                "name": "India VIX",
            },
            {
                "exch_seg": "NFO",
                "token": "future",
                "symbol": "NIFTY30JUL26FUT",
                "name": "NIFTY",
                "expiry": "30JUL2026",
                "lotsize": "75",
                "instrumenttype": "FUTIDX",
            },
            {
                "exch_seg": "NFO",
                "token": "option",
                "symbol": "NIFTY30JUL2624500CE",
                "name": "NIFTY",
                "expiry": "30JUL2026",
                "strike": "2450000",
                "lotsize": "75",
                "instrumenttype": "OPTIDX",
            },
        ]

    async def market_quote(self, *, mode, exchange_tokens):
        self.calls.append("market_quote")
        self.last_quote_request = (mode, exchange_tokens)
        return {
            "status": True,
            "data": {
                "fetched": [
                    {
                        "symbolToken": "option",
                        "ltp": "125.50",
                        "bid": "125.40",
                        "ask": "125.60",
                        "opnInterest": 1000,
                        "tradeVolume": 250,
                    }
                ]
            },
        }

    async def option_greeks(self, params):
        self.calls.append("option_greeks")
        self.last_greeks_request = params
        return {
            "status": True,
            "data": [
                {
                    "tradingSymbol": "NIFTY30JUL2624500CE",
                    "impliedVolatility": "12.4",
                    "delta": "0.51",
                    "gamma": "0.002",
                    "theta": "-3.2",
                    "vega": "8.1",
                }
            ],
        }

    async def ltp_data(self, **_kwargs):
        return {"data": {"ltp": "13.45"}}

    async def historical_candles(self, _params):
        market_date = date(2026, 7, 29)
        start = market_date - timedelta(days=21)
        return {
            "data": [
                [
                    f"{(start + timedelta(days=index)).isoformat()}"
                    "T09:15:00+05:30",
                    "100",
                    "106",
                    "96",
                    "101",
                    1000,
                ]
                for index in range(21)
            ]
        }

    async def close(self) -> None:
        self.calls.append("close")


class _MarketDataFeed:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.subscriptions: list[tuple[str, ...]] = []
        self.unsubscriptions: list[tuple[str, ...]] = []

    async def connect(self) -> None:
        self.calls.append("connect")

    async def subscribe(self, tokens) -> None:
        self.calls.append("subscribe")
        self.subscriptions.append(tuple(token.token for token in tokens))

    async def unsubscribe(self, tokens) -> None:
        self.calls.append("unsubscribe")
        self.unsubscriptions.append(tuple(token.token for token in tokens))

    async def ticks(self):
        if False:
            yield None

    def health_snapshot(self) -> dict[str, object]:
        return {"status": "HEALTHY", "queue_depth": 0}

    async def close(self) -> None:
        self.calls.append("feed_close")


class _SmokeFeedHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.at = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
        self.spot = InstrumentToken(
            Exchange.NSE,
            "spot",
            "NIFTY",
            "NIFTY",
            InstrumentKind.INDEX,
        )
        self.call_option = InstrumentToken(
            Exchange.NFO,
            "call_option",
            "NIFTY",
            "NIFTY30JUL2624500CE",
            InstrumentKind.OPTION,
        )
        self.put_option = InstrumentToken(
            Exchange.NFO,
            "put_option",
            "NIFTY",
            "NIFTY30JUL2624500PE",
            InstrumentKind.OPTION,
        )
        self.call_contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("24500"),
            option_type=OptionType.CALL,
            token=self.call_option,
            lot_size=75,
        )
        self.put_contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("24500"),
            option_type=OptionType.PUT,
            token=self.put_option,
            lot_size=75,
        )
        self.master = InstrumentMaster(
            options=(self.call_contract, self.put_contract),
            spot_tokens={"NIFTY": self.spot},
        )

    async def prepare(self) -> FeedHandlerRuntime:
        self.calls.append("prepare")
        return FeedHandlerRuntime(
            master=self.master,
            token_lookup={
                "spot": self.spot,
                "call_option": self.call_option,
                "put_option": self.put_option,
            },
        )

    async def initialize_reference_data(self, *, state, market_date):
        self.calls.append("reference")
        return {
            "india_vix": {"status": "UNAVAILABLE"},
            "previous_20d_atr": {
                "NIFTY": {"status": "UNAVAILABLE"}
            },
        }

    async def start(self, *, market_date):
        self.calls.append("start")
        return (self.spot,)

    async def subscribe(self, tokens) -> None:
        self.calls.append("subscribe")

    async def unsubscribe(self, tokens) -> None:
        self.calls.append("unsubscribe")

    async def ticks(self):
        self.calls.append("ticks")
        yield MarketTick(
            token=self.spot,
            exchange_timestamp=self.at,
            received_at=self.at,
            ltp=Decimal("24500"),
        )

    async def refresh_option_quotes(self, *, state, contracts):
        self.calls.append("refresh_quotes")
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
        for contract in contracts:
            ltp, bid, ask, oi, volume = values[contract.token.token]
            state.update_tick(MarketTick(
                token=contract.token,
                exchange_timestamp=self.at,
                received_at=self.at,
                ltp=ltp,
                oi=oi,
                volume=volume,
                bid=bid,
                ask=ask,
            ))
        token_ids = tuple(contract.token.token for contract in contracts)
        return {
            "status": "ok",
            "requested_at": self.at,
            "responded_at": self.at,
            "mode": "FULL",
            "exchange_tokens": {"NFO": list(token_ids)},
            "row_count": len(token_ids),
            "normalized_tokens": token_ids,
            "broker_status": True,
            "error": None,
        }

    async def refresh_option_greeks(self, **_kwargs):
        raise AssertionError("Greeks refresh is disabled in this smoke test")

    def health_snapshot(self):
        return {"status": "HEALTHY", "reason": None}

    async def close(self) -> None:
        self.calls.append("close")


class _StartFailureFeedHandler(_SmokeFeedHandler):
    async def start(self, *, market_date):
        self.calls.append("start")
        raise RuntimeError("startup failed")


class _SlowFrameFeedHandler(_SmokeFeedHandler):
    def __init__(self) -> None:
        super().__init__()
        self.quote_started = asyncio.Event()
        self.release_quote = asyncio.Event()
        self.refresh_count = 0

    async def ticks(self):
        self.calls.append("ticks")
        yield MarketTick(
            token=self.spot,
            exchange_timestamp=self.at,
            received_at=self.at,
            ltp=Decimal("24500"),
        )
        await self.quote_started.wait()
        yield MarketTick(
            token=self.spot,
            exchange_timestamp=self.at + timedelta(seconds=1),
            received_at=self.at + timedelta(seconds=1),
            ltp=Decimal("24501"),
        )

    async def refresh_option_quotes(self, *, state, contracts):
        self.refresh_count += 1
        self.quote_started.set()
        await self.release_quote.wait()
        return await super().refresh_option_quotes(
            state=state,
            contracts=contracts,
        )


class _TickStore:
    def __init__(self) -> None:
        self.ticks = []

    async def save_tick(self, tick) -> None:
        self.ticks.append(tick)


class _ReleasingTickStore(_TickStore):
    def __init__(self, handler: _SlowFrameFeedHandler) -> None:
        super().__init__()
        self.handler = handler

    async def save_tick(self, tick) -> None:
        await super().save_tick(tick)
        if len(self.ticks) == 2:
            self.handler.release_quote.set()


class _ChainStore:
    def __init__(self) -> None:
        self.snapshots = []

    async def save_chain_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)


class _LiveStore:
    def __init__(self) -> None:
        self.snapshots = []
        self.analytics = []

    async def publish_chain_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)

    async def publish_analytics_snapshot(self, analytics) -> None:
        self.analytics.append(analytics)


class _Recorder:
    def __init__(self) -> None:
        self.finished = None

    async def record_session_manifest(self, **_kwargs) -> None:
        return None

    async def record_instrument_master(self, **_kwargs) -> None:
        return None

    async def record_subscription_change(self, **_kwargs) -> None:
        return None

    async def finish(
        self,
        *,
        completed_at,
        processed_ticks,
        status,
        error,
    ) -> None:
        self.finished = {
            "processed_ticks": processed_ticks,
            "status": status,
            "error": error,
        }


if __name__ == "__main__":
    unittest.main()
