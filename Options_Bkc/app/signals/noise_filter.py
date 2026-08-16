from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


_DIRECTIONAL = {"BUY_CALL", "BUY_PUT"}


class DebouncePhase(StrEnum):
    WAITING = "WAITING"
    ARMED = "ARMED"
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class SignalDebounceSettings:
    frame_seconds: int = 15
    window_frames: int = 3
    min_confirmed_frames: int = 2

    def __post_init__(self) -> None:
        if self.frame_seconds <= 0:
            raise ValueError("frame_seconds must be positive")
        if self.window_frames <= 0:
            raise ValueError("window_frames must be positive")
        if not 1 <= self.min_confirmed_frames <= self.window_frames:
            raise ValueError(
                "min_confirmed_frames must be within the debounce window"
            )


@dataclass(frozen=True)
class SignalDebounceDecision:
    signal: str
    reason: str
    phase: DebouncePhase
    confirmed_side: str | None
    closed_frame_votes: tuple[str, ...]


@dataclass
class _DebounceState:
    current_bucket: int
    current_signal: str
    current_reason: str
    closed_frames: deque[str] = field(default_factory=deque)
    confirmed_side: str | None = None


class DirectionalSignalDebouncer:
    """Confirm strategy candidates from closed time buckets.

    The latest observation in each bucket acts as that bucket's close. A side
    needs a two-of-three closed-frame majority by default. One contrary frame
    degrades a confirmed setup; two contrary frames invalidate it.
    """

    def __init__(self, settings: SignalDebounceSettings | None = None) -> None:
        self._settings = settings or SignalDebounceSettings()
        self._states: dict[str, _DebounceState] = {}

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
        signal: str | None,
        reason: str,
    ) -> SignalDebounceDecision:
        key = underlying.upper()
        normalized = signal if signal in _DIRECTIONAL else "NEUTRAL"
        bucket = int(captured_at.timestamp()) // self._settings.frame_seconds
        state = self._states.get(key)

        if state is None or bucket < state.current_bucket:
            state = _DebounceState(
                current_bucket=bucket,
                current_signal=normalized,
                current_reason=reason,
                closed_frames=deque(maxlen=self._settings.window_frames),
            )
            self._states[key] = state
        elif bucket == state.current_bucket:
            # The latest observation becomes the close if this bucket ends now.
            state.current_signal = normalized
            state.current_reason = reason
        else:
            state.closed_frames.append(state.current_signal)
            state.current_bucket = bucket
            state.current_signal = normalized
            state.current_reason = reason

        votes = tuple(state.closed_frames)
        counts = Counter(votes)
        winner = next(
            (
                side
                for side in ("BUY_CALL", "BUY_PUT")
                if counts[side] >= self._settings.min_confirmed_frames
            ),
            None,
        )

        if winner is not None:
            state.confirmed_side = winner
        elif state.confirmed_side is not None:
            contrary = sum(
                vote != state.confirmed_side for vote in state.closed_frames
            )
            if contrary >= self._settings.min_confirmed_frames:
                state.confirmed_side = None

        if state.confirmed_side is not None and normalized == state.confirmed_side:
            return SignalDebounceDecision(
                signal=state.confirmed_side,
                reason=(
                    f"NOISE CONFIRMED "
                    f"{counts[state.confirmed_side]}/"
                    f"{self._settings.window_frames}: {reason}"
                ),
                phase=DebouncePhase.CONFIRMED,
                confirmed_side=state.confirmed_side,
                closed_frame_votes=votes,
            )

        if state.confirmed_side is not None:
            return SignalDebounceDecision(
                signal="NEUTRAL",
                reason=(
                    f"NOISE DEGRADED {state.confirmed_side}: current frame "
                    f"is {normalized}; awaiting recovery or "
                    f"{self._settings.min_confirmed_frames} contrary closes."
                ),
                phase=DebouncePhase.DEGRADED,
                confirmed_side=state.confirmed_side,
                closed_frame_votes=votes,
            )

        if normalized in _DIRECTIONAL:
            return SignalDebounceDecision(
                signal="NEUTRAL",
                reason=(
                    f"NOISE ARMED {normalized}: awaiting "
                    f"{self._settings.min_confirmed_frames}/"
                    f"{self._settings.window_frames} closed frames. {reason}"
                ),
                phase=DebouncePhase.ARMED,
                confirmed_side=None,
                closed_frame_votes=votes,
            )

        return SignalDebounceDecision(
            signal="NEUTRAL",
            reason=reason,
            phase=DebouncePhase.WAITING,
            confirmed_side=None,
            closed_frame_votes=votes,
        )
