from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.models import (
    CandlePatternContext,
    EvidenceFamily,
    FuturesFlowContext,
    OpeningContext,
    OptionType,
    PremiumResponse,
    StrategyCandidate,
    StrategyEvidence,
    StrategyFamily,
)


@dataclass(frozen=True)
class OptionChainLeg:
    token: str
    option_type: OptionType
    relative_strike: int
    mid: Decimal
    volume: int
    oi: int
    spread_ratio: Decimal | None


@dataclass(frozen=True)
class StrategyEvaluationContext:
    captured_at: datetime
    spot: Decimal
    pcr_oi: Decimal | None
    expected_upper: Decimal | None
    expected_lower: Decimal | None
    support: Decimal | None
    resistance: Decimal | None
    local_support: Decimal | None
    local_resistance: Decimal | None
    level_tolerance: Decimal
    breakout_threshold: Decimal
    exhaustion_threshold: Decimal
    atm_call_volume: int
    atm_call_oi: int
    atm_put_volume: int
    atm_put_oi: int
    spot_delta: Decimal
    near_support: bool
    near_resistance: bool
    support_volume: int
    support_oi: int
    support_oi_change: int
    resistance_volume: int
    resistance_oi: int
    resistance_oi_change: int
    rotation_signal: str | None
    rotation_reason: str
    gamma_signal: str | None
    gamma_reason: str
    opening_context: OpeningContext | None
    candle_pattern: CandlePatternContext | None
    futures_flow: FuturesFlowContext | None
    future_price: Decimal | None = None
    future_open: Decimal | None = None
    future_previous_close: Decimal | None = None
    underlying: str = "NIFTY"
    active_pcr: Decimal | None = None
    call_oi_change: int = 0
    put_oi_change: int = 0
    call_volume_oi: Decimal = Decimal("0")
    put_volume_oi: Decimal = Decimal("0")
    call_volume: int = 0
    put_volume: int = 0
    call_oi: int = 0
    put_oi: int = 0
    atm_straddle_price: Decimal | None = None
    atm_call_mid: Decimal | None = None
    atm_put_mid: Decimal | None = None
    atm_call_iv: Decimal | None = None
    atm_put_iv: Decimal | None = None
    intraday_iv_rank: Decimal = Decimal("0")
    previous_20d_atr: Decimal | None = None
    india_vix: Decimal | None = None
    is_expiry_day: bool = False
    option_chain_legs: tuple[OptionChainLeg, ...] = ()
    premium_responses: tuple[PremiumResponse, ...] = ()


class StrategyEvaluator(Protocol):
    family: StrategyFamily

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]: ...


def evidence(
    code: str,
    family: EvidenceFamily,
    side: str,
    strength: Decimal = Decimal("0.70"),
) -> StrategyEvidence:
    return StrategyEvidence(code, family, side, strength)


def ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (
        Decimal(numerator) / Decimal(denominator)
    ).quantize(Decimal("0.0001"))


def ratio_confidence(
    observed: Decimal,
    threshold: Decimal,
) -> Decimal:
    if threshold <= 0:
        return Decimal("0.65")
    excess = max(Decimal("0"), observed / threshold - Decimal("1"))
    return min(
        Decimal("0.95"),
        Decimal("0.65") + excess * Decimal("0.10"),
    ).quantize(Decimal("0.0001"))
