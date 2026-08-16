from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from socket import AF_INET, SOCK_STREAM, socket
from threading import Event
from time import monotonic, sleep

from ktrader_simulator.broker.protocols import LiveOrderRouter
from ktrader_simulator.config import Settings, load_settings
from ktrader_simulator.controller import (
    ConnectionStatus,
    ControllerEvent,
    NoticeEvent,
    PortfolioEvent,
    SnapshotEvent,
    StatusEvent,
    TradingController,
    _coalesce_controller_events,
)
from ktrader_simulator.domain.models import Instrument, MarketSnapshot, OptionType, Quote
from ktrader_simulator.intake.ipc import BotOrderSignal, send_buy_event
from ktrader_simulator.trading.models import (
    ExecutionEvent,
    ExecutionEventKind,
    OrderRequest,
    OrderSource,
    OrderType,
    PortfolioSnapshot,
    Position,
    RiskParameters,
)
from tests.test_instruments import _index_rows
from tests.test_market_snapshots import FakeReadOnlyBroker


def _free_tcp_port() -> int:
    with socket(AF_INET, SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class GateableReadOnlyBroker(FakeReadOnlyBroker):
    def __init__(self, rows: list[dict[str, object]]) -> None:
        super().__init__(rows)
        self._block_quotes = False
        self.quote_started = Event()
        self.release_quotes = Event()

    def begin_quote_block(self) -> None:
        self.quote_started.clear()
        self.release_quotes.clear()
        self._block_quotes = True

    async def quotes(
        self,
        instruments: tuple[Instrument, ...],
    ) -> Mapping[str, Quote]:
        if self._block_quotes:
            self.quote_started.set()
            while not self.release_quotes.is_set():
                await asyncio.sleep(0.005)
        return await super().quotes(instruments)


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


def _snapshot_from(event: ControllerEvent) -> MarketSnapshot:
    if not isinstance(event, SnapshotEvent):
        raise AssertionError("expected SnapshotEvent")
    return event.snapshot


def test_controller_events_keep_only_latest_ui_state_and_preserve_execution() -> None:
    now = datetime.now(UTC)
    portfolio = PortfolioSnapshot(
        starting_balance=Decimal("100000"),
        cash_balance=Decimal("100000"),
        realized_pnl=Decimal("0"),
        positions=(),
        pending_orders=(),
        version=1,
    )
    execution = ExecutionEvent(
        kind=ExecutionEventKind.ORDER_CANCELLED,
        order_id="cancelled-order",
        message="Pending order cancelled",
        captured_at=now,
    )

    events = _coalesce_controller_events(
        (
            StatusEvent(ConnectionStatus.CONNECTING),
            PortfolioEvent.from_portfolio(portfolio, executions=(execution,)),
            StatusEvent(ConnectionStatus.CONNECTED),
            PortfolioEvent.from_portfolio(portfolio),
            NoticeEvent("old notice"),
            NoticeEvent("latest notice"),
        )
    )

    statuses = tuple(event for event in events if isinstance(event, StatusEvent))
    portfolios = tuple(event for event in events if isinstance(event, PortfolioEvent))
    notices = tuple(event for event in events if isinstance(event, NoticeEvent))
    assert statuses == (StatusEvent(ConnectionStatus.CONNECTED),)
    assert portfolios[0].executions == (execution,)
    assert notices == (NoticeEvent("latest notice"),)


def test_controller_processes_buy_mark_and_exit_off_gui_thread(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings: Settings = load_settings(
        simulator_root=simulator_root,
        environ={"KTRADER_QUOTE_REFRESH_MS": "250"},
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
        snapshot = _snapshot_from(snapshot_event)
        option = snapshot.rows[2].call
        request = OrderRequest(
            option=option,
            order_type=OrderType.MARKET,
            lots=1,
            limit_price=None,
            risk=RiskParameters(),
            request_id="controller-order",
            created_at=snapshot.captured_at,
        )

        assert controller.submit_order(request)
        opened_event = _wait_for(
            controller,
            lambda event: isinstance(event, PortfolioEvent) and bool(event.portfolio.positions),
        )
        assert isinstance(opened_event, PortfolioEvent)
        assert opened_event.portfolio.positions[0].position_id == "controller-order"
        assert opened_event.portfolio.cash_balance < settings.starting_balance

        assert controller.exit_position("controller-order")
        exited_event = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and bool(event.executions)
                and not event.portfolio.positions
            ),
        )
        assert isinstance(exited_event, PortfolioEvent)
        assert not exited_event.portfolio.positions
        assert exited_event.portfolio.realized_pnl < 0
    finally:
        controller.stop()


def test_controller_cancels_pending_order_and_releases_reservation(
    tmp_path: Path,
) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(
        simulator_root=simulator_root,
        environ={
            "KTRADER_QUOTE_REFRESH_MS": "60000",
            "KTRADER_TRADE_LEDGER_FSYNC": "false",
        },
    )
    broker = GateableReadOnlyBroker(
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
        snapshot = _snapshot_from(
            _wait_for(controller, lambda event: isinstance(event, SnapshotEvent))
        )
        request = OrderRequest(
            option=snapshot.rows[2].call,
            order_type=OrderType.LIMIT,
            lots=1,
            limit_price=Decimal("80"),
            risk=RiskParameters(),
            request_id="pending-controller-order",
            created_at=snapshot.captured_at,
        )
        assert controller.submit_order(request)
        pending = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent) and bool(event.portfolio.pending_orders)
            ),
        )
        assert isinstance(pending, PortfolioEvent)

        broker.begin_quote_block()
        controller.select_index("NIFTY")
        assert broker.quote_started.wait(timeout=1.0)
        assert controller.exit_position("pending-controller-order")
        cancelled = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(
                    item.kind == ExecutionEventKind.ORDER_CANCELLED for item in event.executions
                )
            ),
            timeout=0.5,
        )
        assert isinstance(cancelled, PortfolioEvent)
        assert not cancelled.portfolio.pending_orders
    finally:
        broker.release_quotes.set()
        controller.stop()


class FakeLiveRouter:
    def __init__(self) -> None:
        self.connected = False
        self.entries: list[tuple[OrderRequest, int]] = []
        self.exits: list[Position] = []
        self.cancellations: list[str] = []

    async def connect(self) -> None:
        self.connected = True

    async def place_entry(self, request: OrderRequest, *, lots: int) -> str:
        self.entries.append((request, lots))
        return f"live-buy-{len(self.entries)}"

    async def exit_position(self, position: Position) -> str:
        self.exits.append(position)
        return "live-sell-1"

    async def cancel_order(self, broker_order_id: str) -> str:
        self.cancellations.append(broker_order_id)
        return broker_order_id

    async def available_balance(self) -> Decimal | None:
        return Decimal("77777.25")


def test_live_router_is_used_only_when_explicitly_enabled(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(
        simulator_root=simulator_root,
        environ={
            "BROKER_ORDER_EXECUTION_ENABLED": "true",
            "KTRADER_BOT_ORDER_INTAKE_ENABLED": "false",
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
    live_router = FakeLiveRouter()

    def live_factory(_settings: Settings) -> LiveOrderRouter:
        return live_router

    controller = TradingController(
        settings,
        broker_factory=lambda _settings: broker,
        live_router_factory=live_factory,
    )
    controller.start()
    try:
        snapshot = _snapshot_from(
            _wait_for(controller, lambda event: isinstance(event, SnapshotEvent))
        )
        request = OrderRequest(
            option=snapshot.rows[2].call,
            order_type=OrderType.MARKET,
            lots=1,
            limit_price=None,
            risk=RiskParameters(),
            request_id="live-controller-order",
            created_at=snapshot.captured_at,
        )
        assert controller.submit_order(request)
        opened = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(item.broker_order_id == "live-buy-1" for item in event.executions)
            ),
        )
        assert isinstance(opened, PortfolioEvent)
        assert opened.account_balance == Decimal("77777.25")
        assert live_router.connected
        assert live_router.entries[0][1] == 1

        assert controller.exit_position("live-controller-order")
        exited = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(item.broker_order_id == "live-sell-1" for item in event.executions)
            ),
        )
        assert isinstance(exited, PortfolioEvent)
        assert not exited.portfolio.positions
        assert len(live_router.exits) == 1

        pending_request = OrderRequest(
            option=snapshot.rows[2].call,
            order_type=OrderType.LIMIT,
            lots=1,
            limit_price=Decimal("80"),
            risk=RiskParameters(),
            request_id="live-pending-order",
            created_at=snapshot.captured_at,
        )
        assert controller.submit_order(pending_request)
        pending = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(item.broker_order_id == "live-buy-2" for item in event.executions)
            ),
        )
        assert isinstance(pending, PortfolioEvent)
        assert pending.portfolio.pending_orders[0].broker_order_id == "live-buy-2"

        assert controller.exit_position("live-pending-order")
        cancelled = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and any(
                    item.kind == ExecutionEventKind.ORDER_CANCELLED
                    and item.broker_order_id == "live-buy-2"
                    for item in event.executions
                )
            ),
        )
        assert isinstance(cancelled, PortfolioEvent)
        assert not cancelled.portfolio.pending_orders
        assert live_router.cancellations == ["live-buy-2"]
    finally:
        controller.stop()


def test_new_bot_signal_opens_market_paper_position_for_maximum_lots(
    tmp_path: Path,
) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    ipc_port = _free_tcp_port()
    settings = load_settings(
        simulator_root=simulator_root,
        environ={
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
        snapshot = _snapshot_from(
            _wait_for(controller, lambda event: isinstance(event, SnapshotEvent))
        )
        option = snapshot.rows[2].call
        signal = BotOrderSignal(
            underlying="NIFTY",
            option_type=OptionType.CALL,
            strike=option.strike,
            captured_at=datetime.now(UTC),
        )
        assert (
            asyncio.run(
                send_buy_event(
                    endpoint="KTraderUI",
                    host="127.0.0.1",
                    port=ipc_port,
                    signal=signal,
                )
            )
            == "OK"
        )

        bot_order = _wait_for(
            controller,
            lambda event: (
                isinstance(event, PortfolioEvent)
                and bool(event.portfolio.positions)
                and event.portfolio.positions[0].source == OrderSource.BOT
            ),
        )
        assert isinstance(bot_order, PortfolioEvent)
        position = bot_order.portfolio.positions[0]
        assert snapshot.rows[2].call_quote is not None
        assert position.entry_price == snapshot.rows[2].call_quote.ask
        assert position.lots == 39
        assert position.quantity == 975
        assert bot_order.portfolio.pending_orders == ()
        assert any(
            execution.kind == ExecutionEventKind.POSITION_OPENED
            for execution in bot_order.executions
        )
    finally:
        controller.stop()
