from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.domain.models import GreeksSnapshot, OptionContract, OptionType
from app.instruments.master import normalize_option_strike


def normalize_broker_greeks(
    response: dict[str, object],
    *,
    contracts: tuple[OptionContract, ...],
    captured_at: datetime | None = None,
    source: str = "angleone.optionGreek",
) -> dict[str, GreeksSnapshot]:
    rows = response.get("data")
    if not isinstance(rows, list):
        return {}

    by_symbol = {contract.token.trading_symbol.upper(): contract for contract in contracts}
    by_strike_type = {
        (contract.strike, contract.option_type): contract
        for contract in contracts
    }
    captured = captured_at or datetime.now(UTC)
    snapshots: dict[str, GreeksSnapshot] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        contract = _match_contract(row, by_symbol, by_strike_type)
        if contract is None:
            continue
        snapshots[contract.token.token] = GreeksSnapshot(
            contract=contract,
            captured_at=captured,
            implied_volatility=_decimal(row, "impliedVolatility", "implied_volatility", "iv"),
            delta=_decimal(row, "delta"),
            gamma=_decimal(row, "gamma"),
            theta=_decimal(row, "theta"),
            vega=_decimal(row, "vega"),
            source=source,
        )

    return snapshots


def option_greek_params(*, underlying: str, expiry) -> dict[str, object]:
    return {
        "name": underlying.upper(),
        "expirydate": expiry.strftime("%d%b%Y").upper(),
    }


def _match_contract(
    row: dict[str, object],
    by_symbol: dict[str, OptionContract],
    by_strike_type: dict[tuple[Decimal, OptionType], OptionContract],
) -> OptionContract | None:
    symbol = str(
        row.get("tradingSymbol")
        or row.get("tradingsymbol")
        or row.get("trading_symbol")
        or row.get("symbol")
        or ""
    ).upper()
    if symbol in by_symbol:
        return by_symbol[symbol]

    strike = normalize_option_strike(_decimal(row, "strikePrice", "strike_price", "strike"))
    option_type = _option_type(row)
    if strike is None or option_type is None:
        return None
    return by_strike_type.get((strike, option_type))


def _option_type(row: dict[str, object]) -> OptionType | None:
    value = str(row.get("optionType") or row.get("option_type") or row.get("type") or "").upper()
    if value in {"CE", "CALL"}:
        return OptionType.CALL
    if value in {"PE", "PUT"}:
        return OptionType.PUT
    return None


def _decimal(row: dict[str, object], *keys: str) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None
