from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    GapClass,
    OpeningContext,
    OpeningState,
    OptionChainSnapshot,
)


@dataclass(frozen=True)
class OpeningContextSettings:
    observation_minutes: int = 15
    flat_expected_ratio: Decimal = Decimal("0.15")
    moderate_expected_ratio: Decimal = Decimal("0.60")
    large_expected_ratio: Decimal = Decimal("1.00")
    flat_spot_ratio: Decimal = Decimal("0.0015")
    moderate_spot_ratio: Decimal = Decimal("0.0060")
    large_spot_ratio: Decimal = Decimal("0.0125")
    drive_efficiency: Decimal = Decimal("0.60")
    fade_fill_ratio: Decimal = Decimal("0.50")
    market_timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        if self.observation_minutes <= 0:
            raise ValueError("observation_minutes must be positive")


@dataclass
class _OpeningState:
    session_date: date
    previous_close: Decimal
    session_open: Decimal
    opening_high: Decimal
    opening_low: Decimal
    last_spot: Decimal
    last_captured_at: datetime
    finalized: bool = False


class OpeningContextTracker:
    """Classify opening behavior using only observations available so far."""

    def __init__(self, settings: OpeningContextSettings | None = None) -> None:
        self._settings = settings or OpeningContextSettings()
        self._timezone = ZoneInfo(self._settings.market_timezone)
        self._states: dict[str, _OpeningState] = {}

    def update(self, snapshot: OptionChainSnapshot) -> OpeningContext:
        market = snapshot.market
        if market is None or market.previous_close is None:
            return OpeningContext(
                reason="underlying previous close is unavailable"
            )

        captured = snapshot.captured_at.astimezone(self._timezone)
        key = snapshot.underlying.upper()
        session_open = market.open_price or snapshot.spot_price
        state = self._states.get(key)
        if (
            state is None
            or state.session_date != captured.date()
            or snapshot.captured_at < state.last_captured_at
        ):
            state = _OpeningState(
                session_date=captured.date(),
                previous_close=market.previous_close,
                session_open=session_open,
                opening_high=snapshot.spot_price,
                opening_low=snapshot.spot_price,
                last_spot=snapshot.spot_price,
                last_captured_at=snapshot.captured_at,
            )
            self._states[key] = state

        minutes_since_open = (
            captured.hour * 60 + captured.minute - (9 * 60 + 15)
        )
        if (
            not state.finalized
            and minutes_since_open < self._settings.observation_minutes
        ):
            state.opening_high = max(state.opening_high, snapshot.spot_price)
            state.opening_low = min(state.opening_low, snapshot.spot_price)
            state.last_spot = snapshot.spot_price
            state.last_captured_at = snapshot.captured_at
            return self._build(
                snapshot,
                state,
                OpeningState.OBSERVING_OPEN,
                "collecting the first 15-minute opening range",
            )

        if not state.finalized:
            state.opening_high = max(state.opening_high, snapshot.spot_price)
            state.opening_low = min(state.opening_low, snapshot.spot_price)
            state.finalized = True

        state.last_spot = snapshot.spot_price
        state.last_captured_at = snapshot.captured_at
        opening_state, reason = self._classify(snapshot, state)
        return self._build(snapshot, state, opening_state, reason)

    def reset(self) -> None:
        self._states.clear()

    def _classify(
        self,
        snapshot: OptionChainSnapshot,
        state: _OpeningState,
    ) -> tuple[OpeningState, str]:
        gap = state.session_open - state.previous_close
        gap_class, _ = self._gap_class(snapshot, state)
        opening_range = state.opening_high - state.opening_low
        displacement = snapshot.spot_price - state.session_open
        efficiency = (
            abs(displacement) / opening_range
            if opening_range > 0
            else Decimal("0")
        )
        midpoint = (state.opening_high + state.opening_low) / Decimal("2")
        fill = _gap_fill_ratio(
            gap=gap,
            session_open=state.session_open,
            spot=snapshot.spot_price,
        )

        if snapshot.spot_price > state.opening_high and displacement > 0:
            return OpeningState.OPENING_DRIVE_UP, "opening-range upside accepted"
        if snapshot.spot_price < state.opening_low and displacement < 0:
            return (
                OpeningState.OPENING_DRIVE_DOWN,
                "opening-range downside accepted",
            )

        if gap_class in {GapClass.LARGE_GAP, GapClass.EXTREME_EVENT_GAP}:
            if efficiency < Decimal("0.35"):
                return (
                    OpeningState.LARGE_GAP_ABSORPTION,
                    "large normalized gap is being absorbed inside the opening range",
                )

        if gap > 0:
            if fill >= self._settings.fade_fill_ratio or snapshot.spot_price < midpoint:
                return (
                    OpeningState.GAP_FADE_CANDIDATE_DOWN,
                    "gap-up is losing its opening range and filling",
                )
            if displacement >= 0 and efficiency >= Decimal("0.40"):
                return OpeningState.GAP_AND_GO_UP, "gap-up remains accepted"
        elif gap < 0:
            if fill >= self._settings.fade_fill_ratio or snapshot.spot_price > midpoint:
                return (
                    OpeningState.GAP_FADE_CANDIDATE_UP,
                    "gap-down is losing its opening range and filling",
                )
            if displacement <= 0 and efficiency >= Decimal("0.40"):
                return OpeningState.GAP_AND_GO_DOWN, "gap-down remains accepted"

        if gap_class == GapClass.FLAT_OPEN:
            if efficiency >= self._settings.drive_efficiency:
                if displacement > 0:
                    return OpeningState.OPENING_DRIVE_UP, "flat open developed an upside drive"
                if displacement < 0:
                    return (
                        OpeningState.OPENING_DRIVE_DOWN,
                        "flat open developed a downside drive",
                    )
            return (
                OpeningState.BALANCED_FLAT_OPEN,
                "flat open remains balanced inside the opening range",
            )
        return OpeningState.UNSTABLE_OPEN, "opening state has no stable acceptance"

    def _build(
        self,
        snapshot: OptionChainSnapshot,
        state: _OpeningState,
        opening_state: OpeningState,
        reason: str,
    ) -> OpeningContext:
        gap_class, normalized_gap = self._gap_class(snapshot, state)
        gap = state.session_open - state.previous_close
        direction = (
            "UP"
            if opening_state
            in {
                OpeningState.OPENING_DRIVE_UP,
                OpeningState.GAP_AND_GO_UP,
                OpeningState.GAP_FADE_CANDIDATE_UP,
            }
            else "DOWN"
            if opening_state
            in {
                OpeningState.OPENING_DRIVE_DOWN,
                OpeningState.GAP_AND_GO_DOWN,
                OpeningState.GAP_FADE_CANDIDATE_DOWN,
            }
            else None
        )
        return OpeningContext(
            state=opening_state,
            gap_class=gap_class,
            session_open=state.session_open,
            previous_close=state.previous_close,
            opening_high=state.opening_high,
            opening_low=state.opening_low,
            gap_points=gap,
            normalized_gap=normalized_gap,
            gap_fill_ratio=_gap_fill_ratio(
                gap=gap,
                session_open=state.session_open,
                spot=snapshot.spot_price,
            ),
            opening_range_points=state.opening_high - state.opening_low,
            direction=direction,
            reason=reason,
        )

    def _gap_class(
        self,
        snapshot: OptionChainSnapshot,
        state: _OpeningState,
    ) -> tuple[GapClass, Decimal]:
        market = snapshot.market
        gap = abs(state.session_open - state.previous_close)
        volatility_scale = None
        if market is not None:
            volatility_scale = (
                market.previous_session_expected_move or market.previous_20d_atr
            )
        if volatility_scale is not None and volatility_scale > 0:
            ratio = gap / volatility_scale
            thresholds = (
                self._settings.flat_expected_ratio,
                self._settings.moderate_expected_ratio,
                self._settings.large_expected_ratio,
            )
        else:
            ratio = gap / state.previous_close if state.previous_close > 0 else Decimal("0")
            thresholds = (
                self._settings.flat_spot_ratio,
                self._settings.moderate_spot_ratio,
                self._settings.large_spot_ratio,
            )
        if ratio <= thresholds[0]:
            return GapClass.FLAT_OPEN, ratio
        if ratio <= thresholds[1]:
            return GapClass.MODERATE_GAP, ratio
        if ratio <= thresholds[2]:
            return GapClass.LARGE_GAP, ratio
        return GapClass.EXTREME_EVENT_GAP, ratio


def _gap_fill_ratio(
    *,
    gap: Decimal,
    session_open: Decimal,
    spot: Decimal,
) -> Decimal | None:
    if gap == 0:
        return Decimal("0")
    filled = (
        session_open - spot
        if gap > 0
        else spot - session_open
    )
    return max(
        Decimal("0"),
        min(Decimal("1.5"), filled / abs(gap)),
    )
