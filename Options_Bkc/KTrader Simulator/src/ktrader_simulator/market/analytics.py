from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ktrader_simulator.domain.models import MarketSnapshot


class MetricStatus(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class StrikeAnalyticsRow:
    strike: Decimal
    call_iv: Decimal | None
    call_volume: Decimal | None
    call_oi: Decimal | None
    call_volume_oi: Decimal | None
    put_oi: Decimal | None
    put_volume: Decimal | None
    put_volume_oi: Decimal | None
    put_iv: Decimal | None
    oi_pcr: Decimal | None
    oi_pcr_status: MetricStatus
    volume_pcr: Decimal | None
    put_volume_oi_status: MetricStatus
    straddle: Decimal | None
    straddle_status: MetricStatus
    call_build_up: str
    put_build_up: str


@dataclass(frozen=True, slots=True)
class ChainAnalyticsSnapshot:
    underlying: str
    rows: tuple[StrikeAnalyticsRow, ...]
    oi_pcr: Decimal | None
    volume_pcr: Decimal | None
    call_volume_oi: Decimal | None
    put_volume_oi: Decimal | None
    oi_pcr_status: MetricStatus
    volume_pcr_status: MetricStatus
    put_volume_oi_status: str
    call_volume_oi_status: str


class ChainAnalyticsEngine:
    """Three-minute, five-strike calculation cache; no broker or UI work."""

    def __init__(
        self,
        *,
        oi_pcr_bearish_threshold: Decimal = Decimal("0.95"),
        oi_pcr_bullish_threshold: Decimal = Decimal("1.05"),
        volume_pcr_bearish_threshold: Decimal = Decimal("0.90"),
        volume_pcr_bullish_threshold: Decimal = Decimal("1.10"),
    ) -> None:
        if oi_pcr_bearish_threshold >= oi_pcr_bullish_threshold:
            raise ValueError("OI PCR bearish threshold must be lower than bullish threshold")
        if volume_pcr_bearish_threshold >= volume_pcr_bullish_threshold:
            raise ValueError("Volume PCR bearish threshold must be lower than bullish threshold")
        self._oi_pcr_bearish_threshold = oi_pcr_bearish_threshold
        self._oi_pcr_bullish_threshold = oi_pcr_bullish_threshold
        self._volume_pcr_bearish_threshold = volume_pcr_bearish_threshold
        self._volume_pcr_bullish_threshold = volume_pcr_bullish_threshold
        self._previous: dict[str, tuple[Decimal | None, Decimal | None]] = {}

    def build(self, snapshot: MarketSnapshot) -> ChainAnalyticsSnapshot:
        rows: list[StrikeAnalyticsRow] = []
        call_oi = put_oi = call_volume = put_volume = Decimal("0")
        for row in snapshot.rows:
            call_quote, put_quote = row.call_quote, row.put_quote
            c_oi = call_quote.open_interest if call_quote else None
            p_oi = put_quote.open_interest if put_quote else None
            c_vol = call_quote.volume if call_quote else None
            p_vol = put_quote.volume if put_quote else None
            call_build_up = self._build_up(row.call.instrument.token, call_quote)
            put_build_up = self._build_up(row.put.instrument.token, put_quote)
            strike_oi_pcr = _ratio(p_oi, c_oi)
            put_volume_oi = _ratio(p_vol, p_oi)
            straddle = _sum(
                row.call_quote.ltp if row.call_quote else None,
                row.put_quote.ltp if row.put_quote else None,
            )
            if row.strike >= snapshot.atm_strike:
                call_oi += c_oi or Decimal("0")
                call_volume += c_vol or Decimal("0")
            if row.strike <= snapshot.atm_strike:
                put_oi += p_oi or Decimal("0")
                put_volume += p_vol or Decimal("0")
            rows.append(
                StrikeAnalyticsRow(
                    strike=row.strike,
                    call_iv=call_quote.implied_volatility if call_quote else None,
                    call_volume=c_vol,
                    call_oi=c_oi,
                    call_volume_oi=_ratio(c_vol, c_oi),
                    put_oi=p_oi,
                    put_volume=p_vol,
                    put_volume_oi=put_volume_oi,
                    put_iv=put_quote.implied_volatility if put_quote else None,
                    oi_pcr=strike_oi_pcr,
                    oi_pcr_status=_ratio_status(
                        strike_oi_pcr,
                        bearish_threshold=self._oi_pcr_bearish_threshold,
                        bullish_threshold=self._oi_pcr_bullish_threshold,
                    ),
                    volume_pcr=_ratio(p_vol, c_vol),
                    put_volume_oi_status=_put_flow_status(put_build_up),
                    straddle=straddle,
                    straddle_status=MetricStatus.NEUTRAL,
                    call_build_up=call_build_up,
                    put_build_up=put_build_up,
                )
            )
        oi_pcr = _ratio(put_oi, call_oi)
        volume_pcr = _ratio(put_volume, call_volume)
        return ChainAnalyticsSnapshot(
            underlying=snapshot.underlying,
            rows=tuple(rows),
            oi_pcr=oi_pcr,
            volume_pcr=volume_pcr,
            call_volume_oi=_ratio(call_volume, call_oi),
            put_volume_oi=_ratio(put_volume, put_oi),
            oi_pcr_status=_ratio_status(
                oi_pcr,
                bearish_threshold=self._oi_pcr_bearish_threshold,
                bullish_threshold=self._oi_pcr_bullish_threshold,
            ),
            volume_pcr_status=_ratio_status(
                volume_pcr,
                bearish_threshold=self._volume_pcr_bearish_threshold,
                bullish_threshold=self._volume_pcr_bullish_threshold,
            ),
            put_volume_oi_status=_dominant_build_up(
                (
                    row.put_build_up
                    for row in rows
                    if row.strike <= snapshot.atm_strike
                ),
                labels={
                    "SHORT": "WRITING",
                    "LONG UNWIND": "LONG UNWIND",
                    "LONG": "LONG BUILD",
                    "SHORT COVER": "SHORT COVER",
                },
            ),
            call_volume_oi_status=_dominant_build_up(
                (
                    row.call_build_up
                    for row in rows
                    if row.strike >= snapshot.atm_strike
                ),
                labels={
                    "LONG": "LONG BUILD",
                    "SHORT COVER": "SHORT COVER",
                    "SHORT": "SHORT BUILD",
                    "LONG UNWIND": "LONG UNWIND",
                },
            ),
        )

    def _build_up(self, token: str, quote: object) -> str:
        price = getattr(quote, "ltp", None)
        oi = getattr(quote, "open_interest", None)
        previous = self._previous.get(token)
        self._previous[token] = (price, oi)
        if (
            previous is None
            or price is None
            or oi is None
            or previous[0] is None
            or previous[1] is None
        ):
            return "--"
        price_change = price - previous[0]
        oi_change = oi - previous[1]
        if price_change > 0 and oi_change > 0:
            return "LONG"
        if price_change < 0 and oi_change > 0:
            return "SHORT"
        if price_change > 0 and oi_change < 0:
            return "SHORT COVER"
        if price_change < 0 and oi_change < 0:
            return "LONG UNWIND"
        return "--"


def _ratio_status(
    value: Decimal | None,
    *,
    bearish_threshold: Decimal,
    bullish_threshold: Decimal,
) -> MetricStatus:
    if value is None:
        return MetricStatus.NEUTRAL
    if value >= bullish_threshold:
        return MetricStatus.BULLISH
    if value <= bearish_threshold:
        return MetricStatus.BEARISH
    return MetricStatus.NEUTRAL


def _dominant_build_up(
    build_ups: Iterable[str],
    *,
    labels: dict[str, str],
) -> str:
    counts: dict[str, int] = {}
    for build_up in build_ups:
        if build_up in labels:
            counts[build_up] = counts.get(build_up, 0) + 1
    if not counts:
        return MetricStatus.NEUTRAL.value
    highest = max(counts.values())
    leaders = tuple(build_up for build_up, count in counts.items() if count == highest)
    if len(leaders) != 1:
        return MetricStatus.NEUTRAL.value
    return labels[leaders[0]]


def _put_flow_status(build_up: str) -> MetricStatus:
    if build_up in {"SHORT", "LONG UNWIND"}:
        return MetricStatus.BULLISH
    if build_up in {"LONG", "SHORT COVER"}:
        return MetricStatus.BEARISH
    return MetricStatus.NEUTRAL


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _sum(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None:
        return None
    return left + right
