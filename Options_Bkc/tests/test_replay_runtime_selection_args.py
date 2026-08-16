from __future__ import annotations

import argparse
import unittest
from decimal import Decimal

from dummy_broker_replay.runtime_selection import (
    RuntimeReplaySelection,
    add_runtime_selection_arguments,
    runtime_selection_from_args,
)


class RuntimeReplaySelectionArgumentTests(unittest.TestCase):
    def test_common_arguments_are_shared_by_replay_entry_points(self) -> None:
        parser = argparse.ArgumentParser()
        add_runtime_selection_arguments(parser)
        selection = runtime_selection_from_args(
            parser.parse_args(
                [
                    "--strategies",
                    "DERIVATIVES_QUANT,GAMMA_EXPANSION",
                    "--features",
                    "iv_skew,order_book_imbalance",
                    "--minimum-book-imbalance",
                    "0.30",
                ]
            )
        )

        self.assertEqual(
            selection,
            RuntimeReplaySelection(
                enabled_strategies=(
                    "DERIVATIVES_QUANT",
                    "GAMMA_EXPANSION",
                ),
                enabled_features=(
                    "iv_skew",
                    "order_book_imbalance",
                ),
                minimum_book_imbalance=Decimal("0.30"),
            ),
        )

    def test_omitted_common_arguments_preserve_profile(self) -> None:
        parser = argparse.ArgumentParser()
        add_runtime_selection_arguments(parser)

        self.assertEqual(
            runtime_selection_from_args(parser.parse_args([])),
            RuntimeReplaySelection(),
        )

    def test_book_imbalance_outside_unit_interval_is_rejected(self) -> None:
        parser = argparse.ArgumentParser()
        add_runtime_selection_arguments(parser)

        with self.assertRaisesRegex(ValueError, "between zero and one"):
            runtime_selection_from_args(
                parser.parse_args(
                    ["--minimum-book-imbalance", "1.01"]
                )
            )


if __name__ == "__main__":
    unittest.main()
