from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain.models import Exchange, InstrumentKind, InstrumentToken, MarketTick
from app.microstructure.engine import MicrostructureEngine, MicrostructureSettings


def _option_tick(
    *,
    at: datetime,
    ltp: str,
    bid_quantity: int,
    ask_quantity: int,
    bid_price: str | None = None,
    ask_price: str | None = None,
    option_type: str = "CE",
) -> MarketTick:
    token_value = "111" if option_type == "CE" else "112"
    token = InstrumentToken(
        exchange=Exchange.NFO,
        token=token_value,
        symbol="NIFTY",
        trading_symbol=f"NIFTY24JUL2624250{option_type}",
        kind=InstrumentKind.OPTION,
    )
    return MarketTick(
        token=token,
        exchange_timestamp=at,
        received_at=at,
        ltp=Decimal(ltp),
        raw={
            "depth": {
                "buy": [
                    {
                        "price": bid_price or ltp,
                        "quantity": bid_quantity,
                    }
                ]
                * 5,
                "sell": [
                    {
                        "price": ask_price
                        or str(Decimal(ltp) + Decimal("0.50")),
                        "quantity": ask_quantity,
                    }
                ]
                * 5,
            }
        },
    )


def _future_tick(
    *,
    at: datetime,
    ltp: str,
    bid_quantity: int,
    ask_quantity: int,
) -> MarketTick:
    token = InstrumentToken(
        exchange=Exchange.NFO,
        token="FUT1",
        symbol="NIFTY",
        trading_symbol="NIFTY30JUL26FUT",
        kind=InstrumentKind.FUTURE,
    )
    return MarketTick(
        token=token,
        exchange_timestamp=at,
        received_at=at,
        ltp=Decimal(ltp),
        raw={
            "depth": {
                "buy": [
                    {
                        "price": str(Decimal(ltp) - Decimal("0.05")),
                        "quantity": bid_quantity,
                    }
                ]
                * 5,
                "sell": [
                    {
                        "price": ltp,
                        "quantity": ask_quantity,
                    }
                ]
                * 5,
            }
        },
    )


class MicrostructureEngineTests(unittest.TestCase):
    def test_first_complete_book_is_only_an_ofi_baseline(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
            )
        )
        features, candidate = engine.observe(
            _option_tick(
                at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
                ltp="100",
                bid_quantity=2000,
                ask_quantity=100,
            )
        )

        self.assertIsNotNone(features)
        assert features is not None
        self.assertIsNone(features.book_imbalance)
        self.assertIsNone(candidate)

    def test_dynamic_ofi_detects_bid_depletion_despite_positive_static_depth(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        engine.observe(
            _option_tick(
                at=start,
                ltp="100",
                bid_quantity=2000,
                ask_quantity=1000,
                bid_price="100",
                ask_price="100.50",
            )
        )
        features, _ = engine.observe(
            _option_tick(
                at=start + timedelta(seconds=1),
                ltp="100.10",
                bid_quantity=1200,
                ask_quantity=1000,
                bid_price="100",
                ask_price="100.50",
            )
        )

        self.assertIsNotNone(features)
        assert features is not None
        self.assertEqual(features.book_imbalance, Decimal("-0.7273"))

    def test_dynamic_ofi_adds_bid_growth_and_ask_removal(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        engine.observe(
            _option_tick(
                at=start,
                ltp="100",
                bid_quantity=1000,
                ask_quantity=800,
                bid_price="100",
                ask_price="100.50",
            )
        )
        features, _ = engine.observe(
            _option_tick(
                at=start + timedelta(seconds=1),
                ltp="100.10",
                bid_quantity=1600,
                ask_quantity=500,
                bid_price="100",
                ask_price="100.50",
            )
        )

        self.assertIsNotNone(features)
        assert features is not None
        self.assertEqual(features.book_imbalance, Decimal("0.8571"))

    def test_positive_option_ofi_confirms_both_call_and_put_buying(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
                require_directional_option_book=True,
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        candidates = {}
        for option_type in ("CE", "PE"):
            engine.observe(
                _option_tick(
                    at=start,
                    ltp="100",
                    bid_quantity=1000,
                    ask_quantity=800,
                    bid_price="100",
                    ask_price="100.50",
                    option_type=option_type,
                )
            )
            _, candidates[option_type] = engine.observe(
                _option_tick(
                    at=start + timedelta(seconds=1),
                    ltp="101",
                    bid_quantity=1600,
                    ask_quantity=500,
                    bid_price="100",
                    ask_price="100.50",
                    option_type=option_type,
                )
            )

        self.assertIsNotNone(candidates["CE"])
        self.assertIsNotNone(candidates["PE"])
        self.assertEqual(candidates["CE"].side, "BUY_CALL")
        self.assertEqual(candidates["PE"].side, "BUY_PUT")

    def test_strict_option_book_rejects_neutral_depth(self) -> None:
        relaxed = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
            )
        )
        strict = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=1,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.10"),
                max_spread=Decimal("1"),
                require_directional_option_book=True,
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        relaxed_candidate = strict_candidate = None
        for offset, price in enumerate(("100", "101")):
            tick = _option_tick(
                at=start + timedelta(seconds=offset),
                ltp=price,
                bid_quantity=100,
                ask_quantity=100,
                bid_price="100",
                ask_price="100.50",
            )
            _, relaxed_candidate = relaxed.observe(tick)
            _, strict_candidate = strict.observe(tick)

        self.assertIsNotNone(relaxed_candidate)
        self.assertIsNone(strict_candidate)

    def test_emits_candidate_after_persistent_positive_book_and_premium_velocity(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=3,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.50"),
                max_spread=Decimal("1"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        candidate = None
        for offset, price in enumerate(("100", "101", "102", "103")):
            _, candidate = engine.observe(
                _option_tick(
                    at=start + timedelta(seconds=offset),
                    ltp=price,
                    bid_quantity=1000,
                    ask_quantity=100,
                )
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.side, "BUY_CALL")
        self.assertIn("normalized OFI", candidate.reason)

    def test_rejects_one_sided_book_without_positive_premium_velocity(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=2,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.50"),
                max_spread=Decimal("1"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        engine.observe(_option_tick(at=start, ltp="100", bid_quantity=1000, ask_quantity=100))
        features, candidate = engine.observe(
            _option_tick(at=start + timedelta(seconds=1), ltp="99", bid_quantity=1000, ask_quantity=100)
        )

        self.assertIsNotNone(features)
        self.assertIsNone(candidate)

    def test_emits_bearish_futures_confirmation_from_ask_pressure(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=3,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("0.50"),
                max_spread=Decimal("1"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        candidate = None
        for offset, price in enumerate(("25000", "24999", "24998", "24997")):
            _, candidate = engine.observe(
                _future_tick(
                    at=start + timedelta(seconds=offset),
                    ltp=price,
                    bid_quantity=100,
                    ask_quantity=1000,
                )
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.side, "BUY_PUT")
        self.assertEqual(candidate.token.kind, InstrumentKind.FUTURE)

    def test_option_velocity_is_normalized_by_premium(self) -> None:
        engine = MicrostructureEngine(
            MicrostructureSettings(
                window_seconds=3,
                min_events=3,
                min_imbalance=Decimal("0.25"),
                min_velocity=Decimal("10"),
                max_spread=Decimal("1"),
                min_option_velocity_percent=Decimal("0.10"),
            )
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        candidate = None
        for offset, price in enumerate(("100", "100.2", "100.4", "100.6")):
            _, candidate = engine.observe(
                _option_tick(
                    at=start + timedelta(seconds=offset),
                    ltp=price,
                    bid_quantity=1000,
                    ask_quantity=100,
                )
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertIn("%/s", candidate.reason)
