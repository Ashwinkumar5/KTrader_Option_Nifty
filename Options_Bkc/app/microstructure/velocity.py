from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal


class PremiumVelocityTracker:
    """Calculates signed option-premium movement in points per second per token."""

    def __init__(self, *, window_seconds: int) -> None:
        self._window = timedelta(seconds=max(1, window_seconds))
        self._history: dict[str, deque[tuple[datetime, Decimal]]] = {}

    def update(self, *, token: str, captured_at: datetime, premium: Decimal) -> tuple[Decimal | None, int]:
        history = self._history.setdefault(token, deque())
        history.append((captured_at, premium))
        cutoff = captured_at - self._window
        while len(history) > 1 and history[0][0] < cutoff:
            history.popleft()

        if len(history) < 2:
            return None, len(history)
        start_at, start_price = history[0]
        elapsed = Decimal(str((captured_at - start_at).total_seconds()))
        if elapsed <= 0:
            return None, len(history)
        return ((premium - start_price) / elapsed).quantize(Decimal("0.0001")), len(history)

    def reset(self) -> None:
        self._history.clear()
