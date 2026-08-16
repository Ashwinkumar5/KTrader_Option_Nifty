from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from app.analytics.engine import AnalyticsEngine, _strategy_diagnostics
from app.analytics.strategies.base import StrategyEvaluationContext
from app.analytics.strategy_resolver import StrategyResolution
from app.domain.models import (
    Exchange,
    InstrumentToken,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    SignalSetup,
    StrategyCandidate,
    StrategyFamily,
)


def _quote(
    strike: Decimal,
    option_type: OptionType,
    *,
    oi: int,
    oi_change: int,
    ltp: Decimal | None = None,
    volume: int | None = None,
) -> OptionQuote:
    return OptionQuote(
        contract=OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=strike,
            option_type=option_type,
            token=InstrumentToken(
                exchange=Exchange.NFO,
                token=f"{strike}-{option_type.value}",
                symbol="NIFTY",
                trading_symbol=f"NIFTY30JUL26{strike}{option_type.value}",
            ),
        ),
        oi=oi,
        oi_change=oi_change,
        ltp=ltp,
        volume=volume,
    )


class AnalyticsEngineTests(unittest.TestCase):
    def test_calculates_pcr_and_support_resistance_from_chain_window(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24149"),
            atm_strike=Decimal("24150"),
            captured_at=datetime(2026, 7, 5, 9, 20, tzinfo=UTC),
            quotes=(
                _quote(Decimal("24050"), OptionType.PUT, oi=90, oi_change=10),
                _quote(Decimal("24100"), OptionType.PUT, oi=180, oi_change=20),
                _quote(Decimal("24150"), OptionType.PUT, oi=150, oi_change=30, ltp=Decimal("100")),
                _quote(Decimal("24200"), OptionType.PUT, oi=500, oi_change=50),
                _quote(Decimal("24150"), OptionType.CALL, oi=120, oi_change=15, ltp=Decimal("110")),
                _quote(Decimal("24200"), OptionType.CALL, oi=300, oi_change=25),
                _quote(Decimal("24250"), OptionType.CALL, oi=250, oi_change=35),
                _quote(Decimal("24050"), OptionType.CALL, oi=600, oi_change=40),
            ),
        )

        analytics = AnalyticsEngine().from_chain(snapshot)

        self.assertEqual(analytics.put_call_ratio_oi, Decimal("0.7244"))
        self.assertEqual(analytics.put_call_ratio_oi_change, Decimal("0.9565"))
        self.assertEqual(analytics.atm_straddle_price, Decimal("210"))
        self.assertEqual(analytics.directional_bias, "bearish")
        self.assertEqual(
            [level.strike for level in analytics.support_levels],
            [Decimal("24100"), Decimal("24150"), Decimal("24050")],
        )
        self.assertEqual(
            [level.strike for level in analytics.resistance_levels],
            [Decimal("24200"), Decimal("24250"), Decimal("24150")],
        )

    def test_uses_atm_and_itm_subset_for_pcr(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24150"),
            atm_strike=Decimal("24150"),
            captured_at=datetime(2026, 7, 5, 9, 20, tzinfo=UTC),
            quotes=(
                _quote(Decimal("23950"), OptionType.CALL, oi=10, oi_change=1),
                _quote(Decimal("24000"), OptionType.CALL, oi=20, oi_change=2),
                _quote(Decimal("24050"), OptionType.CALL, oi=30, oi_change=3),
                _quote(Decimal("24100"), OptionType.CALL, oi=40, oi_change=4),
                _quote(Decimal("24150"), OptionType.CALL, oi=50, oi_change=5),
                _quote(Decimal("24150"), OptionType.PUT, oi=60, oi_change=6),
                _quote(Decimal("24200"), OptionType.PUT, oi=70, oi_change=7),
                _quote(Decimal("24250"), OptionType.PUT, oi=80, oi_change=8),
                _quote(Decimal("24300"), OptionType.PUT, oi=90, oi_change=9),
                _quote(Decimal("24350"), OptionType.PUT, oi=100, oi_change=10),
            ),
        )

        analytics = AnalyticsEngine().from_chain(snapshot)

        self.assertEqual(analytics.put_call_ratio_oi, Decimal("2.6667"))
        self.assertEqual(analytics.put_call_ratio_oi_change, Decimal("2.6667"))
        self.assertEqual(analytics.directional_bias, "overbought")

    def test_excludes_strikes_beyond_the_four_itm_window_for_pcr(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24150"),
            atm_strike=Decimal("24150"),
            captured_at=datetime(2026, 7, 5, 9, 20, tzinfo=UTC),
            quotes=(
                _quote(Decimal("23950"), OptionType.CALL, oi=10, oi_change=1),
                _quote(Decimal("24000"), OptionType.CALL, oi=20, oi_change=2),
                _quote(Decimal("24050"), OptionType.CALL, oi=30, oi_change=3),
                _quote(Decimal("24100"), OptionType.CALL, oi=40, oi_change=4),
                _quote(Decimal("24150"), OptionType.CALL, oi=50, oi_change=5),
                _quote(Decimal("23900"), OptionType.CALL, oi=1000, oi_change=100),
                _quote(Decimal("24150"), OptionType.PUT, oi=60, oi_change=6),
                _quote(Decimal("24200"), OptionType.PUT, oi=70, oi_change=7),
                _quote(Decimal("24250"), OptionType.PUT, oi=80, oi_change=8),
                _quote(Decimal("24300"), OptionType.PUT, oi=90, oi_change=9),
                _quote(Decimal("24350"), OptionType.PUT, oi=100, oi_change=10),
                _quote(Decimal("24400"), OptionType.PUT, oi=1000, oi_change=100),
            ),
        )

        analytics = AnalyticsEngine().from_chain(snapshot)

        self.assertEqual(analytics.put_call_ratio_oi, Decimal("2.6667"))
        self.assertEqual(analytics.put_call_ratio_oi_change, Decimal("2.6667"))

    def test_disabled_breakout_is_recorded_but_not_selected(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24210"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 6, 9, 20, tzinfo=UTC),
            quotes=(
                _quote(
                    Decimal("24150"),
                    OptionType.PUT,
                    oi=5000,
                    oi_change=100,
                    volume=1000,
                ),
                _quote(
                    Decimal("24200"),
                    OptionType.PUT,
                    oi=1000,
                    oi_change=50,
                    ltp=Decimal("90"),
                    volume=1000,
                ),
                _quote(
                    Decimal("24200"),
                    OptionType.CALL,
                    oi=1000,
                    oi_change=50,
                    ltp=Decimal("110"),
                    volume=10000,
                ),
            ),
        )

        analytics = AnalyticsEngine(
            strategy_breakout_momentum_enabled=False,
        ).from_chain(snapshot)

        self.assertEqual(len(analytics.strategy_candidates), 1)
        self.assertEqual(
            analytics.strategy_candidates[0].family.value,
            "BREAKOUT_MOMENTUM",
        )
        self.assertIsNone(analytics.selected_strategy)
        self.assertEqual(analytics.signal, "NEUTRAL")

    def test_selected_gamma_diagnostic_keeps_proposed_side(self) -> None:
        at = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)
        candidate = StrategyCandidate(
            family=StrategyFamily.GAMMA_EXPANSION,
            side="BUY_PUT",
            setup_type=SignalSetup.MOMENTUM_EXPANSION,
            reason="GAMMA PUT EXPANSION",
            confidence=Decimal("0.75"),
        )
        context = StrategyEvaluationContext(
            captured_at=at,
            spot=Decimal("24000"),
            pcr_oi=None,
            expected_upper=None,
            expected_lower=None,
            support=None,
            resistance=None,
            local_support=None,
            local_resistance=None,
            level_tolerance=Decimal("10"),
            breakout_threshold=Decimal("1"),
            exhaustion_threshold=Decimal("1"),
            atm_call_volume=0,
            atm_call_oi=0,
            atm_put_volume=0,
            atm_put_oi=0,
            spot_delta=Decimal("0"),
            near_support=False,
            near_resistance=False,
            support_volume=0,
            support_oi=0,
            support_oi_change=0,
            resistance_volume=0,
            resistance_oi=0,
            resistance_oi_change=0,
            rotation_signal=None,
            rotation_reason="",
            gamma_signal="BUY_PUT",
            gamma_reason="GAMMA PUT EXPANSION",
            opening_context=None,
            candle_pattern=None,
            futures_flow=None,
        )
        resolution = StrategyResolution(
            selected=candidate,
            considered=(candidate,),
            rejected=(),
            reason="selected",
        )

        diagnostics = _strategy_diagnostics(
            context=context,
            candidates=(candidate,),
            resolution=resolution,
        )
        gamma = next(
            item
            for item in diagnostics
            if item.family == StrategyFamily.GAMMA_EXPANSION
        )

        self.assertEqual(gamma.status, "SELECTED")
        self.assertEqual(gamma.proposed_side, "BUY_PUT")


if __name__ == "__main__":
    unittest.main()
