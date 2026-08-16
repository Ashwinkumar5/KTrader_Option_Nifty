from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class StructuralLevelSettings:
    """Time bucket used to stabilize option-chain support and resistance."""

    frame_seconds: int = 240

    def __post_init__(self) -> None:
        if self.frame_seconds <= 0:
            raise ValueError("frame_seconds must be positive")


@dataclass(frozen=True)
class StructuralLevels:
    support: Decimal | None
    resistance: Decimal | None
    frame_seconds: int


@dataclass
class _LevelFrame:
    bucket: int
    active_support: Decimal | None
    active_resistance: Decimal | None
    support_votes: Counter[Decimal] = field(default_factory=Counter)
    resistance_votes: Counter[Decimal] = field(default_factory=Counter)


class StructuralLevelTracker:
    """
    Keep structural levels stable inside a time frame.

    The first observation bootstraps the active levels. Thereafter, the most
    persistent candidate in the completed frame becomes active for the next
    frame. This prevents every fast option-chain refresh from moving the
    support/resistance used by the strategy.
    """

    def __init__(self, settings: StructuralLevelSettings | None = None) -> None:
        self._settings = settings or StructuralLevelSettings()
        self._frames: dict[str, _LevelFrame] = {}

    def update(
        self,
        *,
        underlying: str,
        captured_at: datetime,
        support: Decimal | None,
        resistance: Decimal | None,
    ) -> StructuralLevels:
        key = underlying.upper()
        bucket = int(captured_at.timestamp()) // self._settings.frame_seconds
        state = self._frames.get(key)

        if state is None or bucket < state.bucket:
            state = _LevelFrame(
                bucket=bucket,
                active_support=support,
                active_resistance=resistance,
            )
            self._frames[key] = state
        elif bucket > state.bucket:
            state.active_support = _winner(
                state.support_votes,
                state.active_support,
            )
            state.active_resistance = _winner(
                state.resistance_votes,
                state.active_resistance,
            )
            state.bucket = bucket
            state.support_votes.clear()
            state.resistance_votes.clear()

        if support is not None:
            state.support_votes[support] += 1
        if resistance is not None:
            state.resistance_votes[resistance] += 1

        return StructuralLevels(
            support=state.active_support,
            resistance=state.active_resistance,
            frame_seconds=self._settings.frame_seconds,
        )

    def reset(self) -> None:
        self._frames.clear()


def _winner(
    votes: Counter[Decimal],
    fallback: Decimal | None,
) -> Decimal | None:
    if not votes:
        return fallback
    # Counter preserves first-seen order, so equal vote counts resolve
    # deterministically to the level that persisted first in the frame.
    return votes.most_common(1)[0][0]
