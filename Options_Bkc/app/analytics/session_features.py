from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace

from app.domain.models import (
    CandlePatternContext,
    ExpectedMoveContext,
    FuturesFlowContext,
    MomentumExhaustionContext,
    OpeningContext,
    OptionChainSnapshot,
    PremiumResponse,
)

from .expected_move import ExpectedMoveSettings, ExpectedMoveTracker
from .momentum_exhaustion import (
    MomentumExhaustionSettings,
    MomentumExhaustionTracker,
)
from .opening_context import OpeningContextSettings, OpeningContextTracker
from .premium_response import PremiumResponseSettings, PremiumResponseTracker
from .candle_patterns import CandlePatternSettings, CandlePatternTracker
from .futures_flow import FuturesFlowSettings, FuturesFlowTracker


@dataclass(frozen=True)
class FeatureModuleSettings:
    enabled: bool
    sequence: int

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("feature sequence must be non-negative")


@dataclass(frozen=True)
class SessionFeaturePipelineSettings:
    opening: FeatureModuleSettings = FeatureModuleSettings(True, 10)
    expected_move: FeatureModuleSettings = FeatureModuleSettings(True, 20)
    premium_response: FeatureModuleSettings = FeatureModuleSettings(True, 30)
    futures_flow: FeatureModuleSettings = FeatureModuleSettings(True, 35)
    candle_patterns: FeatureModuleSettings = FeatureModuleSettings(True, 37)
    momentum_exhaustion: FeatureModuleSettings = FeatureModuleSettings(True, 40)

    def __post_init__(self) -> None:
        enabled = {
            name: value
            for name, value in self.modules().items()
            if value.enabled
        }
        sequences = [value.sequence for value in enabled.values()]
        if len(sequences) != len(set(sequences)):
            raise ValueError("enabled feature modules require unique sequences")
        if "momentum_exhaustion" in enabled:
            required = {"expected_move", "premium_response"}
            missing = required - set(enabled)
            if missing:
                raise ValueError(
                    "momentum_exhaustion requires enabled modules: "
                    + ", ".join(sorted(missing))
                )
            exhaustion_sequence = enabled["momentum_exhaustion"].sequence
            if any(
                enabled[name].sequence >= exhaustion_sequence
                for name in required
            ):
                raise ValueError(
                    "momentum_exhaustion must run after its dependencies"
                )

    def modules(self) -> dict[str, FeatureModuleSettings]:
        return {
            "opening": self.opening,
            "expected_move": self.expected_move,
            "premium_response": self.premium_response,
            "futures_flow": self.futures_flow,
            "candle_patterns": self.candle_patterns,
            "momentum_exhaustion": self.momentum_exhaustion,
        }


@dataclass(frozen=True)
class SessionFeatures:
    opening: OpeningContext | None = None
    expected_move: ExpectedMoveContext | None = None
    premium_responses: tuple[PremiumResponse, ...] = ()
    futures_flow: FuturesFlowContext | None = None
    candle_pattern: CandlePatternContext | None = None
    momentum_exhaustion: MomentumExhaustionContext | None = None


class SessionFeaturePipeline:
    """Small fixed-stage registry with validated, configurable module order."""

    def __init__(
        self,
        settings: SessionFeaturePipelineSettings | None = None,
        *,
        opening_settings: OpeningContextSettings | None = None,
        expected_move_settings: ExpectedMoveSettings | None = None,
        premium_response_settings: PremiumResponseSettings | None = None,
        futures_flow_settings: FuturesFlowSettings | None = None,
        candle_pattern_settings: CandlePatternSettings | None = None,
        exhaustion_settings: MomentumExhaustionSettings | None = None,
    ) -> None:
        self._settings = settings or SessionFeaturePipelineSettings()
        self._opening = OpeningContextTracker(opening_settings)
        self._expected = ExpectedMoveTracker(expected_move_settings)
        self._premium = PremiumResponseTracker(premium_response_settings)
        self._futures = FuturesFlowTracker(futures_flow_settings)
        self._candles = CandlePatternTracker(candle_pattern_settings)
        self._exhaustion = MomentumExhaustionTracker(exhaustion_settings)
        self._ordered_modules = tuple(
            name
            for name, value in sorted(
                self._settings.modules().items(),
                key=lambda item: item[1].sequence,
            )
            if value.enabled
        )

    def update(self, snapshot: OptionChainSnapshot) -> SessionFeatures:
        previous_expected_move = None
        if "expected_move" in self._ordered_modules:
            previous_expected_move = self._expected.prepare(snapshot)
        feature_snapshot = snapshot
        if (
            previous_expected_move is not None
            and snapshot.market is not None
            and snapshot.market.previous_session_expected_move is None
        ):
            feature_snapshot = replace(
                snapshot,
                market=replace(
                    snapshot.market,
                    previous_session_expected_move=previous_expected_move,
                ),
            )
        opening = None
        expected = ExpectedMoveContext()
        responses: tuple[PremiumResponse, ...] = ()
        futures_flow = None
        candle_pattern = None
        exhaustion = None
        for module in self._ordered_modules:
            if module == "opening":
                opening = self._opening.update(feature_snapshot)
            elif module == "expected_move":
                expected = self._expected.update(feature_snapshot)
            elif module == "premium_response":
                responses = self._premium.update(feature_snapshot)
            elif module == "futures_flow":
                futures_flow = self._futures.update(feature_snapshot)
            elif module == "candle_patterns":
                candle_pattern = self._candles.update(feature_snapshot)
            elif module == "momentum_exhaustion":
                exhaustion = self._exhaustion.update(
                    snapshot=feature_snapshot,
                    expected_move=expected,
                    responses=responses,
                )
        return SessionFeatures(
            opening=opening,
            expected_move=expected if "expected_move" in self._ordered_modules else None,
            premium_responses=responses,
            futures_flow=futures_flow,
            candle_pattern=candle_pattern,
            momentum_exhaustion=exhaustion,
        )

    def reset(self) -> None:
        self._opening.reset()
        self._expected.reset()
        self._premium.reset()
        self._futures.reset()
        self._candles.reset()
