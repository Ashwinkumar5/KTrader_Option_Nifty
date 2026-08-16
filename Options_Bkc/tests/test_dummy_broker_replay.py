from __future__ import annotations

import asyncio
import unittest
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.broker.angleone.instruments import build_instrument_master
from app.domain.models import MarketTick
from app.optionchain.state import OptionChainState
from dummy_broker_replay.dummy_broker import RecordedBrokerClient, quote_rows
from dummy_broker_replay.dummy_broker_feed import RecordedMarketDataFeed
from dummy_broker_replay.reader import RecordedSessionReader
from dummy_broker_replay.runner import (
    _format_strong_signal_summary,
    _populate_snapshot,
    _record_paper_exits,
    _update_strong_signal_exits,
)
from dummy_broker_replay.serde import parse_snapshot
from app.execution.paper import PaperFill


def _contract(token: str, strike: str, option_type: str) -> dict[str, object]:
    return {
        "underlying": "NIFTY",
        "expiry": "2026-07-28",
        "strike": strike,
        "option_type": option_type,
        "token": {
            "exchange": "NFO",
            "token": token,
            "symbol": "NIFTY",
            "trading_symbol": f"NIFTY28JUL26{strike}{option_type}",
            "kind": "option",
        },
        "lot_size": 75,
    }


def _quote(token: str, strike: str, option_type: str) -> dict[str, object]:
    contract = _contract(token, strike, option_type)
    return {
        "contract": contract,
        "ltp": "100",
        "open_price": "95",
        "high_price": "105",
        "low_price": "90",
        "close_price": "96",
        "oi": 10000,
        "oi_change": 100,
        "oi_change_percent": "1",
        "volume": 20000,
        "bid": "99.5",
        "ask": "100.5",
        "greeks": {
            "contract": contract,
            "captured_at": "2026-07-22T03:45:33+00:00",
            "implied_volatility": "12",
            "delta": "0.5" if option_type == "CE" else "-0.5",
            "gamma": "0.01",
            "theta": "-1",
            "vega": "2",
            "source": "recorded",
        },
    }


def _frame_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "gate_decision",
        "captured_at": "2026-07-22T03:45:33+00:00",
        "snapshot": {
            "underlying": "NIFTY",
            "expiry": "2026-07-28",
            "spot_price": "24000",
            "atm_strike": "24000",
            "captured_at": "2026-07-22T03:45:33+00:00",
            "quotes": [
                _quote("CE1", "24000", "CE"),
                _quote("PE1", "24000", "PE"),
            ],
            "reference": None,
        },
        "decision": {"qualified": False},
    }


def _market_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "market_event",
        "captured_at": "2026-07-22T03:45:32+00:00",
        "tick": {
            "token": _contract("CE1", "24000", "CE")["token"],
            "exchange_timestamp": "2026-07-22T03:45:32+00:00",
            "received_at": "2026-07-22T03:45:32+00:00",
            "ltp": "100",
            "quality": "live",
            "raw": {
                "last_traded_price": 10000,
                "exchange_timestamp": 1784691932000,
                "best_5_buy_data": [
                    {"price": 9950, "quantity": 100, "orders": 2}
                ],
                "best_5_sell_data": [
                    {"price": 10050, "quantity": 100, "orders": 2}
                ],
            },
        },
        "features": {},
        "microstructure_signal": None,
    }


class RecordedSessionReaderTests(unittest.TestCase):
    def test_schema_v4_audit_preserves_spot_future_and_capture_window(self) -> None:
        path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "broker_replay_tape_v4.jsonl"
        )

        audit = RecordedSessionReader(path).audit()

        self.assertEqual(audit.market_spot_events, 1)
        self.assertEqual(audit.market_future_events, 1)
        self.assertEqual(len(audit.spot_tokens), 1)
        self.assertEqual(len(audit.future_contracts), 1)
        self.assertEqual(
            audit.capture_configuration["option_window_each_side"],
            0,
        )

    def test_audits_and_event_time_orders_records(self) -> None:
        path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "dummy_broker_capture.jsonl"
        )
        reader = RecordedSessionReader(path)
        audit = reader.audit()
        self.assertEqual(audit.market_events, 1)
        self.assertEqual(audit.gate_frames, 1)
        self.assertEqual(audit.timestamp_regressions, 1)
        records = list(reader.records(mode="event-time"))
        self.assertEqual(records[0][1]["record_type"], "market_event")
        self.assertEqual(records[1][1]["record_type"], "gate_decision")


class RecordedBrokerTests(unittest.TestCase):
    def test_reconstructs_master_quotes_greeks_and_feed_tick(self) -> None:
        frame = _frame_record()
        snapshot_data = frame["snapshot"]
        assert isinstance(snapshot_data, dict)
        contracts = tuple(
            quote["contract"]
            for quote in snapshot_data["quotes"]
            if isinstance(quote, dict)
        )
        broker = RecordedBrokerClient(contracts)
        broker.set_frame(snapshot_data)

        async def exercise():
            rows = await broker.instrument_master()
            master = build_instrument_master(rows, underlyings=("NIFTY",))
            quotes = await broker.market_quote(
                mode="FULL",
                exchange_tokens={"NFO": ["CE1", "PE1"]},
            )
            greeks = await broker.option_greeks({})
            return master, quotes, greeks

        master, quotes, greeks = asyncio.run(exercise())
        self.assertEqual(len(master.options), 2)
        self.assertIn("NIFTY", master.spot_tokens)
        self.assertEqual(len(quote_rows(quotes)), 2)
        self.assertEqual(len(greeks["data"]), 2)

        feed = RecordedMarketDataFeed()
        tick = feed.decode_market_event(_market_record())
        self.assertIsNotNone(tick)
        self.assertEqual(str(tick.ltp), "100")
        self.assertTrue(tick.raw.get("best_5_buy_data"))
        snapshot = parse_snapshot(snapshot_data)
        self.assertEqual(len(snapshot.quotes), 2)

    def test_frame_quotes_override_newer_websocket_state_during_replay(
        self,
    ) -> None:
        frame = _frame_record()
        snapshot_data = frame["snapshot"]
        assert isinstance(snapshot_data, dict)
        contracts = tuple(
            quote["contract"]
            for quote in snapshot_data["quotes"]
            if isinstance(quote, dict)
        )
        broker = RecordedBrokerClient(contracts)
        broker.set_frame(snapshot_data)

        async def exercise():
            rows = await broker.instrument_master()
            master = build_instrument_master(rows, underlyings=("NIFTY",))
            token_lookup = {
                contract.token.token: contract.token
                for contract in master.options
            }
            shared_state = OptionChainState(master=master)
            frame_time = datetime.fromisoformat(
                str(snapshot_data["captured_at"])
            )
            for contract in master.options:
                shared_state.update_tick(
                    MarketTick(
                        token=contract.token,
                        exchange_timestamp=frame_time + timedelta(hours=5),
                        received_at=frame_time + timedelta(hours=5),
                        ltp=Decimal("101"),
                        bid=None,
                        ask=None,
                    )
                )
            source_snapshot = parse_snapshot(snapshot_data)
            return await _populate_snapshot(
                broker=broker,
                state=shared_state,
                token_lookup=token_lookup,
                master=master,
                source_snapshot=source_snapshot,
                each_side=0,
            )

        populated = asyncio.run(exercise())

        self.assertEqual(
            [(quote.bid, quote.ask) for quote in populated.quotes],
            [
                (Decimal("99.5"), Decimal("100.5")),
                (Decimal("99.5"), Decimal("100.5")),
            ],
        )


class ReplayAccountingTests(unittest.TestCase):
    def test_formats_compact_strong_signal_position_summary(self) -> None:
        signal_time = datetime(2026, 7, 31, 7, 49, tzinfo=UTC)
        result = SimpleNamespace(
            strong_signals_count=1,
            strong_signal_details=(
                {
                    "signal_time": signal_time,
                    "strategy": "DERIVATIVES_QUANT",
                    "side": "BUY_CALL",
                    "strike": Decimal("24400"),
                    "option_type": "CE",
                    "entry_price": Decimal("74.3"),
                    "stop_percent": Decimal("5"),
                    "target_percent": Decimal("10"),
                    "horizon_minutes": 15,
                    "outcome": "TIME_EXIT",
                    "exit_time": signal_time,
                    "exit_price": Decimal("76.0"),
                    "gain_percent": Decimal("2.2880"),
                },
            ),
        )

        summary = "\n".join(_format_strong_signal_summary(result))

        self.assertIn("Strong signals identified: 1", summary)
        self.assertIn("side=BUY_CALL", summary)
        self.assertIn("contract=24400 CE", summary)
        self.assertIn("SL=5%", summary)
        self.assertIn("target=10%", summary)
        self.assertIn("horizon=15m", summary)
        self.assertIn("outcome=TIME_EXIT", summary)
        self.assertIn("gain=2.2880%", summary)

    def test_strong_signal_summary_uses_existing_paper_exit(self) -> None:
        captured_at = datetime.now(UTC)
        details: list[dict[str, object]] = [
            {
                "entry_price": Decimal("100"),
                "outcome": "OPEN",
                "exit_time": None,
                "exit_price": None,
                "gain_percent": None,
            }
        ]
        active_by_token = {"CE1": 0}

        _update_strong_signal_exits(
            (
                PaperFill(
                    token="CE1",
                    action="SELL",
                    price=Decimal("110"),
                    quantity=75,
                    captured_at=captured_at,
                    reason="target",
                    realized_pnl=Decimal("750"),
                    maximum_favorable_excursion_percent=Decimal("12"),
                    maximum_adverse_excursion_percent=Decimal("-3"),
                ),
            ),
            details=details,
            active_by_token=active_by_token,
        )

        self.assertEqual(details[0]["outcome"], "TARGET")
        self.assertEqual(details[0]["exit_time"], captured_at)
        self.assertEqual(details[0]["exit_price"], Decimal("110"))
        self.assertEqual(details[0]["gain_percent"], Decimal("10.0000"))
        self.assertEqual(active_by_token, {})

    def test_round_trip_cost_is_applied_only_to_research_net_metrics(
        self,
    ) -> None:
        gross_returns: list[Decimal] = []
        net_returns: list[Decimal] = []
        mfe_returns: list[Decimal] = []
        mae_returns: list[Decimal] = []
        counters: Counter[str] = Counter()
        outcomes: dict[str, Counter[str]] = defaultdict(Counter)

        gross, net, cost = _record_paper_exits(
            (
                PaperFill(
                    token="CE1",
                    action="SELL",
                    price=Decimal("110"),
                    quantity=75,
                    captured_at=datetime.now(UTC),
                    reason="target",
                    realized_pnl=Decimal("750"),
                    maximum_favorable_excursion_percent=Decimal("12"),
                    maximum_adverse_excursion_percent=Decimal("-3"),
                ),
            ),
            entry_prices={"CE1": Decimal("100")},
            entry_strategies={"CE1": "DERIVATIVES_QUANT"},
            strategy_outcomes=outcomes,
            completed_trade_returns=gross_returns,
            net_completed_trade_returns=net_returns,
            completed_trade_mfe_percent=mfe_returns,
            completed_trade_mae_percent=mae_returns,
            counters=counters,
            round_trip_cost_percent=Decimal("0.20"),
        )

        self.assertEqual(gross, Decimal("10"))
        self.assertEqual(net, Decimal("9.80"))
        self.assertEqual(cost, Decimal("15"))
        self.assertEqual(gross_returns, [Decimal("10")])
        self.assertEqual(net_returns, [Decimal("9.80")])
        self.assertEqual(mfe_returns, [Decimal("12")])
        self.assertEqual(mae_returns, [Decimal("-3")])
        self.assertEqual(counters["target_exits"], 1)


if __name__ == "__main__":
    unittest.main()
