from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.structural_levels import (
    StructuralLevelSettings,
    StructuralLevelTracker,
)


class StructuralLevelTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = StructuralLevelTracker(
            StructuralLevelSettings(frame_seconds=240)
        )
        self.start = datetime(2026, 7, 22, 9, 16, tzinfo=UTC)

    def update(
        self,
        seconds: int,
        support: str,
        resistance: str,
    ):
        return self.tracker.update(
            underlying="NIFTY",
            captured_at=self.start + timedelta(seconds=seconds),
            support=Decimal(support),
            resistance=Decimal(resistance),
        )

    def test_keeps_levels_fixed_inside_four_minute_frame(self) -> None:
        first = self.update(0, "24000", "24200")
        noisy = self.update(30, "24050", "24150")

        self.assertEqual(first.frame_seconds, 240)
        self.assertEqual(noisy.support, Decimal("24000"))
        self.assertEqual(noisy.resistance, Decimal("24200"))

    def test_promotes_most_persistent_levels_after_frame_closes(self) -> None:
        self.update(0, "24000", "24200")
        self.update(30, "24050", "24150")
        self.update(60, "24050", "24150")
        next_frame = self.update(240, "24100", "24300")

        self.assertEqual(next_frame.support, Decimal("24050"))
        self.assertEqual(next_frame.resistance, Decimal("24150"))

    def test_timestamp_regression_resets_state(self) -> None:
        self.update(240, "24000", "24200")
        reset = self.update(0, "23950", "24250")

        self.assertEqual(reset.support, Decimal("23950"))
        self.assertEqual(reset.resistance, Decimal("24250"))


if __name__ == "__main__":
    unittest.main()
