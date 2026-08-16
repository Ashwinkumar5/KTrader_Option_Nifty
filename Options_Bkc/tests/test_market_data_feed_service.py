from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.domain.models import (
    Exchange,
    GreeksSnapshot,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionContract,
    OptionType,
)
from app.instruments.master import InstrumentMaster
from app.marketdata.events import (
    FeedStatusEvent,
    MaterializedOptionChainFrame,
    RawMarketTickEvent,
)
from app.marketdata.feed_handler import FeedHandlerRuntime
from app.marketdata.feed_tape import MarketDataFeedTape
from app.marketdata.serde import (
    decode_market_data_event,
    encode_market_data_bootstrap,
)
from app.workers import market_data_feed_service as service_module


_EXPIRY = date(2026, 8, 13)
_AT = datetime(2026, 8, 12, 4, 21, tzinfo=UTC)


def _settings(*, interval_ms: int = 5_000) -> SimpleNamespace:
    return SimpleNamespace(
        market_timezone="Asia/Kolkata",
        default_underlyings=("NIFTY",),
        market_data_feed_interval_ms=interval_ms,
        option_window_each_side=0,
        option_greeks_enabled=True,
    )


def _instrument(
    *,
    exchange: Exchange,
    token: str,
    trading_symbol: str,
    kind: InstrumentKind,
) -> InstrumentToken:
    return InstrumentToken(
        exchange=exchange,
        token=token,
        symbol="NIFTY",
        trading_symbol=trading_symbol,
        kind=kind,
    )


class _FeedHandler:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_refresh: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.fail_start = fail_start
        self.fail_refresh = fail_refresh
        self.quote_refresh_count = 0
        self.greeks_refresh_count = 0
        self.spot = _instrument(
            exchange=Exchange.NSE,
            token="spot",
            trading_symbol="NIFTY",
            kind=InstrumentKind.INDEX,
        )
        self.call_token = _instrument(
            exchange=Exchange.NFO,
            token="atm_call",
            trading_symbol="NIFTY13AUG2624500CE",
            kind=InstrumentKind.OPTION,
        )
        self.put_token = _instrument(
            exchange=Exchange.NFO,
            token="atm_put",
            trading_symbol="NIFTY13AUG2624500PE",
            kind=InstrumentKind.OPTION,
        )
        self.call_contract = OptionContract(
            underlying="NIFTY",
            expiry=_EXPIRY,
            strike=Decimal("24500"),
            option_type=OptionType.CALL,
            token=self.call_token,
            lot_size=75,
        )
        self.put_contract = OptionContract(
            underlying="NIFTY",
            expiry=_EXPIRY,
            strike=Decimal("24500"),
            option_type=OptionType.PUT,
            token=self.put_token,
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
                self.spot.token: self.spot,
                self.call_token.token: self.call_token,
                self.put_token.token: self.put_token,
            },
        )

    async def initialize_reference_data(self, *, state, market_date):
        self.calls.append("reference")
        state.set_previous_20d_atr("NIFTY", Decimal("181.75"))
        return {
            "india_vix": {"status": "OK", "value": "12.71"},
            "previous_20d_atr": {
                "NIFTY": {"status": "OK", "value": "181.75"}
            },
        }

    async def start(self, *, market_date):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("feed start failed")
        return (self.spot,)

    async def subscribe(self, tokens) -> None:
        self.calls.append(
            "subscribe:" + ",".join(token.token for token in tokens)
        )

    async def unsubscribe(self, tokens) -> None:
        self.calls.append(
            "unsubscribe:" + ",".join(token.token for token in tokens)
        )

    async def ticks(self):
        self.calls.append("ticks")
        yield self._spot_tick(Decimal("24500"), seconds=0)

    def _spot_tick(self, value: Decimal, *, seconds: int) -> MarketTick:
        observed_at = _AT + timedelta(seconds=seconds)
        return MarketTick(
            token=self.spot,
            exchange_timestamp=observed_at,
            received_at=observed_at + timedelta(milliseconds=1),
            ltp=value,
            open_price=Decimal("24480"),
            high_price=max(value, Decimal("24510")),
            low_price=Decimal("24470"),
            close_price=Decimal("24490"),
        )

    async def refresh_option_quotes(self, *, state, contracts):
        self.calls.append("refresh_quotes")
        self.quote_refresh_count += 1
        if self.fail_refresh:
            raise RuntimeError("quote refresh failed")
        return self._populate_quotes(state=state, contracts=contracts)

    def _populate_quotes(self, *, state, contracts):
        values = {
            "atm_call": (
                Decimal("135.30"),
                Decimal("135.25"),
                Decimal("135.35"),
                1_250_000,
                840_125,
            ),
            "atm_put": (
                Decimal("81.25"),
                Decimal("81.20"),
                Decimal("81.30"),
                1_410_000,
                910_225,
            ),
        }
        for contract in contracts:
            ltp, bid, ask, oi, volume = values[contract.token.token]
            state.update_tick(
                MarketTick(
                    token=contract.token,
                    exchange_timestamp=_AT,
                    received_at=_AT + timedelta(milliseconds=10),
                    ltp=ltp,
                    open_price=ltp - Decimal("1.00"),
                    high_price=ltp + Decimal("2.00"),
                    low_price=ltp - Decimal("2.00"),
                    close_price=ltp - Decimal("0.50"),
                    oi=oi,
                    oi_change=25_000 if contract.option_type is OptionType.CALL else -18_000,
                    volume=volume,
                    bid=bid,
                    ask=ask,
                )
            )
        token_ids = tuple(contract.token.token for contract in contracts)
        return {
            "status": "ok",
            "requested_at": _AT,
            "responded_at": _AT + timedelta(milliseconds=20),
            "attempts": 1,
            "mode": "FULL",
            "exchange_tokens": {"NFO": list(token_ids)},
            "row_count": len(token_ids),
            "normalized_tokens": token_ids,
            "broker_status": True,
            "error": None,
        }

    async def refresh_option_greeks(self, *, underlying, expiry, contracts):
        self.calls.append("refresh_greeks")
        self.greeks_refresh_count += 1
        values = {
            OptionType.CALL: Decimal("0.519"),
            OptionType.PUT: Decimal("-0.481"),
        }
        greeks = {
            contract.token.token: GreeksSnapshot(
                contract=contract,
                captured_at=_AT + timedelta(milliseconds=30),
                implied_volatility=Decimal("12.10"),
                delta=values[contract.option_type],
                gamma=Decimal("0.00142"),
                theta=Decimal("-4.50"),
                vega=Decimal("7.34"),
                source="fake-broker",
            )
            for contract in contracts
        }
        token_ids = tuple(contract.token.token for contract in contracts)
        return greeks, {
            "status": "ok",
            "requested_at": _AT + timedelta(milliseconds=21),
            "responded_at": _AT + timedelta(milliseconds=40),
            "attempts": 1,
            "mode": "optionGreek",
            "row_count": len(token_ids),
            "normalized_tokens": token_ids,
            "broker_status": True,
        }

    def health_snapshot(self) -> dict[str, object]:
        return {
            "status": "HEALTHY",
            "queue_depth": 2,
            "queue_capacity": 8_192,
            "received_events": 3,
            "dropped_events": 0,
            "last_received_at": _AT,
        }

    async def close(self) -> None:
        self.calls.append("close")


class _SlowFeedHandler(_FeedHandler):
    def __init__(self) -> None:
        super().__init__()
        self.quote_started = asyncio.Event()
        self.release_quote = asyncio.Event()
        self.inflight_refreshes = 0
        self.maximum_inflight_refreshes = 0

    async def ticks(self):
        self.calls.append("ticks")
        yield self._spot_tick(Decimal("24500"), seconds=0)
        await self.quote_started.wait()
        yield self._spot_tick(Decimal("24510"), seconds=1)
        yield self._spot_tick(Decimal("24520"), seconds=2)

    async def refresh_option_quotes(self, *, state, contracts):
        self.calls.append("refresh_quotes")
        self.quote_refresh_count += 1
        self.inflight_refreshes += 1
        self.maximum_inflight_refreshes = max(
            self.maximum_inflight_refreshes,
            self.inflight_refreshes,
        )
        self.quote_started.set()
        try:
            await self.release_quote.wait()
            return self._populate_quotes(state=state, contracts=contracts)
        finally:
            self.inflight_refreshes -= 1


class _FanoutPublisher:
    def __init__(self, *, fail_on_calls: set[int] | None = None) -> None:
        self.fail_on_calls = fail_on_calls or set()
        self.start_count = 0
        self.flush_count = 0
        self.close_count = 0
        self.publish_count = 0
        self.bootstrap = None
        self.accepted: list[bytes] = []
        self.subscribers: tuple[list[object], list[object]] = ([], [])
        self.on_event = None

    async def start(self, bootstrap) -> None:
        self.start_count += 1
        self.bootstrap = bootstrap

    def publish_encoded(self, payload: bytes) -> bool:
        self.publish_count += 1
        if self.publish_count in self.fail_on_calls:
            return False
        encoded = bytes(payload)
        event = decode_market_data_event(encoded)
        self.accepted.append(encoded)
        for subscriber in self.subscribers:
            subscriber.append(event)
        if self.on_event is not None:
            self.on_event(event)
        return True

    async def flush(self) -> None:
        self.flush_count += 1

    def health_snapshot(self) -> dict[str, object]:
        return {"status": "HEALTHY"}

    async def close(self) -> None:
        self.close_count += 1


class _Tape:
    def __init__(self, *, fail_on_calls: set[int] | None = None) -> None:
        self.fail_on_calls = fail_on_calls or set()
        self.record_count = 0
        self.close_count = 0
        self.recorded: list[bytes] = []

    def record_encoded(self, payload: bytes) -> bool:
        self.record_count += 1
        if self.record_count in self.fail_on_calls:
            return False
        self.recorded.append(bytes(payload))
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {"status": "HEALTHY"}

    async def close(self) -> None:
        self.close_count += 1


class _SteppingDateTime:
    current = _AT

    @classmethod
    def reset(cls) -> None:
        cls.current = _AT

    @classmethod
    def now(cls, tz=None) -> datetime:
        value = cls.current
        cls.current += timedelta(seconds=10)
        return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)


class MarketDataFeedServiceTests(unittest.TestCase):
    def test_single_owner_fanout_publishes_tick_then_complete_frame(self) -> None:
        handler = _FeedHandler()
        publisher = _FanoutPublisher()

        tape_path = (
            Path(".test-tmp")
            / "market-data-feed-service"
            / "canonical-feed.jsonl"
        )
        tape_path.parent.mkdir(parents=True, exist_ok=True)
        tape_path.unlink(missing_ok=True)
        self.addCleanup(tape_path.unlink, missing_ok=True)
        tape = MarketDataFeedTape(tape_path, queue_capacity=16)
        asyncio.run(
            service_module.run_market_data_feed_service(
                settings=_settings(),
                feed_handler=handler,
                publisher=publisher,
                tape=tape,
                max_ticks=1,
            )
        )
        tape_payloads = tape_path.read_bytes().splitlines()

        self.assertEqual(handler.calls.count("prepare"), 1)
        self.assertEqual(handler.calls.count("start"), 1)
        self.assertEqual(handler.calls.count("close"), 1)
        self.assertEqual(handler.quote_refresh_count, 1)
        self.assertEqual(handler.greeks_refresh_count, 1)
        self.assertEqual(publisher.start_count, 1)
        self.assertEqual(publisher.close_count, 1)
        self.assertEqual(publisher.subscribers[0], publisher.subscribers[1])
        self.assertEqual(
            tape_payloads,
            [encode_market_data_bootstrap(publisher.bootstrap)]
            + publisher.accepted,
        )
        self.assertEqual(tape.written, 4)

        first_subscriber = publisher.subscribers[0]
        self.assertEqual(
            [type(event) for event in first_subscriber],
            [FeedStatusEvent, RawMarketTickEvent, MaterializedOptionChainFrame],
        )
        frame = first_subscriber[-1]
        quotes = {
            quote.contract.option_type: quote
            for quote in frame.snapshot.quotes
        }
        self.assertEqual(set(quotes), {OptionType.CALL, OptionType.PUT})
        self.assertEqual(quotes[OptionType.CALL].bid, Decimal("135.25"))
        self.assertEqual(quotes[OptionType.CALL].ask, Decimal("135.35"))
        self.assertEqual(quotes[OptionType.CALL].oi, 1_250_000)
        self.assertEqual(quotes[OptionType.PUT].volume, 910_225)
        self.assertEqual(
            quotes[OptionType.PUT].greeks.delta,
            Decimal("-0.481"),
        )
        self.assertEqual(frame.quote_refresh.row_count, 2)
        self.assertEqual(frame.greeks_refresh.row_count, 2)
        self.assertEqual(frame.feed_health.status, "HEALTHY")

    def test_publisher_queue_failure_is_fatal_and_closes_every_owner(self) -> None:
        handler = _FeedHandler()
        publisher = _FanoutPublisher(fail_on_calls={2})
        tape = _Tape()

        with self.assertRaisesRegex(RuntimeError, "publisher queue is full"):
            asyncio.run(
                service_module.run_market_data_feed_service(
                    settings=_settings(),
                    feed_handler=handler,
                    publisher=publisher,
                    tape=tape,
                    max_ticks=1,
                )
            )

        self.assertEqual(handler.calls.count("close"), 1)
        self.assertEqual(publisher.close_count, 1)
        self.assertEqual(tape.close_count, 1)
        self.assertEqual(handler.quote_refresh_count, 0)

    def test_tape_admission_failure_is_fatal_and_closes_every_owner(self) -> None:
        handler = _FeedHandler()
        publisher = _FanoutPublisher()
        tape = _Tape(fail_on_calls={3})

        with self.assertRaisesRegex(RuntimeError, "tape queue is full"):
            asyncio.run(
                service_module.run_market_data_feed_service(
                    settings=_settings(),
                    feed_handler=handler,
                    publisher=publisher,
                    tape=tape,
                    max_ticks=1,
                )
            )

        self.assertEqual(handler.calls.count("close"), 1)
        self.assertEqual(publisher.close_count, 1)
        self.assertEqual(tape.close_count, 1)
        self.assertEqual(handler.quote_refresh_count, 0)
        self.assertTrue(
            any(
                isinstance(event, RawMarketTickEvent)
                for event in publisher.subscribers[0]
            )
        )

    def test_start_failure_cleans_up_publisher_tape_and_handler(self) -> None:
        handler = _FeedHandler(fail_start=True)
        publisher = _FanoutPublisher()
        tape = _Tape()

        with self.assertRaisesRegex(RuntimeError, "feed start failed"):
            asyncio.run(
                service_module.run_market_data_feed_service(
                    settings=_settings(),
                    feed_handler=handler,
                    publisher=publisher,
                    tape=tape,
                    max_ticks=1,
                )
            )

        self.assertEqual(handler.calls.count("prepare"), 1)
        self.assertEqual(handler.calls.count("start"), 1)
        self.assertEqual(handler.calls.count("close"), 1)
        # The bootstrap responder is intentionally not exposed until the
        # broker feed and initial subscriptions are ready.
        self.assertEqual(publisher.start_count, 0)
        self.assertEqual(publisher.close_count, 1)
        self.assertEqual(tape.close_count, 1)

    def test_refresh_failure_is_fatal_and_cleans_up_all_resources(self) -> None:
        handler = _FeedHandler(fail_refresh=True)
        publisher = _FanoutPublisher()
        tape = _Tape()

        with self.assertRaisesRegex(RuntimeError, "quote refresh failed"):
            asyncio.run(
                service_module.run_market_data_feed_service(
                    settings=_settings(),
                    feed_handler=handler,
                    publisher=publisher,
                    tape=tape,
                    max_ticks=1,
                )
            )

        self.assertEqual(handler.quote_refresh_count, 1)
        self.assertEqual(handler.greeks_refresh_count, 0)
        self.assertEqual(handler.calls.count("close"), 1)
        self.assertEqual(publisher.close_count, 1)
        self.assertEqual(tape.close_count, 1)
        self.assertFalse(
            any(
                isinstance(event, MaterializedOptionChainFrame)
                for event in publisher.subscribers[0]
            )
        )
        self.assertEqual(publisher.subscribers[0][-1].status, "FAILED")

    def test_burst_has_one_frame_task_and_uses_latest_spot_after_rest(self) -> None:
        handler = _SlowFeedHandler()
        publisher = _FanoutPublisher()
        tape = _Tape()

        def release_after_latest_tick(event) -> None:
            if (
                isinstance(event, RawMarketTickEvent)
                and event.tick.ltp == Decimal("24520")
            ):
                handler.release_quote.set()

        publisher.on_event = release_after_latest_tick
        _SteppingDateTime.reset()
        with patch.object(service_module, "datetime", _SteppingDateTime):
            asyncio.run(
                service_module.run_market_data_feed_service(
                    settings=_settings(interval_ms=1),
                    feed_handler=handler,
                    publisher=publisher,
                    tape=tape,
                    max_ticks=3,
                )
            )

        frames = [
            event
            for event in publisher.subscribers[0]
            if isinstance(event, MaterializedOptionChainFrame)
        ]
        self.assertEqual(handler.quote_refresh_count, 1)
        self.assertEqual(handler.greeks_refresh_count, 1)
        self.assertEqual(handler.maximum_inflight_refreshes, 1)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].snapshot.spot_price, Decimal("24520"))
        self.assertEqual(
            frames[0].spot_observed_at,
            _AT + timedelta(seconds=2, milliseconds=1),
        )
        event_types = [type(event) for event in publisher.subscribers[0]]
        self.assertEqual(
            event_types,
            [
                FeedStatusEvent,
                RawMarketTickEvent,
                RawMarketTickEvent,
                RawMarketTickEvent,
                MaterializedOptionChainFrame,
            ],
        )


if __name__ == "__main__":
    unittest.main()
