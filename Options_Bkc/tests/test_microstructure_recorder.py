from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.core.config import BrokerName, Settings
from app.domain.models import (
    AnalyticsSnapshot,
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionType,
    StrategyCheck,
    StrategyDiagnostic,
    StrategyFamily,
)
from app.execution.paper import PaperFill
from app.execution.risk import PositionPlan
from app.signals.gate import SignalGateDecision
from app.storage.microstructure_recorder import (
    JsonlMicrostructureRecorder,
    _analytics_trace_records,
)
from app.workers.market_data_worker import _replay_capture_settings


class ReplayTapeRecorderTests(unittest.TestCase):
    def test_gate_decision_records_simulator_signal_correlation(self) -> None:
        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-execution-signal-tape.jsonl",
            session_id="execution-session",
        )
        written: list[bytes] = []
        recorder._write = written.append  # type: ignore[method-assign]
        at = datetime(2026, 8, 9, 4, 15, tzinfo=UTC)

        async def record() -> None:
            await recorder.record_gate_decision(
                snapshot=OptionChainSnapshot(
                    underlying="NIFTY",
                    expiry=date(2026, 8, 13),
                    spot_price=Decimal("24500"),
                    atm_strike=Decimal("24500"),
                    captured_at=at,
                    quotes=(),
                ),
                decision=SignalGateDecision(
                    captured_at=at,
                    raw_signal="BUY_CALL",
                    published_signal="NEUTRAL",
                    qualified=True,
                    reason="shadow qualified",
                    microstructure_signal=None,
                    strong_signal="BUY_CALL",
                ),
                execution_signal={
                    "signal_id": "bot-correlation-test",
                    "profile": "cross_strike_confirmed_impulse_research",
                    "strategy": "OPTION_CHAIN_IMPULSE",
                    "side": "BUY_CALL",
                    "strike": Decimal("24500"),
                    "dispatch_status": "QUEUED",
                },
            )
            await recorder.finish(completed_at=at, processed_ticks=0)

        asyncio.run(record())
        records = [
            json.loads(line)
            for line in b"".join(written).decode("utf-8").splitlines()
        ]
        execution = records[0]["execution_signal"]
        self.assertEqual(execution["signal_id"], "bot-correlation-test")
        self.assertEqual(execution["dispatch_status"], "QUEUED")

    def test_records_paper_fill_and_unresolved_session_state(self) -> None:
        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-paper-fill-tape.jsonl",
            session_id="paper-session",
        )
        written: list[bytes] = []
        recorder._write = written.append  # type: ignore[method-assign]
        at = datetime(2026, 8, 3, 4, 15, tzinfo=UTC)
        plan = PositionPlan(
            token="PE1",
            entry_price=Decimal("58.05"),
            stop_price=Decimal("55.15"),
            target_price=Decimal("63.85"),
            lot_size=75,
            lots=1,
            quantity=75,
            capital_at_risk=Decimal("217.50"),
            gross_exposure=Decimal("4353.75"),
            option_type=OptionType.PUT,
        )
        fill = PaperFill(
            token="PE1",
            action="BUY",
            price=plan.entry_price,
            quantity=plan.quantity,
            captured_at=at,
            reason="qualified strong signal",
        )

        async def record() -> None:
            await recorder.record_paper_fill(
                fill=fill,
                profile="intraday_directional_premium_momentum_research",
                underlying="NIFTY",
                strategy="DERIVATIVES_QUANT",
                side="BUY_PUT",
                strike=Decimal("24550"),
                option_type=OptionType.PUT,
                position_plan=plan,
                realized_pnl=Decimal("0"),
                open_positions=1,
                gross_exposure=plan.gross_exposure,
            )
            await recorder.finish(
                completed_at=at,
                processed_ticks=10,
                paper_state={"open_positions": 1},
            )

        asyncio.run(record())
        records = [
            json.loads(line)
            for line in b"".join(written).decode("utf-8").splitlines()
        ]
        self.assertEqual(records[0]["record_type"], "paper_fill")
        self.assertEqual(records[0]["fill"]["action"], "BUY")
        self.assertEqual(records[0]["strategy"], "DERIVATIVES_QUANT")
        self.assertEqual(records[1]["paper_state"]["open_positions"], 1)

    def test_batched_writer_preserves_all_records_without_per_tick_file_open(self) -> None:
        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-batched-tape.jsonl",
            session_id="batch-session",
            queue_capacity=64,
            batch_size=32,
        )
        written: list[bytes] = []
        recorder._write = written.append  # type: ignore[method-assign]
        now = datetime(2026, 7, 27, 3, 45, tzinfo=UTC)
        option = InstrumentToken(
            exchange=Exchange.NFO,
            token="option",
            symbol="NIFTY",
            trading_symbol="NIFTY30JUL2624100CE",
            kind=InstrumentKind.OPTION,
        )

        async def record() -> None:
            for _ in range(500):
                await recorder.record_market_event(
                    tick=MarketTick(
                        token=option,
                        exchange_timestamp=now,
                        received_at=now,
                        ltp=None,
                    ),
                    features=None,
                    signal=None,
                )
            await recorder.finish(
                completed_at=now,
                processed_ticks=500,
            )

        asyncio.run(record())
        records = b"".join(written).decode("utf-8").splitlines()
        self.assertEqual(len(records), 501)
        self.assertLess(len(written), 100)
        self.assertEqual(json.loads(records[-1])["record_type"], "session_end")

    def test_analytics_trace_is_disabled_by_default(self) -> None:
        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-no-trace-tape.jsonl",
            session_id="no-trace-session",
        )
        written: list[bytes] = []
        analytics_written: list[bytes] = []
        recorder._write = written.append  # type: ignore[method-assign]
        recorder._write_analytics_trace = (  # type: ignore[method-assign]
            analytics_written.append
        )
        now = datetime(2026, 8, 8, 3, 45, tzinfo=UTC)

        async def record() -> None:
            await recorder.record_session_manifest(
                started_at=now,
                effective_settings={},
                code_revision="test",
            )
            await recorder.finish(completed_at=now, processed_ticks=0)

        asyncio.run(record())
        records = [
            json.loads(line)
            for line in b"".join(written).decode("utf-8").splitlines()
        ]
        manifest = records[0]
        self.assertIsNone(manifest["analytics_trace_file"])
        self.assertFalse(
            manifest["capture_capabilities"]["analytics_trace"]
        )
        self.assertIsNone(recorder.analytics_trace_path)
        self.assertEqual(analytics_written, [])
        self.assertFalse(recorder.health_snapshot()["analytics_trace_enabled"])

    def test_manifest_and_spot_event_use_schema_v4_without_secrets(self) -> None:
        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-replay-tape.jsonl",
            session_id="test-session",
            analytics_trace_enabled=True,
        )
        written: list[bytes] = []
        analytics_written: list[bytes] = []
        recorder._write = written.append  # type: ignore[method-assign]
        recorder._write_analytics_trace = (  # type: ignore[method-assign]
            analytics_written.append
        )
        now = datetime(2026, 7, 27, 3, 45, tzinfo=UTC)
        spot = InstrumentToken(
            exchange=Exchange.NSE,
            token="99926000",
            symbol="NIFTY",
            trading_symbol="NIFTY",
            kind=InstrumentKind.INDEX,
        )

        async def record() -> None:
            await recorder.record_session_manifest(
                started_at=now,
                effective_settings={
                    "snapshot_interval_ms": 15000,
                    "strategy_configuration": {
                        "profile": {
                            "name": "derivatives_only",
                            "strategies": {
                                "DERIVATIVES_QUANT": {
                                    "enabled": True,
                                    "priority": 10,
                                },
                                "BREAKOUT_MOMENTUM": {
                                    "enabled": False,
                                    "priority": 20,
                                },
                            },
                        }
                    },
                },
                code_revision="test",
            )
            await recorder.record_market_event(
                tick=MarketTick(
                    token=spot,
                    exchange_timestamp=now,
                    received_at=now,
                    ltp=None,
                    raw={"token": "99926000"},
                ),
                features=None,
                signal=None,
            )
            await recorder.finish(
                completed_at=now,
                processed_ticks=1,
            )

        asyncio.run(record())
        records = [
            json.loads(line)
            for line in b"".join(written).decode("utf-8").splitlines()
        ]
        self.assertEqual(records[0]["schema_version"], 4)
        self.assertEqual(records[0]["record_type"], "session_manifest")
        self.assertEqual(records[0]["session_id"], "test-session")
        self.assertEqual(records[1]["event_role"], "spot")
        self.assertEqual(records[0]["captured_at_ist"], "2026-07-27T09:15:00+05:30")
        self.assertEqual(records[1]["exchange_timestamp_ist"], "2026-07-27T09:15:00+05:30")
        self.assertEqual(records[1]["received_at_ist"], "2026-07-27T09:15:00+05:30")
        self.assertIsNone(records[1]["features"])
        self.assertEqual(records[2]["record_type"], "session_end")
        self.assertEqual(len(analytics_written), 2)
        trace_manifest = json.loads(analytics_written[0])
        self.assertEqual(
            trace_manifest["record_type"],
            "analytics_trace_manifest",
        )
        self.assertEqual(trace_manifest["schema_version"], 2)
        self.assertEqual(
            trace_manifest["enabled_strategies"],
            ["DERIVATIVES_QUANT"],
        )
        self.assertEqual(
            json.loads(analytics_written[1])["record_type"],
            "analytics_trace_end",
        )
        self.assertEqual(
            recorder.analytics_trace_path.name,
            "analytics_engine_trace_20260727_091500_IST_testsess.jsonl",
        )
        self.assertEqual(
            [record["sequence"] for record in records],
            [1, 2, 3],
        )
        serialized = b"".join(written).decode("utf-8").lower()
        self.assertNotIn("angleone_password", serialized)
        self.assertNotIn("angleone_totp_secret", serialized)

    def test_capture_settings_are_explicitly_sanitized(self) -> None:
        settings = Settings(
            app_name="test",
            app_env="test",
            log_level="INFO",
            angleone_api_key="secret-api",
            angleone_client_code="secret-client",
            angleone_password="secret-password",
            angleone_totp_secret="secret-totp",
            angleone_instrument_master_url="https://example.invalid/master.json",
            angleone_instrument_master_path="",
            redis_url="redis://secret",
            database_url="postgresql://secret",
            local_storage_dir="data",
            default_underlyings=("NIFTY",),
            option_window_each_side=4,
            snapshot_interval_ms=15000,
            storage_backend="sqlite",
            broker_name=BrokerName.ANGLEONE,
            market_data_price_source="websocket_snap_quote",
            market_data_oi_source="websocket_snap_quote",
            market_data_greeks_source="option_greek",
            market_data_ws_mode="SNAP_QUOTE",
            option_greeks_enabled=True,
            broker_pcr_enabled=True,
            broker_oi_buildup_enabled=True,
        )
        captured = _replay_capture_settings(settings)
        serialized = json.dumps(captured)
        self.assertEqual(captured["snapshot_interval_ms"], 15000)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("angleone_api_key", captured)
        self.assertNotIn("redis_url", captured)
        self.assertNotIn("broker_config", captured)

    def test_analytics_trace_has_compact_strategy_and_feature_lines(self) -> None:
        now = datetime(2026, 7, 27, 3, 45, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=now,
            atm_strike=Decimal("24000"),
            strategy_diagnostics=(
                StrategyDiagnostic(
                    family=StrategyFamily.DERIVATIVES_QUANT,
                    status="NO_CANDIDATE",
                    reason="waiting for OI",
                    checks=(
                        StrategyCheck(
                            "direction_score",
                            False,
                            "score=0.20",
                            ">= 0.34",
                        ),
                    ),
                    feature_checks=(
                        StrategyCheck(
                            "iv_skew",
                            True,
                            "signed_contribution=+0.0300",
                            ">= +0.0150",
                            proposed_side="BUY_CALL",
                        ),
                        StrategyCheck(
                            "oi_migration",
                            False,
                            "signed_contribution=-0.0100",
                            ">= +0.0150",
                            proposed_side="BUY_PUT",
                        ),
                    ),
                    proposed_side="BUY_CALL",
                ),
                StrategyDiagnostic(
                    family=StrategyFamily.BREAKOUT_MOMENTUM,
                    status="NO_CANDIDATE",
                    reason="disabled strategy diagnostic",
                    proposed_side="BUY_PUT",
                ),
            ),
        )
        decision = SignalGateDecision(
            captured_at=now,
            raw_signal="NEUTRAL",
            published_signal="NEUTRAL",
            qualified=False,
            reason="candidate is not directional",
            microstructure_signal=None,
        )

        traces = _analytics_trace_records(
            analytics=analytics,
            decision=decision,
            enabled_strategies=frozenset(
                {"DERIVATIVES_QUANT", "GAMMA_EXPANSION"}
            ),
            session_id="trace-session",
            source_sequence=42,
        )

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["schema_version"], 2)
        self.assertEqual(
            traces[0]["record_type"],
            "strategy_feature_signals",
        )
        self.assertEqual(
            traces[0]["signals"],
            "strategy=GAMMA_EXPANSION;proposed_signal=null | "
            "strategy=DERIVATIVES_QUANT;proposed_signal=BUY_CALL | "
            "feature=iv_skew;proposed_signal=BUY_CALL | "
            "feature=oi_migration;proposed_signal=BUY_PUT",
        )
        self.assertNotIn("status", traces[0])
        self.assertNotIn("reason", traces[0])
        self.assertNotIn("published_signal", traces[0])

        recorder = JsonlMicrostructureRecorder(
            Path("tests") / "unused-compact-trace-tape.jsonl",
            session_id="compact-trace",
            analytics_trace_enabled=True,
        )
        recorder._analytics_trace_path = (  # type: ignore[attr-defined]
            Path("tests") / "unused-compact-trace.jsonl"
        )
        writes: list[bytes] = []
        recorder._write_analytics_trace = (  # type: ignore[method-assign]
            writes.append
        )
        asyncio.run(recorder._append_analytics_trace_batch(traces))

        self.assertEqual(len(writes), 1)
        serialized = writes[0].decode("utf-8").splitlines()
        self.assertEqual(len(serialized), 1)
        parsed = [json.loads(line) for line in serialized]
        self.assertEqual(
            [record["trace_sequence"] for record in parsed],
            [1],
        )


if __name__ == "__main__":
    unittest.main()
