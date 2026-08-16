from __future__ import annotations

import json
import shutil
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from dummy_broker_replay.generate_research_dashboard import (
    aggregate_combinations,
    generate_dashboard,
)


def _combination(
    name: str,
    *,
    completed: int,
    targets: int,
    stops: int,
    total_return: str,
    drawdown: str,
) -> dict[str, object]:
    return {
        "combination": name,
        "features": ["premium_response"],
        "signals_generated": completed,
        "trades_entered": completed,
        "completed_trades": completed,
        "successful_target_hits": targets,
        "failed_stop_hits": stops,
        "time_exits": completed - targets - stops,
        "management_exits": 0,
        "unresolved_at_tape_end": 0,
        "target_hit_rate_percent": "0",
        "completed_trade_return_percent": total_return,
        "average_trade_return_percent": "0",
        "maximum_trade_drawdown_percent": drawdown,
        "paper_realized_pnl": total_return,
        "net_completed_trade_return_percent": total_return,
        "net_average_trade_return_percent": "0",
        "net_maximum_trade_drawdown_percent": drawdown,
        "net_paper_realized_pnl": total_return,
        "estimated_transaction_cost": "0",
        "qualified_signal_counts_by_strategy": {
            "DERIVATIVES_QUANT": completed,
            "GAMMA_EXPANSION": 0,
        },
    }


class ResearchDashboardTests(unittest.TestCase):
    def test_rolling_aggregation_requires_sessions_and_trade_sample(
        self,
    ) -> None:
        summaries = [
            {
                "experiments": [
                    _combination(
                        "candidate",
                        completed=4,
                        targets=2,
                        stops=2,
                        total_return="8",
                        drawdown="3",
                    ),
                    _combination(
                        "small_sample",
                        completed=1,
                        targets=1,
                        stops=0,
                        total_return="10",
                        drawdown="0",
                    ),
                ]
            },
            {
                "experiments": [
                    _combination(
                        "candidate",
                        completed=4,
                        targets=3,
                        stops=1,
                        total_return="12",
                        drawdown="5",
                    )
                ]
            },
            {
                "experiments": [
                    _combination(
                        "candidate",
                        completed=4,
                        targets=2,
                        stops=2,
                        total_return="-2",
                        drawdown="4",
                    )
                ]
            },
        ]

        rows = aggregate_combinations(
            summaries,
            minimum_cumulative_trades=10,
            minimum_history_sessions=3,
        )
        by_name = {row["combination"]: row for row in rows}

        candidate = by_name["candidate"]
        self.assertEqual(candidate["rolling_rank"], 1)
        self.assertEqual(candidate["sessions"], 3)
        self.assertEqual(candidate["trading_days"], 3)
        self.assertEqual(candidate["completed_trades"], 12)
        self.assertEqual(candidate["successful_target_hits"], 7)
        self.assertEqual(
            candidate["average_trade_return_percent"],
            Decimal("1.50"),
        )
        self.assertEqual(
            candidate["worst_session_drawdown_percent"],
            Decimal("5.00"),
        )
        self.assertEqual(candidate["research_status"], "PROMISING")
        self.assertFalse(by_name["small_sample"]["ranking_eligible"])

    def test_same_date_tapes_do_not_count_as_multiple_trading_days(
        self,
    ) -> None:
        summaries = [
            {
                "trading_date": "2026-07-29",
                "source_sha256": f"tape-{index}",
                "experiments": [
                    _combination(
                        "candidate",
                        completed=10,
                        targets=6,
                        stops=4,
                        total_return="5",
                        drawdown="3",
                    )
                ],
            }
            for index in range(2)
        ]

        row = aggregate_combinations(
            summaries,
            minimum_cumulative_trades=10,
            minimum_trading_days=2,
        )[0]

        self.assertEqual(row["tape_files"], 2)
        self.assertEqual(row["trading_days"], 1)
        self.assertFalse(row["ranking_eligible"])
        self.assertIsNone(row["rolling_rank"])

    def test_losing_sample_never_becomes_research_leader(self) -> None:
        summaries = [
            {
                "trading_date": "2026-07-29",
                "experiments": [
                    _combination(
                        "loser",
                        completed=10,
                        targets=2,
                        stops=8,
                        total_return="-10",
                        drawdown="12",
                    )
                ],
            }
        ]

        row = aggregate_combinations(
            summaries,
            minimum_cumulative_trades=1,
            minimum_trading_days=1,
        )[0]

        self.assertTrue(row["ranking_eligible"])
        self.assertEqual(row["research_status"], "NOT_PROFITABLE")
        self.assertIsNone(row["rolling_rank"])

    def test_generate_dashboard_writes_html_json_and_csv_outputs(
        self,
    ) -> None:
        root = Path.cwd() / ".test-tmp" / f"dashboard-{uuid4().hex}"
        summary_folder = root / "2026-07-30" / "run_test"
        phase1_path = summary_folder / "phase1_summary.json"
        phase2_path = summary_folder / "phase2_summary.json"
        phase1_path_2 = summary_folder / "phase1_summary_2.json"
        phase2_path_2 = summary_folder / "phase2_summary_2.json"
        output = summary_folder / "dashboard"
        summary_folder.mkdir(parents=True)
        phase1 = {
            "record_type": "phase1_feature_research_summary",
            "created_at": "2026-07-30T10:00:00+00:00",
            "source_path": "tape.jsonl",
            "source_sha256": "same-tape",
            "experiments": [
                {
                    "feature": "premium_response",
                    "completed_trades": 1,
                    "successful_target_hits": 1,
                }
            ],
        }
        phase2 = {
            "record_type": "phase2_combination_research_summary",
            "created_at": "2026-07-30T10:05:00+00:00",
            "source_path": "tape.jsonl",
            "source_sha256": "same-tape",
            "experiments": [
                _combination(
                    "gamma_expansion_core",
                    completed=2,
                    targets=1,
                    stops=1,
                    total_return="5",
                    drawdown="5",
                )
            ],
        }
        phase1_path.write_text(json.dumps(phase1), encoding="utf-8")
        phase2_path.write_text(json.dumps(phase2), encoding="utf-8")
        phase1_2 = {
            **phase1,
            "created_at": "2026-07-30T10:10:00+00:00",
            "source_path": "tape_2.jsonl",
            "source_sha256": "second-tape",
        }
        phase2_2 = {
            **phase2,
            "created_at": "2026-07-30T10:15:00+00:00",
            "source_path": "tape_2.jsonl",
            "source_sha256": "second-tape",
        }
        phase1_path_2.write_text(
            json.dumps(phase1_2),
            encoding="utf-8",
        )
        phase2_path_2.write_text(
            json.dumps(phase2_2),
            encoding="utf-8",
        )

        try:
            generated = generate_dashboard(
                phase1_summary_path=(phase1_path, phase1_path_2),
                phase2_summary_path=(phase2_path, phase2_path_2),
                reports_root=root,
                output_directory=output,
                rolling_days=14,
                minimum_cumulative_trades=1,
                minimum_history_sessions=1,
                as_of_date=date(2026, 7, 30),
            )
            manifest = json.loads(
                (generated / "dashboard.json").read_text(
                    encoding="utf-8"
                )
            )
            html = (generated / "dashboard.html").read_text(
                encoding="utf-8"
            )
            csv_outputs_exist = all(
                (generated / name).exists()
                for name in (
                    "daily_features.csv",
                    "daily_combinations.csv",
                    "rolling_14d_combinations.csv",
                )
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

        self.assertEqual(
            manifest["rolling_research_leader"],
            "gamma_expansion_core",
        )
        self.assertEqual(manifest["history_sessions"], 2)
        self.assertEqual(manifest["history_trading_days"], 1)
        self.assertEqual(manifest["batch_file_count"], 2)
        self.assertEqual(manifest["batch_tape_count"], 2)
        self.assertEqual(
            manifest["current_combination_results"][0]["sessions"],
            2,
        )
        self.assertIn("Quant Research Dashboard", html)
        self.assertIn('class="hero"', html)
        self.assertIn("linear-gradient", html)
        self.assertIn('class="rate"', html)
        self.assertTrue(csv_outputs_exist)


if __name__ == "__main__":
    unittest.main()
