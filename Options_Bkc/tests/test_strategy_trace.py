from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from app.research.strategy_trace import analyze_strategy, catalog_traces


class StrategyTraceTests(unittest.TestCase):
    def test_catalog_and_ten_minute_ask_to_bid_outcome(self) -> None:
        temporary_root = Path.cwd() / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        path = temporary_root / (
            f"analytics_engine_trace_test_{uuid4().hex}.jsonl"
        )
        try:
            records = [
                {
                    "record_type": "analytics_trace_manifest",
                    "session_id": "session-1",
                    "enabled_strategies": ["GAMMA_EXPANSION"],
                },
                _frame(
                    "2026-07-28T10:00:00+05:30",
                    bid="99",
                    ask="100",
                    status="SELECTED",
                    side="BUY_CALL",
                ),
                _frame("2026-07-28T10:05:00+05:30", bid="110", ask="111"),
                _frame("2026-07-28T10:10:00+05:30", bid="105", ask="106"),
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )

            catalog = catalog_traces(path)
            outcomes = analyze_strategy(
                catalog.files,
                strategy="GAMMA_EXPANSION",
            )
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(catalog.enabled_strategies, ("GAMMA_EXPANSION",))
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(str(outcomes[0].return_percent), "5.00")
        self.assertEqual(str(outcomes[0].maximum_gain_percent), "10.00")
        self.assertTrue(outcomes[0].complete_horizon)

    def test_old_gamma_trace_infers_side_and_atm_contract(self) -> None:
        temporary_root = Path.cwd() / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        path = temporary_root / (
            f"analytics_engine_trace_old_{uuid4().hex}.jsonl"
        )
        try:
            records = [
                {
                    "record_type": "analytics_trace_manifest",
                    "session_id": "old",
                    "enabled_strategies": ["GAMMA_EXPANSION"],
                },
                _frame(
                    "2026-07-28T10:00:00+05:30",
                    bid="49",
                    ask="50",
                    status="SELECTED",
                    side=None,
                    reason="GAMMA PUT EXPANSION: test",
                    option_type="PE",
                ),
                _frame(
                    "2026-07-28T10:10:00+05:30",
                    bid="55",
                    ask="56",
                    option_type="PE",
                ),
            ]
            path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            outcome = analyze_strategy(
                (path,),
                strategy="GAMMA_EXPANSION",
            )[0]
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(outcome.side, "BUY_PUT")
        self.assertEqual(outcome.token, "PE1")
        self.assertEqual(str(outcome.return_percent), "10.00")


def _frame(
    captured_at: str,
    *,
    bid: str,
    ask: str,
    status: str = "NO_CANDIDATE",
    side: str | None = None,
    reason: str = "",
    option_type: str = "CE",
) -> dict[str, object]:
    token = "CE1" if option_type == "CE" else "PE1"
    contract = (
        {
            "token": token,
            "trading_symbol": f"NIFTY24000{option_type}",
            "entry_ask": ask,
        }
        if side is not None
        else None
    )
    return {
        "record_type": "analytics_engine_trace",
        "captured_at_ist": captured_at,
        "raw_signal": side or "NEUTRAL",
        "qualified": False,
        "gate_reason": "shadow",
        "market_frame": {
            "atm_strike": "24000",
            "option_quotes": [
                {
                    "token": token,
                    "trading_symbol": f"NIFTY24000{option_type}",
                    "strike": "24000",
                    "option_type": option_type,
                    "bid": bid,
                    "ask": ask,
                }
            ],
        },
        "strategies": [
            {
                "strategy": "GAMMA_EXPANSION",
                "status": status,
                "proposed_side": side,
                "reason": reason,
                "research_contract": contract,
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
