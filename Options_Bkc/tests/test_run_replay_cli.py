from __future__ import annotations

import unittest
from decimal import Decimal

from dummy_broker_replay.run_replay import build_parser
from dummy_broker_replay.runtime_selection import (
    runtime_selection_from_args,
)


class RunReplayCliTests(unittest.TestCase):
    def test_runtime_research_arguments_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "tape.jsonl",
                "--strategies",
                "DERIVATIVES_QUANT,GAMMA_EXPANSION",
                "--features",
                "premium_response,iv_skew,order_book_imbalance",
                "--minimum-book-imbalance",
                "0.25",
            ]
        )
        selection = runtime_selection_from_args(args)

        self.assertEqual(
            selection.enabled_strategies,
            ("DERIVATIVES_QUANT", "GAMMA_EXPANSION"),
        )
        self.assertEqual(
            selection.enabled_features,
            (
                "premium_response",
                "iv_skew",
                "order_book_imbalance",
            ),
        )
        self.assertEqual(
            selection.minimum_book_imbalance,
            Decimal("0.25"),
        )

    def test_omitted_research_overrides_preserve_profile(self) -> None:
        args = build_parser().parse_args(["tape.jsonl"])

        self.assertIsNone(args.strategies)
        self.assertIsNone(args.features)
        self.assertIsNone(args.minimum_book_imbalance)
        self.assertFalse(args.compact_output)

    def test_compact_output_is_opt_in(self) -> None:
        args = build_parser().parse_args(
            ["tape.jsonl", "--compact-output"]
        )

        self.assertTrue(args.compact_output)


if __name__ == "__main__":
    unittest.main()
