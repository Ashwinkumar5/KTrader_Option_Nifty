from __future__ import annotations

import unittest
from datetime import date, timedelta
from decimal import Decimal

from app.marketdata.reference_data import (
    calculate_previous_atr,
    extract_ltp,
    normalize_daily_candles,
)


class ReferenceDataTests(unittest.TestCase):
    def test_calculates_atr_from_completed_daily_candles_only(self) -> None:
        market_date = date(2026, 7, 29)
        rows = []
        start = market_date - timedelta(days=21)
        for index in range(22):
            session_date = start + timedelta(days=index)
            rows.append(
                [
                    f"{session_date.isoformat()}T09:15:00+05:30",
                    "100",
                    "106",
                    "96",
                    "101",
                    1000,
                ]
            )

        candles = normalize_daily_candles(
            {"data": rows},
            before_date=market_date,
        )
        atr = calculate_previous_atr(candles)

        self.assertEqual(len(candles), 21)
        self.assertEqual(atr, Decimal("10.00"))

    def test_returns_none_when_atr_history_is_incomplete(self) -> None:
        candles = normalize_daily_candles(
            {
                "data": [
                    ["2026-07-28T09:15:00+05:30", 100, 106, 96, 101]
                ]
            },
            before_date=date(2026, 7, 29),
        )

        self.assertIsNone(calculate_previous_atr(candles))

    def test_extracts_nested_india_vix_ltp(self) -> None:
        self.assertEqual(
            extract_ltp({"data": {"ltp": "13.45"}}),
            Decimal("13.45"),
        )


if __name__ == "__main__":
    unittest.main()
