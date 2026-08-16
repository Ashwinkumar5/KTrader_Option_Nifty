from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.strategy_config import (
    OptionChainImpulseSettings,
    SMCSettings,
)
from app.domain.models import (
    EvidenceFamily,
    SignalSetup,
    StrategyCandidate,
    StrategyCheck,
    StrategyDiagnostic,
    StrategyEvidence,
    StrategyFamily,
)

from .base import StrategyEvaluationContext
from .option_chain_impulse import OptionChainImpulseStrategy


CALL = "BUY_CALL"
PUT = "BUY_PUT"
INDIA_MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class _PricePoint:
    captured_at: datetime
    price: Decimal


@dataclass(frozen=True, slots=True)
class _LiquidityLevel:
    side: str
    price: Decimal
    kind: str
    confirmed_at: datetime

    @property
    def key(self) -> tuple[str, Decimal, str]:
        return self.side, self.price, self.kind


@dataclass(slots=True)
class _PendingSweep:
    side: str
    level: _LiquidityLevel
    swept_at: datetime
    extreme: Decimal
    structure_level: Decimal
    phase: str = "SWEPT"
    reclaimed_at: datetime | None = None
    structure_confirmed_at: datetime | None = None
    displacement_threshold: Decimal | None = None
    option_confirmation: StrategyCandidate | None = None


@dataclass(slots=True)
class _UnderlyingState:
    session_date: date
    points: deque[_PricePoint]
    changes: deque[Decimal]
    high_levels: deque[_LiquidityLevel]
    low_levels: deque[_LiquidityLevel]
    consumed_until: dict[tuple[str, Decimal, str], datetime] = field(
        default_factory=dict
    )
    opening_high: Decimal | None = None
    opening_low: Decimal | None = None
    opening_locked: bool = False
    pending: _PendingSweep | None = None
    last_emitted_at: datetime | None = None


class SMCStrategy:
    """Causal NIFTY-futures liquidity sweep and reclaim strategy.

    This module intentionally owns all SMC chart-state. Cross-strike option
    confirmation is delegated to the existing impulse evaluator and dynamic
    futures/option OFI remains in the downstream signal gate. No order-book
    work is duplicated here.
    """

    family = StrategyFamily.SMC

    def __init__(
        self,
        settings: SMCSettings,
        impulse_settings: OptionChainImpulseSettings,
        *,
        enabled: bool,
    ) -> None:
        self._settings = settings
        self._enabled = enabled
        self._states: dict[str, _UnderlyingState] = {}
        self._option_impulse = OptionChainImpulseStrategy(
            impulse_settings,
            enabled=enabled and settings.require_cross_strike_confirmation,
        )
        self._option_confirmations: dict[
            str, tuple[datetime, StrategyCandidate]
        ] = {}
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="NO_CANDIDATE",
            reason=(
                "strategy is disabled"
                if not enabled
                else "waiting for NIFTY-futures liquidity levels"
            ),
        )

    @property
    def last_diagnostic(self) -> StrategyDiagnostic:
        return self._last_diagnostic

    def reset(self) -> None:
        self._states.clear()
        self._option_confirmations.clear()
        self._option_impulse.reset()

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        if not self._enabled:
            return ()

        key = context.underlying.upper()
        self._capture_option_confirmation(key, context)
        if context.future_price is None or context.future_price <= 0:
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason="SMC requires a synchronized front-future price",
                checks=(
                    StrategyCheck(
                        "future_price",
                        False,
                        "unavailable",
                        "positive synchronized front-future price",
                    ),
                ),
            )
            return ()

        state = self._state(key, context)
        price = context.future_price
        previous = state.points[-1] if state.points else None
        if previous is not None and context.captured_at < previous.captured_at:
            state = self._replace_state(key, context.captured_at.date())
            previous = None

        self._append_price(state, context.captured_at, price)
        self._update_opening_range(state, context.captured_at, price)
        self._confirm_swing(state)
        self._prune_levels(state, context.captured_at)

        candidate = self._advance_pending(
            key=key,
            state=state,
            captured_at=context.captured_at,
            price=price,
        )
        if candidate is not None:
            return (candidate,)

        if state.pending is None and previous is not None:
            pending = self._detect_sweep(
                state=state,
                previous_price=previous.price,
                current_price=price,
                captured_at=context.captured_at,
            )
            if pending is not None:
                state.pending = pending
                self._set_waiting_diagnostic(
                    pending,
                    price,
                    option_confirmed=self._matching_option_confirmation(
                        key, pending.side, context.captured_at
                    )
                    is not None,
                )
                return ()

        self._set_idle_diagnostic(state, price)
        return ()

    def _state(
        self,
        key: str,
        context: StrategyEvaluationContext,
    ) -> _UnderlyingState:
        session_date = context.captured_at.date()
        state = self._states.get(key)
        if state is None or state.session_date != session_date:
            state = self._replace_state(key, session_date)
        return state

    def _replace_state(
        self,
        key: str,
        session_date: date,
    ) -> _UnderlyingState:
        point_capacity = max(
            self._settings.structure_lookback_frames + 1,
            self._settings.swing_left_frames
            + self._settings.swing_right_frames
            + 1,
        )
        state = _UnderlyingState(
            session_date=session_date,
            points=deque(maxlen=point_capacity),
            changes=deque(
                maxlen=self._settings.displacement_lookback_frames
            ),
            high_levels=deque(
                maxlen=self._settings.maximum_active_levels_per_side
            ),
            low_levels=deque(
                maxlen=self._settings.maximum_active_levels_per_side
            ),
        )
        self._states[key] = state
        self._option_confirmations.pop(key, None)
        return state

    def _append_price(
        self,
        state: _UnderlyingState,
        captured_at: datetime,
        price: Decimal,
    ) -> None:
        if state.points and state.points[-1].captured_at == captured_at:
            state.points.pop()
            if state.points:
                if state.changes:
                    state.changes.pop()
                state.changes.append(abs(price - state.points[-1].price))
            state.points.append(_PricePoint(captured_at, price))
            return
        if state.points:
            state.changes.append(abs(price - state.points[-1].price))
        state.points.append(_PricePoint(captured_at, price))

    def _update_opening_range(
        self,
        state: _UnderlyingState,
        captured_at: datetime,
        price: Decimal,
    ) -> None:
        local_timestamp = (
            captured_at.astimezone(INDIA_MARKET_TIMEZONE)
            if captured_at.tzinfo is not None
            else captured_at
        )
        local_time = local_timestamp.timetz().replace(tzinfo=None)
        opening_start = time(9, 15)
        opening_end_dt = (
            datetime.combine(local_timestamp.date(), opening_start)
            + timedelta(minutes=self._settings.opening_range_minutes)
        )
        opening_end = opening_end_dt.time()
        if opening_start <= local_time < opening_end:
            state.opening_high = (
                price
                if state.opening_high is None
                else max(state.opening_high, price)
            )
            state.opening_low = (
                price
                if state.opening_low is None
                else min(state.opening_low, price)
            )
            return
        if local_time >= opening_end and not state.opening_locked:
            state.opening_locked = True
            if state.opening_high is not None:
                self._add_level(
                    state,
                    _LiquidityLevel(
                        "HIGH",
                        state.opening_high,
                        "OPENING_RANGE_HIGH",
                        captured_at,
                    ),
                )
            if state.opening_low is not None:
                self._add_level(
                    state,
                    _LiquidityLevel(
                        "LOW",
                        state.opening_low,
                        "OPENING_RANGE_LOW",
                        captured_at,
                    ),
                )

    def _confirm_swing(self, state: _UnderlyingState) -> None:
        size = (
            self._settings.swing_left_frames
            + self._settings.swing_right_frames
            + 1
        )
        if len(state.points) < size:
            return
        window = list(state.points)[-size:]
        center = window[self._settings.swing_left_frames]
        prices = [item.price for item in window]
        if center.price == max(prices) and prices.count(center.price) == 1:
            self._add_level(
                state,
                _LiquidityLevel(
                    "HIGH", center.price, "CONFIRMED_SWING_HIGH", center.captured_at
                ),
            )
        if center.price == min(prices) and prices.count(center.price) == 1:
            self._add_level(
                state,
                _LiquidityLevel(
                    "LOW", center.price, "CONFIRMED_SWING_LOW", center.captured_at
                ),
            )

    @staticmethod
    def _add_level(
        state: _UnderlyingState,
        level: _LiquidityLevel,
    ) -> None:
        levels = state.high_levels if level.side == "HIGH" else state.low_levels
        if any(
            item.price == level.price and item.kind == level.kind
            for item in levels
        ):
            return
        levels.append(level)

    def _prune_levels(
        self,
        state: _UnderlyingState,
        captured_at: datetime,
    ) -> None:
        cutoff = captured_at - timedelta(
            minutes=self._settings.maximum_level_age_minutes
        )
        while state.high_levels and state.high_levels[0].confirmed_at < cutoff:
            state.high_levels.popleft()
        while state.low_levels and state.low_levels[0].confirmed_at < cutoff:
            state.low_levels.popleft()
        expired = [
            key
            for key, until in state.consumed_until.items()
            if until < captured_at
        ]
        for key in expired:
            state.consumed_until.pop(key, None)

    def _detect_sweep(
        self,
        *,
        state: _UnderlyingState,
        previous_price: Decimal,
        current_price: Decimal,
        captured_at: datetime,
    ) -> _PendingSweep | None:
        if (
            state.last_emitted_at is not None
            and (captured_at - state.last_emitted_at).total_seconds()
            < self._settings.event_cooldown_seconds
        ):
            return None

        low_levels = [
            level
            for level in state.low_levels
            if level.key not in state.consumed_until
            and previous_price >= level.price
            and current_price
            <= level.price - self._settings.minimum_sweep_points
        ]
        high_levels = [
            level
            for level in state.high_levels
            if level.key not in state.consumed_until
            and previous_price <= level.price
            and current_price
            >= level.price + self._settings.minimum_sweep_points
        ]
        if not low_levels and not high_levels:
            return None

        recent = list(state.points)[:-1][
            -self._settings.structure_lookback_frames :
        ]
        if not recent:
            return None
        if low_levels and high_levels:
            low_distance = min(abs(previous_price - item.price) for item in low_levels)
            high_distance = min(abs(previous_price - item.price) for item in high_levels)
            choose_low = low_distance <= high_distance
        else:
            choose_low = bool(low_levels)

        if choose_low:
            level = max(low_levels, key=lambda item: item.price)
            structure_level = max(item.price for item in recent)
            return _PendingSweep(
                side=CALL,
                level=level,
                swept_at=captured_at,
                extreme=current_price,
                structure_level=structure_level,
            )
        level = min(high_levels, key=lambda item: item.price)
        structure_level = min(item.price for item in recent)
        return _PendingSweep(
            side=PUT,
            level=level,
            swept_at=captured_at,
            extreme=current_price,
            structure_level=structure_level,
        )

    def _advance_pending(
        self,
        *,
        key: str,
        state: _UnderlyingState,
        captured_at: datetime,
        price: Decimal,
    ) -> StrategyCandidate | None:
        pending = state.pending
        if pending is None:
            return None

        if pending.side == CALL:
            pending.extreme = min(pending.extreme, price)
        else:
            pending.extreme = max(pending.extreme, price)

        if pending.phase == "SWEPT":
            if (
                captured_at - pending.swept_at
            ).total_seconds() > self._settings.maximum_reclaim_seconds:
                self._consume_and_clear(state, pending, captured_at)
                return None
            reclaimed = (
                price
                >= pending.level.price + self._settings.reclaim_buffer_points
                if pending.side == CALL
                else price
                <= pending.level.price - self._settings.reclaim_buffer_points
            )
            if reclaimed:
                pending.phase = "RECLAIMED"
                pending.reclaimed_at = captured_at

        if pending.phase == "RECLAIMED":
            assert pending.reclaimed_at is not None
            if (
                captured_at - pending.reclaimed_at
            ).total_seconds() > self._settings.maximum_structure_break_seconds:
                self._consume_and_clear(state, pending, captured_at)
                return None
            structure_broken = (
                price
                >= pending.structure_level
                + self._settings.structure_break_buffer_points
                if pending.side == CALL
                else price
                <= pending.structure_level
                - self._settings.structure_break_buffer_points
            )
            threshold = self._displacement_threshold(state)
            displacement = abs(price - pending.extreme)
            if structure_broken and displacement >= threshold:
                pending.phase = "STRUCTURE_CONFIRMED"
                pending.structure_confirmed_at = captured_at
                pending.displacement_threshold = threshold

        if pending.phase == "STRUCTURE_CONFIRMED":
            assert pending.structure_confirmed_at is not None
            if (
                captured_at - pending.structure_confirmed_at
            ).total_seconds() > self._settings.option_confirmation_ttl_seconds:
                self._consume_and_clear(state, pending, captured_at)
                return None
            structure_holds = (
                price
                >= pending.structure_level
                + self._settings.structure_break_buffer_points
                if pending.side == CALL
                else price
                <= pending.structure_level
                - self._settings.structure_break_buffer_points
            )
            if not structure_holds:
                self._consume_and_clear(state, pending, captured_at)
                return None
            option_confirmation = pending.option_confirmation
            if option_confirmation is None:
                option_confirmation = self._matching_option_confirmation(
                    key, pending.side, captured_at
                )
                if option_confirmation is not None:
                    pending.option_confirmation = option_confirmation
            if (
                not self._settings.require_cross_strike_confirmation
                or option_confirmation is not None
            ):
                candidate = self._candidate(
                    pending,
                    price,
                    option_confirmation,
                )
                state.last_emitted_at = captured_at
                self._last_diagnostic = StrategyDiagnostic(
                    family=self.family,
                    status="CANDIDATE",
                    reason=candidate.reason,
                    proposed_side=candidate.side,
                    checks=self._checks(
                        pending,
                        price,
                        option_confirmation is not None,
                    ),
                )
                return candidate

        self._set_waiting_diagnostic(
            pending,
            price,
            option_confirmed=self._matching_option_confirmation(
                key, pending.side, captured_at
            )
            is not None,
        )
        return None

    def _displacement_threshold(
        self,
        state: _UnderlyingState,
    ) -> Decimal:
        positive = [value for value in state.changes if value > 0]
        median = _median(positive) if positive else Decimal("0")
        return max(
            self._settings.minimum_displacement_points,
            median * self._settings.displacement_multiplier,
        )

    def _capture_option_confirmation(
        self,
        key: str,
        context: StrategyEvaluationContext,
    ) -> None:
        if not self._settings.require_cross_strike_confirmation:
            return
        candidates = self._option_impulse.evaluate(context)
        if candidates:
            self._option_confirmations[key] = (
                context.captured_at,
                candidates[0],
            )

    def _matching_option_confirmation(
        self,
        key: str,
        side: str,
        captured_at: datetime,
    ) -> StrategyCandidate | None:
        item = self._option_confirmations.get(key)
        if item is None:
            return None
        confirmed_at, candidate = item
        age = (captured_at - confirmed_at).total_seconds()
        if age < 0 or age > self._settings.option_confirmation_ttl_seconds:
            return None
        return candidate if candidate.side == side else None

    def _candidate(
        self,
        pending: _PendingSweep,
        price: Decimal,
        option_confirmation: StrategyCandidate | None,
    ) -> StrategyCandidate:
        threshold = pending.displacement_threshold or Decimal("1")
        displacement = abs(price - pending.extreme)
        displacement_strength = min(
            Decimal("1"), displacement / max(threshold, Decimal("0.0001"))
        )
        sweep_depth = abs(pending.level.price - pending.extreme)
        sweep_strength = min(
            Decimal("1"),
            sweep_depth
            / max(
                self._settings.minimum_sweep_points * Decimal("2"),
                Decimal("0.0001"),
            ),
        )
        confidence = min(
            Decimal("0.95"),
            Decimal("0.55")
            + Decimal("0.15") * displacement_strength
            + Decimal("0.10") * sweep_strength
            + (
                Decimal("0.10")
                if option_confirmation is not None
                else Decimal("0")
            ),
        ).quantize(Decimal("0.0001"))
        evidence = [
            StrategyEvidence(
                "liquidity_sweep_reclaim",
                EvidenceFamily.LIQUIDITY,
                pending.side,
                max(Decimal("0.50"), sweep_strength),
                mandatory=True,
            ),
            StrategyEvidence(
                "confirmed_market_structure_shift",
                EvidenceFamily.STRUCTURE,
                pending.side,
                Decimal("0.80"),
                mandatory=True,
            ),
            StrategyEvidence(
                "futures_displacement",
                EvidenceFamily.PRICE_ACTION,
                pending.side,
                displacement_strength,
                mandatory=True,
            ),
        ]
        if option_confirmation is not None:
            evidence.append(
                StrategyEvidence(
                    "cross_strike_premium_impulse",
                    EvidenceFamily.FLOW,
                    pending.side,
                    option_confirmation.confidence,
                    mandatory=True,
                )
            )
        signed_score = displacement_strength
        if pending.side == PUT:
            signed_score = -signed_score
        reason = (
            f"SMC LIQUIDITY SWEEP RECLAIM {pending.side}: "
            f"{pending.level.kind}={pending.level.price}, "
            f"extreme={pending.extreme}, structure={pending.structure_level}, "
            f"future={price}, displacement={displacement:.2f}/"
            f"{threshold:.2f}, cross_strike="
            f"{'confirmed' if option_confirmation is not None else 'optional'}."
        )
        return StrategyCandidate(
            family=self.family,
            side=pending.side,
            setup_type=SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
            reason=reason,
            confidence=confidence,
            evidence=tuple(evidence),
            activation_level=pending.level.price,
            direction_score=signed_score.quantize(Decimal("0.0001")),
        )

    def _checks(
        self,
        pending: _PendingSweep,
        price: Decimal,
        option_confirmed: bool,
    ) -> tuple[StrategyCheck, ...]:
        return (
            StrategyCheck(
                "liquidity_sweep",
                True,
                f"{pending.level.kind}={pending.level.price}; extreme={pending.extreme}",
                f">= {self._settings.minimum_sweep_points} points beyond level",
                pending.side,
            ),
            StrategyCheck(
                "level_reclaimed",
                pending.phase in {"RECLAIMED", "STRUCTURE_CONFIRMED"},
                f"future={price}",
                f"reclaim {pending.level.price}",
                pending.side,
            ),
            StrategyCheck(
                "market_structure_shift",
                pending.phase == "STRUCTURE_CONFIRMED",
                f"future={price}; structure={pending.structure_level}",
                "close beyond the pre-sweep micro structure",
                pending.side,
            ),
            StrategyCheck(
                "futures_displacement",
                pending.phase == "STRUCTURE_CONFIRMED",
                f"threshold={pending.displacement_threshold}",
                "structure break with normalized range expansion",
                pending.side,
            ),
            StrategyCheck(
                "cross_strike_confirmation",
                option_confirmed
                or not self._settings.require_cross_strike_confirmation,
                "confirmed" if option_confirmed else "waiting",
                (
                    "same-side cross-strike premium impulse"
                    if self._settings.require_cross_strike_confirmation
                    else "optional"
                ),
                pending.side,
            ),
        )

    def _set_waiting_diagnostic(
        self,
        pending: _PendingSweep,
        price: Decimal,
        *,
        option_confirmed: bool,
    ) -> None:
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="NO_CANDIDATE",
            reason=f"SMC {pending.phase}: waiting for causal confirmation",
            proposed_side=pending.side,
            checks=self._checks(pending, price, option_confirmed),
        )

    def _set_idle_diagnostic(
        self,
        state: _UnderlyingState,
        price: Decimal,
    ) -> None:
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="NO_CANDIDATE",
            reason="SMC waiting for a fresh sweep of a confirmed liquidity level",
            checks=(
                StrategyCheck(
                    "confirmed_liquidity_levels",
                    bool(state.high_levels or state.low_levels),
                    (
                        f"highs={len(state.high_levels)}; "
                        f"lows={len(state.low_levels)}; future={price}"
                    ),
                    "opening-range or non-repainting swing level",
                ),
            ),
        )

    def _consume_and_clear(
        self,
        state: _UnderlyingState,
        pending: _PendingSweep,
        captured_at: datetime,
    ) -> None:
        state.consumed_until[pending.level.key] = captured_at + timedelta(
            seconds=self._settings.event_cooldown_seconds
        )
        state.pending = None


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")
