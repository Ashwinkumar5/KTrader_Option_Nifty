from __future__ import annotations

from app.domain.models import (
    StrategyCandidate,
    StrategyDiagnostic,
    StrategyFamily,
)

from .base import StrategyEvaluationContext, StrategyEvaluator


class StrategyRegistry:
    """Validated immutable evaluator order for auditable candidate research."""

    def __init__(
        self,
        *,
        evaluators: tuple[StrategyEvaluator, ...],
        enabled: frozenset[StrategyFamily],
        priorities: dict[StrategyFamily, int],
    ) -> None:
        families = [item.family for item in evaluators]
        if len(families) != len(set(families)):
            raise ValueError("duplicate strategy evaluator family")
        missing = enabled - set(families)
        if missing:
            raise ValueError(
                "missing strategy evaluators: "
                + ", ".join(sorted(item.value for item in missing))
            )
        # Evaluate all three small modules so disabled-family candidates remain
        # visible in replay ablations. The resolver alone controls selection.
        self._evaluators = tuple(
            sorted(
                evaluators,
                key=lambda item: (
                    priorities[item.family],
                    item.family.value,
                ),
            )
        )
        self._diagnostics: tuple[StrategyDiagnostic, ...] = ()

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        candidates: list[StrategyCandidate] = []
        diagnostics: list[StrategyDiagnostic] = []
        for evaluator in self._evaluators:
            candidates.extend(evaluator.evaluate(context))
            diagnostic = getattr(evaluator, "last_diagnostic", None)
            if isinstance(diagnostic, StrategyDiagnostic):
                diagnostics.append(diagnostic)
        self._diagnostics = tuple(diagnostics)
        return tuple(candidates)

    @property
    def diagnostics(self) -> tuple[StrategyDiagnostic, ...]:
        return self._diagnostics

    def reset(self) -> None:
        self._diagnostics = ()
        for evaluator in self._evaluators:
            reset = getattr(evaluator, "reset", None)
            if callable(reset):
                reset()
