from __future__ import annotations

from decimal import Decimal
from app.domain.models import OptionChainSnapshot, OptionType

BUY_CALL = "BUY_CALL"
BUY_PUT = "BUY_PUT"
NEUTRAL = "NEUTRAL"


def pcr_signal(
    pcr: Decimal | None,
    *,
    spot_price: Decimal | None = None,
    support_level: Decimal | None = None,
    resistance_level: Decimal | None = None,
    bullish_threshold: Decimal,
    bearish_threshold: Decimal,
) -> tuple[str, str]:
    
    if pcr is None:
        return NEUTRAL, "PCR data is unavailable."

    # Backward-compatible pure-PCR mode is retained for the small unit callers.
    # Production callers always pass spot_price and therefore use the structural
    # checks below rather than treating PCR as a direct entry trigger.
    if spot_price is None:
        if pcr >= bullish_threshold:
            return BUY_CALL, f"PCR {pcr} is above bullish threshold {bullish_threshold}."
        if pcr <= bearish_threshold:
            return BUY_PUT, f"PCR {pcr} is below bearish threshold {bearish_threshold}."
        return NEUTRAL, f"PCR {pcr} is in the neutral zone."

    # Production mode treats PCR as context only. Structural strategies in the
    # analytics engine must provide the actual entry candidate.
    if resistance_level and spot_price > resistance_level and pcr <= bearish_threshold:
        return (
            NEUTRAL,
            f"PCR CONTEXT: Spot ({spot_price}) is above Resistance "
            f"({resistance_level}) despite low PCR; possible short-covering squeeze."
        )
        
    if support_level and spot_price < support_level and pcr >= bullish_threshold:
        return (
            NEUTRAL,
            f"PCR CONTEXT: Spot ({spot_price}) is below Support "
            f"({support_level}) despite high PCR; possible long liquidation."
        )

    # Static thresholds describe sentiment; they do not open a trade.
    if pcr <= Decimal("0.6"):
        context = "extreme low/contrarian bounce context"
        
    elif pcr <= bearish_threshold:
        context = "low PCR/call-OI resistance context"
        
    elif pcr < bullish_threshold:
        context = "neutral context"
    else:
        context = "high PCR/put-OI support context"
    return NEUTRAL, f"PCR {pcr} is {context}; confirmation only."
