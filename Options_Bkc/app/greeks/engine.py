from __future__ import annotations

from decimal import Decimal


class GreeksEngine:
    """Placeholder for internal IV and Greeks calculations.

    Phase 1 can compare broker-provided Greeks with internal calculations.
    Keep this dependency-free until formulas and market conventions are locked.
    """

    def implied_volatility(self, *_args: object, **_kwargs: object) -> Decimal | None:
        raise NotImplementedError("IV solver is planned for phase 3.")
