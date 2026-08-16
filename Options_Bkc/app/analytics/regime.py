from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.models import MarketRegime


@dataclass(frozen=True)
class RegimeSettings:
    window_size: int = 20
    window_seconds: int = 300
    maximum_observations: int = 4096
    min_trend_displacement_points: Decimal = Decimal("20")
    trend_efficiency_threshold: Decimal = Decimal("0.65")
    compression_range_fraction: Decimal = Decimal("0.20")
    compression_iv_rank_ceiling: Decimal = Decimal("30")


class MarketRegimeClassifier:
    """Classify the market using only information available at the current frame."""

    def __init__(self, settings: RegimeSettings | None = None) -> None:
        self._settings = settings or RegimeSettings()
        self._spots: dict[
            str,
            deque[tuple[datetime | None, Decimal]],
        ] = {}

    def classify(
        self,
        *,
        underlying: str,
        spot: Decimal,
        support: Decimal | None,
        resistance: Decimal | None,
        iv_rank: Decimal,
        unstable_high_vol: bool,
        gamma_coiled: bool,
        captured_at: datetime | None = None,
    ) -> MarketRegime:
        key = underlying.upper()
        observations = self._spots.setdefault(key, deque())
        if (
            captured_at is not None
            and observations
            and observations[-1][0] is not None
            and captured_at < observations[-1][0]
        ):
            observations.clear()
        observations.append((captured_at, spot))
        if captured_at is None:
            while len(observations) > self._settings.window_size:
                observations.popleft()
        else:
            cutoff = captured_at - timedelta(
                seconds=self._settings.window_seconds
            )
            while (
                len(observations) > 1
                and observations[1][0] is not None
                and observations[1][0] <= cutoff
            ):
                observations.popleft()
            while len(observations) > self._settings.maximum_observations:
                observations.popleft()
        prices = tuple(item[1] for item in observations)
        has_full_time_window = (
            captured_at is None
            or (
                observations
                and observations[0][0] is not None
                and (
                    captured_at - observations[0][0]
                ).total_seconds()
                >= self._settings.window_seconds
            )
        )

        if unstable_high_vol:
            return MarketRegime.UNSTABLE_HIGH_VOL

        has_range = (
            support is not None
            and resistance is not None
            and resistance > support
        )
        if has_range and (spot > resistance or spot < support):
            return MarketRegime.TREND_BREAKOUT

        observed_span = max(prices) - min(prices) if prices else Decimal("0")
        range_width = (
            resistance - support
            if has_range
            else max(self._settings.min_trend_displacement_points, observed_span)
        )
        compression_span = max(
            self._settings.min_trend_displacement_points,
            range_width * self._settings.compression_range_fraction,
        )
        if (
            gamma_coiled
            or (
                has_full_time_window
                and len(prices) >= max(5, self._settings.window_size // 2)
                and observed_span <= compression_span
                and iv_rank <= self._settings.compression_iv_rank_ceiling
            )
        ):
            return MarketRegime.COMPRESSION

        # A directional leg inside intact boundaries is still a range rotation.
        # Trend classification is reserved for a broken range or for sessions
        # where no trustworthy OI boundaries are available.
        if has_range:
            return MarketRegime.RANGE

        if has_full_time_window and len(prices) >= 5:
            displacement = abs(prices[-1] - prices[0])
            path = sum(
                (abs(prices[index] - prices[index - 1]) for index in range(1, len(prices))),
                Decimal("0"),
            )
            efficiency = displacement / path if path > 0 else Decimal("0")
            trend_floor = max(
                self._settings.min_trend_displacement_points,
                range_width * Decimal("0.25"),
            )
            if (
                displacement >= trend_floor
                and efficiency >= self._settings.trend_efficiency_threshold
            ):
                return MarketRegime.TREND_BREAKOUT

        return MarketRegime.UNKNOWN
