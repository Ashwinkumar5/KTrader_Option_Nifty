from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import InstrumentToken, MarketTick, TickQuality


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _price_from_payload(payload: dict[str, object], *keys: str) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None or value == "":
            continue
        price = _decimal_or_none(value)
        if price is None:
            return None
        if key in _PAISE_PRICE_KEYS:
            return price / Decimal("100")
        return price
    return None


def _exchange_timestamp(payload: dict[str, object], received: datetime) -> datetime:
    value = (
        payload.get("exchange_timestamp")
        or payload.get("exchangeTimestamp")
        or payload.get("exchFeedTime")
        or payload.get("exchTradeTime")
    )
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    if isinstance(value, str) and value.strip():
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%y %H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return received


def _best_depth_price(payload: dict[str, object], side: str) -> Decimal | None:
    depth = payload.get("depth")
    if not isinstance(depth, dict):
        return None
    levels = depth.get(side)
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    if not isinstance(first, dict):
        return None
    return _decimal_or_none(first.get("price"))


_PAISE_PRICE_KEYS = {
    "last_traded_price",
    "open_price",
    "open_price_of_the_day",
    "high_price",
    "high_price_of_the_day",
    "low_price",
    "low_price_of_the_day",
    "closed_price",
    "close_price",
    "average_traded_price",
    "upper_circuit_limit",
    "lower_circuit_limit",
    "52_week_high_price",
    "52_week_low_price",
}


def normalize_tick(
    *,
    token: InstrumentToken,
    payload: dict[str, object],
    received_at: datetime | None = None,
) -> MarketTick:
    received = received_at or datetime.now(UTC)
    # Keep full timezone-aware datetimes. Converting this to a display-only time
    # string made event ordering and replay impossible and violated MarketTick's
    # declared contract.
    exchange_timestamp = _exchange_timestamp(payload, received)
    return MarketTick(
        token=token,
        exchange_timestamp=exchange_timestamp,
        received_at=received,
        ltp=_price_from_payload(payload, "ltp", "last_traded_price"),
        open_price=_price_from_payload(
            payload,
            "open",
            "open_price",
            "open_price_of_the_day",
        ),
        high_price=_price_from_payload(
            payload,
            "high",
            "high_price",
            "high_price_of_the_day",
        ),
        low_price=_price_from_payload(
            payload,
            "low",
            "low_price",
            "low_price_of_the_day",
        ),
        close_price=_price_from_payload(payload, "close", "close_price", "closed_price"),
        oi=_int_or_none(payload.get("oi") or payload.get("open_interest") or payload.get("opnInterest")),
        oi_change=_int_or_none(
            payload.get("oi_change")
            or payload.get("oiChange")
            or payload.get("change_in_oi")
            or payload.get("changeinOpenInterest")
        ),
        oi_change_percent=_decimal_or_none(
            payload.get("oi_change_percent")
            or payload.get("oiChangePercent")
            or payload.get("pChangeinOpenInterest")
            or payload.get("open_interest_change_percentage")
        ),
        volume=_int_or_none(
            payload.get("volume")
            or payload.get("trade_volume")
            or payload.get("tradeVolume")
            or payload.get("volume_trade_for_the_day")
        ),
        bid=_decimal_or_none(payload.get("bid") or payload.get("best_bid")) or _best_depth_price(payload, "buy"),
        ask=_decimal_or_none(payload.get("ask") or payload.get("best_ask")) or _best_depth_price(payload, "sell"),
        quality=TickQuality.LIVE,
        raw=payload,
    )
