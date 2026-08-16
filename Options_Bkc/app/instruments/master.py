from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.domain.models import (
    FutureContract,
    InstrumentToken,
    OptionContract,
    OptionType,
)


@dataclass(frozen=True)
class InstrumentMaster:
    options: tuple[OptionContract, ...]
    spot_tokens: dict[str, InstrumentToken]
    futures: tuple[FutureContract, ...] = ()
    reference_tokens: dict[str, InstrumentToken] = field(default_factory=dict)

    def option_for(
        self,
        *,
        underlying: str,
        expiry: date,
        strike: Decimal,
        option_type: OptionType,
    ) -> OptionContract | None:
        for contract in self.options:
            if (
                contract.underlying == underlying
                and contract.expiry == expiry
                and contract.strike == strike
                and contract.option_type == option_type
            ):
                return contract
        return None

    def nearest_future(
        self,
        *,
        underlying: str,
        as_of: date,
    ) -> FutureContract | None:
        eligible = tuple(
            contract
            for contract in self.futures
            if contract.underlying == underlying
            and contract.expiry >= as_of
        )
        return min(eligible, key=lambda item: item.expiry) if eligible else None


def available_expiries(contracts: Iterable[OptionContract], underlying: str) -> tuple[date, ...]:
    return tuple(
        sorted({contract.expiry for contract in contracts if contract.underlying == underlying})
    )


def normalize_option_strike(value: object) -> Decimal | None:
    try:
        strike = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if strike > Decimal("100000"):
        strike = strike / Decimal("100")
    return strike.quantize(Decimal("0.01")).normalize()
