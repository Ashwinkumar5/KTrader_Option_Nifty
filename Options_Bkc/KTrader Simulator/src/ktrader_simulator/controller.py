from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from hashlib import sha256
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from types import MappingProxyType
from zoneinfo import ZoneInfo

from ktrader_simulator.broker.angleone import AngleOneReadOnlyBroker
from ktrader_simulator.broker.angleone_orders import (
    AngleOneLiveOrderRouter,
    LiveOrderError,
)
from ktrader_simulator.broker.protocols import LiveOrderRouter, ReadOnlyBroker
from ktrader_simulator.config import Settings
from ktrader_simulator.domain.models import MarketSnapshot, Quote
from ktrader_simulator.intake.ipc import BotOrderSignal, BotSignalIpcServer
from ktrader_simulator.market.analytics import ChainAnalyticsEngine, ChainAnalyticsSnapshot
from ktrader_simulator.market.snapshots import MarketSnapshotService
from ktrader_simulator.storage.ledger import JsonlTradeJournal, JsonlTradeLedger
from ktrader_simulator.trading.engine import SimulatorEngine
from ktrader_simulator.trading.models import (
    ExecutionEvent,
    ExecutionEventKind,
    ExitReason,
    OrderRequest,
    OrderSource,
    OrderType,
    PortfolioSnapshot,
    RiskParameters,
)


class ConnectionStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class StatusEvent:
    status: ConnectionStatus
    message: str = ""


@dataclass(frozen=True, slots=True)
class SnapshotEvent:
    snapshot: MarketSnapshot


@dataclass(frozen=True, slots=True)
class AnalyticsEvent:
    snapshot: ChainAnalyticsSnapshot


@dataclass(frozen=True, slots=True)
class PositionPnl:
    amount: Decimal
    percentage: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioEvent:
    portfolio: PortfolioSnapshot
    total_pnl: Decimal
    position_pnl: Mapping[str, PositionPnl]
    reserved_balance: Decimal
    available_balance: Decimal
    executions: tuple[ExecutionEvent, ...] = ()
    account_balance: Decimal | None = None

    @classmethod
    def from_portfolio(
        cls,
        portfolio: PortfolioSnapshot,
        *,
        executions: tuple[ExecutionEvent, ...] = (),
        account_balance: Decimal | None = None,
        capital_utilization: Decimal = Decimal("1"),
    ) -> PortfolioEvent:
        displayed_balance = (
            account_balance if account_balance is not None else portfolio.cash_balance
        )
        reserved_balance = sum(
            (pending.reserved_cash for pending in portfolio.pending_orders),
            Decimal("0"),
        )
        available_balance = max(
            Decimal("0"),
            displayed_balance * capital_utilization - reserved_balance,
        )
        return cls(
            portfolio=portfolio,
            total_pnl=portfolio.total_pnl,
            position_pnl=MappingProxyType(
                {
                    position.position_id: PositionPnl(
                        amount=position.unrealized_pnl,
                        percentage=position.pnl_percent,
                    )
                    for position in portfolio.positions
                }
            ),
            reserved_balance=reserved_balance,
            available_balance=available_balance,
            executions=executions,
            account_balance=account_balance,
        )


@dataclass(frozen=True, slots=True)
class NoticeEvent:
    message: str
    error: bool = False


@dataclass(frozen=True, slots=True)
class _SubmitOrderCommand:
    request: OrderRequest


@dataclass(frozen=True, slots=True)
class _ExitPositionCommand:
    position_id: str


@dataclass(frozen=True, slots=True)
class _EodCommand:
    session_date: date


@dataclass(slots=True)
class _PublishedMarketState:
    sequence: int = 0
    snapshot: MarketSnapshot | None = None
    quotes: dict[str, Quote] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _LedgerWrite:
    portfolio: PortfolioSnapshot
    executions: tuple[ExecutionEvent, ...]
    captured_at: datetime


TradingCommand = _SubmitOrderCommand | _ExitPositionCommand | _EodCommand
ControllerEvent = StatusEvent | SnapshotEvent | AnalyticsEvent | PortfolioEvent | NoticeEvent
BrokerFactory = Callable[[Settings], ReadOnlyBroker]
LiveRouterFactory = Callable[[Settings], LiveOrderRouter]


def _coalesce_controller_events(
    events: tuple[ControllerEvent, ...],
) -> tuple[ControllerEvent, ...]:
    """Keep only the newest UI state while preserving unconsumed executions."""

    latest_status: tuple[int, StatusEvent] | None = None
    latest_snapshot: tuple[int, SnapshotEvent] | None = None
    latest_analytics: tuple[int, AnalyticsEvent] | None = None
    latest_portfolio: tuple[int, PortfolioEvent] | None = None
    latest_notice: tuple[int, NoticeEvent] | None = None
    for index, event in enumerate(events):
        if isinstance(event, StatusEvent):
            latest_status = index, event
        elif isinstance(event, SnapshotEvent):
            latest_snapshot = index, event
        elif isinstance(event, AnalyticsEvent):
            latest_analytics = index, event
        elif isinstance(event, PortfolioEvent):
            executions = event.executions
            if latest_portfolio is not None:
                executions = (*latest_portfolio[1].executions, *executions)
            latest_portfolio = index, replace(event, executions=executions)
        elif isinstance(event, NoticeEvent):
            latest_notice = index, event
    selected = tuple(
        item
        for item in (
            latest_status,
            latest_snapshot,
            latest_analytics,
            latest_portfolio,
            latest_notice,
        )
        if item is not None
    )
    return tuple(event for _, event in sorted(selected, key=lambda item: item[0]))


class TradingController:
    """Coordinate isolated market, trading, persistence, and UI event paths."""

    def __init__(
        self,
        settings: Settings,
        *,
        broker_factory: BrokerFactory = AngleOneReadOnlyBroker,
        live_router_factory: LiveRouterFactory = AngleOneLiveOrderRouter,
    ) -> None:
        self._settings = settings
        self._broker_factory = broker_factory
        self._live_router = (
            live_router_factory(settings) if settings.live_execution_enabled else None
        )
        self._broker_available_balance: Decimal | None = None
        self._selected_index = settings.default_index
        self._selection_queue: Queue[str] = Queue(maxsize=1)
        self._commands: Queue[TradingCommand] = Queue(maxsize=settings.market_data_queue_capacity)
        self._events: Queue[ControllerEvent] = Queue(maxsize=32)
        self._engine = SimulatorEngine(
            starting_balance=settings.starting_balance,
            max_capital_utilization=settings.max_capital_utilization,
            charges_buffer_percent=settings.charges_buffer_percent,
            slippage_points=settings.slippage_points,
        )
        self._ledger = JsonlTradeLedger(
            settings.trade_ledger_path,
            fsync=settings.trade_ledger_fsync,
        )
        self._trade_journal = JsonlTradeJournal(
            settings.trade_journal_dir,
            fsync=settings.trade_ledger_fsync,
        )
        self._market_zone = ZoneInfo(settings.market_timezone)
        self._session_date = datetime.now(self._market_zone).date()
        self._eod_completed_for: date | None = None
        self._ledger_queue: Queue[_LedgerWrite] = Queue(maxsize=settings.ledger_queue_capacity)
        self._ledger_stop = Event()
        self._ledger_thread: Thread | None = None
        self._startup_notice: NoticeEvent | None = None
        if settings.session_recovery_enabled:
            try:
                recovered = self._ledger.load_latest()
                if recovered is not None:
                    self._engine.restore(recovered)
            except (OSError, ValueError) as exc:
                self._startup_notice = NoticeEvent(
                    f"Trade-ledger recovery skipped: {type(exc).__name__}: {exc}",
                    error=True,
                )
        recovered_portfolio = self._engine.portfolio()
        self._accepted_order_ids = (
            {position.order_id for position in recovered_portfolio.positions}
            | {
                pending.order_id
                for pending in recovered_portfolio.pending_orders
                if pending.request.created_at is not None
                and pending.request.created_at.astimezone(self._market_zone).date()
                == self._session_date
            }
            | {
                closed.position.order_id
                for closed in recovered_portfolio.closed_positions
                if closed.closed_at.astimezone(self._market_zone).date() == self._session_date
            }
        )
        self._bot_signal_server = (
            BotSignalIpcServer(
                endpoint=settings.bot_ipc_endpoint,
                host=settings.bot_ipc_host,
                port=settings.bot_ipc_port,
                queue_capacity=settings.bot_ipc_queue_capacity,
                max_age_seconds=settings.bot_signal_max_age_seconds,
            )
            if settings.bot_order_intake_enabled
            else None
        )
        self._stop = Event()
        self._thread: Thread | None = None
        self._async_lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._market_wake: asyncio.Event | None = None
        self._trading_wake: asyncio.Event | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._start_ledger_worker()
        self._thread = Thread(
            target=self._thread_main,
            name="ktrader-runtime",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._notify_all_wakes()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self._ledger_stop.set()
        ledger_thread = self._ledger_thread
        if ledger_thread is not None and ledger_thread.is_alive():
            ledger_thread.join(timeout=timeout)
        self._ledger_thread = None

    def select_index(self, underlying: str) -> None:
        normalized = underlying.strip().upper()
        if normalized not in self._settings.supported_indices:
            return
        try:
            self._selection_queue.put_nowait(normalized)
        except Full:
            with suppress(Empty):
                self._selection_queue.get_nowait()
            self._selection_queue.put_nowait(normalized)
        self._notify_market_wake()

    def refresh_market_data(self) -> None:
        """Request one immediate snapshot without adding another polling path."""
        self._notify_market_wake()

    def submit_order(self, request: OrderRequest) -> bool:
        return self._enqueue_command(_SubmitOrderCommand(request))

    def exit_position(self, position_id: str) -> bool:
        normalized = position_id.strip()
        if not normalized:
            return False
        return self._enqueue_command(_ExitPositionCommand(normalized))

    def portfolio(self) -> PortfolioSnapshot:
        return self._engine.portfolio()

    def drain_events(self) -> tuple[ControllerEvent, ...]:
        events: list[ControllerEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return _coalesce_controller_events(tuple(events))

    def _thread_main(self) -> None:
        broker_io = ThreadPoolExecutor(
            max_workers=self._settings.broker_io_workers,
            thread_name_prefix="ktrader-broker-io",
        )
        try:
            asyncio.run(self._run_with_io_pool(broker_io))
        finally:
            broker_io.shutdown(wait=True, cancel_futures=True)

    async def _run_with_io_pool(self, broker_io: ThreadPoolExecutor) -> None:
        asyncio.get_running_loop().set_default_executor(broker_io)
        await self._run()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        market_wake = asyncio.Event()
        trading_wake = asyncio.Event()
        with self._async_lock:
            self._loop = loop
            self._market_wake = market_wake
            self._trading_wake = trading_wake
        try:
            self._emit(
                PortfolioEvent.from_portfolio(
                    self._engine.portfolio(),
                    account_balance=self._broker_available_balance,
                    capital_utilization=self._settings.max_capital_utilization,
                )
            )
            if self._startup_notice is not None:
                self._emit(self._startup_notice)
            while not self._stop.is_set():
                self._emit(StatusEvent(ConnectionStatus.CONNECTING))
                try:
                    broker = self._broker_factory(self._settings)
                    await broker.connect()
                    service = await MarketSnapshotService.create(
                        broker=broker,
                        settings=self._settings,
                    )
                    if self._live_router is not None:
                        await self._live_router.connect()
                        await self._refresh_broker_balance()
                    self._emit(
                        StatusEvent(
                            ConnectionStatus.CONNECTED,
                            f"{service.instrument_count} option instruments",
                        )
                    )
                    if self._live_router is None:
                        self._emit(
                            NoticeEvent(
                                "SHADOW mode: GUI and bot orders are simulated locally; "
                                "broker routing is disabled"
                            )
                        )
                    await self._runtime_loop(
                        service,
                        market_wake=market_wake,
                        trading_wake=trading_wake,
                    )
                except Exception as exc:
                    self._emit(
                        StatusEvent(
                            ConnectionStatus.ERROR,
                            self._safe_error(exc),
                        )
                    )
                    await self._wait_event(
                        market_wake,
                        float(self._settings.broker_retry_seconds),
                    )
        finally:
            with self._async_lock:
                self._loop = None
                self._market_wake = None
                self._trading_wake = None
            self._emit(StatusEvent(ConnectionStatus.DISCONNECTED))

    async def _runtime_loop(
        self,
        service: MarketSnapshotService,
        *,
        market_wake: asyncio.Event,
        trading_wake: asyncio.Event,
    ) -> None:
        state = _PublishedMarketState()
        trading_wake.set()
        signal_server = self._bot_signal_server
        if signal_server is not None:
            await signal_server.start(trading_wake)
            self._emit(
                NoticeEvent(
                    f"Bot IPC {self._settings.bot_ipc_endpoint} listening on "
                    f"{self._settings.bot_ipc_host}:{signal_server.port}"
                )
            )
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(
                    self._market_loop(
                        service,
                        state=state,
                        market_wake=market_wake,
                        trading_wake=trading_wake,
                    ),
                    name="ktrader-market-hot-path",
                )
                tasks.create_task(
                    self._trading_loop(
                        service,
                        state=state,
                        trading_wake=trading_wake,
                    ),
                    name="ktrader-trading-hot-path",
                )
                tasks.create_task(
                    self._eod_timer_loop(trading_wake),
                    name="ktrader-eod-timer",
                )
                tasks.create_task(
                    self._analytics_loop(service, state),
                    name="ktrader-chain-analytics",
                )
        finally:
            if signal_server is not None:
                await signal_server.close()

    async def _market_loop(
        self,
        service: MarketSnapshotService,
        *,
        state: _PublishedMarketState,
        market_wake: asyncio.Event,
        trading_wake: asyncio.Event,
    ) -> None:
        refresh_seconds = self._settings.quote_refresh_ms / 1000.0
        consecutive_errors = 0
        while not self._stop.is_set():
            self._selected_index = self._latest_selection(self._selected_index)
            try:
                snapshot = await service.snapshot(self._selected_index)
                quotes = await self._trading_quotes(
                    service=service,
                    snapshot=snapshot,
                    commands=(),
                )
            except Exception as exc:
                consecutive_errors += 1
                self._emit(StatusEvent(ConnectionStatus.ERROR, self._safe_error(exc)))
                if consecutive_errors >= self._settings.max_consecutive_quote_errors:
                    raise RuntimeError("maximum consecutive quote errors reached") from exc
            else:
                if consecutive_errors:
                    self._emit(StatusEvent(ConnectionStatus.CONNECTED, "Feed recovered"))
                consecutive_errors = 0
                state.sequence += 1
                state.snapshot = snapshot
                state.quotes = quotes
                self._emit(SnapshotEvent(snapshot))
                trading_wake.set()
            await self._wait_event(market_wake, refresh_seconds)

    async def _analytics_loop(
        self,
        service: MarketSnapshotService,
        state: _PublishedMarketState,
    ) -> None:
        engine = ChainAnalyticsEngine(
            oi_pcr_bearish_threshold=self._settings.oi_pcr_bearish_threshold,
            oi_pcr_bullish_threshold=self._settings.oi_pcr_bullish_threshold,
            volume_pcr_bearish_threshold=self._settings.volume_pcr_bearish_threshold,
            volume_pcr_bullish_threshold=self._settings.volume_pcr_bullish_threshold,
        )
        last_sequence = 0
        refresh_seconds = float(self._settings.chain_analytics_refresh_seconds)
        while not self._stop.is_set():
            snapshot = state.snapshot
            if snapshot is not None and state.sequence != last_sequence:
                analytics_snapshot = snapshot
                try:
                    analytics_snapshot = await service.with_implied_volatilities(snapshot)
                except Exception:
                    # IV is auxiliary analytics data. A broker Greek failure must
                    # never stop quotes, trading, risk checks, or the UI.
                    pass
                self._emit(AnalyticsEvent(engine.build(analytics_snapshot)))
                last_sequence = state.sequence
            await self._wait_event(asyncio.Event(), refresh_seconds)

    async def _trading_loop(
        self,
        service: MarketSnapshotService,
        *,
        state: _PublishedMarketState,
        trading_wake: asyncio.Event,
    ) -> None:
        last_sequence = 0
        deferred_commands: tuple[TradingCommand, ...] = ()
        deferred_signals: tuple[BotOrderSignal, ...] = ()
        while not self._stop.is_set():
            await trading_wake.wait()
            trading_wake.clear()
            if self._stop.is_set():
                return

            sequence = state.sequence
            snapshot = state.snapshot
            market_changed = snapshot is not None and sequence != last_sequence
            quotes = dict(state.quotes)
            commands = (*deferred_commands, *self._drain_commands())
            signal_server = self._bot_signal_server
            signals = (
                (*deferred_signals, *signal_server.drain()) if signal_server is not None else ()
            )
            if snapshot is None:
                deferred_signals = signals
            else:
                commands = (
                    *commands,
                    *await self._bot_commands(
                        service,
                        signals=signals,
                        quotes=quotes,
                    ),
                )
                deferred_signals = ()
            deferred_commands = ()
            if not market_changed and not commands and not deferred_signals:
                continue

            captured_at = (
                snapshot.captured_at
                if snapshot is not None and market_changed
                else datetime.now(UTC)
            )
            version_before = self._engine.portfolio().version
            executions: list[ExecutionEvent] = []
            try:
                if market_changed:
                    executions.extend(
                        self._engine.mark(
                            quotes,
                            captured_at=captured_at,
                            apply_risk=self._live_router is None,
                        )
                    )
                    if self._live_router is not None:
                        executions.extend(
                            await self._execute_live_risk_exits(
                                quotes=quotes,
                                captured_at=captured_at,
                            )
                        )
                    last_sequence = sequence

                executable_commands = tuple(
                    command for command in commands if not isinstance(command, _EodCommand)
                )
                immediate, deferred_commands = self._partition_cached_commands(
                    executable_commands,
                    snapshot,
                    now=datetime.now(UTC),
                )
                if immediate:
                    quotes = await self._command_quotes(
                        service=service,
                        snapshot=snapshot,
                        commands=immediate,
                        available_quotes=quotes,
                    )
                    executions.extend(
                        await self._execute_commands(
                            immediate,
                            quotes=quotes,
                            captured_at=captured_at,
                        )
                    )
                eod_commands = tuple(
                    command for command in commands if isinstance(command, _EodCommand)
                )
                if eod_commands:
                    executions.extend(
                        await self._execute_eod(
                            quotes=quotes,
                            captured_at=captured_at,
                            session_date=eod_commands[-1].session_date,
                        )
                    )
            except Exception:
                self._requeue_commands(commands)
                raise

            execution_events = tuple(executions)
            if any(event.broker_order_id for event in execution_events):
                await self._refresh_broker_balance()
            portfolio = self._engine.portfolio()
            if execution_events or portfolio.version != version_before:
                self._persist_portfolio(
                    portfolio=portfolio,
                    executions=execution_events,
                    captured_at=captured_at,
                )
            self._emit(
                PortfolioEvent.from_portfolio(
                    portfolio=portfolio,
                    executions=execution_events,
                    account_balance=self._broker_available_balance,
                    capital_utilization=self._settings.max_capital_utilization,
                )
            )

    async def _trading_quotes(
        self,
        *,
        service: MarketSnapshotService,
        snapshot: MarketSnapshot,
        commands: tuple[TradingCommand, ...],
    ) -> dict[str, Quote]:
        quotes = _snapshot_quotes(snapshot)
        instruments = {
            option.instrument.token: option.instrument for option in self._engine.tracked_options()
        }
        for command in commands:
            if isinstance(command, _SubmitOrderCommand):
                option = command.request.option
                instruments[option.instrument.token] = option.instrument
        missing = tuple(
            instrument for token, instrument in instruments.items() if token not in quotes
        )
        if missing:
            quotes.update(await service.quotes(missing))
        return quotes

    async def _command_quotes(
        self,
        *,
        service: MarketSnapshotService,
        snapshot: MarketSnapshot | None,
        commands: tuple[TradingCommand, ...],
        available_quotes: dict[str, Quote] | None = None,
    ) -> dict[str, Quote]:
        quotes = (
            dict(available_quotes)
            if available_quotes is not None
            else {}
            if snapshot is None
            else _snapshot_quotes(snapshot)
        )
        instruments = {}
        for command in commands:
            if isinstance(command, _SubmitOrderCommand):
                option = command.request.option
                instruments[option.instrument.token] = option.instrument
                continue
            if isinstance(command, _EodCommand):
                continue
            position = self._engine.position(command.position_id)
            if position is not None:
                option = position.option
                instruments[option.instrument.token] = option.instrument
        missing = tuple(
            instrument for token, instrument in instruments.items() if token not in quotes
        )
        if missing:
            quotes.update(await service.quotes(missing))
        return quotes

    def _partition_cached_commands(
        self,
        commands: tuple[TradingCommand, ...],
        snapshot: MarketSnapshot | None,
        *,
        now: datetime,
    ) -> tuple[tuple[TradingCommand, ...], tuple[TradingCommand, ...]]:
        if not commands:
            return (), ()
        snapshot_is_fresh = (
            snapshot is not None
            and snapshot.underlying == self._selected_index
            and max(
                Decimal("0"),
                Decimal(str((now - snapshot.captured_at).total_seconds())),
            )
            <= self._settings.feed_stale_seconds
        )
        immediate: list[TradingCommand] = []
        deferred: list[TradingCommand] = []
        for command in commands:
            pending_cancellation = (
                isinstance(command, _ExitPositionCommand)
                and self._engine.pending_order(command.position_id) is not None
            )
            target = immediate if pending_cancellation or snapshot_is_fresh else deferred
            target.append(command)
        return tuple(immediate), tuple(deferred)

    async def _execute_commands(
        self,
        commands: tuple[TradingCommand, ...],
        *,
        quotes: dict[str, Quote],
        captured_at: datetime,
    ) -> tuple[ExecutionEvent, ...]:
        events: list[ExecutionEvent] = []
        for command in commands:
            if isinstance(command, _EodCommand):
                continue
            if isinstance(command, _SubmitOrderCommand):
                rejection = self._daily_entry_rejection(command.request, captured_at)
                if rejection is not None:
                    events.append(rejection)
                    continue
                token = command.request.option.instrument.token
                entry_events = self._engine.submit(
                    command.request,
                    quotes.get(token),
                    captured_at=captured_at,
                )
                routed_events = await self._route_entry_if_enabled(
                    request=command.request,
                    events=entry_events,
                    captured_at=captured_at,
                )
                events.extend(routed_events)
                if any(
                    event.kind
                    in {
                        ExecutionEventKind.ORDER_PENDING,
                        ExecutionEventKind.POSITION_OPENED,
                    }
                    for event in routed_events
                ):
                    self._accepted_order_ids.add(command.request.request_id)
                continue
            pending = self._engine.pending_order(command.position_id)
            if pending is not None:
                if self._live_router is not None:
                    broker_order_id = pending.broker_order_id
                    if broker_order_id is None:
                        events.append(
                            _routing_rejection(
                                order_id=pending.order_id,
                                position_id=None,
                                message="Pending order has no broker order ID",
                                captured_at=captured_at,
                            )
                        )
                        continue
                    try:
                        cancelled_order_id = await self._live_router.cancel_order(broker_order_id)
                    except LiveOrderError as exc:
                        events.append(
                            _routing_rejection(
                                order_id=pending.order_id,
                                position_id=None,
                                message=str(exc),
                                captured_at=captured_at,
                            )
                        )
                        continue
                    cancelled = self._engine.cancel_pending(
                        pending.order_id,
                        captured_at=captured_at,
                    )
                    events.extend(
                        replace(event, broker_order_id=cancelled_order_id) for event in cancelled
                    )
                    self._accepted_order_ids.discard(pending.order_id)
                    continue
                events.extend(
                    self._engine.cancel_pending(
                        pending.order_id,
                        captured_at=captured_at,
                    )
                )
                self._accepted_order_ids.discard(pending.order_id)
                continue
            position = self._engine.position(command.position_id)
            quote = quotes.get(position.option.instrument.token) if position is not None else None
            if self._live_router is not None and position is not None:
                if not _has_executable_sell_price(quote):
                    events.extend(
                        self._engine.exit_position(
                            command.position_id,
                            quote,
                            captured_at=captured_at,
                        )
                    )
                    continue
                try:
                    broker_order_id = await self._live_router.exit_position(position)
                except LiveOrderError as exc:
                    events.append(
                        _routing_rejection(
                            order_id=position.order_id,
                            position_id=position.position_id,
                            message=str(exc),
                            captured_at=captured_at,
                        )
                    )
                    continue
                exit_events = self._engine.exit_position(
                    command.position_id,
                    quote,
                    captured_at=captured_at,
                )
                events.extend(
                    replace(event, broker_order_id=broker_order_id) for event in exit_events
                )
                continue
            events.extend(
                self._engine.exit_position(
                    command.position_id,
                    quote,
                    captured_at=captured_at,
                )
            )
        return tuple(events)

    def _daily_entry_rejection(
        self,
        request: OrderRequest,
        captured_at: datetime,
    ) -> ExecutionEvent | None:
        local_date = captured_at.astimezone(self._market_zone).date()
        if local_date != self._session_date:
            self._session_date = local_date
            self._accepted_order_ids.clear()
        if self._eod_completed_for == local_date:
            return _routing_rejection(
                order_id=request.request_id,
                position_id=None,
                message="Order rejected: end-of-day session is closed",
                captured_at=captured_at,
            )
        if len(self._accepted_order_ids) >= self._settings.max_accepted_trades_per_day:
            return _routing_rejection(
                order_id=request.request_id,
                position_id=None,
                message=(
                    "Order rejected: daily accepted-trade limit "
                    f"({self._settings.max_accepted_trades_per_day}) reached"
                ),
                captured_at=captured_at,
            )
        return None

    async def _execute_eod(
        self,
        *,
        quotes: dict[str, Quote],
        captured_at: datetime,
        session_date: date,
    ) -> tuple[ExecutionEvent, ...]:
        if self._eod_completed_for == session_date:
            return ()
        events: list[ExecutionEvent] = []
        for pending in self._engine.portfolio().pending_orders:
            events.extend(self._engine.cancel_pending(pending.order_id, captured_at=captured_at))
        for position in self._engine.portfolio().positions:
            quote = quotes.get(position.option.instrument.token)
            if self._live_router is not None:
                if not _has_executable_sell_price(quote):
                    events.append(
                        _routing_rejection(
                            order_id=position.order_id,
                            position_id=position.position_id,
                            message="EOD exit deferred: no executable quote",
                            captured_at=captured_at,
                        )
                    )
                    continue
                try:
                    broker_order_id = await self._live_router.exit_position(position)
                except LiveOrderError as exc:
                    events.append(
                        _routing_rejection(
                            order_id=position.order_id,
                            position_id=position.position_id,
                            message=f"EOD exit not routed: {exc}",
                            captured_at=captured_at,
                        )
                    )
                    continue
                events.extend(
                    replace(event, broker_order_id=broker_order_id)
                    for event in self._engine.exit_position(
                        position.position_id,
                        quote,
                        captured_at=captured_at,
                        reason=ExitReason.EOD,
                    )
                )
            else:
                events.extend(
                    self._engine.exit_position(
                        position.position_id,
                        quote,
                        captured_at=captured_at,
                        reason=ExitReason.EOD,
                    )
                )
        portfolio = self._engine.portfolio()
        if portfolio.positions or portfolio.pending_orders:
            self._emit(NoticeEvent("EOD incomplete: an open order could not be closed", error=True))
            return tuple(events)
        self._trade_journal.write(session_date=session_date, portfolio=portfolio)
        self._engine.reset_daily_pnl()
        self._accepted_order_ids.clear()
        self._eod_completed_for = session_date
        self._emit(NoticeEvent(f"EOD journal written for {session_date.isoformat()}"))
        return tuple(events)

    async def _eod_timer_loop(self, trading_wake: asyncio.Event) -> None:
        hour, minute = (int(value) for value in self._settings.eod_close_time.split(":"))
        cutoff = time(hour=hour, minute=minute)
        while not self._stop.is_set():
            local_now = datetime.now(self._market_zone)
            is_weekday_session = local_now.weekday() < 5
            if (
                is_weekday_session
                and local_now.time() >= cutoff
                and self._eod_completed_for != local_now.date()
            ):
                self._enqueue_command(_EodCommand(local_now.date()))
                trading_wake.set()
            await self._wait_event(asyncio.Event(), 15.0)

    async def _route_entry_if_enabled(
        self,
        *,
        request: OrderRequest,
        events: tuple[ExecutionEvent, ...],
        captured_at: datetime,
    ) -> tuple[ExecutionEvent, ...]:
        router = self._live_router
        accepted = next(
            (
                event
                for event in events
                if event.kind
                in {
                    ExecutionEventKind.ORDER_PENDING,
                    ExecutionEventKind.POSITION_OPENED,
                }
            ),
            None,
        )
        if router is None or accepted is None or accepted.quantity is None:
            return events
        lots = accepted.quantity // request.option.lot_size
        try:
            broker_order_id = await router.place_entry(request, lots=lots)
        except LiveOrderError as exc:
            self._engine.rollback_entry(request.request_id)
            return (
                _routing_rejection(
                    order_id=request.request_id,
                    position_id=accepted.position_id,
                    message=str(exc),
                    captured_at=captured_at,
                ),
            )
        if accepted.kind == ExecutionEventKind.ORDER_PENDING:
            self._engine.attach_pending_broker_order_id(
                request.request_id,
                broker_order_id,
            )
        return tuple(
            replace(event, broker_order_id=broker_order_id) if event is accepted else event
            for event in events
        )

    async def _execute_live_risk_exits(
        self,
        *,
        quotes: dict[str, Quote],
        captured_at: datetime,
    ) -> tuple[ExecutionEvent, ...]:
        router = self._live_router
        if router is None:
            return ()
        events: list[ExecutionEvent] = []
        for position_id, reason in self._engine.risk_exit_candidates():
            position = self._engine.position(position_id)
            if position is None:
                continue
            quote = quotes.get(position.option.instrument.token)
            if not _has_executable_sell_price(quote):
                continue
            try:
                broker_order_id = await router.exit_position(position)
            except LiveOrderError as exc:
                events.append(
                    _routing_rejection(
                        order_id=position.order_id,
                        position_id=position.position_id,
                        message=f"Risk exit not routed: {exc}",
                        captured_at=captured_at,
                    )
                )
                continue
            exit_events = self._engine.exit_position(
                position_id,
                quote,
                captured_at=captured_at,
                reason=reason,
            )
            events.extend(replace(event, broker_order_id=broker_order_id) for event in exit_events)
        return tuple(events)

    async def _refresh_broker_balance(self) -> None:
        router = self._live_router
        if router is None:
            return
        try:
            balance = await router.available_balance()
        except Exception as exc:
            self._emit(
                NoticeEvent(
                    f"Broker balance refresh failed: {type(exc).__name__}: {exc}",
                    error=True,
                )
            )
            return
        if balance is not None:
            self._broker_available_balance = balance

    async def _bot_commands(
        self,
        service: MarketSnapshotService,
        *,
        signals: tuple[BotOrderSignal, ...],
        quotes: dict[str, Quote],
    ) -> tuple[TradingCommand, ...]:
        commands: list[TradingCommand] = []
        for signal in signals:
            option = service.option_for_contract(
                underlying=signal.underlying,
                strike=signal.strike,
                option_type=signal.option_type,
            )
            if option is None:
                self._emit(
                    NoticeEvent(
                        f"Bot signal ignored: {signal.underlying} {signal.strike} "
                        f"{signal.option_type.value} is not an active contract",
                        error=True,
                    )
                )
                continue
            paper_market_order = self._live_router is None
            quote = quotes.get(option.instrument.token)
            quote_ready = (
                _has_executable_buy_price(quote)
                if paper_market_order
                else quote is not None and quote.bid is not None and quote.bid > 0
            )
            if not quote_ready:
                fetched = await service.quotes((option.instrument,))
                quotes.update(fetched)
                quote = fetched.get(option.instrument.token)
            quote_ready = (
                _has_executable_buy_price(quote)
                if paper_market_order
                else quote is not None and quote.bid is not None and quote.bid > 0
            )
            if not quote_ready:
                self._emit(
                    NoticeEvent(
                        f"Bot signal ignored: no executable quote for "
                        f"{option.instrument.trading_symbol}",
                        error=True,
                    )
                )
                continue
            limit_price = None
            if not paper_market_order:
                assert quote is not None and quote.bid is not None
                limit_price = (quote.bid + self._settings.default_buy_price_offset).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            identity = (
                f"{signal.underlying}:{signal.strike}:{signal.option_type.value}:"
                f"{signal.captured_at.isoformat()}"
            )
            request_id = signal.signal_id or (
                "bot-" + sha256(identity.encode("utf-8")).hexdigest()[:28]
            )
            commands.append(
                _SubmitOrderCommand(
                    OrderRequest(
                        option=option,
                        order_type=(OrderType.MARKET if paper_market_order else OrderType.LIMIT),
                        lots=None,
                        limit_price=limit_price,
                        risk=RiskParameters(
                            target_percent=self._settings.default_target_percent,
                            stop_loss_percent=self._settings.default_stop_loss_percent,
                            trailing_stop_percent=(self._settings.default_trailing_sl_percent),
                        ),
                        source=OrderSource.BOT,
                        request_id=request_id,
                        created_at=signal.captured_at,
                    )
                )
            )
        return tuple(commands)

    async def _wait_event(self, event: asyncio.Event, timeout: float) -> None:
        if self._stop.is_set():
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=timeout)
        event.clear()

    def _latest_selection(self, current: str) -> str:
        latest = current
        while True:
            try:
                latest = self._selection_queue.get_nowait()
            except Empty:
                return latest

    def _emit(self, event: ControllerEvent) -> None:
        try:
            self._events.put_nowait(event)
        except Full:
            with suppress(Empty):
                self._events.get_nowait()
            self._events.put_nowait(event)

    def _enqueue_command(self, command: TradingCommand) -> bool:
        try:
            self._commands.put_nowait(command)
        except Full:
            return False
        self._notify_trading_wake()
        return True

    def _drain_commands(self) -> tuple[TradingCommand, ...]:
        commands: list[TradingCommand] = []
        while True:
            try:
                commands.append(self._commands.get_nowait())
            except Empty:
                return tuple(commands)

    def _requeue_commands(self, commands: tuple[TradingCommand, ...]) -> None:
        for command in commands:
            try:
                self._commands.put_nowait(command)
            except Full:
                self._emit(
                    StatusEvent(
                        ConnectionStatus.ERROR,
                        "Trading command queue overflowed during broker recovery",
                    )
                )
                return

    def _persist_portfolio(
        self,
        *,
        portfolio: PortfolioSnapshot,
        executions: tuple[ExecutionEvent, ...],
        captured_at: datetime,
    ) -> None:
        try:
            self._ledger_queue.put_nowait(
                _LedgerWrite(
                    portfolio=portfolio,
                    executions=executions,
                    captured_at=captured_at,
                )
            )
        except Full:
            self._emit(
                NoticeEvent(
                    "Trade-ledger queue is full; the latest state was not journaled",
                    error=True,
                )
            )

    def _start_ledger_worker(self) -> None:
        thread = self._ledger_thread
        if thread is not None and thread.is_alive():
            return
        self._ledger_stop.clear()
        self._ledger_thread = Thread(
            target=self._ledger_worker_main,
            name="ktrader-ledger-writer",
            daemon=True,
        )
        self._ledger_thread.start()

    def _ledger_worker_main(self) -> None:
        while not self._ledger_stop.is_set() or not self._ledger_queue.empty():
            try:
                write = self._ledger_queue.get(timeout=0.1)
            except Empty:
                continue
            try:
                self._ledger.append(
                    portfolio=write.portfolio,
                    executions=write.executions,
                    recorded_at=write.captured_at,
                )
            except OSError as exc:
                self._emit(
                    NoticeEvent(
                        f"Trade-ledger write failed: {type(exc).__name__}: {exc}",
                        error=True,
                    )
                )

    def _notify_market_wake(self) -> None:
        with self._async_lock:
            loop = self._loop
            market_wake = self._market_wake
        if loop is not None and market_wake is not None:
            loop.call_soon_threadsafe(market_wake.set)

    def _notify_trading_wake(self) -> None:
        with self._async_lock:
            loop = self._loop
            trading_wake = self._trading_wake
        if loop is not None and trading_wake is not None:
            loop.call_soon_threadsafe(trading_wake.set)

    def _notify_all_wakes(self) -> None:
        with self._async_lock:
            loop = self._loop
            market_wake = self._market_wake
            trading_wake = self._trading_wake
        if loop is None:
            return
        if market_wake is not None:
            loop.call_soon_threadsafe(market_wake.set)
        if trading_wake is not None:
            loop.call_soon_threadsafe(trading_wake.set)

    def _safe_error(self, error: Exception) -> str:
        message = f"{type(error).__name__}: {error}"
        for secret in (
            self._settings.angleone_api_key,
            self._settings.angleone_client_code,
            self._settings.angleone_password,
            self._settings.angleone_totp_secret,
        ):
            if secret:
                message = message.replace(secret, "***")
        return message


def _snapshot_quotes(snapshot: MarketSnapshot) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for row in snapshot.rows:
        if row.call_quote is not None:
            quotes[row.call.instrument.token] = row.call_quote
        if row.put_quote is not None:
            quotes[row.put.instrument.token] = row.put_quote
    return quotes


def _has_executable_sell_price(quote: Quote | None) -> bool:
    if quote is None:
        return False
    return bool(
        (quote.bid is not None and quote.bid > 0) or (quote.ltp is not None and quote.ltp > 0)
    )


def _has_executable_buy_price(quote: Quote | None) -> bool:
    if quote is None:
        return False
    return bool(
        (quote.ask is not None and quote.ask > 0) or (quote.ltp is not None and quote.ltp > 0)
    )


def _routing_rejection(
    *,
    order_id: str,
    position_id: str | None,
    message: str,
    captured_at: datetime,
) -> ExecutionEvent:
    return ExecutionEvent(
        kind=ExecutionEventKind.ORDER_REJECTED,
        order_id=order_id,
        position_id=position_id,
        message=f"Live broker rejected order: {message}",
        captured_at=captured_at,
    )
