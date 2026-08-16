from __future__ import annotations

import unittest

from app.core.config import load_settings
from app.domain.models import StrategyFamily, StrategyResolverPolicy
from dummy_broker_replay.strategy_experiments import (
    apply_strategy_overrides,
    enabled_strategy_names,
    generate_strategy_matrix,
    parse_strategy_families,
    strategy_priority_names,
)


class StrategyExperimentSettingsTests(unittest.TestCase):
    def test_parses_enabled_family_list(self) -> None:
        self.assertEqual(
            parse_strategy_families(
                "gamma_expansion,level_reversal"
            ),
            (
                StrategyFamily.GAMMA_EXPANSION,
                StrategyFamily.LEVEL_REVERSAL,
            ),
        )

    def test_applies_enabled_set_policy_and_priority(self) -> None:
        configured = apply_strategy_overrides(
            load_settings(),
            enabled_families=(
                StrategyFamily.GAMMA_EXPANSION,
                StrategyFamily.LEVEL_REVERSAL,
            ),
            resolver_policy=StrategyResolverPolicy.FIXED_PRIORITY,
            priority_order=(
                StrategyFamily.GAMMA_EXPANSION,
                StrategyFamily.LEVEL_REVERSAL,
            ),
        )

        self.assertEqual(
            enabled_strategy_names(configured),
            ("LEVEL_REVERSAL", "GAMMA_EXPANSION"),
        )
        self.assertEqual(
            strategy_priority_names(configured),
            ("GAMMA_EXPANSION", "LEVEL_REVERSAL"),
        )
        self.assertEqual(
            configured.strategy_resolver_policy,
            "FIXED_PRIORITY",
        )
        self.assertFalse(configured.strategy_breakout_momentum_enabled)
        self.assertEqual(configured.strategy_gamma_expansion_priority, 10)
        self.assertEqual(configured.strategy_level_reversal_priority, 20)

    def test_priority_must_cover_enabled_families(self) -> None:
        with self.assertRaisesRegex(ValueError, "every enabled family"):
            apply_strategy_overrides(
                load_settings(),
                enabled_families=tuple(StrategyFamily),
                priority_order=(StrategyFamily.LEVEL_REVERSAL,),
            )

    def test_generates_all_non_empty_family_ablations(self) -> None:
        experiments = generate_strategy_matrix(
            load_settings(),
            include_priority_permutations=False,
        )

        self.assertEqual(len(experiments), 7)
        self.assertEqual(
            len({label for label, _ in experiments}),
            len(experiments),
        )

    def test_priority_matrix_adds_all_meaningful_orders(self) -> None:
        experiments = generate_strategy_matrix(
            load_settings(),
            include_priority_permutations=True,
        )

        # 7 regime ablations + 6 two-family orders + 6 three-family orders.
        self.assertEqual(len(experiments), 19)


if __name__ == "__main__":
    unittest.main()
