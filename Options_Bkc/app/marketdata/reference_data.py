from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class DailyCandle:
    session_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal


def normalize_daily_candles(
    response: object,
    *,
    before_date: date,
) -> tuple[DailyCandle, ...]:
    rows = response.get("data") if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return ()
    by_date: dict[date, DailyCandle] = {}
    for row in rows:
        candle = _normalize_candle(row)
        if candle is not None and candle.session_date < before_date:
            by_date[candle.session_date] = candle
    return tuple(by_date[key] for key in sorted(by_date))


def calculate_previous_atr(
    candles: tuple[DailyCandle, ...],
    *,
    periods: int = 20,
) -> Decimal | None:
    if periods <= 0:
        raise ValueError("periods must be positive")
    if len(candles) < periods + 1:
        return None
    selected = candles[-(periods + 1) :]
    true_ranges: list[Decimal] = []
    for previous, current in zip(selected, selected[1:]):
        true_ranges.append(
            max(
                current.high_price - current.low_price,
                abs(current.high_price - previous.close_price),
                abs(current.low_price - previous.close_price),
            )
        )
    return (
        sum(true_ranges, Decimal("0")) / Decimal(periods)
    ).quantize(Decimal("0.01"))


def extract_ltp(response: object) -> Decimal | None:
    if isinstance(response, dict):
        for key in ("ltp", "last_traded_price", "lastTradedPrice"):
            value = _decimal(response.get(key))
            if value is not None and value > 0:
                return value
        for key in ("data", "fetched"):
            value = extract_ltp(response.get(key))
            if value is not None:
                return value
        for value in response.values():
            nested = extract_ltp(value)
            if nested is not None:
                return nested
    elif isinstance(response, list):
        for item in response:
            value = extract_ltp(item)
            if value is not None:
                return value
    return None


def _normalize_candle(value: object) -> DailyCandle | None:
    if isinstance(value, (list, tuple)) and len(value) >= 5:
        timestamp, open_price, high_price, low_price, close_price = value[:5]
    elif isinstance(value, dict):
        timestamp = (
            value.get("timestamp")
            or value.get("time")
            or value.get("date")
        )
        open_price = value.get("open")
        high_price = value.get("high")
        low_price = value.get("low")
        close_price = value.get("close")
    else:
        return None
    session_date = _date(timestamp)
    open_value = _decimal(open_price)
    high_value = _decimal(high_price)
    low_value = _decimal(low_price)
    close_value = _decimal(close_price)
    if (
        session_date is None
        or open_value is None
        or high_value is None
        or low_value is None
        or close_value is None
        or min(open_value, high_value, low_value, close_value) <= 0
        or high_value < low_value
    ):
        return None
    return DailyCandle(
        session_date=session_date,
        open_price=open_value,
        high_price=high_value,
        low_price=low_value,
        close_price=close_value,
    )


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None
