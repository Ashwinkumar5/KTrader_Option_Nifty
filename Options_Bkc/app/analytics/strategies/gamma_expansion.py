from __future__ import annotations

from decimal import Decimal

from app.domain.models import (
    EvidenceFamily,
    SignalSetup,
    StrategyCandidate,
    StrategyFamily,
)

from .base import StrategyEvaluationContext, evidence


class GammaExpansionStrategy:
    family = StrategyFamily.GAMMA_EXPANSION

    def __init__(
        self,
        enabled_features: frozenset[str] | None = None,
    ) -> None:
        self._enabled_features = enabled_features

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        if (
            self._enabled_features is not None
            and "gamma_concentration" not in self._enabled_features
        ):
            return ()
        side = context.gamma_signal
        if side not in {"BUY_CALL", "BUY_PUT"}:
            return ()
        confidence = self._confidence(context)
        reason = (
            f"{context.gamma_reason}; "
            f"confidence={confidence:.4f} "
            f"(iv_rank={context.intraday_iv_rank}, "
            f"india_vix={context.india_vix})"
        )
        return (
            StrategyCandidate(
                family=self.family,
                side=side,
                setup_type=SignalSetup.MOMENTUM_EXPANSION,
                reason=reason,
                confidence=confidence,
                evidence=(
                    evidence("compression", EvidenceFamily.STRUCTURE, side),
                    evidence(
                        "iv_skew_expansion",
                        EvidenceFamily.VOLATILITY,
                        side,
                    ),
                ),
            ),
        )

    def _confidence(self, context: StrategyEvaluationContext) -> Decimal:
        """Score entry quality instead of using a fixed confidence.

        Long-gamma entries are only attractive when premium is cheap relative
        to its own recent level and the volatility regime is not elevated.
        Both factors feed the score so expensive-premium entries are graded
        down rather than emitted at a flat 0.75.
        """

        iv_cost_score = _clamp(
            Decimal("1")
            - context.intraday_iv_rank / Decimal("100"),
            Decimal("0"),
            Decimal("1"),
        )
        vix_score = _vix_regime_score(context.india_vix)
        confidence = (
            Decimal("0.45")
            + iv_cost_score * Decimal("0.25")
            + vix_score * Decimal("0.15")
        )
        return min(Decimal("0.95"), confidence).quantize(Decimal("0.0001"))


def _vix_regime_score(value: Decimal | None) -> Decimal:
    if value is None or value <= 0:
        return Decimal("0")
    if value < Decimal("12"):
        return _clamp(
            Decimal("0.50") + value / Decimal("24"),
            Decimal("0"),
            Decimal("1"),
        )
    if value <= Decimal("22"):
        return Decimal("1")
    if value <= Decimal("30"):
        return _clamp(
            Decimal("1")
            - (value - Decimal("22")) / Decimal("10"),
            Decimal("0.20"),
            Decimal("1"),
        )
    return Decimal("0.20")


def _clamp(
    value: Decimal,
    lower: Decimal,
    upper: Decimal,
) -> Decimal:
    return min(upper, max(lower, value))
