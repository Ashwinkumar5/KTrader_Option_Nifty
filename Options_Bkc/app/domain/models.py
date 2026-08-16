from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class Exchange(StrEnum):
    NSE = "NSE"
    NFO = "NFO"


class OptionType(StrEnum):
    CALL = "CE"
    PUT = "PE"


class InstrumentKind(StrEnum):
    INDEX = "index"
    FUTURE = "future"
    OPTION = "option"


class UnderlyingSymbol(StrEnum):
    NIFTY = "NIFTY"
    


class TickQuality(StrEnum):
    LIVE = "live"
    STALE = "stale"
    RECONNECT_GAP = "reconnect_gap"
    SNAPSHOT = "snapshot"


class SignalSetup(StrEnum):
    """Structured setup families consumed by the signal-quality gate."""

    NONE = "NONE"
    BREAKOUT = "BREAKOUT"
    LEVEL_REVERSAL = "LEVEL_REVERSAL"
    LOCAL_LEVEL_REVERSAL = "LOCAL_LEVEL_REVERSAL"
    RANGE_ROTATION = "RANGE_ROTATION"
    MOMENTUM_EXPANSION = "MOMENTUM_EXPANSION"
    DERIVATIVES_QUANT = "DERIVATIVES_QUANT"
    OPTION_CHAIN_IMPULSE = "OPTION_CHAIN_IMPULSE"
    LIQUIDITY_SWEEP_RECLAIM = "LIQUIDITY_SWEEP_RECLAIM"


class StrategyFamily(StrEnum):
    """Independent entry-strategy families evaluated by the analytics engine."""

    LEVEL_REVERSAL = "LEVEL_REVERSAL"
    BREAKOUT_MOMENTUM = "BREAKOUT_MOMENTUM"
    GAMMA_EXPANSION = "GAMMA_EXPANSION"
    DERIVATIVES_QUANT = "DERIVATIVES_QUANT"
    OPTION_CHAIN_IMPULSE = "OPTION_CHAIN_IMPULSE"
    SMC = "SMC"


class StrategyResolverPolicy(StrEnum):
    """Deterministic policy used when multiple strategy candidates coexist."""

    REGIME_EXCLUSIVE = "REGIME_EXCLUSIVE"
    FIXED_PRIORITY = "FIXED_PRIORITY"
    HIGHEST_CONFIDENCE = "HIGHEST_CONFIDENCE"
    CONFLICT_NO_TRADE = "CONFLICT_NO_TRADE"


class EvidenceFamily(StrEnum):
    STRUCTURE = "STRUCTURE"
    PRICE_ACTION = "PRICE_ACTION"
    POSITIONING = "POSITIONING"
    VOLATILITY = "VOLATILITY"
    FLOW = "FLOW"
    MICROSTRUCTURE = "MICROSTRUCTURE"
    LIQUIDITY = "LIQUIDITY"
    RISK = "RISK"
    SESSION = "SESSION"


class OpeningState(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    OBSERVING_OPEN = "OBSERVING_OPEN"
    BALANCED_FLAT_OPEN = "BALANCED_FLAT_OPEN"
    OPENING_DRIVE_UP = "OPENING_DRIVE_UP"
    OPENING_DRIVE_DOWN = "OPENING_DRIVE_DOWN"
    GAP_FADE_CANDIDATE_UP = "GAP_FADE_CANDIDATE_UP"
    GAP_FADE_CANDIDATE_DOWN = "GAP_FADE_CANDIDATE_DOWN"
    GAP_AND_GO_UP = "GAP_AND_GO_UP"
    GAP_AND_GO_DOWN = "GAP_AND_GO_DOWN"
    LARGE_GAP_ABSORPTION = "LARGE_GAP_ABSORPTION"
    UNSTABLE_OPEN = "UNSTABLE_OPEN"


class GapClass(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    FLAT_OPEN = "FLAT_OPEN"
    MODERATE_GAP = "MODERATE_GAP"
    LARGE_GAP = "LARGE_GAP"
    EXTREME_EVENT_GAP = "EXTREME_EVENT_GAP"


class ExpectedMoveBand(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    INSIDE_FIRST = "INSIDE_FIRST"
    FIRST_EXPANSION = "FIRST_EXPANSION"
    EXTENDED_MOVE = "EXTENDED_MOVE"
    EXHAUSTION_WATCH = "EXHAUSTION_WATCH"


class ExhaustionState(StrEnum):
    NONE = "NONE"
    EARLY_WARNING = "EARLY_WARNING"
    DIRECTIONAL_EXHAUSTION = "DIRECTIONAL_EXHAUSTION"
    IV_CRUSH_ONLY = "IV_CRUSH_ONLY"
    LIQUIDITY_DISTORTION = "LIQUIDITY_DISTORTION"
    CONFIRMED_REVERSAL = "CONFIRMED_REVERSAL"


class TradeManagementAction(StrEnum):
    NONE = "NONE"
    TIGHTEN_STOP = "TIGHTEN_STOP"
    EXIT_OR_TIGHTEN = "EXIT_OR_TIGHTEN"


class FuturesFlowState(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    NEUTRAL = "NEUTRAL"
    LONG_BUILDUP = "LONG_BUILDUP"
    SHORT_BUILDUP = "SHORT_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    LONG_UNWINDING = "LONG_UNWINDING"


class CandlePattern(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    NONE = "NONE"
    DOJI = "DOJI"
    DRAGONFLY_DOJI = "DRAGONFLY_DOJI"
    GRAVESTONE_DOJI = "GRAVESTONE_DOJI"
    HAMMER = "HAMMER"
    SHOOTING_STAR = "SHOOTING_STAR"


class MarketRegime(StrEnum):
    """Mutually exclusive market states used to route strategy families."""

    UNKNOWN = "UNKNOWN"
    RANGE = "RANGE"
    TREND_BREAKOUT = "TREND_BREAKOUT"
    COMPRESSION = "COMPRESSION"
    UNSTABLE_HIGH_VOL = "UNSTABLE_HIGH_VOL"


@dataclass(frozen=True)
class StrategyEvidence:
    code: str
    family: EvidenceFamily
    side: str | None
    strength: Decimal = Decimal("0")
    mandatory: bool = False

    def __post_init__(self) -> None:
        if self.side not in {None, "BUY_CALL", "BUY_PUT"}:
            raise ValueError("evidence side must be directional or None")
        if not Decimal("-1") <= self.strength <= Decimal("1"):
            raise ValueError("evidence strength must be between -1 and 1")
        if not self.code.strip():
            raise ValueError("evidence code must not be empty")


@dataclass(frozen=True)
class StrategyCandidate:
    family: StrategyFamily
    side: str
    setup_type: SignalSetup
    reason: str
    confidence: Decimal = Decimal("0")
    evidence: tuple[StrategyEvidence, ...] = ()
    activation_level: Decimal | None = None
    direction_score: Decimal | None = None
    buyability_score: Decimal | None = None
    forecast_underlying_move: Decimal | None = None
    forecast_iv_change: Decimal | None = None

    def __post_init__(self) -> None:
        if self.side not in {"BUY_CALL", "BUY_PUT"}:
            raise ValueError("strategy candidate side must be directional")
        if not Decimal("0") <= self.confidence <= Decimal("1"):
            raise ValueError(
                "strategy candidate confidence must be between 0 and 1"
            )
        if not self.reason.strip():
            raise ValueError("strategy candidate reason must not be empty")


@dataclass(frozen=True)
class StrategyCheck:
    code: str
    passed: bool
    observed: str
    required: str
    proposed_side: str | None = None


@dataclass(frozen=True)
class StrategyDiagnostic:
    family: StrategyFamily
    status: str
    reason: str
    checks: tuple[StrategyCheck, ...] = ()
    feature_checks: tuple[StrategyCheck, ...] = ()
    proposed_side: str | None = None


@dataclass(frozen=True)
class InstrumentToken:
    exchange: Exchange
    token: str
    symbol: str
    trading_symbol: str
    kind: InstrumentKind | None = None


@dataclass(frozen=True)
class FutureContract:
    underlying: str
    expiry: date
    token: InstrumentToken
    lot_size: int | None = None


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: date
    strike: Decimal
    option_type: OptionType
    token: InstrumentToken
    lot_size: int | None = None


@dataclass(frozen=True)
class MarketTick:
    token: InstrumentToken
    exchange_timestamp: datetime
    received_at: datetime
    ltp: Decimal | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    oi: int | None = None
    oi_change: int | None = None
    oi_change_percent: Decimal | None = None
    volume: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    quality: TickQuality = TickQuality.LIVE
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketDepthLevel:
    """One visible level from the broker's best-five order book."""

    price: Decimal
    quantity: int
    order_count: int | None = None


@dataclass(frozen=True)
class OrderBookSnapshot:
    """A normalized best-five book attached to one exchange event."""

    token: InstrumentToken
    captured_at: datetime
    bids: tuple[MarketDepthLevel, ...]
    asks: tuple[MarketDepthLevel, ...]


@dataclass(frozen=True)
class MicrostructureFeatures:
    """Replayable measurements calculated from one option tick and its visible book."""

    token: InstrumentToken
    captured_at: datetime
    book_imbalance: Decimal | None
    bid_depth: int
    ask_depth: int
    spread: Decimal | None
    premium_velocity: Decimal | None
    event_count: int
    has_complete_book: bool


@dataclass(frozen=True)
class MicrostructureSignal:
    """A research-only candidate emitted after depth and velocity agree."""

    token: InstrumentToken
    underlying: str
    side: str
    captured_at: datetime
    confidence: Decimal
    reason: str


@dataclass(frozen=True)
class GreeksSnapshot:
    contract: OptionContract
    captured_at: datetime
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    source: str = "internal"


@dataclass(frozen=True)
class OptionQuote:
    contract: OptionContract
    ltp: Decimal | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    oi: int | None = None
    oi_change: int | None = None
    oi_change_percent: Decimal | None = None
    volume: int | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    greeks: GreeksSnapshot | None = None


@dataclass(frozen=True)
class UnderlyingReference:
    underlying: str
    index_token: InstrumentToken
    future_token: InstrumentToken | None
    index_price: Decimal | None = None
    future_price: Decimal | None = None
    basis: Decimal | None = None


@dataclass(frozen=True)
class UnderlyingMarketSnapshot:
    underlying: str
    captured_at: datetime
    spot_observed_at: datetime | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    previous_close: Decimal | None = None
    future_observed_at: datetime | None = None
    future_price: Decimal | None = None
    future_open: Decimal | None = None
    future_high: Decimal | None = None
    future_low: Decimal | None = None
    future_previous_close: Decimal | None = None
    future_volume: int | None = None
    future_oi: int | None = None
    future_vwap: Decimal | None = None
    basis: Decimal | None = None
    previous_20d_atr: Decimal | None = None
    previous_session_expected_move: Decimal | None = None
    market_breadth: Decimal | None = None
    india_vix: Decimal | None = None


@dataclass(frozen=True)
class OptionChainSnapshot:
    underlying: str
    expiry: date
    spot_price: Decimal
    atm_strike: Decimal
    captured_at: datetime
    quotes: tuple[OptionQuote, ...]
    reference: UnderlyingReference | None = None
    market: UnderlyingMarketSnapshot | None = None


@dataclass(frozen=True)
class OpeningContext:
    state: OpeningState = OpeningState.UNAVAILABLE
    gap_class: GapClass = GapClass.UNAVAILABLE
    session_open: Decimal | None = None
    previous_close: Decimal | None = None
    opening_high: Decimal | None = None
    opening_low: Decimal | None = None
    gap_points: Decimal | None = None
    normalized_gap: Decimal | None = None
    gap_fill_ratio: Decimal | None = None
    opening_range_points: Decimal | None = None
    direction: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ExpectedMoveContext:
    available: bool = False
    captured_at: datetime | None = None
    anchor_spot: Decimal | None = None
    fixed_strike: Decimal | None = None
    straddle_mid: Decimal | None = None
    minutes_to_expiry: int | None = None
    utilization: Decimal | None = None
    gap_consumption_ratio: Decimal | None = None
    band: ExpectedMoveBand = ExpectedMoveBand.UNAVAILABLE
    first_band: Decimal | None = None
    extended_band: Decimal | None = None
    exhaustion_band: Decimal | None = None
    reason: str = ""


@dataclass(frozen=True)
class PremiumResponse:
    token: str
    option_type: OptionType
    captured_at: datetime
    premium_change: Decimal
    return_percent: Decimal | None
    expected_change: Decimal
    residual_change: Decimal
    spot_change: Decimal
    iv_change: Decimal | None
    spread: Decimal | None
    favorable_actual_change: Decimal | None = None
    favorable_expected_change: Decimal | None = None
    expected_return_percent: Decimal | None = None
    transmission_ratio: Decimal | None = None
    favorable_directional_actual_change: Decimal | None = None
    favorable_directional_expected_change: Decimal | None = None
    directional_expected_return_percent: Decimal | None = None
    directional_transmission_ratio: Decimal | None = None


@dataclass(frozen=True)
class FuturesFlowHorizonContext:
    horizon_seconds: int
    state: FuturesFlowState = FuturesFlowState.NEUTRAL
    side: str | None = None
    price_change: Decimal | None = None
    oi_change: int | None = None
    oi_change_percent: Decimal | None = None
    strength: Decimal = Decimal("0")


@dataclass(frozen=True)
class FuturesPositioningContext:
    ready: bool = False
    state: FuturesFlowState = FuturesFlowState.UNAVAILABLE
    side: str | None = None
    strength: Decimal = Decimal("0")
    horizon_agreement: int = 0
    horizons: tuple[FuturesFlowHorizonContext, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FuturesFlowContext:
    state: FuturesFlowState = FuturesFlowState.UNAVAILABLE
    side: str | None = None
    price_change: Decimal | None = None
    oi_change: int | None = None
    oi_change_percent: Decimal | None = None
    basis_change: Decimal | None = None
    strength: Decimal = Decimal("0")
    reason: str = ""
    positioning: FuturesPositioningContext | None = None


@dataclass(frozen=True)
class CandlePatternContext:
    pattern: CandlePattern = CandlePattern.UNAVAILABLE
    closed_at: datetime | None = None
    open_price: Decimal | None = None
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    close_price: Decimal | None = None
    potential_side: str | None = None
    follow_through: bool = False
    reason: str = ""


@dataclass(frozen=True)
class MomentumExhaustionContext:
    state: ExhaustionState = ExhaustionState.NONE
    winning_side: str | None = None
    opposite_side: str | None = None
    action: TradeManagementAction = TradeManagementAction.NONE
    reason: str = ""


@dataclass(frozen=True)
class SupportResistanceLevel:
    strike: Decimal
    option_type: OptionType
    oi: int
    oi_change: int | None = None
    distance_from_spot: Decimal | None = None


@dataclass(frozen=True)
class AnalyticsSnapshot:
    underlying: str
    captured_at: datetime
    atm_strike: Decimal
    put_call_ratio_oi: Decimal | None = None
    put_call_ratio_oi_change: Decimal | None = None
    strike_level_ratios: dict[Decimal, dict[str, Decimal]] =None# NEW: Renamed from pcr_per_strike and updated type to nested dict
    max_pain: Decimal | None = None
    atm_straddle_price: Decimal | None = None
    gamma_exposure: Decimal | None = None
    directional_bias: str | None = None
    signal: str | None = None
    signal_reason: str | None = None
    target_strike: Decimal | None = None
    target_option_type: OptionType | None = None
    target_ltp: Decimal | None = None
    target_delta: Decimal | None = None
    activation_level: Decimal | None = None
    local_support: Decimal | None = None
    local_resistance: Decimal | None = None
    strategy_source: str | None = None
    setup_type: SignalSetup = SignalSetup.NONE
    support_levels: tuple[SupportResistanceLevel, ...] = ()
    resistance_levels: tuple[SupportResistanceLevel, ...] = ()
    notes: tuple[str, ...] = ()
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    directional_confirmations: tuple[str, ...] = ()
    directional_conflicts: tuple[str, ...] = ()
    intraday_iv_rank: Decimal | None = None
    volatility_cost_high: bool = False
    strategy_candidates: tuple[StrategyCandidate, ...] = ()
    strategy_diagnostics: tuple[StrategyDiagnostic, ...] = ()
    selected_strategy: StrategyFamily | None = None
    resolver_policy: StrategyResolverPolicy | None = None
    directional_evidence: tuple[StrategyEvidence, ...] = ()
    opening_context: OpeningContext | None = None
    expected_move_context: ExpectedMoveContext | None = None
    premium_responses: tuple[PremiumResponse, ...] = ()
    momentum_exhaustion: MomentumExhaustionContext | None = None
    futures_flow: FuturesFlowContext | None = None
    candle_pattern: CandlePatternContext | None = None
    strategy_profile: str | None = None
    quant_direction_score: Decimal | None = None
    quant_buyability_score: Decimal | None = None
    quant_forecast_underlying_move: Decimal | None = None
    quant_forecast_iv_change: Decimal | None = None
    quant_expected_option_return_percent: Decimal | None = None
