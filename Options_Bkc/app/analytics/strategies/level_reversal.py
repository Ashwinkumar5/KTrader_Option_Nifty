from __future__ import annotations

from decimal import Decimal

from app.domain.models import (
    EvidenceFamily,
    OpeningState,
    SignalSetup,
    StrategyCandidate,
    StrategyFamily,
)

from .base import (
    StrategyEvaluationContext,
    evidence,
    ratio,
    ratio_confidence,
)


class LevelReversalStrategy:
    family = StrategyFamily.LEVEL_REVERSAL

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        candidates: list[StrategyCandidate] = []
        if (
            context.expected_upper is not None
            and context.expected_lower is not None
            and (
                context.spot >= context.expected_upper
                or context.spot <= context.expected_lower
            )
            and context.pcr_oi is not None
            and Decimal("0.9") <= context.pcr_oi <= Decimal("1.1")
        ):
            side = (
                "BUY_PUT"
                if context.spot >= context.expected_upper
                else "BUY_CALL"
            )
            boundary = "Upper" if side == "BUY_PUT" else "Lower"
            candidates.append(
                StrategyCandidate(
                    family=self.family,
                    side=side,
                    setup_type=SignalSetup.LEVEL_REVERSAL,
                    reason=(
                        f"MEAN REVERSION: Spot {context.spot} hit "
                        f"Boundary {boundary}. PCR flat."
                    ),
                    confidence=Decimal("0.65"),
                    evidence=(
                        evidence(
                            "expected_move_boundary",
                            EvidenceFamily.STRUCTURE,
                            side,
                        ),
                        evidence(
                            "pcr_flat",
                            EvidenceFamily.POSITIONING,
                            side,
                        ),
                    ),
                )
            )

        if (
            context.spot_delta < 0
            and context.near_support
            and context.support_oi > 0
        ):
            observed = ratio(
                context.support_volume,
                context.support_oi,
            )
            if (
                observed >= context.exhaustion_threshold
                and context.support_oi_change <= 0
            ):
                candidates.append(
                    self._exhaustion_candidate(
                        side="BUY_CALL",
                        reason=(
                            "EXHAUSTION REVERSAL: Capitulation flow "
                            "detected at Support."
                        ),
                        observed=observed,
                        threshold=context.exhaustion_threshold,
                        structure_code="support_rejection",
                        flow_code="put_exhaustion",
                    )
                )
        elif (
            context.spot_delta > 0
            and context.near_resistance
            and context.resistance_oi > 0
        ):
            observed = ratio(
                context.resistance_volume,
                context.resistance_oi,
            )
            if (
                observed >= context.exhaustion_threshold
                and context.resistance_oi_change <= 0
            ):
                candidates.append(
                    self._exhaustion_candidate(
                        side="BUY_PUT",
                        reason=(
                            "EXHAUSTION TOP: Climax buying volume "
                            "exhaustion at Resistance."
                        ),
                        observed=observed,
                        threshold=context.exhaustion_threshold,
                        structure_code="resistance_rejection",
                        flow_code="call_exhaustion",
                    )
                )

        if context.rotation_signal in {"BUY_CALL", "BUY_PUT"}:
            candidates.append(
                StrategyCandidate(
                    family=self.family,
                    side=context.rotation_signal,
                    setup_type=SignalSetup.RANGE_ROTATION,
                    reason=context.rotation_reason,
                    confidence=Decimal("0.70"),
                    evidence=(
                        evidence(
                            "defended_boundary",
                            EvidenceFamily.STRUCTURE,
                            context.rotation_signal,
                        ),
                        evidence(
                            "range_rotation",
                            EvidenceFamily.FLOW,
                            context.rotation_signal,
                        ),
                    ),
                )
            )
        local_reversal = self._local_level_reversal(context)
        if local_reversal is not None:
            candidates.append(local_reversal)
        return tuple(candidates)

    def _local_level_reversal(
        self,
        context: StrategyEvaluationContext,
    ) -> StrategyCandidate | None:
        candle = context.candle_pattern
        if (
            candle is None
            or candle.potential_side not in {"BUY_CALL", "BUY_PUT"}
            or not candle.follow_through
            or candle.closed_at is None
            or candle.close_price is None
        ):
            return None
        age_seconds = (
            context.captured_at - candle.closed_at
        ).total_seconds()
        if age_seconds < 0 or age_seconds > 240:
            return None

        side = candle.potential_side
        tolerance = max(Decimal("1"), context.level_tolerance)
        if side == "BUY_PUT":
            level = context.local_resistance
            touched = (
                level is not None
                and candle.high_price is not None
                and abs(candle.high_price - level) <= tolerance
            )
            followed_through = context.spot < candle.close_price
            positioning_agrees = (
                context.pcr_oi is not None
                and context.pcr_oi <= Decimal("1")
            )
            futures_opposes = (
                context.futures_flow is not None
                and context.futures_flow.side == "BUY_CALL"
                and context.futures_flow.strength > Decimal("0")
            )
            opening_failure = (
                context.opening_context is not None
                and context.opening_context.state
                in {
                    OpeningState.OPENING_DRIVE_UP,
                    OpeningState.GAP_AND_GO_UP,
                }
            )
            boundary_name = "resistance"
        else:
            level = context.local_support
            touched = (
                level is not None
                and candle.low_price is not None
                and abs(candle.low_price - level) <= tolerance
            )
            followed_through = context.spot > candle.close_price
            positioning_agrees = (
                context.pcr_oi is not None
                and context.pcr_oi >= Decimal("1")
            )
            futures_opposes = (
                context.futures_flow is not None
                and context.futures_flow.side == "BUY_PUT"
                and context.futures_flow.strength > Decimal("0")
            )
            opening_failure = (
                context.opening_context is not None
                and context.opening_context.state
                in {
                    OpeningState.OPENING_DRIVE_DOWN,
                    OpeningState.GAP_AND_GO_DOWN,
                }
            )
            boundary_name = "support"

        if (
            not touched
            or not followed_through
            or not positioning_agrees
            or futures_opposes
            or level is None
        ):
            return None

        label = (
            "OPENING FAILURE REVERSAL"
            if opening_failure
            else "LOCAL LEVEL REVERSAL"
        )
        return StrategyCandidate(
            family=self.family,
            side=side,
            setup_type=SignalSetup.LOCAL_LEVEL_REVERSAL,
            reason=(
                f"{label}: closed {candle.pattern.value} rejected local "
                f"{boundary_name} {level}; follow-through and PCR confirmed "
                f"{side}."
            ),
            confidence=(
                Decimal("0.78")
                if opening_failure
                else Decimal("0.72")
            ),
            evidence=(
                evidence(
                    "local_oi_level_rejection",
                    EvidenceFamily.STRUCTURE,
                    side,
                    Decimal("0.80"),
                ),
                evidence(
                    "closed_reversal_candle_follow_through",
                    EvidenceFamily.PRICE_ACTION,
                    side,
                    Decimal("0.80"),
                ),
                evidence(
                    "pcr_context",
                    EvidenceFamily.POSITIONING,
                    side,
                    Decimal("0.60"),
                ),
            ),
            activation_level=level,
        )

    def _exhaustion_candidate(
        self,
        *,
        side: str,
        reason: str,
        observed: Decimal,
        threshold: Decimal,
        structure_code: str,
        flow_code: str,
    ) -> StrategyCandidate:
        return StrategyCandidate(
            family=self.family,
            side=side,
            setup_type=SignalSetup.LEVEL_REVERSAL,
            reason=reason,
            confidence=ratio_confidence(observed, threshold),
            evidence=(
                evidence(structure_code, EvidenceFamily.STRUCTURE, side),
                evidence(flow_code, EvidenceFamily.FLOW, side),
            ),
        )
