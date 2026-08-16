from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ktrader_simulator.domain.models import (
    Instrument,
    InstrumentWindow,
    OptionInstrument,
    OptionType,
)


class InstrumentCatalogError(RuntimeError):
    """Raised when the broker instrument master cannot satisfy a selection."""


_EXPIRY_FORMATS = ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d")
_ALIASES: dict[str, frozenset[str]] = {
    "NIFTY": frozenset({"NIFTY", "NIFTY50"}),
    "BANKNIFTY": frozenset({"BANKNIFTY", "NIFTYBANK"}),
    "SENSEX": frozenset({"SENSEX"}),
    "BANKEX": frozenset({"BANKEX"}),
}
_SPOT_EXCHANGE = {
    "NIFTY": "NSE",
    "BANKNIFTY": "NSE",
    "SENSEX": "BSE",
    "BANKEX": "BSE",
}


class InstrumentCatalog:
    def __init__(
        self,
        *,
        spots: Mapping[str, Instrument],
        references: Mapping[str, Instrument],
        options: Iterable[OptionInstrument],
    ) -> None:
        self._spots = dict(spots)
        self._references = dict(references)
        self._options: dict[tuple[str, date, Decimal, OptionType], OptionInstrument] = {}
        self._strikes: dict[tuple[str, date], set[Decimal]] = defaultdict(set)
        self._expiries: dict[str, set[date]] = defaultdict(set)
        self._options_by_token: dict[str, OptionInstrument] = {}

        for option in options:
            key = (
                option.underlying,
                option.expiry,
                option.strike,
                option.option_type,
            )
            self._options[key] = option
            self._options_by_token[option.instrument.token] = option
            self._strikes[(option.underlying, option.expiry)].add(option.strike)
            self._expiries[option.underlying].add(option.expiry)

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[Mapping[str, object]],
        *,
        supported_indices: tuple[str, ...],
    ) -> InstrumentCatalog:
        wanted = frozenset(supported_indices)
        spots: dict[str, Instrument] = {}
        references: dict[str, Instrument] = {}
        options: list[OptionInstrument] = []

        for row in rows:
            exchange = _exchange(row.get("exch_seg") or row.get("exchange"))
            if exchange is None:
                continue
            trading_symbol = str(row.get("symbol") or row.get("tradingsymbol") or "").strip()
            token = str(row.get("token") or row.get("symboltoken") or "").strip()
            if not trading_symbol or not token:
                continue

            name = str(row.get("name") or "").strip()
            reference_name = _reference_name(name, trading_symbol)
            if reference_name is not None:
                references.setdefault(
                    reference_name,
                    Instrument(
                        exchange=exchange,
                        token=token,
                        trading_symbol=trading_symbol,
                    ),
                )
                continue
            underlying = _underlying_for(name, trading_symbol, wanted)
            if underlying is None:
                continue

            option_type = _option_type(row, trading_symbol)
            if option_type is None:
                if exchange == _SPOT_EXCHANGE[underlying]:
                    spots.setdefault(
                        underlying,
                        Instrument(
                            exchange=exchange,
                            token=token,
                            trading_symbol=trading_symbol,
                        ),
                    )
                continue

            expiry = _parse_expiry(row.get("expiry"))
            strike = _parse_strike(row.get("strike"))
            lot_size = _parse_positive_int(row.get("lotsize") or row.get("lot_size"))
            if expiry is None or strike is None or lot_size is None:
                continue
            options.append(
                OptionInstrument(
                    underlying=underlying,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    instrument=Instrument(
                        exchange=exchange,
                        token=token,
                        trading_symbol=trading_symbol,
                    ),
                    lot_size=lot_size,
                )
            )

        return cls(spots=spots, references=references, options=options)

    @property
    def option_count(self) -> int:
        return len(self._options)

    @property
    def available_indices(self) -> tuple[str, ...]:
        return tuple(
            underlying
            for underlying in _ALIASES
            if underlying in self._spots and self._expiries.get(underlying)
        )

    def spot_for(self, underlying: str) -> Instrument:
        normalized = underlying.upper()
        try:
            return self._spots[normalized]
        except KeyError as exc:
            raise InstrumentCatalogError(f"No spot instrument found for {normalized}") from exc

    def reference_for(self, name: str) -> Instrument | None:
        return self._references.get(name.upper())

    def option_for_token(self, token: str) -> OptionInstrument | None:
        return self._options_by_token.get(token)

    def option_for_contract(
        self,
        *,
        underlying: str,
        strike: Decimal,
        option_type: OptionType,
        as_of: date,
    ) -> OptionInstrument | None:
        normalized = underlying.upper()
        expiries = sorted(
            expiry for expiry in self._expiries.get(normalized, ()) if expiry >= as_of
        )
        if not expiries:
            return None
        return self._options.get((normalized, expiries[0], strike, option_type))

    def window(
        self,
        *,
        underlying: str,
        spot_price: Decimal,
        as_of: date,
    ) -> InstrumentWindow:
        normalized = underlying.upper()
        spot = self.spot_for(normalized)
        expiries = sorted(
            expiry for expiry in self._expiries.get(normalized, ()) if expiry >= as_of
        )
        if not expiries:
            raise InstrumentCatalogError(f"No active option expiry found for {normalized}")
        expiry = expiries[0]
        strikes = sorted(
            strike
            for strike in self._strikes[(normalized, expiry)]
            if self._has_pair(normalized, expiry, strike)
        )
        if len(strikes) < 5:
            raise InstrumentCatalogError(
                f"Fewer than five paired strikes found for {normalized} {expiry}"
            )

        atm = min(strikes, key=lambda strike: (abs(strike - spot_price), -strike))
        atm_index = strikes.index(atm)
        if atm_index < 2 or atm_index + 2 >= len(strikes):
            raise InstrumentCatalogError(f"ATM strike {atm} does not have two strikes on each side")
        selected_strikes = tuple(strikes[atm_index - 2 : atm_index + 3])
        calls = tuple(
            self._options[(normalized, expiry, strike, OptionType.CALL)]
            for strike in selected_strikes
        )
        puts = tuple(
            self._options[(normalized, expiry, strike, OptionType.PUT)]
            for strike in selected_strikes
        )
        return InstrumentWindow(
            underlying=normalized,
            expiry=expiry,
            spot=spot,
            atm_strike=atm,
            strikes=selected_strikes,
            calls=calls,
            puts=puts,
        )

    def _has_pair(self, underlying: str, expiry: date, strike: Decimal) -> bool:
        return all(
            (underlying, expiry, strike, option_type) in self._options for option_type in OptionType
        )


def _exchange(value: object) -> str | None:
    normalized = str(value or "").strip().upper()
    return {
        "NSE_CM": "NSE",
        "NSE_FO": "NFO",
        "BSE_CM": "BSE",
        "BSE_FO": "BFO",
    }.get(normalized, normalized if normalized in {"NSE", "NFO", "BSE", "BFO"} else None)


def _underlying_for(
    name: str,
    trading_symbol: str,
    wanted: frozenset[str],
) -> str | None:
    normalized_name = _compact(name)
    normalized_symbol = _compact(trading_symbol)
    for underlying in sorted(wanted, key=len, reverse=True):
        aliases = _ALIASES.get(underlying, frozenset({underlying}))
        if normalized_name in aliases or normalized_symbol in aliases:
            return underlying
        if normalized_symbol.startswith(underlying):
            remainder = normalized_symbol[len(underlying) :]
            if remainder and remainder[0].isdigit():
                return underlying
    return None


def _compact(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _reference_name(name: str, trading_symbol: str) -> str | None:
    if _compact(name) in {"INDIAVIX", "INDIAVIXINDEX"} or _compact(trading_symbol) in {
        "INDIAVIX",
        "INDIAVIXINDEX",
    }:
        return "INDIA_VIX"
    return None


def _option_type(
    row: Mapping[str, object],
    trading_symbol: str,
) -> OptionType | None:
    raw_type = str(
        row.get("optiontype")
        or row.get("option_type")
        or row.get("instrumenttype")
        or row.get("instrument_type")
        or ""
    ).upper()
    symbol = trading_symbol.upper()
    if raw_type in {"CE", "CALL"} or symbol.endswith("CE"):
        return OptionType.CALL
    if raw_type in {"PE", "PUT"} or symbol.endswith("PE"):
        return OptionType.PUT
    return None


def _parse_expiry(value: object) -> date | None:
    raw_value = str(value or "").strip().upper()
    for date_format in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(raw_value, date_format).date()
        except ValueError:
            continue
    return None


def _parse_strike(value: object) -> Decimal | None:
    try:
        strike = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not strike.is_finite() or strike <= 0:
        return None
    if strike > Decimal("100000"):
        strike /= Decimal("100")
    return strike.quantize(Decimal("0.01")).normalize()


def _parse_positive_int(value: object) -> int | None:
    try:
        parsed = int(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None
