from __future__ import annotations

from decimal import Decimal
from datetime import time
from typing import Any

from app.domain.models import OptionType, SupportResistanceLevel

class IVAnalyticsEngine:
    """
    Quantitative module for Implied Volatility (Vega) skew analysis, Intraday IV Rank tracking,
    and Smart Money order flow validations.
    
    Designed to operate alongside the Volume/OI physics engine to act as a final conviction filter.
    """
    
    def __init__(
        self, 
        vega_trap_threshold: Decimal = Decimal("1.3"), # 30% spike over morning baseline
        skew_threshold: Decimal = Decimal("1.2"),       # 20% imbalance between Call/Put IV
        iv_rank_high_threshold: Decimal = Decimal("70"), # Top 70% of session volatility (Overpriced)
        iv_rank_low_threshold: Decimal = Decimal("30")   # Bottom 30% of session volatility (Cheap)
    ) -> None:
        self._vega_trap_threshold = vega_trap_threshold
        self._skew_threshold = skew_threshold
        self._iv_rank_high_threshold = iv_rank_high_threshold
        self._iv_rank_low_threshold = iv_rank_low_threshold
        
        # State memory for Vega Trap filter
        self._morning_atm_iv: Decimal | None = None
        self._iv_captured: bool = False
        
        # Dynamic Intraday Session Bounds
        self._session_high_iv: Decimal | None = None
        self._session_low_iv: Decimal | None = None

    def capture_morning_iv(self, current_time: time, atm_iv: Decimal) -> None:
        """
        Captures the baseline Implied Volatility after the initial 9:15 AM market open noise settles.
        """
        if not self._iv_captured and current_time >= time(9, 20):
            if atm_iv > 0:
                self._morning_atm_iv = atm_iv
                self._iv_captured = True

    def update_session_bounds(self, current_atm_iv: Decimal) -> None:
        """
        Dynamically adjusts the highest and lowest ATM Implied Volatility observed 
        during the current trading session.
        """
        if current_atm_iv <= 0:
            return

        if self._session_high_iv is None or current_atm_iv > self._session_high_iv:
            self._session_high_iv = current_atm_iv

        if self._session_low_iv is None or current_atm_iv < self._session_low_iv:
            self._session_low_iv = current_atm_iv

    def calculate_intraday_iv_rank(self, current_atm_iv: Decimal) -> Decimal:
        """
        Computes the relative position of the current ATM IV within the session's 
        established volatility bounds on a 0 to 100 scale.
        """
        self.update_session_bounds(current_atm_iv)

        if self._session_high_iv is None or self._session_low_iv is None:
            return Decimal("0")

        iv_range = self._session_high_iv - self._session_low_iv
        
        # Guard against division by zero on the first tick or completely flat session profiles
        if iv_range == 0:
            return Decimal("0")

        rank = ((current_atm_iv - self._session_low_iv) / iv_range) * Decimal("100")
        return rank.quantize(Decimal("0.01"))

    def evaluate_intraday_iv_rank(self, current_atm_iv: Decimal) -> tuple[bool, str]:
        """
        STRATEGY 4: INTRADAY IV RANK FILTER
        Acts as a gatekeeper for naked option buyers. Prevents premium execution
        when option values are temporarily inflated relative to the daily range.
        
        Returns True if the volatility is high (suppress buy signals), False if safe to trade.
        """
        rank = self.calculate_intraday_iv_rank(current_atm_iv)

        if rank > self._iv_rank_high_threshold:
            reason = (
                f"INTRADAY IV RANK VETO: Current Rank ({rank}%) is above the high threshold "
                f"({self._iv_rank_high_threshold}%). Option premiums are overstretched for today's session. "
                f"High risk of immediate volatility contraction. Signal suppressed to NEUTRAL."
            )
            return True, reason

        return False, f"INTRADAY IV RANK PASS: Rank is at {rank}%."

    def check_vega_trap(self, current_atm_iv: Decimal) -> tuple[bool, str]:
        """
        STRATEGY 2: VEGA TRAP FILTER
        Protects the bot from buying naked options during extreme fear/greed spikes.
        Returns True if a trap is detected, allowing the main engine to suppress the signal to NEUTRAL.
        """
        if not self._iv_captured or not self._morning_atm_iv:
            return False, ""
            
        if current_atm_iv > (self._morning_atm_iv * self._vega_trap_threshold):
            reason = (
                f"VEGA TRAP TRIGGERED: Current ATM IV ({current_atm_iv}) is >30% above "
                f"the morning baseline ({self._morning_atm_iv}). Premiums are artificially inflated. "
                f"IV Crush imminent. Signal suppressed to NEUTRAL."
            )
            return True, reason
            
        return False, ""

    def evaluate_iv_skew(
        self, 
        support: SupportResistanceLevel | None, 
        resistance: SupportResistanceLevel | None, 
        strike_oi_map: dict[Decimal, dict[OptionType, dict[str, Any]]]
    ) -> tuple[str | None, str]:
        """
        STRATEGY 1: IV SKEW IMBALANCE
        Detects institutional pre-positioning by analyzing the premium market makers are demanding
        at structural boundaries BEFORE the breakout actually occurs.
        """
        if not support or not resistance:
            return None, ""
            
        # O(1) dictionary lookups for instantaneous Greek extraction
        sup_put_iv = strike_oi_map.get(support.strike, {}).get(OptionType.PUT, {}).get("iv", Decimal("0"))
        res_call_iv = strike_oi_map.get(resistance.strike, {}).get(OptionType.CALL, {}).get("iv", Decimal("0"))
        
        if sup_put_iv == Decimal("0") or res_call_iv == Decimal("0"):
            return None, ""

        # Call Skew Spike (Pre-Breakout Tell)
        if res_call_iv > (sup_put_iv * self._skew_threshold):
            reason = (
                f"IV SKEW IMBALANCE: Resistance Call IV ({res_call_iv}) is bidding aggressively "
                f">20% over Support Put IV ({sup_put_iv}). Pre-breakout institutional Call buying detected."
            )
            return "BUY_CALL", reason
            
        # Put Skew Spike (Pre-Breakdown Tell)
        if sup_put_iv > (res_call_iv * self._skew_threshold):
            reason = (
                f"IV SKEW IMBALANCE: Support Put IV ({sup_put_iv}) is bidding aggressively "
                f">20% over Resistance Call IV ({res_call_iv}). Pre-breakdown institutional Put buying detected."
            )
            return "BUY_PUT", reason
            
        return None, ""

    def evaluate_smart_money_divergence(
        self,
        proposed_signal: str,
        atm_strike: Decimal,
        interval: Decimal | None,
        strike_oi_map: dict[Decimal, dict[OptionType, dict[str, Any]]]
    ) -> tuple[str, str]:
        """
        STRATEGY 3: SMART MONEY ITM DIVERGENCE
        Validates directional signals by checking if Deep In-The-Money (ITM) options 
        are confirming the move with high Vol/OI (Institutional Delta replication).
        """
        if proposed_signal not in ("BUY_CALL", "BUY_PUT") or not interval:
            return proposed_signal, ""
            
        # Target strikes 2 intervals deep ITM for institutional tracking
        deep_itm_call_strike = atm_strike - (interval * Decimal("2"))
        deep_itm_put_strike = atm_strike + (interval * Decimal("2"))
        
        if proposed_signal == "BUY_CALL":
            itm_data = strike_oi_map.get(deep_itm_call_strike, {}).get(OptionType.CALL, {})
            strike = deep_itm_call_strike
        else: # BUY_PUT
            itm_data = strike_oi_map.get(deep_itm_put_strike, {}).get(OptionType.PUT, {})
            strike = deep_itm_put_strike
            
        itm_oi = itm_data.get("oi", 0)
        itm_vol = itm_data.get("volume", 0)
        
        if itm_oi > 0:
            vol_oi = (Decimal(itm_vol) / Decimal(itm_oi)).quantize(Decimal("0.0001"))
            
            # If ITM Vol/OI > 2.0, institutions are aggressively building synthetic futures positions
            if vol_oi >= Decimal("2.0"):
                conviction_note = (
                    f" | SMART MONEY CONFIRMED: Deep ITM strike ({strike}) "
                    f"shows institutional Vol/OI accumulation of {vol_oi}."
                )
                return proposed_signal, conviction_note
                
            # If Volume is massive but OI is dropping (ITM Unwinding), it is a retail trap
            if vol_oi < Decimal("0.5"):
                warning_note = (
                    f" | WARNING: Poor ITM conviction. Deep ITM strike ({strike}) "
                    f"shows weak Vol/OI of {vol_oi}. Potential retail-driven fakeout."
                )
                return proposed_signal, warning_note

        return proposed_signal, ""