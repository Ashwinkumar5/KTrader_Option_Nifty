from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    ExpectedMoveBand,
    ExpectedMoveContext,
    OptionChainSnapshot,
    OptionType,
)


@dataclass(frozen=True)
class ExpectedMoveSettings:
    capture_time: time = time(9, 45)
    first_band_ratio: Decimal = Decimal("0.50")
    extended_band_ratio: Decimal = Decimal("0.80")
    exhaustion_band_ratio: Decimal = Decimal("1.00")
    market_timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        ratios = (
            self.first_band_ratio,
            self.extended_band_ratio,
            self.exhaustion_band_ratio,
        )
        if not (
            Decimal("0") < ratios[0] < ratios[1] < ratios[2]
        ):
            raise ValueError("expected-move bands must be positive and increasing")


@dataclass
class _ExpectedState:
    session_date: date
    last_captured_at: datetime
    captured_at: datetime | None = None
    anchor_spot: Decimal | None = None
    fixed_strike: Decimal | None = None
    straddle_mid: Decimal | None = None
    minutes_to_expiry: int | None = None


class ExpectedMoveTracker:
    """Capture one synchronized fixed-strike 09:45 ATM straddle per session."""

    def __init__(self, settings: ExpectedMoveSettings | None = None) -> None:
        self._settings = settings or ExpectedMoveSettings()
        self._timezone = ZoneInfo(self._settings.market_timezone)
        self._states: dict[str, _ExpectedState] = {}
        self._previous_session_move: dict[str, Decimal] = {}

    def update(self, snapshot: OptionChainSnapshot) -> ExpectedMoveContext:
        key = snapshot.underlying.upper()
        market_time = snapshot.captured_at.astimezone(self._timezone)
        state = self._ensure_state(snapshot)
        if (
            state.straddle_mid is None
            and market_time.time() >= self._settings.capture_time
        ):
            call = _quote_mid(snapshot, snapshot.atm_strike, OptionType.CALL)
            put = _quote_mid(snapshot, snapshot.atm_strike, OptionType.PUT)
            if call is not None and put is not None:
                state.captured_at = snapshot.captured_at
                state.anchor_spot = snapshot.spot_price
                state.fixed_strike = snapshot.atm_strike
                state.straddle_mid = call + put
                expiry_close = datetime.combine(
                    snapshot.expiry,
                    time(15, 30),
                    tzinfo=self._timezone,
                )
                state.minutes_to_expiry = max(
                    0,
                    int((expiry_close - market_time).total_seconds() // 60),
                )

        if (
            state.straddle_mid is None
            or state.anchor_spot is None
            or state.straddle_mid <= 0
        ):
            return ExpectedMoveContext(
                reason="waiting for synchronized 09:45 fixed-strike CE/PE mids"
            )

        move = abs(snapshot.spot_price - state.anchor_spot)
        utilization = move / state.straddle_mid
        first = state.straddle_mid * self._settings.first_band_ratio
        extended = state.straddle_mid * self._settings.extended_band_ratio
        exhaustion = (
            state.straddle_mid * self._settings.exhaustion_band_ratio
        )
        band = (
            ExpectedMoveBand.EXHAUSTION_WATCH
            if move >= exhaustion
            else ExpectedMoveBand.EXTENDED_MOVE
            if move >= extended
            else ExpectedMoveBand.FIRST_EXPANSION
            if move >= first
            else ExpectedMoveBand.INSIDE_FIRST
        )
        gap_consumption = None
        market = snapshot.market
        if (
            market is not None
            and market.open_price is not None
            and market.previous_close is not None
        ):
            gap_consumption = (
                abs(market.open_price - market.previous_close)
                / state.straddle_mid
            )
        return ExpectedMoveContext(
            available=True,
            captured_at=state.captured_at,
            anchor_spot=state.anchor_spot,
            fixed_strike=state.fixed_strike,
            straddle_mid=state.straddle_mid,
            minutes_to_expiry=state.minutes_to_expiry,
            utilization=utilization,
            gap_consumption_ratio=gap_consumption,
            band=band,
            first_band=first,
            extended_band=extended,
            exhaustion_band=exhaustion,
            reason=f"move utilization {utilization:.4f}; band={band.value}",
        )

    def previous_session_expected_move(
        self,
        underlying: str,
    ) -> Decimal | None:
        return self._previous_session_move.get(underlying.upper())

    def prepare(self, snapshot: OptionChainSnapshot) -> Decimal | None:
        """Advance session state without capturing a straddle."""

        self._ensure_state(snapshot)
        return self.previous_session_expected_move(snapshot.underlying)

    def reset(self) -> None:
        self._states.clear()
        self._previous_session_move.clear()

    def _ensure_state(
        self,
        snapshot: OptionChainSnapshot,
    ) -> _ExpectedState:
        key = snapshot.underlying.upper()
        market_date = snapshot.captured_at.astimezone(
            self._timezone
        ).date()
        state = self._states.get(key)
        if state is None:
            state = _ExpectedState(
                session_date=market_date,
                last_captured_at=snapshot.captured_at,
            )
            self._states[key] = state
        elif state.session_date != market_date:
            if (
                market_date > state.session_date
                and state.straddle_mid is not None
            ):
                self._previous_session_move[key] = state.straddle_mid
            state = _ExpectedState(
                session_date=market_date,
                last_captured_at=snapshot.captured_at,
            )
            self._states[key] = state
        elif snapshot.captured_at < state.last_captured_at:
            # A replay rewind invalidates the current-session capture. It must
            # never promote a future observation into previous-session state.
            state = _ExpectedState(
                session_date=market_date,
                last_captured_at=snapshot.captured_at,
            )
            self._states[key] = state
        state.last_captured_at = snapshot.captured_at
        return state


def _quote_mid(
    snapshot: OptionChainSnapshot,
    strike: Decimal,
    option_type: OptionType,
) -> Decimal | None:
    quote = next(
        (
            item
            for item in snapshot.quotes
            if item.contract.strike == strike
            and item.contract.option_type == option_type
        ),
        None,
    )
    if (
        quote is None
        or quote.bid is None
        or quote.ask is None
        or quote.bid <= 0
        or quote.ask < quote.bid
    ):
        return None
    return (quote.bid + quote.ask) / Decimal("2")
