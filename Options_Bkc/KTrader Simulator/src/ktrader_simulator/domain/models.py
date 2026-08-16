from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class OptionType(StrEnum):
    CALL = "CE"
    PUT = "PE"


class Moneyness(StrEnum):
    ITM = "ITM"
    ATM = "ATM"
    OTM = "OTM"


@dataclass(frozen=True, slots=True)
class Instrument:
    exchange: str
    token: str
    trading_symbol: str


@dataclass(frozen=True, slots=True)
class OptionInstrument:
    underlying: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    instrument: Instrument
    lot_size: int


@dataclass(frozen=True, slots=True)
class InstrumentWindow:
    underlying: str
    expiry: date
    spot: Instrument
    atm_strike: Decimal
    strikes: tuple[Decimal, ...]
    calls: tuple[OptionInstrument, ...]
    puts: tuple[OptionInstrument, ...]

    @property
    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(
            option.instrument for pair in zip(self.calls, self.puts, strict=True) for option in pair
        )


@dataclass(frozen=True, slots=True)
class Quote:
    token: str
    ltp: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    captured_at: datetime
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    implied_volatility: Decimal | None = None
    session_open: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ChainRow:
    strike: Decimal
    call: OptionInstrument
    put: OptionInstrument
    call_quote: Quote | None
    put_quote: Quote | None
    call_moneyness: Moneyness
    put_moneyness: Moneyness

    @property
    def strike_label(self) -> str:
        strike = format_strike(self.strike)
        if self.call_moneyness == Moneyness.ATM:
            return f"{strike} (ATM)"
        return f"{strike} CE:{self.call_moneyness.value} PE:{self.put_moneyness.value}"


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    underlying: str
    expiry: date
    spot_price: Decimal
    atm_strike: Decimal
    captured_at: datetime
    rows: tuple[ChainRow, ...]
    india_vix: Decimal | None = None
    india_vix_sod_price: Decimal | None = None
    nifty_price: Decimal | None = None
    nifty_sod_price: Decimal | None = None

    def __post_init__(self) -> None:
        if len(self.rows) != 5:
            raise ValueError("market snapshot must contain exactly five strike rows")


def format_strike(strike: Decimal) -> str:
    text = format(strike, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
