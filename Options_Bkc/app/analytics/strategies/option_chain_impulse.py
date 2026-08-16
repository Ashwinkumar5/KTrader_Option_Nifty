from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.strategy_config import OptionChainImpulseSettings
from app.domain.models import (
    EvidenceFamily,
    OptionType,
    SignalSetup,
    StrategyCandidate,
    StrategyCheck,
    StrategyDiagnostic,
    StrategyEvidence,
    StrategyFamily,
)

from .base import OptionChainLeg, StrategyEvaluationContext


CALL = "BUY_CALL"
PUT = "BUY_PUT"


@dataclass(frozen=True)
class _Frame:
    captured_at: datetime
    spot: Decimal
    legs: dict[str, OptionChainLeg]
    residual_returns: dict[str, Decimal]
    residual_changes: dict[str, Decimal]


@dataclass(frozen=True)
class _SideState:
    side: str
    same_return: Decimal
    opposite_return: Decimal
    return_gap: Decimal
    same_breadth: Decimal
    opposite_decay_breadth: Decimal
    residual_return: Decimal
    residual_breadth: Decimal
    average_spread_ratio: Decimal
    volume_ratio: Decimal
    leg_count: int
    opposite_leg_count: int


class OptionChainImpulseStrategy:
    """Causal cross-strike long-premium impulse with opposite-leg decay."""

    family = StrategyFamily.OPTION_CHAIN_IMPULSE

    def __init__(
        self,
        settings: OptionChainImpulseSettings,
        *,
        enabled: bool,
    ) -> None:
        self._settings = settings
        self._enabled = enabled
        self._history: dict[str, deque[_Frame]] = {}
        self._session_dates: dict[str, date] = {}
        self._active_sides: dict[str, str] = {}
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="NO_CANDIDATE",
            reason=(
                "strategy is disabled"
                if not enabled
                else "strategy has not received a synchronized option frame"
            ),
        )

    @property
    def last_diagnostic(self) -> StrategyDiagnostic:
        return self._last_diagnostic

    def reset(self) -> None:
        self._history.clear()
        self._session_dates.clear()
        self._active_sides.clear()

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        if not self._enabled:
            return ()

        history = self._update_history(context)
        baseline = self._baseline(history, context.captured_at)
        if baseline is None:
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason="waiting for the causal premium-impulse baseline",
                checks=(
                    StrategyCheck(
                        "impulse_window",
                        False,
                        f"observations={len(history)}",
                        f">= {self._settings.window_seconds}s of history",
                    ),
                ),
            )
            return ()

        current = history[-1]
        call_state = self._side_state(
            side=CALL,
            baseline=baseline,
            current=current,
            history=history,
        )
        put_state = self._side_state(
            side=PUT,
            baseline=baseline,
            current=current,
            history=history,
        )
        available = tuple(
            item for item in (call_state, put_state) if item is not None
        )
        if not available:
            self._active_sides.pop(context.underlying.upper(), None)
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason="insufficient common liquid CE/PE legs across the window",
            )
            return ()

        proposed = max(available, key=lambda item: item.return_gap)
        checks = self._checks(proposed)
        qualified = all(item.passed for item in checks)
        if not qualified:
            self._active_sides.pop(context.underlying.upper(), None)
            failed = ", ".join(item.code for item in checks if not item.passed)
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason=f"OPTION_CHAIN_IMPULSE waiting: {failed}",
                proposed_side=proposed.side,
                checks=checks,
            )
            return ()

        underlying = context.underlying.upper()
        if self._active_sides.get(underlying) == proposed.side:
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason="qualified impulse was already emitted",
                proposed_side=proposed.side,
                checks=checks,
            )
            return ()
        self._active_sides[underlying] = proposed.side

        strength = min(
            Decimal("1"),
            proposed.return_gap
            / (self._settings.minimum_return_gap_percent * Decimal("2")),
        )
        breadth = (
            proposed.same_breadth + proposed.opposite_decay_breadth
        ) / Decimal("2")
        liquidity = max(
            Decimal("0"),
            Decimal("1")
            - proposed.average_spread_ratio
            / self._settings.maximum_average_spread_ratio,
        )
        confidence = min(
            Decimal("0.95"),
            Decimal("0.45")
            + Decimal("0.25") * strength
            + Decimal("0.20") * breadth
            + Decimal("0.10") * liquidity,
        ).quantize(Decimal("0.0001"))

        evidence = [
            StrategyEvidence(
                "greek_adjusted_residual_breadth",
                EvidenceFamily.FLOW,
                proposed.side,
                min(Decimal("1"), proposed.residual_breadth),
            ),
            StrategyEvidence(
                "opposite_leg_decay",
                EvidenceFamily.PRICE_ACTION,
                proposed.side,
                min(Decimal("1"), proposed.opposite_decay_breadth),
            ),
            StrategyEvidence(
                "executable_option_liquidity",
                EvidenceFamily.LIQUIDITY,
                proposed.side,
                liquidity,
            ),
        ]
        if proposed.volume_ratio >= Decimal("1.20"):
            evidence.append(
                StrategyEvidence(
                    "same_side_volume_acceleration",
                    EvidenceFamily.POSITIONING,
                    proposed.side,
                    min(Decimal("1"), proposed.volume_ratio / Decimal("2")),
                )
            )
        if (
            context.futures_flow is not None
            and context.futures_flow.side == proposed.side
            and context.futures_flow.strength > 0
        ):
            evidence.append(
                StrategyEvidence(
                    "optional_futures_support",
                    EvidenceFamily.FLOW,
                    proposed.side,
                    context.futures_flow.strength,
                )
            )

        signed_score = min(
            Decimal("1"),
            proposed.return_gap
            / (self._settings.minimum_return_gap_percent * Decimal("2")),
        )
        if proposed.side == PUT:
            signed_score = -signed_score
        reason = (
            f"OPTION CHAIN IMPULSE {proposed.side}: "
            f"same={proposed.same_return:+.3f}%, "
            f"opposite={proposed.opposite_return:+.3f}%, "
            f"gap={proposed.return_gap:+.3f}%, "
            f"breadth={proposed.same_breadth:.2f}, "
            f"opposite_decay={proposed.opposite_decay_breadth:.2f}, "
            f"residual={proposed.residual_return:+.3f}%/"
            f"{proposed.residual_breadth:.2f}, "
            f"legs={proposed.leg_count}/{proposed.opposite_leg_count}, "
            f"volume_ratio={proposed.volume_ratio:.2f}."
        )
        candidate = StrategyCandidate(
            family=self.family,
            side=proposed.side,
            setup_type=SignalSetup.OPTION_CHAIN_IMPULSE,
            reason=reason,
            confidence=confidence,
            evidence=tuple(evidence),
            direction_score=signed_score.quantize(Decimal("0.0001")),
            buyability_score=liquidity.quantize(Decimal("0.0001")),
        )
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="CANDIDATE",
            reason=reason,
            proposed_side=proposed.side,
            checks=checks,
        )
        return (candidate,)

    def _update_history(
        self,
        context: StrategyEvaluationContext,
    ) -> deque[_Frame]:
        key = context.underlying.upper()
        session_date = context.captured_at.date()
        history = self._history.setdefault(key, deque())
        if (
            self._session_dates.get(key) != session_date
            or (history and context.captured_at < history[-1].captured_at)
        ):
            history.clear()
            self._session_dates[key] = session_date
            self._active_sides.pop(key, None)
        legs = {
            item.token: item
            for item in context.option_chain_legs
            if item.mid > 0
            and abs(item.relative_strike) <= self._settings.strike_depth
        }
        response_by_token = {
            item.token: item for item in context.premium_responses
        }
        residual_returns = {
            token: (
                response_by_token[token].residual_change / leg.mid
                * Decimal("100")
            )
            for token, leg in legs.items()
            if token in response_by_token and leg.mid > 0
        }
        residual_changes = {
            token: response_by_token[token].residual_change
            for token in legs
            if token in response_by_token
        }
        history.append(
            _Frame(
                context.captured_at,
                context.spot,
                legs,
                residual_returns,
                residual_changes,
            )
        )
        cutoff = context.captured_at - timedelta(
            seconds=self._settings.window_seconds * 3
        )
        while history and history[0].captured_at < cutoff:
            history.popleft()
        return history

    def _baseline(
        self,
        history: deque[_Frame],
        captured_at: datetime,
    ) -> _Frame | None:
        cutoff = captured_at - timedelta(seconds=self._settings.window_seconds)
        candidates = tuple(item for item in history if item.captured_at <= cutoff)
        return candidates[-1] if candidates else None

    def _side_state(
        self,
        *,
        side: str,
        baseline: _Frame,
        current: _Frame,
        history: deque[_Frame],
    ) -> _SideState | None:
        same_type = OptionType.CALL if side == CALL else OptionType.PUT
        opposite_type = OptionType.PUT if side == CALL else OptionType.CALL
        returns: dict[OptionType, list[Decimal]] = {
            OptionType.CALL: [],
            OptionType.PUT: [],
        }
        spreads: list[Decimal] = []
        volume_changes: dict[OptionType, int] = {
            OptionType.CALL: 0,
            OptionType.PUT: 0,
        }
        for token, current_leg in current.legs.items():
            baseline_leg = baseline.legs.get(token)
            if baseline_leg is None or baseline_leg.mid <= 0:
                continue
            if (
                current_leg.spread_ratio is None
                or current_leg.spread_ratio
                > self._settings.maximum_average_spread_ratio
            ):
                continue
            returns[current_leg.option_type].append(
                (current_leg.mid / baseline_leg.mid - Decimal("1"))
                * Decimal("100")
            )
            spreads.append(current_leg.spread_ratio)
            volume_changes[current_leg.option_type] += max(
                0, current_leg.volume - baseline_leg.volume
            )

        same = returns[same_type]
        opposite = returns[opposite_type]
        if (
            len(same) < self._settings.minimum_legs_per_side
            or len(opposite) < self._settings.minimum_legs_per_side
        ):
            return None
        same_return = _median(same)
        opposite_return = _median(opposite)
        same_breadth = _ratio_count(
            same,
            lambda value: value
            >= self._settings.same_side_leg_return_percent,
        )
        opposite_decay_breadth = _ratio_count(
            opposite,
            lambda value: value
            <= self._settings.opposite_leg_decay_percent,
        )
        same_tokens = {
            token
            for token, leg in current.legs.items()
            if leg.option_type == same_type
        }
        residuals = self._residual_returns(
            same_tokens=same_tokens,
            baseline=baseline,
            current=current,
            history=history,
        )
        residual_return = _median(residuals) if residuals else Decimal("0")
        residual_breadth = (
            _ratio_count(
                residuals,
                lambda value: value
                >= self._settings.minimum_residual_return_percent,
            )
            if residuals
            else Decimal("0")
        )
        same_volume = volume_changes[same_type]
        opposite_volume = volume_changes[opposite_type]
        volume_ratio = (
            Decimal(same_volume) / Decimal(opposite_volume)
            if opposite_volume > 0
            else Decimal("2") if same_volume > 0 else Decimal("0")
        )
        return _SideState(
            side=side,
            same_return=same_return,
            opposite_return=opposite_return,
            return_gap=same_return - opposite_return,
            same_breadth=same_breadth,
            opposite_decay_breadth=opposite_decay_breadth,
            residual_return=residual_return,
            residual_breadth=residual_breadth,
            average_spread_ratio=(
                sum(spreads, Decimal("0")) / Decimal(len(spreads))
            ),
            volume_ratio=volume_ratio,
            leg_count=len(same),
            opposite_leg_count=len(opposite),
        )

    def _residual_returns(
        self,
        *,
        same_tokens: set[str],
        baseline: _Frame,
        current: _Frame,
        history: deque[_Frame],
    ) -> list[Decimal]:
        if not self._settings.aggregate_residual_over_window:
            return [
                value
                for token, value in current.residual_returns.items()
                if token in same_tokens
            ]

        window_frames = tuple(
            frame
            for frame in history
            if baseline.captured_at < frame.captured_at <= current.captured_at
        )
        residuals: list[Decimal] = []
        for token in same_tokens:
            current_leg = current.legs.get(token)
            if (
                token not in baseline.legs
                or current_leg is None
                or current_leg.mid <= 0
            ):
                continue
            changes = [
                frame.residual_changes[token]
                for frame in window_frames
                if token in frame.residual_changes
            ]
            if changes:
                residuals.append(
                    sum(changes, Decimal("0"))
                    / current_leg.mid
                    * Decimal("100")
                )
        return residuals

    def _checks(self, state: _SideState) -> tuple[StrategyCheck, ...]:
        return (
            StrategyCheck(
                "same_side_premium_impulse",
                state.same_return
                >= self._settings.minimum_basket_return_percent,
                f"median={state.same_return:+.3f}%",
                f">= {self._settings.minimum_basket_return_percent}%",
                state.side,
            ),
            StrategyCheck(
                "opposite_leg_decay",
                state.opposite_return
                <= self._settings.maximum_opposite_return_percent,
                f"median={state.opposite_return:+.3f}%",
                f"<= {self._settings.maximum_opposite_return_percent}%",
                state.side,
            ),
            StrategyCheck(
                "cross_side_return_gap",
                state.return_gap >= self._settings.minimum_return_gap_percent,
                f"gap={state.return_gap:+.3f}%",
                f">= {self._settings.minimum_return_gap_percent}%",
                state.side,
            ),
            StrategyCheck(
                "impulse_not_overextended",
                state.return_gap <= self._settings.maximum_return_gap_percent,
                f"gap={state.return_gap:+.3f}%",
                f"<= {self._settings.maximum_return_gap_percent}%",
                state.side,
            ),
            StrategyCheck(
                "same_side_breadth",
                state.same_breadth >= self._settings.minimum_same_side_breadth,
                f"breadth={state.same_breadth:.3f}",
                f">= {self._settings.minimum_same_side_breadth}",
                state.side,
            ),
            StrategyCheck(
                "opposite_decay_breadth",
                state.opposite_decay_breadth
                >= self._settings.minimum_opposite_decay_breadth,
                f"breadth={state.opposite_decay_breadth:.3f}",
                f">= {self._settings.minimum_opposite_decay_breadth}",
                state.side,
            ),
            StrategyCheck(
                "greek_adjusted_residual",
                state.residual_return
                >= self._settings.minimum_residual_return_percent,
                f"median={state.residual_return:+.3f}%",
                f">= {self._settings.minimum_residual_return_percent}%",
                state.side,
            ),
            StrategyCheck(
                "residual_breadth",
                state.residual_breadth
                >= self._settings.minimum_residual_breadth,
                f"breadth={state.residual_breadth:.3f}",
                f">= {self._settings.minimum_residual_breadth}",
                state.side,
            ),
            StrategyCheck(
                "same_side_volume_participation",
                state.volume_ratio >= self._settings.minimum_volume_ratio,
                f"ratio={state.volume_ratio:.3f}",
                f">= {self._settings.minimum_volume_ratio}",
                state.side,
            ),
            StrategyCheck(
                "premium_not_chased",
                state.same_return
                <= self._settings.maximum_basket_chase_percent,
                f"median={state.same_return:+.3f}%",
                f"<= {self._settings.maximum_basket_chase_percent}%",
                state.side,
            ),
            StrategyCheck(
                "cross_strike_liquidity",
                state.average_spread_ratio
                <= self._settings.maximum_average_spread_ratio,
                f"average_spread_ratio={state.average_spread_ratio:.4f}",
                f"<= {self._settings.maximum_average_spread_ratio}",
                state.side,
            ),
        )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _ratio_count(values: list[Decimal], predicate) -> Decimal:
    return Decimal(sum(1 for value in values if predicate(value))) / Decimal(
        len(values)
    )
