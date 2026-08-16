from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.strategies.base import StrategyEvaluationContext
from app.analytics.strategies.base import OptionChainLeg
from app.analytics.strategies.derivatives_quant import (
    DerivativesQuantStrategy,
    _expected_move_buyability_score,
    _vix_buyability_score,
)
from app.analytics.strategies.gamma_expansion import GammaExpansionStrategy
from app.core.strategy_config import DerivativesQuantSettings
from app.domain.models import (
    FuturesFlowContext,
    FuturesFlowHorizonContext,
    FuturesFlowState,
    FuturesPositioningContext,
    OptionType,
)


def _context(at: datetime) -> StrategyEvaluationContext:
    return StrategyEvaluationContext(
        underlying="NIFTY",
        captured_at=at,
        spot=Decimal("25000"),
        pcr_oi=Decimal("1.1"),
        expected_upper=None,
        expected_lower=None,
        support=None,
        resistance=None,
        local_support=None,
        local_resistance=None,
        level_tolerance=Decimal("25"),
        breakout_threshold=Decimal("1"),
        exhaustion_threshold=Decimal("2"),
        atm_call_volume=40000,
        atm_call_oi=10000,
        atm_put_volume=2000,
        atm_put_oi=10000,
        spot_delta=Decimal("2"),
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
        gamma_signal="BUY_CALL",
        gamma_reason="call-side gamma expansion",
        opening_context=None,
        candle_pattern=None,
        futures_flow=FuturesFlowContext(
            state=FuturesFlowState.LONG_BUILDUP,
            side="BUY_CALL",
            basis_change=Decimal("5"),
            strength=Decimal("1"),
            reason="future price and OI rising",
        ),
        active_pcr=Decimal("1.1"),
        call_oi_change=-100,
        put_oi_change=1000,
        call_volume_oi=Decimal("4"),
        put_volume_oi=Decimal("0.2"),
        atm_straddle_price=Decimal("220"),
        atm_call_mid=Decimal("100"),
        atm_put_mid=Decimal("100"),
        atm_call_iv=Decimal("20"),
        atm_put_iv=Decimal("10"),
        intraday_iv_rank=Decimal("20"),
        previous_20d_atr=Decimal("220"),
        india_vix=Decimal("14"),
    )


def _put_context(at: datetime) -> StrategyEvaluationContext:
    return replace(
        _context(at),
        gamma_signal="BUY_PUT",
        gamma_reason="put-side gamma expansion",
        futures_flow=FuturesFlowContext(
            state=FuturesFlowState.SHORT_BUILDUP,
            side="BUY_PUT",
            basis_change=Decimal("-5"),
            strength=Decimal("1"),
            reason="future price falling while OI rises",
        ),
        atm_call_volume=2000,
        atm_put_volume=40000,
        atm_call_iv=Decimal("10"),
        atm_put_iv=Decimal("20"),
    )


def _positioning_legs(
    side: str,
    *,
    moved: bool,
    move_factor: Decimal = Decimal("1"),
) -> tuple[OptionChainLeg, ...]:
    legs: list[OptionChainLeg] = []
    for option_type, prefix in (("CE", "C"), ("PE", "P")):
        for relative in (-1, 0, 1, 2):
            baseline_mid = Decimal("100")
            if not moved:
                mid = baseline_mid
            elif side == "BUY_CALL":
                mid = (
                    baseline_mid + Decimal("5") * move_factor
                    if option_type == "CE"
                    else baseline_mid - Decimal("5") * move_factor
                )
            else:
                mid = (
                    baseline_mid - Decimal("5") * move_factor
                    if option_type == "CE"
                    else baseline_mid + Decimal("5") * move_factor
                )
            legs.append(
                OptionChainLeg(
                    token=f"{prefix}{relative}",
                    option_type=(
                        OptionType.CALL
                        if option_type == "CE"
                        else OptionType.PUT
                    ),
                    relative_strike=relative,
                    mid=mid,
                    volume=10_000,
                    oi=(
                        10_000 + int(Decimal("100") * move_factor)
                        if moved
                        else 10_000
                    ),
                    spread_ratio=Decimal("0.01"),
                )
            )
    return tuple(legs)


class DerivativesQuantStrategyTests(unittest.TestCase):
    def test_cross_strike_option_positioning_is_symmetric(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            weights={"oi_migration": Decimal("1")},
            minimum_direction_score=Decimal("0.20"),
            warmup_direction_score=Decimal("0.20"),
            early_direction_score=Decimal("0.10"),
            minimum_independent_families=1,
            minimum_horizon_agreement=1,
            early_min_independent_families=1,
            early_min_option_chain_families=1,
            early_min_horizon_agreement=1,
            minimum_buyability_score=Decimal("0"),
            early_min_buyability_score=Decimal("0"),
            require_expansion_trigger=False,
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        for side in ("BUY_CALL", "BUY_PUT"):
            with self.subTest(side=side):
                strategy = DerivativesQuantStrategy(
                    settings,
                    frozenset({"volume_oi"}),
                )
                base = _context(at) if side == "BUY_CALL" else _put_context(at)
                strategy.evaluate(
                    replace(
                        base,
                        option_chain_legs=_positioning_legs(side, moved=False),
                    )
                )
                candidates = strategy.evaluate(
                    replace(
                        base,
                        captured_at=at + timedelta(seconds=60),
                        option_chain_legs=_positioning_legs(side, moved=True),
                    )
                )

                self.assertEqual(
                    len(candidates),
                    1,
                    strategy.last_diagnostic,
                )
                self.assertEqual(candidates[0].side, side)
                self.assertIn("cross_strike_option_positioning", {
                    item.code for item in candidates[0].evidence
                })

    def test_failed_auction_lockout_is_symmetric_and_expires(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            weights={"futures_flow": Decimal("1")},
            minimum_direction_score=Decimal("0.34"),
            warmup_direction_score=Decimal("0.34"),
            early_direction_score=Decimal("0.22"),
            minimum_independent_families=1,
            minimum_horizon_agreement=1,
            early_min_independent_families=1,
            early_min_option_chain_families=0,
            early_min_horizon_agreement=1,
            minimum_buyability_score=Decimal("0"),
            early_min_buyability_score=Decimal("0"),
            early_score_persistence_frames=1,
            require_early_acceleration=False,
            require_expansion_trigger=False,
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        cases = (
            ("BUY_CALL", FuturesFlowState.SHORT_BUILDUP, "BUY_PUT"),
            ("BUY_PUT", FuturesFlowState.LONG_BUILDUP, "BUY_CALL"),
        )
        for side, opposing_state, opposing_side in cases:
            with self.subTest(side=side):
                strategy = DerivativesQuantStrategy(
                    settings,
                    frozenset({"futures_flow"}),
                )
                context = _context(at) if side == "BUY_CALL" else _put_context(at)
                strategy.evaluate(
                    replace(
                        context,
                        futures_flow=FuturesFlowContext(
                            state=opposing_state,
                            side=opposing_side,
                            strength=Decimal("1"),
                            reason="opposite impulse",
                        ),
                    )
                )
                blocked = strategy.evaluate(
                    replace(
                        context,
                        captured_at=at + timedelta(seconds=15),
                        futures_flow=FuturesFlowContext(
                            state=(
                                FuturesFlowState.LONG_BUILDUP
                                if side == "BUY_CALL"
                                else FuturesFlowState.SHORT_BUILDUP
                            ),
                            side=side,
                            strength=Decimal("0.50"),
                            reason="early reversal impulse",
                        ),
                    )
                )
                auction_check = next(
                    item
                    for item in strategy.last_diagnostic.checks
                    if item.code == "failed_auction_stability"
                )
                self.assertEqual(blocked, ())
                self.assertFalse(auction_check.passed)

                released = strategy.evaluate(
                    replace(
                        context,
                        captured_at=at + timedelta(seconds=301),
                        futures_flow=FuturesFlowContext(
                            state=(
                                FuturesFlowState.LONG_BUILDUP
                                if side == "BUY_CALL"
                                else FuturesFlowState.SHORT_BUILDUP
                            ),
                            side=side,
                            strength=Decimal("0.50"),
                            reason="stable reversal impulse",
                        ),
                    )
                )
                self.assertEqual(len(released), 1)
                self.assertEqual(released[0].side, side)

    def test_positioning_does_not_bypass_convexity_gate_on_either_side(
        self,
    ) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            weights={
                "futures_flow": Decimal("0.15"),
                "oi_migration": Decimal("0.05"),
            },
            minimum_compression_observations=3,
            minimum_independent_families=1,
            minimum_horizon_agreement=2,
            minimum_buyability_score=Decimal("0.50"),
            require_expansion_trigger=True,
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        for side in ("BUY_CALL", "BUY_PUT"):
            with self.subTest(side=side):
                state = (
                    FuturesFlowState.LONG_BUILDUP
                    if side == "BUY_CALL"
                    else FuturesFlowState.SHORT_BUILDUP
                )
                positioning = FuturesPositioningContext(
                    ready=True,
                    state=state,
                    side=side,
                    strength=Decimal("0.80"),
                    horizon_agreement=3,
                    horizons=tuple(
                        FuturesFlowHorizonContext(
                            horizon_seconds=horizon,
                            state=state,
                            side=side,
                            strength=Decimal("0.80"),
                        )
                        for horizon in (15, 60, 180)
                    ),
                    reason="persistent futures positioning",
                )
                flow = FuturesFlowContext(
                    state=state,
                    side=side,
                    strength=Decimal("0.80"),
                    reason="legacy flow",
                    positioning=positioning,
                )
                strategy = DerivativesQuantStrategy(
                    settings,
                    frozenset({"futures_flow", "volume_oi"}),
                )
                base = _context(at) if side == "BUY_CALL" else _put_context(at)
                for seconds, moved in ((0, False), (60, True), (180, True)):
                    candidates = strategy.evaluate(
                        replace(
                            base,
                            captured_at=at + timedelta(seconds=seconds),
                            spot=Decimal("25000"),
                            gamma_signal=None,
                            futures_flow=flow,
                            option_chain_legs=_positioning_legs(
                                side,
                                moved=moved,
                            ),
                        )
                    )

                self.assertEqual(candidates, ())
                convexity = next(
                    item
                    for item in strategy.last_diagnostic.checks
                    if item.code == "convexity_expansion"
                )
                self.assertFalse(convexity.passed)

    def test_quant_prefers_ready_multi_horizon_futures_positioning(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            weights={"futures_flow": Decimal("1")},
            minimum_direction_score=Decimal("0.20"),
            warmup_direction_score=Decimal("0.20"),
            early_direction_score=Decimal("0.10"),
            minimum_independent_families=1,
            minimum_horizon_agreement=2,
            minimum_buyability_score=Decimal("0"),
            require_expansion_trigger=False,
        )
        strategy = DerivativesQuantStrategy(
            settings,
            frozenset({"futures_flow"}),
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        positioning = FuturesPositioningContext(
            ready=True,
            state=FuturesFlowState.LONG_BUILDUP,
            side="BUY_CALL",
            strength=Decimal("0.75"),
            horizon_agreement=3,
            horizons=tuple(
                FuturesFlowHorizonContext(
                    horizon_seconds=horizon,
                    state=FuturesFlowState.LONG_BUILDUP,
                    side="BUY_CALL",
                    strength=Decimal("0.75"),
                )
                for horizon in (15, 60, 180)
            ),
            reason="three futures horizons confirm long buildup",
        )
        context = replace(
            _context(at),
            futures_flow=FuturesFlowContext(
                state=FuturesFlowState.SHORT_BUILDUP,
                side="BUY_PUT",
                strength=Decimal("0.80"),
                reason="legacy short-window conflict",
                positioning=positioning,
            ),
        )

        strategy.evaluate(context)

        self.assertEqual(strategy.last_diagnostic.proposed_side, "BUY_CALL")
        flow_check = next(
            item
            for item in strategy.last_diagnostic.feature_checks
            if item.code == "futures_flow"
        )
        self.assertEqual(flow_check.proposed_side, "BUY_CALL")
        self.assertIn("+0.7500", flow_check.observed)

    def test_non_directional_quant_regimes_have_bounded_scores(self) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        expected_move_context = replace(
            _context(at),
            spot=Decimal("25075"),
            expected_lower=Decimal("24900"),
            expected_upper=Decimal("25100"),
        )

        self.assertEqual(
            _expected_move_buyability_score(expected_move_context),
            Decimal("1"),
        )
        self.assertEqual(
            _expected_move_buyability_score(
                replace(
                    expected_move_context,
                    expected_lower=None,
                    expected_upper=None,
                )
            ),
            Decimal("0"),
        )
        self.assertEqual(
            _vix_buyability_score(Decimal("18")),
            Decimal("1"),
        )
        self.assertLess(
            _vix_buyability_score(Decimal("35")),
            _vix_buyability_score(Decimal("18")),
        )

    def test_profile_feature_mask_blocks_unselected_quant_inputs(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            weights={"index_momentum": Decimal("1")},
            minimum_direction_score=Decimal("0.10"),
            warmup_direction_score=Decimal("0.10"),
            early_direction_score=Decimal("0.05"),
            minimum_independent_families=1,
            minimum_horizon_agreement=1,
            minimum_buyability_score=Decimal("0"),
            require_expansion_trigger=False,
        )
        strategy = DerivativesQuantStrategy(
            settings,
            frozenset({"futures_flow"}),
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_context(at))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=15)),
                spot=Decimal("25050"),
            )
        )

        self.assertEqual(candidates, ())
        direction_check = next(
            item
            for item in strategy.last_diagnostic.checks
            if item.code == "direction_score"
        )
        self.assertIn("score=+0.0000", direction_check.observed)

    def test_gamma_strategy_requires_gamma_feature_when_profiled(self) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)

        disabled = GammaExpansionStrategy(
            frozenset({"iv_skew"})
        ).evaluate(_context(at))
        enabled = GammaExpansionStrategy(
            frozenset({"gamma_concentration"})
        ).evaluate(_context(at))

        self.assertEqual(disabled, ())
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0].side, "BUY_CALL")

    def test_emits_only_after_independent_quant_families_align(self) -> None:
        strategy = DerivativesQuantStrategy(
            DerivativesQuantSettings()
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_context(at))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=15)),
                spot=Decimal("25005"),
                atm_call_mid=Decimal("103"),
                atm_put_mid=Decimal("97"),
                call_volume=50000,
                put_volume=10000,
            )
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.side, "BUY_CALL")
        self.assertGreater(candidate.direction_score or Decimal("0"), 0)
        self.assertGreaterEqual(
            candidate.buyability_score or Decimal("0"),
            Decimal("0.45"),
        )
        self.assertGreater(
            candidate.forecast_underlying_move or Decimal("0"),
            Decimal("0"),
        )

    def test_expiry_profile_rejects_non_expiry_session(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            require_expiry_day=True,
        )
        strategy = DerivativesQuantStrategy(settings)
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(replace(_context(at), is_expiry_day=False))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=15)),
                spot=Decimal("25005"),
                atm_call_mid=Decimal("103"),
                atm_put_mid=Decimal("97"),
                call_volume=50000,
                put_volume=10000,
                is_expiry_day=False,
            )
        )

        self.assertEqual(candidates, ())
        expiry_check = next(
            item
            for item in strategy.last_diagnostic.checks
            if item.code == "expiry_day"
        )
        self.assertFalse(expiry_check.passed)

    def test_rejects_entry_after_matching_option_leg_is_chased(self) -> None:
        strategy = DerivativesQuantStrategy(
            DerivativesQuantSettings()
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_context(at))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=1)),
                spot=Decimal("25002"),
                atm_call_mid=Decimal("120"),
            )
        )

        self.assertEqual(candidates, ())
        failed = {
            item.code
            for item in strategy.last_diagnostic.checks
            if not item.passed
        }
        self.assertIn("premium_not_chased", failed)

    def test_put_signal_is_the_symmetric_inverse_of_call_signal(self) -> None:
        strategy = DerivativesQuantStrategy(DerivativesQuantSettings())
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_put_context(at))

        candidates = strategy.evaluate(
            replace(
                _put_context(at + timedelta(seconds=15)),
                spot=Decimal("24995"),
                atm_call_mid=Decimal("97"),
                atm_put_mid=Decimal("103"),
                call_volume=10000,
                put_volume=50000,
            )
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].side, "BUY_PUT")
        self.assertLess(
            candidates[0].direction_score or Decimal("0"),
            Decimal("0"),
        )

    def test_identical_relative_data_is_independent_of_clock_time(self) -> None:
        def evaluate_at(at: datetime) -> tuple[str, Decimal | None]:
            strategy = DerivativesQuantStrategy(DerivativesQuantSettings())
            strategy.evaluate(_context(at))
            candidate = strategy.evaluate(
                replace(
                    _context(at + timedelta(seconds=15)),
                    spot=Decimal("25005"),
                    atm_call_mid=Decimal("103"),
                    atm_put_mid=Decimal("97"),
                    call_volume=50000,
                    put_volume=10000,
                )
            )[0]
            return candidate.side, candidate.direction_score

        morning = evaluate_at(datetime(2026, 7, 28, 4, 30, tzinfo=UTC))
        afternoon = evaluate_at(datetime(2026, 7, 28, 9, 0, tzinfo=UTC))

        self.assertEqual(morning, afternoon)

    def test_early_quant_path_requires_persistent_symmetric_flow(self) -> None:
        settings = replace(
            DerivativesQuantSettings(),
            minimum_direction_score=Decimal("0.90"),
            warmup_direction_score=Decimal("0.90"),
            early_direction_score=Decimal("0.10"),
            early_min_buyability_score=Decimal("0.50"),
        )
        strategy = DerivativesQuantStrategy(settings)
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_context(at))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=15)),
                spot=Decimal("25005"),
                atm_call_mid=Decimal("103"),
                atm_put_mid=Decimal("97"),
                call_volume=50000,
                put_volume=10000,
            )
        )

        self.assertEqual(len(candidates), 1)
        self.assertIn("activation=EARLY_QUANT_FLOW", candidates[0].reason)

    def test_research_profile_can_make_early_acceleration_optional(self) -> None:
        class NonAcceleratingQuantStrategy(DerivativesQuantStrategy):
            def _early_direction_persistence(self, **_kwargs):
                return 2, False

        settings = replace(
            DerivativesQuantSettings(),
            weights={"index_momentum": Decimal("1")},
            minimum_direction_score=Decimal("0.90"),
            warmup_direction_score=Decimal("0.90"),
            early_direction_score=Decimal("0.10"),
            early_min_horizon_agreement=1,
            early_min_independent_families=1,
            early_min_option_chain_families=0,
            early_min_buyability_score=Decimal("0.10"),
            early_score_persistence_frames=2,
            minimum_independent_families=1,
            minimum_horizon_agreement=1,
            minimum_buyability_score=Decimal("0.10"),
            require_early_acceleration=False,
            require_expansion_trigger=False,
        )
        strategy = NonAcceleratingQuantStrategy(
            settings,
            frozenset({"atr_normalization"}),
        )
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        strategy.evaluate(_context(at))

        candidates = strategy.evaluate(
            replace(
                _context(at + timedelta(seconds=15)),
                spot=Decimal("25005"),
            )
        )

        self.assertEqual(len(candidates), 1)
        self.assertIn("activation=EARLY_QUANT_FLOW", candidates[0].reason)


if __name__ == "__main__":
    unittest.main()
