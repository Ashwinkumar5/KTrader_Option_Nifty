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

    def test_profile_inheritance_merges_quant_weights_from_child(self) -> None:
        parent = load_strategy_configuration(
            profile_name="derivatives_only"
        ).profile
        child = load_strategy_configuration(
            profile_name="intraday_directional_premium_momentum_research"
        ).profile

        self.assertEqual(
            child.quant.weights["index_momentum"],
            Decimal("0.65"),
        )
        self.assertEqual(
            child.quant.weights["futures_flow"],
            Decimal("0.35"),
        )
        self.assertNotEqual(
            child.quant.weights["index_momentum"],
            parent.quant.weights["index_momentum"],
        )


if __name__ == "__main__":
    unittest.main()
