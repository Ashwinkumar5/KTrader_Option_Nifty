from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

import orjson

from app.domain.models import (
    Exchange,
    FutureContract,
    GreeksSnapshot,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    TickQuality,
    UnderlyingMarketSnapshot,
    UnderlyingReference,
)
from app.marketdata.events import (
    FeedHealthSnapshot,
    MarketDataBootstrap,
    MaterializedOptionChainFrame,
    RawMarketTickEvent,
    RefreshProvenance,
)
from app.marketdata.serde import (
    DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
    decode_market_data_bootstrap,
    decode_market_data_event,
    encode_market_data_bootstrap,
    encode_market_data_event,
)


_EXPIRY = date(2026, 8, 13)
_CAPTURED_AT = datetime(2026, 8, 12, 4, 21, 5, 125000, tzinfo=UTC)


def _token(
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


_SPOT_TOKEN = _token(
    exchange=Exchange.NSE,
    token="99926000",
    trading_symbol="NIFTY",
    kind=InstrumentKind.INDEX,
)
_FUTURE_TOKEN = _token(
    exchange=Exchange.NFO,
    token="57001",
    trading_symbol="NIFTY13AUG26FUT",
    kind=InstrumentKind.FUTURE,
)
_VIX_TOKEN = InstrumentToken(
    exchange=Exchange.NSE,
    token="99926017",
    symbol="INDIA_VIX",
    trading_symbol="INDIA VIX",
    kind=InstrumentKind.INDEX,
)


def _option_contract(option_type: OptionType) -> OptionContract:
    suffix = option_type.value
    return OptionContract(
        underlying="NIFTY",
        expiry=_EXPIRY,
        strike=Decimal("24550"),
        option_type=option_type,
        token=_token(
            exchange=Exchange.NFO,
            token="57101" if option_type is OptionType.CALL else "57102",
            trading_symbol=f"NIFTY13AUG2624550{suffix}",
            kind=InstrumentKind.OPTION,
        ),
        lot_size=75,
    )


def _option_quote(option_type: OptionType) -> OptionQuote:
    contract = _option_contract(option_type)
    is_call = option_type is OptionType.CALL
    return OptionQuote(
        contract=contract,
        ltp=Decimal("135.30") if is_call else Decimal("81.25"),
        open_price=Decimal("132.45") if is_call else Decimal("84.30"),
        high_price=Decimal("136.25") if is_call else Decimal("84.50"),
        low_price=Decimal("130.75") if is_call else Decimal("78.60"),
        close_price=Decimal("133.10") if is_call else Decimal("83.80"),
        oi=1_250_000 if is_call else 1_410_000,
        oi_change=25_000 if is_call else -18_000,
        oi_change_percent=(
            Decimal("2.0408") if is_call else Decimal("-1.2605")
        ),
        volume=840_125 if is_call else 910_225,
        bid=Decimal("135.25") if is_call else Decimal("81.20"),
        ask=Decimal("135.35") if is_call else Decimal("81.30"),
        greeks=GreeksSnapshot(
            contract=contract,
            captured_at=_CAPTURED_AT,
            implied_volatility=(
                Decimal("11.82") if is_call else Decimal("12.14")
            ),
            delta=Decimal("0.519") if is_call else Decimal("-0.481"),
            gamma=Decimal("0.00142"),
            theta=Decimal("-4.62") if is_call else Decimal("-4.38"),
            vega=Decimal("7.34"),
            source="angleone-option-greeks",
        ),
    )


def _raw_tick_event() -> RawMarketTickEvent:
    return RawMarketTickEvent(
        handler_epoch="feed-epoch-20260812-a",
        event_id="tick-0000000001",
        published_at=_CAPTURED_AT,
        tick=MarketTick(
            token=_FUTURE_TOKEN,
            exchange_timestamp=_CAPTURED_AT,
            received_at=datetime(
                2026,
                8,
                12,
                4,
                21,
                5,
                127000,
                tzinfo=UTC,
            ),
            ltp=Decimal("24570.65"),
            open_price=Decimal("24507.65"),
            high_price=Decimal("24578.40"),
            low_price=Decimal("24495.20"),
            close_price=Decimal("24505.15"),
            oi=11_255_750,
            oi_change=-95_250,
            oi_change_percent=Decimal("-0.8394"),
            volume=1_842_375,
            bid=Decimal("24570.60"),
            ask=Decimal("24570.70"),
            quality=TickQuality.LIVE,
            raw={
                "subscription_mode": 3,
                "exchange_type": 2,
                "sequence_number": 834_115,
                "exchange_timestamp": 1_786_506_065_125,
                "best_5_buy_data": [
                    {"price": 2_457_060, "quantity": 450, "orders": 12},
                    {"price": 2_457_055, "quantity": 825, "orders": 19},
                ],
                "best_5_sell_data": [
                    {"price": 2_457_070, "quantity": 375, "orders": 9},
                    {"price": 2_457_075, "quantity": 600, "orders": 15},
                ],
            },
        ),
    )


def _frame_event() -> MaterializedOptionChainFrame:
    return MaterializedOptionChainFrame(
        handler_epoch="feed-epoch-20260812-a",
        event_id="frame-0000000421",
        published_at=datetime(
            2026,
            8,
            12,
            4,
            21,
            5,
            250000,
            tzinfo=UTC,
        ),
        snapshot=OptionChainSnapshot(
            underlying="NIFTY",
            expiry=_EXPIRY,
            spot_price=Decimal("24570.65"),
            atm_strike=Decimal("24550"),
            captured_at=_CAPTURED_AT,
            quotes=(
                _option_quote(OptionType.CALL),
                _option_quote(OptionType.PUT),
            ),
            reference=UnderlyingReference(
                underlying="NIFTY",
                index_token=_SPOT_TOKEN,
                future_token=_FUTURE_TOKEN,
                index_price=Decimal("24570.65"),
                future_price=Decimal("24582.10"),
                basis=Decimal("11.45"),
            ),
            market=UnderlyingMarketSnapshot(
                underlying="NIFTY",
                captured_at=_CAPTURED_AT,
                spot_observed_at=datetime(
                    2026, 8, 12, 4, 21, 5, 100000, tzinfo=UTC
                ),
                open_price=Decimal("24507.65"),
                high_price=Decimal("24578.40"),
                low_price=Decimal("24495.20"),
                previous_close=Decimal("24505.15"),
                future_observed_at=datetime(
                    2026, 8, 12, 4, 21, 5, 110000, tzinfo=UTC
                ),
                future_price=Decimal("24582.10"),
                future_open=Decimal("24520.50"),
                future_high=Decimal("24590.25"),
                future_low=Decimal("24505.30"),
                future_previous_close=Decimal("24517.40"),
                future_volume=1_842_375,
                future_oi=11_255_750,
                future_vwap=Decimal("24548.35"),
                basis=Decimal("11.45"),
                previous_20d_atr=Decimal("181.75"),
                previous_session_expected_move=Decimal("151.20"),
                market_breadth=Decimal("0.43"),
                india_vix=Decimal("12.71"),
            ),
        ),
        scheduled_for=datetime(2026, 8, 12, 4, 21, 5, tzinfo=UTC),
        frame_started_at=datetime(
            2026, 8, 12, 4, 21, 5, 105000, tzinfo=UTC
        ),
        trigger_tick_received_at=datetime(
            2026, 8, 12, 4, 21, 5, 102000, tzinfo=UTC
        ),
        spot_observed_at=datetime(
            2026, 8, 12, 4, 21, 5, 100000, tzinfo=UTC
        ),
        window_each_side=4,
        source_interval_ms=5_000,
        quote_refresh=RefreshProvenance(
            status="success",
            requested_at=datetime(
                2026, 8, 12, 4, 21, 5, 108000, tzinfo=UTC
            ),
            responded_at=datetime(
                2026, 8, 12, 4, 21, 5, 180000, tzinfo=UTC
            ),
            attempts=1,
            row_count=2,
            normalized_tokens=("57101", "57102"),
            exchange_tokens=(("NFO", ("57101", "57102")),),
            mode="FULL",
            broker_status=True,
        ),
        greeks_refresh=RefreshProvenance(
            status="success",
            requested_at=datetime(
                2026, 8, 12, 4, 21, 5, 181000, tzinfo=UTC
            ),
            responded_at=datetime(
                2026, 8, 12, 4, 21, 5, 225000, tzinfo=UTC
            ),
            attempts=1,
            row_count=2,
            normalized_tokens=("57101", "57102"),
            mode="optionGreek",
            broker_status=True,
        ),
        feed_health=FeedHealthSnapshot(
            status="HEALTHY",
            reason="receiving",
            queue_depth=3,
            queue_capacity=8_192,
            queue_pressure_threshold=6_144,
            queue_high_watermark=51,
            received_events=8_341,
            enqueued_events=8_341,
            dropped_events=0,
            queue_pressure_events=0,
            last_received_at=datetime(
                2026, 8, 12, 4, 21, 5, 127000, tzinfo=UTC
            ),
        ),
    )


def _bootstrap() -> MarketDataBootstrap:
    return MarketDataBootstrap(
        handler_epoch="feed-epoch-20260812-a",
        generated_at=_CAPTURED_AT,
        source_interval_ms=5_000,
        option_window_each_side=4,
        selected_expiries=(("NIFTY", _EXPIRY),),
        spot_tokens=(_SPOT_TOKEN,),
        option_contracts=(
            _option_contract(OptionType.CALL),
            _option_contract(OptionType.PUT),
        ),
        future_contracts=(
            FutureContract(
                underlying="NIFTY",
                expiry=_EXPIRY,
                token=_FUTURE_TOKEN,
                lot_size=75,
            ),
        ),
        reference_tokens=(_VIX_TOKEN,),
        reference_values=(("INDIA_VIX", Decimal("12.71")),),
        previous_20d_atr=(("NIFTY", Decimal("181.75")),),
    )


def _replace_event_value(
    event: RawMarketTickEvent | MaterializedOptionChainFrame,
    *path: str | int,
    value: object,
) -> bytes:
    raw = orjson.loads(encode_market_data_event(event))
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return orjson.dumps(raw, option=orjson.OPT_SORT_KEYS)


def _delete_event_value(
    event: RawMarketTickEvent | MaterializedOptionChainFrame,
    *path: str | int,
) -> bytes:
    raw = orjson.loads(encode_market_data_event(event))
    target = raw
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    return orjson.dumps(raw, option=orjson.OPT_SORT_KEYS)


class MarketDataTransportCodecTests(unittest.TestCase):
    def test_raw_tick_round_trip_preserves_depth_and_all_tick_fields(self) -> None:
        event = _raw_tick_event()

        decoded = decode_market_data_event(encode_market_data_event(event))

        self.assertEqual(decoded, event)
        self.assertEqual(decoded.tick.raw["best_5_buy_data"][0]["orders"], 12)
        self.assertEqual(decoded.tick.oi_change, -95_250)

    def test_materialized_frame_round_trip_is_lossless(self) -> None:
        frame = _frame_event()

        decoded = decode_market_data_event(encode_market_data_event(frame))

        self.assertEqual(decoded, frame)
        self.assertEqual(decoded.snapshot.reference.future_token, _FUTURE_TOKEN)
        self.assertEqual(decoded.snapshot.market.future_oi, 11_255_750)
        self.assertEqual(decoded.snapshot.quotes[0].bid, Decimal("135.25"))
        self.assertEqual(decoded.snapshot.quotes[1].ask, Decimal("81.30"))
        self.assertEqual(
            decoded.snapshot.quotes[1].greeks.source,
            "angleone-option-greeks",
        )
        self.assertEqual(decoded.quote_refresh.exchange_tokens[0][0], "NFO")
        self.assertEqual(decoded.feed_health.queue_capacity, 8_192)

    def test_bootstrap_round_trip_contains_no_broker_credentials(self) -> None:
        bootstrap = _bootstrap()

        payload = encode_market_data_bootstrap(bootstrap)
        decoded = decode_market_data_bootstrap(payload)

        self.assertEqual(decoded, bootstrap)
        self.assertEqual(
            decoded.instrument_master().nearest_future(
                underlying="NIFTY",
                as_of=_EXPIRY,
            ).token,
            _FUTURE_TOKEN,
        )
        raw = orjson.loads(payload)
        credential_fields = {
            "api_key",
            "client_code",
            "client_id",
            "password",
            "pin",
            "totp",
            "jwt_token",
            "feed_token",
            "refresh_token",
            "authorization",
        }

        def all_keys(value: object) -> set[str]:
            if isinstance(value, dict):
                return set(value).union(
                    *(all_keys(item) for item in value.values())
                )
            if isinstance(value, list):
                return set().union(*(all_keys(item) for item in value))
            return set()

        self.assertTrue(credential_fields.isdisjoint(all_keys(raw)))

    def test_codec_output_is_canonical_after_decode_and_reencode(self) -> None:
        for value, encode, decode in (
            (_raw_tick_event(), encode_market_data_event, decode_market_data_event),
            (_frame_event(), encode_market_data_event, decode_market_data_event),
            (
                _bootstrap(),
                encode_market_data_bootstrap,
                decode_market_data_bootstrap,
            ),
        ):
            with self.subTest(value_type=type(value).__name__):
                encoded = encode(value)
                self.assertEqual(encode(decode(encoded)), encoded)

    def test_unknown_schema_and_message_types_are_rejected(self) -> None:
        event = _raw_tick_event()
        cases = (
            _replace_event_value(event, "schema_version", value=2),
            _replace_event_value(event, "schema_version", value=True),
            _replace_event_value(event, "event_type", value="future-version"),
        )
        for payload in cases:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ValueError):
                    decode_market_data_event(payload)

        raw_bootstrap = orjson.loads(
            encode_market_data_bootstrap(_bootstrap())
        )
        raw_bootstrap["message_type"] = "credentials"
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            decode_market_data_bootstrap(
                orjson.dumps(raw_bootstrap, option=orjson.OPT_SORT_KEYS)
            )

    def test_missing_or_naive_required_timestamps_are_rejected(self) -> None:
        frame = _frame_event()
        cases = (
            _delete_event_value(frame, "published_at"),
            _delete_event_value(frame, "snapshot", "captured_at"),
            _replace_event_value(
                frame,
                "published_at",
                value="2026-08-12T04:21:05.250000",
            ),
            _replace_event_value(
                frame,
                "snapshot",
                "quotes",
                0,
                "greeks",
                "captured_at",
                value="2026-08-12T04:21:05.125000",
            ),
        )
        for payload in cases:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ValueError):
                    decode_market_data_event(payload)

        raw_bootstrap = orjson.loads(
            encode_market_data_bootstrap(_bootstrap())
        )
        raw_bootstrap["generated_at"] = "2026-08-12T04:21:05"
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            decode_market_data_bootstrap(
                orjson.dumps(raw_bootstrap, option=orjson.OPT_SORT_KEYS)
            )

    def test_nonfinite_or_negative_required_numbers_are_rejected(self) -> None:
        frame = _frame_event()
        cases = (
            _replace_event_value(
                frame,
                "snapshot",
                "spot_price",
                value="NaN",
            ),
            _replace_event_value(
                frame,
                "snapshot",
                "atm_strike",
                value="Infinity",
            ),
            _replace_event_value(frame, "source_interval_ms", value=-1),
            _replace_event_value(frame, "window_each_side", value=-1),
        )
        for payload in cases:
            with self.subTest(payload=payload[:80]):
                with self.assertRaises(ValueError):
                    decode_market_data_event(payload)

        raw_bootstrap = orjson.loads(
            encode_market_data_bootstrap(_bootstrap())
        )
        raw_bootstrap["previous_20d_atr"][0][1] = "-0.01"
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            decode_market_data_bootstrap(
                orjson.dumps(raw_bootstrap, option=orjson.OPT_SORT_KEYS)
            )

    def test_oversized_payload_is_rejected_before_json_decoding(self) -> None:
        payload = b"{" + (
            b" " * DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES
        )

        with self.assertRaisesRegex(ValueError, "exceeds"):
            decode_market_data_event(payload)


if __name__ == "__main__":
    unittest.main()
