from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    CandlePattern,
    CandlePatternContext,
    OptionChainSnapshot,
)


@dataclass(frozen=True)
class CandlePatternSettings:
    frame_seconds: int = 240
    doji_body_ratio: Decimal = Decimal("0.12")
    dominant_wick_ratio: Decimal = Decimal("0.60")
    small_wick_ratio: Decimal = Decimal("0.15")
    market_timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        if self.frame_seconds <= 0:
            raise ValueError("frame_seconds must be positive")
        for value in (
            self.doji_body_ratio,
            self.dominant_wick_ratio,
            self.small_wick_ratio,
        ):
            if value < 0 or value > 1:
                raise ValueError("candle ratios must be between zero and one")


@dataclass
class _Bar:
    bucket_start: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal

    def observe(self, price: Decimal) -> None:
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.close_price = price


class CandlePatternTracker:
    """Build event-time bars and expose only the last fully closed 4-minute bar."""

    def __init__(self, settings: CandlePatternSettings | None = None) -> None:
        self._settings = settings or CandlePatternSettings()
        self._timezone = ZoneInfo(self._settings.market_timezone)
        self._bars: dict[str, _Bar] = {}
        self._closed: dict[str, CandlePatternContext] = {}
        self._session_dates: dict[str, date] = {}
        self._last_captured_at: dict[str, datetime] = {}

    def update(self, snapshot: OptionChainSnapshot) -> CandlePatternContext:
        key = snapshot.underlying.upper()
        local_at = snapshot.captured_at.astimezone(self._timezone)
        last = self._last_captured_at.get(key)
        if (
            self._session_dates.get(key) != local_at.date()
            or (last is not None and snapshot.captured_at <= last)
        ):
            self._bars.pop(key, None)
            self._closed.pop(key, None)
            self._session_dates[key] = local_at.date()
        self._last_captured_at[key] = snapshot.captured_at

        bucket_start = self._bucket_start(local_at)
        current = self._bars.get(key)
        if current is None:
            self._bars[key] = _Bar(
                bucket_start=bucket_start,
                open_price=snapshot.spot_price,
                high_price=snapshot.spot_price,
                low_price=snapshot.spot_price,
                close_price=snapshot.spot_price,
            )
            return CandlePatternContext(reason="collecting first closed 4-minute bar")

        if bucket_start == current.bucket_start:
            current.observe(snapshot.spot_price)
        elif bucket_start > current.bucket_start:
            self._closed[key] = self._classify(
                current,
                current_spot=snapshot.spot_price,
            )
            self._bars[key] = _Bar(
                bucket_start=bucket_start,
                open_price=snapshot.spot_price,
                high_price=snapshot.spot_price,
                low_price=snapshot.spot_price,
                close_price=snapshot.spot_price,
            )
        return self._with_follow_through(
            self._closed.get(
                key,
                CandlePatternContext(reason="no closed 4-minute bar"),
            ),
            snapshot.spot_price,
        )

    def _bucket_start(self, observed_at: datetime) -> datetime:
        session_anchor = observed_at.replace(
            hour=9,
            minute=15,
            second=0,
            microsecond=0,
        )
        seconds = int((observed_at - session_anchor).total_seconds())
        bucket = seconds // self._settings.frame_seconds
        return session_anchor + timedelta(
            seconds=bucket * self._settings.frame_seconds
        )

    def _classify(
        self,
        bar: _Bar,
        *,
        current_spot: Decimal,
    ) -> CandlePatternContext:
        total_range = bar.high_price - bar.low_price
        closed_at = bar.bucket_start + timedelta(
            seconds=self._settings.frame_seconds
        )
        if total_range <= 0:
            return CandlePatternContext(
                pattern=CandlePattern.NONE,
                closed_at=closed_at,
                open_price=bar.open_price,
                high_price=bar.high_price,
                low_price=bar.low_price,
                close_price=bar.close_price,
                reason="closed bar has no range",
            )

        body = abs(bar.close_price - bar.open_price)
        upper = bar.high_price - max(bar.open_price, bar.close_price)
        lower = min(bar.open_price, bar.close_price) - bar.low_price
        body_ratio = body / total_range
        upper_ratio = upper / total_range
        lower_ratio = lower / total_range
        pattern = CandlePattern.NONE
        side = None
        if body_ratio <= self._settings.doji_body_ratio:
            if (
                lower_ratio >= self._settings.dominant_wick_ratio
                and upper_ratio <= self._settings.small_wick_ratio
            ):
                pattern = CandlePattern.DRAGONFLY_DOJI
                side = "BUY_CALL"
            elif (
                upper_ratio >= self._settings.dominant_wick_ratio
                and lower_ratio <= self._settings.small_wick_ratio
            ):
                pattern = CandlePattern.GRAVESTONE_DOJI
                side = "BUY_PUT"
            else:
                pattern = CandlePattern.DOJI
        elif (
            lower_ratio >= self._settings.dominant_wick_ratio
            and upper_ratio <= self._settings.small_wick_ratio
        ):
            pattern = CandlePattern.HAMMER
            side = "BUY_CALL"
        elif (
            upper_ratio >= self._settings.dominant_wick_ratio
            and lower_ratio <= self._settings.small_wick_ratio
        ):
            pattern = CandlePattern.SHOOTING_STAR
            side = "BUY_PUT"

        return CandlePatternContext(
            pattern=pattern,
            closed_at=closed_at,
            open_price=bar.open_price,
            high_price=bar.high_price,
            low_price=bar.low_price,
            close_price=bar.close_price,
            potential_side=side,
            follow_through=(
                current_spot > bar.close_price
                if side == "BUY_CALL"
                else current_spot < bar.close_price
                if side == "BUY_PUT"
                else False
            ),
            reason=(
                f"closed 4-minute {pattern.value}; body={body_ratio:.3f}, "
                f"upper_wick={upper_ratio:.3f}, lower_wick={lower_ratio:.3f}"
            ),
        )

    @staticmethod
    def _with_follow_through(
        context: CandlePatternContext,
        current_spot: Decimal,
    ) -> CandlePatternContext:
        if context.close_price is None or context.potential_side is None:
            return context
        follow_through = (
            current_spot > context.close_price
            if context.potential_side == "BUY_CALL"
            else current_spot < context.close_price
        )
        if follow_through == context.follow_through:
            return context
        return CandlePatternContext(
            pattern=context.pattern,
            closed_at=context.closed_at,
            open_price=context.open_price,
            high_price=context.high_price,
            low_price=context.low_price,
            close_price=context.close_price,
            potential_side=context.potential_side,
            follow_through=follow_through,
            reason=context.reason,
        )

    def reset(self) -> None:
        self._bars.clear()
        self._closed.clear()
        self._session_dates.clear()
        self._last_captured_at.clear()
