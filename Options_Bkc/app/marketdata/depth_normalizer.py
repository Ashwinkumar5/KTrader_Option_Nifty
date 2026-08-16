from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.domain.models import MarketDepthLevel, MarketTick, OrderBookSnapshot


def normalize_order_book(tick: MarketTick) -> OrderBookSnapshot | None:
    """Extract a broker-neutral best-five book from a normalized tick's raw payload.

    SmartAPI payload variants use either ``depth.buy/sell`` or best-five named
    arrays. The parser accepts both forms and rejects incomplete books instead of
    manufacturing a signal from a one-sided update.
    """

    payload = tick.raw
    depth = payload.get("depth")
    if isinstance(depth, dict):
        raw_bids = depth.get("buy") or depth.get("bids")
        raw_asks = depth.get("sell") or depth.get("asks")
    else:
        raw_bids = payload.get("best_5_buy_data") or payload.get("bestFiveBuyData")
        raw_asks = payload.get("best_5_sell_data") or payload.get("bestFiveSellData")

    bids = tuple(_levels(raw_bids, reference_price=tick.ltp))
    asks = tuple(_levels(raw_asks, reference_price=tick.ltp))
    if not bids or not asks:
        return None

    return OrderBookSnapshot(token=tick.token, captured_at=tick.received_at, bids=bids, asks=asks)


def _levels(values: object, *, reference_price: Decimal | None) -> Iterable[MarketDepthLevel]:
    if not isinstance(values, list):
        return ()

    levels: list[MarketDepthLevel] = []
    for value in values[:5]:
        if not isinstance(value, dict):
            continue
        price = _price(value.get("price"), reference_price)
        quantity = _integer(value.get("quantity") or value.get("qty"))
        if price is None or quantity is None or quantity <= 0:
            continue
        levels.append(
            MarketDepthLevel(
                price=price,
                quantity=quantity,
                order_count=_integer(value.get("orders") or value.get("num_of_orders")),
            )
        )
    return levels


def _price(value: object, reference_price: Decimal | None) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None

    # WebSocket prices can be paise while REST depth prices are decimal rupees.
    # The tick's already-normalized LTP provides a reliable local discriminator.
    if reference_price is not None and reference_price > 0 and price > reference_price * Decimal("50"):
        price /= Decimal("100")
    return price


def _integer(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
