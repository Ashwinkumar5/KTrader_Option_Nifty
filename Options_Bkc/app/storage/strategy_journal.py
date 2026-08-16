from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from app.domain.models import (
    AnalyticsSnapshot,
    OptionType,
    StrategyCandidate,
    StrategyFamily,
)


_STOP: Final = object()
_MAX_FEATURES: Final = 5


def strategy_journal_filename(
    strategy_name: str,
    started_at: datetime,
) -> str:
    """Return a stable, human-readable journal name for one worker session."""

    name = _safe_strategy_name(strategy_name)
    if started_at.tzinfo is None:
        raise ValueError("strategy journal timestamp must be timezone-aware")
    return f"{name}_{started_at.strftime('%Y%m%d_%H%M%S')}.journal.log"


@dataclass(frozen=True, slots=True)
class JournalFeature:
    name: str
    status: str
    mandatory: bool

    def render(self) -> str:
        requirement = "MANDATORY" if self.mandatory else "NON_MANDATORY"
        return f"{self.name}={self.status}:{requirement}"


class StrategyJournal:
    """Small, non-blocking human journal for one strategy worker.

    The JSONL recorder remains the complete replay source.  This journal only
    records a target's lifecycle and the five most relevant supporting
    features, so operators can inspect a strategy without parsing a tape.
    """

    __slots__ = (
        "_last_state",
        "_path",
        "_queue",
        "_started",
        "_strategy_name",
        "_writer_task",
    )

    def __init__(
        self,
        path: Path,
        *,
        strategy_name: str,
        queue_capacity: int = 128,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("strategy journal queue capacity must be positive")
        self._path = path
        self._strategy_name = _safe_strategy_name(strategy_name)
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(queue_capacity)
        self._last_state: dict[tuple[str, str, str], str] = {}
        self._started = False
        self._writer_task: asyncio.Task[None] | None = None

    @property
    def path(self) -> Path:
        return self._path

    async def start(self) -> None:
        if self._started:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._writer_task = asyncio.create_task(
            self._write_lines(),
            name=f"strategy-journal-{self._strategy_name}",
        )
        self._started = True

    def record_target(
        self,
        *,
        analytics: AnalyticsSnapshot,
        state: str,
        router_status: str | None = None,
    ) -> str | None:
        """Record one meaningful target state change, never a frame-by-frame log."""

        if not self._started:
            return None
        strike = analytics.target_strike
        side = _option_side(analytics)
        features = journal_features(analytics, self._strategy_name)
        if strike is None or side is None or not features:
            return None
        key = (analytics.underlying.upper(), str(strike), side)
        previous = self._last_state.get(key)
        if previous == state:
            return None

        captured_at = analytics.captured_at
        if captured_at.tzinfo is None:
            return None
        fields = [
            captured_at.isoformat(),
            f"STRATEGY={self._strategy_name}",
            f"UNDERLYING={analytics.underlying.upper()}",
            f"STRIKE={strike}",
            f"SIDE={side}",
            f"STATE={state}",
            "FEATURES=" + " | ".join(item.render() for item in features),
        ]
        if router_status is not None:
            fields.append(f"ROUTER={router_status}")
        line = " | ".join(fields)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            # This is an operator aid, never a reason to delay or change a trade.
            return None
        self._last_state[key] = state
        return line

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False
        task = self._writer_task
        self._writer_task = None
        if task is None:
            return
        try:
            self._queue.put_nowait(_STOP)
        except asyncio.QueueFull:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _write_lines(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is _STOP:
                    return
                try:
                    await asyncio.to_thread(_append_line, self._path, str(item))
                except OSError:
                    # Journaling must never alter a strategy's decision path.
                    return
            finally:
                self._queue.task_done()


def journal_features(
    analytics: AnalyticsSnapshot,
    strategy_name: str,
) -> tuple[JournalFeature, ...]:
    """Return at most five passed features for this strategy's target.

    Evidence is emitted by the strategy only when it supports the candidate.
    Mandatory evidence is shown first; ties are ordered by strength then name,
    making the same decision produce the same readable feature order.
    """

    family = _strategy_family(strategy_name)
    if family is None:
        return ()
    candidate = _candidate_for_family(analytics, family)
    if candidate is None:
        return ()
    evidence = sorted(
        candidate.evidence,
        key=lambda item: (
            not item.mandatory,
            -abs(item.strength),
            item.code,
        ),
    )
    return tuple(
        JournalFeature(
            name=item.code,
            status="PASS",
            mandatory=item.mandatory,
        )
        for item in evidence[:_MAX_FEATURES]
    )


def _candidate_for_family(
    analytics: AnalyticsSnapshot,
    family: StrategyFamily,
) -> StrategyCandidate | None:
    candidates = tuple(
        candidate
        for candidate in analytics.strategy_candidates
        if candidate.family == family
    )
    if not candidates:
        return None
    if analytics.signal in {"BUY_CALL", "BUY_PUT"}:
        return next(
            (candidate for candidate in candidates if candidate.side == analytics.signal),
            candidates[0],
        )
    return candidates[0]


def _option_side(analytics: AnalyticsSnapshot) -> str | None:
    if analytics.target_option_type == OptionType.CALL or analytics.signal == "BUY_CALL":
        return "CALL"
    if analytics.target_option_type == OptionType.PUT or analytics.signal == "BUY_PUT":
        return "PUT"
    return None


def _strategy_family(strategy_name: str) -> StrategyFamily | None:
    normalized = strategy_name.strip().upper()
    try:
        return StrategyFamily(normalized)
    except ValueError:
        return None


def _safe_strategy_name(strategy_name: str) -> str:
    value = strategy_name.strip().upper()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError("strategy name must be a plain filename component")
    return value


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.write("\n")
