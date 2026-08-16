from __future__ import annotations

from dataclasses import replace
from itertools import combinations, permutations

from app.core.config import Settings
from app.domain.models import StrategyFamily, StrategyResolverPolicy


def parse_strategy_families(value: str) -> tuple[StrategyFamily, ...]:
    names = tuple(
        item.strip().upper()
        for item in value.split(",")
        if item.strip()
    )
    if not names:
        raise ValueError("at least one strategy family must be enabled")
    try:
        families = tuple(StrategyFamily(name) for name in names)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in StrategyFamily)
        raise ValueError(
            f"unknown strategy family; expected one of: {allowed}"
        ) from exc
    if len(families) != len(set(families)):
        raise ValueError("strategy family list contains duplicates")
    return families


def apply_strategy_overrides(
    settings: Settings,
    *,
    enabled_families: tuple[StrategyFamily, ...] | None = None,
    resolver_policy: StrategyResolverPolicy | str | None = None,
    priority_order: tuple[StrategyFamily, ...] | None = None,
) -> Settings:
    enabled = (
        set(enabled_families)
        if enabled_families is not None
        else {
            family
            for family, is_enabled in (
                (
                    StrategyFamily.LEVEL_REVERSAL,
                    settings.strategy_level_reversal_enabled,
                ),
                (
                    StrategyFamily.BREAKOUT_MOMENTUM,
                    settings.strategy_breakout_momentum_enabled,
                ),
                (
                    StrategyFamily.GAMMA_EXPANSION,
                    settings.strategy_gamma_expansion_enabled,
                ),
            )
            if is_enabled
        }
    )
    if not enabled:
        raise ValueError("at least one strategy family must be enabled")

    priorities = {
        StrategyFamily.LEVEL_REVERSAL: (
            settings.strategy_level_reversal_priority
        ),
        StrategyFamily.BREAKOUT_MOMENTUM: (
            settings.strategy_breakout_momentum_priority
        ),
        StrategyFamily.GAMMA_EXPANSION: (
            settings.strategy_gamma_expansion_priority
        ),
    }
    if priority_order is not None:
        if len(priority_order) != len(set(priority_order)):
            raise ValueError("strategy priority order contains duplicates")
        if set(priority_order) != enabled:
            raise ValueError(
                "strategy priority order must contain every enabled family "
                "exactly once"
            )
        for index, family in enumerate(priority_order, start=1):
            priorities[family] = index * 10

    policy = StrategyResolverPolicy(
        str(resolver_policy or settings.strategy_resolver_policy).upper()
    )
    return replace(
        settings,
        strategy_resolver_policy=policy.value,
        strategy_level_reversal_enabled=(
            StrategyFamily.LEVEL_REVERSAL in enabled
        ),
        strategy_breakout_momentum_enabled=(
            StrategyFamily.BREAKOUT_MOMENTUM in enabled
        ),
        strategy_gamma_expansion_enabled=(
            StrategyFamily.GAMMA_EXPANSION in enabled
        ),
        strategy_level_reversal_priority=priorities[
            StrategyFamily.LEVEL_REVERSAL
        ],
        strategy_breakout_momentum_priority=priorities[
            StrategyFamily.BREAKOUT_MOMENTUM
        ],
        strategy_gamma_expansion_priority=priorities[
            StrategyFamily.GAMMA_EXPANSION
        ],
    )


def enabled_strategy_names(settings: Settings) -> tuple[str, ...]:
    return tuple(
        family.value
        for family, enabled in (
            (
                StrategyFamily.LEVEL_REVERSAL,
                settings.strategy_level_reversal_enabled,
            ),
            (
                StrategyFamily.BREAKOUT_MOMENTUM,
                settings.strategy_breakout_momentum_enabled,
            ),
            (
                StrategyFamily.GAMMA_EXPANSION,
                settings.strategy_gamma_expansion_enabled,
            ),
        )
        if enabled
    )


def strategy_priority_names(settings: Settings) -> tuple[str, ...]:
    priorities = (
        (
            settings.strategy_level_reversal_priority,
            StrategyFamily.LEVEL_REVERSAL.value,
        ),
        (
            settings.strategy_breakout_momentum_priority,
            StrategyFamily.BREAKOUT_MOMENTUM.value,
        ),
        (
            settings.strategy_gamma_expansion_priority,
            StrategyFamily.GAMMA_EXPANSION.value,
        ),
    )
    enabled = set(enabled_strategy_names(settings))
    return tuple(
        name
        for _, name in sorted(priorities)
        if name in enabled
    )


def generate_strategy_matrix(
    base_settings: Settings,
    *,
    include_priority_permutations: bool,
) -> tuple[tuple[str, Settings], ...]:
    experiments: list[tuple[str, Settings]] = []
    # This matrix is retained only for legacy three-family comparisons.
    # Central strategy profiles own derivatives-quant experiments.
    families = (
        StrategyFamily.LEVEL_REVERSAL,
        StrategyFamily.BREAKOUT_MOMENTUM,
        StrategyFamily.GAMMA_EXPANSION,
    )

    for size in range(1, len(families) + 1):
        for enabled in combinations(families, size):
            label = "regime__" + "-".join(
                family.value.lower() for family in enabled
            )
            experiments.append(
                (
                    label,
                    apply_strategy_overrides(
                        base_settings,
                        enabled_families=enabled,
                        resolver_policy=(
                            StrategyResolverPolicy.REGIME_EXCLUSIVE
                        ),
                    ),
                )
            )

    if include_priority_permutations:
        for size in range(2, len(families) + 1):
            for enabled in combinations(families, size):
                for priority in permutations(enabled):
                    label = "priority__" + "-".join(
                        family.value.lower() for family in priority
                    )
                    experiments.append(
                        (
                            label,
                            apply_strategy_overrides(
                                base_settings,
                                enabled_families=enabled,
                                resolver_policy=(
                                    StrategyResolverPolicy.FIXED_PRIORITY
                                ),
                                priority_order=priority,
                            ),
                        )
                    )
    return tuple(experiments)
