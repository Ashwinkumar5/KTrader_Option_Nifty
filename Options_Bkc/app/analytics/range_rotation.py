from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class RangeRotationPhase(StrEnum):
    WAITING = "WAITING"
    SUPPORT_TEST = "SUPPORT_TEST"
    ROTATING_UP = "ROTATING_UP"
    DEGRADED_UP = "DEGRADED_UP"
    RESISTANCE_TEST = "RESISTANCE_TEST"
    ROTATING_DOWN = "ROTATING_DOWN"
    DEGRADED_DOWN = "DEGRADED_DOWN"


@dataclass(frozen=True)
class RangeRotationSettings:
    """Conservative defaults for a stateful intraday range rotation."""

    min_range_width_points: Decimal = Decimal("75")
    min_reversal_points: Decimal = Decimal("5")
    min_remaining_room_points: Decimal = Decimal("20")
    min_reward_risk: Decimal = Decimal("1.5")
    risk_buffer_ratio: Decimal = Decimal("0.10")
    level_shift_tolerance_points: Decimal = Decimal("25")
    max_rotation_age: timedelta = timedelta(minutes=90)
    decision_frame_seconds: int = 15
    soft_breach_window_frames: int = 3
    soft_breach_frames: int = 2
    hard_invalidation_points: Decimal = Decimal("5")
    recovery_buffer_points: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        if self.decision_frame_seconds <= 0:
            raise ValueError("decision_frame_seconds must be positive")
        if self.soft_breach_window_frames <= 0:
            raise ValueError("soft_breach_window_frames must be positive")
        if not 1 <= self.soft_breach_frames <= self.soft_breach_window_frames:
            raise ValueError(
                "soft_breach_frames must be within the breach window"
            )


@dataclass(frozen=True)
class RangeRotationDecision:
    signal: str | None
    reason: str
    phase: RangeRotationPhase
    invalidation: Decimal | None = None
    target: Decimal | None = None
    reward_risk: Decimal | None = None


@dataclass
class _RotationState:
    support: Decimal
    resistance: Decimal
    phase: RangeRotationPhase
    last_spot: Decimal
    last_at: datetime
    started_at: datetime
    pivot: Decimal | None = None
    invalidation: Decimal | None = None
    pullback_extreme: Decimal | None = None
    soft_breach_bucket: int | None = None
    soft_breach_current: bool = False
    soft_breach_history: deque[bool] = field(
        default_factory=lambda: deque(maxlen=3)
    )


class RangeRotationTracker:
    """Remember a defended range boundary and follow the subsequent rotation.

    This tracker deliberately proposes candidates only. The downstream signal
    gate still requires fresh, matching option microstructure before a strong
    signal can qualify.
    """

    def __init__(self, settings: RangeRotationSettings | None = None) -> None:
        self._settings = settings or RangeRotationSettings()
        self._states: dict[str, _RotationState] = {}

    def reset(self, underlying: str | None = None) -> None:
        if underlying is None:
            self._states.clear()
        else:
            self._states.pop(underlying.upper(), None)

    def update(
        self,
        *,
        underlying: str,
        captured_at: datetime,
        spot: Decimal,
        support: Decimal | None,
        resistance: Decimal | None,
        level_zone: Decimal,
    ) -> RangeRotationDecision:
        key = underlying.upper()
        zone = max(Decimal("1"), level_zone)

        if (
            support is None
            or resistance is None
            or resistance <= support
            or resistance - support < self._settings.min_range_width_points
        ):
            self.reset(key)
            return self._neutral(RangeRotationPhase.WAITING, "range boundaries are not usable")

        state = self._states.get(key)
        if state is not None and captured_at < state.last_at:
            # Never carry future state backwards during a replay.
            self.reset(key)
            state = None

        if state is not None and captured_at - state.started_at > self._settings.max_rotation_age:
            self.reset(key)
            state = None

        level_shift_limit = max(
            zone,
            self._settings.level_shift_tolerance_points,
        )
        if state is not None and (
            abs(support - state.support) > level_shift_limit
            or abs(resistance - state.resistance) > level_shift_limit
        ):
            # A materially different OI range is a new market structure.
            self.reset(key)
            state = None

        if spot < support - zone or spot > resistance + zone:
            self.reset(key)
            return self._neutral(RangeRotationPhase.WAITING, "spot left the active range")

        if state is None:
            state = _RotationState(
                support=support,
                resistance=resistance,
                phase=RangeRotationPhase.WAITING,
                last_spot=spot,
                last_at=captured_at,
                started_at=captured_at,
                soft_breach_history=deque(
                    maxlen=self._settings.soft_breach_window_frames
                ),
            )
            self._states[key] = state

        delta = spot - state.last_spot
        reversal_points = max(
            self._settings.min_reversal_points,
            zone * Decimal("0.25"),
        )
        buffer = max(Decimal("1"), zone * self._settings.risk_buffer_ratio)
        near_support = abs(spot - support) <= zone
        near_resistance = abs(spot - resistance) <= zone

        if state.phase in {
            RangeRotationPhase.ROTATING_UP,
            RangeRotationPhase.DEGRADED_UP,
        } and near_resistance:
            state.phase = RangeRotationPhase.RESISTANCE_TEST
            state.pivot = spot
            state.invalidation = None
            state.pullback_extreme = None
            return self._finish(
                state,
                spot,
                captured_at,
                self._neutral(state.phase, "up rotation reached resistance zone"),
            )

        if state.phase in {
            RangeRotationPhase.ROTATING_DOWN,
            RangeRotationPhase.DEGRADED_DOWN,
        } and near_support:
            state.phase = RangeRotationPhase.SUPPORT_TEST
            state.pivot = spot
            state.invalidation = None
            state.pullback_extreme = None
            return self._finish(
                state,
                spot,
                captured_at,
                self._neutral(state.phase, "down rotation reached support zone"),
            )

        if state.phase == RangeRotationPhase.WAITING:
            if near_support:
                state.phase = RangeRotationPhase.SUPPORT_TEST
                state.pivot = spot
            elif near_resistance:
                state.phase = RangeRotationPhase.RESISTANCE_TEST
                state.pivot = spot
            return self._finish(
                state,
                spot,
                captured_at,
                self._neutral(state.phase, "waiting for boundary rejection"),
            )

        if state.phase == RangeRotationPhase.SUPPORT_TEST:
            state.pivot = min(state.pivot or spot, spot)
            if spot < support - zone:
                self.reset(key)
                return self._neutral(RangeRotationPhase.WAITING, "support failed")
            if delta > 0 and spot - state.pivot >= reversal_points:
                state.phase = RangeRotationPhase.ROTATING_UP
                state.invalidation = min(state.pivot, support) - buffer
                state.pullback_extreme = None
                self._clear_soft_breaches(state)
                decision = self._directional_decision(state, spot, "BUY_CALL", zone)
                return self._finish(state, spot, captured_at, decision)
            return self._finish(
                state,
                spot,
                captured_at,
                self._neutral(state.phase, "support rejection is not confirmed"),
            )

        if state.phase == RangeRotationPhase.RESISTANCE_TEST:
            state.pivot = max(state.pivot or spot, spot)
            if spot > resistance + zone:
                self.reset(key)
                return self._neutral(RangeRotationPhase.WAITING, "resistance failed")
            if delta < 0 and state.pivot - spot >= reversal_points:
                state.phase = RangeRotationPhase.ROTATING_DOWN
                state.invalidation = max(state.pivot, resistance) + buffer
                state.pullback_extreme = None
                self._clear_soft_breaches(state)
                decision = self._directional_decision(state, spot, "BUY_PUT", zone)
                return self._finish(state, spot, captured_at, decision)
            return self._finish(
                state,
                spot,
                captured_at,
                self._neutral(state.phase, "resistance rejection is not confirmed"),
            )

        hard_buffer = max(
            self._settings.hard_invalidation_points,
            zone * Decimal("0.25"),
        )
        recovery_buffer = max(
            self._settings.recovery_buffer_points,
            zone * Decimal("0.10"),
        )

        if state.phase in {
            RangeRotationPhase.ROTATING_UP,
            RangeRotationPhase.DEGRADED_UP,
        }:
            if state.invalidation is None:
                self.reset(key)
                return self._neutral(RangeRotationPhase.WAITING, "bullish rotation invalidated")
            soft_breached = spot <= state.invalidation
            two_closed_breaches = self._record_soft_breach(
                state,
                captured_at,
                soft_breached,
            )
            if spot <= state.invalidation - hard_buffer:
                self.reset(key)
                return self._neutral(
                    RangeRotationPhase.WAITING,
                    "bullish rotation hit hard invalidation",
                )
            if two_closed_breaches:
                self.reset(key)
                return self._neutral(
                    RangeRotationPhase.WAITING,
                    "bullish rotation invalidated by two closed-frame breaches",
                )
            if state.phase == RangeRotationPhase.DEGRADED_UP:
                if (
                    spot > state.invalidation + recovery_buffer
                    and delta > 0
                ):
                    state.phase = RangeRotationPhase.ROTATING_UP
                    return self._finish(
                        state,
                        spot,
                        captured_at,
                        self._neutral(
                            state.phase,
                            "bullish rotation recovered; awaiting renewed confirmation",
                        ),
                    )
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(
                        state.phase,
                        "bullish rotation is degraded after a soft breach",
                    ),
                )
            if soft_breached:
                state.phase = RangeRotationPhase.DEGRADED_UP
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(
                        state.phase,
                        "bullish rotation soft invalidation touched; state degraded",
                    ),
                )
            if delta < 0:
                state.pullback_extreme = min(state.pullback_extreme or spot, spot)
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(state.phase, "bullish rotation is pulling back"),
                )
            if delta > 0:
                if state.pullback_extreme is not None:
                    state.invalidation = max(
                        state.invalidation,
                        state.pullback_extreme - buffer,
                    )
                    state.pullback_extreme = None
                    self._clear_soft_breaches(state)
                decision = self._directional_decision(state, spot, "BUY_CALL", zone)
                return self._finish(state, spot, captured_at, decision)

        if state.phase in {
            RangeRotationPhase.ROTATING_DOWN,
            RangeRotationPhase.DEGRADED_DOWN,
        }:
            if state.invalidation is None:
                self.reset(key)
                return self._neutral(RangeRotationPhase.WAITING, "bearish rotation invalidated")
            soft_breached = spot >= state.invalidation
            two_closed_breaches = self._record_soft_breach(
                state,
                captured_at,
                soft_breached,
            )
            if spot >= state.invalidation + hard_buffer:
                self.reset(key)
                return self._neutral(
                    RangeRotationPhase.WAITING,
                    "bearish rotation hit hard invalidation",
                )
            if two_closed_breaches:
                self.reset(key)
                return self._neutral(
                    RangeRotationPhase.WAITING,
                    "bearish rotation invalidated by two closed-frame breaches",
                )
            if state.phase == RangeRotationPhase.DEGRADED_DOWN:
                if (
                    spot < state.invalidation - recovery_buffer
                    and delta < 0
                ):
                    state.phase = RangeRotationPhase.ROTATING_DOWN
                    return self._finish(
                        state,
                        spot,
                        captured_at,
                        self._neutral(
                            state.phase,
                            "bearish rotation recovered; awaiting renewed confirmation",
                        ),
                    )
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(
                        state.phase,
                        "bearish rotation is degraded after a soft breach",
                    ),
                )
            if soft_breached:
                state.phase = RangeRotationPhase.DEGRADED_DOWN
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(
                        state.phase,
                        "bearish rotation soft invalidation touched; state degraded",
                    ),
                )
            if delta > 0:
                state.pullback_extreme = max(state.pullback_extreme or spot, spot)
                return self._finish(
                    state,
                    spot,
                    captured_at,
                    self._neutral(state.phase, "bearish rotation is pulling back"),
                )
            if delta < 0:
                if state.pullback_extreme is not None:
                    state.invalidation = min(
                        state.invalidation,
                        state.pullback_extreme + buffer,
                    )
                    state.pullback_extreme = None
                    self._clear_soft_breaches(state)
                decision = self._directional_decision(state, spot, "BUY_PUT", zone)
                return self._finish(state, spot, captured_at, decision)

        return self._finish(
            state,
            spot,
            captured_at,
            self._neutral(state.phase, "rotation has no renewed directional movement"),
        )

    def _record_soft_breach(
        self,
        state: _RotationState,
        captured_at: datetime,
        breached: bool,
    ) -> bool:
        bucket = (
            int(captured_at.timestamp())
            // self._settings.decision_frame_seconds
        )
        if state.soft_breach_bucket is None or bucket < state.soft_breach_bucket:
            state.soft_breach_history.clear()
            state.soft_breach_bucket = bucket
            state.soft_breach_current = breached
        elif bucket == state.soft_breach_bucket:
            # Latest observation is the close if the bucket ends now.
            state.soft_breach_current = breached
        else:
            state.soft_breach_history.append(state.soft_breach_current)
            state.soft_breach_bucket = bucket
            state.soft_breach_current = breached
        return (
            sum(state.soft_breach_history)
            >= self._settings.soft_breach_frames
        )

    @staticmethod
    def _clear_soft_breaches(state: _RotationState) -> None:
        state.soft_breach_history.clear()
        state.soft_breach_bucket = None
        state.soft_breach_current = False

    def _directional_decision(
        self,
        state: _RotationState,
        spot: Decimal,
        side: str,
        zone: Decimal,
    ) -> RangeRotationDecision:
        if state.invalidation is None:
            return self._neutral(state.phase, "rotation has no invalidation level")

        if side == "BUY_CALL":
            target = state.resistance - zone
            reward = target - spot
            risk = spot - state.invalidation
            boundary = state.support
            direction = "up"
        else:
            target = state.support + zone
            reward = spot - target
            risk = state.invalidation - spot
            boundary = state.resistance
            direction = "down"

        minimum_room = max(self._settings.min_remaining_room_points, zone)
        if reward < minimum_room or risk <= 0:
            return self._neutral(state.phase, "insufficient room remains in the range")

        reward_risk = (reward / risk).quantize(Decimal("0.01"))
        if reward_risk < self._settings.min_reward_risk:
            return RangeRotationDecision(
                signal=None,
                reason=(
                    f"range rotation {direction} has weak reward/risk "
                    f"{reward_risk} < {self._settings.min_reward_risk}"
                ),
                phase=state.phase,
                invalidation=state.invalidation,
                target=target,
                reward_risk=reward_risk,
            )

        return RangeRotationDecision(
            signal=side,
            reason=(
                f"RANGE ROTATION CONTINUATION: {boundary} boundary defended; "
                f"spot {spot} rotating {direction} toward {target}; "
                f"invalidation {state.invalidation}; R:R {reward_risk}."
            ),
            phase=state.phase,
            invalidation=state.invalidation,
            target=target,
            reward_risk=reward_risk,
        )

    @staticmethod
    def _neutral(phase: RangeRotationPhase, reason: str) -> RangeRotationDecision:
        return RangeRotationDecision(signal=None, reason=reason, phase=phase)

    @staticmethod
    def _finish(
        state: _RotationState,
        spot: Decimal,
        captured_at: datetime,
        decision: RangeRotationDecision,
    ) -> RangeRotationDecision:
        state.last_spot = spot
        state.last_at = captured_at
        return decision
