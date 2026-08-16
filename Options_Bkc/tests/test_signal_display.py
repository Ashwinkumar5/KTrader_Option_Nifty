from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.models import AnalyticsSnapshot, OptionChainSnapshot, OptionType
from app.signals.display import ActiveStrategyTarget, format_signal_line


class SignalDisplayTests(unittest.TestCase):
    def test_formats_timestamp_target_and_active_gamma_target(self) -> None:
        captured_at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        line = format_signal_line(
            snapshot=OptionChainSnapshot(
                underlying="NIFTY",
                expiry=date(2026, 7, 30),
                spot_price=Decimal("24050"),
                atm_strike=Decimal("24050"),
                captured_at=captured_at,
                quotes=(),
            ),
            analytics=AnalyticsSnapshot(
                underlying="NIFTY",
                captured_at=captured_at,
                atm_strike=Decimal("24050"),
                signal="NEUTRAL",
                target_strike=Decimal("24050"),
                target_option_type=OptionType.CALL,
                target_ltp=Decimal("134.9"),
                target_delta=Decimal("0.51"),
                strategy_source="GAMMA",
            ),
            active_gamma_target=ActiveStrategyTarget(
                source="GAMMA",
                side="BUY_CALL",
                strike=Decimal("24050"),
                option_type=OptionType.CALL,
                ltp=Decimal("135.2"),
                delta=Decimal("0.51"),
                captured_at=captured_at,
            ),
        )

        self.assertIn("2026-07-22T09:30:00+00:00 [SIGNAL]", line)
        self.assertIn("TARGET_STRIKE=24050", line)
        self.assertIn("TARGET_LTP=134.9", line)
        self.assertIn("GAMMA_ACTIVE_STRIKE=24050", line)
        self.assertIn("GAMMA_ACTIVE_LTP=135.2", line)


if __name__ == "__main__":
    unittest.main()
