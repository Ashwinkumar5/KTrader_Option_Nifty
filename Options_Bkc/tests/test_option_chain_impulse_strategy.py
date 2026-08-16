from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.strategies.base import (
    OptionChainLeg,
    StrategyEvaluationContext,
)
from app.analytics.strategies.option_chain_impulse import (
    OptionChainImpulseStrategy,
)
from app.core.strategy_config import OptionChainImpulseSettings
from app.domain.models import OptionType, PremiumResponse, StrategyFamily


def _legs(call_price: str, put_price: str, *, call_volume: int) -> tuple[OptionChainLeg, ...]:
    result = []
    for relative in (-1, 0, 1):
        result.extend(
            (
                OptionChainLeg(
                    token=f"call-{relative}",
                    option_type=OptionType.CALL,
                    relative_strike=relative,
                    mid=Decimal(call_price),
                    volume=call_volume,
                    oi=10000,
                    spread_ratio=Decimal("0.005"),
                ),
                OptionChainLeg(
                    token=f"put-{relative}",
                    option_type=OptionType.PUT,
                    relative_strike=relative,
                    mid=Decimal(put_price),
                    volume=1100,
                    oi=10000,
                    spread_ratio=Decimal("0.005"),
                ),
            )
        )
    return tuple(result)


def _responses(at: datetime, call_residual: str) -> tuple[PremiumResponse, ...]:
    result = []
    for relative in (-1, 0, 1):
        for option_type, residual in (
            (OptionType.CALL, call_residual),
            (OptionType.PUT, "-0.20"),
        ):
            result.append(
                PremiumResponse(
                    token=f"{'call' if option_type == OptionType.CALL else 'put'}-{relative}",
                    option_type=option_type,
                    captured_at=at,
                    premium_change=Decimal(residual),
                    return_percent=None,
                    expected_change=Decimal("0"),
                    residual_change=Decimal(residual),
                    spot_change=Decimal("0"),
                    iv_change=None,
                    spread=Decimal("0.5"),
                )
            )
    return tuple(result)


def _context(
    at: datetime,
    legs: tuple[OptionChainLeg, ...],
    responses: tuple[PremiumResponse, ...] = (),
) -> StrategyEvaluationContext:
    return StrategyEvaluationContext(
        underlying="NIFTY",
        captured_at=at,
        spot=Decimal("25000"),
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
        option_chain_legs=legs,
        premium_responses=responses,
    )


class OptionChainImpulseStrategyTests(unittest.TestCase):
    def test_call_impulse_requires_cross_strike_breadth_and_put_decay(self) -> None:
        strategy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

        self.assertEqual(
            strategy.evaluate(_context(at, _legs("100", "100", call_volume=1000))),
            (),
        )
        candidates = strategy.evaluate(
            _context(
                at + timedelta(seconds=30),
                _legs("101", "99.5", call_volume=1400),
                _responses(at + timedelta(seconds=30), "0.20"),
            )
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].family, StrategyFamily.OPTION_CHAIN_IMPULSE)
        self.assertEqual(candidates[0].side, "BUY_CALL")
        self.assertEqual(strategy.last_diagnostic.status, "CANDIDATE")
        self.assertEqual(
            strategy.evaluate(
                _context(
                    at + timedelta(seconds=35),
                    _legs("101.1", "99.4", call_volume=1500),
                    _responses(at + timedelta(seconds=35), "0.20"),
                )
            ),
            (),
        )

    def test_same_side_rise_without_opposite_decay_is_rejected(self) -> None:
        strategy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        strategy.evaluate(_context(at, _legs("100", "100", call_volume=1000)))

        candidates = strategy.evaluate(
            _context(
                at + timedelta(seconds=30),
                _legs("101", "100.2", call_volume=1400),
                _responses(at + timedelta(seconds=30), "0.20"),
            )
        )

        self.assertEqual(candidates, ())
        failed = {
            item.code
            for item in strategy.last_diagnostic.checks
            if not item.passed
        }
        self.assertIn("opposite_leg_decay", failed)

    def test_mechanical_option_repricing_without_residual_is_rejected(self) -> None:
        strategy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        strategy.evaluate(_context(at, _legs("100", "100", call_volume=1000)))

        candidates = strategy.evaluate(
            _context(
                at + timedelta(seconds=30),
                _legs("101", "99.5", call_volume=1400),
                _responses(at + timedelta(seconds=30), "0.02"),
            )
        )

        self.assertEqual(candidates, ())
        failed = {
            item.code
            for item in strategy.last_diagnostic.checks
            if not item.passed
        }
        self.assertIn("greek_adjusted_residual", failed)

    def test_confirmed_profile_aggregates_residual_over_impulse_window(self) -> None:
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        confirmed = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(
                aggregate_residual_over_window=True,
            ),
            enabled=True,
        )
        legacy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=True,
        )
        initial = _context(at, _legs("100", "100", call_volume=1000))
        confirmed.evaluate(initial)
        legacy.evaluate(initial)

        observations = (
            (10, "100.3", "99.9", 1100),
            (20, "100.6", "99.7", 1200),
            (30, "101", "99.5", 1400),
        )
        confirmed_result = ()
        legacy_result = ()
        for seconds, call, put, volume in observations:
            observed_at = at + timedelta(seconds=seconds)
            context = _context(
                observed_at,
                _legs(call, put, call_volume=volume),
                _responses(observed_at, "0.04"),
            )
            confirmed_result = confirmed.evaluate(context)
            legacy_result = legacy.evaluate(context)

        self.assertEqual(len(confirmed_result), 1)
        self.assertEqual(confirmed_result[0].side, "BUY_CALL")
        self.assertEqual(legacy_result, ())
        self.assertIn(
            "greek_adjusted_residual",
            {
                item.code
                for item in legacy.last_diagnostic.checks
                if not item.passed
            },
        )

    def test_overextended_cross_side_gap_is_rejected(self) -> None:
        strategy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=True,
        )
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)
        strategy.evaluate(_context(at, _legs("100", "100", call_volume=1000)))

        candidates = strategy.evaluate(
            _context(
                at + timedelta(seconds=30),
                _legs("102", "98", call_volume=1400),
                _responses(at + timedelta(seconds=30), "0.20"),
            )
        )

        self.assertEqual(candidates, ())
        failed = {
            item.code
            for item in strategy.last_diagnostic.checks
            if not item.passed
        }
        self.assertIn("impulse_not_overextended", failed)

    def test_disabled_strategy_does_not_collect_or_emit(self) -> None:
        strategy = OptionChainImpulseStrategy(
            OptionChainImpulseSettings(),
            enabled=False,
        )
        at = datetime(2026, 8, 7, 9, 0, tzinfo=UTC)

        self.assertEqual(
            strategy.evaluate(_context(at, _legs("100", "100", call_volume=1000))),
            (),
        )
        self.assertIn("disabled", strategy.last_diagnostic.reason)


if __name__ == "__main__":
    unittest.main()
