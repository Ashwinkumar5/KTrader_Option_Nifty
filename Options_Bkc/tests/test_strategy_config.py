from __future__ import annotations

import unittest
from decimal import Decimal

from app.core.strategy_config import (
    apply_runtime_strategy_selection,
    load_strategy_configuration,
)


class StrategyConfigurationTests(unittest.TestCase):
    def test_no_runtime_selection_preserves_configured_profile(self) -> None:
        configuration = load_strategy_configuration(
            profile_name="derivatives_only"
        )

        self.assertIs(
            apply_runtime_strategy_selection(configuration),
            configuration,
        )

    def test_runtime_selection_enables_only_requested_items(self) -> None:
        configuration = apply_runtime_strategy_selection(
            load_strategy_configuration(profile_name="derivatives_only"),
            enabled_strategies=("gamma_blast",),
            enabled_features=(
                "gamma_blast",
                "iv_skew",
                "order_book_imbalance",
            ),
            minimum_book_imbalance=Decimal("0.30"),
        )
        profile = configuration.profile

        self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertFalse(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertTrue(profile.feature_enabled("gamma_concentration"))
        self.assertTrue(profile.feature_enabled("iv_skew"))
        self.assertTrue(profile.feature_enabled("order_book_imbalance"))
        self.assertFalse(profile.feature_enabled("futures_flow"))
        self.assertEqual(
            profile.microstructure.minimum_book_imbalance,
            Decimal("0.30"),
        )
        self.assertEqual(profile.name, "derivatives_only__runtime")

    def test_runtime_selection_rejects_unknown_names(self) -> None:
        configuration = load_strategy_configuration(
            profile_name="derivatives_only"
        )

        with self.assertRaisesRegex(ValueError, "unknown feature"):
            apply_runtime_strategy_selection(
                configuration,
                enabled_features=("not_a_feature",),
            )

    def test_runtime_can_enable_feature_omitted_from_profile_json(self) -> None:
        profile = apply_runtime_strategy_selection(
            load_strategy_configuration(profile_name="derivatives_only"),
            enabled_strategies=("GAMMA_EXPANSION",),
            enabled_features=("opening_context",),
        ).profile

        self.assertTrue(profile.feature_enabled("opening_context"))
        self.assertFalse(profile.feature_enabled("premium_response"))

    def test_runtime_feature_selection_removes_hidden_book_gate(self) -> None:
        configuration = apply_runtime_strategy_selection(
            load_strategy_configuration(profile_name="derivatives_only"),
            enabled_features=("iv_skew",),
        )
        microstructure = configuration.profile.microstructure

        self.assertFalse(
            microstructure.require_target_option_confirmation
        )
        self.assertFalse(microstructure.require_futures_confirmation)
        self.assertEqual(microstructure.minimum_option_confirmations, 0)
        self.assertEqual(microstructure.minimum_futures_confirmations, 0)

    def test_runtime_feature_selection_normalizes_quant_weights(self) -> None:
        configuration = apply_runtime_strategy_selection(
            load_strategy_configuration(profile_name="derivatives_only"),
            enabled_strategies=("DERIVATIVES_QUANT",),
            enabled_features=("futures_flow", "consolidated_pcr"),
        )
        quant = configuration.profile.quant

        self.assertEqual(
            sum(quant.weights.values(), Decimal("0")),
            Decimal("1.000000"),
        )
        self.assertGreater(quant.weights["futures_flow"], Decimal("0"))
        self.assertGreater(quant.weights["pcr_context"], Decimal("0"))
        self.assertTrue(
            all(
                value == 0
                for name, value in quant.weights.items()
                if name not in {"futures_flow", "pcr_context"}
            )
        )
        self.assertEqual(quant.minimum_independent_families, 2)
        self.assertFalse(quant.require_expansion_trigger)

    def test_context_only_quant_runtime_selection_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "directional feature"):
            apply_runtime_strategy_selection(
                load_strategy_configuration(
                    profile_name="derivatives_only"
                ),
                enabled_strategies=("DERIVATIVES_QUANT",),
                enabled_features=("india_vix_regime",),
            )

    def test_runtime_strategy_priority_orders_enabled_strategies(self) -> None:
        configuration = apply_runtime_strategy_selection(
            load_strategy_configuration(profile_name="derivatives_only"),
            enabled_strategies=(
                "DERIVATIVES_QUANT",
                "GAMMA_EXPANSION",
            ),
            strategy_priority=(
                "GAMMA_EXPANSION",
                "DERIVATIVES_QUANT",
            ),
        )
        profile = configuration.profile

        self.assertEqual(
            profile.strategy_priority("GAMMA_EXPANSION"),
            10,
        )
        self.assertEqual(
            profile.strategy_priority("DERIVATIVES_QUANT"),
            20,
        )

    def test_derivatives_profile_keeps_only_quant_and_gamma_strategies(self) -> None:
        configuration = load_strategy_configuration(
            profile_name="derivatives_only"
        )
        profile = configuration.profile

        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("LEVEL_REVERSAL"))
        self.assertFalse(profile.strategy_enabled("BREAKOUT_MOMENTUM"))
        self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertFalse(profile.feature_enabled("opening_context"))
        self.assertFalse(profile.feature_enabled("candle_patterns"))
        self.assertFalse(
            profile.microstructure.require_futures_confirmation
        )

    def test_intraday_directional_premium_profile_is_research_only_branch(
        self,
    ) -> None:
        profile = load_strategy_configuration(
            profile_name="intraday_directional_premium_momentum_research"
        ).profile

        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertTrue(profile.feature_enabled("expected_move"))
        self.assertTrue(profile.feature_enabled("premium_response"))
        self.assertTrue(profile.feature_enabled("futures_flow"))
        self.assertTrue(profile.feature_enabled("iv_surface"))
        self.assertTrue(profile.feature_enabled("atr_normalization"))
        self.assertTrue(profile.feature_enabled("order_book_imbalance"))
        self.assertFalse(profile.feature_enabled("consolidated_pcr"))
        self.assertEqual(
            profile.quant.weights["index_momentum"],
            Decimal("0.65"),
        )
        self.assertEqual(
            profile.quant.weights["option_premium_momentum"],
            Decimal("0.0"),
        )
        self.assertEqual(
            {
                name
                for name, weight in profile.quant.weights.items()
                if weight > 0
            },
            {"futures_flow", "index_momentum"},
        )
        self.assertFalse(profile.quant.require_expansion_trigger)
        self.assertFalse(profile.quant.require_early_acceleration)
        self.assertEqual(profile.quant.early_min_option_chain_families, 0)
        self.assertEqual(
            profile.microstructure.gate_minimum_directional_confirmations,
            1,
        )
        self.assertEqual(
            profile.microstructure.gate_minimum_independent_families,
            1,
        )
        self.assertEqual(profile.execution.stop_percent, Decimal("5.0"))
        self.assertEqual(profile.execution.target_percent, Decimal("10.0"))
        self.assertFalse(profile.execution.event_driven_exit)
        self.assertIsNone(profile.execution.trailing_activation_percent)

    def test_intraday_from_legends_is_a_separate_trend_profile(self) -> None:
        profile = load_strategy_configuration(
            profile_name="intraday_from_legends"
        ).profile

        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertTrue(profile.feature_enabled("consolidated_pcr"))
        self.assertTrue(profile.feature_enabled("volume_oi"))
        self.assertTrue(profile.feature_enabled("momentum_exhaustion"))
        self.assertEqual(
            sum(profile.quant.weights.values(), Decimal("0")),
            Decimal("1.00"),
        )
        self.assertTrue(profile.microstructure.event_driven_entry)
        self.assertTrue(profile.execution.event_driven_exit)
        self.assertEqual(
            profile.execution.trailing_activation_percent,
            Decimal("5.0"),
        )
        self.assertEqual(
            profile.execution.trailing_drawdown_percent,
            Decimal("3.0"),
        )
        self.assertTrue(profile.execution.close_at_tape_end)

    def test_cross_strike_impulse_is_a_separate_profile(self) -> None:
        profile = load_strategy_configuration(
            profile_name="cross_strike_premium_impulse_research"
        ).profile

        self.assertTrue(profile.strategy_enabled("OPTION_CHAIN_IMPULSE"))
        self.assertFalse(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertTrue(profile.feature_enabled("premium_response"))
        self.assertTrue(profile.feature_enabled("volume_oi"))
        self.assertFalse(profile.feature_enabled("iv_surface"))
        self.assertEqual(profile.impulse.window_seconds, 30)
        self.assertEqual(
            profile.impulse.minimum_return_gap_percent,
            Decimal("1.2"),
        )
        self.assertEqual(
            profile.impulse.maximum_return_gap_percent,
            Decimal("3.0"),
        )
        self.assertEqual(
            profile.impulse.minimum_volume_ratio,
            Decimal("0.75"),
        )
        self.assertEqual(
            profile.microstructure.event_entry_cutoff_time,
            "13:30:00",
        )
        self.assertEqual(profile.execution.stop_percent, Decimal("2.5"))
        self.assertEqual(profile.execution.no_follow_through_seconds, 120)
        self.assertEqual(
            profile.execution.trailing_activation_percent,
            Decimal("2.0"),
        )
        self.assertTrue(profile.execution.event_driven_exit)
        self.assertFalse(profile.impulse.aggregate_residual_over_window)
        self.assertFalse(
            profile.microstructure.require_directional_option_book
        )
        self.assertIsNone(
            profile.microstructure.minimum_candidate_premium_chase_percent
        )

    def test_confirmed_cross_strike_profile_is_opt_in(self) -> None:
        profile = load_strategy_configuration(
            profile_name="cross_strike_confirmed_impulse_research"
        ).profile

        self.assertTrue(profile.strategy_enabled("OPTION_CHAIN_IMPULSE"))
        self.assertTrue(profile.impulse.aggregate_residual_over_window)
        self.assertEqual(
            profile.microstructure.minimum_book_imbalance,
            Decimal("0.25"),
        )
        self.assertTrue(
            profile.microstructure.require_directional_option_book
        )
        self.assertEqual(
            profile.microstructure.minimum_candidate_premium_chase_percent,
            Decimal("0"),
        )
        self.assertEqual(
            profile.microstructure.maximum_candidate_premium_chase_percent,
            Decimal("1"),
        )

    def test_gamma_blast_profile_is_isolated_and_executable(self) -> None:
        profile = load_strategy_configuration(
            profile_name="gamma_blast"
        ).profile

        self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertFalse(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertTrue(profile.feature_enabled("gamma_concentration"))
        self.assertTrue(profile.feature_enabled("straddle_expansion"))
        self.assertTrue(profile.feature_enabled("iv_surface"))
        self.assertTrue(profile.feature_enabled("premium_response"))
        self.assertTrue(profile.feature_enabled("order_book_imbalance"))
        self.assertFalse(profile.feature_enabled("iv_skew"))
        self.assertFalse(profile.feature_enabled("futures_flow"))
        self.assertEqual(
            profile.microstructure.gate_minimum_confirmations,
            1,
        )
        self.assertEqual(
            profile.microstructure.gate_minimum_directional_confirmations,
            1,
        )
        self.assertFalse(
            profile.microstructure.gamma_require_structural_room
        )
        self.assertEqual(profile.execution.stop_percent, Decimal("5.0"))
        self.assertEqual(profile.execution.target_percent, Decimal("50.0"))

    def test_combined_gamma_and_directional_profile_is_executable(self) -> None:
        profile = load_strategy_configuration(
            profile_name="gamma_blast_directional_momentum_research"
        ).profile

        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertEqual(
            {
                name
                for name, enabled in profile.features.items()
                if enabled
            },
            {
                "expected_move",
                "premium_response",
                "futures_flow",
                "iv_surface",
                "atr_normalization",
                "gamma_concentration",
                "straddle_expansion",
                "order_book_imbalance",
            },
        )
        self.assertEqual(
            profile.quant.weights["index_momentum"],
            Decimal("0.65"),
        )
        self.assertEqual(
            profile.quant.weights["futures_flow"],
            Decimal("0.35"),
        )
        self.assertFalse(
            profile.microstructure.gamma_require_structural_room
        )
        self.assertEqual(
            profile.microstructure.gate_minimum_confirmations,
            1,
        )
        self.assertEqual(profile.execution.stop_percent, Decimal("5.0"))
        self.assertEqual(profile.execution.target_percent, Decimal("10.0"))

    def test_expiry_impulse_profile_is_isolated_from_gamma(self) -> None:
        profile = load_strategy_configuration(
            profile_name="expiry_long_premium_impulse_research"
        ).profile

        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertFalse(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertTrue(profile.quant.require_expiry_day)
        self.assertTrue(profile.quant.require_futures_flow)
        self.assertTrue(profile.quant.require_expansion_trigger)
        self.assertEqual(
            profile.quant.minimum_buyability_score,
            Decimal("0.60"),
        )
        self.assertEqual(
            profile.quant.early_min_buyability_score,
            Decimal("0.60"),
        )
        self.assertEqual(profile.quant.minimum_independent_families, 4)
        self.assertEqual(profile.quant.early_min_option_chain_families, 2)
        self.assertEqual(
            {
                name
                for name, weight in profile.quant.weights.items()
                if weight > 0
            },
            {
                "index_momentum",
                "futures_flow",
                "option_premium_momentum",
                "option_volume_flow",
                "oi_migration",
            },
        )
        self.assertEqual(
            profile.microstructure.minimum_option_confirmations,
            2,
        )


    def test_child_feature_section_replaces_parent_features(self) -> None:
        profile = load_strategy_configuration(
            profile_name="gamma_blast"
        ).profile

        self.assertEqual(
            {
                name
                for name, enabled in profile.features.items()
                if enabled
            },
            {
                "premium_response",
                "iv_surface",
                "gamma_concentration",
                "straddle_expansion",
                "order_book_imbalance",
            },
        )
        self.assertIn("opening_context", profile.features)
        self.assertFalse(profile.feature_enabled("opening_context"))

    def test_profile_inheritance_can_disable_futures_book_gate(self) -> None:
        configuration = load_strategy_configuration(
            profile_name="derivatives_without_futures_book"
        )

        self.assertFalse(
            configuration.profile.microstructure
            .require_futures_confirmation
        )
        self.assertEqual(
            configuration.profile.quant.minimum_direction_score,
            load_strategy_configuration(
                profile_name="derivatives_only"
            ).profile.quant.minimum_direction_score,
        )

    def test_mandatory_futures_book_is_an_explicit_ablation(self) -> None:
        configuration = load_strategy_configuration(
            profile_name="derivatives_with_mandatory_futures_book"
        )

        self.assertTrue(
            configuration.profile.microstructure
            .require_futures_confirmation
        )

    def test_high_recall_threshold_is_eod_only_profile(self) -> None:
        active = load_strategy_configuration(
            profile_name="derivatives_only"
        ).profile
        experimental = load_strategy_configuration(
            profile_name="derivatives_adaptive_recall_experimental"
        ).profile

        self.assertEqual(
            active.quant.minimum_direction_score,
            Decimal("0.34"),
        )
        self.assertEqual(
            experimental.quant.minimum_direction_score,
            Decimal("0.18"),
        )

    def test_isolated_quant_research_profiles_change_one_threshold(self) -> None:
        baseline = load_strategy_configuration(
            profile_name="derivatives_only"
        ).profile.quant
        horizon = load_strategy_configuration(
            profile_name="derivatives_quant_horizon_research"
        ).profile.quant
        family = load_strategy_configuration(
            profile_name="derivatives_quant_family_research"
        ).profile.quant
        buyability = load_strategy_configuration(
            profile_name="derivatives_quant_buyability_research"
        ).profile.quant

        self.assertEqual(baseline.early_min_horizon_agreement, 3)
        self.assertEqual(horizon.early_min_horizon_agreement, 2)
        self.assertEqual(horizon.early_min_independent_families, 4)
        self.assertEqual(family.early_min_independent_families, 3)
        self.assertEqual(family.early_min_buyability_score, Decimal("0.65"))
        self.assertEqual(
            buyability.early_min_buyability_score,
            Decimal("0.60"),
        )
        self.assertEqual(buyability.early_min_horizon_agreement, 3)


if __name__ == "__main__":
    unittest.main()
