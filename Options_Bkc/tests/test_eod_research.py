from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from dummy_broker_replay.run_eod_research import run_eod_research


class EodResearchTests(unittest.TestCase):
    def test_validate_only_writes_audit_before_any_matrix(self) -> None:
        source = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        output_root = Path.cwd() / ".test-tmp"
        output_root.mkdir(exist_ok=True)
        eod_id = f"test-{uuid4().hex}"
        created_directory = output_root / f"{source.stem}_{eod_id}"
        try:
            result = asyncio.run(
                run_eod_research(
                    argparse.Namespace(
                        path=source,
                        mode="event-time",
                        output_root=output_root,
                        eod_id=eod_id,
                        max_frames=None,
                        include_priority_permutations=False,
                        validate_only=True,
                    )
                )
            )
            audit_path = (
                created_directory / "capture_audit.json"
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        finally:
            shutil.rmtree(created_directory, ignore_errors=True)

        self.assertEqual(result, 0)
        self.assertTrue(audit["validation_passed"])
        self.assertFalse(audit["strategy_matrix_started"])


if __name__ == "__main__":
    unittest.main()
