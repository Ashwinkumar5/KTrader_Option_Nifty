from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from app.core.strategy_config import load_strategy_configuration
from dummy_broker_replay.run_phase1_feature_research import (
    PHASE1_FEATURES,
    PAIRED_BASELINE_PROFILE,
    PRICE_ACTION_FEATURES,
    _parse_features,
    _write_phase1_strategy_config,
    run_phase1_feature_research,
)
from dummy_broker_replay.research_features import (
    DIRECTIONAL_FEATURES,
    PAIRED_BASELINE_FEATURES,
    experiment_mode,
)


class Phase1FeatureResearchTests(unittest.TestCase):
    def test_feature_parser_rejects_unknown_and_duplicate_names(self) -> None:
        self.assertEqual(
            _parse_features("futures_flow,iv_skew"),
            ("futures_flow", "iv_skew"),
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            _parse_features("futures_flow,candle_patterns")
        with self.assertRaisesRegex(ValueError, "duplicates"):
            _parse_features("iv_skew,iv_skew")

    def test_profiles_use_standalone_direction_and_paired_ablations(
        self,
    ) -> None:
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        generated_config = (
            output_root / f"phase1-config-{uuid4().hex}.json"
        )
        try:
            names = _write_phase1_strategy_config(
                generated_config,
                selected_features=PHASE1_FEATURES,
                base_config_path=None,
                base_profile="derivatives_only",
            )
            profiles = {
                feature: load_strategy_configuration(
                    generated_config,
                    profile_name=names[feature],
                ).profile
                for feature in PHASE1_FEATURES
            }
            baseline_profile = load_strategy_configuration(
                generated_config,
                profile_name=PAIRED_BASELINE_PROFILE,
            ).profile
        finally:
            generated_config.unlink(missing_ok=True)

        for feature, profile in profiles.items():
            enabled = tuple(
                name
                for name in PHASE1_FEATURES
                if profile.feature_enabled(name)
            )
            expected = (
                (feature,)
                if feature in DIRECTIONAL_FEATURES
                else tuple(
                    dict.fromkeys(PAIRED_BASELINE_FEATURES + (feature,))
                )
            )
            self.assertEqual(enabled, expected)
            self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
            self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
            self.assertFalse(profile.strategy_enabled("LEVEL_REVERSAL"))
            self.assertFalse(profile.strategy_enabled("BREAKOUT_MOMENTUM"))
            self.assertEqual(profile.execution.stop_percent, 5)
            self.assertEqual(profile.execution.target_percent, 10)
            self.assertEqual(
                experiment_mode(feature),
                (
                    "STANDALONE"
                    if feature in DIRECTIONAL_FEATURES
                    else "PAIRED_ABLATION"
                ),
            )
        self.assertEqual(
            tuple(
                feature
                for feature in PHASE1_FEATURES
                if baseline_profile.feature_enabled(feature)
            ),
            PAIRED_BASELINE_FEATURES,
        )
        self.assertIn("atr_normalization", PHASE1_FEATURES)
        self.assertNotIn("atr_normalization", PRICE_ACTION_FEATURES)

    def test_one_feature_replay_writes_isolated_profile_and_final_status(
        self,
    ) -> None:
        source = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        phase1_id = f"test-{uuid4().hex}"
        created_directory = (
            output_root / f"{source.stem}_phase1_{phase1_id}"
        )
        try:
            exit_code = asyncio.run(
                run_phase1_feature_research(
                    argparse.Namespace(
                        path=source,
                        mode="event-time",
                        output_root=output_root,
                        phase1_id=phase1_id,
                        max_frames=1,
                        base_strategy_config=None,
                        base_profile="derivatives_only",
                        features="futures_flow",
                        analytics_traces=[],
                    )
                )
            )
            summary = json.loads(
                (
                    created_directory / "phase1_summary.json"
                ).read_text(encoding="utf-8")
            )
            generated_config = (
                created_directory / "phase1_strategy_config.json"
            )
            profile = load_strategy_configuration(
                generated_config,
                profile_name="phase1_futures_flow",
            ).profile
            output_jsonl = Path(
                summary["experiments"][0]["output_jsonl"]
            )
            last_record = json.loads(
                output_jsonl.read_text(encoding="utf-8").splitlines()[-1]
            )
        finally:
            shutil.rmtree(created_directory, ignore_errors=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["feature_count"], 1)
        self.assertTrue(profile.strategy_enabled("DERIVATIVES_QUANT"))
        self.assertTrue(profile.strategy_enabled("GAMMA_EXPANSION"))
        self.assertFalse(profile.strategy_enabled("LEVEL_REVERSAL"))
        self.assertFalse(profile.strategy_enabled("BREAKOUT_MOMENTUM"))
        self.assertEqual(
            tuple(
                name
                for name in PHASE1_FEATURES
                if profile.feature_enabled(name)
            ),
            ("futures_flow",),
        )
        self.assertTrue(
            all(
                not profile.feature_enabled(name)
                for name in PRICE_ACTION_FEATURES
            )
        )
        self.assertEqual(
            profile.quant.weights["futures_flow"],
            1,
        )
        self.assertTrue(
            all(
                value == 0
                for name, value in profile.quant.weights.items()
                if name != "futures_flow"
            )
        )
        self.assertEqual(last_record["record_type"], "phase1_feature_status")
        self.assertEqual(last_record["feature"], "futures_flow")
        self.assertEqual(output_jsonl.parent.name, "f01")
        self.assertIn("successful_target_hits", last_record)
        self.assertIn("failed_stop_hits", last_record)
        self.assertEqual(last_record["feature_role"], "DIRECTIONAL")
        self.assertEqual(last_record["experiment_mode"], "STANDALONE")
        self.assertIn("net_paper_realized_pnl", last_record)
        self.assertIn("target_feature_coverage", last_record)

    def test_context_feature_runs_against_shared_directional_baseline(
        self,
    ) -> None:
        source = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        phase1_id = f"test-{uuid4().hex}"
        created_directory = (
            output_root / f"{source.stem}_phase1_{phase1_id}"
        )
        try:
            exit_code = asyncio.run(
                run_phase1_feature_research(
                    argparse.Namespace(
                        path=source,
                        mode="event-time",
                        output_root=output_root,
                        phase1_id=phase1_id,
                        max_frames=1,
                        base_strategy_config=None,
                        base_profile="derivatives_only",
                        features="expected_move",
                        analytics_traces=[],
                        round_trip_cost_percent="0.20",
                    )
                )
            )
            summary = json.loads(
                (
                    created_directory / "phase1_summary.json"
                ).read_text(encoding="utf-8")
            )
            experiment = summary["experiments"][0]
            profile = load_strategy_configuration(
                created_directory / "phase1_strategy_config.json",
                profile_name="phase1_expected_move",
            ).profile
            baseline_output_exists = (
                created_directory
                / "b00"
                / "broker_tape_paired_baseline.jsonl"
            ).is_file()
        finally:
            shutil.rmtree(created_directory, ignore_errors=True)

        self.assertEqual(exit_code, 0)
        self.assertTrue(baseline_output_exists)
        self.assertEqual(experiment["experiment_mode"], "PAIRED_ABLATION")
        self.assertEqual(experiment["feature_role"], "CONTEXT")
        self.assertIn(
            "delta_net_average_trade_return_percent",
            experiment,
        )
        self.assertIsNotNone(summary["paired_baseline"])
        self.assertEqual(
            {
                feature
                for feature in PHASE1_FEATURES
                if profile.feature_enabled(feature)
            },
            {
                "premium_response",
                "futures_flow",
                "expected_move",
            },
        )


if __name__ == "__main__":
    unittest.main()
