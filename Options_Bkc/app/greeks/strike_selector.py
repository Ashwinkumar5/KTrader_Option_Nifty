from __future__ import annotations
from decimal import Decimal
from typing import Optional

from app.domain.models import OptionChainSnapshot, OptionQuote, OptionType
import logging

logger = logging.getLogger("StrikeSelector")

class OptimalStrikeSelector:
    """
    Quantitative targeting system for naked option buyers.
    Scans the option chain to find the strike with maximum Gamma acceleration 
    (closest to 50 Delta) and highest liquidity.
    """
    
    __slots__ = (
        "_target_delta",
        "_min_volume_threshold",
        "_min_oi_threshold",
        "_min_abs_delta",
        "_max_abs_delta",
        "_max_spread_points",
        "_max_spread_ratio",
        "_max_dte",
        "_max_iv",
    )

    def __init__(
        self, 
        target_delta: float = 0.50,         # 50 Delta has the highest Gamma 
        min_volume_threshold: int = 5000,   # Guard against illiquid strikes
        min_oi_threshold: int = 5000,
        min_abs_delta: float = 0.35,
        max_abs_delta: float = 0.65,
        max_spread_points: Decimal = Decimal("1.50"),
        max_spread_ratio: Decimal = Decimal("0.02"),
        max_dte: int = 14,
        max_iv: Decimal = Decimal("100"),
    ) -> None:
        self._target_delta = target_delta
        self._min_volume_threshold = min_volume_threshold
        self._min_oi_threshold = min_oi_threshold
        self._min_abs_delta = min_abs_delta
        self._max_abs_delta = max_abs_delta
        self._max_spread_points = max_spread_points
        self._max_spread_ratio = max_spread_ratio
        self._max_dte = max_dte
        self._max_iv = max_iv

    def select_optimal_strike(
        self, 
        snapshot: OptionChainSnapshot, 
        signal: str,
        *,
        expiry_day_fallback_enabled: bool = False,
    ) -> Optional[OptionQuote]:
        """
        Takes the directional signal and finds the optimal quote based on Delta and Liquidity.
        Returns the exact OptionQuote to execute, or None if no safe strike exists.
        """
        if signal not in ("BUY_CALL", "BUY_PUT"):
            return None
            
        target_type = OptionType.CALL if signal == "BUY_CALL" else OptionType.PUT
        
        # 1. Keep only contracts that are executable independently of delta.
        executable_quotes = [
            quote
            for quote in snapshot.quotes
            if quote.contract.option_type == target_type
            and 0
            <= (quote.contract.expiry - snapshot.captured_at.date()).days
            <= self._max_dte
            and quote.volume is not None
            and quote.volume >= self._min_volume_threshold
            and quote.oi is not None
            and quote.oi >= self._min_oi_threshold
            and _has_executable_spread(
                quote,
                max_points=self._max_spread_points,
                max_ratio=self._max_spread_ratio,
            )
            and quote.greeks is not None
            and quote.greeks.delta is not None
            and quote.greeks.implied_volatility is not None
            and Decimal("0") < quote.greeks.implied_volatility <= self._max_iv
        ]

        # Broker deltas can collapse toward zero or one close to expiry. Keep
        # the normal delta band whenever it supplies an executable contract.
        valid_quotes = [
            quote
            for quote in executable_quotes
            if self._min_abs_delta
            <= abs(float(quote.greeks.delta))
            <= self._max_abs_delta
        ]

        if not valid_quotes and expiry_day_fallback_enabled:
            expiry_quotes = [
                quote
                for quote in executable_quotes
                if (
                    quote.contract.expiry - snapshot.captured_at.date()
                ).days
                == 0
            ]
            if expiry_quotes:
                fallback = min(
                    expiry_quotes,
                    key=lambda quote: (
                        abs(quote.contract.strike - snapshot.atm_strike),
                        quote.ask - quote.bid,
                        -(quote.oi or 0),
                        -(quote.volume or 0),
                    ),
                )
                logger.info(
                    "STRIKE EXPIRY FALLBACK: Selected ATM-nearest %s %s | "
                    "Delta: %.2f | Volume: %s | OI: %s",
                    fallback.contract.strike,
                    target_type.name,
                    float(fallback.greeks.delta),
                    fallback.volume,
                    fallback.oi,
                )
                return fallback

        if not valid_quotes:
            logger.warning(
                "STRIKE ABORT: No executable %s strike passed DTE, volume, OI, "
                "spread, IV and delta filters.",
                target_type.name,
            )
            return None

        # 2. Find the strike closest to the target Delta (Gamma Sweet Spot)
        # Note: Call deltas are positive (0 to 1), Put deltas are negative (0 to -1).
        # We use absolute value to normalize the math for both.
        
        try:
            optimal_quote = min(
                valid_quotes,
                key=lambda quote: (
                    abs(abs(float(quote.greeks.delta)) - self._target_delta),
                    quote.ask - quote.bid,
                    -(quote.oi or 0),
                    -(quote.volume or 0),
                ),
            )
            
            delta_val = float(optimal_quote.greeks.delta)
            logger.info(
                "STRIKE ACQUIRED: Selected %s %s | Delta: %.2f | Volume: %s",
                optimal_quote.contract.strike,
                target_type.name,
                delta_val,
                optimal_quote.volume,
            )
            return optimal_quote
            
        except Exception as exc:
            logger.error("STRIKE ERROR: Failed to calculate optimal contract: %s", exc)
            return None


def _has_executable_spread(
    quote: OptionQuote,
    *,
    max_points: Decimal,
    max_ratio: Decimal,
) -> bool:
    if (
        quote.bid is None
        or quote.ask is None
        or quote.bid <= 0
        or quote.ask <= 0
        or quote.ask < quote.bid
    ):
        return False
    spread = quote.ask - quote.bid
    midpoint = (quote.ask + quote.bid) / Decimal("2")
    return spread <= max_points and spread / midpoint <= max_ratio
