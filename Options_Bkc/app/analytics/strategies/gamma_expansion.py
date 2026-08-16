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
        return (
            StrategyCandidate(
                family=self.family,
                side=side,
                setup_type=SignalSetup.MOMENTUM_EXPANSION,
                reason=context.gamma_reason,
                confidence=Decimal("0.75"),
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
