from __future__ import annotations

from app.domain.models import (
    EvidenceFamily,
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


class BreakoutMomentumStrategy:
    family = StrategyFamily.BREAKOUT_MOMENTUM

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        if (
            context.resistance is not None
            and context.spot > context.resistance
        ):
            observed = ratio(
                context.atm_call_volume,
                context.atm_call_oi,
            )
            if observed > context.breakout_threshold:
                side = "BUY_CALL"
                return (
                    StrategyCandidate(
                        family=self.family,
                        side=side,
                        setup_type=SignalSetup.BREAKOUT,
                        reason=(
                            "BREAKOUT VALIDATED: ATM Call Vol/OI "
                            f"{observed} > {context.breakout_threshold}."
                        ),
                        confidence=ratio_confidence(
                            observed,
                            context.breakout_threshold,
                        ),
                        evidence=(
                            evidence(
                                "resistance_break",
                                EvidenceFamily.STRUCTURE,
                                side,
                            ),
                            evidence(
                                "call_volume_oi",
                                EvidenceFamily.FLOW,
                                side,
                            ),
                        ),
                    ),
                )
        if (
            context.support is not None
            and context.spot < context.support
        ):
            observed = ratio(
                context.atm_put_volume,
                context.atm_put_oi,
            )
            if observed > context.breakout_threshold:
                side = "BUY_PUT"
                return (
                    StrategyCandidate(
                        family=self.family,
                        side=side,
                        setup_type=SignalSetup.BREAKOUT,
                        reason=(
                            "BREAKDOWN VALIDATED: ATM Put Vol/OI "
                            f"{observed} > {context.breakout_threshold}."
                        ),
                        confidence=ratio_confidence(
                            observed,
                            context.breakout_threshold,
                        ),
                        evidence=(
                            evidence(
                                "support_break",
                                EvidenceFamily.STRUCTURE,
                                side,
                            ),
                            evidence(
                                "put_volume_oi",
                                EvidenceFamily.FLOW,
                                side,
                            ),
                        ),
                    ),
                )
        return ()
