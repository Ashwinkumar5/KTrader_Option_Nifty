from __future__ import annotations

import unittest
from pathlib import Path

from app.backtesting.microstructure_replay import summarize


class MicrostructureReplayTests(unittest.TestCase):
    def test_summarizes_market_events_and_shadow_qualified_decisions(self) -> None:
        path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "microstructure_summary.jsonl"
        )
        summary, qualified = summarize(path)

        self.assertEqual(summary.market_events, 2)
        self.assertEqual(summary.complete_books, 1)
        self.assertEqual(summary.microstructure_candidates, 1)
        self.assertEqual(summary.candidates_by_side, {"BUY_CALL": 1})
        self.assertEqual(summary.gate_decisions, 2)
        self.assertEqual(summary.shadow_qualified, 1)
        self.assertEqual(len(qualified), 1)
