from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from importlib import import_module
from threading import Lock
from typing import Any

from ktrader_simulator.config import Settings
from ktrader_simulator.domain.models import Instrument, OptionInstrument, OptionType, Quote


class BrokerConnectionError(RuntimeError):
    """Raised when the read-only broker session or quote request fails."""


class AngleOneReadOnlyBroker:
    """Adapter over the existing bot client; deliberately exposes no order API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None
        self._connected = False
        self._io_lock = Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return
        if not self._settings.broker_credentials_configured:
            raise BrokerConnectionError("AngleOne credentials are incomplete")

        client_class = _existing_angleone_client(self._settings)
        client = client_class(self._settings)
        try:
            await asyncio.to_thread(_run_client_call, self._io_lock, client.login)
        except Exception as exc:
            raise BrokerConnectionError(
                f"AngleOne authentication failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._client = client
        self._connected = True

    async def instrument_master(self) -> Sequence[Mapping[str, object]]:
        client = self._require_client()
        try:
            rows = await asyncio.to_thread(
                _run_client_call,
                self._io_lock,
                client.instrument_master,
            )
        except Exception as exc:
            raise BrokerConnectionError(
                f"Instrument master request failed: {type(exc).__name__}: {exc}"
            ) from exc
        return tuple(row for row in rows if isinstance(row, Mapping))

    async def quotes(self, instruments: tuple[Instrument, ...]) -> Mapping[str, Quote]:
        if not instruments:
            return {}
        client = self._require_client()
        exchange_tokens: dict[str, list[str]] = {}
        for instrument in instruments:
            exchange_tokens.setdefault(instrument.exchange, []).append(instrument.token)
        exchange_tokens = {
            exchange: sorted(set(tokens)) for exchange, tokens in exchange_tokens.items()
        }
        try:
            response = await asyncio.to_thread(
                _run_client_call,
                self._io_lock,
                lambda: client.market_quote(
                    mode=self._settings.quote_mode,
                    exchange_tokens=exchange_tokens,
                ),
            )
        except Exception as exc:
            raise BrokerConnectionError(
                f"Quote request failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalize_quotes(response)

    async def implied_volatilities(
        self,
        *,
        underlying: str,
        expiry: date,
        options: tuple[OptionInstrument, ...],
    ) -> Mapping[str, Decimal]:
        if not options:
            return {}
        client = self._require_client()
        params = {
            "name": underlying.upper(),
            "expirydate": expiry.strftime("%d%b%Y").upper(),
        }
        try:
            response = await asyncio.to_thread(
                _run_client_call,
                self._io_lock,
                lambda: client.option_greeks(params),
            )
        except Exception as exc:
            raise BrokerConnectionError(
                f"Option Greek request failed: {type(exc).__name__}: {exc}"
            ) from exc
        return _normalize_implied_volatilities(response, options)

    def _require_client(self) -> Any:
        if self._client is None or not self._connected:
            raise BrokerConnectionError("AngleOne is not connected")
        return self._client


def _existing_angleone_client(settings: Settings) -> Any:
    bot_root = str(settings.bot_root)
    if bot_root not in sys.path:
        sys.path.insert(0, bot_root)
    try:
        module = import_module("app.broker.angleone.client")
        return module.__dict__["AngleOneClient"]
    except (ImportError, KeyError) as exc:
        raise BrokerConnectionError(
            f"Unable to load existing AngleOne client from {settings.bot_root}"
        ) from exc


def _run_client_call(
    io_lock: Lock,
    factory: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    """Run the bot's coroutine wrapper on an I/O worker, not the runtime loop."""

    with io_lock:
        return asyncio.run(factory())


def _normalize_quotes(response: object) -> dict[str, Quote]:
    captured_at = datetime.now(UTC)
    result: dict[str, Quote] = {}
    for payload in _quote_payloads(response):
        token = _token_id(payload)
        if token is None:
            continue
        result[token] = Quote(
            token=token,
            ltp=_decimal(payload.get("ltp") or payload.get("last_traded_price")),
            bid=_decimal(payload.get("bid") or payload.get("best_bid"))
            or _depth_price(payload, "buy"),
            ask=_decimal(payload.get("ask") or payload.get("best_ask"))
            or _depth_price(payload, "sell"),
            captured_at=captured_at,
            volume=_decimal(
                payload.get("volume")
                or payload.get("totalTradedVolume")
                or payload.get("tradeVolume")
            ),
            open_interest=_decimal(
                payload.get("openInterest") or payload.get("opnInterest") or payload.get("oi")
            ),
            implied_volatility=_decimal(
                payload.get("impliedVolatility")
                or payload.get("implied_volatility")
                or payload.get("iv")
            ),
            session_open=_decimal(
                payload.get("open")
                or payload.get("openPrice")
                or payload.get("open_price")
            ),
        )
    return result


def _normalize_implied_volatilities(
    response: object,
    options: tuple[OptionInstrument, ...],
) -> dict[str, Decimal]:
    if not isinstance(response, Mapping) or response.get("status") is False:
        return {}
    rows = response.get("data")
    if not isinstance(rows, list):
        return {}

    by_symbol = {
        option.instrument.trading_symbol.upper(): option for option in options
    }
    by_contract = {
        (option.strike, option.option_type): option for option in options
    }
    result: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        option = by_symbol.get(
            str(
                row.get("tradingSymbol")
                or row.get("tradingsymbol")
                or row.get("symbol")
                or ""
            ).upper()
        )
        if option is None:
            strike = _decimal(
                row.get("strikePrice")
                or row.get("strike_price")
                or row.get("strike")
            )
            option_type = _greek_option_type(row)
            if strike is not None and option_type is not None:
                option = by_contract.get((strike.normalize(), option_type))
        iv = _decimal(
            row.get("impliedVolatility")
            or row.get("implied_volatility")
            or row.get("iv")
        )
        if option is not None and iv is not None and iv > 0:
            result[option.instrument.token] = iv
    return result


def _greek_option_type(row: Mapping[str, object]) -> OptionType | None:
    value = str(
        row.get("optionType") or row.get("option_type") or row.get("type") or ""
    ).upper()
    if value in {"CE", "CALL"}:
        return OptionType.CALL
    if value in {"PE", "PUT"}:
        return OptionType.PUT
    return None


def _quote_payloads(response: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(response, Mapping):
        return ()
    data = response.get("data")
    if isinstance(data, Mapping):
        for key in ("fetched", "data", "quotes", "items"):
            entries = data.get(key)
            if isinstance(entries, list):
                return tuple(item for item in entries if isinstance(item, Mapping))
        return (data,)
    if isinstance(data, list):
        return tuple(item for item in data if isinstance(item, Mapping))
    return (response,)


def _token_id(payload: Mapping[str, object]) -> str | None:
    for key in (
        "token",
        "symbolToken",
        "symbol_token",
        "securityToken",
        "instrument_token",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None


def _depth_price(payload: Mapping[str, object], side: str) -> Decimal | None:
    depth = payload.get("depth")
    if not isinstance(depth, Mapping):
        return None
    levels = depth.get(side)
    if not isinstance(levels, list) or not levels:
        return None
    first = levels[0]
    return _decimal(first.get("price")) if isinstance(first, Mapping) else None


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None
