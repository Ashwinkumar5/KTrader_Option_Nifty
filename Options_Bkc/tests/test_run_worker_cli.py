from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from decimal import Decimal
from io import StringIO
from pathlib import Path

from scripts.run_worker import _csv_selection, _nats_strategy_selection, build_parser


class RunWorkerCliTests(unittest.TestCase):
    def test_live_runtime_selection_arguments_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "--strategy-profile",
                "derivatives_only",
                "--strategies",
                "GAMMA_EXPANSION",
                "--features",
                "gamma_blast,iv_skew,order_book_imbalance",
                "--minimum-book-imbalance",
                "0.30",
                "--snapshot-interval-ms",
                "5000",
            ]
        )

        self.assertEqual(args.strategies, "GAMMA_EXPANSION")
        self.assertEqual(
            _csv_selection(args.features),
            ("gamma_blast", "iv_skew", "order_book_imbalance"),
        )
        self.assertEqual(args.minimum_book_imbalance, Decimal("0.30"))
        self.assertEqual(args.snapshot_interval_ms, 5000)

    def test_omitted_runtime_selection_keeps_config_defaults(self) -> None:
        args = build_parser().parse_args([])

        self.assertIsNone(args.strategies)
        self.assertIsNone(args.features)
        self.assertIsNone(args.minimum_book_imbalance)
        self.assertIsNone(args.snapshot_interval_ms)
        self.assertEqual(args.market_data_mode, "embedded")
        self.assertIsNone(args.heartbeat_file)
        self.assertEqual(args.heartbeat_stall_seconds, 10.0)

    def test_nats_subscriber_mode_accepts_any_single_strategy(self) -> None:
        args = build_parser().parse_args(
            [
                "--strategies",
                "DERIVATIVES_QUANT",
                "--market-data-mode",
                "nats-subscriber",
            ]
        )

        self.assertEqual(args.market_data_mode, "nats-subscriber")
        self.assertEqual(
            _nats_strategy_selection(_csv_selection(args.strategies)),
            ("DERIVATIVES_QUANT",),
        )
        self.assertEqual(
            _nats_strategy_selection(("GAMMA_EXPANSION",)),
            ("GAMMA_EXPANSION",),
        )
        self.assertEqual(
            _nats_strategy_selection(("OPTION_CHAIN_IMPULSE",)),
            ("OPTION_CHAIN_IMPULSE",),
        )
        self.assertEqual(_nats_strategy_selection(("SMC",)), ("SMC",))

    def test_nats_subscriber_requires_one_strategy_per_process(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _nats_strategy_selection(None)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            _nats_strategy_selection(("DERIVATIVES_QUANT", "SMC"))

    def test_worker_heartbeat_arguments_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "--heartbeat-file",
                "process_watch_dog/runtime/dq.heartbeat",
                "--heartbeat-stall-seconds",
                "12.5",
            ]
        )

        self.assertEqual(
            args.heartbeat_file,
            Path("process_watch_dog/runtime/dq.heartbeat"),
        )
        self.assertEqual(args.heartbeat_stall_seconds, 12.5)

    def test_snapshot_interval_must_be_positive(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(
                    ["--snapshot-interval-ms", "0"]
                )


if __name__ == "__main__":
    unittest.main()
