from __future__ import annotations

from collections import deque
from datetime import datetime
from math import ceil
from typing import Any

from app.domain.models import MarketTick


class MarketDataRuntimeMetrics:
    """Bounded, allocation-conscious latency measurements for the live worker."""

    def __init__(self, sample_capacity: int = 2048) -> None:
        if sample_capacity <= 0:
            raise ValueError("sample_capacity must be positive")
        self._exchange_to_receipt_ms: deque[float] = deque(
            maxlen=sample_capacity
        )
        self._receipt_to_processing_ms: deque[float] = deque(
            maxlen=sample_capacity
        )
        self._frame_duration_ms: deque[float] = deque(maxlen=256)
        self._chain_write_ms: deque[float] = deque(maxlen=256)
        self._processed_ticks = 0

    def observe_tick(
        self,
        tick: MarketTick,
        *,
        processing_started_at: datetime,
    ) -> None:
        self._processed_ticks += 1
        self._exchange_to_receipt_ms.append(
            _elapsed_ms(tick.received_at, tick.exchange_timestamp)
        )
        self._receipt_to_processing_ms.append(
            _elapsed_ms(processing_started_at, tick.received_at)
        )

    def observe_frame(self, duration_ms: float) -> None:
        self._frame_duration_ms.append(max(duration_ms, 0.0))

    def observe_chain_write(self, duration_ms: float) -> None:
        self._chain_write_ms.append(max(duration_ms, 0.0))

    def snapshot(
        self,
        *,
        feed_health: dict[str, object] | None,
        recorder_health: dict[str, object] | None,
    ) -> dict[str, Any]:
        return {
            "processed_ticks": self._processed_ticks,
            "exchange_to_receipt_ms": _summary(
                self._exchange_to_receipt_ms
            ),
            "receipt_to_processing_ms": _summary(
                self._receipt_to_processing_ms
            ),
            "frame_duration_ms": _summary(self._frame_duration_ms),
            "chain_write_ms": _summary(self._chain_write_ms),
            "feed": feed_health or {"status": "UNAVAILABLE"},
            "recorder": recorder_health or {"status": "DISABLED"},
        }


def _elapsed_ms(later: datetime, earlier: datetime) -> float:
    return max((later - earlier).total_seconds() * 1000, 0.0)


def _summary(values: deque[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "samples": 0,
            "latest": None,
            "p95": None,
            "maximum": None,
        }
    ordered = sorted(values)
    p95_index = max(ceil(len(ordered) * 0.95) - 1, 0)
    return {
        "samples": len(ordered),
        "latest": round(values[-1], 3),
        "p95": round(ordered[p95_index], 3),
        "maximum": round(ordered[-1], 3),
    }
