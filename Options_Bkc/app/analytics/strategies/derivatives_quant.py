from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.core.strategy_config import DerivativesQuantSettings
from app.domain.models import (
    EvidenceFamily,
    FuturesFlowContext,
    SignalSetup,
    StrategyCandidate,
    StrategyCheck,
    StrategyDiagnostic,
    StrategyEvidence,
    StrategyFamily,
)

from .base import OptionChainLeg, StrategyEvaluationContext


CALL = "BUY_CALL"
PUT = "BUY_PUT"
_FAILED_AUCTION_LOOKBACK_SECONDS = 300
_OPTION_POSITIONING_PRICE_THRESHOLD = Decimal("0.25")
_OPTION_POSITIONING_OI_THRESHOLD = Decimal("0.02")
_OPTION_POSITIONING_MINIMUM_LEGS = 4


@dataclass(frozen=True)
class _Observation:
    captured_at: datetime
    spot: Decimal
    pcr: Decimal | None
    straddle: Decimal | None
    atm_iv: Decimal | None
    call_iv: Decimal | None
    put_iv: Decimal | None
    call_mid: Decimal | None
    put_mid: Decimal | None
    call_volume: int
    put_volume: int
    call_oi: int
    put_oi: int
    india_vix: Decimal | None
    option_chain_legs: tuple[OptionChainLeg, ...]


@dataclass(frozen=True)
class _ScoreObservation:
    captured_at: datetime
    score: Decimal


@dataclass(frozen=True)
class _OptionPositioning:
    available: bool = False
    score: Decimal = Decimal("0")
    side: str | None = None
    strength: Decimal = Decimal("0")
    aligned_legs: int = 0
    observed_legs: int = 0
    reason: str = "cross-strike option positioning is unavailable"


class DerivativesQuantStrategy:
    """One causal derivatives-flow strategy with auditable sequential gates."""

    family = StrategyFamily.DERIVATIVES_QUANT

    _DIRECTION_FEATURES = {
        "index_momentum": "atr_normalization",
        "option_premium_momentum": "premium_response",
        "option_volume_flow": "volume_oi",
        "iv_skew": "iv_skew",
        "oi_migration": "volume_oi",
        "pcr_context": ("consolidated_pcr", "strike_pcr"),
        "futures_flow": "futures_flow",
        "futures_basis": "futures_basis",
    }

    def __init__(
        self,
        settings: DerivativesQuantSettings,
        enabled_features: frozenset[str] | None = None,
    ) -> None:
        self._settings = settings
        # ``None`` preserves the historical direct-construction behavior used
        # by unit tests and legacy callers. A profile supplies an explicit set,
        # including an empty set for a true no-feature research control.
        self._enabled_features = enabled_features
        self._history: dict[str, deque[_Observation]] = {}
        self._direction_scores: dict[str, deque[Decimal]] = {}
        self._recent_direction_scores: dict[
            str, deque[_ScoreObservation]
        ] = {}
        self._session_dates: dict[str, date] = {}
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="NO_CANDIDATE",
            reason="strategy has not received a synchronized frame",
        )

    @property
    def last_diagnostic(self) -> StrategyDiagnostic:
        return self._last_diagnostic

    def reset(self) -> None:
        self._history.clear()
        self._direction_scores.clear()
        self._recent_direction_scores.clear()
        self._session_dates.clear()

    def evaluate(
        self,
        context: StrategyEvaluationContext,
    ) -> tuple[StrategyCandidate, ...]:
        history = self._update_history(context)
        baseline = _baseline(
            history,
            context.captured_at
            - timedelta(seconds=self._settings.direction_window_seconds),
        )
        compression_baseline = _baseline(
            history,
            context.captured_at
            - timedelta(seconds=self._settings.compression_window_seconds),
        )
        current = history[-1]

        direction_inputs, horizon_scores, option_positioning = (
            self._direction_inputs(
                context=context,
                history=history,
            )
        )
        direction_score = sum(
            direction_inputs.values(),
            Decimal("0"),
        )
        direction_score = _clamp(
            direction_score,
            Decimal("-1"),
            Decimal("1"),
        )
        activation_threshold = self._direction_activation_threshold(
            context.underlying
        )
        side = CALL if direction_score > 0 else PUT
        proposed_strategy_side = side if direction_score != 0 else None
        side_sign = Decimal("1") if side == CALL else Decimal("-1")
        aligned = {
            name: value
            for name, value in direction_inputs.items()
            if value * side_sign >= Decimal("0.015")
        }
        opposing = {
            name: value
            for name, value in direction_inputs.items()
            if value * side_sign <= Decimal("-0.015")
        }
        horizon_agreement = sum(
            1
            for value in horizon_scores.values()
            if value * side_sign >= Decimal("0.15")
        )

        compression_ready, observed_range = self._compression_ready(
            history=history,
            baseline=compression_baseline,
            context=context,
        )
        straddle_expansion = (
            _percent_change(current.straddle, baseline.straddle)
            if self._feature_enabled("straddle_expansion")
            else Decimal("0")
        )
        current_iv = _average_positive(
            context.atm_call_iv,
            context.atm_put_iv,
        )
        iv_expansion = (
            _percent_change(current_iv, baseline.atm_iv)
            if self._feature_enabled("iv_surface")
            else Decimal("0")
        )
        gamma_matches = (
            self._feature_enabled("gamma_concentration")
            and context.gamma_signal == side
        )
        if self._feature_enabled("premium_response"):
            matching_leg_impulse, matching_leg_impulse_z = _latest_leg_impulse(
                history,
                side=side,
                zscore_clip=self._settings.zscore_clip,
            )
        else:
            matching_leg_impulse = Decimal("0")
            matching_leg_impulse_z = Decimal("0")
        momentum_expansion_triggered = (
            gamma_matches
            or matching_leg_impulse_z
            >= self._settings.minimum_leg_impulse_zscore
            or straddle_expansion
            >= self._settings.minimum_straddle_expansion_percent
        )
        iv_expansion_triggered = (
            iv_expansion >= self._settings.minimum_iv_expansion_percent
        )
        # A pure IV expansion signals that premium is getting more expensive
        # without confirming that the underlying or its options are already
        # moving. Requiring a momentum-based trigger (gamma alignment, leg
        # impulse, or straddle expansion) avoids buying long premium into an
        # IV spike that later reverses as vega unwinds.
        expansion_triggered = (
            momentum_expansion_triggered
            or (
                not self._settings.require_momentum_expansion_trigger
                and iv_expansion_triggered
            )
        )

        leg_now = (
            context.atm_call_mid if side == CALL else context.atm_put_mid
        )
        leg_then = baseline.call_mid if side == CALL else baseline.put_mid
        leg_chase = _percent_change(leg_now, leg_then)
        spread_ratio = _matching_spread_ratio(context, side)
        liquidity_score = (
            Decimal("1")
            if spread_ratio is not None
            and spread_ratio <= Decimal("0.02")
            else Decimal("0.50")
            if spread_ratio is not None
            and spread_ratio <= Decimal("0.04")
            else Decimal("0")
        )
        iv_cost_score = (
            _clamp(
                Decimal("1")
                - context.intraday_iv_rank
                / max(self._settings.maximum_iv_rank, Decimal("1")),
                Decimal("0"),
                Decimal("1"),
            )
            if self._feature_enabled("iv_surface")
            else Decimal("0")
        )
        vix_regime_score = (
            _vix_buyability_score(context.india_vix)
            if self._feature_enabled("india_vix_regime")
            else Decimal("0")
        )
        volatility_context_scores = [
            score
            for enabled, score in (
                (self._feature_enabled("iv_surface"), iv_cost_score),
                (
                    self._feature_enabled("india_vix_regime"),
                    vix_regime_score,
                ),
            )
            if enabled
        ]
        volatility_context_score = _average(
            volatility_context_scores
        )
        expected_move_score = (
            _expected_move_buyability_score(context)
            if self._feature_enabled("expected_move")
            else Decimal("0")
        )
        setup_context_scores = [
            Decimal("1") if compression_ready else Decimal("0")
        ]
        if self._feature_enabled("expected_move"):
            setup_context_scores.append(expected_move_score)
        setup_context_score = _average(setup_context_scores)
        raw_buyability_score = (
            Decimal("0.15")
            * _scaled_positive(
                straddle_expansion,
                self._settings.minimum_straddle_expansion_percent,
            )
            + Decimal("0.15")
            * _scaled_positive(
                iv_expansion,
                self._settings.minimum_iv_expansion_percent,
            )
            + Decimal("0.15") * (Decimal("1") if gamma_matches else Decimal("0"))
            + Decimal("0.20")
            * _scaled_positive(
                matching_leg_impulse_z,
                self._settings.minimum_leg_impulse_zscore,
            )
            + Decimal("0.15") * liquidity_score
            + Decimal("0.10") * volatility_context_score
            + Decimal("0.10") * setup_context_score
        ).quantize(Decimal("0.0001"))
        buyability_capacity = self._buyability_capacity()
        buyability_score = _clamp(
            raw_buyability_score / buyability_capacity,
            Decimal("0"),
            Decimal("1"),
        ).quantize(Decimal("0.0001"))
        option_chain_families = {
            "option_premium_momentum",
            "option_volume_flow",
            "iv_skew",
            "oi_migration",
            "pcr_context",
        }
        aligned_option_chain_families = sum(
            name in option_chain_families for name in aligned
        )
        early_persistence, early_accelerating = (
            self._early_direction_persistence(
                underlying=context.underlying,
                side_sign=side_sign,
                current_score=direction_score,
            )
        )
        futures_side, futures_strength, futures_reason = (
            _effective_futures_signal(context.futures_flow)
        )
        futures_ready = futures_side == side and futures_strength > 0
        auction_stable, auction_reason = self._auction_stability(
            underlying=context.underlying,
            captured_at=context.captured_at,
            side_sign=side_sign,
        )
        strong_direction_ready = (
            abs(direction_score) >= activation_threshold
        )
        early_direction_ready = (
            abs(direction_score) >= self._settings.early_direction_score
            and horizon_agreement
            >= self._settings.early_min_horizon_agreement
            and len(aligned)
            >= self._settings.early_min_independent_families
            and aligned_option_chain_families
            >= self._settings.early_min_option_chain_families
            and buyability_score
            >= self._settings.early_min_buyability_score
            and leg_chase
            <= self._settings.early_max_leg_chase_percent
            and early_persistence
            >= self._settings.early_score_persistence_frames
            and (
                early_accelerating
                or not self._settings.require_early_acceleration
            )
            and (
                expansion_triggered
                or not self._settings.require_expansion_trigger
            )
            and auction_stable
        )
        activation_mode = (
            "STRONG_SCORE"
            if strong_direction_ready
            else "EARLY_QUANT_FLOW"
            if early_direction_ready
            else "WAIT"
        )
        feature_names = {
            "index_momentum": "index_momentum",
            "option_premium_momentum": "premium_momentum",
            "option_volume_flow": "volume_flow",
            "iv_skew": "iv_skew",
            "oi_migration": "oi_migration",
            "pcr_context": "pcr_context",
            "futures_flow": "futures_flow",
            "futures_basis": "futures_basis",
        }
        feature_checks = tuple(
            StrategyCheck(
                code=feature_names[name],
                passed=value * side_sign >= Decimal("0.015"),
                observed=(
                    f"contribution={value:+.4f}; "
                    f"signed_contribution={value * side_sign:+.4f}; "
                    "proposed_side="
                    + (
                        CALL
                        if value > 0
                        else PUT
                        if value < 0
                        else "NEUTRAL"
                    )
                ),
                required="signed contribution >= +0.0150",
                proposed_side=(
                    CALL
                    if value > 0
                    else PUT
                    if value < 0
                    else None
                ),
            )
            for name, value in direction_inputs.items()
        ) + (
            StrategyCheck(
                "cross_strike_positioning",
                option_positioning.side == side,
                option_positioning.reason,
                "same-side cross-strike price/OI positioning",
                proposed_side=option_positioning.side,
            ),
            StrategyCheck(
                "straddle_expansion",
                straddle_expansion
                >= self._settings.minimum_straddle_expansion_percent,
                f"change={straddle_expansion:+.4f}%",
                (
                    f">= "
                    f"{self._settings.minimum_straddle_expansion_percent}%"
                ),
            ),
            StrategyCheck(
                "iv_expansion",
                iv_expansion >= self._settings.minimum_iv_expansion_percent,
                f"change={iv_expansion:+.4f}%",
                f">= {self._settings.minimum_iv_expansion_percent}%",
            ),
            StrategyCheck(
                "leg_impulse",
                matching_leg_impulse_z
                >= self._settings.minimum_leg_impulse_zscore,
                (
                    f"change={matching_leg_impulse:+.4f}%; "
                    f"zscore={matching_leg_impulse_z:+.4f}"
                ),
                f"zscore >= {self._settings.minimum_leg_impulse_zscore}",
            ),
            StrategyCheck(
                "gamma_expansion",
                gamma_matches,
                (
                    f"observed={context.gamma_signal or 'NONE'}; "
                    f"proposed_side={side}"
                ),
                "gamma signal matches proposed side",
            ),
            StrategyCheck(
                "option_liquidity",
                liquidity_score > 0,
                (
                    f"spread_ratio={spread_ratio}; "
                    f"liquidity_score={liquidity_score}"
                ),
                "positive synchronized option-liquidity score",
            ),
            StrategyCheck(
                "expected_move_context",
                (
                    not self._feature_enabled("expected_move")
                    or expected_move_score > 0
                ),
                f"buyability_score={expected_move_score:.4f}",
                "positive expected-move utilization score when enabled",
            ),
            StrategyCheck(
                "india_vix_regime",
                (
                    not self._feature_enabled("india_vix_regime")
                    or vix_regime_score > 0
                ),
                (
                    f"india_vix={context.india_vix}; "
                    f"buyability_score={vix_regime_score:.4f}"
                ),
                "positive India-VIX option-buying regime score when enabled",
            ),
        )
        checks = (
            StrategyCheck(
                "expiry_day",
                (
                    context.is_expiry_day
                    or not self._settings.require_expiry_day
                ),
                f"is_expiry_day={context.is_expiry_day}",
                (
                    "selected option contract expires today"
                    if self._settings.require_expiry_day
                    else "expiry-day restriction is disabled"
                ),
            ),
            StrategyCheck(
                "direction_score",
                strong_direction_ready or early_direction_ready,
                (
                    f"score={direction_score:+.4f}; "
                    f"causal_threshold={activation_threshold:.4f}; "
                    f"activation={activation_mode}; "
                    f"early_persistence={early_persistence}; "
                    f"early_accelerating={early_accelerating}; "
                    f"early_option_families="
                    f"{aligned_option_chain_families}; "
                    + ", ".join(
                        f"{name}={value:+.4f}"
                        for name, value in direction_inputs.items()
                    )
                ),
                (
                    "strong score or symmetric early quantitative "
                    "persistence/consensus gate"
                ),
            ),
            StrategyCheck(
                "failed_auction_stability",
                auction_stable,
                auction_reason,
                "no recent opposite quantitative impulse",
            ),
            StrategyCheck(
                "multi_horizon_agreement",
                horizon_agreement
                >= self._settings.minimum_horizon_agreement,
                (
                    f"aligned={horizon_agreement}; "
                    + ", ".join(
                        f"{horizon}s={score:+.3f}"
                        for horizon, score in horizon_scores.items()
                    )
                ),
                (
                    f">= {self._settings.minimum_horizon_agreement} "
                    "statistically aligned horizons"
                ),
            ),
            StrategyCheck(
                "independent_families",
                len(aligned)
                >= self._settings.minimum_independent_families,
                f"aligned={','.join(aligned) or 'none'}",
                (
                    f">= {self._settings.minimum_independent_families} "
                    "aligned quantitative families"
                ),
            ),
            StrategyCheck(
                "compression_readiness",
                compression_ready or not self._settings.require_compression,
                (
                    f"ready={compression_ready}; range={observed_range}; "
                    f"observations={len(history)}"
                ),
                (
                    "prior compression required"
                    if self._settings.require_compression
                    else "compression contributes score but is optional"
                ),
            ),
            StrategyCheck(
                "convexity_expansion",
                expansion_triggered
                or not self._settings.require_expansion_trigger,
                (
                    f"straddle={straddle_expansion:+.3f}%; "
                    f"iv={iv_expansion:+.3f}%; "
                    f"leg_impulse={matching_leg_impulse:+.3f}%; "
                    f"leg_z={matching_leg_impulse_z:+.3f}; "
                    f"gamma_match={gamma_matches}"
                ),
                "matching-leg, gamma, straddle, or IV expansion trigger",
            ),
            StrategyCheck(
                "premium_not_chased",
                leg_chase <= self._settings.maximum_leg_chase_percent,
                f"matching ATM leg change={leg_chase:+.3f}%",
                f"<= {self._settings.maximum_leg_chase_percent}% before entry",
            ),
            StrategyCheck(
                "volatility_cost",
                (
                    not self._feature_enabled("iv_surface")
                    or context.intraday_iv_rank
                    <= self._settings.maximum_iv_rank
                ),
                f"intraday IV rank={context.intraday_iv_rank}",
                f"<= {self._settings.maximum_iv_rank}",
            ),
            StrategyCheck(
                "futures_flow",
                futures_ready or not self._settings.require_futures_flow,
                (
                    futures_reason
                ),
                (
                    "same-side futures flow required"
                    if self._settings.require_futures_flow
                    else "same-side futures flow increases direction score"
                ),
            ),
            StrategyCheck(
                "buyability_score",
                buyability_score
                >= self._settings.minimum_buyability_score,
                f"score={buyability_score}",
                f">= {self._settings.minimum_buyability_score}",
            ),
        )
        self._direction_scores.setdefault(
            context.underlying.upper(),
            deque(maxlen=2048),
        ).append(direction_score)
        recent_scores = self._recent_direction_scores.setdefault(
            context.underlying.upper(),
            deque(maxlen=256),
        )
        recent_scores.append(
            _ScoreObservation(context.captured_at, direction_score)
        )
        recent_cutoff = context.captured_at - timedelta(
            seconds=_FAILED_AUCTION_LOOKBACK_SECONDS
        )
        while (
            len(recent_scores) > 1
            and recent_scores[1].captured_at <= recent_cutoff
        ):
            recent_scores.popleft()
        failed = tuple(check for check in checks if not check.passed)
        if failed:
            self._last_diagnostic = StrategyDiagnostic(
                family=self.family,
                status="NO_CANDIDATE",
                reason=(
                    "DERIVATIVES_QUANT waiting: "
                    + ", ".join(check.code for check in failed)
                ),
                checks=checks,
                feature_checks=feature_checks,
                proposed_side=proposed_strategy_side,
            )
            return ()

        forecast_move = self._forecast_move(
            context=context,
            history=history,
            direction_score=direction_score,
        )
        forecast_iv_change = max(
            Decimal("0"),
            (current_iv or Decimal("0"))
            - (baseline.atm_iv or current_iv or Decimal("0")),
        )
        confidence = min(
            Decimal("0.95"),
            Decimal("0.45")
            + abs(direction_score) * Decimal("0.35")
            + buyability_score * Decimal("0.20"),
        ).quantize(Decimal("0.0001"))
        evidence = self._evidence(
            side=side,
            direction_inputs=direction_inputs,
            aligned=aligned,
            horizon_agreement=horizon_agreement,
            compression_ready=compression_ready,
            gamma_matches=gamma_matches,
            expansion_triggered=expansion_triggered,
            option_positioning=option_positioning,
        )
        reason = (
            f"DERIVATIVES QUANT {side}: direction={direction_score:+.4f}, "
            f"activation={activation_mode}, "
            f"buyability={buyability_score:.4f}, aligned="
            f"{','.join(aligned)}, opposing={','.join(opposing) or 'none'}, "
            f"straddle_expansion={straddle_expansion:+.3f}%, "
            f"iv_expansion={iv_expansion:+.3f}%, "
            f"leg_impulse={matching_leg_impulse:+.3f}% "
            f"(z={matching_leg_impulse_z:+.3f}), "
            f"horizons={horizon_agreement}/"
            f"{len(self._settings.direction_horizons_seconds)}, "
            f"option_positioning={option_positioning.score:+.3f}, "
            f"leg_chase={leg_chase:+.3f}%."
        )
        self._last_diagnostic = StrategyDiagnostic(
            family=self.family,
            status="CANDIDATE",
            reason=reason,
            checks=checks,
            feature_checks=feature_checks,
            proposed_side=side,
        )
        return (
            StrategyCandidate(
                family=self.family,
                side=side,
                setup_type=SignalSetup.DERIVATIVES_QUANT,
                reason=reason,
                confidence=confidence,
                evidence=evidence,
                direction_score=direction_score,
                buyability_score=buyability_score,
                forecast_underlying_move=forecast_move,
                forecast_iv_change=forecast_iv_change,
            ),
        )

    def _direction_activation_threshold(self, underlying: str) -> Decimal:
        scores = self._direction_scores.get(underlying.upper(), ())
        if (
            len(scores)
            < self._settings.direction_activation_min_observations
        ):
            return self._settings.warmup_direction_score
        rolling_quantile = _quantile_decimal(
            tuple(abs(score) for score in scores),
            self._settings.direction_activation_quantile,
        )
        return max(
            self._settings.minimum_direction_score,
            rolling_quantile,
        ).quantize(Decimal("0.0001"))

    def _early_direction_persistence(
        self,
        *,
        underlying: str,
        side_sign: Decimal,
        current_score: Decimal,
    ) -> tuple[int, bool]:
        scores = self._direction_scores.get(underlying.upper(), ())
        persistence = 1
        minimum_prior_score = (
            self._settings.early_direction_score * Decimal("0.75")
        )
        for score in reversed(scores):
            if score * side_sign < minimum_prior_score:
                break
            persistence += 1
            if persistence >= self._settings.early_score_persistence_frames:
                break
        accelerating = bool(
            scores
            and scores[-1] * side_sign > 0
            and abs(current_score) >= abs(scores[-1])
        )
        return persistence, accelerating

    def _auction_stability(
        self,
        *,
        underlying: str,
        captured_at: datetime,
        side_sign: Decimal,
    ) -> tuple[bool, str]:
        cutoff = captured_at - timedelta(
            seconds=_FAILED_AUCTION_LOOKBACK_SECONDS
        )
        opposite_threshold = (
            self._settings.early_direction_score * Decimal("0.75")
        )
        recent_opposite = tuple(
            item
            for item in self._recent_direction_scores.get(
                underlying.upper(), ()
            )
            if item.captured_at >= cutoff
            and item.score * side_sign <= -opposite_threshold
        )
        if not recent_opposite:
            return True, "no recent opposite quantitative impulse"
        latest = recent_opposite[-1]
        age_seconds = max(
            0,
            int((captured_at - latest.captured_at).total_seconds()),
        )
        return (
            False,
            "recent opposite quantitative impulse indicates an unresolved "
            f"failed auction ({age_seconds}s ago, score={latest.score:+.4f})",
        )

    def _update_history(
        self,
        context: StrategyEvaluationContext,
    ) -> deque[_Observation]:
        key = context.underlying.upper()
        session_date = context.captured_at.date()
        history = self._history.setdefault(key, deque())
        if (
            self._session_dates.get(key) != session_date
            or (
                history
                and context.captured_at <= history[-1].captured_at
            )
        ):
            history.clear()
            self._direction_scores.pop(key, None)
            self._recent_direction_scores.pop(key, None)
            self._session_dates[key] = session_date
        current_iv = (
            _average_positive(context.atm_call_iv, context.atm_put_iv)
            if (
                self._feature_enabled("iv_surface")
                or self._feature_enabled("iv_skew")
            )
            else None
        )
        pcr_values = tuple(
            value
            for enabled, value in (
                (
                    self._feature_enabled("strike_pcr"),
                    context.active_pcr,
                ),
                (
                    self._feature_enabled("consolidated_pcr"),
                    context.pcr_oi,
                ),
            )
            if enabled and value is not None and value > 0
        )
        history.append(
            _Observation(
                captured_at=context.captured_at,
                spot=context.spot,
                pcr=_average(list(pcr_values)) if pcr_values else None,
                straddle=(
                    context.atm_straddle_price
                    if self._feature_enabled("straddle_expansion")
                    else None
                ),
                atm_iv=current_iv,
                call_iv=(
                    context.atm_call_iv
                    if self._feature_enabled("iv_skew")
                    else None
                ),
                put_iv=(
                    context.atm_put_iv
                    if self._feature_enabled("iv_skew")
                    else None
                ),
                call_mid=(
                    context.atm_call_mid
                    if (
                        self._feature_enabled("premium_response")
                        or self._feature_enabled("volume_oi")
                    )
                    else None
                ),
                put_mid=(
                    context.atm_put_mid
                    if (
                        self._feature_enabled("premium_response")
                        or self._feature_enabled("volume_oi")
                    )
                    else None
                ),
                call_volume=(
                    context.call_volume
                    if self._feature_enabled("volume_oi")
                    else 0
                ),
                put_volume=(
                    context.put_volume
                    if self._feature_enabled("volume_oi")
                    else 0
                ),
                call_oi=(
                    context.call_oi
                    if self._feature_enabled("volume_oi")
                    else 0
                ),
                put_oi=(
                    context.put_oi
                    if self._feature_enabled("volume_oi")
                    else 0
                ),
                india_vix=(
                    context.india_vix
                    if self._feature_enabled("india_vix_regime")
                    else None
                ),
                option_chain_legs=(
                    context.option_chain_legs
                    if self._feature_enabled("volume_oi")
                    else ()
                ),
            )
        )
        cutoff = context.captured_at - timedelta(
            seconds=max(
                self._settings.normalization_window_seconds,
                self._settings.compression_window_seconds,
                self._settings.direction_window_seconds,
                max(self._settings.direction_horizons_seconds),
            )
        )
        while len(history) > 1 and history[1].captured_at <= cutoff:
            history.popleft()
        return history

    def _direction_inputs(
        self,
        *,
        context: StrategyEvaluationContext,
        history: deque[_Observation],
    ) -> tuple[
        dict[str, Decimal],
        dict[int, Decimal],
        _OptionPositioning,
    ]:
        weights = self._normalized_weights()
        current = history[-1]
        prior_history = tuple(history)[:-1]
        typical_seconds = _typical_step_seconds(history)
        index_step_scale = _robust_scale(
            tuple(
                right.spot - left.spot
                for left, right in _pairs(prior_history)
            ),
            fallback=_market_move_scale(context, typical_seconds),
        )
        premium_step_scale = _robust_scale(
            tuple(
                _relative_option_move(right, left)
                for left, right in _pairs(prior_history)
            ),
            fallback=Decimal("0.50"),
        )
        iv_step_scale = _robust_scale(
            tuple(
                _relative_iv_move(right, left)
                for left, right in _pairs(prior_history)
            ),
            fallback=Decimal("0.20"),
        )
        pcr_step_scale = _robust_scale(
            tuple(
                (
                    right.pcr - left.pcr
                    if right.pcr is not None and left.pcr is not None
                    else Decimal("0")
                )
                for left, right in _pairs(prior_history)
            ),
            fallback=Decimal("0.02"),
        )
        feature_values: dict[str, list[Decimal]] = {
            "index_momentum": [],
            "option_premium_momentum": [],
            "option_volume_flow": [],
            "iv_skew": [],
            "oi_migration": [],
            "pcr_context": [],
        }
        option_positioning_by_horizon = {
            horizon: _option_positioning(
                current=current,
                baseline=_baseline(
                    history,
                    context.captured_at - timedelta(seconds=horizon),
                ),
            )
            for horizon in self._settings.direction_horizons_seconds
        }
        horizon_scores: dict[int, Decimal] = {}
        for horizon in self._settings.direction_horizons_seconds:
            baseline = _baseline(
                history,
                context.captured_at - timedelta(seconds=horizon),
            )
            elapsed = max(
                1,
                int(
                    (
                        context.captured_at - baseline.captured_at
                    ).total_seconds()
                ),
            )
            scale_multiplier = Decimal(
                str((elapsed / max(1, typical_seconds)) ** 0.5)
            )
            index_value = _normalized_zscore(
                current.spot - baseline.spot,
                index_step_scale * scale_multiplier,
                self._settings.zscore_clip,
            )
            premium_value = _normalized_zscore(
                _relative_option_move(current, baseline),
                premium_step_scale * scale_multiplier,
                self._settings.zscore_clip,
            )
            iv_value = _normalized_zscore(
                _relative_iv_move(current, baseline),
                iv_step_scale * scale_multiplier,
                self._settings.zscore_clip,
            )
            call_volume_delta = max(
                0,
                current.call_volume - baseline.call_volume,
            )
            put_volume_delta = max(
                0,
                current.put_volume - baseline.put_volume,
            )
            volume_total = call_volume_delta + put_volume_delta
            volume_value = (
                Decimal(call_volume_delta - put_volume_delta)
                / Decimal(volume_total)
                if volume_total > 0
                else Decimal("0")
            )
            call_oi_delta = current.call_oi - baseline.call_oi
            put_oi_delta = current.put_oi - baseline.put_oi
            oi_total = abs(call_oi_delta) + abs(put_oi_delta)
            call_return = _percent_change(
                current.call_mid,
                baseline.call_mid,
            )
            put_return = _percent_change(
                current.put_mid,
                baseline.put_mid,
            )
            oi_value = (
                (
                    Decimal(call_oi_delta)
                    * _clamp(
                        call_return / Decimal("2"),
                        Decimal("-1"),
                        Decimal("1"),
                    )
                    - Decimal(put_oi_delta)
                    * _clamp(
                        put_return / Decimal("2"),
                        Decimal("-1"),
                        Decimal("1"),
                    )
                )
                / Decimal(oi_total)
                if oi_total > 0
                else Decimal("0")
            )
            cross_strike_positioning = option_positioning_by_horizon[horizon]
            if cross_strike_positioning.available:
                oi_value = cross_strike_positioning.score
            pcr_change = (
                current.pcr - baseline.pcr
                if current.pcr is not None and baseline.pcr is not None
                else Decimal("0")
            )
            pcr_value = _normalized_zscore(
                pcr_change,
                pcr_step_scale * scale_multiplier,
                self._settings.zscore_clip,
            )
            feature_values["index_momentum"].append(index_value)
            feature_values["option_premium_momentum"].append(
                premium_value
            )
            feature_values["option_volume_flow"].append(volume_value)
            feature_values["iv_skew"].append(iv_value)
            feature_values["oi_migration"].append(oi_value)
            feature_values["pcr_context"].append(pcr_value)
            horizon_scores[horizon] = (
                index_value * Decimal("0.40")
                + premium_value * Decimal("0.40")
                + iv_value * Decimal("0.20")
            ).quantize(Decimal("0.0001"))

        inputs = {
            name: weights.get(name, Decimal("0"))
            * _average(values)
            for name, values in feature_values.items()
        }
        flow = context.futures_flow
        futures_side, futures_strength, _ = _effective_futures_signal(flow)
        futures_value = Decimal("0")
        if futures_side in {CALL, PUT}:
            futures_value = (
                futures_strength if futures_side == CALL else -futures_strength
            )
        inputs["futures_flow"] = (
            weights.get("futures_flow", Decimal("0")) * futures_value
        )
        futures_horizon_values = _futures_horizon_values(flow)

        basis_change = (
            flow.basis_change
            if flow is not None and flow.basis_change is not None
            else Decimal("0")
        )
        inputs["futures_basis"] = weights.get(
            "futures_basis", Decimal("0")
        ) * _clamp(
            basis_change / Decimal("5"),
            Decimal("-1"),
            Decimal("1"),
        )
        basis_value = _clamp(
            basis_change / Decimal("5"),
            Decimal("-1"),
            Decimal("1"),
        )
        all_direction_inputs_enabled = all(
            self._direction_input_enabled(name)
            for name in self._DIRECTION_FEATURES
        )
        if all_direction_inputs_enabled and self._direction_input_enabled(
            "futures_flow"
        ):
            futures_weight = weights.get("futures_flow", Decimal("0"))
            for horizon in self._settings.direction_horizons_seconds:
                horizon_scores[horizon] = _clamp(
                    horizon_scores[horizon]
                    + futures_weight
                    * futures_horizon_values.get(horizon, futures_value),
                    Decimal("-1"),
                    Decimal("1"),
                ).quantize(Decimal("0.0001"))
        elif not all_direction_inputs_enabled:
            for index, horizon in enumerate(
                self._settings.direction_horizons_seconds
            ):
                horizon_values = [
                    values[index]
                    for name, values in feature_values.items()
                    if self._direction_input_enabled(name)
                ]
                if self._direction_input_enabled("futures_flow"):
                    horizon_values.append(
                        futures_horizon_values.get(horizon, futures_value)
                    )
                if self._direction_input_enabled("futures_basis"):
                    horizon_values.append(basis_value)
                horizon_scores[horizon] = (
                    _average(horizon_values)
                    if horizon_values
                    else Decimal("0")
                ).quantize(Decimal("0.0001"))

        for input_name in self._DIRECTION_FEATURES:
            if not self._direction_input_enabled(input_name):
                inputs[input_name] = Decimal("0")
        return (
            inputs,
            horizon_scores,
            _aggregate_option_positioning(
                option_positioning_by_horizon,
                self._settings.direction_horizons_seconds,
            ),
        )

    def _direction_input_enabled(self, name: str) -> bool:
        if self._settings.weights.get(name, Decimal("0")) <= 0:
            return False
        feature_name = self._DIRECTION_FEATURES[name]
        if isinstance(feature_name, tuple):
            return any(
                self._feature_enabled(item) for item in feature_name
            )
        return self._feature_enabled(feature_name)

    def _normalized_weights(self) -> dict[str, Decimal]:
        """Renormalize enabled weights so the direction score sums to one.

        Configured weights express relative feature importance and can sum to
        less than one (for example 0.96 for the active profile). Without this
        normalization the clamped score never reaches its full range, which
        biases the quantile activation threshold and makes the score an
        inconsistent probability-like measure across profiles. Only weights of
        enabled inputs are included so single-feature research controls keep an
        effective weight of exactly one.
        """

        enabled_total = sum(
            self._settings.weights.get(name, Decimal("0"))
            for name in self._DIRECTION_FEATURES
            if self._direction_input_enabled(name)
        )
        if enabled_total <= 0:
            return dict(self._settings.weights)
        return {
            name: (
                value / enabled_total
                if value > 0
                else Decimal("0")
            ).quantize(Decimal("0.000001"))
            for name, value in self._settings.weights.items()
        }

    def _feature_enabled(self, name: str) -> bool:
        return (
            self._enabled_features is None
            or name in self._enabled_features
        )

    def _buyability_capacity(self) -> Decimal:
        """Normalize buyability when research explicitly disables inputs."""

        capacity = Decimal("0.25")  # liquidity plus setup context
        if self._feature_enabled("premium_response"):
            capacity += Decimal("0.20")
        if self._feature_enabled("straddle_expansion"):
            capacity += Decimal("0.15")
        if self._feature_enabled("iv_surface"):
            capacity += Decimal("0.15")
        if self._feature_enabled("gamma_concentration"):
            capacity += Decimal("0.15")
        if (
            self._feature_enabled("iv_surface")
            or self._feature_enabled("india_vix_regime")
        ):
            capacity += Decimal("0.10")
        return capacity

    def _compression_ready(
        self,
        *,
        history: deque[_Observation],
        baseline: _Observation,
        context: StrategyEvaluationContext,
    ) -> tuple[bool, Decimal]:
        relevant = tuple(
            item
            for item in history
            if item.captured_at >= baseline.captured_at
        )
        observed_range = (
            max(item.spot for item in relevant)
            - min(item.spot for item in relevant)
            if relevant
            else Decimal("0")
        )
        range_ceiling = max(
            self._settings.maximum_compression_range_points,
            _intraday_move_scale(
                context.previous_20d_atr,
                self._settings.compression_window_seconds,
            )
            * Decimal("1.5"),
        )
        ready = (
            len(relevant) >= self._settings.minimum_compression_observations
            and observed_range <= range_ceiling
        )
        return ready, observed_range

    def _forecast_move(
        self,
        *,
        context: StrategyEvaluationContext,
        history: deque[_Observation],
        direction_score: Decimal,
    ) -> Decimal:
        side_sign = Decimal("1") if direction_score > 0 else Decimal("-1")
        aligned_projections: list[Decimal] = []
        for horizon in self._settings.direction_horizons_seconds:
            baseline = _baseline(
                history,
                context.captured_at - timedelta(seconds=horizon),
            )
            elapsed = Decimal(
                str(
                    max(
                        1,
                        (
                            context.captured_at - baseline.captured_at
                        ).total_seconds(),
                    )
                )
            )
            projected = (
                (context.spot - baseline.spot)
                / elapsed
                * Decimal(self._settings.forecast_horizon_seconds)
            )
            if projected * side_sign > 0:
                aligned_projections.append(abs(projected))
        fallback = _market_move_scale(
            context,
            self._settings.forecast_horizon_seconds,
        )
        observed_projection = (
            _median_decimal(aligned_projections)
            if aligned_projections
            else Decimal("0")
        )
        magnitude = max(observed_projection, fallback * abs(direction_score))
        straddle_cap = (
            context.atm_straddle_price * Decimal("0.35")
            if context.atm_straddle_price is not None
            else fallback * Decimal("2")
        )
        magnitude = min(magnitude, max(fallback, straddle_cap))
        return (
            magnitude if direction_score > 0 else -magnitude
        ).quantize(Decimal("0.0001"))

    def _evidence(
        self,
        *,
        side: str,
        direction_inputs: dict[str, Decimal],
        aligned: dict[str, Decimal],
        horizon_agreement: int,
        compression_ready: bool,
        gamma_matches: bool,
        expansion_triggered: bool,
        option_positioning: _OptionPositioning,
    ) -> tuple[StrategyEvidence, ...]:
        family_map = {
            "futures_flow": EvidenceFamily.FLOW,
            "index_momentum": EvidenceFamily.FLOW,
            "option_premium_momentum": EvidenceFamily.FLOW,
            "option_volume_flow": EvidenceFamily.POSITIONING,
            "iv_skew": EvidenceFamily.VOLATILITY,
            "oi_migration": EvidenceFamily.POSITIONING,
            "pcr_context": EvidenceFamily.POSITIONING,
            "futures_basis": EvidenceFamily.FLOW,
        }
        evidence = [
            StrategyEvidence(
                code=name,
                family=family_map[name],
                side=side,
                strength=min(
                    Decimal("1"),
                    abs(direction_inputs[name]) * Decimal("3"),
                ),
            )
            for name in aligned
        ]
        evidence.append(
            StrategyEvidence(
                "multi_horizon_consensus",
                EvidenceFamily.FLOW,
                side,
                _clamp(
                    Decimal(horizon_agreement)
                    / Decimal(len(self._settings.direction_horizons_seconds)),
                    Decimal("0"),
                    Decimal("1"),
                ),
                mandatory=True,
            )
        )
        if compression_ready:
            evidence.append(
                StrategyEvidence(
                    "quant_compression",
                    EvidenceFamily.VOLATILITY,
                    side,
                    Decimal("0.55"),
                )
            )
        if expansion_triggered:
            evidence.append(
                StrategyEvidence(
                    "convexity_expansion",
                    EvidenceFamily.VOLATILITY,
                    side,
                    Decimal("0.75"),
                    mandatory=True,
                )
            )
        if gamma_matches:
            evidence.append(
                StrategyEvidence(
                    "gamma_expansion",
                    EvidenceFamily.VOLATILITY,
                    side,
                    Decimal("0.80"),
                )
            )
        if option_positioning.side == side:
            evidence.append(
                StrategyEvidence(
                    "cross_strike_option_positioning",
                    EvidenceFamily.POSITIONING,
                    side,
                    option_positioning.strength,
                )
            )
        return tuple(evidence)


def _baseline(
    history: deque[_Observation],
    cutoff: datetime,
) -> _Observation:
    selected = history[0]
    for item in history:
        if item.captured_at <= cutoff:
            selected = item
        else:
            break
    return selected


def _option_positioning(
    *,
    current: _Observation,
    baseline: _Observation,
) -> _OptionPositioning:
    if current.captured_at <= baseline.captured_at:
        return _OptionPositioning()
    current_legs = {
        item.token: item
        for item in current.option_chain_legs
        if abs(item.relative_strike) <= 4 and item.oi > 0 and item.mid > 0
    }
    baseline_legs = {
        item.token: item
        for item in baseline.option_chain_legs
        if abs(item.relative_strike) <= 4 and item.oi > 0 and item.mid > 0
    }
    matches = tuple(
        (item, baseline_legs[token])
        for token, item in current_legs.items()
        if token in baseline_legs
    )
    if not matches:
        return _OptionPositioning()

    contributions: list[Decimal] = []
    regimes: dict[str, int] = {}
    for current_leg, baseline_leg in matches:
        premium_return = _percent_change(
            current_leg.mid,
            baseline_leg.mid,
        )
        oi_delta = current_leg.oi - baseline_leg.oi
        oi_change_percent = (
            Decimal(oi_delta)
            / Decimal(baseline_leg.oi)
            * Decimal("100")
        )
        if (
            abs(premium_return) < _OPTION_POSITIONING_PRICE_THRESHOLD
            or abs(oi_change_percent) < _OPTION_POSITIONING_OI_THRESHOLD
        ):
            continue

        premium_sign = Decimal("1") if premium_return > 0 else Decimal("-1")
        direction_sign = (
            premium_sign
            if current_leg.option_type.value == "CE"
            else -premium_sign
        )
        if premium_return > 0 and oi_delta > 0:
            regime = "long_buildup"
            reliability = Decimal("1")
        elif premium_return > 0:
            regime = "short_covering"
            reliability = Decimal("0.85")
        elif oi_delta > 0:
            regime = "writing"
            reliability = Decimal("1")
        else:
            regime = "long_unwinding"
            reliability = Decimal("0.65")
        magnitude = _clamp(
            abs(oi_change_percent) / Decimal("0.10"),
            Decimal("0.50"),
            Decimal("1"),
        )
        contributions.append(direction_sign * reliability * magnitude)
        key = f"{current_leg.option_type.value}_{regime}"
        regimes[key] = regimes.get(key, 0) + 1

    observed = len(contributions)
    if observed < _OPTION_POSITIONING_MINIMUM_LEGS:
        return _OptionPositioning(
            observed_legs=observed,
            reason=(
                f"cross-strike option positioning has {observed}/"
                f"{_OPTION_POSITIONING_MINIMUM_LEGS} material legs"
            ),
        )
    score = _clamp(
        sum(contributions, Decimal("0")) / Decimal(len(matches)),
        Decimal("-1"),
        Decimal("1"),
    ).quantize(Decimal("0.0001"))
    side = CALL if score >= Decimal("0.20") else PUT if score <= Decimal("-0.20") else None
    aligned = sum(
        contribution > 0 if side == CALL else contribution < 0
        for contribution in contributions
    ) if side is not None else 0
    regime_summary = ",".join(
        f"{name}={count}" for name, count in sorted(regimes.items())
    )
    return _OptionPositioning(
        available=True,
        score=score,
        side=side,
        strength=abs(score),
        aligned_legs=aligned,
        observed_legs=observed,
        reason=(
            f"cross-strike option positioning score={score:+.4f}; "
            f"aligned={aligned}/{observed}; {regime_summary}"
        ),
    )


def _aggregate_option_positioning(
    by_horizon: dict[int, _OptionPositioning],
    horizons: tuple[int, ...],
) -> _OptionPositioning:
    available = tuple(
        (index, by_horizon[horizon])
        for index, horizon in enumerate(horizons, start=1)
        if by_horizon[horizon].available
    )
    if not available:
        return _OptionPositioning()
    total_weight = sum((Decimal(index) for index, _ in available), Decimal("0"))
    score = _clamp(
        sum(
            (
                Decimal(index) * positioning.score
                for index, positioning in available
            ),
            Decimal("0"),
        )
        / total_weight,
        Decimal("-1"),
        Decimal("1"),
    ).quantize(Decimal("0.0001"))
    proposed_side = (
        CALL
        if score >= Decimal("0.20")
        else PUT
        if score <= Decimal("-0.20")
        else None
    )
    horizon_agreement = sum(
        item.side == proposed_side for _, item in available
    ) if proposed_side is not None else 0
    side = proposed_side if horizon_agreement >= 2 else None
    aligned_legs = sum(
        item.aligned_legs
        for _, item in available
        if item.side == side
    ) if side is not None else 0
    observed_legs = sum(item.observed_legs for _, item in available)
    return _OptionPositioning(
        available=True,
        score=score,
        side=side,
        strength=abs(score) if side is not None else Decimal("0"),
        aligned_legs=aligned_legs,
        observed_legs=observed_legs,
        reason=(
            f"multi-horizon cross-strike positioning score={score:+.4f}; "
            f"horizons={horizon_agreement}/{len(available)}; "
            f"aligned_legs={aligned_legs}/{observed_legs}"
        ),
    )


def _effective_futures_signal(
    flow: FuturesFlowContext | None,
) -> tuple[str | None, Decimal, str]:
    if flow is None:
        return None, Decimal("0"), "unavailable"
    positioning = flow.positioning
    if positioning is not None and positioning.ready:
        return (
            positioning.side,
            positioning.strength,
            positioning.reason,
        )
    return flow.side, flow.strength, flow.reason


def _futures_horizon_values(
    flow: FuturesFlowContext | None,
) -> dict[int, Decimal]:
    if flow is None or flow.positioning is None or not flow.positioning.ready:
        return {}
    return {
        item.horizon_seconds: (
            item.strength
            if item.side == CALL
            else -item.strength
            if item.side == PUT
            else Decimal("0")
        )
        for item in flow.positioning.horizons
    }


def _average_positive(
    left: Decimal | None,
    right: Decimal | None,
) -> Decimal | None:
    values = tuple(
        value for value in (left, right) if value is not None and value > 0
    )
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def _percent_change(
    current: Decimal | None,
    baseline: Decimal | None,
) -> Decimal:
    if (
        current is None
        or baseline is None
        or current <= 0
        or baseline <= 0
    ):
        return Decimal("0")
    return (
        (current / baseline - Decimal("1")) * Decimal("100")
    ).quantize(Decimal("0.0001"))


def _pairs(
    values: tuple[_Observation, ...],
) -> tuple[tuple[_Observation, _Observation], ...]:
    return tuple(zip(values, values[1:]))


def _typical_step_seconds(history: deque[_Observation]) -> int:
    elapsed = sorted(
        max(
            1,
            int((right.captured_at - left.captured_at).total_seconds()),
        )
        for left, right in _pairs(tuple(history))
        if right.captured_at > left.captured_at
    )
    if not elapsed:
        return 15
    return elapsed[len(elapsed) // 2]


def _median_decimal(values: list[Decimal] | tuple[Decimal, ...]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _quantile_decimal(
    values: tuple[Decimal, ...],
    quantile: Decimal,
) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal("0")
    position = quantile * Decimal(len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - Decimal(lower_index)
    return (
        ordered[lower_index] * (Decimal("1") - fraction)
        + ordered[upper_index] * fraction
    )


def _robust_scale(
    values: tuple[Decimal, ...],
    *,
    fallback: Decimal,
) -> Decimal:
    nonzero = tuple(abs(value) for value in values if value != 0)
    floor = max(Decimal("0.0001"), abs(fallback) * Decimal("0.25"))
    if len(nonzero) < 4:
        return max(floor, abs(fallback))
    median_absolute_move = _median_decimal(nonzero)
    robust_sigma = median_absolute_move / Decimal("0.6745")
    return max(floor, robust_sigma)


def _normalized_zscore(
    value: Decimal,
    scale: Decimal,
    clip: Decimal,
) -> Decimal:
    safe_scale = max(abs(scale), Decimal("0.0001"))
    clipped = _clamp(value / safe_scale, -clip, clip)
    return (clipped / clip).quantize(Decimal("0.0001"))


def _average(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _relative_option_move(
    current: _Observation,
    baseline: _Observation,
) -> Decimal:
    return (
        _percent_change(current.call_mid, baseline.call_mid)
        - _percent_change(current.put_mid, baseline.put_mid)
    ) / Decimal("2")


def _relative_iv_move(
    current: _Observation,
    baseline: _Observation,
) -> Decimal:
    return (
        _percent_change(current.call_iv, baseline.call_iv)
        - _percent_change(current.put_iv, baseline.put_iv)
    ) / Decimal("2")


def _latest_leg_impulse(
    history: deque[_Observation],
    *,
    side: str,
    zscore_clip: Decimal,
) -> tuple[Decimal, Decimal]:
    observations = tuple(history)
    if len(observations) < 2:
        return Decimal("0"), Decimal("0")
    leg_returns = tuple(
        _percent_change(
            right.call_mid if side == CALL else right.put_mid,
            left.call_mid if side == CALL else left.put_mid,
        )
        for left, right in _pairs(observations)
    )
    current_return = leg_returns[-1]
    scale = _robust_scale(
        leg_returns[:-1],
        fallback=Decimal("0.50"),
    )
    zscore = _clamp(
        current_return / max(scale, Decimal("0.0001")),
        -zscore_clip,
        zscore_clip,
    )
    return current_return, zscore.quantize(Decimal("0.0001"))


def _intraday_move_scale(
    atr: Decimal | None,
    horizon_seconds: int,
) -> Decimal:
    if atr is None or atr <= 0:
        return Decimal("10")
    session_seconds = Decimal("22500")
    ratio = Decimal(horizon_seconds) / session_seconds
    return max(
        Decimal("3"),
        atr * Decimal(str(float(ratio) ** 0.5)),
    )


def _market_move_scale(
    context: StrategyEvaluationContext,
    horizon_seconds: int,
) -> Decimal:
    if context.previous_20d_atr is not None and context.previous_20d_atr > 0:
        return _intraday_move_scale(
            context.previous_20d_atr,
            horizon_seconds,
        )
    if (
        context.atm_straddle_price is not None
        and context.atm_straddle_price > 0
    ):
        session_seconds = Decimal("22500")
        ratio = Decimal(horizon_seconds) / session_seconds
        return max(
            Decimal("2"),
            context.atm_straddle_price
            * Decimal(str(float(ratio) ** 0.5)),
        )
    return Decimal("5")


def _matching_spread_ratio(
    context: StrategyEvaluationContext,
    side: str,
) -> Decimal | None:
    # Mid prices are synchronized, but the evaluator intentionally does not
    # receive order-book state. Exact spread/depth is enforced by the gate.
    mid = context.atm_call_mid if side == CALL else context.atm_put_mid
    return Decimal("0.01") if mid is not None and mid > 0 else None


def _expected_move_buyability_score(
    context: StrategyEvaluationContext,
) -> Decimal:
    if (
        context.expected_upper is None
        or context.expected_lower is None
        or context.expected_upper <= context.expected_lower
    ):
        return Decimal("0")
    midpoint = (
        context.expected_upper + context.expected_lower
    ) / Decimal("2")
    half_range = (
        context.expected_upper - context.expected_lower
    ) / Decimal("2")
    utilization = abs(context.spot - midpoint) / half_range
    if utilization <= Decimal("0.35"):
        return _clamp(
            utilization / Decimal("0.35"),
            Decimal("0"),
            Decimal("1"),
        )
    if utilization <= Decimal("1.10"):
        return Decimal("1")
    return _clamp(
        (Decimal("1.50") - utilization) / Decimal("0.40"),
        Decimal("0"),
        Decimal("1"),
    )


def _vix_buyability_score(value: Decimal | None) -> Decimal:
    if value is None or value <= 0:
        return Decimal("0")
    if value < Decimal("12"):
        return _clamp(
            Decimal("0.50") + value / Decimal("24"),
            Decimal("0"),
            Decimal("1"),
        )
    if value <= Decimal("22"):
        return Decimal("1")
    if value <= Decimal("30"):
        return _clamp(
            Decimal("1")
            - (value - Decimal("22")) / Decimal("10"),
            Decimal("0.20"),
            Decimal("1"),
        )
    return Decimal("0.20")


def _scaled_positive(value: Decimal, threshold: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if threshold <= 0:
        return Decimal("1")
    return _clamp(
        value / threshold,
        Decimal("0"),
        Decimal("1"),
    )


def _clamp(
    value: Decimal,
    lower: Decimal,
    upper: Decimal,
) -> Decimal:
    return min(upper, max(lower, value))
