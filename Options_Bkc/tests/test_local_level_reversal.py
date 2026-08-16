from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from app.analytics.engine import _opening_regime_override
from app.analytics.strategies.base import StrategyEvaluationContext
from app.analytics.strategies.level_reversal import LevelReversalStrategy
from app.analytics.strategy_resolver import (
    StrategyCandidateResolver,
    StrategyFamilySettings,
    StrategyResolverSettings,
)
from app.domain.models import (
    AnalyticsSnapshot,
    CandlePattern,
    CandlePatternContext,
    EvidenceFamily,
    FuturesFlowContext,
    MarketRegime,
    OpeningContext,
    OpeningState,
    OptionChainSnapshot,
    OptionType,
    SignalSetup,
    StrategyEvidence,
    StrategyFamily,
    StrategyResolverPolicy,
    SupportResistanceLevel,
)
from app.signals.gate import SignalGate, SignalGateSettings


def _context(side: str) -> StrategyEvaluationContext:
    at = datetime(2026, 7, 27, 4, 23, 6, tzinfo=UTC)
    put = side == "BUY_PUT"
    return StrategyEvaluationContext(
        captured_at=at,
        spot=Decimal("23948.85") if put else Decimal("23951.15"),
        pcr_oi=Decimal("0.78") if put else Decimal("1.20"),
        expected_upper=Decimal("24028"),
        expected_lower=Decimal("23871"),
        support=Decimal("23900"),
        resistance=Decimal("24000"),
        local_support=Decimal("23950"),
        local_resistance=Decimal("23950"),
        level_tolerance=Decimal("25"),
        breakout_threshold=Decimal("4"),
        exhaustion_threshold=Decimal("7"),
        atm_call_volume=60,
        atm_call_oi=10,
        atm_put_volume=48,
        atm_put_oi=10,
        spot_delta=Decimal("0.85") if put else Decimal("-0.85"),
        near_support=False,
        near_resistance=False,
        support_volume=45,
        support_oi=10,
        support_oi_change=0,
        resistance_volume=36,
        resistance_oi=10,
        resistance_oi_change=0,
        rotation_signal=None,
        rotation_reason="waiting",
        gamma_signal=None,
        gamma_reason="no expansion",
        opening_context=OpeningContext(
            state=(
                OpeningState.OPENING_DRIVE_UP
                if put
                else OpeningState.OPENING_DRIVE_DOWN
            )
        ),
        candle_pattern=CandlePatternContext(
            pattern=(
                CandlePattern.SHOOTING_STAR
                if put
                else CandlePattern.HAMMER
            ),
            closed_at=datetime(2026, 7, 27, 4, 21, tzinfo=UTC),
            high_price=Decimal("23954.5"),
            low_price=Decimal("23945.5"),
            close_price=Decimal("23949.65")
            if put
            else Decimal("23950.35"),
            potential_side=side,
            follow_through=True,
        ),
        futures_flow=FuturesFlowContext(),
    )


class LocalLevelReversalTests(unittest.TestCase):
    def test_opening_failure_put_is_generated_at_secondary_resistance(self) -> None:
        candidates = LevelReversalStrategy().evaluate(_context("BUY_PUT"))

        candidate = next(
            item
            for item in candidates
            if item.setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
        )
        self.assertEqual(candidate.side, "BUY_PUT")
        self.assertEqual(candidate.activation_level, Decimal("23950"))
        self.assertIn("OPENING FAILURE REVERSAL", candidate.reason)

    def test_opening_failure_call_is_symmetric(self) -> None:
        candidates = LevelReversalStrategy().evaluate(_context("BUY_CALL"))

        candidate = next(
            item
            for item in candidates
            if item.setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
        )
        self.assertEqual(candidate.side, "BUY_CALL")
        self.assertEqual(candidate.activation_level, Decimal("23950"))

    def test_structural_range_is_not_overridden_by_opening_drive(self) -> None:
        self.assertEqual(
            _opening_regime_override(
                MarketRegime.RANGE,
                OpeningState.OPENING_DRIVE_UP,
            ),
            MarketRegime.RANGE,
        )

    def test_regime_exclusive_allows_confirmed_local_reversal(self) -> None:
        candidate = next(
            item
            for item in LevelReversalStrategy().evaluate(
                _context("BUY_PUT")
            )
            if item.setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
        )
        resolver = StrategyCandidateResolver(
            StrategyResolverSettings(
                policy=StrategyResolverPolicy.REGIME_EXCLUSIVE,
                families=(
                    StrategyFamilySettings(
                        StrategyFamily.LEVEL_REVERSAL,
                        True,
                        10,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.BREAKOUT_MOMENTUM,
                        True,
                        20,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.GAMMA_EXPANSION,
                        True,
                        30,
                    ),
                ),
            )
        )

        resolution = resolver.resolve(
            candidates=(candidate,),
            regime=MarketRegime.TREND_BREAKOUT,
        )

        self.assertEqual(resolution.selected, candidate)

    def test_gate_can_qualify_local_reversal_without_microstructure(self) -> None:
        at = datetime(2026, 7, 27, 4, 23, 21, tzinfo=UTC)
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=datetime(2026, 7, 28, tzinfo=UTC).date(),
            spot_price=Decimal("23946.25"),
            atm_strike=Decimal("23950"),
            captured_at=at,
            quotes=(),
        )
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("23950"),
            signal="BUY_PUT",
            signal_reason=(
                "OPENING FAILURE REVERSAL: closed SHOOTING_STAR rejected "
                "local resistance 23950"
            ),
            setup_type=SignalSetup.LOCAL_LEVEL_REVERSAL,
            activation_level=Decimal("23950"),
            market_regime=MarketRegime.RANGE,
            support_levels=(
                SupportResistanceLevel(
                    Decimal("23900"),
                    option_type=OptionType.PUT,
                    oi=10,
                ),
            ),
            resistance_levels=(
                SupportResistanceLevel(
                    Decimal("24000"),
                    option_type=OptionType.CALL,
                    oi=10,
                ),
            ),
            directional_confirmations=(
                "structure:LOCAL_LEVEL_REVERSAL",
                "local_oi_level_rejection",
                "closed_reversal_candle_follow_through",
            ),
            directional_evidence=(
                StrategyEvidence(
                    "closed_reversal_candle_follow_through",
                    EvidenceFamily.PRICE_ACTION,
                    "BUY_PUT",
                    Decimal("0.8"),
                ),
                StrategyEvidence(
                    "pcr_context",
                    EvidenceFamily.POSITIONING,
                    "BUY_PUT",
                    Decimal("0.6"),
                ),
            ),
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=2,
                cooldown_seconds=0,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=3,
                min_signal_score=Decimal("80"),
                min_directional_confirmations=2,
                min_independent_confirmation_families=2,
                require_regime_match=True,
            )
        )

        _, decision = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertTrue(decision.qualified, decision.reason)
        self.assertGreaterEqual(decision.confidence_score, Decimal("80"))


if __name__ == "__main__":
    unittest.main()
