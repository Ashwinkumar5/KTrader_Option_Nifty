from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.models import (
    GreeksSnapshot,
    MarketTick,
    OptionChainSnapshot,
    OptionQuote,
    UnderlyingMarketSnapshot,
)
from app.instruments.master import InstrumentMaster
from app.optionchain.atm import select_option_window


class OptionChainState:
    def __init__(self, *, master: InstrumentMaster) -> None:
        self._master = master
        self._latest_by_token: dict[str, MarketTick] = {}
        self._greeks_by_token: dict[str, GreeksSnapshot] = {}
        self._previous_20d_atr: dict[str, Decimal] = {}
        self._reference_values: dict[str, Decimal] = {}

    def update_tick(self, tick: MarketTick) -> None:
        current = self._latest_by_token.get(tick.token.token)
        if current is not None and (
            tick.exchange_timestamp < current.exchange_timestamp
            or (
                tick.exchange_timestamp == current.exchange_timestamp
                and tick.received_at < current.received_at
            )
        ):
            return
        self._latest_by_token[tick.token.token] = tick

    def reset_session(self) -> None:
        """Clear live observations at a recorded process restart."""

        self._latest_by_token.clear()
        self._greeks_by_token.clear()

    def latest_tick(self, token: str) -> MarketTick | None:
        return self._latest_by_token.get(token)

    def update_greeks(self, greeks_by_token: dict[str, GreeksSnapshot]) -> None:
        self._greeks_by_token.update(greeks_by_token)

    def set_previous_20d_atr(
        self,
        underlying: str,
        value: Decimal,
    ) -> None:
        if value > 0:
            self._previous_20d_atr[underlying.upper()] = value

    def set_reference_value(
        self,
        name: str,
        value: Decimal,
    ) -> None:
        if value > 0:
            self._reference_values[name.upper()] = value

    def build_snapshot(
        self,
        *,
        underlying: str,
        expiry: date,
        spot_price: Decimal,
        each_side: int,
        captured_at: datetime | None = None,
        market: UnderlyingMarketSnapshot | None = None,
    ) -> OptionChainSnapshot:
        atm, contracts = select_option_window(
            master=self._master,
            underlying=underlying,
            expiry=expiry,
            spot_price=spot_price,
            each_side=each_side,
        )
        quotes = tuple(
            OptionQuote(
                contract=contract,
                ltp=tick.ltp if tick else None,
                open_price=tick.open_price if tick else None,
                high_price=tick.high_price if tick else None,
                low_price=tick.low_price if tick else None,
                close_price=tick.close_price if tick else None,
                oi=tick.oi if tick else None,
                oi_change=tick.oi_change if tick else None,
                oi_change_percent=tick.oi_change_percent if tick else None,
                volume=tick.volume if tick else None,
                bid=tick.bid if tick else None,
                ask=tick.ask if tick else None,
                greeks=self._greeks_by_token.get(contract.token.token),
            )
            for contract in contracts
            for tick in (self._latest_by_token.get(contract.token.token),)
        )
        return OptionChainSnapshot(
            underlying=underlying,
            expiry=expiry,
            spot_price=spot_price,
            atm_strike=atm,
            captured_at=captured_at or datetime.now(UTC),
            quotes=quotes,
            market=market,
        )

    def build_underlying_market_snapshot(
        self,
        *,
        underlying: str,
        captured_at: datetime,
    ) -> UnderlyingMarketSnapshot | None:
        token = self._master.spot_tokens.get(underlying)
        if token is None:
            return None
        tick = self._latest_by_token.get(token.token)
        if tick is None:
            return None
        future = self._master.nearest_future(
            underlying=underlying,
            as_of=captured_at.date(),
        )
        future_tick = (
            self._latest_by_token.get(future.token.token)
            if future is not None
            else None
        )
        spot_price = tick.ltp
        future_price = future_tick.ltp if future_tick is not None else None
        india_vix_token = self._master.reference_tokens.get("INDIA_VIX")
        india_vix_tick = (
            self._latest_by_token.get(india_vix_token.token)
            if india_vix_token is not None
            else None
        )
        india_vix = (
            india_vix_tick.ltp
            if india_vix_tick is not None and india_vix_tick.ltp is not None
            else self._reference_values.get("INDIA_VIX")
        )
        return UnderlyingMarketSnapshot(
            underlying=underlying,
            captured_at=captured_at,
            spot_observed_at=tick.exchange_timestamp,
            open_price=tick.open_price,
            high_price=tick.high_price,
            low_price=tick.low_price,
            previous_close=tick.close_price,
            future_observed_at=(
                future_tick.exchange_timestamp
                if future_tick is not None
                else None
            ),
            future_price=future_price,
            future_open=(
                future_tick.open_price if future_tick is not None else None
            ),
            future_high=(
                future_tick.high_price if future_tick is not None else None
            ),
            future_low=(
                future_tick.low_price if future_tick is not None else None
            ),
            future_previous_close=(
                future_tick.close_price if future_tick is not None else None
            ),
            future_volume=(
                future_tick.volume if future_tick is not None else None
            ),
            future_oi=future_tick.oi if future_tick is not None else None,
            basis=(
                future_price - spot_price
                if future_price is not None and spot_price is not None
                else None
            ),
            previous_20d_atr=self._previous_20d_atr.get(
                underlying.upper()
            ),
            india_vix=india_vix,
        )
