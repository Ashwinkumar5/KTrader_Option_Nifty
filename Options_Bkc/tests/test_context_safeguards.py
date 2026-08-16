from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.analytics.candle_patterns import CandlePatternTracker
from app.analytics.futures_flow import FuturesFlowSettings, FuturesFlowTracker
from app.analytics.premium_response import PremiumResponseTracker
from app.domain.models import (
    AnalyticsSnapshot,
    CandlePattern,
    Exchange,
    FuturesFlowState,
    GreeksSnapshot,
    InstrumentToken,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    UnderlyingMarketSnapshot,
)
from app.signals.gate import SignalGate, SignalGateSettings


IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 7, 30)


def _quote(
    option_type: OptionType,
    *,
    price: str,
    delta: str,
    iv: str = "15",
    vega: str | None = None,
) -> OptionQuote:
    token = InstrumentToken(
        Exchange.NFO,
        option_type.value,
        "NIFTY",
        f"NIFTY30JUL26{option_type.value}",
    )
    contract = OptionContract(
        "NIFTY",
        EXPIRY,
        Decimal("24000"),
        option_type,
        token,
    )
    greeks = GreeksSnapshot(
        contract=contract,
        captured_at=datetime(2026, 7, 22, 10, 0, tzinfo=IST),
        implied_volatility=Decimal(iv),
        delta=Decimal(delta),
        vega=Decimal(vega) if vega is not None else None,
    )
    midpoint = Decimal(price)
    return OptionQuote(
        contract=contract,
        ltp=midpoint,
        bid=midpoint - Decimal("0.5"),
        ask=midpoint + Decimal("0.5"),
        greeks=greeks,
        volume=10_000,
        oi=20_000,
    )


def _snapshot(
    at: datetime,
    *,
    spot: str,
    quotes: tuple[OptionQuote, ...] = (),
    future_price: str | None = None,
    future_oi: int | None = None,
) -> OptionChainSnapshot:
    market = UnderlyingMarketSnapshot(
        underlying="NIFTY",
        captured_at=at,
        spot_observed_at=at,
        future_observed_at=at if future_price is not None else None,
        future_price=(
            Decimal(future_price) if future_price is not None else None
        ),
        future_oi=future_oi,
        basis=(
            Decimal(future_price) - Decimal(spot)
            if future_price is not None
            else None
        ),
    )
    return OptionChainSnapshot(
        underlying="NIFTY",
        expiry=EXPIRY,
        spot_price=Decimal(spot),
        atm_strike=Decimal("24000"),
        captured_at=at,
        quotes=quotes,
        market=market,
    )


class ContextSafeguardTests(unittest.TestCase):
    def test_weak_premium_transmission_is_symmetric_for_call_and_put(self) -> None:
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        cases = (
            (OptionType.CALL, "0.5", "24020"),
            (OptionType.PUT, "-0.5", "23980"),
        )
        for option_type, delta, moved_spot in cases:
            with self.subTest(option_type=option_type):
                tracker = PremiumResponseTracker()
                tracker.update(
                    _snapshot(
                        start,
                        spot="24000",
                        quotes=(
                            _quote(option_type, price="100", delta=delta),
                        ),
                    )
                )
                response = tracker.update(
                    _snapshot(
                        start + timedelta(seconds=15),
                        spot=moved_spot,
                        quotes=(
                            _quote(option_type, price="102", delta=delta),
                        ),
                    )
                )[0]
                self.assertEqual(
                    response.favorable_expected_change,
                    Decimal("10.0"),
                )
                self.assertEqual(
                    response.favorable_actual_change,
                    Decimal("2"),
                )
                self.assertEqual(
                    response.transmission_ratio,
                    Decimal("0.2"),
                )

    def test_gate_rejects_mature_exact_contract_under_response(self) -> None:
        at = datetime(2026, 7, 22, 10, 0, 15, tzinfo=IST)
        quote = _quote(OptionType.CALL, price="102", delta="0.5")
        snapshot = _snapshot(at, spot="24020", quotes=(quote,))
        tracker = PremiumResponseTracker()
        tracker.update(
            _snapshot(
                at - timedelta(seconds=15),
                spot="24000",
                quotes=(_quote(OptionType.CALL, price="100", delta="0.5"),),
            )
        )
        response = tracker.update(snapshot)[0]
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24000"),
            signal="BUY_CALL",
            target_strike=Decimal("24000"),
            target_option_type=OptionType.CALL,
            premium_responses=(response,),
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=0,
                max_level_distance=Decimal("20"),
                max_microstructure_age_seconds=3,
                require_target_contract=True,
                premium_transmission_enabled=True,
                premium_transmission_min_expected_return_percent=Decimal("3"),
                premium_transmission_min_ratio=Decimal("0.35"),
            )
        )

        _, decision = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertFalse(decision.qualified)
        self.assertIn("weak premium transmission", decision.reason)

    def test_directional_transmission_exposes_iv_crush_offset(self) -> None:
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        tracker = PremiumResponseTracker()
        tracker.update(
            _snapshot(
                start,
                spot="24000",
                quotes=(
                    _quote(
                        OptionType.CALL,
                        price="100",
                        delta="0.5",
                        iv="15",
                        vega="2",
                    ),
                ),
            )
        )
        response = tracker.update(
            _snapshot(
                start + timedelta(seconds=15),
                spot="24020",
                quotes=(
                    _quote(
                        OptionType.CALL,
                        price="102",
                        delta="0.5",
                        iv="11",
                        vega="2",
                    ),
                ),
            )
        )[0]

        self.assertEqual(response.favorable_expected_change, Decimal("2.0"))
        self.assertEqual(response.transmission_ratio, Decimal("1"))
        self.assertEqual(
            response.favorable_directional_expected_change,
            Decimal("10.0"),
        )
        self.assertEqual(
            response.directional_transmission_ratio,
            Decimal("0.2"),
        )

    def test_futures_flow_uses_price_and_oi_together(self) -> None:
        tracker = FuturesFlowTracker()
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        tracker.update(
            _snapshot(
                start,
                spot="24000",
                future_price="24010",
                future_oi=100_000,
            )
        )
        long_buildup = tracker.update(
            _snapshot(
                start + timedelta(seconds=60),
                spot="24010",
                future_price="24020",
                future_oi=100_100,
            )
        )

        self.assertEqual(long_buildup.state, FuturesFlowState.LONG_BUILDUP)
        self.assertEqual(long_buildup.side, "BUY_CALL")
        self.assertEqual(long_buildup.strength, Decimal("0.80"))

    def test_futures_positioning_classifies_all_four_regimes(self) -> None:
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        cases = (
            (45, 6_000, FuturesFlowState.LONG_BUILDUP, "BUY_CALL"),
            (-45, 6_000, FuturesFlowState.SHORT_BUILDUP, "BUY_PUT"),
            (45, -6_000, FuturesFlowState.SHORT_COVERING, "BUY_CALL"),
            (-45, -6_000, FuturesFlowState.LONG_UNWINDING, "BUY_PUT"),
        )
        strengths: dict[FuturesFlowState, Decimal] = {}
        for price_delta, oi_delta, state, side in cases:
            with self.subTest(state=state):
                tracker = FuturesFlowTracker()
                for seconds, fraction in ((0, 0), (15, 1), (60, 2), (180, 3)):
                    result = tracker.update(
                        _snapshot(
                            start + timedelta(seconds=seconds),
                            spot="24000",
                            future_price=str(
                                Decimal("24000")
                                + Decimal(price_delta * fraction) / Decimal("3")
                            ),
                            future_oi=1_000_000 + oi_delta * fraction // 3,
                        )
                    )
                positioning = result.positioning
                self.assertIsNotNone(positioning)
                assert positioning is not None
                self.assertTrue(positioning.ready)
                self.assertEqual(positioning.state, state)
                self.assertEqual(positioning.side, side)
                self.assertGreaterEqual(positioning.horizon_agreement, 2)
                strengths[state] = positioning.strength

        self.assertGreater(
            strengths[FuturesFlowState.LONG_BUILDUP],
            strengths[FuturesFlowState.SHORT_COVERING],
        )
        self.assertGreater(
            strengths[FuturesFlowState.SHORT_BUILDUP],
            strengths[FuturesFlowState.LONG_UNWINDING],
        )

    def test_futures_positioning_resets_and_keeps_bounded_history(self) -> None:
        settings = FuturesFlowSettings(
            positioning_horizons_seconds=(5, 10, 100),
            positioning_sample_seconds=1,
            max_positioning_observations=8,
        )
        tracker = FuturesFlowTracker(settings)
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        for seconds in range(20):
            tracker.update(
                _snapshot(
                    start + timedelta(seconds=seconds),
                    spot="24000",
                    future_price=str(Decimal("24000") + seconds),
                    future_oi=1_000_000 + seconds * 100,
                )
            )
        self.assertLessEqual(
            len(tracker._positioning_history["NIFTY"]),
            settings.max_positioning_observations,
        )

        reset_result = tracker.update(
            _snapshot(
                start - timedelta(seconds=1),
                spot="24000",
                future_price="24000",
                future_oi=1_000_000,
            )
        )
        self.assertIsNotNone(reset_result.positioning)
        assert reset_result.positioning is not None
        self.assertFalse(reset_result.positioning.ready)
        self.assertEqual(len(tracker._positioning_history["NIFTY"]), 1)

    def test_closed_four_minute_dragonfly_requires_follow_through(self) -> None:
        tracker = CandlePatternTracker()
        start = datetime(2026, 7, 22, 9, 15, tzinfo=IST)
        tracker.update(_snapshot(start, spot="100"))
        tracker.update(_snapshot(start + timedelta(minutes=1), spot="90"))
        tracker.update(
            _snapshot(start + timedelta(minutes=3, seconds=45), spot="99")
        )
        result = tracker.update(
            _snapshot(start + timedelta(minutes=4), spot="101")
        )

        self.assertEqual(result.pattern, CandlePattern.DRAGONFLY_DOJI)
        self.assertEqual(result.potential_side, "BUY_CALL")
        self.assertTrue(result.follow_through)

    def test_generic_doji_has_no_automatic_direction(self) -> None:
        tracker = CandlePatternTracker()
        start = datetime(2026, 7, 22, 9, 15, tzinfo=IST)
        tracker.update(_snapshot(start, spot="100"))
        tracker.update(_snapshot(start + timedelta(minutes=1), spot="110"))
        tracker.update(_snapshot(start + timedelta(minutes=2), spot="90"))
        tracker.update(
            _snapshot(start + timedelta(minutes=3, seconds=45), spot="100")
        )
        result = tracker.update(
            _snapshot(start + timedelta(minutes=4), spot="101")
        )

        self.assertEqual(result.pattern, CandlePattern.DOJI)
        self.assertIsNone(result.potential_side)
        self.assertFalse(result.follow_through)


if __name__ == "__main__":
    unittest.main()
