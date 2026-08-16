from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ktrader_simulator.broker.angleone import _existing_angleone_client
from ktrader_simulator.config import Settings
from ktrader_simulator.trading.models import OrderRequest, OrderType, Position


class LiveOrderError(RuntimeError):
    """Raised when an explicitly enabled broker order is not acknowledged."""


class AngleOneLiveOrderRouter:
    """Narrow SmartAPI order adapter, constructed only behind the live-order flag."""

    def __init__(self, settings: Settings, *, smart_api: Any | None = None) -> None:
        self._settings = settings
        self._smart_api = smart_api

    async def connect(self) -> None:
        if self._smart_api is not None:
            return
        client_class = _existing_angleone_client(self._settings)
        client = client_class(self._settings)
        await client.login()
        smart_api = getattr(client, "_smart_api", None)
        if smart_api is None:
            raise LiveOrderError("AngleOne live-order session was not initialized")
        self._smart_api = smart_api

    async def place_entry(self, request: OrderRequest, *, lots: int) -> str:
        if lots <= 0:
            raise LiveOrderError("Live order lots must be positive")
        price = request.limit_price if request.order_type == OrderType.LIMIT else None
        return await self._place(
            trading_symbol=request.option.instrument.trading_symbol,
            symbol_token=request.option.instrument.token,
            exchange=request.option.instrument.exchange,
            transaction_type="BUY",
            order_type=request.order_type,
            quantity=lots * request.option.lot_size,
            price=price,
            order_tag=_order_tag(request.request_id),
        )

    async def exit_position(self, position: Position) -> str:
        return await self._place(
            trading_symbol=position.option.instrument.trading_symbol,
            symbol_token=position.option.instrument.token,
            exchange=position.option.instrument.exchange,
            transaction_type="SELL",
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            price=None,
            order_tag=_order_tag(f"exit-{position.position_id}"),
        )

    async def cancel_order(self, broker_order_id: str) -> str:
        normalized = broker_order_id.strip()
        if not normalized:
            raise LiveOrderError("Broker order ID is required for cancellation")
        smart_api = self._require_smart_api()
        try:
            response = await asyncio.to_thread(
                smart_api.cancelOrder,
                normalized,
                self._settings.broker_order_variety,
            )
        except Exception as exc:
            raise LiveOrderError(
                f"AngleOne cancellation failed: {type(exc).__name__}: {exc}"
            ) from exc
        if isinstance(response, Mapping) and response.get("status") is True:
            return _order_id(response) or normalized
        raise LiveOrderError(f"AngleOne did not cancel the order: {_response_message(response)}")

    async def available_balance(self) -> Decimal | None:
        smart_api = self._require_smart_api()
        response = await asyncio.to_thread(smart_api.rmsLimit)
        if not isinstance(response, Mapping):
            return None
        data = response.get("data")
        if not isinstance(data, Mapping):
            return None
        for key in ("availablecash", "availableCash", "net"):
            parsed = _decimal(data.get(key))
            if parsed is not None:
                return parsed
        return None

    async def _place(
        self,
        *,
        trading_symbol: str,
        symbol_token: str,
        exchange: str,
        transaction_type: str,
        order_type: OrderType,
        quantity: int,
        price: Decimal | None,
        order_tag: str,
    ) -> str:
        smart_api = self._require_smart_api()
        payload = {
            "variety": self._settings.broker_order_variety,
            "tradingsymbol": trading_symbol,
            "symboltoken": symbol_token,
            "transactiontype": transaction_type,
            "exchange": exchange,
            "ordertype": order_type.value,
            "producttype": self._settings.broker_order_product_type,
            "duration": self._settings.broker_order_duration,
            "price": str(price) if price is not None else "0",
            "triggerprice": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
            "ordertag": order_tag,
        }
        try:
            response = await asyncio.to_thread(
                smart_api.placeOrderFullResponse,
                payload,
            )
        except Exception as exc:
            reconciled = await self._reconcile_order(order_tag)
            if reconciled is not None:
                return reconciled
            raise LiveOrderError(
                f"AngleOne order request failed: {type(exc).__name__}: {exc}"
            ) from exc
        order_id = _order_id(response)
        if order_id is None:
            message = _response_message(response)
            raise LiveOrderError(f"AngleOne did not acknowledge the order: {message}")
        return order_id

    async def _reconcile_order(self, order_tag: str) -> str | None:
        smart_api = self._require_smart_api()
        try:
            response = await asyncio.to_thread(smart_api.orderBook)
        except Exception:
            return None
        if not isinstance(response, Mapping):
            return None
        data = response.get("data")
        if not isinstance(data, list):
            return None
        for value in reversed(data):
            if not isinstance(value, Mapping):
                continue
            if str(value.get("ordertag") or "") != order_tag:
                continue
            order_id = value.get("orderid")
            if isinstance(order_id, (str, int)) and str(order_id).strip():
                return str(order_id)
        return None

    def _require_smart_api(self) -> Any:
        if self._smart_api is None:
            raise LiveOrderError("AngleOne live-order router is not connected")
        return self._smart_api


def _order_tag(request_id: str) -> str:
    compact = "".join(character for character in request_id if character.isalnum())
    return f"KTS{compact}"[:20]


def _order_id(response: object) -> str | None:
    if isinstance(response, (str, int)) and str(response).strip():
        return str(response)
    if not isinstance(response, Mapping) or response.get("status") is not True:
        return None
    data = response.get("data")
    if not isinstance(data, Mapping):
        return None
    value = data.get("orderid") or data.get("uniqueorderid")
    return str(value) if isinstance(value, (str, int)) and str(value).strip() else None


def _response_message(response: object) -> str:
    if isinstance(response, Mapping):
        for key in ("message", "errorcode", "text"):
            value = response.get(key)
            if value not in (None, ""):
                return str(value)
    return "empty or invalid response"


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None
