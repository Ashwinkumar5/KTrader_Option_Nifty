from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.range_rotation import (
    RangeRotationPhase,
    RangeRotationSettings,
    RangeRotationTracker,
)


class RangeRotationTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        self.tracker = RangeRotationTracker(
            RangeRotationSettings(
                min_range_width_points=Decimal("75"),
                min_reversal_points=Decimal("5"),
                min_remaining_room_points=Decimal("20"),
                min_reward_risk=Decimal("1.5"),
            )
        )

    def update(self, seconds: int, spot: str):
        return self.tracker.update(
            underlying="NIFTY",
            captured_at=self.start + timedelta(seconds=seconds),
            spot=Decimal(spot),
            support=Decimal("24700"),
            resistance=Decimal("24850"),
            level_zone=Decimal("10"),
        )

    def test_confirms_support_rejection_and_emits_call_away_from_boundary(self) -> None:
        first = self.update(0, "24705")
        confirmed = self.update(5, "24718")

        self.assertIsNone(first.signal)
        self.assertEqual(first.phase, RangeRotationPhase.SUPPORT_TEST)
        self.assertEqual(confirmed.signal, "BUY_CALL")
        self.assertEqual(confirmed.phase, RangeRotationPhase.ROTATING_UP)
        self.assertGreaterEqual(confirmed.reward_risk, Decimal("1.5"))

    def test_keeps_up_rotation_alive_through_midrange_continuation(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        continuation = self.update(10, "24740")

        self.assertEqual(continuation.signal, "BUY_CALL")
        self.assertEqual(continuation.phase, RangeRotationPhase.ROTATING_UP)
        self.assertIn("RANGE ROTATION CONTINUATION", continuation.reason)

    def test_uses_pullback_low_as_tighter_invalidation_on_renewed_move(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        self.update(10, "24745")
        pullback = self.update(15, "24732")
        resumed = self.update(20, "24742")

        self.assertIsNone(pullback.signal)
        self.assertEqual(resumed.signal, "BUY_CALL")
        self.assertEqual(resumed.invalidation, Decimal("24731"))

    def test_stops_proposing_when_remaining_reward_risk_is_too_small(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        late = self.update(10, "24820")

        self.assertIsNone(late.signal)
        self.assertEqual(late.phase, RangeRotationPhase.ROTATING_UP)
        self.assertIn("weak reward/risk", late.reason)

    def test_confirms_resistance_rejection_and_emits_put(self) -> None:
        self.update(0, "24845")
        confirmed = self.update(5, "24832")

        self.assertEqual(confirmed.signal, "BUY_PUT")
        self.assertEqual(confirmed.phase, RangeRotationPhase.ROTATING_DOWN)

    def test_single_call_soft_breach_degrades_then_recovers(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        self.update(10, "24745")
        self.update(15, "24732")
        resumed = self.update(20, "24742")
        degraded = self.update(25, "24730")
        recovered = self.update(30, "24734")

        self.assertEqual(resumed.invalidation, Decimal("24731"))
        self.assertIsNone(degraded.signal)
        self.assertEqual(degraded.phase, RangeRotationPhase.DEGRADED_UP)
        self.assertIsNone(recovered.signal)
        self.assertEqual(recovered.phase, RangeRotationPhase.ROTATING_UP)

    def test_call_hard_invalidation_clears_immediately(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        self.update(10, "24745")
        self.update(15, "24732")
        self.update(20, "24742")

        invalidated = self.update(25, "24725")

        self.assertEqual(invalidated.phase, RangeRotationPhase.WAITING)
        self.assertIn("hard invalidation", invalidated.reason)

    def test_two_closed_call_soft_breaches_clear_rotation(self) -> None:
        self.update(0, "24705")
        self.update(5, "24718")
        self.update(10, "24745")
        self.update(15, "24732")
        self.update(20, "24742")
        self.update(30, "24730")
        self.update(45, "24730")

        invalidated = self.update(60, "24730")

        self.assertEqual(invalidated.phase, RangeRotationPhase.WAITING)
        self.assertIn("two closed-frame breaches", invalidated.reason)

    def test_put_soft_breach_and_recovery_are_symmetric(self) -> None:
        self.update(0, "24845")
        self.update(5, "24832")
        self.update(10, "24810")
        self.update(15, "24830")
        resumed = self.update(20, "24820")
        degraded = self.update(25, "24832")
        recovered = self.update(30, "24828")

        self.assertEqual(resumed.invalidation, Decimal("24831"))
        self.assertEqual(degraded.phase, RangeRotationPhase.DEGRADED_DOWN)
        self.assertIsNone(degraded.signal)
        self.assertEqual(recovered.phase, RangeRotationPhase.ROTATING_DOWN)
        self.assertIsNone(recovered.signal)

    def test_timestamp_regression_cannot_reuse_future_rotation_state(self) -> None:
        self.update(10, "24705")
        self.update(20, "24720")
        regressed = self.update(5, "24735")

        self.assertIsNone(regressed.signal)
        self.assertEqual(regressed.phase, RangeRotationPhase.WAITING)


if __name__ == "__main__":
    unittest.main()
