from __future__ import annotations

import json
import unittest
from pathlib import Path
from uuid import uuid4

from dummy_broker_replay.validate_tape import (
    _validate_complete_frame,
    validate_tape,
)


class ReplayTapeValidatorTests(unittest.TestCase):
    def test_accepts_research_ready_schema_v4_tape(self) -> None:
        session = "v4-session"
        token = lambda value, symbol, kind: {
            "exchange": "NFO" if kind != "index" else "NSE",
            "token": value,
            "symbol": "NIFTY",
            "trading_symbol": symbol,
            "kind": kind,
        }
        call = {
            "underlying": "NIFTY",
            "expiry": "2026-07-30",
            "strike": "24100",
            "option_type": "CE",
            "token": token("CE", "NIFTY30JUL2624100CE", "option"),
            "lot_size": 75,
        }
        put = {
            **call,
            "option_type": "PE",
            "token": token("PE", "NIFTY30JUL2624100PE", "option"),
        }
        future = {
            "underlying": "NIFTY",
            "expiry": "2026-07-30",
            "token": token("FUT", "NIFTY30JUL26FUT", "future"),
            "lot_size": 75,
        }
        market = {
            "open_price": "24050",
            "previous_close": "24000",
            "spot_observed_at": "2026-07-22T04:15:00+00:00",
            "future_observed_at": "2026-07-22T04:15:00+00:00",
            "future_price": "24120",
            "future_volume": 1000,
            "future_oi": 2000,
        }
        quotes = [
            {
                "contract": contract,
                "ltp": "100",
                "bid": "99",
                "ask": "101",
                "oi": 1000,
                "volume": 500,
                "greeks": {
                    "implied_volatility": "15",
                    "delta": "0.5",
                },
            }
            for contract in (call, put)
        ]
        records = [
            {
                "schema_version": 4,
                "record_type": "session_manifest",
                "sequence": 1,
                "session_id": session,
                "effective_settings": {
                    "option_window_each_side": 0,
                    "option_greeks_enabled": True,
                },
                "capture_capabilities": {},
            },
            {
                "schema_version": 4,
                "record_type": "instrument_master",
                "sequence": 2,
                "session_id": session,
                "spot_tokens": [token("SPOT", "NIFTY", "index")],
                "option_contracts": [call, put],
                "future_contracts": [future],
            },
            {
                "schema_version": 4,
                "record_type": "subscription_change",
                "sequence": 3,
                "session_id": session,
            },
            {
                "schema_version": 4,
                "record_type": "market_event",
                "sequence": 4,
                "session_id": session,
                "event_role": "spot",
            },
            {
                "schema_version": 4,
                "record_type": "market_event",
                "sequence": 5,
                "session_id": session,
                "event_role": "future",
            },
            {
                "schema_version": 4,
                "record_type": "market_event",
                "sequence": 6,
                "session_id": session,
                "event_role": "option",
            },
            {
                "schema_version": 4,
                "record_type": "gate_decision",
                "sequence": 7,
                "session_id": session,
                "frame": {
                    "data_quality": {"status": "VALID"},
                    "research_quality": {"status": "RESEARCH_READY"},
                    "market_context": market,
                    "window": {
                        "expected_contract_count": 2,
                        "selected_contract_count": 2,
                        "quote_tokens": ["CE", "PE"],
                        "greeks_tokens": ["CE", "PE"],
                        "valid_bid_ask_tokens": ["CE", "PE"],
                        "oi_volume_tokens": ["CE", "PE"],
                        "usable_iv_delta_tokens": ["CE", "PE"],
                    },
                },
                "snapshot": {"quotes": quotes},
                "analytics": {
                    "directional_evidence": [],
                    "opening_context": {},
                    "expected_move_context": {},
                    "premium_responses": [],
                    "momentum_exhaustion": {},
                    "strategy_candidates": [],
                },
            },
            {
                "schema_version": 4,
                "record_type": "session_end",
                "sequence": 8,
                "session_id": session,
            },
        ]
        temporary_root = Path.cwd() / ".test-tmp"
        temporary_root.mkdir(exist_ok=True)
        path = temporary_root / f"capture-{uuid4().hex}.jsonl"
        try:
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            summary, issues = validate_tape(path)

            abandoned_session = {
                "schema_version": 4,
                "record_type": "session_manifest",
                "sequence": 1,
                "session_id": "abandoned-startup",
                "effective_settings": {
                    "option_window_each_side": 0,
                    "option_greeks_enabled": True,
                },
                "capture_capabilities": {},
            }
            path.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in (abandoned_session, *records)
                ),
                encoding="utf-8",
            )
            recovered_summary, recovered_issues = validate_tape(path)

            unfinished_session_records = (
                {
                    **abandoned_session,
                    "session_id": "unfinished-with-data",
                },
                {
                    "schema_version": 4,
                    "record_type": "market_event",
                    "sequence": 2,
                    "session_id": "unfinished-with-data",
                    "event_role": "option",
                },
            )
            path.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in (*records, *unfinished_session_records)
                ),
                encoding="utf-8",
            )
            _, unfinished_issues = validate_tape(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(issues, [])
        self.assertTrue(summary["replay_ready"])
        self.assertEqual(
            summary["research_statuses"],
            {"RESEARCH_READY": 1},
        )
        self.assertEqual(recovered_issues, [])
        self.assertTrue(recovered_summary["replay_ready"])
        self.assertEqual(
            recovered_summary["abandoned_manifest_only_sessions"],
            ["abandoned-startup"],
        )
        self.assertTrue(
            any(
                "unfinished-with-data" in issue
                for issue in unfinished_issues
            )
        )

    def test_accepts_complete_schema_v3_fixture(self) -> None:
        path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v3.jsonl"
        )
        summary, issues = validate_tape(path)
        self.assertEqual(issues, [])
        self.assertEqual(summary["frame_statuses"], {"VALID": 1})
        self.assertEqual(summary["record_counts"]["market_event_spot"], 1)
        self.assertEqual(summary["record_counts"]["market_event_option"], 1)
        self.assertEqual(len(summary["sha256"]), 64)

    def test_complete_frame_count_is_configuration_driven(self) -> None:
        issues: list[str] = []
        tokens = [str(index) for index in range(10)]
        _validate_complete_frame(
            {
                "expected_contract_count": 10,
                "selected_contract_count": 10,
                "quote_tokens": tokens,
                "greeks_tokens": tokens,
            },
            1,
            issues,
            expected_contract_count=10,
            require_greeks=True,
        )
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
