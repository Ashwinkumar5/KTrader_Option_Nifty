from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any


DEFAULT_STRATEGY_CONFIG_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "strategy_config.json"
)


_KNOWN_FEATURES = (
    "opening_context",
    "expected_move",
    "candle_patterns",
    "momentum_exhaustion",
    "premium_response",
    "futures_flow",
    "consolidated_pcr",
    "strike_pcr",
    "volume_oi",
    "iv_surface",
    "iv_skew",
    "atr_normalization",
    "india_vix_regime",
    "gamma_concentration",
    "straddle_expansion",
    "futures_basis",
    "order_book_imbalance",
)


_RUNTIME_DIRECTION_COMPONENT_FEATURES: dict[str, tuple[str, ...]] = {
    "futures_flow": ("futures_flow",),
    "index_momentum": ("atr_normalization",),
    "option_premium_momentum": ("premium_response",),
    "option_volume_flow": ("volume_oi",),
    "iv_skew": ("iv_skew",),
    "oi_migration": ("volume_oi",),
    "pcr_context": ("consolidated_pcr", "strike_pcr"),
    "futures_basis": ("futures_basis",),
}

_RUNTIME_OPTION_CHAIN_COMPONENTS = frozenset(
    {
        "option_premium_momentum",
        "option_volume_flow",
        "iv_skew",
        "oi_migration",
        "pcr_context",
    }
)

_RUNTIME_EXPANSION_FEATURES = frozenset(
    {
        "premium_response",
        "iv_surface",
        "gamma_concentration",
        "straddle_expansion",
    }
)


def _decimal(value: object, default: str) -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _integer(value: object, default: int) -> int:
    return default if value is None else int(value)


def _boolean(value: object, default: bool) -> bool:
    return default if value is None else bool(value)


def _strategy_publish_flag(
    value: object,
    *,
    strategy_name: str,
) -> bool:
    """Parse the execution flag strictly so text such as ``"N"`` fails safe."""

    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(
            f"strategy {strategy_name} publish_to_simulator must be boolean"
        )
    return value


@dataclass(frozen=True)
class StrategyToggle:
    enabled: bool
    priority: int
    publish_to_simulator: bool = False

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise ValueError("strategy priority must be non-negative")


@dataclass(frozen=True)
class DerivativesQuantSettings:
    direction_window_seconds: int = 60
    direction_horizons_seconds: tuple[int, ...] = (15, 60, 180)
    normalization_window_seconds: int = 900
    compression_window_seconds: int = 180
    forecast_horizon_seconds: int = 180
    minimum_direction_score: Decimal = Decimal("0.34")
    direction_activation_quantile: Decimal = Decimal("0.85")
    direction_activation_min_observations: int = 20
    warmup_direction_score: Decimal = Decimal("0.34")
    early_direction_score: Decimal = Decimal("0.22")
    early_min_horizon_agreement: int = 3
    early_min_independent_families: int = 4
    early_min_option_chain_families: int = 2
    early_min_buyability_score: Decimal = Decimal("0.65")
    early_max_leg_chase_percent: Decimal = Decimal("4")
    early_score_persistence_frames: int = 2
    require_early_acceleration: bool = True
    minimum_buyability_score: Decimal = Decimal("0.50")
    minimum_independent_families: int = 3
    minimum_horizon_agreement: int = 2
    zscore_clip: Decimal = Decimal("3")
    minimum_expected_option_return_percent: Decimal = Decimal("3")
    minimum_straddle_expansion_percent: Decimal = Decimal("0.35")
    minimum_iv_expansion_percent: Decimal = Decimal("0.25")
    minimum_leg_impulse_zscore: Decimal = Decimal("1")
    maximum_iv_rank: Decimal = Decimal("85")
    maximum_leg_chase_percent: Decimal = Decimal("8")
    maximum_compression_range_points: Decimal = Decimal("25")
    minimum_compression_observations: int = 5
    require_compression: bool = False
    require_expansion_trigger: bool = True
    require_momentum_expansion_trigger: bool = True
    require_futures_flow: bool = False
    require_expiry_day: bool = False
    weights: dict[str, Decimal] = field(
        default_factory=lambda: {
            "futures_flow": Decimal("0.22"),
            "index_momentum": Decimal("0.16"),
            "option_premium_momentum": Decimal("0.20"),
            "option_volume_flow": Decimal("0.14"),
            "iv_skew": Decimal("0.10"),
            "oi_migration": Decimal("0.05"),
            "pcr_context": Decimal("0.05"),
            "futures_basis": Decimal("0.04"),
        }
    )

    def __post_init__(self) -> None:
        for name in (
            "direction_window_seconds",
            "normalization_window_seconds",
            "compression_window_seconds",
            "forecast_horizon_seconds",
            "direction_activation_min_observations",
            "early_min_horizon_agreement",
            "early_min_independent_families",
            "early_score_persistence_frames",
            "minimum_compression_observations",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.early_min_option_chain_families < 0:
            raise ValueError(
                "early_min_option_chain_families cannot be negative"
            )
        for name in (
            "minimum_direction_score",
            "direction_activation_quantile",
            "warmup_direction_score",
            "early_direction_score",
            "early_min_buyability_score",
            "minimum_buyability_score",
        ):
            value = getattr(self, name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between zero and one")
        if self.direction_activation_quantile <= 0:
            raise ValueError(
                "direction_activation_quantile must be greater than zero"
            )
        if self.warmup_direction_score < self.minimum_direction_score:
            raise ValueError(
                "warmup direction score must not be below the score floor"
            )
        if self.early_direction_score >= self.minimum_direction_score:
            raise ValueError(
                "early direction score must be below the strong score floor"
            )
        if self.early_max_leg_chase_percent <= 0:
            raise ValueError(
                "early maximum leg chase percent must be positive"
            )
        if not self.direction_horizons_seconds or any(
            item <= 0 for item in self.direction_horizons_seconds
        ):
            raise ValueError("direction horizons must be positive")
        if tuple(sorted(set(self.direction_horizons_seconds))) != (
            self.direction_horizons_seconds
        ):
            raise ValueError(
                "direction horizons must be unique and increasing"
            )
        if self.minimum_independent_families <= 0:
            raise ValueError("minimum_independent_families must be positive")
        if not 1 <= self.minimum_horizon_agreement <= len(
            self.direction_horizons_seconds
        ):
            raise ValueError(
                "minimum horizon agreement exceeds configured horizons"
            )
        if not 1 <= self.early_min_horizon_agreement <= len(
            self.direction_horizons_seconds
        ):
            raise ValueError(
                "early horizon agreement exceeds configured horizons"
            )
        if self.zscore_clip <= 0:
            raise ValueError("zscore clip must be positive")
        if self.minimum_leg_impulse_zscore < 0:
            raise ValueError(
                "minimum leg impulse z-score must be non-negative"
            )
        if any(value < 0 for value in self.weights.values()):
            raise ValueError("quant feature weights must be non-negative")
        if sum(self.weights.values(), Decimal("0")) <= 0:
            raise ValueError("at least one quant feature weight is required")


@dataclass(frozen=True)
class OptionChainImpulseSettings:
    window_seconds: int = 30
    strike_depth: int = 2
    minimum_legs_per_side: int = 3
    minimum_basket_return_percent: Decimal = Decimal("0.80")
    maximum_opposite_return_percent: Decimal = Decimal("-0.30")
    minimum_return_gap_percent: Decimal = Decimal("1.20")
    maximum_return_gap_percent: Decimal = Decimal("3.00")
    minimum_same_side_breadth: Decimal = Decimal("0.60")
    minimum_opposite_decay_breadth: Decimal = Decimal("0.60")
    same_side_leg_return_percent: Decimal = Decimal("0.30")
    opposite_leg_decay_percent: Decimal = Decimal("-0.20")
    minimum_residual_return_percent: Decimal = Decimal("0.10")
    minimum_residual_breadth: Decimal = Decimal("0.60")
    minimum_volume_ratio: Decimal = Decimal("0.75")
    maximum_basket_chase_percent: Decimal = Decimal("2.50")
    maximum_average_spread_ratio: Decimal = Decimal("0.025")
    aggregate_residual_over_window: bool = False

    def __post_init__(self) -> None:
        for name in ("window_seconds", "strike_depth", "minimum_legs_per_side"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.minimum_basket_return_percent <= 0:
            raise ValueError("minimum_basket_return_percent must be positive")
        if self.minimum_return_gap_percent <= 0:
            raise ValueError("minimum_return_gap_percent must be positive")
        if self.maximum_return_gap_percent <= self.minimum_return_gap_percent:
            raise ValueError(
                "maximum_return_gap_percent must exceed the minimum"
            )
        if self.maximum_basket_chase_percent <= 0:
            raise ValueError("maximum_basket_chase_percent must be positive")
        if self.minimum_residual_return_percent <= 0:
            raise ValueError("minimum_residual_return_percent must be positive")
        if self.minimum_volume_ratio < 0:
            raise ValueError("minimum_volume_ratio cannot be negative")
        if not Decimal("0") <= self.minimum_same_side_breadth <= Decimal("1"):
            raise ValueError("minimum_same_side_breadth must be between zero and one")
        if not Decimal("0") <= self.minimum_opposite_decay_breadth <= Decimal("1"):
            raise ValueError(
                "minimum_opposite_decay_breadth must be between zero and one"
            )
        if not Decimal("0") <= self.minimum_residual_breadth <= Decimal("1"):
            raise ValueError("minimum_residual_breadth must be between zero and one")
        if not Decimal("0") < self.maximum_average_spread_ratio <= Decimal("1"):
            raise ValueError(
                "maximum_average_spread_ratio must be between zero and one"
            )


@dataclass(frozen=True)
class SMCSettings:
    """Causal settings for the NIFTY-futures liquidity sweep model."""

    opening_range_minutes: int = 15
    swing_left_frames: int = 3
    swing_right_frames: int = 3
    structure_lookback_frames: int = 12
    displacement_lookback_frames: int = 60
    maximum_active_levels_per_side: int = 8
    maximum_level_age_minutes: int = 180
    minimum_sweep_points: Decimal = Decimal("2")
    reclaim_buffer_points: Decimal = Decimal("0.5")
    structure_break_buffer_points: Decimal = Decimal("0.5")
    minimum_displacement_points: Decimal = Decimal("4")
    displacement_multiplier: Decimal = Decimal("1.5")
    maximum_reclaim_seconds: int = 30
    maximum_structure_break_seconds: int = 60
    option_confirmation_ttl_seconds: int = 30
    event_cooldown_seconds: int = 120
    require_cross_strike_confirmation: bool = True

    def __post_init__(self) -> None:
        for name in (
            "opening_range_minutes",
            "swing_left_frames",
            "swing_right_frames",
            "structure_lookback_frames",
            "displacement_lookback_frames",
            "maximum_active_levels_per_side",
            "maximum_level_age_minutes",
            "maximum_reclaim_seconds",
            "maximum_structure_break_seconds",
            "option_confirmation_ttl_seconds",
            "event_cooldown_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"SMC {name} must be positive")
        for name in (
            "minimum_sweep_points",
            "reclaim_buffer_points",
            "structure_break_buffer_points",
            "minimum_displacement_points",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"SMC {name} cannot be negative")
        if self.displacement_multiplier <= 0:
            raise ValueError("SMC displacement_multiplier must be positive")


@dataclass(frozen=True)
class QuantMicrostructureSettings:
    feature_window_seconds: int = 3
    feature_min_events: int = 3
    minimum_book_imbalance: Decimal = Decimal("0.25")
    minimum_price_velocity: Decimal = Decimal("0.50")
    minimum_option_velocity_percent_per_second: Decimal = Decimal("0.15")
    maximum_spread_points: Decimal = Decimal("1.50")
    require_target_option_confirmation: bool = True
    require_futures_confirmation: bool = False
    minimum_futures_confirmations: int = 2
    minimum_option_confirmations: int = 2
    maximum_age_seconds: int = 5
    minimum_confidence: Decimal = Decimal("0.35")
    gate_minimum_directional_confirmations: int | None = None
    gate_minimum_independent_families: int | None = None
    gate_minimum_confirmations: int | None = None
    gamma_require_structural_room: bool = True
    event_driven_entry: bool = False
    candidate_ttl_seconds: int = 10
    minimum_candidate_premium_chase_percent: Decimal | None = None
    maximum_candidate_premium_chase_percent: Decimal = Decimal("2")
    require_directional_option_book: bool = False
    event_entry_cutoff_time: str | None = None

    def __post_init__(self) -> None:
        if self.feature_window_seconds <= 0:
            raise ValueError(
                "microstructure feature window must be positive"
            )
        if self.feature_min_events <= 0:
            raise ValueError(
                "microstructure feature event count must be positive"
            )
        if not Decimal("0") <= self.minimum_book_imbalance <= Decimal("1"):
            raise ValueError(
                "minimum book imbalance must be between zero and one"
            )
        if self.minimum_price_velocity < 0:
            raise ValueError(
                "minimum price velocity must be non-negative"
            )
        if self.minimum_option_velocity_percent_per_second < 0:
            raise ValueError(
                "minimum option velocity percent must be non-negative"
            )
        if self.maximum_spread_points <= 0:
            raise ValueError(
                "maximum spread points must be positive"
            )
        if self.minimum_futures_confirmations < 0:
            raise ValueError("minimum_futures_confirmations cannot be negative")
        if self.minimum_option_confirmations < 0:
            raise ValueError("minimum_option_confirmations cannot be negative")
        if self.maximum_age_seconds <= 0:
            raise ValueError("maximum_age_seconds must be positive")
        if not Decimal("0") <= self.minimum_confidence <= Decimal("1"):
            raise ValueError("minimum_confidence must be between zero and one")
        if self.candidate_ttl_seconds <= 0:
            raise ValueError("candidate_ttl_seconds must be positive")
        if self.maximum_candidate_premium_chase_percent <= 0:
            raise ValueError(
                "maximum_candidate_premium_chase_percent must be positive"
            )
        if (
            self.minimum_candidate_premium_chase_percent is not None
            and self.minimum_candidate_premium_chase_percent
            > self.maximum_candidate_premium_chase_percent
        ):
            raise ValueError(
                "minimum_candidate_premium_chase_percent cannot exceed "
                "the maximum"
            )
        if self.event_entry_cutoff_time is not None:
            try:
                time.fromisoformat(self.event_entry_cutoff_time)
            except ValueError as exc:
                raise ValueError(
                    "event_entry_cutoff_time must use HH:MM or HH:MM:SS"
                ) from exc
        for name in (
            "gate_minimum_directional_confirmations",
            "gate_minimum_independent_families",
            "gate_minimum_confirmations",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class QuantExecutionSettings:
    stop_percent: Decimal = Decimal("5")
    target_percent: Decimal = Decimal("10")
    maximum_hold_minutes: int = 15
    cooldown_seconds: int = 900
    trailing_activation_percent: Decimal | None = None
    trailing_drawdown_percent: Decimal | None = None
    no_follow_through_seconds: int | None = None
    minimum_follow_through_percent: Decimal | None = None
    event_driven_exit: bool = False
    close_at_tape_end: bool = False

    def __post_init__(self) -> None:
        if self.stop_percent <= 0:
            raise ValueError("stop_percent must be positive")
        if self.target_percent <= 0:
            raise ValueError("target_percent must be positive")
        if self.maximum_hold_minutes <= 0:
            raise ValueError("maximum_hold_minutes must be positive")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        if (self.trailing_activation_percent is None) != (
            self.trailing_drawdown_percent is None
        ):
            raise ValueError(
                "trailing activation and drawdown must be configured together"
            )
        if (
            self.trailing_activation_percent is not None
            and self.trailing_activation_percent <= 0
        ):
            raise ValueError("trailing_activation_percent must be positive")
        if (
            self.trailing_drawdown_percent is not None
            and self.trailing_drawdown_percent <= 0
        ):
            raise ValueError("trailing_drawdown_percent must be positive")
        if (self.no_follow_through_seconds is None) != (
            self.minimum_follow_through_percent is None
        ):
            raise ValueError(
                "no-follow-through time and return must be configured together"
            )
        if (
            self.no_follow_through_seconds is not None
            and self.no_follow_through_seconds <= 0
        ):
            raise ValueError("no_follow_through_seconds must be positive")
        if (
            self.minimum_follow_through_percent is not None
            and self.minimum_follow_through_percent < 0
        ):
            raise ValueError(
                "minimum_follow_through_percent cannot be negative"
            )


@dataclass(frozen=True)
class StrategyProfile:
    name: str
    description: str
    strategies: dict[str, StrategyToggle]
    features: dict[str, bool]
    quant: DerivativesQuantSettings
    impulse: OptionChainImpulseSettings
    smc: SMCSettings
    microstructure: QuantMicrostructureSettings
    execution: QuantExecutionSettings

    def __post_init__(self) -> None:
        enabled = [
            item.priority
            for item in self.strategies.values()
            if item.enabled
        ]
        if not enabled:
            raise ValueError("a strategy profile must enable at least one strategy")
        if len(enabled) != len(set(enabled)):
            raise ValueError("enabled strategies require unique priorities")

    def strategy_enabled(self, name: str) -> bool:
        item = self.strategies.get(name.upper())
        return bool(item and item.enabled)

    def strategy_priority(self, name: str, default: int = 100) -> int:
        item = self.strategies.get(name.upper())
        return item.priority if item is not None else default

    def strategy_publishes_to_simulator(
        self,
        name: str,
        default: bool = False,
    ) -> bool:
        item = self.strategies.get(name.upper())
        return (
            item.publish_to_simulator
            if item is not None
            else default
        )

    def feature_enabled(self, name: str, default: bool = False) -> bool:
        return self.features.get(name, default)


@dataclass(frozen=True)
class StrategyConfiguration:
    version: int
    source_path: Path
    source_sha256: str
    profile: StrategyProfile

    def manifest(self) -> dict[str, Any]:
        payload = asdict(self.profile)
        payload["quant"]["weights"] = {
            key: str(value)
            for key, value in self.profile.quant.weights.items()
        }
        for section in (
            "quant",
            "impulse",
            "smc",
            "microstructure",
            "execution",
        ):
            payload[section] = {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in payload[section].items()
            }
        return {
            "version": self.version,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "profile": payload,
        }


def load_strategy_configuration(
    path: str | Path | None = None,
    *,
    profile_name: str | None = None,
) -> StrategyConfiguration:
    source = Path(path) if path else DEFAULT_STRATEGY_CONFIG_PATH
    source = source.expanduser().resolve()
    raw_bytes = source.read_bytes()
    document = json.loads(raw_bytes)
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("strategy configuration has no profiles")
    selected_name = str(
        profile_name or document.get("active_profile") or ""
    ).strip()
    if selected_name not in profiles:
        raise ValueError(f"unknown strategy profile: {selected_name}")
    resolved = _resolve_profile(selected_name, profiles, ())
    profile = _parse_profile(selected_name, resolved)
    return StrategyConfiguration(
        version=int(document.get("version") or 1),
        source_path=source,
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        profile=profile,
    )


def available_strategy_profiles(
    path: str | Path | None = None,
) -> tuple[str, ...]:
    source = Path(path) if path else DEFAULT_STRATEGY_CONFIG_PATH
    document = json.loads(source.expanduser().resolve().read_bytes())
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("strategy configuration has no profiles")
    return tuple(str(name) for name in profiles)


def apply_runtime_strategy_selection(
    configuration: StrategyConfiguration,
    *,
    enabled_strategies: tuple[str, ...] | None = None,
    enabled_features: tuple[str, ...] | None = None,
    minimum_book_imbalance: Decimal | None = None,
    strategy_priority: tuple[str, ...] | None = None,
) -> StrategyConfiguration:
    """Apply explicit live-instance selections over a loaded profile.

    ``None`` means that the corresponding section remains entirely owned by
    the configured profile. When a selection is supplied, every unselected
    item in that section is disabled.
    """
    if (
        enabled_strategies is None
        and enabled_features is None
        and minimum_book_imbalance is None
        and strategy_priority is None
    ):
        return configuration

    profile = configuration.profile
    strategies = profile.strategies
    features = profile.features
    quant = profile.quant
    microstructure = profile.microstructure
    audit_parts: list[str] = []

    if enabled_strategies is not None:
        selected_strategies = _resolve_runtime_selection(
            enabled_strategies,
            available=tuple(strategies),
            aliases={"GAMMA_BLAST": "GAMMA_EXPANSION"},
            label="strategy",
            uppercase=True,
        )
        strategies = {
            name: replace(toggle, enabled=name in selected_strategies)
            for name, toggle in strategies.items()
        }
        audit_parts.append(
            "strategies=" + ",".join(sorted(selected_strategies))
        )

    if strategy_priority is not None:
        priority_order = _resolve_runtime_selection_order(
            strategy_priority,
            available=tuple(strategies),
            aliases={"GAMMA_BLAST": "GAMMA_EXPANSION"},
            label="strategy priority",
            uppercase=True,
        )
        enabled_names = {
            name for name, toggle in strategies.items() if toggle.enabled
        }
        if set(priority_order) != enabled_names:
            raise ValueError(
                "strategy priority must contain every enabled strategy "
                "exactly once"
            )
        priority_by_name = {
            name: index * 10
            for index, name in enumerate(priority_order, start=1)
        }
        strategies = {
            name: replace(
                toggle,
                priority=priority_by_name.get(name, toggle.priority),
            )
            for name, toggle in strategies.items()
        }
        audit_parts.append(
            "strategy_priority=" + ",".join(priority_order)
        )

    if enabled_features is not None:
        selected_features = _resolve_runtime_selection(
            enabled_features,
            available=tuple(features),
            aliases={"gamma_blast": "gamma_concentration"},
            label="feature",
            uppercase=False,
        )
        features = {
            name: name in selected_features
            for name in features
        }
        if "order_book_imbalance" not in selected_features:
            microstructure = replace(
                microstructure,
                require_target_option_confirmation=False,
                require_futures_confirmation=False,
                minimum_option_confirmations=0,
                minimum_futures_confirmations=0,
            )
        if any(
            toggle.enabled
            for name, toggle in strategies.items()
            if name == "DERIVATIVES_QUANT"
        ):
            quant = _normalize_runtime_quant_settings(
                quant,
                selected_features,
            )
        audit_parts.append(
            "features=" + ",".join(sorted(selected_features))
        )

    if minimum_book_imbalance is not None:
        microstructure = replace(
            microstructure,
            minimum_book_imbalance=Decimal(
                str(minimum_book_imbalance)
            ),
        )
        audit_parts.append(
            "minimum_book_imbalance="
            + str(microstructure.minimum_book_imbalance)
        )

    effective_profile = replace(
        profile,
        name=f"{profile.name}__runtime",
        description=(
            f"{profile.description} Runtime selection: "
            + "; ".join(audit_parts)
        ),
        strategies=strategies,
        features=features,
        quant=quant,
        microstructure=microstructure,
    )
    return replace(configuration, profile=effective_profile)


def _normalize_runtime_quant_settings(
    settings: DerivativesQuantSettings,
    selected_features: frozenset[str],
) -> DerivativesQuantSettings:
    """Make explicit feature subsets comparable without changing defaults."""

    active_weights: dict[str, Decimal] = {}
    for component, feature_names in (
        _RUNTIME_DIRECTION_COMPONENT_FEATURES.items()
    ):
        if not selected_features.intersection(feature_names):
            continue
        configured = settings.weights.get(component, Decimal("0"))
        if configured > 0:
            active_weights[component] = configured

    total = sum(active_weights.values(), Decimal("0"))
    if total <= 0:
        raise ValueError(
            "DERIVATIVES_QUANT feature selection requires at least one "
            "directional feature; use Phase 1 paired ablation for "
            "context-only features"
        )

    normalized = {
        name: (
            active_weights.get(name, Decimal("0")) / total
        ).quantize(Decimal("0.000001"))
        for name in settings.weights
    }
    active_count = len(active_weights)
    active_option_count = sum(
        name in _RUNTIME_OPTION_CHAIN_COMPONENTS
        for name in active_weights
    )
    return replace(
        settings,
        weights=normalized,
        minimum_independent_families=min(
            settings.minimum_independent_families,
            active_count,
        ),
        early_min_independent_families=min(
            settings.early_min_independent_families,
            active_count,
        ),
        early_min_option_chain_families=min(
            settings.early_min_option_chain_families,
            max(1, active_option_count),
        ),
        require_expansion_trigger=(
            settings.require_expansion_trigger
            and bool(
                selected_features.intersection(
                    _RUNTIME_EXPANSION_FEATURES
                )
            )
        ),
        require_futures_flow=(
            settings.require_futures_flow
            and "futures_flow" in selected_features
        ),
    )


def _resolve_runtime_selection(
    requested: tuple[str, ...],
    *,
    available: tuple[str, ...],
    aliases: dict[str, str],
    label: str,
    uppercase: bool,
) -> frozenset[str]:
    canonical_available = {
        (name.upper() if uppercase else name.lower()): name
        for name in available
    }
    selected: set[str] = set()
    unknown: set[str] = set()
    for raw_name in requested:
        candidate = str(raw_name).strip()
        if not candidate:
            continue
        candidate = candidate.upper() if uppercase else candidate.lower()
        candidate = aliases.get(candidate, candidate)
        actual_name = canonical_available.get(candidate)
        if actual_name is None:
            unknown.add(str(raw_name).strip())
        else:
            selected.add(actual_name)

    if unknown:
        raise ValueError(
            f"unknown {label}(s): {', '.join(sorted(unknown))}; "
            f"available: {', '.join(sorted(available))}"
        )
    if not selected:
        raise ValueError(f"at least one {label} must be selected")
    return frozenset(selected)


def _resolve_runtime_selection_order(
    requested: tuple[str, ...],
    *,
    available: tuple[str, ...],
    aliases: dict[str, str],
    label: str,
    uppercase: bool,
) -> tuple[str, ...]:
    canonical_available = {
        (name.upper() if uppercase else name.lower()): name
        for name in available
    }
    resolved: list[str] = []
    unknown: set[str] = set()
    for raw_name in requested:
        candidate = str(raw_name).strip()
        if not candidate:
            continue
        candidate = candidate.upper() if uppercase else candidate.lower()
        candidate = aliases.get(candidate, candidate)
        actual_name = canonical_available.get(candidate)
        if actual_name is None:
            unknown.add(str(raw_name).strip())
        else:
            resolved.append(actual_name)
    if unknown:
        raise ValueError(
            f"unknown {label}(s): {', '.join(sorted(unknown))}; "
            f"available: {', '.join(sorted(available))}"
        )
    if not resolved:
        raise ValueError(f"at least one {label} must be selected")
    if len(resolved) != len(set(resolved)):
        raise ValueError(f"{label} contains duplicates")
    return tuple(resolved)


def _resolve_profile(
    name: str,
    profiles: dict[str, object],
    stack: tuple[str, ...],
) -> dict[str, object]:
    if name in stack:
        raise ValueError("cyclic strategy profile inheritance")
    raw = profiles.get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"strategy profile {name} must be an object")
    parent_name = raw.get("extends")
    if not parent_name:
        return dict(raw)
    parent = _resolve_profile(str(parent_name), profiles, stack + (name,))
    child = {key: value for key, value in raw.items() if key != "extends"}
    resolved = _deep_merge(parent, child)
    if "features" in child:
        resolved["features"] = child["features"]
    child_quant = child.get("quant")
    resolved_quant = resolved.get("quant")
    if (
        isinstance(child_quant, dict)
        and "weights" in child_quant
        and isinstance(resolved_quant, dict)
    ):
        resolved_quant = dict(resolved_quant)
        resolved_quant["weights"] = child_quant["weights"]
        resolved["quant"] = resolved_quant
    return resolved


def _deep_merge(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _parse_profile(name: str, data: dict[str, object]) -> StrategyProfile:
    raw_strategies = data.get("strategies")
    if not isinstance(raw_strategies, dict):
        raise ValueError(f"profile {name} has no strategies")
    strategies = {}
    for strategy_name, raw in raw_strategies.items():
        if not isinstance(raw, dict):
            raise ValueError(f"strategy {strategy_name} must be an object")
        strategies[str(strategy_name).upper()] = StrategyToggle(
            enabled=_boolean(raw.get("enabled"), False),
            priority=_integer(raw.get("priority"), 100),
            publish_to_simulator=_strategy_publish_flag(
                raw.get("publish_to_simulator"),
                strategy_name=str(strategy_name),
            ),
        )

    raw_features = data.get("features")
    configured_features = (
        {str(key): bool(value) for key, value in raw_features.items()}
        if isinstance(raw_features, dict)
        else {}
    )
    features = {
        name: configured_features.get(name, False)
        for name in _KNOWN_FEATURES
    }
    features.update(configured_features)
    raw_quant = data.get("quant")
    raw_quant = raw_quant if isinstance(raw_quant, dict) else {}
    raw_weights = raw_quant.get("weights")
    default_weights = DerivativesQuantSettings().weights
    if isinstance(raw_weights, dict):
        weights = {name: Decimal("0") for name in default_weights}
        weights.update(
            {
                str(key): Decimal(str(value))
                for key, value in raw_weights.items()
            }
        )
    else:
        weights = default_weights
    quant = DerivativesQuantSettings(
        direction_window_seconds=_integer(
            raw_quant.get("direction_window_seconds"), 60
        ),
        direction_horizons_seconds=tuple(
            int(value)
            for value in raw_quant.get(
                "direction_horizons_seconds",
                (15, 60, 180),
            )
        ),
        normalization_window_seconds=_integer(
            raw_quant.get("normalization_window_seconds"), 900
        ),
        compression_window_seconds=_integer(
            raw_quant.get("compression_window_seconds"), 180
        ),
        forecast_horizon_seconds=_integer(
            raw_quant.get("forecast_horizon_seconds"), 180
        ),
        minimum_direction_score=_decimal(
            raw_quant.get("minimum_direction_score"), "0.34"
        ),
        direction_activation_quantile=_decimal(
            raw_quant.get("direction_activation_quantile"), "0.85"
        ),
        direction_activation_min_observations=_integer(
            raw_quant.get("direction_activation_min_observations"), 20
        ),
        warmup_direction_score=_decimal(
            raw_quant.get("warmup_direction_score"), "0.34"
        ),
        early_direction_score=_decimal(
            raw_quant.get("early_direction_score"), "0.22"
        ),
        early_min_horizon_agreement=_integer(
            raw_quant.get("early_min_horizon_agreement"), 3
        ),
        early_min_independent_families=_integer(
            raw_quant.get("early_min_independent_families"), 4
        ),
        early_min_option_chain_families=_integer(
            raw_quant.get("early_min_option_chain_families"), 2
        ),
        early_min_buyability_score=_decimal(
            raw_quant.get("early_min_buyability_score"), "0.65"
        ),
        early_max_leg_chase_percent=_decimal(
            raw_quant.get("early_max_leg_chase_percent"), "4"
        ),
        early_score_persistence_frames=_integer(
            raw_quant.get("early_score_persistence_frames"), 2
        ),
        require_early_acceleration=_boolean(
            raw_quant.get("require_early_acceleration"), True
        ),
        minimum_buyability_score=_decimal(
            raw_quant.get("minimum_buyability_score"), "0.50"
        ),
        minimum_independent_families=_integer(
            raw_quant.get("minimum_independent_families"), 3
        ),
        minimum_horizon_agreement=_integer(
            raw_quant.get("minimum_horizon_agreement"), 2
        ),
        zscore_clip=_decimal(
            raw_quant.get("zscore_clip"), "3"
        ),
        minimum_expected_option_return_percent=_decimal(
            raw_quant.get("minimum_expected_option_return_percent"), "3"
        ),
        minimum_straddle_expansion_percent=_decimal(
            raw_quant.get("minimum_straddle_expansion_percent"), "0.35"
        ),
        minimum_iv_expansion_percent=_decimal(
            raw_quant.get("minimum_iv_expansion_percent"), "0.25"
        ),
        minimum_leg_impulse_zscore=_decimal(
            raw_quant.get("minimum_leg_impulse_zscore"), "1"
        ),
        maximum_iv_rank=_decimal(
            raw_quant.get("maximum_iv_rank"), "85"
        ),
        maximum_leg_chase_percent=_decimal(
            raw_quant.get("maximum_leg_chase_percent"), "8"
        ),
        maximum_compression_range_points=_decimal(
            raw_quant.get("maximum_compression_range_points"), "25"
        ),
        minimum_compression_observations=_integer(
            raw_quant.get("minimum_compression_observations"), 5
        ),
        require_compression=_boolean(
            raw_quant.get("require_compression"), False
        ),
        require_expansion_trigger=_boolean(
            raw_quant.get("require_expansion_trigger"), True
        ),
        require_momentum_expansion_trigger=_boolean(
            raw_quant.get("require_momentum_expansion_trigger"), True
        ),
        require_futures_flow=_boolean(
            raw_quant.get("require_futures_flow"), False
        ),
        require_expiry_day=_boolean(
            raw_quant.get("require_expiry_day"), False
        ),
        weights=weights,
    )

    raw_impulse = data.get("impulse")
    raw_impulse = raw_impulse if isinstance(raw_impulse, dict) else {}
    impulse = OptionChainImpulseSettings(
        window_seconds=_integer(raw_impulse.get("window_seconds"), 30),
        strike_depth=_integer(raw_impulse.get("strike_depth"), 2),
        minimum_legs_per_side=_integer(
            raw_impulse.get("minimum_legs_per_side"), 3
        ),
        minimum_basket_return_percent=_decimal(
            raw_impulse.get("minimum_basket_return_percent"), "0.80"
        ),
        maximum_opposite_return_percent=_decimal(
            raw_impulse.get("maximum_opposite_return_percent"), "-0.30"
        ),
        minimum_return_gap_percent=_decimal(
            raw_impulse.get("minimum_return_gap_percent"), "1.20"
        ),
        maximum_return_gap_percent=_decimal(
            raw_impulse.get("maximum_return_gap_percent"), "3.00"
        ),
        minimum_same_side_breadth=_decimal(
            raw_impulse.get("minimum_same_side_breadth"), "0.60"
        ),
        minimum_opposite_decay_breadth=_decimal(
            raw_impulse.get("minimum_opposite_decay_breadth"), "0.60"
        ),
        same_side_leg_return_percent=_decimal(
            raw_impulse.get("same_side_leg_return_percent"), "0.30"
        ),
        opposite_leg_decay_percent=_decimal(
            raw_impulse.get("opposite_leg_decay_percent"), "-0.20"
        ),
        minimum_residual_return_percent=_decimal(
            raw_impulse.get("minimum_residual_return_percent"), "0.10"
        ),
        minimum_residual_breadth=_decimal(
            raw_impulse.get("minimum_residual_breadth"), "0.60"
        ),
        minimum_volume_ratio=_decimal(
            raw_impulse.get("minimum_volume_ratio"), "0.75"
        ),
        maximum_basket_chase_percent=_decimal(
            raw_impulse.get("maximum_basket_chase_percent"), "2.50"
        ),
        maximum_average_spread_ratio=_decimal(
            raw_impulse.get("maximum_average_spread_ratio"), "0.025"
        ),
        aggregate_residual_over_window=_boolean(
            raw_impulse.get("aggregate_residual_over_window"), False
        ),
    )

    raw_smc = data.get("smc")
    raw_smc = raw_smc if isinstance(raw_smc, dict) else {}
    smc = SMCSettings(
        opening_range_minutes=_integer(
            raw_smc.get("opening_range_minutes"), 15
        ),
        swing_left_frames=_integer(raw_smc.get("swing_left_frames"), 3),
        swing_right_frames=_integer(raw_smc.get("swing_right_frames"), 3),
        structure_lookback_frames=_integer(
            raw_smc.get("structure_lookback_frames"), 12
        ),
        displacement_lookback_frames=_integer(
            raw_smc.get("displacement_lookback_frames"), 60
        ),
        maximum_active_levels_per_side=_integer(
            raw_smc.get("maximum_active_levels_per_side"), 8
        ),
        maximum_level_age_minutes=_integer(
            raw_smc.get("maximum_level_age_minutes"), 180
        ),
        minimum_sweep_points=_decimal(
            raw_smc.get("minimum_sweep_points"), "2"
        ),
        reclaim_buffer_points=_decimal(
            raw_smc.get("reclaim_buffer_points"), "0.5"
        ),
        structure_break_buffer_points=_decimal(
            raw_smc.get("structure_break_buffer_points"), "0.5"
        ),
        minimum_displacement_points=_decimal(
            raw_smc.get("minimum_displacement_points"), "4"
        ),
        displacement_multiplier=_decimal(
            raw_smc.get("displacement_multiplier"), "1.5"
        ),
        maximum_reclaim_seconds=_integer(
            raw_smc.get("maximum_reclaim_seconds"), 30
        ),
        maximum_structure_break_seconds=_integer(
            raw_smc.get("maximum_structure_break_seconds"), 60
        ),
        option_confirmation_ttl_seconds=_integer(
            raw_smc.get("option_confirmation_ttl_seconds"), 30
        ),
        event_cooldown_seconds=_integer(
            raw_smc.get("event_cooldown_seconds"), 120
        ),
        require_cross_strike_confirmation=_boolean(
            raw_smc.get("require_cross_strike_confirmation"), True
        ),
    )

    raw_micro = data.get("microstructure")
    raw_micro = raw_micro if isinstance(raw_micro, dict) else {}
    microstructure = QuantMicrostructureSettings(
        feature_window_seconds=_integer(
            raw_micro.get("feature_window_seconds"), 3
        ),
        feature_min_events=_integer(
            raw_micro.get("feature_min_events"), 3
        ),
        minimum_book_imbalance=_decimal(
            raw_micro.get("minimum_book_imbalance"), "0.25"
        ),
        minimum_price_velocity=_decimal(
            raw_micro.get("minimum_price_velocity"), "0.50"
        ),
        minimum_option_velocity_percent_per_second=_decimal(
            raw_micro.get(
                "minimum_option_velocity_percent_per_second"
            ),
            "0.15",
        ),
        maximum_spread_points=_decimal(
            raw_micro.get("maximum_spread_points"), "1.50"
        ),
        require_target_option_confirmation=_boolean(
            raw_micro.get("require_target_option_confirmation"), True
        ),
        require_futures_confirmation=_boolean(
            raw_micro.get("require_futures_confirmation"), False
        ),
        minimum_futures_confirmations=_integer(
            raw_micro.get("minimum_futures_confirmations"), 2
        ),
        minimum_option_confirmations=_integer(
            raw_micro.get("minimum_option_confirmations"), 2
        ),
        maximum_age_seconds=_integer(
            raw_micro.get("maximum_age_seconds"), 5
        ),
        minimum_confidence=_decimal(
            raw_micro.get("minimum_confidence"), "0.35"
        ),
        gate_minimum_directional_confirmations=(
            _integer(
                raw_micro.get("gate_minimum_directional_confirmations"),
                0,
            )
            if "gate_minimum_directional_confirmations" in raw_micro
            else None
        ),
        gate_minimum_independent_families=(
            _integer(
                raw_micro.get("gate_minimum_independent_families"),
                0,
            )
            if "gate_minimum_independent_families" in raw_micro
            else None
        ),
        gate_minimum_confirmations=(
            _integer(raw_micro.get("gate_minimum_confirmations"), 0)
            if "gate_minimum_confirmations" in raw_micro
            else None
        ),
        gamma_require_structural_room=_boolean(
            raw_micro.get("gamma_require_structural_room"), True
        ),
        event_driven_entry=_boolean(
            raw_micro.get("event_driven_entry"), False
        ),
        candidate_ttl_seconds=_integer(
            raw_micro.get("candidate_ttl_seconds"), 10
        ),
        minimum_candidate_premium_chase_percent=(
            _decimal(
                raw_micro.get("minimum_candidate_premium_chase_percent"),
                "0",
            )
            if "minimum_candidate_premium_chase_percent" in raw_micro
            else None
        ),
        maximum_candidate_premium_chase_percent=_decimal(
            raw_micro.get("maximum_candidate_premium_chase_percent"), "2"
        ),
        require_directional_option_book=_boolean(
            raw_micro.get("require_directional_option_book"), False
        ),
        event_entry_cutoff_time=(
            str(raw_micro["event_entry_cutoff_time"])
            if raw_micro.get("event_entry_cutoff_time") is not None
            else None
        ),
    )

    raw_execution = data.get("execution")
    raw_execution = raw_execution if isinstance(raw_execution, dict) else {}
    execution = QuantExecutionSettings(
        stop_percent=_decimal(raw_execution.get("stop_percent"), "5"),
        target_percent=_decimal(raw_execution.get("target_percent"), "10"),
        maximum_hold_minutes=_integer(
            raw_execution.get("maximum_hold_minutes"), 15
        ),
        cooldown_seconds=_integer(
            raw_execution.get("cooldown_seconds"), 900
        ),
        trailing_activation_percent=(
            Decimal(str(raw_execution["trailing_activation_percent"]))
            if raw_execution.get("trailing_activation_percent") is not None
            else None
        ),
        trailing_drawdown_percent=(
            Decimal(str(raw_execution["trailing_drawdown_percent"]))
            if raw_execution.get("trailing_drawdown_percent") is not None
            else None
        ),
        no_follow_through_seconds=(
            int(raw_execution["no_follow_through_seconds"])
            if raw_execution.get("no_follow_through_seconds") is not None
            else None
        ),
        minimum_follow_through_percent=(
            Decimal(str(raw_execution["minimum_follow_through_percent"]))
            if raw_execution.get("minimum_follow_through_percent") is not None
            else None
        ),
        event_driven_exit=_boolean(
            raw_execution.get("event_driven_exit"), False
        ),
        close_at_tape_end=_boolean(
            raw_execution.get("close_at_tape_end"), False
        ),
    )
    return StrategyProfile(
        name=name,
        description=str(data.get("description") or ""),
        strategies=strategies,
        features=features,
        quant=quant,
        impulse=impulse,
        smc=smc,
        microstructure=microstructure,
        execution=execution,
    )
