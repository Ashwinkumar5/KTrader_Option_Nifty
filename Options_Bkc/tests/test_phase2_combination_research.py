from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.core.strategy_config import load_strategy_configuration
from dummy_broker_replay.run_phase1_feature_research import (
    PHASE1_FEATURES,
    PRICE_ACTION_FEATURES,
)
from dummy_broker_replay.run_phase2_combination_research import (
    PHASE2_COMBINATIONS,
    _parse_combinations,
    _rank_statuses,
    _write_phase2_strategy_config,
    run_phase2_combination_research,
)


class Phase2CombinationResearchTests(unittest.TestCase):
    def test_combination_parser_rejects_unknown_and_duplicate_names(
        self,
    ) -> None:
        selected = _parse_combinations(
            "gamma_expansion_core,cross_market_derivatives"
        )
        self.assertEqual(
            tuple(item.name for item in selected),
            (
                "gamma_expansion_core",
                "cross_market_derivatives",
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            _parse_combinations("gamma_expansion_core,price_breakout")
        with self.assertRaisesRegex(ValueError, "duplicates"):
            _parse_combinations(
                "gamma_expansion_core,gamma_expansion_core"
            )

    def test_all_combination_profiles_are_valid_quant_only_profiles(
        self,
    ) -> None:
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        generated_config = (
            output_root / f"phase2-config-{uuid4().hex}.json"
        )
        try:
            names = _write_phase2_strategy_config(
                generated_config,
                combinations=PHASE2_COMBINATIONS,
                base_config_path=None,
                base_profile="derivatives_only",
            )
            profiles = {
                combination.name: load_strategy_configuration(
                    generated_config,
                    profile_name=names[combination.name],
                ).profile
                for combination in PHASE2_COMBINATIONS
            }
        finally:
            generated_config.unlink(missing_ok=True)

        for combination in PHASE2_COMBINATIONS:
            profile = profiles[combination.name]
            enabled = {
                name
                for name in PHASE1_FEATURES
                if profile.feature_enabled(name)
            }
            self.assertEqual(enabled, set(combination.features))
            self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
            self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
            self.assertFalse(profile.strategy_enabled("LEVEL_REVERSAL"))
            self.assertFalse(profile.strategy_enabled("BREAKOUT_MOMENTUM"))
            self.assertTrue(
                all(
                    not profile.feature_enabled(name)
                    for name in PRICE_ACTION_FEATURES
                )
            )
            self.assertEqual(
                profile.quant.weights["index_momentum"],
                Decimal("0"),
            )
            self.assertAlmostEqual(
                float(sum(profile.quant.weights.values())),
                1.0,
                places=5,
            )
            self.assertEqual(profile.execution.stop_percent, 5)
            self.assertEqual(profile.execution.target_percent, 10)
        by_name = {
            item.name: item for item in PHASE2_COMBINATIONS
        }
        self.assertIn(
            "atr_normalization",
            by_name["volatility_surface_regime"].features,
        )
        self.assertIn(
            "atr_normalization",
            by_name["full_quant_ensemble"].features,
        )

    def test_research_rank_requires_sample_and_prefers_expectancy_then_drawdown(
        self,
    ) -> None:
        statuses = [
            {
                "combination": "a",
                "completed_trades": 5,
                "average_trade_return_percent": "2.0",
                "maximum_trade_drawdown_percent": "4.0",
                "target_hit_rate_percent": "50",
            },
            {
                "combination": "b",
                "completed_trades": 5,
                "average_trade_return_percent": "2.0",
                "maximum_trade_drawdown_percent": "2.0",
                "target_hit_rate_percent": "40",
            },
            {
                "combination": "small_sample",
                "completed_trades": 1,
                "average_trade_return_percent": "10",
                "maximum_trade_drawdown_percent": "0",
                "target_hit_rate_percent": "100",
            },
        ]

        ranked = _rank_statuses(statuses, minimum_trades=3)
        by_name = {
            str(item["combination"]): item for item in ranked
        }

        self.assertEqual(by_name["b"]["research_rank"], 1)
        self.assertEqual(by_name["a"]["research_rank"], 2)
        self.assertIsNone(
            by_name["small_sample"]["research_rank"]
        )
        self.assertFalse(
            by_name["small_sample"]["ranking_eligible"]
        )

    def test_one_combination_replay_writes_report_and_final_status(
        self,
    ) -> None:
        source = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        phase2_id = f"test-{uuid4().hex}"
        created_directory = (
            output_root / f"{source.stem}_phase2_{phase2_id}"
        )
        try:
            exit_code = asyncio.run(
                run_phase2_combination_research(
                    argparse.Namespace(
                        path=source,
                        mode="event-time",
                        output_root=output_root,
                        phase2_id=phase2_id,
                        max_frames=1,
                        base_strategy_config=None,
                        base_profile="derivatives_only",
                        combinations="gamma_expansion_core",
                        minimum_ranking_trades=3,
                        phase1_summary=None,
                        analytics_traces=[],
                    )
                )
            )
            summary = json.loads(
                (
                    created_directory / "phase2_summary.json"
                ).read_text(encoding="utf-8")
            )
            output_jsonl = Path(
                summary["experiments"][0]["output_jsonl"]
            )
            last_record = json.loads(
                output_jsonl.read_text(encoding="utf-8").splitlines()[-1]
            )
        finally:
            shutil.rmtree(created_directory, ignore_errors=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["combination_count"], 1)
        self.assertFalse(summary["automatic_production_selection"])
        self.assertEqual(
            last_record["record_type"],
            "phase2_combination_status",
        )
        self.assertEqual(
            last_record["combination"],
            "gamma_expansion_core",
        )
        self.assertEqual(output_jsonl.parent.name, "c01")
        self.assertIn("maximum_trade_drawdown_percent", last_record)
        self.assertIn(
            "qualified_signal_counts_by_strategy",
            last_record,
        )
        self.assertIn("derivatives_quant_signals", last_record)
        self.assertIn("gamma_expansion_signals", last_record)
        self.assertIn("net_paper_realized_pnl", last_record)
        self.assertIn("feature_coverage", last_record)


if __name__ == "__main__":
    unittest.main()
