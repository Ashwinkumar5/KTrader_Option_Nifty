from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.analytics.expected_move import ExpectedMoveTracker
from app.analytics.momentum_exhaustion import MomentumExhaustionTracker
from app.analytics.opening_context import OpeningContextTracker
from app.analytics.premium_response import PremiumResponseTracker
from app.analytics.session_features import (
    FeatureModuleSettings,
    SessionFeaturePipelineSettings,
)
from app.domain.models import (
    Exchange,
    ExhaustionState,
    ExpectedMoveContext,
    GreeksSnapshot,
    InstrumentToken,
    OpeningState,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    PremiumResponse,
    UnderlyingMarketSnapshot,
)


IST = ZoneInfo("Asia/Kolkata")
EXPIRY = date(2026, 7, 30)


def _quote(
    option_type: OptionType,
    *,
    strike: str = "24100",
    bid: str = "99",
    ask: str = "101",
    delta: str | None = None,
) -> OptionQuote:
    token = InstrumentToken(
        Exchange.NFO,
        f"{strike}-{option_type.value}",
        "NIFTY",
        f"NIFTY30JUL26{strike}{option_type.value}",
    )
    contract = OptionContract(
        "NIFTY",
        EXPIRY,
        Decimal(strike),
        option_type,
        token,
    )
    greeks = (
        GreeksSnapshot(
            contract=contract,
            captured_at=datetime(2026, 7, 22, 9, 45, tzinfo=IST),
            implied_volatility=Decimal("15"),
            delta=Decimal(delta),
        )
        if delta is not None
        else None
    )
    return OptionQuote(
        contract=contract,
        ltp=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        bid=Decimal(bid),
        ask=Decimal(ask),
        greeks=greeks,
    )


def _snapshot(
    at: datetime,
    *,
    spot: str = "24100",
    atm: str = "24100",
    quotes: tuple[OptionQuote, ...] = (),
    open_price: str = "24100",
    previous_close: str = "24000",
) -> OptionChainSnapshot:
    market = UnderlyingMarketSnapshot(
        underlying="NIFTY",
        captured_at=at,
        spot_observed_at=at,
        open_price=Decimal(open_price),
        previous_close=Decimal(previous_close),
        previous_session_expected_move=Decimal("100"),
    )
    return OptionChainSnapshot(
        underlying="NIFTY",
        expiry=EXPIRY,
        spot_price=Decimal(spot),
        atm_strike=Decimal(atm),
        captured_at=at,
        quotes=quotes,
        market=market,
    )


class SessionFeatureTests(unittest.TestCase):
    def test_opening_context_detects_normalized_gap_fade(self) -> None:
        tracker = OpeningContextTracker()
        observing = tracker.update(
            _snapshot(datetime(2026, 7, 22, 9, 20, tzinfo=IST))
        )
        faded = tracker.update(
            _snapshot(
                datetime(2026, 7, 22, 9, 30, tzinfo=IST),
                spot="24040",
            )
        )

        self.assertEqual(observing.state, OpeningState.OBSERVING_OPEN)
        self.assertEqual(faded.state, OpeningState.GAP_FADE_CANDIDATE_DOWN)
        self.assertEqual(faded.normalized_gap, Decimal("1"))
        self.assertGreaterEqual(faded.gap_fill_ratio or Decimal("0"), Decimal("0.5"))

    def test_expected_move_requires_synchronized_mids_and_fixes_strike(self) -> None:
        tracker = ExpectedMoveTracker()
        before = tracker.update(
            _snapshot(datetime(2026, 7, 22, 9, 44, tzinfo=IST))
        )
        captured = tracker.update(
            _snapshot(
                datetime(2026, 7, 22, 9, 45, tzinfo=IST),
                quotes=(
                    _quote(OptionType.CALL, bid="100", ask="102"),
                    _quote(OptionType.PUT, bid="90", ask="92"),
                ),
            )
        )
        moved = tracker.update(
            _snapshot(
                datetime(2026, 7, 22, 9, 46, tzinfo=IST),
                spot="24200",
                atm="24200",
            )
        )

        self.assertFalse(before.available)
        self.assertTrue(captured.available)
        self.assertEqual(captured.fixed_strike, Decimal("24100"))
        self.assertEqual(captured.straddle_mid, Decimal("192"))
        self.assertEqual(moved.fixed_strike, Decimal("24100"))
        self.assertEqual(moved.utilization, Decimal("100") / Decimal("192"))

    def test_premium_response_is_incremental_and_resets_on_time_regression(self) -> None:
        tracker = PremiumResponseTracker()
        start = datetime(2026, 7, 22, 10, 0, tzinfo=IST)
        first_quote = _quote(
            OptionType.CALL,
            bid="99",
            ask="101",
            delta="0.5",
        )
        second_quote = _quote(
            OptionType.CALL,
            bid="109",
            ask="111",
            delta="0.5",
        )
        self.assertEqual(
            tracker.update(_snapshot(start, quotes=(first_quote,))),
            (),
        )
        responses = tracker.update(
            _snapshot(
                start + timedelta(seconds=15),
                spot="24110",
                quotes=(second_quote,),
            )
        )
        regressed = tracker.update(
            _snapshot(
                start - timedelta(seconds=15),
                quotes=(first_quote,),
            )
        )

        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].premium_change, Decimal("10"))
        self.assertEqual(responses[0].expected_change, Decimal("5.0"))
        self.assertEqual(regressed, ())

    def test_exhaustion_is_management_only(self) -> None:
        at = datetime(2026, 7, 22, 13, 30, tzinfo=IST)
        quote = _quote(OptionType.CALL, bid="199", ask="201")
        response = PremiumResponse(
            token=quote.contract.token.token,
            option_type=OptionType.CALL,
            captured_at=at,
            premium_change=Decimal("-2"),
            return_percent=Decimal("100"),
            expected_change=Decimal("0"),
            residual_change=Decimal("-2"),
            spot_change=Decimal("0"),
            iv_change=Decimal("0"),
            spread=Decimal("2"),
        )
        result = MomentumExhaustionTracker().update(
            snapshot=_snapshot(at, spot="24200", quotes=(quote,)),
            expected_move=ExpectedMoveContext(
                available=True,
                utilization=Decimal("1"),
            ),
            responses=(response,),
        )

        self.assertEqual(result.state, ExhaustionState.DIRECTIONAL_EXHAUSTION)
        self.assertEqual(result.winning_side, "BUY_CALL")
        self.assertEqual(result.opposite_side, "BUY_PUT")
        self.assertNotEqual(result.action.value, "NONE")

    def test_pipeline_rejects_unsafe_module_order(self) -> None:
        with self.assertRaises(ValueError):
            SessionFeaturePipelineSettings(
                opening=FeatureModuleSettings(True, 10),
                expected_move=FeatureModuleSettings(True, 20),
                premium_response=FeatureModuleSettings(True, 20),
                momentum_exhaustion=FeatureModuleSettings(True, 40),
            )
        with self.assertRaises(ValueError):
            SessionFeaturePipelineSettings(
                opening=FeatureModuleSettings(True, 10),
                expected_move=FeatureModuleSettings(False, 20),
                premium_response=FeatureModuleSettings(True, 30),
                momentum_exhaustion=FeatureModuleSettings(True, 40),
            )


if __name__ == "__main__":
    unittest.main()
