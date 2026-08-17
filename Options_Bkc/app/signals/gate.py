from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    AnalyticsSnapshot,
    EvidenceFamily,
    InstrumentKind,
    MarketRegime,
    MicrostructureSignal,
    OptionChainSnapshot,
    OptionQuote,
    SignalSetup,
)


@dataclass(frozen=True)
class SignalGateSettings:
    min_confirmations: int
    cooldown_seconds: int
    max_level_distance: Decimal
    max_microstructure_age_seconds: int
    mode: str = "shadow"
    min_microstructure_confidence: Decimal = Decimal("0.40")
    min_signal_score: Decimal = Decimal("80")
    straddle_zone_ratio: Decimal = Decimal("0.10")
    min_range_room_points: Decimal = Decimal("20")
    allowed_underlyings: tuple[str, ...] = ("NIFTY",)
    require_complete_chain: bool = False
    min_chain_quotes: int = 6
    require_greeks: bool = False
    require_target_contract: bool = False
    max_contract_spread: Decimal = Decimal("1.50")
    min_directional_confirmations: int = 0
    min_independent_confirmation_families: int = 0
    require_regime_match: bool = False
    enforce_session: bool = False
    market_timezone: str = "Asia/Kolkata"
    session_start: time = time(9, 15)
    last_entry_time: time = time(15, 15)
    max_underlying_age_seconds: int = 3
    max_daily_loss: Decimal = Decimal("0")
    max_concurrent_positions: int = 0
    max_gross_exposure: Decimal = Decimal("0")
    premium_transmission_enabled: bool = True
    premium_transmission_min_expected_return_percent: Decimal = Decimal("3")
    premium_transmission_min_ratio: Decimal = Decimal("0.35")
    local_reversal_cooldown_seconds: int = 900
    quant_require_target_option_confirmation: bool = True
    quant_require_futures_confirmation: bool = True
    quant_min_option_confirmations: int = 2
    quant_min_futures_confirmations: int = 2
    quant_cooldown_seconds: int = 900
    profile_min_directional_confirmations: int | None = None
    profile_min_independent_confirmation_families: int | None = None
    profile_min_confirmations: int | None = None
    gamma_require_microstructure_confirmation: bool = True
    gamma_require_target_option_confirmation: bool = False
    gamma_require_structural_room: bool = True

    def __post_init__(self) -> None:
        if self.premium_transmission_min_expected_return_percent < 0:
            raise ValueError(
                "premium transmission expected-return threshold must be non-negative"
            )
        if self.premium_transmission_min_ratio < 0:
            raise ValueError("premium transmission ratio must be non-negative")
        if self.quant_min_option_confirmations < 0:
            raise ValueError(
                "quant option confirmations cannot be negative"
            )
        if self.quant_min_futures_confirmations < 0:
            raise ValueError(
                "quant futures confirmations cannot be negative"
            )
        if self.quant_cooldown_seconds < 0:
            raise ValueError("quant cooldown cannot be negative")
        for name in (
            "profile_min_directional_confirmations",
            "profile_min_independent_confirmation_families",
            "profile_min_confirmations",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class TradingRiskState:
    realized_pnl: Decimal = Decimal("0")
    open_positions: int = 0
    gross_exposure: Decimal = Decimal("0")


@dataclass(frozen=True)
class SignalGateDecision:
    """Auditable research decision produced by the strong-signal gate."""

    captured_at: datetime
    raw_signal: str
    published_signal: str
    qualified: bool
    reason: str
    microstructure_signal: MicrostructureSignal | None
    setup_type: SignalSetup = SignalSetup.NONE
    confidence_score: Decimal = Decimal("0")
    confirmation_count: int = 0
    strong_signal: str = "NO_SIGNAL"
    evidence: tuple[str, ...] = ()


class SignalGate:
    """Publish only persistent, fresh and structurally coherent research signals.

    Microstructure events are accumulated independently from the slower option-chain
    snapshot interval. Support/resistance rules are setup-specific: they remain
    strict for breakouts and level reversals, while range momentum is allowed when
    sufficient room remains before the next opposing level.
    """

    def __init__(self, settings: SignalGateSettings) -> None:
        self._settings = settings
        self._micro_history: dict[str, deque[MicrostructureSignal]] = {}
        self._last_qualified: dict[tuple[str, str, SignalSetup], datetime] = {}
        self._risk_state = TradingRiskState()

    def update_risk_state(
        self,
        *,
        realized_pnl: Decimal,
        open_positions: int,
        gross_exposure: Decimal,
    ) -> None:
        """Update paper/live account state used by pre-trade limits."""

        self._risk_state = TradingRiskState(
            realized_pnl=realized_pnl,
            open_positions=max(0, open_positions),
            gross_exposure=max(Decimal("0"), gross_exposure),
        )

    def reset_session(self) -> None:
        """Clear feed-derived persistence while retaining account risk."""

        self._micro_history.clear()
        self._last_qualified.clear()

    def observe_microstructure(self, signal: MicrostructureSignal) -> None:
        history = self._micro_history.setdefault(signal.underlying, deque())
        identity = (signal.token.token, signal.side, signal.captured_at)
        if not any(
            (item.token.token, item.side, item.captured_at) == identity
            for item in history
        ):
            history.append(signal)
        self._prune_microstructure(signal.underlying, signal.captured_at)

    def preflight_data(
        self,
        *,
        snapshot: OptionChainSnapshot,
        underlying_observed_at: datetime | None,
        refreshed_quote_tokens: set[str] | None,
        refreshed_greeks_tokens: set[str] | None,
    ) -> str | None:
        """Reject unusable frames before running stateful strategy analysis."""

        allowed = {item.upper() for item in self._settings.allowed_underlyings}
        if snapshot.underlying.upper() not in allowed:
            return (
                f"underlying {snapshot.underlying} is outside the "
                "configured universe"
            )
        integrity_error = _chain_integrity_error(snapshot)
        if integrity_error is not None:
            return integrity_error
        return self._base_data_quality_error(
            snapshot=snapshot,
            underlying_observed_at=underlying_observed_at,
            refreshed_quote_tokens=refreshed_quote_tokens,
            refreshed_greeks_tokens=refreshed_greeks_tokens,
        )

    def reject_preflight(
        self,
        *,
        snapshot: OptionChainSnapshot,
        reason: str,
    ) -> tuple[AnalyticsSnapshot, SignalGateDecision]:
        analytics = AnalyticsSnapshot(
            underlying=snapshot.underlying,
            captured_at=snapshot.captured_at,
            atm_strike=snapshot.atm_strike,
            signal="NEUTRAL",
            signal_reason=f"DATA PREFLIGHT REJECTED: {reason}",
        )
        return analytics, SignalGateDecision(
            captured_at=snapshot.captured_at,
            raw_signal="NEUTRAL",
            published_signal="NEUTRAL",
            qualified=False,
            reason=f"DATA PREFLIGHT REJECTED: {reason}",
            microstructure_signal=None,
        )

    def evaluate(
        self,
        *,
        snapshot: OptionChainSnapshot,
        analytics: AnalyticsSnapshot,
        microstructure_signal: MicrostructureSignal | None,
        underlying_observed_at: datetime | None = None,
        refreshed_quote_tokens: set[str] | None = None,
        refreshed_greeks_tokens: set[str] | None = None,
        microstructure_not_before: datetime | None = None,
    ) -> tuple[AnalyticsSnapshot, SignalGateDecision]:
        if microstructure_signal is not None:
            self.observe_microstructure(microstructure_signal)

        raw_signal = analytics.signal or "NEUTRAL"
        setup_type = _resolve_setup_type(analytics)
        (
            rejection,
            selected_micro,
            confirmations,
            score,
            evidence,
        ) = self._validate_candidate(
            snapshot=snapshot,
            analytics=analytics,
            setup_type=setup_type,
            fallback_micro=microstructure_signal,
            underlying_observed_at=underlying_observed_at,
            refreshed_quote_tokens=refreshed_quote_tokens,
            refreshed_greeks_tokens=refreshed_greeks_tokens,
            microstructure_not_before=microstructure_not_before,
        )

        qualified = rejection is None
        if qualified:
            self._last_qualified[
                (snapshot.underlying, raw_signal, setup_type)
            ] = snapshot.captured_at
            reason = (
                f"SHADOW QUALIFIED STRONG {raw_signal} [{setup_type.value}] "
                f"score={score}: {analytics.signal_reason}"
            )
        else:
            reason = f"GATE REJECTED {raw_signal}: {rejection}"

        # The worker remains research-only. A strong research signal is exposed
        # separately while the broker-facing published signal stays neutral in
        # shadow mode.
        published = (
            "NEUTRAL"
            if self._settings.mode == "shadow"
            else raw_signal if qualified else "NEUTRAL"
        )
        strong_signal = raw_signal if qualified else "NO_SIGNAL"
        gated = replace(analytics, signal=published, signal_reason=reason)
        return gated, SignalGateDecision(
            captured_at=snapshot.captured_at,
            raw_signal=raw_signal,
            published_signal=published,
            qualified=qualified,
            reason=reason,
            microstructure_signal=selected_micro,
            setup_type=setup_type,
            confidence_score=score,
            confirmation_count=confirmations,
            strong_signal=strong_signal,
            evidence=evidence,
        )

    def _validate_candidate(
        self,
        *,
        snapshot: OptionChainSnapshot,
        analytics: AnalyticsSnapshot,
        setup_type: SignalSetup,
        fallback_micro: MicrostructureSignal | None,
        underlying_observed_at: datetime | None,
        refreshed_quote_tokens: set[str] | None,
        refreshed_greeks_tokens: set[str] | None,
        microstructure_not_before: datetime | None,
    ) -> tuple[
        str | None,
        MicrostructureSignal | None,
        int,
        Decimal,
        tuple[str, ...],
    ]:
        side = analytics.signal or "NEUTRAL"
        is_quant = setup_type in {
            SignalSetup.DERIVATIVES_QUANT,
            SignalSetup.OPTION_CHAIN_IMPULSE,
            SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
        }
        if side not in {"BUY_CALL", "BUY_PUT"}:
            return (
                "candidate is not directional",
                fallback_micro,
                0,
                Decimal("0"),
                (),
            )

        allowed = {item.upper() for item in self._settings.allowed_underlyings}
        if snapshot.underlying.upper() not in allowed:
            return (
                f"underlying {snapshot.underlying} is outside the configured universe",
                fallback_micro,
                0,
                Decimal("0"),
                (),
            )

        chain_error = _chain_integrity_error(snapshot)
        if chain_error is not None:
            return chain_error, fallback_micro, 0, Decimal("0"), ()
        if analytics.underlying != snapshot.underlying:
            return (
                "analytics underlying does not match the option-chain snapshot",
                fallback_micro,
                0,
                Decimal("0"),
                (),
            )
        analytics_age = abs(
            (snapshot.captured_at - analytics.captured_at).total_seconds()
        )
        if analytics_age > 0.001:
            return (
                f"analytics snapshot is not synchronized ({analytics_age:.3f}s)",
                fallback_micro,
                0,
                Decimal("0"),
                (),
            )

        quality_error = self._data_quality_error(
            snapshot=snapshot,
            side=side,
            underlying_observed_at=underlying_observed_at,
            refreshed_quote_tokens=refreshed_quote_tokens,
            refreshed_greeks_tokens=refreshed_greeks_tokens,
        )
        if quality_error is not None:
            return quality_error, fallback_micro, 0, Decimal("0"), ()

        risk_error = self._risk_error(snapshot.captured_at)
        if risk_error is not None:
            return risk_error, fallback_micro, 0, Decimal("0"), ()

        if self._settings.require_regime_match:
            regime_error = _regime_setup_error(
                analytics.market_regime,
                setup_type,
                analytics.signal_reason,
            )
            if regime_error is not None:
                return regime_error, fallback_micro, 0, Decimal("0"), ()

        exhaustion = analytics.momentum_exhaustion
        if (
            exhaustion is not None
            and exhaustion.winning_side == side
            and exhaustion.action.value != "NONE"
        ):
            return (
                "momentum exhaustion blocks a fresh same-side option entry; "
                f"management action is {exhaustion.action.value}",
                fallback_micro,
                0,
                Decimal("0"),
                (exhaustion.state.value,),
            )

        required_directional_confirmations = (
            self._settings.profile_min_directional_confirmations
            if self._settings.profile_min_directional_confirmations is not None
            else self._settings.min_directional_confirmations
        )
        if (
            len(analytics.directional_confirmations)
            < required_directional_confirmations
        ):
            return (
                f"only {len(analytics.directional_confirmations)}/"
                f"{required_directional_confirmations} "
                "directional confirmations",
                fallback_micro,
                0,
                Decimal("0"),
                tuple(analytics.directional_confirmations),
            )

        confirmation_families = {
            item.family
            for item in analytics.directional_evidence
            if item.side in {None, side}
            and item.strength > 0
            and item.family
            in {
                EvidenceFamily.PRICE_ACTION,
                EvidenceFamily.POSITIONING,
                EvidenceFamily.VOLATILITY,
                EvidenceFamily.FLOW,
            }
        }
        required_confirmation_families = (
            self._settings.profile_min_independent_confirmation_families
            if self._settings.profile_min_independent_confirmation_families
            is not None
            else self._settings.min_independent_confirmation_families
        )
        if len(confirmation_families) < required_confirmation_families:
            return (
                f"only {len(confirmation_families)}/"
                f"{required_confirmation_families} "
                "independent confirmation families",
                fallback_micro,
                0,
                Decimal("0"),
                tuple(
                    f"{item.family.value}:{item.code}"
                    for item in analytics.directional_evidence
                    if item.side in {None, side} and item.strength > 0
                ),
            )

        is_gamma = setup_type == SignalSetup.MOMENTUM_EXPANSION
        target_quote = _target_quote(snapshot, analytics)
        requires_target_contract = (
            self._settings.require_target_contract
            or (
                is_quant
                and self._settings.quant_require_target_option_confirmation
            )
            or (
                is_gamma
                and self._settings.gamma_require_target_option_confirmation
            )
        )
        if requires_target_contract:
            target_error = self._target_contract_error(target_quote, analytics)
            if target_error is not None:
                return target_error, fallback_micro, 0, Decimal("0"), ()
        transmission_error = self._premium_transmission_error(
            target_quote=target_quote,
            analytics=analytics,
        )
        if transmission_error is not None:
            return transmission_error, fallback_micro, 0, Decimal("0"), (
                "weak_premium_transmission",
            )

        if (
            setup_type == SignalSetup.MOMENTUM_EXPANSION
            and not self._settings.gamma_require_microstructure_confirmation
        ):
            return None, None, 0, Decimal("0"), ()

        fresh = self._fresh_microstructure(
            snapshot.underlying,
            snapshot.captured_at,
            not_before=microstructure_not_before,
        )
        target_token = (
            target_quote.contract.token.token
            if target_quote is not None
            and requires_target_contract
            else None
        )
        target_option_fresh = [
            item
            for item in fresh
            if item.token.kind != InstrumentKind.FUTURE
            and (target_token is None or item.token.token == target_token)
        ]
        futures_fresh = [
            item
            for item in fresh
            if item.token.kind == InstrumentKind.FUTURE
        ]
        relevant_fresh = (
            sorted(
                target_option_fresh + futures_fresh,
                key=lambda item: item.captured_at,
            )
            if is_quant
            else target_option_fresh
        )
        relevant_fallback = (
            fallback_micro
            if (
                fallback_micro is not None
                and (
                    (
                        fallback_micro.token.kind
                        != InstrumentKind.FUTURE
                        and (
                            target_token is None
                            or fallback_micro.token.token == target_token
                        )
                    )
                    or (
                        is_quant
                        and fallback_micro.token.kind
                        == InstrumentKind.FUTURE
                    )
                )
            )
            else None
        )
        selected_micro = (
            relevant_fresh[-1]
            if relevant_fresh
            else relevant_fallback
        )
        micro_optional = (
            setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
        )
        if not relevant_fresh and not micro_optional and not is_quant:
            if fresh:
                rejection = (
                    f"only 0/{self._settings.min_confirmations} "
                    "fresh microstructure confirmations for the selected "
                    "contract"
                )
            elif relevant_fallback is None:
                rejection = "no fresh depth-and-velocity confirmation"
            else:
                age = (
                    snapshot.captured_at
                    - relevant_fallback.captured_at
                ).total_seconds()
                rejection = f"microstructure confirmation is stale ({age:.1f}s)"
            return rejection, selected_micro, 0, Decimal("0"), ()

        matching = [
            item
            for item in relevant_fresh
            if item.side == side
        ]
        opposing = [
            item for item in relevant_fresh if item.side != side
        ]
        if opposing:
            latest_opposing = opposing[-1]
            return (
                f"fresh microstructure conflict: {latest_opposing.side} also present",
                latest_opposing,
                len(matching),
                Decimal("0"),
                (),
            )

        option_matching = [
            item
            for item in matching
            if item.token.kind != InstrumentKind.FUTURE
        ]
        futures_matching = [
            item
            for item in matching
            if item.token.kind == InstrumentKind.FUTURE
        ]
        if (
            is_quant
            and self._settings.quant_require_target_option_confirmation
            and len(option_matching)
            < self._settings.quant_min_option_confirmations
        ):
            return (
                f"only {len(option_matching)}/"
                f"{self._settings.quant_min_option_confirmations} fresh "
                "target-option liquidity confirmations",
                option_matching[-1] if option_matching else selected_micro,
                len(matching),
                Decimal("0"),
                ("target_option_liquidity_missing",),
            )
        if (
            is_quant
            and self._settings.quant_require_futures_confirmation
            and len(futures_matching)
            < self._settings.quant_min_futures_confirmations
        ):
            return (
                f"only {len(futures_matching)}/"
                f"{self._settings.quant_min_futures_confirmations} fresh "
                "NIFTY-futures order-book confirmations",
                futures_matching[-1] if futures_matching else selected_micro,
                len(matching),
                Decimal("0"),
                ("futures_liquidity_missing",),
            )
        required_non_quant_confirmations = (
            self._settings.profile_min_confirmations
            if self._settings.profile_min_confirmations is not None
            else self._settings.min_confirmations
        )
        if (
            not is_quant
            and len(matching) < required_non_quant_confirmations
            and not micro_optional
        ):
            return (
                f"only {len(matching)}/{required_non_quant_confirmations} "
                "fresh microstructure confirmations",
                matching[-1] if matching else selected_micro,
                len(matching),
                Decimal("0"),
                (),
            )

        average_confidence = (
            (
                sum(
                    (item.confidence for item in matching),
                    Decimal("0"),
                )
                / Decimal(len(matching))
            )
            if matching
            else Decimal("0")
        )
        if (
            matching
            and average_confidence
            < self._settings.min_microstructure_confidence
            and not micro_optional
        ):
            return (
                f"microstructure confidence {average_confidence:.4f} is below "
                f"{self._settings.min_microstructure_confidence:.4f}",
                matching[-1] if matching else selected_micro,
                len(matching),
                Decimal("0"),
                (),
            )

        structure_ok, structure_reason = self._at_valid_location(
            snapshot=snapshot,
            analytics=analytics,
            side=side,
            setup_type=setup_type,
        )
        if not structure_ok:
            return (
                structure_reason,
                matching[-1] if matching else selected_micro,
                len(matching),
                Decimal("0"),
                (),
            )

        previous = self._last_qualified.get(
            (snapshot.underlying, side, setup_type)
        )
        cooldown_seconds = (
            max(
                self._settings.cooldown_seconds,
                self._settings.local_reversal_cooldown_seconds,
            )
            if setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
            else (
                max(
                    self._settings.cooldown_seconds,
                    self._settings.quant_cooldown_seconds,
                )
                if is_quant
                else self._settings.cooldown_seconds
            )
        )
        if (
            previous
            and snapshot.captured_at - previous
            < timedelta(seconds=cooldown_seconds)
        ):
            return (
                "setup cooldown is active",
                matching[-1] if matching else selected_micro,
                len(matching),
                Decimal("0"),
                (),
            )

        score, evidence = self._score(
            setup_type=setup_type,
            average_micro_confidence=average_confidence,
            confirmation_count=len(matching),
            structure_reason=structure_reason,
            analytics=analytics,
        )
        if score < self._settings.min_signal_score:
            return (
                f"signal-quality score {score} is below "
                f"{self._settings.min_signal_score}",
                matching[-1] if matching else selected_micro,
                len(matching),
                score,
                evidence,
            )
        return (
            None,
            matching[-1] if matching else selected_micro,
            len(matching),
            score,
            evidence,
        )

    def _premium_transmission_error(
        self,
        *,
        target_quote: OptionQuote | None,
        analytics: AnalyticsSnapshot,
    ) -> str | None:
        """Reject only measurable under-response; unavailable/immature data is neutral."""

        if (
            not self._settings.premium_transmission_enabled
            or target_quote is None
        ):
            return None
        token = target_quote.contract.token.token
        response = next(
            (
                item
                for item in analytics.premium_responses
                if item.token == token
            ),
            None,
        )
        if response is None:
            return None
        expected_return = (
            response.directional_expected_return_percent
            if response.directional_expected_return_percent is not None
            else response.expected_return_percent
        )
        transmission_ratio = (
            response.directional_transmission_ratio
            if response.directional_transmission_ratio is not None
            else response.transmission_ratio
        )
        expected_change = (
            response.favorable_directional_expected_change
            if response.favorable_directional_expected_change is not None
            else response.favorable_expected_change
        )
        if (
            expected_return is None
            or transmission_ratio is None
            or expected_return
            < self._settings.premium_transmission_min_expected_return_percent
        ):
            return None
        if (
            transmission_ratio
            >= self._settings.premium_transmission_min_ratio
        ):
            return None
        actual = (
            response.favorable_directional_actual_change
            if response.favorable_directional_actual_change is not None
            else response.favorable_actual_change
        ) or Decimal("0")
        expected = expected_change or Decimal("0")
        return (
            "exact contract shows weak premium transmission: "
            f"actual={actual:.2f}, delta-gamma-implied={expected:.2f}, "
            f"ratio={transmission_ratio:.3f} below "
            f"{self._settings.premium_transmission_min_ratio:.3f}"
        )

    def _data_quality_error(
        self,
        *,
        snapshot: OptionChainSnapshot,
        side: str,
        underlying_observed_at: datetime | None,
        refreshed_quote_tokens: set[str] | None,
        refreshed_greeks_tokens: set[str] | None,
    ) -> str | None:
        base_error = self._base_data_quality_error(
            snapshot=snapshot,
            underlying_observed_at=underlying_observed_at,
            refreshed_quote_tokens=refreshed_quote_tokens,
            refreshed_greeks_tokens=refreshed_greeks_tokens,
        )
        if base_error is not None:
            return base_error
        if not self._settings.require_complete_chain:
            return None
        usable = _usable_quotes(snapshot)

        directional_type = "CE" if side == "BUY_CALL" else "PE"
        if not any(
            quote.contract.option_type.value == directional_type
            and _valid_bid_ask(quote, self._settings.max_contract_spread)
            for quote in usable
        ):
            return f"no executable {directional_type} quote with a valid bid/ask"

        return None

    def _base_data_quality_error(
        self,
        *,
        snapshot: OptionChainSnapshot,
        underlying_observed_at: datetime | None,
        refreshed_quote_tokens: set[str] | None,
        refreshed_greeks_tokens: set[str] | None,
    ) -> str | None:
        if not self._settings.require_complete_chain:
            return None
        usable = _usable_quotes(snapshot)
        if len(usable) < self._settings.min_chain_quotes:
            return (
                f"incomplete option chain: {len(usable)}/"
                f"{self._settings.min_chain_quotes} usable quotes"
            )
        expected_tokens = {
            quote.contract.token.token for quote in snapshot.quotes
        }
        if (
            refreshed_quote_tokens is None
            or not expected_tokens.issubset(refreshed_quote_tokens)
        ):
            return "current quote refresh does not cover the selected chain"
        atm_types = {
            quote.contract.option_type.value
            for quote in usable
            if quote.contract.strike == snapshot.atm_strike
        }
        if atm_types != {"CE", "PE"}:
            return "incomplete option chain: ATM call/put pair is missing"
        if self._settings.require_greeks:
            if (
                refreshed_greeks_tokens is None
                or not expected_tokens.issubset(refreshed_greeks_tokens)
            ):
                return "current greeks refresh does not cover the selected chain"
            greeks_count = sum(quote.greeks is not None for quote in usable)
            if greeks_count < self._settings.min_chain_quotes:
                return (
                    f"incomplete option chain: {greeks_count}/"
                    f"{self._settings.min_chain_quotes} quotes have greeks"
                )
        if underlying_observed_at is None:
            return "underlying freshness is unavailable"
        age = (snapshot.captured_at - underlying_observed_at).total_seconds()
        if age < 0:
            return "underlying observation is from the future"
        if age > self._settings.max_underlying_age_seconds:
            return f"underlying price is stale ({age:.1f}s)"
        return None

    def _risk_error(self, captured_at: datetime) -> str | None:
        if self._settings.enforce_session:
            local = captured_at.astimezone(ZoneInfo(self._settings.market_timezone))
            if local.weekday() >= 5:
                return "session gate is closed on weekends"
            if not (
                self._settings.session_start
                <= local.time().replace(tzinfo=None)
                <= self._settings.last_entry_time
            ):
                return (
                    f"outside entry session "
                    f"{self._settings.session_start.isoformat(timespec='minutes')}-"
                    f"{self._settings.last_entry_time.isoformat(timespec='minutes')}"
                )

        state = self._risk_state
        if (
            self._settings.max_daily_loss > 0
            and state.realized_pnl <= -self._settings.max_daily_loss
        ):
            return "daily-loss limit has been reached"
        if (
            self._settings.max_concurrent_positions > 0
            and state.open_positions >= self._settings.max_concurrent_positions
        ):
            return "maximum concurrent positions reached"
        if (
            self._settings.max_gross_exposure > 0
            and state.gross_exposure >= self._settings.max_gross_exposure
        ):
            return "maximum gross exposure reached"
        return None

    def _target_contract_error(
        self,
        quote: OptionQuote | None,
        analytics: AnalyticsSnapshot,
    ) -> str | None:
        if quote is None:
            return "no executable target contract was selected"
        expected = "CE" if analytics.signal == "BUY_CALL" else "PE"
        if quote.contract.option_type.value != expected:
            return "selected target contract has the wrong option direction"
        if not _valid_bid_ask(quote, self._settings.max_contract_spread):
            return "selected target contract has an invalid or excessive spread"
        if quote.volume is None or quote.volume <= 0:
            return "selected target contract has no traded volume"
        if quote.oi is None or quote.oi <= 0:
            return "selected target contract has no open interest"
        if quote.greeks is None or quote.greeks.delta is None:
            return "selected target contract has no usable delta"
        dte = (quote.contract.expiry - analytics.captured_at.date()).days
        if dte < 0:
            return "selected target contract is expired"
        return None

    def _fresh_microstructure(
        self,
        underlying: str,
        captured_at: datetime,
        *,
        not_before: datetime | None = None,
    ) -> list[MicrostructureSignal]:
        self._prune_microstructure(underlying, captured_at)
        history = self._micro_history.get(underlying, ())
        return [
            item
            for item in history
            if 0
            <= (captured_at - item.captured_at).total_seconds()
            <= self._settings.max_microstructure_age_seconds
            and (not_before is None or item.captured_at >= not_before)
        ]

    def _prune_microstructure(self, underlying: str, captured_at: datetime) -> None:
        history = self._micro_history.get(underlying)
        if history is None:
            return
        cutoff = captured_at - timedelta(
            seconds=self._settings.max_microstructure_age_seconds
        )
        while history and history[0].captured_at < cutoff:
            history.popleft()

    def _at_valid_location(
        self,
        *,
        snapshot: OptionChainSnapshot,
        analytics: AnalyticsSnapshot,
        side: str,
        setup_type: SignalSetup,
    ) -> tuple[bool, str]:
        support = (
            analytics.support_levels[0].strike
            if analytics.support_levels
            else None
        )
        resistance = (
            analytics.resistance_levels[0].strike
            if analytics.resistance_levels
            else None
        )
        spot = snapshot.spot_price
        zone = self._level_zone(analytics)

        if setup_type == SignalSetup.BREAKOUT:
            if side == "BUY_CALL":
                valid = resistance is not None and spot >= resistance
            else:
                valid = support is not None and spot <= support
            return valid, (
                "confirmed structural breakout"
                if valid
                else "breakout setup has not crossed its structural level"
            )

        if setup_type in {
            SignalSetup.DERIVATIVES_QUANT,
            SignalSetup.OPTION_CHAIN_IMPULSE,
            SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
        }:
            return True, (
                "causal derivatives direction and exact-contract liquidity "
                "are aligned"
            )

        if (
            setup_type == SignalSetup.MOMENTUM_EXPANSION
            and not self._settings.gamma_require_structural_room
        ):
            return True, (
                "gamma compression, OTM-IV expansion and exact-contract "
                "liquidity are aligned"
            )

        if setup_type == SignalSetup.LEVEL_REVERSAL:
            if side == "BUY_CALL":
                valid = support is not None and abs(spot - support) <= zone
                if valid and resistance is not None:
                    valid = resistance - spot >= self._minimum_room(analytics, zone)
            else:
                valid = resistance is not None and abs(spot - resistance) <= zone
                if valid and support is not None:
                    valid = spot - support >= self._minimum_room(analytics, zone)
            return valid, (
                "confirmed level-reversal zone with room to the opposing level"
                if valid
                else "level-reversal setup lacks a valid activation zone or range room"
            )

        if setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL:
            activation = analytics.activation_level
            if activation is None:
                return False, "local reversal has no activation level"
            valid = abs(spot - activation) <= zone
            if valid and side == "BUY_CALL" and resistance is not None:
                valid = (
                    resistance - spot
                    >= self._minimum_room(analytics, zone)
                )
            if valid and side == "BUY_PUT" and support is not None:
                valid = (
                    spot - support
                    >= self._minimum_room(analytics, zone)
                )
            return valid, (
                "closed-candle rejection at persistent local OI level"
                if valid
                else "local reversal lacks activation proximity or range room"
            )

        if setup_type in {
            SignalSetup.RANGE_ROTATION,
            SignalSetup.MOMENTUM_EXPANSION,
        }:
            if side == "BUY_CALL":
                if support is not None and abs(spot - support) <= zone:
                    if resistance is None:
                        return False, "range setup has no resistance boundary"
                    if resistance - spot < self._minimum_room(analytics, zone):
                        return False, "insufficient room before resistance"
                    return True, "bullish rotation from support zone"
                if resistance is None:
                    return False, "range setup has no resistance boundary"
                room = resistance - spot
                if room < Decimal("0"):
                    return True, "bullish continuation is above resistance"
                if Decimal("0") <= room <= zone:
                    return False, "BUY_CALL is too close to unbroken resistance"
                if room > Decimal("0") and room < self._minimum_room(analytics, zone):
                    return False, "insufficient room before resistance"
                return True, "bullish intrarange momentum has room to resistance"

            if resistance is not None and abs(spot - resistance) <= zone:
                if support is None:
                    return False, "range setup has no support boundary"
                if spot - support < self._minimum_room(analytics, zone):
                    return False, "insufficient room before support"
                return True, "bearish rotation from resistance zone"
            if support is None:
                return False, "range setup has no support boundary"
            room = spot - support
            if room < Decimal("0"):
                return True, "bearish continuation is below support"
            if Decimal("0") <= room <= zone:
                return False, "BUY_PUT is too close to unbroken support"
            if room > Decimal("0") and room < self._minimum_room(analytics, zone):
                return False, "insufficient room before support"
            return True, "bearish intrarange momentum has room to support"

        return False, "candidate has no recognized structured setup"

    def _level_zone(self, analytics: AnalyticsSnapshot) -> Decimal:
        straddle_zone = (
            (analytics.atm_straddle_price or Decimal("0"))
            * self._settings.straddle_zone_ratio
        )
        return max(self._settings.max_level_distance, straddle_zone)

    def _minimum_room(
        self,
        analytics: AnalyticsSnapshot,
        zone: Decimal,
    ) -> Decimal:
        straddle_room = (
            (analytics.atm_straddle_price or Decimal("0"))
            * Decimal("0.15")
        )
        return max(self._settings.min_range_room_points, zone, straddle_room)

    def _score(
        self,
        *,
        setup_type: SignalSetup,
        average_micro_confidence: Decimal,
        confirmation_count: int,
        structure_reason: str,
        analytics: AnalyticsSnapshot,
    ) -> tuple[Decimal, tuple[str, ...]]:
        setup_score = (
            Decimal("25")
            if setup_type
            in {
                SignalSetup.DERIVATIVES_QUANT,
                SignalSetup.OPTION_CHAIN_IMPULSE,
                SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
            }
            else Decimal("20")
            if setup_type in {SignalSetup.BREAKOUT, SignalSetup.LEVEL_REVERSAL}
            else Decimal("15")
        )
        micro_score = min(Decimal("25"), average_micro_confidence * Decimal("25"))
        required_micro_confirmations = (
            self._settings.profile_min_confirmations
            if self._settings.profile_min_confirmations is not None
            else self._settings.min_confirmations
        )
        if setup_type in {
            SignalSetup.DERIVATIVES_QUANT,
            SignalSetup.OPTION_CHAIN_IMPULSE,
            SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
        }:
            required_micro_confirmations = (
                self._settings.quant_min_option_confirmations
                if self._settings.quant_require_target_option_confirmation
                else 0
            ) + (
                self._settings.quant_min_futures_confirmations
                if self._settings.quant_require_futures_confirmation
                else 0
            )
        persistence_ratio = min(
            Decimal("1"),
            Decimal(confirmation_count)
            / Decimal(max(1, required_micro_confirmations)),
        )
        persistence_score = persistence_ratio * Decimal("20")
        directional_score = min(
            Decimal("10"),
            Decimal(len(analytics.directional_confirmations)) * Decimal("3"),
        )
        conflict_penalty = min(
            Decimal("15"),
            Decimal(len(analytics.directional_conflicts)) * Decimal("5"),
        )
        evidence_families = {
            item.family
            for item in analytics.directional_evidence
            if item.side in {None, analytics.signal}
            and item.strength > 0
        }
        alternative_confirmation_score = (
            Decimal("25")
            if (
                setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
                and EvidenceFamily.PRICE_ACTION in evidence_families
                and EvidenceFamily.POSITIONING in evidence_families
            )
            else Decimal("0")
        )
        score = (
            Decimal("10")  # chain identity/data-integrity checks
            + Decimal("25")  # setup-specific structural validation
            + setup_score
            + micro_score
            + persistence_score
            + directional_score
            + alternative_confirmation_score
            - conflict_penalty
        ).quantize(Decimal("0.1"))
        evidence = (
            f"setup={setup_type.value}",
            f"regime={analytics.market_regime.value}",
            structure_reason,
            f"micro_confidence={average_micro_confidence:.4f}",
            f"fresh_confirmations={confirmation_count}",
            (
                "microstructure=setup_optional"
                if (
                    setup_type == SignalSetup.LOCAL_LEVEL_REVERSAL
                    and confirmation_count == 0
                )
                else "microstructure=confirmed"
            ),
            f"directional_confirmations={','.join(analytics.directional_confirmations) or 'none'}",
            f"directional_conflicts={','.join(analytics.directional_conflicts) or 'none'}",
            "chain_identity_valid",
        )
        return score, evidence


def _target_quote(
    snapshot: OptionChainSnapshot,
    analytics: AnalyticsSnapshot,
) -> OptionQuote | None:
    if analytics.target_strike is None or analytics.target_option_type is None:
        return None
    return next(
        (
            quote
            for quote in snapshot.quotes
            if quote.contract.strike == analytics.target_strike
            and quote.contract.option_type == analytics.target_option_type
        ),
        None,
    )


def _usable_quotes(
    snapshot: OptionChainSnapshot,
) -> list[OptionQuote]:
    return [
        quote
        for quote in snapshot.quotes
        if quote.contract.underlying == snapshot.underlying
        and quote.contract.expiry == snapshot.expiry
        and quote.ltp is not None
        and quote.oi is not None
        and quote.volume is not None
    ]


def _valid_bid_ask(quote: OptionQuote, max_spread: Decimal) -> bool:
    return (
        quote.bid is not None
        and quote.ask is not None
        and quote.bid > 0
        and quote.ask > 0
        and quote.ask >= quote.bid
        and quote.ask - quote.bid <= max_spread
    )


def _regime_setup_error(
    regime: MarketRegime,
    setup: SignalSetup,
    reason: str | None,
) -> str | None:
    if regime == MarketRegime.UNSTABLE_HIGH_VOL:
        return "unstable/high-volatility regime is no-trade"
    if regime == MarketRegime.UNKNOWN:
        return "market regime is unclassified"
    if setup in {
        SignalSetup.DERIVATIVES_QUANT,
        SignalSetup.OPTION_CHAIN_IMPULSE,
        SignalSetup.LIQUIDITY_SWEEP_RECLAIM,
    }:
        return None
    if setup == SignalSetup.BREAKOUT and regime != MarketRegime.TREND_BREAKOUT:
        return f"breakout strategy is incompatible with {regime.value}"
    if (
        setup in {SignalSetup.LEVEL_REVERSAL, SignalSetup.RANGE_ROTATION}
        and regime != MarketRegime.RANGE
    ):
        return f"range/reversal strategy is incompatible with {regime.value}"
    if (
        setup == SignalSetup.LOCAL_LEVEL_REVERSAL
        and regime
        not in {
            MarketRegime.RANGE,
            MarketRegime.TREND_BREAKOUT,
        }
    ):
        return f"local reversal is incompatible with {regime.value}"
    if setup == SignalSetup.MOMENTUM_EXPANSION and (
        "GAMMA " not in (reason or "").upper()
        or regime != MarketRegime.COMPRESSION
    ):
        return f"gamma expansion is incompatible with {regime.value}"
    if setup == SignalSetup.NONE or regime == MarketRegime.UNKNOWN:
        return "strategy setup or market regime is unclassified"
    return None


def _resolve_setup_type(analytics: AnalyticsSnapshot) -> SignalSetup:
    if analytics.setup_type != SignalSetup.NONE:
        return analytics.setup_type
    reason = (analytics.signal_reason or "").upper()
    if "BREAKOUT VALIDATED" in reason or "BREAKDOWN VALIDATED" in reason:
        return SignalSetup.BREAKOUT
    if (
        "EXHAUSTION REVERSAL" in reason
        or "EXHAUSTION TOP" in reason
        or "MEAN REVERSION" in reason
    ):
        return SignalSetup.LEVEL_REVERSAL
    if (
        "OPENING FAILURE REVERSAL" in reason
        or "LOCAL LEVEL REVERSAL" in reason
    ):
        return SignalSetup.LOCAL_LEVEL_REVERSAL
    if "GAMMA " in reason or "CHAIN VELOCITY" in reason:
        return SignalSetup.MOMENTUM_EXPANSION
    if "SMC LIQUIDITY SWEEP RECLAIM" in reason:
        return SignalSetup.LIQUIDITY_SWEEP_RECLAIM
    if "DIVERGENCE" in reason or "PCR" in reason or "INSTITUTIONAL PRE-TELL" in reason:
        return SignalSetup.RANGE_ROTATION
    return SignalSetup.NONE


def _chain_integrity_error(snapshot: OptionChainSnapshot) -> str | None:
    underlying = snapshot.underlying.upper()
    for quote in snapshot.quotes:
        contract = quote.contract
        trading_symbol = contract.token.trading_symbol.upper()
        if contract.underlying.upper() != underlying:
            return (
                f"chain contamination: {contract.underlying} contract in "
                f"{snapshot.underlying} snapshot"
            )
        if not _has_underlying_prefix(trading_symbol, underlying):
            return (
                f"chain contamination: {trading_symbol} does not belong to "
                f"{snapshot.underlying}"
            )
    return None


def _has_underlying_prefix(trading_symbol: str, underlying: str) -> bool:
    if not trading_symbol.startswith(underlying):
        return False
    remainder = trading_symbol[len(underlying):]
    return not remainder or not remainder[0].isalpha()
