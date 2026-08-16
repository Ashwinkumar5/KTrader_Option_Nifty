from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.signals.noise_filter import (
    DebouncePhase,
    DirectionalSignalDebouncer,
    SignalDebounceSettings,
)


class DirectionalSignalDebouncerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        self.filter = DirectionalSignalDebouncer(
            SignalDebounceSettings(
                frame_seconds=15,
                window_frames=3,
                min_confirmed_frames=2,
            )
        )

    def update(self, seconds: int, signal: str):
        return self.filter.update(
            underlying="NIFTY",
            captured_at=self.start + timedelta(seconds=seconds),
            signal=signal,
            reason=f"{signal} test",
        )

    def test_single_call_frame_only_arms_candidate(self) -> None:
        armed = self.update(0, "BUY_CALL")
        next_bucket = self.update(15, "BUY_CALL")

        self.assertEqual(armed.phase, DebouncePhase.ARMED)
        self.assertEqual(next_bucket.signal, "NEUTRAL")

    def test_two_closed_call_frames_confirm_buy_call(self) -> None:
        self.update(0, "BUY_CALL")
        self.update(15, "BUY_CALL")
        confirmed = self.update(30, "BUY_CALL")

        self.assertEqual(confirmed.signal, "BUY_CALL")
        self.assertEqual(confirmed.phase, DebouncePhase.CONFIRMED)

    def test_put_confirmation_is_symmetric(self) -> None:
        self.update(0, "BUY_PUT")
        self.update(15, "BUY_PUT")
        confirmed = self.update(30, "BUY_PUT")

        self.assertEqual(confirmed.signal, "BUY_PUT")

    def test_one_contrary_frame_degrades_without_flipping(self) -> None:
        self.update(0, "BUY_CALL")
        self.update(15, "BUY_CALL")
        self.update(30, "BUY_CALL")
        degraded = self.update(45, "BUY_PUT")

        self.assertEqual(degraded.signal, "NEUTRAL")
        self.assertEqual(degraded.confirmed_side, "BUY_CALL")
        self.assertEqual(degraded.phase, DebouncePhase.DEGRADED)

    def test_two_closed_put_frames_can_confirm_opposite_direction(self) -> None:
        self.update(0, "BUY_CALL")
        self.update(15, "BUY_CALL")
        self.update(30, "BUY_CALL")
        self.update(45, "BUY_PUT")
        self.update(60, "BUY_PUT")
        flipped = self.update(75, "BUY_PUT")

        self.assertEqual(flipped.signal, "BUY_PUT")
        self.assertEqual(flipped.confirmed_side, "BUY_PUT")


if __name__ == "__main__":
    unittest.main()
