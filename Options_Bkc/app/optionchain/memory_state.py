from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import NamedTuple


logger = logging.getLogger("GammaSpring")


class TickSnapshot(NamedTuple):
    captured_at: datetime
    spot_price: float
    atm_iv: float
    iv_rank: float
    otm_call_iv: float
    otm_put_iv: float
    atm_call_delta: float
    otm_call_token: str | None = None
    otm_put_token: str | None = None
    otm_call_mid: float | None = None
    otm_put_mid: float | None = None
    otm_call_spread_ratio: float | None = None
    otm_put_spread_ratio: float | None = None


class CoiledSpringDetector:
    """Detect compression over an event-time window, independent of cadence."""

    __slots__ = (
        "_history",
        "_window_seconds",
        "_minimum_observations",
        "_maximum_observations",
        "_pin_range_pts",
        "_iv_rank_threshold",
        "_skew_spike_threshold",
        "_minimum_sensor_mid",
        "_maximum_sensor_spread_ratio",
        "_minimum_confirmations",
        "_cooldown_seconds",
        "_pending_side",
        "_pending_confirmations",
        "_last_emitted_at",
    )

    def __init__(
        self,
        *,
        window_seconds: int = 300,
        minimum_observations: int = 5,
        maximum_observations: int = 4096,
        pin_range_pts: float = 15.0,
        iv_rank_threshold: float = 25.0,
        skew_spike_threshold: float = 1.10,
        minimum_sensor_mid: float = 1.0,
        maximum_sensor_spread_ratio: float = 0.05,
        minimum_confirmations: int = 2,
        cooldown_seconds: int = 300,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if minimum_observations < 2:
            raise ValueError("minimum_observations must be at least two")
        if maximum_observations < minimum_observations:
            raise ValueError(
                "maximum_observations must cover minimum_observations"
            )
        if minimum_sensor_mid <= 0:
            raise ValueError("minimum_sensor_mid must be positive")
        if maximum_sensor_spread_ratio <= 0:
            raise ValueError("maximum_sensor_spread_ratio must be positive")
        if minimum_confirmations <= 0:
            raise ValueError("minimum_confirmations must be positive")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        self._history: deque[TickSnapshot] = deque()
        self._window_seconds = window_seconds
        self._minimum_observations = minimum_observations
        self._maximum_observations = maximum_observations
        self._pin_range_pts = pin_range_pts
        self._iv_rank_threshold = iv_rank_threshold
        self._skew_spike_threshold = skew_spike_threshold
        self._minimum_sensor_mid = minimum_sensor_mid
        self._maximum_sensor_spread_ratio = maximum_sensor_spread_ratio
        self._minimum_confirmations = minimum_confirmations
        self._cooldown_seconds = cooldown_seconds
        self._pending_side: str | None = None
        self._pending_confirmations = 0
        self._last_emitted_at: datetime | None = None

    def update(self, snapshot: TickSnapshot) -> None:
        if (
            self._history
            and snapshot.captured_at < self._history[-1].captured_at
        ):
            self._history.clear()
            self._pending_side = None
            self._pending_confirmations = 0
            self._last_emitted_at = None
        self._history.append(snapshot)
        cutoff = snapshot.captured_at - timedelta(
            seconds=self._window_seconds
        )
        # Retain one predecessor as the baseline crossing the window boundary.
        while (
            len(self._history) > 1
            and self._history[1].captured_at <= cutoff
        ):
            self._history.popleft()
        while len(self._history) > self._maximum_observations:
            self._history.popleft()

    def evaluate_gamma_blast(self) -> tuple[str | None, str]:
        if len(self._history) < self._minimum_observations:
            self._reset_pending()
            return None, "BUFFER_FILLING: Not enough observations for baseline."
        current = self._history[-1]
        baseline = self._history[0]
        observed_seconds = (
            current.captured_at - baseline.captured_at
        ).total_seconds()
        if observed_seconds < self._window_seconds:
            self._reset_pending()
            return (
                None,
                "BUFFER_FILLING: Event-time compression window is incomplete.",
            )

        if current.iv_rank > self._iv_rank_threshold:
            self._reset_pending()
            return (
                None,
                f"REJECT: IV Rank ({current.iv_rank:.1f}%) is too high. "
                "Spring not compressed.",
            )

        max_spot = max(tick.spot_price for tick in self._history)
        min_spot = min(tick.spot_price for tick in self._history)
        price_range = max_spot - min_spot
        if price_range > self._pin_range_pts:
            self._reset_pending()
            return (
                None,
                f"REJECT: Spot price not pinned. Range is {price_range:.2f} "
                f"pts (Limit: {self._pin_range_pts}).",
            )

        call_growth = (
            current.otm_call_iv / baseline.otm_call_iv
            if baseline.otm_call_iv > 0
            else 0
        )
        put_growth = (
            current.otm_put_iv / baseline.otm_put_iv
            if baseline.otm_put_iv > 0
            else 0
        )
        if (
            call_growth >= self._skew_spike_threshold
            and call_growth > put_growth
        ):
            quality_error = self._sensor_quality_error(
                baseline=baseline,
                current=current,
                side="CALL",
            )
            if quality_error is not None:
                self._reset_pending()
                return None, f"REJECT: {quality_error}"
            return self._confirm_signal(
                "BUY_CALL",
                "GAMMA CALL EXPANSION: Nifty pinned over the event-time "
                f"window; OTM Call IV rose {(call_growth - 1) * 100:.1f}%.",
                price_range=price_range,
                iv_rank=current.iv_rank,
                growth_percent=(call_growth - 1) * 100,
            )
        if (
            put_growth >= self._skew_spike_threshold
            and put_growth > call_growth
        ):
            quality_error = self._sensor_quality_error(
                baseline=baseline,
                current=current,
                side="PUT",
            )
            if quality_error is not None:
                self._reset_pending()
                return None, f"REJECT: {quality_error}"
            return self._confirm_signal(
                "BUY_PUT",
                "GAMMA PUT EXPANSION: Nifty pinned over the event-time "
                f"window; OTM Put IV rose {(put_growth - 1) * 100:.1f}%.",
                price_range=price_range,
                iv_rank=current.iv_rank,
                growth_percent=(put_growth - 1) * 100,
            )
        self._reset_pending()
        return (
            None,
            "WAIT: Spring is coiled, but no skew divergence is present.",
        )

    def _sensor_quality_error(
        self,
        *,
        baseline: TickSnapshot,
        current: TickSnapshot,
        side: str,
    ) -> str | None:
        if side == "CALL":
            baseline_token = baseline.otm_call_token
            current_token = current.otm_call_token
            baseline_mid = baseline.otm_call_mid
            current_mid = current.otm_call_mid
            baseline_spread = baseline.otm_call_spread_ratio
            current_spread = current.otm_call_spread_ratio
            observed_tokens = {
                item.otm_call_token for item in self._history
            }
        else:
            baseline_token = baseline.otm_put_token
            current_token = current.otm_put_token
            baseline_mid = baseline.otm_put_mid
            current_mid = current.otm_put_mid
            baseline_spread = baseline.otm_put_spread_ratio
            current_spread = current.otm_put_spread_ratio
            observed_tokens = {
                item.otm_put_token for item in self._history
            }
        if not baseline_token or not current_token:
            return f"OTM {side} sensor contract is unavailable"
        if observed_tokens != {current_token}:
            return f"OTM {side} sensor contract changed inside the window"
        minimum_mid = min(
            value
            for value in (baseline_mid, current_mid)
            if value is not None
        ) if baseline_mid is not None or current_mid is not None else None
        if minimum_mid is None or minimum_mid < self._minimum_sensor_mid:
            rendered = (
                "unavailable" if minimum_mid is None else f"{minimum_mid:.2f}"
            )
            return (
                f"OTM {side} sensor midpoint {rendered} is below "
                f"{self._minimum_sensor_mid:.2f}"
            )
        maximum_spread = max(
            value
            for value in (baseline_spread, current_spread)
            if value is not None
        ) if baseline_spread is not None or current_spread is not None else None
        if maximum_spread is None or maximum_spread > self._maximum_sensor_spread_ratio:
            rendered = (
                "unavailable"
                if maximum_spread is None
                else f"{maximum_spread * 100:.1f}%"
            )
            return (
                f"OTM {side} sensor spread {rendered} exceeds "
                f"{self._maximum_sensor_spread_ratio * 100:.1f}%"
            )
        return None

    def _confirm_signal(
        self,
        side: str,
        reason: str,
        *,
        price_range: float,
        iv_rank: float,
        growth_percent: float,
    ) -> tuple[str | None, str]:
        current_at = self._history[-1].captured_at
        if (
            self._last_emitted_at is not None
            and (
                current_at - self._last_emitted_at
            ).total_seconds() < self._cooldown_seconds
        ):
            self._reset_pending()
            return (
                None,
                f"SUPPRESSED: Gamma event cooldown is active for {side}.",
            )
        if self._pending_side == side:
            self._pending_confirmations += 1
        else:
            self._pending_side = side
            self._pending_confirmations = 1
        if self._pending_confirmations < self._minimum_confirmations:
            return (
                None,
                f"CONFIRMING {side}: {self._pending_confirmations}/"
                f"{self._minimum_confirmations} valid Gamma frames.",
            )
        self._last_emitted_at = current_at
        self._reset_pending()
        logger.info(
            "GAMMA BLAST %s | pinned=%.2f IV-rank=%.1f skew=%.1f%%",
            "UP" if side == "BUY_CALL" else "DOWN",
            price_range,
            iv_rank,
            growth_percent,
        )
        return side, reason

    def _reset_pending(self) -> None:
        self._pending_side = None
        self._pending_confirmations = 0
