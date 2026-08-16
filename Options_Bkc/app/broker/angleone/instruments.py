from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from app.domain.models import (
    Exchange,
    FutureContract,
    InstrumentKind,
    InstrumentToken,
    OptionContract,
    OptionType,
)
from app.instruments.master import InstrumentMaster, normalize_option_strike


_EXPIRY_FORMATS = ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d")


def build_instrument_master(
    rows: list[dict[str, object]],
    *,
    underlyings: tuple[str, ...],
) -> InstrumentMaster:
    wanted = tuple(symbol.upper() for symbol in underlyings)
    options: list[OptionContract] = []
    futures: list[FutureContract] = []
    spot_tokens: dict[str, InstrumentToken] = {}
    reference_tokens: dict[str, InstrumentToken] = {}

    for row in rows:
        exchange = _exchange(row.get("exch_seg") or row.get("exchange"))
        if exchange is None:
            continue

        trading_symbol = str(row.get("symbol") or row.get("tradingsymbol") or "")
        name = str(row.get("name") or row.get("symbol") or "").upper()
        reference_name = _reference_name(name, trading_symbol)
        if reference_name is not None:
            token = InstrumentToken(
                exchange=exchange,
                token=str(row.get("token") or row.get("symboltoken") or ""),
                symbol=reference_name,
                trading_symbol=trading_symbol,
                kind=InstrumentKind.INDEX,
            )
            if token.token and token.trading_symbol:
                reference_tokens.setdefault(reference_name, token)
            continue
        underlying = _underlying_for(name, trading_symbol, wanted)
        if underlying is None:
            continue

        token = InstrumentToken(
            exchange=exchange,
            token=str(row.get("token") or row.get("symboltoken") or ""),
            symbol=underlying,
            trading_symbol=trading_symbol,
            kind=_instrument_kind(row),
        )
        if not token.token or not token.trading_symbol:
            continue

        if token.kind == InstrumentKind.FUTURE:
            expiry = _parse_expiry(row.get("expiry"))
            if expiry is not None:
                futures.append(
                    FutureContract(
                        underlying=underlying,
                        expiry=expiry,
                        token=token,
                        lot_size=_parse_int(
                            row.get("lotsize") or row.get("lot_size")
                        ),
                    )
                )
            continue

        option_type = _option_type(row, trading_symbol)
        if option_type is None:
            if token.kind == InstrumentKind.INDEX:
                spot_tokens.setdefault(underlying, token)
            continue

        expiry = _parse_expiry(row.get("expiry"))
        strike = normalize_option_strike(row.get("strike"))
        if expiry is None or strike is None:
            continue

        options.append(
            OptionContract(
                underlying=underlying,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
                token=InstrumentToken(
                    exchange=token.exchange,
                    token=token.token,
                    symbol=token.symbol,
                    trading_symbol=token.trading_symbol,
                    kind=InstrumentKind.OPTION,
                ),
                lot_size=_parse_int(row.get("lotsize") or row.get("lot_size")),
            )
        )

    return InstrumentMaster(
        options=tuple(options),
        spot_tokens=spot_tokens,
        futures=tuple(futures),
        reference_tokens=reference_tokens,
    )


def _exchange(value: object) -> Exchange | None:
    text = str(value or "").upper()
    if text in {"NSE", "NSE_CM"}:
        return Exchange.NSE
    if text in {"NFO", "NSE_FO"}:
        return Exchange.NFO
    return None


def _instrument_kind(row: dict[str, object]) -> InstrumentKind | None:
    instrument_type = str(row.get("instrumenttype") or row.get("instrument_type") or "").upper()
    exchange = str(row.get("exch_seg") or row.get("exchange") or "").upper()
    if instrument_type in {"OPTIDX", "OPTSTK", "CE", "PE"}:
        return InstrumentKind.OPTION
    if instrument_type in {"FUTIDX", "FUTSTK"}:
        return InstrumentKind.FUTURE
    if exchange in {"NSE", "NSE_CM"}:
        return InstrumentKind.INDEX
    return None


def _underlying_for(name: str, trading_symbol: str, underlyings: tuple[str, ...]) -> str | None:
    normalized_name = re.sub(r"[^A-Z0-9]", "", name.upper())
    symbol = trading_symbol.upper()
    for underlying in sorted(underlyings, key=len, reverse=True):
        normalized_underlying = re.sub(r"[^A-Z0-9]", "", underlying.upper())
        if normalized_name in {normalized_underlying, f"{normalized_underlying}50"}:
            return underlying
        if symbol.startswith(underlying):
            remainder = symbol[len(underlying):]
            # Contract symbols continue with an expiry digit. This boundary check
            # prevents NIFTY from matching FINNIFTY or BANKNIFTY.
            if not remainder or not remainder[0].isalpha():
                return underlying
        if symbol == underlying:
            return underlying
    return None


def _reference_name(name: str, trading_symbol: str) -> str | None:
    normalized_name = re.sub(r"[^A-Z0-9]", "", name.upper())
    normalized_symbol = re.sub(r"[^A-Z0-9]", "", trading_symbol.upper())
    if normalized_name in {"INDIAVIX", "INDIAVIXINDEX"} or normalized_symbol in {
        "INDIAVIX",
        "INDIAVIXINDEX",
    }:
        return "INDIA_VIX"
    return None


def _option_type(row: dict[str, object], trading_symbol: str) -> OptionType | None:
    option_type = str(
        row.get("optiontype")
        or row.get("option_type")
        or row.get("instrumenttype")
        or row.get("instrument_type")
        or ""
    ).upper()
    symbol = trading_symbol.upper()
    if option_type in {"CE", "CALL"} or symbol.endswith("CE"):
        return OptionType.CALL
    if option_type in {"PE", "PUT"} or symbol.endswith("PE"):
        return OptionType.PUT
    return None


def _parse_expiry(value: object) -> date | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    for expiry_format in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, expiry_format).date()
        except ValueError:
            continue
    return None


def _parse_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(Decimal(str(value)))
