from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone

from app.domain.models import AnalyticsSnapshot, OptionChainSnapshot, OptionType
from app.signals.gate import SignalGateDecision


@dataclass(frozen=True)
class ActiveStrategyTarget:
    source: str
    side: str
    strike: object
    option_type: object
    ltp: object
    delta: object
    captured_at: object


def format_signal_line(
    *,
    snapshot: OptionChainSnapshot,
    analytics: AnalyticsSnapshot,
    recent_pcr: tuple[object, ...] = (),
    gate_decision: SignalGateDecision | None = None,
    active_gamma_target: ActiveStrategyTarget | None = None,
) -> str:
    history = " -> ".join(str(value) for value in recent_pcr if value is not None) or "n/a"
    support = str(int(analytics.support_levels[0].strike)) if analytics.support_levels else "n/a"
    resistance = str(int(analytics.resistance_levels[0].strike)) if analytics.resistance_levels else "n/a"
    support_distance = _distance(snapshot.spot_price, analytics.support_levels[0].strike) if analytics.support_levels else "n/a"
    resistance_distance = _distance(snapshot.spot_price, analytics.resistance_levels[0].strike) if analytics.resistance_levels else "n/a"
    raw_signal = gate_decision.raw_signal if gate_decision else analytics.signal or "NEUTRAL"
    published_signal = gate_decision.published_signal if gate_decision else analytics.signal or "NEUTRAL"
    qualified = str(gate_decision.qualified).upper() if gate_decision else "n/a"
    strong_signal = gate_decision.strong_signal if gate_decision else "NO_SIGNAL"
    setup_type = gate_decision.setup_type.value if gate_decision else "NONE"
    confidence = gate_decision.confidence_score if gate_decision else "n/a"
    confirmations = gate_decision.confirmation_count if gate_decision else 0
    micro_side = (
        gate_decision.microstructure_signal.side
        if gate_decision and gate_decision.microstructure_signal is not None
        else "n/a"
    )
    micro_age = (
        f"{(snapshot.captured_at - gate_decision.microstructure_signal.captured_at).total_seconds():.1f}s"
        if gate_decision and gate_decision.microstructure_signal is not None
        else "n/a"
    )
    target = _format_target(analytics)
    active_gamma = _format_active_target(active_gamma_target)
    return (
        f"{_format_time(snapshot)} [SIGNAL] {snapshot.underlying} ATM={snapshot.atm_strike} "
        f"SPOT={snapshot.spot_price} "
        f"CE_OI={_oi_total(snapshot, OptionType.CALL)} "
        f"PE_OI={_oi_total(snapshot, OptionType.PUT)} "
        f"PCR={analytics.put_call_ratio_oi} "
        f"RAW={raw_signal} "
        f"STRONG={strong_signal} "
        f"SETUP={setup_type} "
        f"SCORE={confidence} "
        f"CONFIRMATIONS={confirmations} "
        f"PUBLISHED={published_signal} "
        f"QUALIFIED={qualified} "
        f"MICRO={micro_side} "
        f"MICRO_AGE={micro_age} "
        f"HISTORY={history} "
        f"STRATEGY={analytics.strategy_source or 'n/a'} "
        f"{target} "
        f"{active_gamma} "
        f"REASON={analytics.signal_reason or 'No signal reason available.'} "
        f"SUPPORT={support} "
        f"DIST_SUPPORT={support_distance} "
        f"RESISTANCE={resistance} "
        f"DIST_RESISTANCE={resistance_distance} "
        f"STRADDLE={analytics.atm_straddle_price}"
    )


def _oi_total(snapshot: OptionChainSnapshot, option_type: OptionType) -> int:
    return sum(
        quote.oi or 0
        for quote in snapshot.quotes
        if quote.contract.option_type == option_type
    )


def _distance(spot, level) -> str:
    return str(spot - level)


def _format_time(snapshot: OptionChainSnapshot) -> str:
    captured_at = snapshot.captured_at
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(timezone.utc)
    return captured_at.isoformat()


def _format_target(analytics: AnalyticsSnapshot) -> str:
    if analytics.target_strike is None:
        return "TARGET_STRIKE=n/a TARGET_TYPE=n/a TARGET_LTP=n/a TARGET_DELTA=n/a"
    option_type = analytics.target_option_type.value if analytics.target_option_type else "n/a"
    return (
        f"TARGET_STRIKE={analytics.target_strike} "
        f"TARGET_TYPE={option_type} "
        f"TARGET_LTP={analytics.target_ltp if analytics.target_ltp is not None else 'n/a'} "
        f"TARGET_DELTA={analytics.target_delta if analytics.target_delta is not None else 'n/a'}"
    )


def _format_active_target(target: ActiveStrategyTarget | None) -> str:
    if target is None:
        return "GAMMA_ACTIVE_STRIKE=n/a GAMMA_ACTIVE_TYPE=n/a GAMMA_ACTIVE_LTP=n/a GAMMA_ACTIVE_SIDE=n/a"
    option_type = target.option_type.value if hasattr(target.option_type, "value") else target.option_type
    return (
        f"GAMMA_ACTIVE_STRIKE={target.strike} "
        f"GAMMA_ACTIVE_TYPE={option_type} "
        f"GAMMA_ACTIVE_LTP={target.ltp if target.ltp is not None else 'n/a'} "
        f"GAMMA_ACTIVE_SIDE={target.side}"
    )
