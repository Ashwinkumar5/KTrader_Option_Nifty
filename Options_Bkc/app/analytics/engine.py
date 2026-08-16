from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import NamedTuple, Any
from datetime import datetime, time
from zoneinfo import ZoneInfo

from .iv_strategy import IVAnalyticsEngine
from .range_rotation import RangeRotationSettings, RangeRotationTracker
from .regime import MarketRegimeClassifier, RegimeSettings
from .strategy_resolver import (
    StrategyCandidateResolver,
    StrategyFamilySettings,
    StrategyResolution,
    StrategyResolverSettings,
)
from .structural_levels import StructuralLevelSettings, StructuralLevelTracker
from .session_features import (
    FeatureModuleSettings,
    SessionFeaturePipeline,
    SessionFeaturePipelineSettings,
)
from .opening_context import OpeningContextSettings
from .expected_move import ExpectedMoveSettings
from .momentum_exhaustion import MomentumExhaustionSettings
from .candle_patterns import CandlePatternSettings
from .futures_flow import FuturesFlowSettings
from .strategies import (
    BreakoutMomentumStrategy,
    DerivativesQuantStrategy,
    GammaExpansionStrategy,
    LevelReversalStrategy,
    OptionChainImpulseStrategy,
    SMCStrategy,
    OptionChainLeg,
    StrategyEvaluationContext,
    StrategyRegistry,
)
from app.core.strategy_config import (
    DerivativesQuantSettings,
    OptionChainImpulseSettings,
    SMCSettings,
    StrategyProfile,
)
from app.optionchain.memory_state import CoiledSpringDetector, TickSnapshot
from app.greeks.strike_selector import OptimalStrikeSelector

from app.domain.models import (
    AnalyticsSnapshot,
    EvidenceFamily,
    CandlePatternContext,
    MarketRegime,
    OpeningState,
    OptionChainSnapshot,
    OptionQuote,
    OptionType,
    SignalSetup,
    StrategyCandidate,
    StrategyCheck,
    StrategyDiagnostic,
    StrategyEvidence,
    StrategyFamily,
    StrategyResolverPolicy,
    SupportResistanceLevel,
)
from app.signals.noise_filter import (
    DirectionalSignalDebouncer,
    SignalDebounceSettings,
)

#-----------------------------------------------------------------------------------------------------------------------------------------------------------

class _ChainMetrics(NamedTuple):
    put_oi: int
    call_oi: int
    put_oi_change: int
    call_oi_change: int
    put_volume: int           
    call_volume: int          
    straddle_price: Decimal | None
    strike_level_ratios: dict[Decimal, dict[str, Decimal]]
    support_level: tuple[SupportResistanceLevel, ...]
    resistance_level: tuple[SupportResistanceLevel, ...]
    atm_call_vol: int
    atm_call_oi: int
    atm_put_vol: int          
    atm_put_oi: int           
    sup_vol: int
    sup_oi: int
    sup_oi_chg: int
    res_vol: int              
    res_oi: int               
    res_oi_chg: int           
    atm_iv: Decimal
    atm_call_iv: Decimal
    atm_put_iv: Decimal
    atm_call_mid: Decimal | None
    atm_put_mid: Decimal | None
    strike_oi_map: dict[Decimal, dict[OptionType, dict[str, Any]]]

class AnalyticsEngine:
    
    def __init__(
        self,
        *,
        pcr_bullish_threshold: Decimal = Decimal("1.4"),
        pcr_bearish_threshold: Decimal = Decimal("0.7"),
        market_timezone: str = "Asia/Kolkata",
        signal_debounce_frame_seconds: int = 15,
        signal_debounce_window_frames: int = 3,
        signal_debounce_min_confirmed_frames: int = 2,
        range_soft_breach_frames: int = 2,
        range_hard_invalidation_points: Decimal = Decimal("5"),
        range_recovery_buffer_points: Decimal = Decimal("2"),
        structural_level_frame_seconds: int = 240,
        strategy_resolver_policy: StrategyResolverPolicy | str = (
            StrategyResolverPolicy.REGIME_EXCLUSIVE
        ),
        strategy_level_reversal_enabled: bool = True,
        strategy_breakout_momentum_enabled: bool = True,
        strategy_gamma_expansion_enabled: bool = True,
        strategy_level_reversal_priority: int = 10,
        strategy_breakout_momentum_priority: int = 20,
        strategy_gamma_expansion_priority: int = 30,
        strategy_derivatives_quant_enabled: bool = False,
        strategy_derivatives_quant_priority: int = 40,
        strategy_option_chain_impulse_enabled: bool = False,
        strategy_option_chain_impulse_priority: int = 50,
        strategy_smc_enabled: bool = False,
        strategy_smc_priority: int = 60,
        strategy_profile: StrategyProfile | None = None,
        feature_opening_context_enabled: bool = True,
        feature_opening_context_sequence: int = 10,
        feature_expected_move_enabled: bool = True,
        feature_expected_move_sequence: int = 20,
        feature_premium_response_enabled: bool = True,
        feature_premium_response_sequence: int = 30,
        feature_futures_flow_enabled: bool = True,
        feature_futures_flow_sequence: int = 35,
        feature_candle_patterns_enabled: bool = True,
        feature_candle_patterns_sequence: int = 37,
        feature_momentum_exhaustion_enabled: bool = True,
        feature_momentum_exhaustion_sequence: int = 40,
        opening_observation_minutes: int = 15,
        expected_move_capture_time: time | str = time(9, 45),
        expected_move_first_band_ratio: Decimal = Decimal("0.50"),
        expected_move_extended_band_ratio: Decimal = Decimal("0.80"),
        expected_move_exhaustion_band_ratio: Decimal = Decimal("1.00"),
        exhaustion_earliest_time: time | str = time(13, 15),
        exhaustion_minimum_premium_return_percent: Decimal = Decimal("75"),
        exhaustion_minimum_move_utilization: Decimal = Decimal("0.80"),
        gamma_window_seconds: int = 300,
        regime_window_seconds: int = 300,
        futures_flow_window_seconds: int = 60,
        reversal_candle_confirmation_required: bool = False,
    ) -> None:
        self._strategy_profile = (
            strategy_profile.name
            if strategy_profile is not None
            else "legacy_inline"
        )
        self._quant_settings = (
            strategy_profile.quant
            if strategy_profile is not None
            else DerivativesQuantSettings()
        )
        self._impulse_settings = (
            strategy_profile.impulse
            if strategy_profile is not None
            else OptionChainImpulseSettings()
        )
        self._smc_settings = (
            strategy_profile.smc
            if strategy_profile is not None
            else SMCSettings()
        )
        self._enabled_features = (
            frozenset(
                name
                for name, enabled in strategy_profile.features.items()
                if enabled
            )
            if strategy_profile is not None
            else None
        )
        if strategy_profile is not None:
            strategy_level_reversal_enabled = (
                strategy_profile.strategy_enabled("LEVEL_REVERSAL")
            )
            strategy_breakout_momentum_enabled = (
                strategy_profile.strategy_enabled("BREAKOUT_MOMENTUM")
            )
            strategy_gamma_expansion_enabled = (
                strategy_profile.strategy_enabled("GAMMA_EXPANSION")
            )
            strategy_derivatives_quant_enabled = (
                strategy_profile.strategy_enabled("DERIVATIVES_QUANT")
            )
            strategy_option_chain_impulse_enabled = (
                strategy_profile.strategy_enabled("OPTION_CHAIN_IMPULSE")
            )
            strategy_smc_enabled = strategy_profile.strategy_enabled("SMC")
            strategy_level_reversal_priority = (
                strategy_profile.strategy_priority("LEVEL_REVERSAL")
            )
            strategy_breakout_momentum_priority = (
                strategy_profile.strategy_priority("BREAKOUT_MOMENTUM")
            )
            strategy_gamma_expansion_priority = (
                strategy_profile.strategy_priority("GAMMA_EXPANSION")
            )
            strategy_derivatives_quant_priority = (
                strategy_profile.strategy_priority("DERIVATIVES_QUANT")
            )
            strategy_option_chain_impulse_priority = (
                strategy_profile.strategy_priority("OPTION_CHAIN_IMPULSE")
            )
            strategy_smc_priority = strategy_profile.strategy_priority("SMC")
            feature_opening_context_enabled = (
                strategy_profile.feature_enabled("opening_context")
            )
            feature_expected_move_enabled = (
                strategy_profile.feature_enabled("expected_move")
            )
            feature_premium_response_enabled = (
                strategy_profile.feature_enabled("premium_response")
            )
            feature_futures_flow_enabled = (
                strategy_profile.feature_enabled("futures_flow")
                or strategy_profile.feature_enabled("futures_basis")
            )
            feature_candle_patterns_enabled = (
                strategy_profile.feature_enabled("candle_patterns")
            )
            feature_momentum_exhaustion_enabled = (
                strategy_profile.feature_enabled("momentum_exhaustion")
            )
        self._pcr_bullish_threshold = pcr_bullish_threshold
        self._pcr_bearish_threshold = pcr_bearish_threshold
        self._market_timezone = ZoneInfo(market_timezone)

        # System State Tracking
        self._morning_straddle_price: Decimal | None = None
        self._morning_spot_price: Decimal | None = None
        self._straddle_upper_bound: Decimal | None = None
        self._straddle_lower_bound: Decimal | None = None
        self._straddle_captured: bool = False
        self._last_spot_price: Decimal | None = None
        self._last_valid_straddle_price: Decimal | None = None
        self._reversal_candle_confirmation_required = (
            reversal_candle_confirmation_required
        )
        self._latest_target_strike: Decimal | None = None
        self._latest_target_option_type: OptionType | None = None
        self._latest_target_ltp: Decimal | None = None
        self._latest_target_delta: Decimal | None = None
        
        # Initialize Targeting Architecture
        self._strike_selector = OptimalStrikeSelector(target_delta=0.50, min_volume_threshold=5000)
        # Initialize the 5-minute rolling window for Gamma Blasts
        self._gamma_window_seconds = gamma_window_seconds
        self._regime_window_seconds = regime_window_seconds
        self._gamma_spring_detector = CoiledSpringDetector(
            window_seconds=gamma_window_seconds
        )
        # Instantiate Volatility Architecture
        self._iv_engine = IVAnalyticsEngine()
        # Preserve defended-boundary context while spot travels through a range.
        self._range_rotation = RangeRotationTracker(
            RangeRotationSettings(
                decision_frame_seconds=signal_debounce_frame_seconds,
                soft_breach_window_frames=signal_debounce_window_frames,
                soft_breach_frames=range_soft_breach_frames,
                hard_invalidation_points=range_hard_invalidation_points,
                recovery_buffer_points=range_recovery_buffer_points,
            )
        )
        self._regime_classifier = MarketRegimeClassifier(
            RegimeSettings(window_seconds=regime_window_seconds)
        )
        self._structural_levels = StructuralLevelTracker(
            StructuralLevelSettings(
                frame_seconds=structural_level_frame_seconds,
            )
        )
        self._signal_debouncer = DirectionalSignalDebouncer(
            SignalDebounceSettings(
                frame_seconds=signal_debounce_frame_seconds,
                window_frames=signal_debounce_window_frames,
                min_confirmed_frames=signal_debounce_min_confirmed_frames,
            )
        )
        self._strategy_resolver = StrategyCandidateResolver(
            StrategyResolverSettings(
                policy=StrategyResolverPolicy(
                    str(strategy_resolver_policy)
                ),
                families=(
                    StrategyFamilySettings(
                        StrategyFamily.LEVEL_REVERSAL,
                        strategy_level_reversal_enabled,
                        strategy_level_reversal_priority,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.BREAKOUT_MOMENTUM,
                        strategy_breakout_momentum_enabled,
                        strategy_breakout_momentum_priority,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.GAMMA_EXPANSION,
                        strategy_gamma_expansion_enabled,
                        strategy_gamma_expansion_priority,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.DERIVATIVES_QUANT,
                        strategy_derivatives_quant_enabled,
                        strategy_derivatives_quant_priority,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.OPTION_CHAIN_IMPULSE,
                        strategy_option_chain_impulse_enabled,
                        strategy_option_chain_impulse_priority,
                    ),
                    StrategyFamilySettings(
                        StrategyFamily.SMC,
                        strategy_smc_enabled,
                        strategy_smc_priority,
                    ),
                ),
            )
        )
        enabled_strategies = frozenset(
            family
            for family, enabled in (
                (
                    StrategyFamily.LEVEL_REVERSAL,
                    strategy_level_reversal_enabled,
                ),
                (
                    StrategyFamily.BREAKOUT_MOMENTUM,
                    strategy_breakout_momentum_enabled,
                ),
                (
                    StrategyFamily.GAMMA_EXPANSION,
                    strategy_gamma_expansion_enabled,
                ),
                (
                    StrategyFamily.DERIVATIVES_QUANT,
                    strategy_derivatives_quant_enabled,
                ),
                (
                    StrategyFamily.OPTION_CHAIN_IMPULSE,
                    strategy_option_chain_impulse_enabled,
                ),
                (StrategyFamily.SMC, strategy_smc_enabled),
            )
            if enabled
        )
        self._strategy_registry = StrategyRegistry(
            evaluators=(
                LevelReversalStrategy(),
                BreakoutMomentumStrategy(),
                GammaExpansionStrategy(self._enabled_features),
                DerivativesQuantStrategy(
                    self._quant_settings,
                    self._enabled_features,
                ),
                OptionChainImpulseStrategy(
                    self._impulse_settings,
                    enabled=strategy_option_chain_impulse_enabled,
                ),
                SMCStrategy(
                    self._smc_settings,
                    self._impulse_settings,
                    enabled=strategy_smc_enabled,
                ),
            ),
            enabled=enabled_strategies,
            priorities={
                StrategyFamily.LEVEL_REVERSAL: (
                    strategy_level_reversal_priority
                ),
                StrategyFamily.BREAKOUT_MOMENTUM: (
                    strategy_breakout_momentum_priority
                ),
                StrategyFamily.GAMMA_EXPANSION: (
                    strategy_gamma_expansion_priority
                ),
                StrategyFamily.DERIVATIVES_QUANT: (
                    strategy_derivatives_quant_priority
                ),
                StrategyFamily.OPTION_CHAIN_IMPULSE: (
                    strategy_option_chain_impulse_priority
                ),
                StrategyFamily.SMC: strategy_smc_priority,
            },
        )
        self._session_features = SessionFeaturePipeline(
            SessionFeaturePipelineSettings(
                opening=FeatureModuleSettings(
                    feature_opening_context_enabled,
                    feature_opening_context_sequence,
                ),
                expected_move=FeatureModuleSettings(
                    feature_expected_move_enabled,
                    feature_expected_move_sequence,
                ),
                premium_response=FeatureModuleSettings(
                    feature_premium_response_enabled,
                    feature_premium_response_sequence,
                ),
                futures_flow=FeatureModuleSettings(
                    feature_futures_flow_enabled,
                    feature_futures_flow_sequence,
                ),
                candle_patterns=FeatureModuleSettings(
                    feature_candle_patterns_enabled,
                    feature_candle_patterns_sequence,
                ),
                momentum_exhaustion=FeatureModuleSettings(
                    feature_momentum_exhaustion_enabled,
                    feature_momentum_exhaustion_sequence,
                ),
            ),
            opening_settings=OpeningContextSettings(
                observation_minutes=opening_observation_minutes,
                market_timezone=market_timezone,
            ),
            expected_move_settings=ExpectedMoveSettings(
                capture_time=_coerce_time(expected_move_capture_time),
                first_band_ratio=expected_move_first_band_ratio,
                extended_band_ratio=expected_move_extended_band_ratio,
                exhaustion_band_ratio=expected_move_exhaustion_band_ratio,
                market_timezone=market_timezone,
            ),
            futures_flow_settings=FuturesFlowSettings(
                window_seconds=futures_flow_window_seconds,
            ),
            candle_pattern_settings=CandlePatternSettings(
                frame_seconds=structural_level_frame_seconds,
                market_timezone=market_timezone,
            ),
            exhaustion_settings=MomentumExhaustionSettings(
                earliest_time=_coerce_time(exhaustion_earliest_time),
                minimum_premium_return_percent=(
                    exhaustion_minimum_premium_return_percent
                ),
                minimum_move_utilization=(
                    exhaustion_minimum_move_utilization
                ),
                market_timezone=market_timezone,
            ),
        )
        self._session_date = None

    def from_chain(self, snapshot: OptionChainSnapshot) -> AnalyticsSnapshot:
        market_datetime = snapshot.captured_at.astimezone(self._market_timezone)
        if self._session_date != market_datetime.date():
            self._reset_intraday_state()
            self._session_date = market_datetime.date()

        session_features = self._session_features.update(snapshot)
        self._latest_target_strike = None
        self._latest_target_option_type = None
        self._latest_target_ltp = None
        self._latest_target_delta = None

        interval = _window_interval(snapshot)        
        metrics = self._aggregate_chain_metrics(snapshot, interval)
        pcr_oi = _ratio(metrics.put_oi, metrics.call_oi)
        current_spot = snapshot.spot_price
        
        raw_support = (
            metrics.support_level[0].strike if metrics.support_level else None
        )
        raw_resistance = (
            metrics.resistance_level[0].strike
            if metrics.resistance_level
            else None
        )
        structural_levels = self._structural_levels.update(
            underlying=snapshot.underlying,
            captured_at=snapshot.captured_at,
            support=raw_support,
            resistance=raw_resistance,
        )
        support_strike = structural_levels.support
        resistance_strike = structural_levels.resistance
        local_support = _select_local_level(
            metrics.support_level,
            spot=current_spot,
            interval=interval,
        )
        local_resistance = _select_local_level(
            metrics.resistance_level,
            spot=current_spot,
            interval=interval,
        )
        # Exhaustion is meaningful only at the level being defended or rejected.
        # A half-strike tolerance prevents ordinary mid-range ticks from being
        # labelled as a support/rejection event.
        level_tolerance = interval / Decimal("2") if interval else Decimal("0")
        near_support = support_strike is not None and abs(current_spot - support_strike) <= level_tolerance
        near_resistance = resistance_strike is not None and abs(current_spot - resistance_strike) <= level_tolerance
        rotation_decision = self._range_rotation.update(
            underlying=snapshot.underlying,
            captured_at=snapshot.captured_at,
            spot=current_spot,
            support=support_strike,
            resistance=resistance_strike,
            level_zone=level_tolerance,
        )
        
        breakout_thresh, exhaustion_thresh = _get_dynamic_thresholds(
            snapshot.underlying,
            market_datetime,
        )
        
        # =====================================================================
        # PHASE 0: BASELINE AND MEMORY CAPTURES
        # =====================================================================
        self._iv_engine.capture_morning_iv(market_datetime.time(), metrics.atm_iv)
        
        spot_delta = current_spot - (self._last_spot_price or current_spot)
        
        expected_move = session_features.expected_move
        if (
            expected_move is not None
            and expected_move.available
            and expected_move.straddle_mid is not None
            and expected_move.anchor_spot is not None
            and expected_move.first_band is not None
        ):
            self._morning_straddle_price = expected_move.straddle_mid
            self._morning_spot_price = expected_move.anchor_spot
            self._straddle_upper_bound = (
                expected_move.anchor_spot + expected_move.first_band
            )
            self._straddle_lower_bound = (
                expected_move.anchor_spot - expected_move.first_band
            )
            self._straddle_captured = True

        # =====================================================================
        # PHASE 1: VOLATILITY-COST STATE (SETUP-AWARE, NOT A GLOBAL VETO)
        # =====================================================================
        is_trap, trap_reason = self._iv_engine.check_vega_trap(metrics.atm_iv)
        is_rank_inflated, rank_reason = self._iv_engine.evaluate_intraday_iv_rank(
            metrics.atm_iv
        )

        # =====================================================================
        # PHASE 1.5 - CAPTURE AND EVALUATE ROLLING STATE (GAMMA SPRING)
        # =====================================================================
        
        # Calculate current Intraday IV Rank
        current_iv_rank = self._iv_engine.calculate_intraday_iv_rank(metrics.atm_iv)
        
        # Extract OTM Greeks (e.g., 50 points OTM)
        otm_call_strike = snapshot.atm_strike + (interval if interval else Decimal("50"))
        otm_put_strike = snapshot.atm_strike - (interval if interval else Decimal("50"))
        otm_call_quote = _quote_at_strike(
            snapshot,
            strike=otm_call_strike,
            option_type=OptionType.CALL,
        )
        otm_put_quote = _quote_at_strike(
            snapshot,
            strike=otm_put_strike,
            option_type=OptionType.PUT,
        )
        
        otm_call_iv = metrics.strike_oi_map.get(otm_call_strike, {}).get(OptionType.CALL, {}).get("iv", Decimal("0"))
        otm_put_iv = metrics.strike_oi_map.get(otm_put_strike, {}).get(OptionType.PUT, {}).get("iv", Decimal("0"))
        
        atm_call_delta = metrics.strike_oi_map.get(snapshot.atm_strike, {}).get(OptionType.CALL, {}).get("delta", Decimal("0.5"))

        # Build the High-Speed Snapshot
        tick = TickSnapshot(
            captured_at=snapshot.captured_at,
            spot_price=float(current_spot),
            atm_iv=float(metrics.atm_iv),
            iv_rank=float(current_iv_rank),
            otm_call_iv=float(otm_call_iv),
            otm_put_iv=float(otm_put_iv),
            atm_call_delta=float(atm_call_delta),
            otm_call_token=(
                otm_call_quote.contract.token.token
                if otm_call_quote is not None
                else None
            ),
            otm_put_token=(
                otm_put_quote.contract.token.token
                if otm_put_quote is not None
                else None
            ),
            otm_call_mid=_gamma_quote_mid(otm_call_quote),
            otm_put_mid=_gamma_quote_mid(otm_put_quote),
            otm_call_spread_ratio=_quote_spread_ratio(otm_call_quote),
            otm_put_spread_ratio=_quote_spread_ratio(otm_put_quote),
        )
        
        # Update the memory buffer
        self._gamma_spring_detector.update(tick)
        
        # Check if the Gamma Spring is Coiled (Bidirectional)
        gamma_signal, gamma_reason = self._gamma_spring_detector.evaluate_gamma_blast()
        
        volatility_cost_enabled = self._feature_enabled("iv_surface")
        gamma_feature_enabled = self._feature_enabled(
            "gamma_concentration"
        )
        market_regime = self._regime_classifier.classify(
            underlying=snapshot.underlying,
            spot=current_spot,
            support=support_strike,
            resistance=resistance_strike,
            iv_rank=(
                current_iv_rank
                if volatility_cost_enabled
                else Decimal("0")
            ),
            unstable_high_vol=(
                volatility_cost_enabled
                and is_trap
                and is_rank_inflated
            ),
            gamma_coiled=(
                gamma_feature_enabled
                and gamma_signal in ("BUY_CALL", "BUY_PUT")
            ),
            captured_at=snapshot.captured_at,
        )
        market_regime = _opening_regime_override(
            market_regime,
            session_features.opening.state
            if session_features.opening is not None
            else OpeningState.UNAVAILABLE,
        )

        # Build independent strategy candidates. No strategy is allowed to
        # overwrite another strategy's result; the configured resolver owns
        # selection and conflict handling.
        strategy_candidates: list[StrategyCandidate] = []
        directional_confirmations: list[str] = []
        directional_conflicts: list[str] = []
        directional_evidence: list[StrategyEvidence] = []
        total_call_velocity = Decimal("0")
        total_put_velocity = Decimal("0")
        active_pcr = Decimal("0")
        local_divergence_side: str | None = None

        active_zone_put_oi = 0
        active_zone_call_oi = 0
        for strike, ratios in metrics.strike_level_ratios.items():
            total_call_velocity += ratios.get(
                "call_vol_oi",
                Decimal("0"),
            )
            total_put_velocity += ratios.get(
                "put_vol_oi",
                Decimal("0"),
            )
            if interval and abs(strike - current_spot) <= interval:
                active_zone_put_oi += (
                    metrics.strike_oi_map.get(strike, {})
                    .get(OptionType.PUT, {})
                    .get("oi", 0)
                )
                active_zone_call_oi += (
                    metrics.strike_oi_map.get(strike, {})
                    .get(OptionType.CALL, {})
                    .get("oi", 0)
                )

        active_pcr = (
            _ratio(active_zone_put_oi, active_zone_call_oi)
            or Decimal("0")
        )
        if pcr_oi is not None:
            if active_pcr < Decimal("0.7") and pcr_oi > Decimal("1.4"):
                local_divergence_side = "BUY_PUT"
            elif active_pcr > Decimal("1.3") and pcr_oi < Decimal("0.8"):
                local_divergence_side = "BUY_CALL"

        strategy_context = StrategyEvaluationContext(
            underlying=snapshot.underlying,
            captured_at=snapshot.captured_at,
            spot=current_spot,
            pcr_oi=(
                pcr_oi
                if self._feature_enabled("consolidated_pcr")
                else None
            ),
            expected_upper=self._straddle_upper_bound,
            expected_lower=self._straddle_lower_bound,
            support=support_strike,
            resistance=resistance_strike,
            local_support=local_support,
            local_resistance=local_resistance,
            level_tolerance=level_tolerance,
            breakout_threshold=breakout_thresh,
            exhaustion_threshold=exhaustion_thresh,
            atm_call_volume=metrics.atm_call_vol,
            atm_call_oi=metrics.atm_call_oi,
            atm_put_volume=metrics.atm_put_vol,
            atm_put_oi=metrics.atm_put_oi,
            spot_delta=spot_delta,
            near_support=near_support,
            near_resistance=near_resistance,
            support_volume=metrics.sup_vol,
            support_oi=metrics.sup_oi,
            support_oi_change=metrics.sup_oi_chg,
            resistance_volume=metrics.res_vol,
            resistance_oi=metrics.res_oi,
            resistance_oi_change=metrics.res_oi_chg,
            rotation_signal=rotation_decision.signal,
            rotation_reason=rotation_decision.reason,
            gamma_signal=(
                gamma_signal if gamma_feature_enabled else None
            ),
            gamma_reason=gamma_reason,
            opening_context=session_features.opening,
            candle_pattern=session_features.candle_pattern,
            futures_flow=session_features.futures_flow,
            future_price=(
                snapshot.market.future_price
                if snapshot.market is not None
                else None
            ),
            future_open=(
                snapshot.market.future_open
                if snapshot.market is not None
                else None
            ),
            future_previous_close=(
                snapshot.market.future_previous_close
                if snapshot.market is not None
                else None
            ),
            active_pcr=(
                active_pcr
                if self._feature_enabled("strike_pcr")
                else None
            ),
            call_oi_change=metrics.call_oi_change,
            put_oi_change=metrics.put_oi_change,
            call_volume_oi=(
                _ratio(metrics.call_volume, metrics.call_oi)
                or Decimal("0")
                if self._feature_enabled("volume_oi")
                else Decimal("0")
            ),
            put_volume_oi=(
                _ratio(metrics.put_volume, metrics.put_oi)
                or Decimal("0")
                if self._feature_enabled("volume_oi")
                else Decimal("0")
            ),
            call_volume=(
                metrics.call_volume
                if self._feature_enabled("volume_oi")
                else 0
            ),
            put_volume=(
                metrics.put_volume
                if self._feature_enabled("volume_oi")
                else 0
            ),
            call_oi=(
                metrics.call_oi
                if self._feature_enabled("volume_oi")
                else 0
            ),
            put_oi=(
                metrics.put_oi
                if self._feature_enabled("volume_oi")
                else 0
            ),
            atm_straddle_price=(
                metrics.straddle_price
                if self._feature_enabled("straddle_expansion")
                else None
            ),
            atm_call_mid=metrics.atm_call_mid,
            atm_put_mid=metrics.atm_put_mid,
            atm_call_iv=(
                metrics.atm_call_iv
                if (
                    self._feature_enabled("iv_surface")
                    or self._feature_enabled("iv_skew")
                )
                else None
            ),
            atm_put_iv=(
                metrics.atm_put_iv
                if (
                    self._feature_enabled("iv_surface")
                    or self._feature_enabled("iv_skew")
                )
                else None
            ),
            intraday_iv_rank=(
                current_iv_rank
                if volatility_cost_enabled
                else Decimal("0")
            ),
            previous_20d_atr=(
                snapshot.market.previous_20d_atr
                if (
                    snapshot.market is not None
                    and self._feature_enabled("atr_normalization")
                )
                else None
            ),
            india_vix=(
                snapshot.market.india_vix
                if (
                    snapshot.market is not None
                    and self._feature_enabled("india_vix_regime")
                )
                else None
            ),
            is_expiry_day=(
                snapshot.expiry == snapshot.captured_at.date()
            ),
            option_chain_legs=_strategy_option_chain_legs(
                snapshot,
                interval,
            ),
            premium_responses=session_features.premium_responses,
        )
        strategy_candidates.extend(
            self._strategy_registry.evaluate(strategy_context)
        )

        resolution = self._strategy_resolver.resolve(
            candidates=tuple(strategy_candidates),
            regime=market_regime,
        )
        selected_candidate = resolution.selected
        legacy_strategy_diagnostics = _strategy_diagnostics(
            context=strategy_context,
            candidates=tuple(strategy_candidates),
            resolution=resolution,
        )
        strategy_diagnostics = (
            tuple(
                item
                for item in legacy_strategy_diagnostics
                if item.family
                not in {
                    StrategyFamily.DERIVATIVES_QUANT,
                    StrategyFamily.OPTION_CHAIN_IMPULSE,
                    StrategyFamily.SMC,
                }
            )
            + self._strategy_registry.diagnostics
        )
        validated_signal = (
            selected_candidate.side if selected_candidate else "NEUTRAL"
        )
        validation_reason = resolution.reason

        if (
            volatility_cost_enabled
            and market_regime == MarketRegime.UNSTABLE_HIGH_VOL
        ):
            volatility_reason = (
                trap_reason
                or rank_reason
                or "volatility state is unstable"
            )
            validated_signal = "NEUTRAL"
            validation_reason = (
                "REGIME NO TRADE [UNSTABLE_HIGH_VOL]: "
                f"{volatility_reason}"
            )
        elif (
            selected_candidate is not None
            and selected_candidate.family
            == StrategyFamily.LEVEL_REVERSAL
            and (is_trap or is_rank_inflated)
        ):
            validation_reason = (
                "VOLATILITY-COST FILTER: range/reversal option buying is "
                f"too expensive. {trap_reason or rank_reason}"
            )
            validated_signal = "NEUTRAL"

        candle_confirms_reversal = _candle_confirms_reversal(
            context=session_features.candle_pattern,
            side=validated_signal,
            support=support_strike,
            resistance=resistance_strike,
            tolerance=level_tolerance,
            current_spot=current_spot,
        )
        if (
            selected_candidate is not None
            and selected_candidate.setup_type
            == SignalSetup.LOCAL_LEVEL_REVERSAL
        ):
            candle_confirms_reversal = True
        if (
            validated_signal in {"BUY_CALL", "BUY_PUT"}
            and selected_candidate is not None
            and selected_candidate.family == StrategyFamily.LEVEL_REVERSAL
            and self._reversal_candle_confirmation_required
            and not candle_confirms_reversal
        ):
            validated_signal = "NEUTRAL"
            validation_reason = (
                "LEVEL REVERSAL WAIT: no matching closed 4-minute reversal "
                "candle with follow-through at the structural level"
            )

        # =====================================================================
        # PHASE 3: DIRECTIONAL CONFIRMATIONS (NO STANDALONE IV-SKEW SIGNAL)
        # =====================================================================
        if validated_signal in ("BUY_CALL", "BUY_PUT"):
            setup = (
                selected_candidate.setup_type
                if selected_candidate is not None
                else SignalSetup.NONE
            )
            directional_confirmations.append(f"structure:{setup.value}")
            if selected_candidate is not None:
                directional_evidence.extend(selected_candidate.evidence)
            if (
                selected_candidate is not None
                and selected_candidate.setup_type
                == SignalSetup.LOCAL_LEVEL_REVERSAL
            ):
                directional_confirmations.extend(
                    (
                        "local_oi_level_rejection",
                        "closed_reversal_candle_follow_through",
                    )
                )

            if (
                selected_candidate is not None
                and selected_candidate.family == StrategyFamily.LEVEL_REVERSAL
                and candle_confirms_reversal
            ):
                directional_confirmations.append("four_minute_reversal_candle")
                directional_evidence.append(
                    _evidence(
                        "four_minute_reversal_candle",
                        EvidenceFamily.STRUCTURE,
                        validated_signal,
                        Decimal("0.65"),
                    )
                )

            futures_flow = session_features.futures_flow
            if (
                self._feature_enabled("futures_flow")
                and futures_flow is not None
                and futures_flow.side == validated_signal
                and futures_flow.strength > 0
            ):
                directional_confirmations.append("futures_flow")
                directional_evidence.append(
                    _evidence(
                        f"futures_{futures_flow.state.value.lower()}",
                        EvidenceFamily.FLOW,
                        validated_signal,
                        futures_flow.strength,
                    )
                )
            elif (
                self._feature_enabled("futures_flow")
                and futures_flow is not None
                and futures_flow.side is not None
            ):
                directional_conflicts.append("futures_flow_opposes")

            enabled_macro_pcr = (
                pcr_oi
                if self._feature_enabled("consolidated_pcr")
                else None
            )
            enabled_active_pcr = (
                active_pcr
                if self._feature_enabled("strike_pcr")
                else None
            )
            if _pcr_agrees(
                validated_signal,
                enabled_macro_pcr,
                enabled_active_pcr,
            ):
                directional_confirmations.append("pcr_context")
                directional_evidence.append(
                    _evidence(
                        "pcr_context",
                        EvidenceFamily.POSITIONING,
                        validated_signal,
                    )
                )
            if (
                self._feature_enabled("consolidated_pcr")
                and self._feature_enabled("strike_pcr")
                and local_divergence_side == validated_signal
            ):
                directional_confirmations.append("local_divergence")
                directional_evidence.append(
                    _evidence(
                        "local_divergence",
                        EvidenceFamily.POSITIONING,
                        validated_signal,
                        Decimal("0.40"),
                    )
                )
            elif (
                self._feature_enabled("consolidated_pcr")
                and self._feature_enabled("strike_pcr")
                and local_divergence_side is not None
            ):
                directional_conflicts.append("local_divergence_opposes")
            if (
                self._feature_enabled("volume_oi")
                and (
                    validated_signal == "BUY_CALL"
                    and metrics.sup_oi_chg > 0
                    or validated_signal == "BUY_PUT"
                    and metrics.res_oi_chg > 0
                )
            ):
                directional_confirmations.append("boundary_oi_growth")
                directional_evidence.append(
                    _evidence(
                        "boundary_oi_growth",
                        EvidenceFamily.POSITIONING,
                        validated_signal,
                    )
                )

            velocity_side = _chain_velocity_side(
                total_call_velocity,
                total_put_velocity,
            )
            if (
                self._feature_enabled("volume_oi")
                and velocity_side == validated_signal
            ):
                directional_confirmations.append("chain_velocity")
                directional_evidence.append(
                    _evidence(
                        "chain_velocity",
                        EvidenceFamily.FLOW,
                        validated_signal,
                    )
                )
            elif (
                self._feature_enabled("volume_oi")
                and velocity_side is not None
            ):
                directional_conflicts.append("chain_velocity_opposes")

            skew_signal, skew_reason = self._iv_engine.evaluate_iv_skew(
                metrics.support_level[0] if metrics.support_level else None,
                metrics.resistance_level[0] if metrics.resistance_level else None,
                metrics.strike_oi_map,
            )
            if (
                self._feature_enabled("iv_skew")
                and skew_signal == validated_signal
            ):
                directional_confirmations.append("iv_skew")
                directional_evidence.append(
                    _evidence(
                        "iv_skew",
                        EvidenceFamily.VOLATILITY,
                        validated_signal,
                    )
                )
                if skew_reason:
                    validation_reason += f" | IV CONFIRMED: {skew_reason}"
            elif (
                self._feature_enabled("iv_skew")
                and skew_signal is not None
            ):
                directional_conflicts.append("iv_skew_opposes")
                if skew_reason:
                    validation_reason += f" | IV CONFLICT: {skew_reason}"

            if volatility_cost_enabled and (is_trap or is_rank_inflated):
                directional_conflicts.append("high_volatility_cost")

            conviction_note = ""
            if self._feature_enabled("volume_oi"):
                _, conviction_note = (
                    self._iv_engine.evaluate_smart_money_divergence(
                        validated_signal,
                        snapshot.atm_strike,
                        interval,
                        metrics.strike_oi_map,
                    )
                )
            if conviction_note and validation_reason:
                validation_reason += conviction_note
            if "SMART MONEY CONFIRMED" in conviction_note:
                directional_confirmations.append("smart_money")
                directional_evidence.append(
                    _evidence(
                        "smart_money",
                        EvidenceFamily.POSITIONING,
                        validated_signal,
                        Decimal("0.35"),
                    )
                )

        if (
            selected_candidate is not None
            and selected_candidate.family
            in {
                StrategyFamily.DERIVATIVES_QUANT,
                StrategyFamily.GAMMA_EXPANSION,
                StrategyFamily.OPTION_CHAIN_IMPULSE,
                StrategyFamily.SMC,
            }
        ):
            # Quant has its own causal persistence checks. Gamma is confirmed
            # inside the detector before it emits and must not be debounced a
            # second time.
            pass
        else:
            debounce_decision = self._signal_debouncer.update(
                underlying=snapshot.underlying,
                captured_at=snapshot.captured_at,
                signal=validated_signal,
                reason=validation_reason,
            )
            validated_signal = debounce_decision.signal
            validation_reason = debounce_decision.reason

        self._last_spot_price = current_spot

        return self._build_snapshot(
            snapshot,
            pcr_oi,
            validated_signal,
            validation_reason,
            metrics,
            market_regime=market_regime,
            directional_confirmations=tuple(directional_confirmations),
            directional_conflicts=tuple(directional_conflicts),
            intraday_iv_rank=current_iv_rank,
            volatility_cost_high=(
                volatility_cost_enabled
                and (is_trap or is_rank_inflated)
            ),
            strategy_candidates=tuple(strategy_candidates),
            strategy_diagnostics=strategy_diagnostics,
            selected_strategy=(
                selected_candidate.family
                if selected_candidate is not None
                else None
            ),
            selected_setup=(
                selected_candidate.setup_type
                if selected_candidate is not None
                else SignalSetup.NONE
            ),
            activation_level=(
                selected_candidate.activation_level
                if selected_candidate is not None
                else None
            ),
            quant_direction_score=(
                selected_candidate.direction_score
                if selected_candidate is not None
                else None
            ),
            quant_buyability_score=(
                selected_candidate.buyability_score
                if selected_candidate is not None
                else None
            ),
            quant_forecast_underlying_move=(
                selected_candidate.forecast_underlying_move
                if selected_candidate is not None
                else None
            ),
            quant_forecast_iv_change=(
                selected_candidate.forecast_iv_change
                if selected_candidate is not None
                else None
            ),
            local_support=local_support,
            local_resistance=local_resistance,
            resolver_policy=self._strategy_resolver.policy,
            directional_evidence=tuple(
                _deduplicate_evidence(directional_evidence)
            ),
            opening_context=session_features.opening,
            expected_move_context=session_features.expected_move,
            premium_responses=session_features.premium_responses,
            momentum_exhaustion=session_features.momentum_exhaustion,
            futures_flow=session_features.futures_flow,
            candle_pattern=session_features.candle_pattern,
        )

    def _feature_enabled(self, name: str) -> bool:
        return (
            self._enabled_features is None
            or name in self._enabled_features
        )

    def reset(self) -> None:
        """Reset state exactly as a fresh live worker process would."""

        self._reset_intraday_state()
        self._session_features.reset()
        self._strategy_registry.reset()
        self._session_date = None

    def _reset_intraday_state(self) -> None:
        self._morning_straddle_price = None
        self._morning_spot_price = None
        self._straddle_upper_bound = None
        self._straddle_lower_bound = None
        self._straddle_captured = False
        self._last_spot_price = None
        self._last_valid_straddle_price = None
        self._gamma_spring_detector = CoiledSpringDetector(
            window_seconds=self._gamma_window_seconds
        )
        self._iv_engine = IVAnalyticsEngine()
        self._range_rotation.reset()
        self._regime_classifier = MarketRegimeClassifier(
            RegimeSettings(window_seconds=self._regime_window_seconds)
        )
        self._structural_levels.reset()
        self._signal_debouncer.reset()
        self._strategy_registry.reset()

    def with_optimal_target(
        self,
        *,
        snapshot: OptionChainSnapshot,
        analytics: AnalyticsSnapshot,
    ) -> AnalyticsSnapshot:
        """Select an executable contract before microstructure qualification."""

        if analytics.signal not in {"BUY_CALL", "BUY_PUT"}:
            return analytics
        optimal_quote = self._strike_selector.select_optimal_strike(
            snapshot=snapshot,
            signal=analytics.signal,
            expiry_day_fallback_enabled=(
                self._quant_settings.require_expiry_day
            ),
        )
        if optimal_quote is None:
            return analytics
        expected_return_percent = _expected_option_return_percent(
            quote=optimal_quote,
            forecast_underlying_move=(
                analytics.quant_forecast_underlying_move
            ),
            forecast_iv_change=analytics.quant_forecast_iv_change,
            forecast_horizon_seconds=(
                self._quant_settings.forecast_horizon_seconds
            ),
        )
        signal = analytics.signal
        reason = analytics.signal_reason or ""
        if (
            analytics.selected_strategy == StrategyFamily.DERIVATIVES_QUANT
            and (
                expected_return_percent is None
                or expected_return_percent
                < self._quant_settings.minimum_expected_option_return_percent
            )
        ):
            signal = "NEUTRAL"
            rendered_return = (
                "unavailable"
                if expected_return_percent is None
                else f"{expected_return_percent:.3f}%"
            )
            reason = (
                f"{reason} | EXPECTED RETURN FILTER: {rendered_return} "
                f"< {self._quant_settings.minimum_expected_option_return_percent}%"
            )
        return replace(
            analytics,
            signal=signal,
            signal_reason=reason,
            target_strike=optimal_quote.contract.strike,
            target_option_type=optimal_quote.contract.option_type,
            target_ltp=optimal_quote.ltp,
            target_delta=optimal_quote.greeks.delta if optimal_quote.greeks else None,
            quant_expected_option_return_percent=expected_return_percent,
        )

    def _build_snapshot(
        self, 
        snapshot: OptionChainSnapshot, 
        pcr_oi: Decimal | None, 
        signal: str | None, 
        reason: str, 
        metrics: _ChainMetrics,
        *,
        market_regime: MarketRegime = MarketRegime.UNKNOWN,
        directional_confirmations: tuple[str, ...] = (),
        directional_conflicts: tuple[str, ...] = (),
        intraday_iv_rank: Decimal | None = None,
        volatility_cost_high: bool = False,
        strategy_candidates: tuple[StrategyCandidate, ...] = (),
        strategy_diagnostics: tuple[StrategyDiagnostic, ...] = (),
        selected_strategy: StrategyFamily | None = None,
        selected_setup: SignalSetup = SignalSetup.NONE,
        activation_level: Decimal | None = None,
        local_support: Decimal | None = None,
        local_resistance: Decimal | None = None,
        resolver_policy: StrategyResolverPolicy | None = None,
        directional_evidence: tuple[StrategyEvidence, ...] = (),
        opening_context=None,
        expected_move_context=None,
        premium_responses=(),
        momentum_exhaustion=None,
        futures_flow=None,
        candle_pattern=None,
        quant_direction_score: Decimal | None = None,
        quant_buyability_score: Decimal | None = None,
        quant_forecast_underlying_move: Decimal | None = None,
        quant_forecast_iv_change: Decimal | None = None,
    ) -> AnalyticsSnapshot:
        return AnalyticsSnapshot(
            underlying=snapshot.underlying,
            captured_at=snapshot.captured_at,
            atm_strike=snapshot.atm_strike,
            put_call_ratio_oi=pcr_oi,
            put_call_ratio_oi_change=_ratio(metrics.put_oi_change, metrics.call_oi_change),
            atm_straddle_price=self._morning_straddle_price if self._straddle_captured else metrics.straddle_price,
            directional_bias=_pcr_bias(pcr_oi),
            signal=signal or "NEUTRAL",
            signal_reason=reason,
            strategy_source=_legacy_strategy_source(selected_strategy),
            setup_type=selected_setup,
            target_strike=getattr(self, "_latest_target_strike", None),
            target_option_type=getattr(self, "_latest_target_option_type", None),
            target_ltp=getattr(self, "_latest_target_ltp", None),
            target_delta=getattr(self, "_latest_target_delta", None),
            activation_level=activation_level,
            local_support=local_support,
            local_resistance=local_resistance,
            support_levels=metrics.support_level,
            resistance_levels=metrics.resistance_level,
            market_regime=market_regime,
            directional_confirmations=directional_confirmations,
            directional_conflicts=directional_conflicts,
            intraday_iv_rank=intraday_iv_rank,
            volatility_cost_high=volatility_cost_high,
            strategy_candidates=strategy_candidates,
            strategy_diagnostics=strategy_diagnostics,
            selected_strategy=selected_strategy,
            resolver_policy=resolver_policy,
            directional_evidence=directional_evidence,
            opening_context=opening_context,
            expected_move_context=expected_move_context,
            premium_responses=premium_responses,
            momentum_exhaustion=momentum_exhaustion,
            futures_flow=futures_flow,
            candle_pattern=candle_pattern,
            strategy_profile=self._strategy_profile,
            quant_direction_score=quant_direction_score,
            quant_buyability_score=quant_buyability_score,
            quant_forecast_underlying_move=quant_forecast_underlying_move,
            quant_forecast_iv_change=quant_forecast_iv_change,
        )

    def _aggregate_chain_metrics(self, snapshot: OptionChainSnapshot, interval: Decimal | None) -> _ChainMetrics:
        spot = snapshot.spot_price
        atm = snapshot.atm_strike

        call_oi = put_oi = call_oi_chg = put_oi_chg = call_vol = put_vol = 0
        atm_call_ltp = atm_put_ltp = None
        atm_call_vol = atm_call_oi = atm_put_vol = atm_put_oi = 0
        
        strike_oi_map: dict[Decimal, dict[OptionType, dict[str, Any]]] = {}
        max_sup_oi = max_res_oi = -1
        max_sup_dist = max_res_dist = Decimal("-Infinity")
        best_sup_quote = best_res_quote = None
        atm_iv = Decimal("0")
        atm_call_iv = Decimal("0")
        atm_put_iv = Decimal("0")
        atm_call_mid = None
        atm_put_mid = None
        
        for quote in snapshot.quotes:
            strike = quote.contract.strike
            opt_type = quote.contract.option_type
            if not _is_selected_chain_window(strike, atm, interval, opt_type):
                continue
            oi = quote.oi or 0
            oi_change = quote.oi_change or 0
            vol = quote.volume or 0     
            iv = getattr(quote.greeks, "implied_volatility", Decimal("0")) if quote.greeks else Decimal("0")
            
            if strike not in strike_oi_map:
                strike_oi_map[strike] = {
                    OptionType.PUT: {
                        "oi": 0,
                        "oi_change": 0,
                        "volume": 0,
                        "iv": Decimal("0"),
                    },
                    OptionType.CALL: {
                        "oi": 0,
                        "oi_change": 0,
                        "volume": 0,
                        "iv": Decimal("0"),
                    },
                }
                
            strike_oi_map[strike][opt_type]["oi"] += oi
            strike_oi_map[strike][opt_type]["oi_change"] += oi_change
            strike_oi_map[strike][opt_type]["volume"] += vol
            strike_oi_map[strike][opt_type]["iv"] = iv
            if quote.greeks is not None:
                strike_oi_map[strike][opt_type].update(
                    {
                        "delta": quote.greeks.delta,
                        "gamma": quote.greeks.gamma,
                        "theta": quote.greeks.theta,
                        "vega": quote.greeks.vega,
                    }
                )

            if strike == atm and quote.ltp is not None:
                atm_iv = iv
                if opt_type == OptionType.PUT:
                    atm_put_ltp, atm_put_vol, atm_put_oi = quote.ltp, vol, oi
                    atm_put_iv = iv
                    atm_put_mid = _quote_mid(quote)
                elif opt_type == OptionType.CALL:											 
                    atm_call_ltp, atm_call_vol, atm_call_oi = quote.ltp, vol, oi
                    atm_call_iv = iv
                    atm_call_mid = _quote_mid(quote)

            dist = -abs(strike - spot)
            if opt_type == OptionType.PUT:
                put_oi += oi
                put_oi_chg += oi_change
                put_vol += vol
                if strike <= atm and oi > 0 and (oi > max_sup_oi or (oi == max_sup_oi and dist > max_sup_dist)):
                    max_sup_oi, max_sup_dist, best_sup_quote = oi, dist, quote
            elif opt_type == OptionType.CALL:
                call_oi += oi
                call_oi_chg += oi_change
                call_vol += vol
                if strike >= atm and oi > 0 and (oi > max_res_oi or (oi == max_res_oi and dist > max_res_dist)):
                    max_res_oi, max_res_dist, best_res_quote = oi, dist, quote

        straddle_price = self._valid_straddle_price(
            spot=spot,
            call_ltp=atm_call_ltp,
            put_ltp=atm_put_ltp,
        )
        positive_atm_ivs = tuple(
            value
            for value in (atm_call_iv, atm_put_iv)
            if value > 0
        )
        if positive_atm_ivs:
            atm_iv = sum(
                positive_atm_ivs,
                Decimal("0"),
            ) / Decimal(len(positive_atm_ivs))

        strike_level_ratios: dict[Decimal, dict[str, Decimal]] = {}      
        for s, data in strike_oi_map.items():                            
            p_oi, c_oi = data[OptionType.PUT]["oi"], data[OptionType.CALL]["oi"]                           
            p_vol, c_vol = data[OptionType.PUT]["volume"], data[OptionType.CALL]["volume"]                      
            
            strike_level_ratios[s] = {                                   
                "pcr_oi": _ratio(p_oi, c_oi) or Decimal("0"),            
                "pcr_vol": _ratio(p_vol, c_vol) or Decimal("0"),         
                "call_vol_oi": _ratio(c_vol, c_oi) or Decimal("0"),      
                "put_vol_oi": _ratio(p_vol, p_oi) or Decimal("0"),       
            }                                                            
            
        sup_vol = best_sup_quote.volume or 0 if best_sup_quote else 0
        sup_oi = best_sup_quote.oi or 0 if best_sup_quote else 0
        sup_oi_chg = best_sup_quote.oi_change or 0 if best_sup_quote else 0
        res_vol = best_res_quote.volume or 0 if best_res_quote else 0
        res_oi = best_res_quote.oi or 0 if best_res_quote else 0
        res_oi_chg = best_res_quote.oi_change or 0 if best_res_quote else 0

        support_level = tuple(
            SupportResistanceLevel(
                strike=quote.contract.strike,
                option_type=OptionType.PUT,
                oi=quote.oi or 0,
                oi_change=quote.oi_change,
                distance_from_spot=quote.contract.strike - spot,
            )
            for quote in sorted(
                (
                    quote
                    for quote in snapshot.quotes
                    if quote.contract.option_type == OptionType.PUT
                    and quote.contract.strike <= atm
                    and _is_selected_chain_window(quote.contract.strike, atm, interval, quote.contract.option_type)
                ),
                key=lambda quote: (-(quote.oi or 0), abs(quote.contract.strike - spot)),
            )[:3]
            if (quote.oi or 0) > 0
        )
        resistance_level = tuple(
            SupportResistanceLevel(
                strike=quote.contract.strike,
                option_type=OptionType.CALL,
                oi=quote.oi or 0,
                oi_change=quote.oi_change,
                distance_from_spot=quote.contract.strike - spot,
            )
            for quote in sorted(
                (
                    quote
                    for quote in snapshot.quotes
                    if quote.contract.option_type == OptionType.CALL
                    and quote.contract.strike >= atm
                    and _is_selected_chain_window(quote.contract.strike, atm, interval, quote.contract.option_type)
                ),
                key=lambda quote: (-(quote.oi or 0), abs(quote.contract.strike - spot)),
            )[:3]
            if (quote.oi or 0) > 0
        )

        return _ChainMetrics(
            put_oi=put_oi, call_oi=call_oi, put_oi_change=put_oi_chg, call_oi_change=call_oi_chg,
            put_volume=put_vol, call_volume=call_vol, straddle_price=straddle_price,
            strike_level_ratios=strike_level_ratios, support_level=support_level, resistance_level=resistance_level,
            atm_call_vol=atm_call_vol, atm_call_oi=atm_call_oi, atm_put_vol=atm_put_vol, atm_put_oi=atm_put_oi,
            sup_vol=sup_vol, sup_oi=sup_oi, sup_oi_chg=sup_oi_chg, res_vol=res_vol, res_oi=res_oi, res_oi_chg=res_oi_chg,
            atm_iv=atm_iv,
            atm_call_iv=atm_call_iv,
            atm_put_iv=atm_put_iv,
            atm_call_mid=atm_call_mid,
            atm_put_mid=atm_put_mid,
            strike_oi_map=strike_oi_map,
        )

    def _valid_straddle_price(
        self,
        *,
        spot: Decimal,
        call_ltp: Decimal | None,
        put_ltp: Decimal | None,
    ) -> Decimal | None:
        if call_ltp is None or put_ltp is None:
            return None
        max_leg_price = spot * Decimal("0.03")
        if call_ltp <= 0 or put_ltp <= 0 or call_ltp > max_leg_price or put_ltp > max_leg_price:
            return self._last_valid_straddle_price
        straddle = call_ltp + put_ltp
        max_straddle = spot * Decimal("0.05")
        if straddle > max_straddle:
            return self._last_valid_straddle_price
        if self._last_valid_straddle_price is not None and straddle > self._last_valid_straddle_price * Decimal("3"):
            return self._last_valid_straddle_price
        self._last_valid_straddle_price = straddle
        return straddle

# ---------------------------------------------------------
# Core Mathematical Helper Matrix
# ---------------------------------------------------------
def _evidence(
    code: str,
    family: EvidenceFamily,
    side: str,
    strength: Decimal = Decimal("0.70"),
) -> StrategyEvidence:
    return StrategyEvidence(
        code=code,
        family=family,
        side=side,
        strength=strength,
    )


def _coerce_time(value: time | str) -> time:
    if isinstance(value, time):
        return value
    return time.fromisoformat(value)


def _opening_regime_override(
    regime: MarketRegime,
    opening_state: OpeningState,
) -> MarketRegime:
    if regime == MarketRegime.UNSTABLE_HIGH_VOL:
        return regime
    if opening_state in {
        OpeningState.OPENING_DRIVE_UP,
        OpeningState.OPENING_DRIVE_DOWN,
        OpeningState.GAP_AND_GO_UP,
        OpeningState.GAP_AND_GO_DOWN,
    } and regime in {
        MarketRegime.UNKNOWN,
        MarketRegime.TREND_BREAKOUT,
    }:
        return MarketRegime.TREND_BREAKOUT
    if opening_state in {
        OpeningState.BALANCED_FLAT_OPEN,
        OpeningState.GAP_FADE_CANDIDATE_UP,
        OpeningState.GAP_FADE_CANDIDATE_DOWN,
        OpeningState.LARGE_GAP_ABSORPTION,
    } and regime != MarketRegime.COMPRESSION:
        return MarketRegime.RANGE
    return regime


def _select_local_level(
    levels: tuple[SupportResistanceLevel, ...],
    *,
    spot: Decimal,
    interval: Decimal | None,
    minimum_primary_oi_ratio: Decimal = Decimal("0.50"),
) -> Decimal | None:
    if not levels:
        return None
    primary_oi = max(level.oi for level in levels)
    if primary_oi <= 0:
        return None
    maximum_distance = interval or Decimal("0")
    qualified = tuple(
        level
        for level in levels
        if (
            Decimal(level.oi) / Decimal(primary_oi)
            >= minimum_primary_oi_ratio
            and (
                maximum_distance <= 0
                or abs(level.strike - spot) <= maximum_distance
            )
        )
    )
    if not qualified:
        return None
    return min(
        qualified,
        key=lambda level: (
            abs(level.strike - spot),
            -level.oi,
            level.strike,
        ),
    ).strike


def _strategy_diagnostics(
    *,
    context: StrategyEvaluationContext,
    candidates: tuple[StrategyCandidate, ...],
    resolution: StrategyResolution,
) -> tuple[StrategyDiagnostic, ...]:
    by_family = {
        family: tuple(
            candidate
            for candidate in candidates
            if candidate.family == family
        )
        for family in StrategyFamily
    }
    call_ratio = _ratio(
        context.atm_call_volume,
        context.atm_call_oi,
    ) or Decimal("0")
    put_ratio = _ratio(
        context.atm_put_volume,
        context.atm_put_oi,
    ) or Decimal("0")
    candle = context.candle_pattern
    local_side = (
        candle.potential_side
        if candle is not None and candle.follow_through
        else None
    )
    diagnostics: list[StrategyDiagnostic] = []
    checks_by_family = {
        StrategyFamily.LEVEL_REVERSAL: (
            StrategyCheck(
                "expected_move_boundary",
                (
                    context.expected_upper is not None
                    and context.spot >= context.expected_upper
                    or context.expected_lower is not None
                    and context.spot <= context.expected_lower
                ),
                f"spot={context.spot}",
                (
                    f"outside={context.expected_lower}.."
                    f"{context.expected_upper}"
                ),
            ),
            StrategyCheck(
                "local_reversal_candle",
                local_side in {"BUY_CALL", "BUY_PUT"},
                (
                    f"side={local_side}; "
                    f"local_support={context.local_support}; "
                    f"local_resistance={context.local_resistance}"
                ),
                "closed directional candle with follow-through at local level",
            ),
            StrategyCheck(
                "range_rotation",
                context.rotation_signal in {"BUY_CALL", "BUY_PUT"},
                f"signal={context.rotation_signal}",
                "defended boundary with rotation follow-through",
            ),
        ),
        StrategyFamily.BREAKOUT_MOMENTUM: (
            StrategyCheck(
                "structural_break",
                (
                    context.resistance is not None
                    and context.spot > context.resistance
                    or context.support is not None
                    and context.spot < context.support
                ),
                (
                    f"spot={context.spot}; support={context.support}; "
                    f"resistance={context.resistance}"
                ),
                "spot outside active structural range",
            ),
            StrategyCheck(
                "call_volume_oi",
                call_ratio > context.breakout_threshold,
                f"ratio={call_ratio}",
                f">{context.breakout_threshold}",
            ),
            StrategyCheck(
                "put_volume_oi",
                put_ratio > context.breakout_threshold,
                f"ratio={put_ratio}",
                f">{context.breakout_threshold}",
            ),
        ),
        StrategyFamily.GAMMA_EXPANSION: (
            StrategyCheck(
                "gamma_expansion",
                context.gamma_signal in {"BUY_CALL", "BUY_PUT"},
                context.gamma_reason,
                "completed compression plus directional OTM-IV expansion",
            ),
        ),
        StrategyFamily.DERIVATIVES_QUANT: (),
        StrategyFamily.OPTION_CHAIN_IMPULSE: (),
        StrategyFamily.SMC: (),
    }
    for family in StrategyFamily:
        family_candidates = by_family[family]
        selected = (
            resolution.selected is not None
            and resolution.selected.family == family
        )
        if selected:
            status = "SELECTED"
            reason = resolution.selected.reason
            proposed_side = resolution.selected.side
        elif family_candidates:
            status = "SUPPRESSED"
            proposed_side = family_candidates[0].side
            matching_rejections = tuple(
                item
                for item in resolution.rejected
                if item.startswith(f"{family.value}:")
            )
            reason = (
                "; ".join(matching_rejections)
                or "another compatible candidate won resolution"
            )
        else:
            status = "NO_CANDIDATE"
            proposed_side = None
            reason = "strategy entry conditions were not satisfied"
        diagnostics.append(
            StrategyDiagnostic(
                family=family,
                status=status,
                reason=reason,
                proposed_side=proposed_side,
                checks=checks_by_family[family],
            )
        )
    return tuple(diagnostics)


def _deduplicate_evidence(
    items: list[StrategyEvidence],
) -> tuple[StrategyEvidence, ...]:
    strongest: dict[tuple[str, EvidenceFamily, str | None], StrategyEvidence] = {}
    for item in items:
        key = (item.code, item.family, item.side)
        previous = strongest.get(key)
        if previous is None or item.strength > previous.strength:
            strongest[key] = item
    return tuple(strongest.values())


def _candle_confirms_reversal(
    *,
    context: CandlePatternContext | None,
    side: str,
    support: Decimal | None,
    resistance: Decimal | None,
    tolerance: Decimal,
    current_spot: Decimal,
) -> bool:
    """Require a directional shape, structural location and post-close follow-through."""

    if (
        context is None
        or context.potential_side != side
        or not context.follow_through
        or context.close_price is None
    ):
        return False
    if side == "BUY_CALL":
        return (
            support is not None
            and context.low_price is not None
            and abs(context.low_price - support) <= tolerance
            and current_spot > context.close_price
        )
    if side == "BUY_PUT":
        return (
            resistance is not None
            and context.high_price is not None
            and abs(context.high_price - resistance) <= tolerance
            and current_spot < context.close_price
        )
    return False


def _window_interval(snapshot: OptionChainSnapshot) -> Decimal | None:
    strikes = sorted({q.contract.strike for q in snapshot.quotes if q.contract.strike != snapshot.atm_strike})
    if not strikes: return None
    distances = sorted({abs(strike - snapshot.atm_strike) for strike in strikes if abs(strike - snapshot.atm_strike) > 0})
    return distances[0] if distances else None


def _quote_at_strike(
    snapshot: OptionChainSnapshot,
    *,
    strike: Decimal,
    option_type: OptionType,
) -> OptionQuote | None:
    return next(
        (
            quote
            for quote in snapshot.quotes
            if quote.contract.strike == strike
            and quote.contract.option_type == option_type
        ),
        None,
    )


def _gamma_quote_mid(quote: OptionQuote | None) -> float | None:
    if quote is None:
        return None
    mid = _quote_mid(quote)
    return float(mid) if mid is not None else None


def _quote_spread_ratio(quote: OptionQuote | None) -> float | None:
    if (
        quote is None
        or quote.bid is None
        or quote.ask is None
        or quote.bid <= 0
        or quote.ask < quote.bid
    ):
        return None
    midpoint = (quote.bid + quote.ask) / Decimal("2")
    if midpoint <= 0:
        return None
    return float((quote.ask - quote.bid) / midpoint)


def _quote_mid(quote: OptionQuote) -> Decimal | None:
    if (
        quote.bid is not None
        and quote.ask is not None
        and quote.bid > 0
        and quote.ask >= quote.bid
    ):
        return (quote.bid + quote.ask) / Decimal("2")
    return quote.ltp if quote.ltp is not None and quote.ltp > 0 else None


def _strategy_option_chain_legs(
    snapshot: OptionChainSnapshot,
    interval: Decimal | None,
) -> tuple[OptionChainLeg, ...]:
    if interval is None or interval <= 0:
        return ()
    legs: list[OptionChainLeg] = []
    for quote in snapshot.quotes:
        mid = _quote_mid(quote)
        if mid is None or mid <= 0:
            continue
        distance = quote.contract.strike - snapshot.atm_strike
        relative = distance / interval
        if relative != relative.to_integral_value():
            continue
        spread_ratio = None
        if (
            quote.bid is not None
            and quote.ask is not None
            and quote.bid > 0
            and quote.ask >= quote.bid
        ):
            spread_ratio = (quote.ask - quote.bid) / mid
        legs.append(
            OptionChainLeg(
                token=quote.contract.token.token,
                option_type=quote.contract.option_type,
                relative_strike=int(relative),
                mid=mid,
                volume=quote.volume or 0,
                oi=quote.oi or 0,
                spread_ratio=spread_ratio,
            )
        )
    return tuple(legs)


def _expected_option_return_percent(
    *,
    quote: OptionQuote,
    forecast_underlying_move: Decimal | None,
    forecast_iv_change: Decimal | None,
    forecast_horizon_seconds: int,
) -> Decimal | None:
    greeks = quote.greeks
    if (
        greeks is None
        or greeks.delta is None
        or forecast_underlying_move is None
    ):
        return None
    entry_price = (
        quote.ask
        if quote.ask is not None and quote.ask > 0
        else _quote_mid(quote)
    )
    if entry_price is None or entry_price <= 0:
        return None
    move = forecast_underlying_move
    option_change = greeks.delta * move
    if greeks.gamma is not None:
        option_change += (
            Decimal("0.5") * greeks.gamma * move * move
        )
    if greeks.vega is not None and forecast_iv_change is not None:
        option_change += greeks.vega * forecast_iv_change
    if greeks.theta is not None:
        option_change += (
            greeks.theta
            * Decimal(forecast_horizon_seconds)
            / Decimal("86400")
        )
    spread_cost = (
        quote.ask - quote.bid
        if (
            quote.ask is not None
            and quote.bid is not None
            and quote.ask >= quote.bid > 0
        )
        else Decimal("0")
    )
    return (
        (option_change - spread_cost)
        / entry_price
        * Decimal("100")
    ).quantize(Decimal("0.001"))


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0: return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.0001"))

def _is_selected_chain_window(strike: Decimal, atm_strike: Decimal, interval: Decimal | None, option_type: OptionType) -> bool:
    if interval is None or interval <= 0: return strike == atm_strike
    max_distance = Decimal("4") * interval
    # PCR is a property of the complete configured chain, not separate directional
    # halves. Support/resistance selection below applies the call/put side rules.
    return abs(strike - atm_strike) <= max_distance


def _pcr_bias(pcr: Decimal | None) -> str | None:
    if pcr is None:
        return None
    if pcr < Decimal("1"):
        return "bearish"
    if pcr > Decimal("1.4"):
        return "overbought"
    return "neutral"


def _ratio_confidence(
    observed: Decimal,
    threshold: Decimal,
) -> Decimal:
    if threshold <= 0:
        return Decimal("0.65")
    excess = max(Decimal("0"), (observed / threshold) - Decimal("1"))
    return min(
        Decimal("0.95"),
        Decimal("0.65") + excess * Decimal("0.10"),
    ).quantize(Decimal("0.0001"))


def _legacy_strategy_source(
    family: StrategyFamily | None,
) -> str | None:
    if family == StrategyFamily.LEVEL_REVERSAL:
        return "LEVEL_REVERSAL"
    if family == StrategyFamily.BREAKOUT_MOMENTUM:
        return "BREAKOUT"
    if family == StrategyFamily.GAMMA_EXPANSION:
        return "GAMMA"
    if family == StrategyFamily.DERIVATIVES_QUANT:
        return "DERIVATIVES_QUANT"
    if family == StrategyFamily.OPTION_CHAIN_IMPULSE:
        return "OPTION_CHAIN_IMPULSE"
    if family == StrategyFamily.SMC:
        return "SMC"
    return None


def _pcr_agrees(
    side: str,
    macro_pcr: Decimal | None,
    active_pcr: Decimal | None,
) -> bool:
    values = tuple(
        value
        for value in (macro_pcr, active_pcr)
        if value is not None and value > 0
    )
    if side == "BUY_CALL":
        return any(value >= Decimal("1") for value in values)
    if side == "BUY_PUT":
        return any(value <= Decimal("1") for value in values)
    return False


def _chain_velocity_side(
    call_velocity: Decimal,
    put_velocity: Decimal,
) -> str | None:
    if call_velocity > put_velocity * Decimal("3"):
        return "BUY_CALL"
    if put_velocity > call_velocity * Decimal("3"):
        return "BUY_PUT"
    return None


def _get_dynamic_thresholds(underlying: str, dt: datetime) -> tuple[Decimal, Decimal]:
    try:
        #convert string to date time
      
        day = dt.weekday() 
        underlying_upper = underlying.upper() if underlying else ""
        if "SENSEX" in underlying_upper:
            thresholds = {0: (Decimal("1.5"), Decimal("3.0")), 1: (Decimal("2.5"), Decimal("5.0")), 2: (Decimal("4.5"), Decimal("8.0")), 3: (Decimal("18.0"), Decimal("30.0")), 4: (Decimal("0.7"), Decimal("1.5"))}
        else:
            thresholds = {0: (Decimal("4.0"), Decimal("7.0")), 1: (Decimal("15.0"), Decimal("25.0")), 2: (Decimal("0.8"), Decimal("1.5")), 3: (Decimal("1.8"), Decimal("3.5")), 4: (Decimal("1.8"), Decimal("3.5"))}
        return thresholds.get(day, (Decimal("1.5"), Decimal("3.0")))
    except Exception as exp:
        print(f"Error in _get_dynamic_thresholds: {exp}")
        return (Decimal("1.5"), Decimal("3.0"))
