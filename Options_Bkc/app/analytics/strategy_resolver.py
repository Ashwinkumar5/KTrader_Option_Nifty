from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.models import (
    MarketRegime,
    SignalSetup,
    StrategyCandidate,
    StrategyFamily,
    StrategyResolverPolicy,
)


@dataclass(frozen=True)
class StrategyFamilySettings:
    family: StrategyFamily
    enabled: bool
    priority: int

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise ValueError("strategy priority must be non-negative")


@dataclass(frozen=True)
class StrategyResolverSettings:
    policy: StrategyResolverPolicy
    families: tuple[StrategyFamilySettings, ...]

    def __post_init__(self) -> None:
        configured = [item.family for item in self.families]
        if len(configured) != len(set(configured)):
            raise ValueError("each strategy family may be configured only once")
        enabled_priorities = [
            item.priority for item in self.families if item.enabled
        ]
        if len(enabled_priorities) != len(set(enabled_priorities)):
            raise ValueError(
                "enabled strategy families must have unique priorities"
            )


@dataclass(frozen=True)
class StrategyResolution:
    selected: StrategyCandidate | None
    considered: tuple[StrategyCandidate, ...]
    rejected: tuple[str, ...]
    reason: str


class StrategyCandidateResolver:
    """Resolve independent candidates without strategy-order side effects."""

    def __init__(self, settings: StrategyResolverSettings) -> None:
        self._settings = settings
        self._family_settings = {
            item.family: item for item in settings.families
        }

    @property
    def policy(self) -> StrategyResolverPolicy:
        return self._settings.policy

    def resolve(
        self,
        *,
        candidates: tuple[StrategyCandidate, ...],
        regime: MarketRegime,
    ) -> StrategyResolution:
        considered: list[StrategyCandidate] = []
        rejected: list[str] = []

        for candidate in candidates:
            family_settings = self._family_settings.get(candidate.family)
            if family_settings is None:
                rejected.append(f"{candidate.family.value}:unconfigured")
                continue
            if not family_settings.enabled:
                rejected.append(f"{candidate.family.value}:disabled")
                continue
            if regime == MarketRegime.UNSTABLE_HIGH_VOL:
                rejected.append(
                    f"{candidate.family.value}:regime={regime.value}"
                )
                continue
            if (
                self._settings.policy
                == StrategyResolverPolicy.REGIME_EXCLUSIVE
                and not _regime_allows(candidate, regime)
            ):
                rejected.append(
                    f"{candidate.family.value}:regime={regime.value}"
                )
                continue
            considered.append(candidate)

        if not considered:
            return StrategyResolution(
                selected=None,
                considered=(),
                rejected=tuple(rejected),
                reason=(
                    "NO STRATEGY CANDIDATE: no enabled candidate passed "
                    f"{self._settings.policy.value} in regime {regime.value}"
                ),
            )

        sides = {candidate.side for candidate in considered}
        if (
            self._settings.policy
            in {
                StrategyResolverPolicy.CONFLICT_NO_TRADE,
                StrategyResolverPolicy.REGIME_EXCLUSIVE,
            }
            and len(sides) > 1
        ):
            return StrategyResolution(
                selected=None,
                considered=tuple(considered),
                rejected=tuple(rejected),
                reason=(
                    "STRATEGY CONFLICT: enabled regime-compatible candidates "
                    "oppose each other"
                ),
            )

        selected = min(
            considered,
            key=self._selection_key,
        )
        return StrategyResolution(
            selected=selected,
            considered=tuple(considered),
            rejected=tuple(rejected),
            reason=(
                f"RESOLVED {selected.family.value} via "
                f"{self._settings.policy.value}: {selected.reason}"
            ),
        )

    def _selection_key(
        self,
        candidate: StrategyCandidate,
    ) -> tuple[Decimal | int, int, str]:
        priority = self._family_settings[candidate.family].priority
        if self._settings.policy == StrategyResolverPolicy.HIGHEST_CONFIDENCE:
            return (-candidate.confidence, priority, candidate.family.value)
        return (priority, -int(candidate.confidence * 10000), candidate.family.value)


def _regime_allows(
    candidate: StrategyCandidate,
    regime: MarketRegime,
) -> bool:
    if regime == MarketRegime.UNSTABLE_HIGH_VOL:
        return False
    if candidate.setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL:
        return regime in {
            MarketRegime.RANGE,
            MarketRegime.TREND_BREAKOUT,
        }
    family = candidate.family
    if family in {
        StrategyFamily.DERIVATIVES_QUANT,
        StrategyFamily.OPTION_CHAIN_IMPULSE,
        StrategyFamily.SMC,
    }:
        return True
    return (
        family == StrategyFamily.LEVEL_REVERSAL
        and regime == MarketRegime.RANGE
        or family == StrategyFamily.BREAKOUT_MOMENTUM
        and regime == MarketRegime.TREND_BREAKOUT
        or family == StrategyFamily.GAMMA_EXPANSION
        and regime == MarketRegime.COMPRESSION
    )
