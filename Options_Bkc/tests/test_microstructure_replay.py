from __future__ import annotations

import unittest
from pathlib import Path

from dummy_broker_replay.reader import RecordedSessionReader


class MicrostructureReplayTests(unittest.TestCase):
    def test_audits_recorded_session_summary(self) -> None:
        path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )
        audit = RecordedSessionReader(path).audit()

        self.assertEqual(audit.market_events, 3)
        self.assertEqual(audit.gate_frames, 1)
        self.assertEqual(audit.source_qualified, 0)
        self.assertEqual(audit.timestamp_regressions, 0)
        self.assertEqual(audit.excluded_contaminated_contracts, 0)
        self.assertEqual(len(audit.unique_contracts), 2)
        self.assertEqual(audit.underlyings, ("NIFTY",))
