from __future__ import annotations

import unittest
from decimal import Decimal

from app.signals.pcr import BUY_CALL, BUY_PUT, NEUTRAL, pcr_signal


class PCRSignalTests(unittest.TestCase):
    def test_pcr_signal_uses_configurable_thresholds(self) -> None:
        self.assertEqual(
            pcr_signal(
                Decimal("1.6"),
                bullish_threshold=Decimal("1.5"),
                bearish_threshold=Decimal("0.7"),
            )[0],
            BUY_CALL,
        )
        self.assertEqual(
            pcr_signal(
                Decimal("0.6"),
                bullish_threshold=Decimal("1.5"),
                bearish_threshold=Decimal("0.7"),
            )[0],
            BUY_PUT,
        )
        self.assertEqual(
            pcr_signal(
                Decimal("1.0"),
                bullish_threshold=Decimal("1.5"),
                bearish_threshold=Decimal("0.7"),
            )[0],
            NEUTRAL,
        )

    def test_structural_mode_uses_pcr_as_context_only(self) -> None:
        signal, reason = pcr_signal(
            Decimal("1.8"),
            spot_price=Decimal("24750"),
            support_level=Decimal("24700"),
            resistance_level=Decimal("24850"),
            bullish_threshold=Decimal("1.4"),
            bearish_threshold=Decimal("0.7"),
        )

        self.assertEqual(signal, NEUTRAL)
        self.assertIn("confirmation only", reason)


if __name__ == "__main__":
    unittest.main()
