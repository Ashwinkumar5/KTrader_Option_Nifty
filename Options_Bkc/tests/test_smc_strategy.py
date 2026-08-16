from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.analytics.strategies.base import StrategyEvaluationContext
from app.analytics.strategies.smc import SMCStrategy
from app.core.strategy_config import (
    OptionChainImpulseSettings,
    SMCSettings,
    load_strategy_configuration,
)
from app.domain.models import SignalSetup, StrategyFamily


IST = ZoneInfo("Asia/Kolkata")


def _context(at: datetime, future_price: str) -> StrategyEvaluationContext:
    return StrategyEvaluationContext(
        underlying="NIFTY",
        captured_at=at,
        spot=Decimal(future_price) - Decimal("5"),
        pcr_oi=None,
        expected_upper=None,
        expected_lower=None,
        support=None,
        resistance=None,
        local_support=None,
        local_resistance=None,
        level_tolerance=Decimal("25"),
        breakout_threshold=Decimal("1"),
        exhaustion_threshold=Decimal("2"),
        atm_call_volume=1000,
        atm_call_oi=10000,
        atm_put_volume=1000,
        atm_put_oi=10000,
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
        rotation_reason="disabled",
        gamma_signal=None,
        gamma_reason="disabled",
        opening_context=None,
        candle_pattern=None,
        futures_flow=None,
        future_price=Decimal(future_price),
    )


def _settings() -> SMCSettings:
    return SMCSettings(
        opening_range_minutes=1,
        swing_left_frames=1,
        swing_right_frames=1,
        structure_lookback_frames=3,
        displacement_lookback_frames=12,
        maximum_active_levels_per_side=4,
        maximum_level_age_minutes=60,
        minimum_sweep_points=Decimal("1"),
        reclaim_buffer_points=Decimal("0.5"),
        structure_break_buffer_points=Decimal("0.5"),
        minimum_displacement_points=Decimal("1"),
        displacement_multiplier=Decimal("1"),
        maximum_reclaim_seconds=30,
        maximum_structure_break_seconds=60,
        option_confirmation_ttl_seconds=30,
        event_cooldown_seconds=60,
        require_cross_strike_confirmation=False,
    )


class SMCStrategyTests(unittest.TestCase):
    def test_profile_is_isolated_and_requires_both_books(self) -> None:
        profile = load_strategy_configuration(
            profile_name="Liquidity_Sweep_Reclaim"
        ).profile

        self.assertTrue(profile.strategy_enabled("SMC"))
        self.assertFalse(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("OPTION_CHAIN_IMPULSE"))
        self.assertTrue(profile.smc.require_cross_strike_confirmation)
        self.assertTrue(profile.microstructure.require_futures_confirmation)
        self.assertTrue(
            profile.microstructure.require_target_option_confirmation
        )
        self.assertTrue(
            profile.microstructure.require_directional_option_book
        )

    def test_sell_side_sweep_reclaim_emits_call_after_structure_shift(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
        frames = (
            (0, "100"),
            (30, "102"),
            (55, "99"),
            (60, "98"),
            (65, "99.5"),
        )
        for seconds, price in frames:
            self.assertEqual(
                strategy.evaluate(_context(at + timedelta(seconds=seconds), price)),
                (),
            )

        candidates = strategy.evaluate(
            _context(at + timedelta(seconds=70), "102.5")
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.family, StrategyFamily.SMC)
        self.assertEqual(candidate.side, "BUY_CALL")
        self.assertEqual(
            candidate.setup_type,
            SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
        )
        self.assertEqual(candidate.activation_level, Decimal("99"))
        self.assertIn("OPENING_RANGE_LOW", candidate.reason)

    def test_buy_side_sweep_reclaim_emits_put_after_structure_shift(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
        frames = (
            (0, "100"),
            (30, "102"),
            (55, "99"),
            (60, "103"),
            (65, "101.5"),
        )
        for seconds, price in frames:
            self.assertEqual(
                strategy.evaluate(_context(at + timedelta(seconds=seconds), price)),
                (),
            )

        candidates = strategy.evaluate(
            _context(at + timedelta(seconds=70), "98.5")
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].side, "BUY_PUT")
        self.assertEqual(candidates[0].activation_level, Decimal("102"))
        self.assertIn("LIQUIDITY SWEEP RECLAIM BUY_PUT", candidates[0].reason)

    def test_sweep_without_fast_reclaim_expires_without_candidate(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
        for seconds, price in (
            (0, "100"),
            (30, "102"),
            (55, "99"),
            (60, "98"),
            (95, "98.5"),
            (100, "103"),
        ):
            self.assertEqual(
                strategy.evaluate(_context(at + timedelta(seconds=seconds), price)),
                (),
            )

    def test_confirmed_setup_remains_available_during_confirmation_ttl(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
        for seconds, price in (
            (0, "100"),
            (30, "102"),
            (55, "99"),
            (60, "98"),
            (65, "99.5"),
        ):
            self.assertEqual(
                strategy.evaluate(_context(at + timedelta(seconds=seconds), price)),
                (),
            )

        first = strategy.evaluate(_context(at + timedelta(seconds=70), "102.5"))
        retry = strategy.evaluate(_context(at + timedelta(seconds=75), "102.7"))
        expired = strategy.evaluate(_context(at + timedelta(seconds=105), "102.8"))

        self.assertEqual(len(first), 1)
        self.assertEqual(len(retry), 1)
        self.assertEqual(expired, ())

    def test_opening_range_uses_india_time_for_utc_replay_timestamps(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at_utc = datetime(2026, 8, 10, 3, 45, tzinfo=ZoneInfo("UTC"))

        strategy.evaluate(_context(at_utc, "100"))
        strategy.evaluate(_context(at_utc + timedelta(seconds=30), "102"))
        strategy.evaluate(_context(at_utc + timedelta(seconds=60), "101"))

        diagnostic = strategy.last_diagnostic
        self.assertTrue(diagnostic.checks[0].passed)
        self.assertNotIn("highs=0", diagnostic.checks[0].observed)
        self.assertIn("lows=1", diagnostic.checks[0].observed)

    def test_confirmed_setup_is_cancelled_if_structure_break_fails(self) -> None:
        strategy = SMCStrategy(
            _settings(),
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 10, 9, 15, tzinfo=IST)
        for seconds, price in (
            (0, "100"),
            (30, "102"),
            (55, "99"),
            (60, "98"),
            (65, "99.5"),
        ):
            self.assertEqual(
                strategy.evaluate(_context(at + timedelta(seconds=seconds), price)),
                (),
            )

        confirmed = strategy.evaluate(
            _context(at + timedelta(seconds=70), "102.5")
        )
        invalidated = strategy.evaluate(
            _context(at + timedelta(seconds=75), "101.5")
        )

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(invalidated, ())


if __name__ == "__main__":
    unittest.main()
