from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dummy_broker_replay.runner import run_replay


class ReplayRuntimeSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_runtime_selection_is_used_and_recorded(self) -> None:
        source = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        temporary_root = Path.cwd() / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
            result = await run_replay(
                source,
                output_root=Path(directory),
                run_id="runtime-selection",
                max_frames=1,
                enabled_strategies=(
                    "DERIVATIVES_QUANT",
                    "GAMMA_EXPANSION",
                ),
                enabled_features=(
                    "premium_response",
                    "futures_flow",
                    "iv_skew",
                    "gamma_concentration",
                    "order_book_imbalance",
                ),
                minimum_book_imbalance=Decimal("0.25"),
            )
            manifest = json.loads(
                (result.run_directory / "run_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        profile = manifest["strategy_configuration"]["profile"]
        enabled_features = {
            name for name, enabled in profile["features"].items() if enabled
        }
        enabled_strategies = {
            name
            for name, settings in profile["strategies"].items()
            if settings["enabled"]
        }
        self.assertEqual(
            enabled_strategies,
            {"DERIVATIVES_QUANT", "GAMMA_EXPANSION"},
        )
        self.assertEqual(
            enabled_features,
            {
                "premium_response",
                "futures_flow",
                "iv_skew",
                "gamma_concentration",
                "order_book_imbalance",
            },
        )
        self.assertEqual(
            profile["microstructure"]["minimum_book_imbalance"],
            "0.25",
        )
        self.assertEqual(
            result.enabled_strategies,
            ("DERIVATIVES_QUANT", "GAMMA_EXPANSION"),
        )


if __name__ == "__main__":
    unittest.main()
