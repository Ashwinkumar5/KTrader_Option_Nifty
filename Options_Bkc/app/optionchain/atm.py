from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.domain.models import OptionContract, OptionType
from app.instruments.master import InstrumentMaster, normalize_option_strike


def round_to_nearest_strike(spot_price: Decimal, strike_interval: Decimal) -> Decimal:
    if strike_interval <= 0:
        raise ValueError("strike_interval must be positive")

    normalized_spot = normalize_option_strike(spot_price)
    if normalized_spot is None:
        raise ValueError("spot_price must be numeric")

    units = (normalized_spot / strike_interval).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * strike_interval


def build_strike_window(
    *,
    atm_strike: Decimal,
    strike_interval: Decimal,
    each_side: int,
) -> tuple[Decimal, ...]:
    if each_side < 0:
        raise ValueError("each_side cannot be negative")
    return tuple(
        atm_strike + (Decimal(offset) * strike_interval)
        for offset in range(-each_side, each_side + 1)
    )


def strike_interval_for_underlying(underlying: str) -> Decimal:
    normalized = underlying.upper()
    if normalized == "NIFTY":
        return Decimal("50")
    if normalized == "BANKNIFTY":
        return Decimal("100")
    raise ValueError(f"Unsupported underlying: {underlying}")


def select_option_window(
    *,
    master: InstrumentMaster,
    underlying: str,
    expiry,
    spot_price: Decimal,
    each_side: int = 4,
) -> tuple[Decimal, tuple[OptionContract, ...]]:
    interval = strike_interval_for_underlying(underlying)
    atm = round_to_nearest_strike(spot_price, interval)
    strike_levels = build_strike_window(atm_strike=atm, strike_interval=interval, each_side=each_side)

    selected: list[OptionContract] = []
    for strike in strike_levels:
        normalized_strike = normalize_option_strike(strike)
        if normalized_strike is None:
            continue

        for option_type in (OptionType.CALL, OptionType.PUT):
            contract = master.option_for(
                underlying=underlying,
                expiry=expiry,
                strike=normalized_strike,
                option_type=option_type,
            )
            if contract is not None:
                selected.append(contract)

    return atm, tuple(selected)
