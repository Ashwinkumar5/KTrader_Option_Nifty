from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from threading import RLock

from ktrader_simulator.domain.models import OptionInstrument, Quote
from ktrader_simulator.trading.models import (
    ClosedPosition,
    ExecutionEvent,
    ExecutionEventKind,
    ExitReason,
    OrderRequest,
    OrderType,
    PendingOrder,
    PortfolioSnapshot,
    Position,
)


class SimulatorEngine:
    """Thread-safe long-option simulator with deterministic exchange semantics."""

    def __init__(
        self,
        *,
        starting_balance: Decimal,
        max_capital_utilization: Decimal = Decimal("1"),
        charges_buffer_percent: Decimal = Decimal("0"),
        slippage_points: Decimal = Decimal("0"),
    ) -> None:
        if not _positive_finite(starting_balance):
            raise ValueError("starting_balance must be positive and finite")
        if not max_capital_utilization.is_finite() or not Decimal(
            "0"
        ) < max_capital_utilization <= Decimal("1"):
            raise ValueError("max_capital_utilization must be in (0, 1]")
        if not _non_negative_finite(charges_buffer_percent):
            raise ValueError("charges_buffer_percent must be non-negative and finite")
        if not _non_negative_finite(slippage_points):
            raise ValueError("slippage_points must be non-negative and finite")

        self._starting_balance = starting_balance
        self._cash_balance = starting_balance
        self._realized_pnl = Decimal("0")
        self._max_capital_utilization = max_capital_utilization
        self._charges_multiplier = Decimal("1") + charges_buffer_percent / Decimal("100")
        self._slippage_points = slippage_points
        self._positions: dict[str, Position] = {}
        self._pending_orders: dict[str, PendingOrder] = {}
        self._closed_positions: list[ClosedPosition] = []
        self._seen_order_ids: set[str] = set()
        self._version = 0
        self._lock = RLock()

    def submit(
        self,
        request: OrderRequest,
        quote: Quote | None,
        *,
        captured_at: datetime | None = None,
    ) -> tuple[ExecutionEvent, ...]:
        now = captured_at or datetime.now(UTC)
        with self._lock:
            if self._order_exists(request.request_id):
                return (
                    self._event(
                        ExecutionEventKind.ORDER_REJECTED,
                        request,
                        now,
                        "Duplicate order request rejected",
                    ),
                )

            candidate_price = self._candidate_buy_price(request, quote)
            if request.order_type == OrderType.MARKET and candidate_price is None:
                return (
                    self._event(
                        ExecutionEventKind.ORDER_REJECTED,
                        request,
                        now,
                        "Market order rejected: no executable ask or LTP",
                    ),
                )

            sizing_price = candidate_price or request.limit_price
            if sizing_price is None or sizing_price <= 0:
                return (
                    self._event(
                        ExecutionEventKind.ORDER_REJECTED,
                        request,
                        now,
                        "Order rejected: no valid sizing price",
                    ),
                )
            lots = request.lots or self._maximum_affordable_lots(
                price=sizing_price,
                lot_size=request.option.lot_size,
            )
            if lots <= 0:
                return (
                    self._event(
                        ExecutionEventKind.ORDER_REJECTED,
                        request,
                        now,
                        "Order rejected: insufficient available balance",
                    ),
                )

            required_cash = self._required_cash(
                price=sizing_price,
                quantity=lots * request.option.lot_size,
            )
            if required_cash > self._spendable_cash():
                return (
                    self._event(
                        ExecutionEventKind.ORDER_REJECTED,
                        request,
                        now,
                        "Order rejected: insufficient available balance",
                    ),
                )

            if candidate_price is not None:
                return (
                    self._open_position(
                        request=request,
                        lots=lots,
                        fill_price=candidate_price,
                        captured_at=now,
                    ),
                )

            pending = PendingOrder(
                request=request,
                lots=lots,
                reserved_cash=required_cash,
            )
            self._pending_orders[pending.order_id] = pending
            self._seen_order_ids.add(pending.order_id)
            self._version += 1
            return (
                self._event(
                    ExecutionEventKind.ORDER_PENDING,
                    request,
                    now,
                    f"Limit order pending at {request.limit_price}",
                    quantity=pending.quantity,
                ),
            )

    def mark(
        self,
        quotes: dict[str, Quote],
        *,
        captured_at: datetime | None = None,
        apply_risk: bool = True,
    ) -> tuple[ExecutionEvent, ...]:
        now = captured_at or datetime.now(UTC)
        events: list[ExecutionEvent] = []
        with self._lock:
            for order_id, pending in tuple(self._pending_orders.items()):
                quote = quotes.get(pending.request.option.instrument.token)
                fill_price = self._candidate_buy_price(pending.request, quote)
                if fill_price is None:
                    continue
                del self._pending_orders[order_id]
                events.append(
                    self._open_position(
                        request=pending.request,
                        lots=pending.lots,
                        fill_price=fill_price,
                        captured_at=now,
                    )
                )

            for position_id, original in tuple(self._positions.items()):
                quote = quotes.get(original.option.instrument.token)
                mark_price = self._sell_price(quote)
                if mark_price is None:
                    continue
                high_watermark = max(original.high_watermark, mark_price)
                position = replace(
                    original,
                    current_price=mark_price,
                    high_watermark=high_watermark,
                )
                self._positions[position_id] = position
                if high_watermark != original.high_watermark:
                    self._version += 1
                reason = _risk_exit_reason(position)
                if reason is None or not apply_risk:
                    continue
                events.append(self._close_position(position, now, reason))
        return tuple(events)

    def risk_exit_candidates(self) -> tuple[tuple[str, ExitReason], ...]:
        with self._lock:
            return tuple(
                (position.position_id, reason)
                for position in self._positions.values()
                if (reason := _risk_exit_reason(position)) is not None
            )

    def position(self, position_id: str) -> Position | None:
        with self._lock:
            return self._positions.get(position_id)

    def pending_order(self, order_id: str) -> PendingOrder | None:
        with self._lock:
            return self._pending_orders.get(order_id)

    def attach_pending_broker_order_id(
        self,
        order_id: str,
        broker_order_id: str,
    ) -> bool:
        normalized = broker_order_id.strip()
        if not normalized:
            return False
        with self._lock:
            pending = self._pending_orders.get(order_id)
            if pending is None:
                return False
            self._pending_orders[order_id] = replace(
                pending,
                broker_order_id=normalized,
            )
            self._version += 1
            return True

    def rollback_entry(self, order_id: str) -> bool:
        """Undo a local entry when the gated live broker rejects it."""

        with self._lock:
            pending = self._pending_orders.pop(order_id, None)
            if pending is not None:
                self._seen_order_ids.discard(order_id)
                self._version += 1
                return True
            position = self._positions.pop(order_id, None)
            if position is None:
                return False
            self._cash_balance += position.cost
            self._seen_order_ids.discard(order_id)
            self._version += 1
            return True

    def exit_position(
        self,
        position_id: str,
        quote: Quote | None,
        *,
        captured_at: datetime | None = None,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> tuple[ExecutionEvent, ...]:
        now = captured_at or datetime.now(UTC)
        with self._lock:
            position = self._positions.get(position_id)
            if position is None:
                return (
                    ExecutionEvent(
                        kind=ExecutionEventKind.ORDER_REJECTED,
                        order_id="",
                        position_id=position_id,
                        message="Exit rejected: position is not open",
                        captured_at=now,
                    ),
                )
            exit_price = self._sell_price(quote)
            if exit_price is None:
                return (
                    ExecutionEvent(
                        kind=ExecutionEventKind.ORDER_REJECTED,
                        order_id=position.order_id,
                        position_id=position_id,
                        message="Exit rejected: no executable bid or LTP",
                        captured_at=now,
                    ),
                )
            marked = replace(
                position,
                current_price=exit_price,
                high_watermark=max(position.high_watermark, exit_price),
            )
            self._positions[position_id] = marked
            return (self._close_position(marked, now, reason),)

    def cancel_pending(
        self,
        order_id: str,
        *,
        captured_at: datetime | None = None,
    ) -> tuple[ExecutionEvent, ...]:
        now = captured_at or datetime.now(UTC)
        with self._lock:
            pending = self._pending_orders.pop(order_id, None)
            if pending is None:
                return ()
            self._version += 1
            return (
                self._event(
                    ExecutionEventKind.ORDER_CANCELLED,
                    pending.request,
                    now,
                    "Pending order cancelled",
                    quantity=pending.quantity,
                ),
            )

    def portfolio(self) -> PortfolioSnapshot:
        with self._lock:
            return PortfolioSnapshot(
                starting_balance=self._starting_balance,
                cash_balance=self._cash_balance,
                realized_pnl=self._realized_pnl,
                positions=tuple(sorted(self._positions.values(), key=lambda item: item.opened_at)),
                pending_orders=tuple(
                    sorted(
                        self._pending_orders.values(),
                        key=lambda item: (
                            item.request.created_at or datetime.min.replace(tzinfo=UTC)
                        ),
                    )
                ),
                version=self._version,
                closed_positions=tuple(self._closed_positions),
            )

    def restore(self, snapshot: PortfolioSnapshot) -> None:
        """Restore the last durable local state before the controller starts."""

        if snapshot.starting_balance != self._starting_balance:
            raise ValueError("ledger starting balance does not match configuration")
        if not snapshot.cash_balance.is_finite() or snapshot.cash_balance < 0:
            raise ValueError("ledger cash balance must be non-negative and finite")
        if not snapshot.realized_pnl.is_finite():
            raise ValueError("ledger realized P&L must be finite")
        positions = {position.position_id: position for position in snapshot.positions}
        pending = {order.order_id: order for order in snapshot.pending_orders}
        if len(positions) != len(snapshot.positions):
            raise ValueError("ledger contains duplicate position IDs")
        if len(pending) != len(snapshot.pending_orders):
            raise ValueError("ledger contains duplicate pending order IDs")
        if positions.keys() & pending.keys():
            raise ValueError("ledger order IDs overlap positions and pending orders")
        with self._lock:
            self._cash_balance = snapshot.cash_balance
            self._realized_pnl = snapshot.realized_pnl
            self._positions = positions
            self._pending_orders = pending
            self._closed_positions = list(snapshot.closed_positions)
            self._seen_order_ids = set(positions) | set(pending)
            self._version = snapshot.version

    def reset_daily_pnl(self) -> None:
        """Clear only completed-trade history after it has been journaled."""
        with self._lock:
            self._realized_pnl = Decimal("0")
            self._closed_positions.clear()
            self._version += 1

    def tracked_tokens(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                position.option.instrument.token for position in self._positions.values()
            ) | frozenset(
                pending.request.option.instrument.token for pending in self._pending_orders.values()
            )

    def tracked_options(self) -> tuple[OptionInstrument, ...]:
        with self._lock:
            by_token = {
                position.option.instrument.token: position.option
                for position in self._positions.values()
            }
            by_token.update(
                {
                    pending.request.option.instrument.token: pending.request.option
                    for pending in self._pending_orders.values()
                }
            )
            return tuple(by_token.values())

    def _open_position(
        self,
        *,
        request: OrderRequest,
        lots: int,
        fill_price: Decimal,
        captured_at: datetime,
    ) -> ExecutionEvent:
        quantity = lots * request.option.lot_size
        cost = fill_price * quantity
        if cost > self._cash_balance:
            return self._event(
                ExecutionEventKind.ORDER_REJECTED,
                request,
                captured_at,
                "Order rejected: balance changed before fill",
                quantity=quantity,
            )
        self._cash_balance -= cost
        self._seen_order_ids.add(request.request_id)
        position = Position(
            position_id=request.request_id,
            order_id=request.request_id,
            option=request.option,
            lots=lots,
            entry_price=fill_price,
            current_price=fill_price,
            high_watermark=fill_price,
            opened_at=captured_at,
            risk=request.risk,
            source=request.source,
        )
        self._positions[position.position_id] = position
        self._version += 1
        return self._event(
            ExecutionEventKind.POSITION_OPENED,
            request,
            captured_at,
            f"Bought {lots} lot(s) at {fill_price}",
            position_id=position.position_id,
            price=fill_price,
            quantity=quantity,
        )

    def _close_position(
        self,
        position: Position,
        captured_at: datetime,
        reason: ExitReason,
    ) -> ExecutionEvent:
        proceeds = position.market_value
        realized_pnl = position.unrealized_pnl
        self._cash_balance += proceeds
        self._realized_pnl += realized_pnl
        del self._positions[position.position_id]
        self._closed_positions.append(
            ClosedPosition(
                position=position,
                closed_at=captured_at,
                exit_reason=reason,
            )
        )
        self._version += 1
        return ExecutionEvent(
            kind=ExecutionEventKind.POSITION_EXITED,
            order_id=position.order_id,
            position_id=position.position_id,
            message=f"Position exited at {position.current_price}: {reason.value}",
            captured_at=captured_at,
            price=position.current_price,
            quantity=position.quantity,
            realized_pnl=realized_pnl,
            exit_reason=reason,
        )

    def _candidate_buy_price(
        self,
        request: OrderRequest,
        quote: Quote | None,
    ) -> Decimal | None:
        executable = _ask_or_ltp(quote)
        if executable is None:
            return None
        if request.order_type == OrderType.LIMIT:
            limit = request.limit_price
            if limit is None or executable > limit:
                return None
            return min(executable, limit)
        return executable + self._slippage_points

    def _sell_price(self, quote: Quote | None) -> Decimal | None:
        executable = _bid_or_ltp(quote)
        if executable is None:
            return None
        return max(Decimal("0"), executable - self._slippage_points)

    def _maximum_affordable_lots(self, *, price: Decimal, lot_size: int) -> int:
        per_lot = self._required_cash(price=price, quantity=lot_size)
        if per_lot <= 0:
            return 0
        return int((self._spendable_cash() / per_lot).to_integral_value(rounding=ROUND_FLOOR))

    def _required_cash(self, *, price: Decimal, quantity: int) -> Decimal:
        return price * quantity * self._charges_multiplier

    def _reserved_cash(self) -> Decimal:
        return sum(
            (pending.reserved_cash for pending in self._pending_orders.values()),
            Decimal("0"),
        )

    def _spendable_cash(self) -> Decimal:
        utilization_limit = self._cash_balance * self._max_capital_utilization
        return max(Decimal("0"), utilization_limit - self._reserved_cash())

    def _order_exists(self, order_id: str) -> bool:
        return order_id in self._seen_order_ids

    @staticmethod
    def _event(
        kind: ExecutionEventKind,
        request: OrderRequest,
        captured_at: datetime,
        message: str,
        *,
        position_id: str | None = None,
        price: Decimal | None = None,
        quantity: int | None = None,
    ) -> ExecutionEvent:
        return ExecutionEvent(
            kind=kind,
            order_id=request.request_id,
            position_id=position_id,
            message=message,
            captured_at=captured_at,
            price=price,
            quantity=quantity,
        )


def _ask_or_ltp(quote: Quote | None) -> Decimal | None:
    if quote is None:
        return None
    if quote.ask is not None and quote.ask > 0:
        return quote.ask
    return quote.ltp if quote.ltp is not None and quote.ltp > 0 else None


def _bid_or_ltp(quote: Quote | None) -> Decimal | None:
    if quote is None:
        return None
    if quote.bid is not None and quote.bid > 0:
        return quote.bid
    return quote.ltp if quote.ltp is not None and quote.ltp > 0 else None


def _risk_exit_reason(position: Position) -> ExitReason | None:
    risk = position.risk
    price = position.current_price
    peak_profit_percent = (
        (position.high_watermark - position.entry_price)
        * Decimal("100")
        / position.entry_price
    )
    # Profit lock policy: floor at entry + 0.15 after 2%, then lock gains at
    # 5% and 10%.  A 2% trailing stop only becomes active after +15%.
    locked_stop: Decimal | None = None
    if peak_profit_percent >= Decimal("10"):
        locked_stop = position.entry_price * Decimal("1.10")
    elif peak_profit_percent >= Decimal("5"):
        locked_stop = position.entry_price * Decimal("1.05")
    elif peak_profit_percent >= Decimal("2"):
        locked_stop = position.entry_price + Decimal("0.15")
    configured_stop = (
        position.entry_price * (Decimal("1") - risk.stop_loss_percent / Decimal("100"))
        if risk.stop_loss_percent > 0
        else None
    )
    stops = tuple(value for value in (configured_stop, locked_stop) if value is not None)
    stop = max(stops) if stops else None
    if stop is not None and price <= stop:
        return ExitReason.STOP_LOSS
    if peak_profit_percent >= Decimal("15"):
        trailing_percent = Decimal("2")
    else:
        trailing_percent = risk.trailing_stop_percent
    if trailing_percent > 0 and peak_profit_percent >= Decimal("15"):
        trailing_stop = position.high_watermark * (Decimal("1") - trailing_percent / Decimal("100"))
        if price <= trailing_stop:
            return ExitReason.TRAILING_STOP
    if risk.target_percent > 0:
        target = position.entry_price * (Decimal("1") + risk.target_percent / Decimal("100"))
        if price >= target:
            return ExitReason.TARGET
    return None


def _positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > 0


def _non_negative_finite(value: Decimal) -> bool:
    return value.is_finite() and value >= 0
