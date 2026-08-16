from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock

from ktrader_simulator.domain.models import Instrument, OptionInstrument, OptionType
from ktrader_simulator.trading.models import (
    ClosedPosition,
    ExecutionEvent,
    ExitReason,
    OrderRequest,
    OrderSource,
    OrderType,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    RiskParameters,
)


class JsonlTradeLedger:
    """Append-only audit journal with last-valid-record crash recovery."""

    def __init__(self, path: Path, *, fsync: bool) -> None:
        self._path = path
        self._fsync = fsync
        self._lock = Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(
        self,
        *,
        portfolio: PortfolioSnapshot,
        executions: tuple[ExecutionEvent, ...],
        recorded_at: datetime,
    ) -> None:
        record = {
            "schema_version": 1,
            "record_type": "simulator_portfolio_state",
            "recorded_at": recorded_at.isoformat(),
            "executions": [_execution_to_json(event) for event in executions],
            "portfolio": _portfolio_to_json(portfolio),
        }
        serialized = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

    def load_latest(self) -> PortfolioSnapshot | None:
        if not self._path.is_file():
            return None
        latest: PortfolioSnapshot | None = None
        with self._lock, self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    raw: object = json.loads(line)
                    record = _mapping(raw, "ledger record")
                    if record.get("record_type") != "simulator_portfolio_state":
                        continue
                    latest = _portfolio_from_json(record.get("portfolio"))
                except (InvalidOperation, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
        return latest


class JsonlTradeJournal:
    """One compact, immutable end-of-day record per session."""

    def __init__(self, directory: Path, *, fsync: bool) -> None:
        self._directory = directory
        self._fsync = fsync
        self._lock = Lock()

    def write(self, *, session_date: date, portfolio: PortfolioSnapshot) -> Path:
        record = {
            "schema_version": 1,
            "record_type": "ktrader_end_of_day",
            "trade_date": session_date.isoformat(),
            "portfolio": _portfolio_to_json(portfolio),
        }
        payload = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        path = self._directory / f"{session_date.isoformat()}.journal"
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
        return path


def _portfolio_to_json(portfolio: PortfolioSnapshot) -> dict[str, object]:
    return {
        "starting_balance": str(portfolio.starting_balance),
        "cash_balance": str(portfolio.cash_balance),
        "realized_pnl": str(portfolio.realized_pnl),
        "version": portfolio.version,
        "positions": [_position_to_json(position) for position in portfolio.positions],
        "pending_orders": [_pending_to_json(pending) for pending in portfolio.pending_orders],
        "closed_positions": [
            _closed_position_to_json(closed) for closed in portfolio.closed_positions
        ],
    }


def _portfolio_from_json(value: object) -> PortfolioSnapshot:
    payload = _mapping(value, "portfolio")
    positions = _sequence(payload.get("positions"), "positions")
    pending = _sequence(payload.get("pending_orders"), "pending_orders")
    closed = _sequence(payload.get("closed_positions", []), "closed_positions")
    return PortfolioSnapshot(
        starting_balance=_decimal(payload.get("starting_balance"), "starting_balance"),
        cash_balance=_decimal(payload.get("cash_balance"), "cash_balance"),
        realized_pnl=_decimal(payload.get("realized_pnl"), "realized_pnl"),
        positions=tuple(_position_from_json(item) for item in positions),
        pending_orders=tuple(_pending_from_json(item) for item in pending),
        version=_integer(payload.get("version"), "version", minimum=0),
        closed_positions=tuple(_closed_position_from_json(item) for item in closed),
    )


def _position_to_json(position: Position) -> dict[str, object]:
    return {
        "position_id": position.position_id,
        "order_id": position.order_id,
        "option": _option_to_json(position.option),
        "lots": position.lots,
        "entry_price": str(position.entry_price),
        "current_price": str(position.current_price),
        "high_watermark": str(position.high_watermark),
        "opened_at": position.opened_at.isoformat(),
        "risk": _risk_to_json(position.risk),
        "source": position.source.value,
    }


def _position_from_json(value: object) -> Position:
    payload = _mapping(value, "position")
    return Position(
        position_id=_text(payload.get("position_id"), "position_id"),
        order_id=_text(payload.get("order_id"), "order_id"),
        option=_option_from_json(payload.get("option")),
        lots=_integer(payload.get("lots"), "lots", minimum=1),
        entry_price=_positive_decimal(payload.get("entry_price"), "entry_price"),
        current_price=_non_negative_decimal(payload.get("current_price"), "current_price"),
        high_watermark=_positive_decimal(payload.get("high_watermark"), "high_watermark"),
        opened_at=_datetime(payload.get("opened_at"), "opened_at"),
        risk=_risk_from_json(payload.get("risk")),
        source=OrderSource(_text(payload.get("source"), "source")),
    )


def _closed_position_to_json(closed: ClosedPosition) -> dict[str, object]:
    return {
        "position": _position_to_json(closed.position),
        "closed_at": closed.closed_at.isoformat(),
        "exit_reason": closed.exit_reason.value,
    }


def _closed_position_from_json(value: object) -> ClosedPosition:
    payload = _mapping(value, "closed position")
    return ClosedPosition(
        position=_position_from_json(payload.get("position")),
        closed_at=_datetime(payload.get("closed_at"), "closed_at"),
        exit_reason=ExitReason(_text(payload.get("exit_reason"), "exit_reason")),
    )


def _pending_to_json(pending: PendingOrder) -> dict[str, object]:
    return {
        "request": _request_to_json(pending.request),
        "lots": pending.lots,
        "reserved_cash": str(pending.reserved_cash),
        "broker_order_id": pending.broker_order_id,
    }


def _pending_from_json(value: object) -> PendingOrder:
    payload = _mapping(value, "pending order")
    raw_broker_order_id = payload.get("broker_order_id")
    return PendingOrder(
        request=_request_from_json(payload.get("request")),
        lots=_integer(payload.get("lots"), "lots", minimum=1),
        reserved_cash=_non_negative_decimal(payload.get("reserved_cash"), "reserved_cash"),
        broker_order_id=(
            None if raw_broker_order_id is None else _text(raw_broker_order_id, "broker_order_id")
        ),
    )


def _request_to_json(request: OrderRequest) -> dict[str, object]:
    return {
        "option": _option_to_json(request.option),
        "order_type": request.order_type.value,
        "lots": request.lots,
        "limit_price": str(request.limit_price) if request.limit_price is not None else None,
        "risk": _risk_to_json(request.risk),
        "source": request.source.value,
        "request_id": request.request_id,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }


def _request_from_json(value: object) -> OrderRequest:
    payload = _mapping(value, "order request")
    raw_lots = payload.get("lots")
    raw_limit = payload.get("limit_price")
    raw_created_at = payload.get("created_at")
    return OrderRequest(
        option=_option_from_json(payload.get("option")),
        order_type=OrderType(_text(payload.get("order_type"), "order_type")),
        lots=None if raw_lots is None else _integer(raw_lots, "lots", minimum=1),
        limit_price=(None if raw_limit is None else _positive_decimal(raw_limit, "limit_price")),
        risk=_risk_from_json(payload.get("risk")),
        source=OrderSource(_text(payload.get("source"), "source")),
        request_id=_text(payload.get("request_id"), "request_id"),
        created_at=(None if raw_created_at is None else _datetime(raw_created_at, "created_at")),
    )


def _option_to_json(option: OptionInstrument) -> dict[str, object]:
    return {
        "underlying": option.underlying,
        "expiry": option.expiry.isoformat(),
        "strike": str(option.strike),
        "option_type": option.option_type.value,
        "lot_size": option.lot_size,
        "instrument": {
            "exchange": option.instrument.exchange,
            "token": option.instrument.token,
            "trading_symbol": option.instrument.trading_symbol,
        },
    }


def _option_from_json(value: object) -> OptionInstrument:
    payload = _mapping(value, "option")
    instrument = _mapping(payload.get("instrument"), "instrument")
    return OptionInstrument(
        underlying=_text(payload.get("underlying"), "underlying"),
        expiry=_date(payload.get("expiry"), "expiry"),
        strike=_positive_decimal(payload.get("strike"), "strike"),
        option_type=OptionType(_text(payload.get("option_type"), "option_type")),
        instrument=Instrument(
            exchange=_text(instrument.get("exchange"), "exchange"),
            token=_text(instrument.get("token"), "token"),
            trading_symbol=_text(instrument.get("trading_symbol"), "trading_symbol"),
        ),
        lot_size=_integer(payload.get("lot_size"), "lot_size", minimum=1),
    )


def _risk_to_json(risk: RiskParameters) -> dict[str, str]:
    return {
        "target_percent": str(risk.target_percent),
        "stop_loss_percent": str(risk.stop_loss_percent),
        "trailing_stop_percent": str(risk.trailing_stop_percent),
    }


def _risk_from_json(value: object) -> RiskParameters:
    payload = _mapping(value, "risk")
    return RiskParameters(
        target_percent=_non_negative_decimal(payload.get("target_percent"), "target_percent"),
        stop_loss_percent=_non_negative_decimal(
            payload.get("stop_loss_percent"), "stop_loss_percent"
        ),
        trailing_stop_percent=_non_negative_decimal(
            payload.get("trailing_stop_percent"), "trailing_stop_percent"
        ),
    )


def _execution_to_json(event: ExecutionEvent) -> dict[str, object]:
    return {
        "kind": event.kind.value,
        "order_id": event.order_id,
        "position_id": event.position_id,
        "message": event.message,
        "captured_at": event.captured_at.isoformat(),
        "price": str(event.price) if event.price is not None else None,
        "quantity": event.quantity,
        "realized_pnl": str(event.realized_pnl),
        "exit_reason": event.exit_reason.value if event.exit_reason else None,
        "broker_order_id": event.broker_order_id,
    }


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return tuple(value)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _decimal(value: object, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _non_negative_decimal(value: object, name: str) -> Decimal:
    parsed = _decimal(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def _datetime(value: object, name: str) -> datetime:
    text = _text(value, name)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _date(value: object, name: str) -> date:
    return date.fromisoformat(_text(value, name))
