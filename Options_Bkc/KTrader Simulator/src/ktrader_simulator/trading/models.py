from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from ktrader_simulator.domain.models import OptionInstrument


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderSource(StrEnum):
    GUI = "GUI"
    BOT = "BOT"


class ExitReason(StrEnum):
    MANUAL = "MANUAL"
    TARGET = "TARGET"
    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    EOD = "EOD"


class ExecutionEventKind(StrEnum):
    ORDER_PENDING = "ORDER_PENDING"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_UPDATED = "POSITION_UPDATED"
    POSITION_EXITED = "POSITION_EXITED"


@dataclass(frozen=True, slots=True)
class RiskParameters:
    target_percent: Decimal = Decimal("0")
    stop_loss_percent: Decimal = Decimal("0")
    trailing_stop_percent: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        _percentage(self.target_percent, "target_percent", maximum=Decimal("1000"))
        _percentage(self.stop_loss_percent, "stop_loss_percent")
        _percentage(self.trailing_stop_percent, "trailing_stop_percent")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    option: OptionInstrument
    order_type: OrderType
    lots: int | None
    limit_price: Decimal | None
    risk: RiskParameters
    source: OrderSource = OrderSource.GUI
    request_id: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.option.lot_size <= 0:
            raise ValueError("option lot size must be positive")
        if self.lots is not None and self.lots <= 0:
            raise ValueError("lots must be positive or None for automatic sizing")
        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None or not _positive_finite(self.limit_price):
                raise ValueError("limit orders require a positive finite limit price")
        elif self.limit_price is not None and not self.limit_price.is_finite():
            raise ValueError("limit_price must be finite")
        if not self.request_id:
            object.__setattr__(self, "request_id", uuid4().hex)
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(UTC))

    @property
    def quantity(self) -> int | None:
        return None if self.lots is None else self.lots * self.option.lot_size


@dataclass(frozen=True, slots=True)
class PendingOrder:
    request: OrderRequest
    lots: int
    reserved_cash: Decimal
    broker_order_id: str | None = None

    @property
    def order_id(self) -> str:
        return self.request.request_id

    @property
    def quantity(self) -> int:
        return self.lots * self.request.option.lot_size


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    order_id: str
    option: OptionInstrument
    lots: int
    entry_price: Decimal
    current_price: Decimal
    high_watermark: Decimal
    opened_at: datetime
    risk: RiskParameters
    source: OrderSource

    @property
    def quantity(self) -> int:
        return self.lots * self.option.lot_size

    @property
    def cost(self) -> Decimal:
        return self.entry_price * self.quantity

    @property
    def market_value(self) -> Decimal:
        return self.current_price * self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        return self.market_value - self.cost

    @property
    def pnl_percent(self) -> Decimal:
        if self.entry_price <= 0:
            return Decimal("0")
        return (self.current_price - self.entry_price) * Decimal("100") / self.entry_price


@dataclass(frozen=True, slots=True)
class ClosedPosition:
    """Immutable final state retained for the current trading session."""

    position: Position
    closed_at: datetime
    exit_reason: ExitReason

    @property
    def realized_pnl(self) -> Decimal:
        return self.position.unrealized_pnl


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    starting_balance: Decimal
    cash_balance: Decimal
    realized_pnl: Decimal
    positions: tuple[Position, ...]
    pending_orders: tuple[PendingOrder, ...]
    version: int
    closed_positions: tuple[ClosedPosition, ...] = ()

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(
            (position.unrealized_pnl for position in self.positions),
            Decimal("0"),
        )

    @property
    def market_value(self) -> Decimal:
        return sum(
            (position.market_value for position in self.positions),
            Decimal("0"),
        )

    @property
    def equity(self) -> Decimal:
        return self.cash_balance + self.market_value

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    kind: ExecutionEventKind
    order_id: str
    message: str
    captured_at: datetime
    position_id: str | None = None
    price: Decimal | None = None
    quantity: int | None = None
    realized_pnl: Decimal = Decimal("0")
    exit_reason: ExitReason | None = None
    broker_order_id: str | None = None


def _percentage(value: Decimal, name: str, *, maximum: Decimal = Decimal("100")) -> None:
    if not value.is_finite() or value < 0 or value > maximum:
        raise ValueError(f"{name} must be between 0 and {maximum}")


def _positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > 0
