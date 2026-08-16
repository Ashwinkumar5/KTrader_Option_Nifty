from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from time import monotonic, sleep

from ktrader_simulator.config import load_settings
from ktrader_simulator.controller import (
    ControllerEvent,
    PortfolioEvent,
    SnapshotEvent,
    TradingController,
)
from ktrader_simulator.domain.models import OptionType
from ktrader_simulator.intake.ipc import BotOrderSignal, send_buy_event
from ktrader_simulator.trading.models import ExecutionEventKind, OrderSource
from tests.test_instruments import _index_rows
from tests.test_market_snapshots import FakeReadOnlyBroker


def test_offline_bot_event_entry_and_exit(tmp_path: Path) -> None:
    """Exercise bot IPC, market-paper entry, portfolio, and exit offline."""

    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    ipc_port = _free_tcp_port()
    settings = load_settings(
        simulator_root=simulator_root,
        environ={
            "BROKER_ORDER_EXECUTION_ENABLED": "false",
            "KTRADER_BOT_ORDER_INTAKE_ENABLED": "true",
            "KTRADER_BOT_IPC_PORT": str(ipc_port),
            "KTRADER_QUOTE_REFRESH_MS": "250",
            "KTRADER_TRADE_LEDGER_FSYNC": "false",
        },
    )
    broker = FakeReadOnlyBroker(
        _index_rows(
            "NIFTY",
            spot_exchange="NSE",
            option_exchange="NFO",
            atm=22500,
            interval=50,
        )
    )
    controller = TradingController(settings, broker_factory=lambda _settings: broker)
    controller.start()
    try:
        snapshot_event = _wait_for(
            controller,
            lambda event: isinstance(event, SnapshotEvent),
        )
        assert isinstance(snapshot_event, SnapshotEvent)
        row = snapshot_event.snapshot.rows[2]
        call = row.call

        signal_id = "bot-offline-correlation-test"
        reply = asyncio.run(
            send_buy_event(
                endpoint="KTraderUI",
                host="127.0.0.1",
                port=ipc_port,
                signal=BotOrderSignal(
                    underlying="NIFTY",
                    strike=call.strike,
                    option_type=OptionType.CALL,
                    captured_at=datetime.now(UTC),
                    signal_id=signal_id,
                    profile="cross_strike_confirmed_impulse_research",
                    strategy="OPTION_CHAIN_IMPULSE",
                ),
            )
        )
        assert reply == "OK"

        opened_event = _wait_for(
            controller,
            lambda event: isinstance(event, PortfolioEvent) and len(event.portfolio.positions) == 1,
        )
        assert isinstance(opened_event, PortfolioEvent)
        position = opened_event.portfolio.positions[0]
        assert position.order_id == signal_id
        assert row.call_quote is not None
        assert position.entry_price == row.call_quote.ask
        assert position.source == OrderSource.BOT
        assert any(
            execution.kind == ExecutionEventKind.POSITION_OPENED
            for execution in opened_event.executions
        )

        assert controller.exit_position(position.position_id)
        exited_event = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(
                    execution.kind == ExecutionEventKind.POSITION_EXITED
                    for execution in event.executions
                )
            ),
        )
        assert isinstance(exited_event, PortfolioEvent)
        assert exited_event.portfolio.positions == ()
        assert exited_event.portfolio.pending_orders == ()
    finally:
        controller.stop()


def _free_tcp_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for(
    controller: TradingController,
    predicate: Callable[[ControllerEvent], bool],
    *,
    timeout: float = 3.0,
) -> ControllerEvent:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        for event in controller.drain_events():
            if predicate(event):
                return event
        sleep(0.01)
    raise AssertionError("timed out waiting for controller event")
