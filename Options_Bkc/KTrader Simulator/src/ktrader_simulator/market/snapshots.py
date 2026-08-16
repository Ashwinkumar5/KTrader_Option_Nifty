from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ktrader_simulator.broker.protocols import ReadOnlyBroker
from ktrader_simulator.config import Settings
from ktrader_simulator.domain.models import (
    ChainRow,
    Instrument,
    InstrumentWindow,
    MarketSnapshot,
    Moneyness,
    OptionInstrument,
    OptionType,
    Quote,
)
from ktrader_simulator.market.instruments import InstrumentCatalog


class MarketDataError(RuntimeError):
    """Raised when a complete dashboard snapshot cannot be built."""


class MarketSnapshotService:
    def __init__(
        self,
        *,
        broker: ReadOnlyBroker,
        catalog: InstrumentCatalog,
        settings: Settings,
    ) -> None:
        self._broker = broker
        self._catalog = catalog
        self._settings = settings
        self._windows: dict[str, InstrumentWindow] = {}
        self._india_vix = catalog.reference_for("INDIA_VIX")
        self._nifty = (
            catalog.spot_for("NIFTY") if "NIFTY" in catalog.available_indices else None
        )

    @classmethod
    async def create(
        cls,
        *,
        broker: ReadOnlyBroker,
        settings: Settings,
    ) -> MarketSnapshotService:
        rows = await broker.instrument_master()
        catalog = InstrumentCatalog.from_rows(
            rows,
            supported_indices=settings.supported_indices,
        )
        if not catalog.available_indices:
            raise MarketDataError("Instrument master contains no supported index chains")
        return cls(broker=broker, catalog=catalog, settings=settings)

    @property
    def available_indices(self) -> tuple[str, ...]:
        return self._catalog.available_indices

    @property
    def instrument_count(self) -> int:
        return self._catalog.option_count

    async def snapshot(self, underlying: str) -> MarketSnapshot:
        normalized = underlying.upper()
        cached_window = self._windows.get(normalized)
        if cached_window is None:
            spot_instrument = self._catalog.spot_for(normalized)
            spot_quotes = await self._broker.quotes((spot_instrument,))
            spot_price = _required_ltp(spot_instrument, spot_quotes)
            window = self._resolve_window(normalized, spot_price)
            option_quotes = await self._broker.quotes(
                _with_optional_references(
                    window.instruments,
                    (self._india_vix, self._nifty),
                    excluded_tokens=frozenset(spot_quotes),
                )
            )
            quotes = {**spot_quotes, **option_quotes}
        else:
            requested = _with_optional_references(
                (cached_window.spot, *cached_window.instruments),
                (self._india_vix, self._nifty),
            )
            quotes = dict(await self._broker.quotes(requested))
            spot_price = _required_ltp(cached_window.spot, quotes)
            window = self._resolve_window(normalized, spot_price)
            if _window_identity(window) != _window_identity(cached_window):
                quotes.update(await self._broker.quotes(window.instruments))

        self._windows[normalized] = window
        spot_price = _required_ltp(window.spot, quotes)
        captured_at = datetime.now(UTC)
        rows = tuple(
            _chain_row(
                strike=strike,
                atm_strike=window.atm_strike,
                call=call,
                put=put,
                quotes=quotes,
            )
            for strike, call, put in zip(
                window.strikes,
                window.calls,
                window.puts,
                strict=True,
            )
        )
        return MarketSnapshot(
            underlying=normalized,
            expiry=window.expiry,
            spot_price=spot_price,
            atm_strike=window.atm_strike,
            captured_at=captured_at,
            rows=rows,
            india_vix=_optional_ltp(self._india_vix, quotes),
            india_vix_sod_price=_optional_open(self._india_vix, quotes),
            nifty_price=_optional_ltp(self._nifty, quotes),
            nifty_sod_price=_optional_open(self._nifty, quotes),
        )

    async def quotes(self, instruments: tuple[Instrument, ...]) -> Mapping[str, Quote]:
        """Fetch quotes for positions that moved outside the visible five strikes."""

        return await self._broker.quotes(instruments)

    async def with_implied_volatilities(
        self,
        snapshot: MarketSnapshot,
    ) -> MarketSnapshot:
        """Enrich only the slow analytics snapshot; never touch the quote hot path."""

        if not self._settings.option_greeks_enabled:
            return snapshot
        options = tuple(
            option
            for row in snapshot.rows
            for option in (row.call, row.put)
        )
        values = await self._broker.implied_volatilities(
            underlying=snapshot.underlying,
            expiry=snapshot.expiry,
            options=options,
        )
        if not values:
            return snapshot

        def enriched_quote(quote: Quote | None, option: OptionInstrument) -> Quote | None:
            iv = values.get(option.instrument.token)
            return quote if quote is None or iv is None else replace(
                quote,
                implied_volatility=iv,
            )

        return replace(
            snapshot,
            rows=tuple(
                replace(
                    row,
                    call_quote=enriched_quote(row.call_quote, row.call),
                    put_quote=enriched_quote(row.put_quote, row.put),
                )
                for row in snapshot.rows
            ),
        )

    def option_for_token(self, token: str) -> OptionInstrument | None:
        return self._catalog.option_for_token(token)

    def option_for_contract(
        self,
        *,
        underlying: str,
        strike: Decimal,
        option_type: OptionType,
    ) -> OptionInstrument | None:
        market_date = datetime.now(ZoneInfo(self._settings.market_timezone)).date()
        return self._catalog.option_for_contract(
            underlying=underlying,
            strike=strike,
            option_type=option_type,
            as_of=market_date,
        )

    def _resolve_window(self, underlying: str, spot_price: Decimal) -> InstrumentWindow:
        market_date = datetime.now(ZoneInfo(self._settings.market_timezone)).date()
        return self._catalog.window(
            underlying=underlying,
            spot_price=spot_price,
            as_of=market_date,
        )


def _required_ltp(
    instrument: Instrument,
    quotes: Mapping[str, Quote],
) -> Decimal:
    quote = quotes.get(instrument.token)
    if quote is None or quote.ltp is None or quote.ltp <= 0:
        raise MarketDataError(
            f"No valid LTP returned for {instrument.trading_symbol} ({instrument.token})"
        )
    return quote.ltp


def _optional_ltp(
    instrument: Instrument | None,
    quotes: Mapping[str, Quote],
) -> Decimal | None:
    if instrument is None:
        return None
    quote = quotes.get(instrument.token)
    if quote is None or quote.ltp is None or quote.ltp <= 0:
        return None
    return quote.ltp


def _optional_open(
    instrument: Instrument | None,
    quotes: Mapping[str, Quote],
) -> Decimal | None:
    if instrument is None:
        return None
    quote = quotes.get(instrument.token)
    if quote is None or quote.session_open is None or quote.session_open <= 0:
        return None
    return quote.session_open


def _with_optional_references(
    instruments: tuple[Instrument, ...],
    references: tuple[Instrument | None, ...],
    *,
    excluded_tokens: frozenset[str] = frozenset(),
) -> tuple[Instrument, ...]:
    requested = list(instruments)
    seen = {item.token for item in requested} | excluded_tokens
    for reference in references:
        if reference is not None and reference.token not in seen:
            requested.append(reference)
            seen.add(reference.token)
    return tuple(requested)


def _window_identity(window: InstrumentWindow) -> tuple[object, ...]:
    return window.expiry, window.atm_strike, window.strikes


def _chain_row(
    *,
    strike: Decimal,
    atm_strike: Decimal,
    call: OptionInstrument,
    put: OptionInstrument,
    quotes: Mapping[str, Quote],
) -> ChainRow:
    if strike == atm_strike:
        call_moneyness = put_moneyness = Moneyness.ATM
    elif strike < atm_strike:
        call_moneyness = Moneyness.ITM
        put_moneyness = Moneyness.OTM
    else:
        call_moneyness = Moneyness.OTM
        put_moneyness = Moneyness.ITM
    return ChainRow(
        strike=strike,
        call=call,
        put=put,
        call_quote=quotes.get(call.instrument.token),
        put_quote=quotes.get(put.instrument.token),
        call_moneyness=call_moneyness,
        put_moneyness=put_moneyness,
    )
