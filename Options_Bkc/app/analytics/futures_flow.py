from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    FuturesFlowContext,
    FuturesFlowHorizonContext,
    FuturesFlowState,
    FuturesPositioningContext,
    OptionChainSnapshot,
)


@dataclass(frozen=True)
class FuturesFlowSettings:
    window_seconds: int = 60
    minimum_price_change_points: Decimal = Decimal("5")
    minimum_oi_change_percent: Decimal = Decimal("0.02")
    max_observations: int = 16
    positioning_horizons_seconds: tuple[int, ...] = (15, 60, 180)
    positioning_sample_seconds: int = 5
    max_positioning_observations: int = 64

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.minimum_price_change_points < 0:
            raise ValueError("minimum_price_change_points must be non-negative")
        if self.minimum_oi_change_percent < 0:
            raise ValueError("minimum_oi_change_percent must be non-negative")
        if self.max_observations < 2:
            raise ValueError("max_observations must be at least two")
        if (
            not self.positioning_horizons_seconds
            or any(item <= 0 for item in self.positioning_horizons_seconds)
            or tuple(sorted(set(self.positioning_horizons_seconds)))
            != self.positioning_horizons_seconds
        ):
            raise ValueError(
                "positioning horizons must be unique, positive, and increasing"
            )
        if self.positioning_sample_seconds <= 0:
            raise ValueError("positioning_sample_seconds must be positive")
        if self.max_positioning_observations < 2:
            raise ValueError(
                "max_positioning_observations must be at least two"
            )


@dataclass(frozen=True)
class _Observation:
    captured_at: datetime
    price: Decimal
    oi: int
    basis: Decimal | None


class FuturesFlowTracker:
    """Classify futures price/OI jointly; OI by itself has no direction."""

    def __init__(self, settings: FuturesFlowSettings | None = None) -> None:
        self._settings = settings or FuturesFlowSettings()
        self._history: dict[str, deque[_Observation]] = {}
        self._positioning_history: dict[str, deque[_Observation]] = {}
        self._session_dates: dict[str, date] = {}
        self._last_captured_at: dict[str, datetime] = {}

    def update(self, snapshot: OptionChainSnapshot) -> FuturesFlowContext:
        key = snapshot.underlying.upper()
        market = snapshot.market
        if (
            market is None
            or market.future_price is None
            or market.future_oi is None
            or market.future_price <= 0
            or market.future_oi <= 0
        ):
            return FuturesFlowContext(
                reason="fresh futures price and OI are unavailable"
            )

        session_date = snapshot.captured_at.date()
        last = self._last_captured_at.get(key)
        if (
            self._session_dates.get(key) != session_date
            or (last is not None and snapshot.captured_at <= last)
        ):
            self._history[key] = deque()
            self._positioning_history[key] = deque()
            self._session_dates[key] = session_date
        self._last_captured_at[key] = snapshot.captured_at

        history = self._history.setdefault(key, deque())
        observation = _Observation(
            captured_at=snapshot.captured_at,
            price=market.future_price,
            oi=market.future_oi,
            basis=market.basis,
        )
        history.append(observation)
        positioning = self._update_positioning(key, observation)
        cutoff = snapshot.captured_at - timedelta(
            seconds=self._settings.window_seconds
        )
        while len(history) > 2 and history[1].captured_at <= cutoff:
            history.popleft()
        while len(history) > self._settings.max_observations:
            history.popleft()
        if len(history) < 2:
            return replace(
                FuturesFlowContext(
                    state=FuturesFlowState.NEUTRAL,
                    reason="collecting futures price/OI persistence window",
                ),
                positioning=positioning,
            )

        baseline = history[0]
        price_change = observation.price - baseline.price
        oi_change = observation.oi - baseline.oi
        oi_change_percent = (
            Decimal(oi_change) / Decimal(baseline.oi) * Decimal("100")
            if baseline.oi > 0
            else None
        )
        basis_change = (
            observation.basis - baseline.basis
            if observation.basis is not None and baseline.basis is not None
            else None
        )
        minimum_oi_change = (
            Decimal(baseline.oi)
            * self._settings.minimum_oi_change_percent
            / Decimal("100")
        )
        if (
            abs(price_change) < self._settings.minimum_price_change_points
            or abs(Decimal(oi_change)) < minimum_oi_change
        ):
            return replace(
                FuturesFlowContext(
                    state=FuturesFlowState.NEUTRAL,
                    price_change=price_change,
                    oi_change=oi_change,
                    oi_change_percent=oi_change_percent,
                    basis_change=basis_change,
                    reason=(
                        "futures price/OI change is below the persistence "
                        "threshold"
                    ),
                ),
                positioning=positioning,
            )

        if price_change > 0 and oi_change > 0:
            state = FuturesFlowState.LONG_BUILDUP
            side = "BUY_CALL"
            strength = Decimal("0.80")
        elif price_change < 0 and oi_change > 0:
            state = FuturesFlowState.SHORT_BUILDUP
            side = "BUY_PUT"
            strength = Decimal("0.80")
        elif price_change > 0 and oi_change < 0:
            state = FuturesFlowState.SHORT_COVERING
            side = "BUY_CALL"
            strength = Decimal("0.45")
        else:
            state = FuturesFlowState.LONG_UNWINDING
            side = "BUY_PUT"
            strength = Decimal("0.45")
        return FuturesFlowContext(
            state=state,
            side=side,
            price_change=price_change,
            oi_change=oi_change,
            oi_change_percent=oi_change_percent,
            basis_change=basis_change,
            strength=strength,
            reason=(
                f"{state.value}: future price {price_change:+} and "
                f"OI {oi_change:+} over the event-time window"
            ),
            positioning=positioning,
        )

    def _update_positioning(
        self,
        key: str,
        current: _Observation,
    ) -> FuturesPositioningContext:
        """Build a causal, bounded multi-horizon price/OI consensus."""

        history = self._positioning_history.setdefault(key, deque())
        if (
            not history
            or (
                current.captured_at - history[-1].captured_at
            ).total_seconds() >= self._settings.positioning_sample_seconds
        ):
            history.append(current)

        longest_horizon = self._settings.positioning_horizons_seconds[-1]
        cutoff = current.captured_at - timedelta(seconds=longest_horizon)
        while len(history) > 2 and history[1].captured_at <= cutoff:
            history.popleft()
        while len(history) > self._settings.max_positioning_observations:
            history.popleft()

        horizons = tuple(
            self._horizon_context(history, current, horizon_seconds)
            for horizon_seconds in self._settings.positioning_horizons_seconds
        )
        longest_ready = (
            horizons[-1].price_change is not None
            and horizons[-1].oi_change is not None
        )
        if not longest_ready:
            return FuturesPositioningContext(
                horizons=horizons,
                reason=(
                    "collecting the full multi-horizon futures positioning "
                    "window"
                ),
            )

        weighted_net = Decimal("0")
        total_weight = Decimal("0")
        for index, horizon in enumerate(horizons, start=1):
            weight = Decimal(index)
            total_weight += weight
            if horizon.side == "BUY_CALL":
                weighted_net += horizon.strength * weight
            elif horizon.side == "BUY_PUT":
                weighted_net -= horizon.strength * weight
        net_strength = (
            weighted_net / total_weight
            if total_weight > 0
            else Decimal("0")
        )
        if abs(net_strength) < Decimal("0.25"):
            return FuturesPositioningContext(
                ready=True,
                state=FuturesFlowState.NEUTRAL,
                horizons=horizons,
                reason="multi-horizon futures positioning has no stable side",
            )

        side = "BUY_CALL" if net_strength > 0 else "BUY_PUT"
        aligned = tuple(item for item in horizons if item.side == side)
        if len(aligned) < 2:
            return FuturesPositioningContext(
                ready=True,
                state=FuturesFlowState.NEUTRAL,
                horizons=horizons,
                reason="futures positioning lacks two-horizon confirmation",
            )
        dominant = aligned[-1]
        strength = min(Decimal("1"), abs(net_strength)).quantize(
            Decimal("0.0001")
        )
        return FuturesPositioningContext(
            ready=True,
            state=dominant.state,
            side=side,
            strength=strength,
            horizon_agreement=len(aligned),
            horizons=horizons,
            reason=(
                f"{dominant.state.value}: {len(aligned)}/{len(horizons)} "
                f"futures price/OI horizons align for {side} "
                f"(strength={strength})"
            ),
        )

    def _horizon_context(
        self,
        history: deque[_Observation],
        current: _Observation,
        horizon_seconds: int,
    ) -> FuturesFlowHorizonContext:
        cutoff = current.captured_at - timedelta(seconds=horizon_seconds)
        baseline = _at_or_before(history, cutoff)
        if baseline is None:
            return FuturesFlowHorizonContext(horizon_seconds=horizon_seconds)

        price_change = current.price - baseline.price
        oi_change = current.oi - baseline.oi
        oi_change_percent = (
            Decimal(oi_change) / Decimal(baseline.oi) * Decimal("100")
            if baseline.oi > 0
            else Decimal("0")
        )
        horizon_scale = _clamp(
            (Decimal(horizon_seconds) / Decimal("60")).sqrt(),
            Decimal("0.5"),
            Decimal("2"),
        )
        price_threshold = (
            self._settings.minimum_price_change_points * horizon_scale
        )
        oi_threshold = (
            self._settings.minimum_oi_change_percent * horizon_scale
        )
        if (
            abs(price_change) < price_threshold
            or abs(oi_change_percent) < oi_threshold
        ):
            return FuturesFlowHorizonContext(
                horizon_seconds=horizon_seconds,
                price_change=price_change,
                oi_change=oi_change,
                oi_change_percent=oi_change_percent,
            )

        if price_change > 0 and oi_change > 0:
            state = FuturesFlowState.LONG_BUILDUP
            side = "BUY_CALL"
            reliability = Decimal("1")
        elif price_change < 0 and oi_change > 0:
            state = FuturesFlowState.SHORT_BUILDUP
            side = "BUY_PUT"
            reliability = Decimal("1")
        elif price_change > 0 and oi_change < 0:
            state = FuturesFlowState.SHORT_COVERING
            side = "BUY_CALL"
            reliability = Decimal("0.65")
        else:
            state = FuturesFlowState.LONG_UNWINDING
            side = "BUY_PUT"
            reliability = Decimal("0.65")
        price_multiple = abs(price_change) / max(
            price_threshold, Decimal("0.0001")
        )
        oi_multiple = abs(oi_change_percent) / max(
            oi_threshold, Decimal("0.0001")
        )
        magnitude = _clamp(
            min(price_multiple, oi_multiple) / Decimal("2"),
            Decimal("0.25"),
            Decimal("1"),
        )
        return FuturesFlowHorizonContext(
            horizon_seconds=horizon_seconds,
            state=state,
            side=side,
            price_change=price_change,
            oi_change=oi_change,
            oi_change_percent=oi_change_percent,
            strength=(magnitude * reliability).quantize(Decimal("0.0001")),
        )

    def reset(self) -> None:
        self._history.clear()
        self._positioning_history.clear()
        self._session_dates.clear()
        self._last_captured_at.clear()


def _at_or_before(
    history: deque[_Observation],
    cutoff: datetime,
) -> _Observation | None:
    selected = None
    for item in history:
        if item.captured_at <= cutoff:
            selected = item
        else:
            break
    return selected


def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))
