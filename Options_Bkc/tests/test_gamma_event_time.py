from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.optionchain.memory_state import CoiledSpringDetector, TickSnapshot


class GammaEventTimeTests(unittest.TestCase):
    def test_same_five_minute_signal_at_one_and_fifteen_second_cadence(self) -> None:
        start = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        outcomes = []
        for cadence in (1, 15):
            detector = CoiledSpringDetector(window_seconds=300)
            emitted = None
            for elapsed in range(0, 316, cadence):
                detector.update(
                    _tick(
                        start + timedelta(seconds=elapsed),
                        spot=24000.0 + (elapsed % 4),
                        call_iv=11.5 if elapsed >= 300 else 10.0,
                    )
                )
                signal, _reason = detector.evaluate_gamma_blast()
                emitted = signal or emitted
            outcomes.append(emitted)

        self.assertEqual(outcomes, ["BUY_CALL", "BUY_CALL"])

    def test_does_not_treat_many_fast_ticks_as_five_minutes(self) -> None:
        detector = CoiledSpringDetector(window_seconds=300)
        start = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        for elapsed in range(100):
            detector.update(
                _tick(
                    start + timedelta(milliseconds=elapsed),
                    call_iv=12 if elapsed == 99 else 10,
                )
            )

        signal, reason = detector.evaluate_gamma_blast()
        self.assertIsNone(signal)
        self.assertIn("incomplete", reason)

    def test_persistent_expansion_emits_once_during_cooldown(self) -> None:
        detector = CoiledSpringDetector(
            window_seconds=300,
            minimum_confirmations=2,
            cooldown_seconds=300,
        )
        start = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        emitted = []
        for elapsed in range(0, 361, 15):
            detector.update(
                _tick(
                    start + timedelta(seconds=elapsed),
                    call_iv=11.5 if elapsed >= 300 else 10,
                )
            )
            signal, _reason = detector.evaluate_gamma_blast()
            if signal is not None:
                emitted.append(signal)

        self.assertEqual(emitted, ["BUY_CALL"])

    def test_rejects_low_premium_wide_spread_iv_sensor(self) -> None:
        detector = CoiledSpringDetector(window_seconds=300)
        start = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        reason = ""
        for elapsed in range(0, 316, 15):
            detector.update(
                _tick(
                    start + timedelta(seconds=elapsed),
                    call_iv=20 if elapsed >= 300 else 10,
                    call_mid=0.075,
                    call_spread=0.667,
                )
            )
            signal, reason = detector.evaluate_gamma_blast()
            self.assertIsNone(signal)

        self.assertIn("midpoint", reason)

    def test_rejects_sensor_contract_change_inside_window(self) -> None:
        detector = CoiledSpringDetector(window_seconds=300)
        start = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        reason = ""
        for elapsed in range(0, 316, 15):
            detector.update(
                _tick(
                    start + timedelta(seconds=elapsed),
                    call_iv=12 if elapsed >= 300 else 10,
                    call_token="CALL-B" if elapsed >= 300 else "CALL-A",
                )
            )
            signal, reason = detector.evaluate_gamma_blast()
            self.assertIsNone(signal)

        self.assertIn("changed inside the window", reason)


def _tick(
    captured_at: datetime,
    *,
    spot: float = 24000,
    call_iv: float = 10,
    put_iv: float = 10,
    call_mid: float = 10,
    put_mid: float = 10,
    call_spread: float = 0.01,
    put_spread: float = 0.01,
    call_token: str = "CALL-A",
    put_token: str = "PUT-A",
) -> TickSnapshot:
    return TickSnapshot(
        captured_at=captured_at,
        spot_price=spot,
        atm_iv=14,
        iv_rank=20,
        otm_call_iv=call_iv,
        otm_put_iv=put_iv,
        atm_call_delta=0.5,
        otm_call_token=call_token,
        otm_put_token=put_token,
        otm_call_mid=call_mid,
        otm_put_mid=put_mid,
        otm_call_spread_ratio=call_spread,
        otm_put_spread_ratio=put_spread,
    )


if __name__ == "__main__":
    unittest.main()
