from __future__ import annotations

import unittest
from decimal import Decimal

from app.analytics.strategy_resolver import (
    StrategyCandidateResolver,
    StrategyFamilySettings,
    StrategyResolverSettings,
)
from app.domain.models import (
    MarketRegime,
    SignalSetup,
    StrategyCandidate,
    StrategyFamily,
    StrategyResolverPolicy,
)


def _candidate(
    family: StrategyFamily,
    side: str,
    confidence: str = "0.70",
) -> StrategyCandidate:
    setups = {
        StrategyFamily.LEVEL_REVERSAL: SignalSetup.LEVEL_REVERSAL,
        StrategyFamily.BREAKOUT_MOMENTUM: SignalSetup.BREAKOUT,
        StrategyFamily.GAMMA_EXPANSION: SignalSetup.MOMENTUM_EXPANSION,
    }
    return StrategyCandidate(
        family=family,
        side=side,
        setup_type=setups[family],
        reason=f"{family.value} candidate",
        confidence=Decimal(confidence),
    )


def _settings(
    policy: StrategyResolverPolicy,
    *,
    level_enabled: bool = True,
    level_priority: int = 10,
    breakout_priority: int = 20,
    gamma_priority: int = 30,
) -> StrategyResolverSettings:
    return StrategyResolverSettings(
        policy=policy,
        families=(
            StrategyFamilySettings(
                StrategyFamily.LEVEL_REVERSAL,
                level_enabled,
                level_priority,
            ),
            StrategyFamilySettings(
                StrategyFamily.BREAKOUT_MOMENTUM,
                True,
                breakout_priority,
            ),
            StrategyFamilySettings(
                StrategyFamily.GAMMA_EXPANSION,
                True,
                gamma_priority,
            ),
        ),
    )


class StrategyCandidateResolverTests(unittest.TestCase):
    def test_regime_exclusive_selects_only_compatible_family(self) -> None:
        resolver = StrategyCandidateResolver(
            _settings(StrategyResolverPolicy.REGIME_EXCLUSIVE)
        )

        resolution = resolver.resolve(
            candidates=(
                _candidate(StrategyFamily.LEVEL_REVERSAL, "BUY_CALL"),
                _candidate(StrategyFamily.BREAKOUT_MOMENTUM, "BUY_CALL"),
            ),
            regime=MarketRegime.TREND_BREAKOUT,
        )

        self.assertEqual(
            resolution.selected.family,
            StrategyFamily.BREAKOUT_MOMENTUM,
        )

    def test_disabled_family_cannot_be_selected(self) -> None:
        resolver = StrategyCandidateResolver(
            _settings(
                StrategyResolverPolicy.REGIME_EXCLUSIVE,
                level_enabled=False,
            )
        )

        resolution = resolver.resolve(
            candidates=(
                _candidate(StrategyFamily.LEVEL_REVERSAL, "BUY_CALL"),
            ),
            regime=MarketRegime.RANGE,
        )

        self.assertIsNone(resolution.selected)
        self.assertIn("LEVEL_REVERSAL:disabled", resolution.rejected)

    def test_fixed_priority_uses_lower_number_first(self) -> None:
        resolver = StrategyCandidateResolver(
            _settings(
                StrategyResolverPolicy.FIXED_PRIORITY,
                level_priority=20,
                breakout_priority=10,
            )
        )

        resolution = resolver.resolve(
            candidates=(
                _candidate(StrategyFamily.LEVEL_REVERSAL, "BUY_CALL"),
                _candidate(StrategyFamily.BREAKOUT_MOMENTUM, "BUY_PUT"),
            ),
            regime=MarketRegime.RANGE,
        )

        self.assertEqual(
            resolution.selected.family,
            StrategyFamily.BREAKOUT_MOMENTUM,
        )

    def test_highest_confidence_breaks_same_family_candidate_tie(self) -> None:
        resolver = StrategyCandidateResolver(
            _settings(StrategyResolverPolicy.HIGHEST_CONFIDENCE)
        )

        resolution = resolver.resolve(
            candidates=(
                _candidate(
                    StrategyFamily.LEVEL_REVERSAL,
                    "BUY_CALL",
                    "0.65",
                ),
                _candidate(
                    StrategyFamily.LEVEL_REVERSAL,
                    "BUY_PUT",
                    "0.85",
                ),
            ),
            regime=MarketRegime.RANGE,
        )

        self.assertEqual(resolution.selected.side, "BUY_PUT")

    def test_conflict_policy_returns_no_trade_for_opposing_candidates(self) -> None:
        resolver = StrategyCandidateResolver(
            _settings(StrategyResolverPolicy.CONFLICT_NO_TRADE)
        )

        resolution = resolver.resolve(
            candidates=(
                _candidate(StrategyFamily.LEVEL_REVERSAL, "BUY_CALL"),
                _candidate(StrategyFamily.LEVEL_REVERSAL, "BUY_PUT"),
            ),
            regime=MarketRegime.RANGE,
        )

        self.assertIsNone(resolution.selected)
        self.assertIn("STRATEGY CONFLICT", resolution.reason)

    def test_duplicate_enabled_priorities_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique priorities"):
            _settings(
                StrategyResolverPolicy.FIXED_PRIORITY,
                level_priority=10,
                breakout_priority=10,
            )


if __name__ == "__main__":
    unittest.main()
