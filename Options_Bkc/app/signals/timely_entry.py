from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.strategy_config import QuantMicrostructureSettings
from app.domain.models import (
    AnalyticsSnapshot,
    MarketTick,
    MicrostructureSignal,
    OptionChainSnapshot,
    OptionQuote,
    StrategyFamily,
)
from app.marketdata.depth_normalizer import normalize_order_book
from app.signals.gate import SignalGateDecision


@dataclass(frozen=True, slots=True)
class ArmedEntryCandidate:
    snapshot: OptionChainSnapshot
    analytics: AnalyticsSnapshot
    armed_at: datetime
    expires_at: datetime
    target_token: str
    initial_ask: Decimal
    refreshed_quote_tokens: frozenset[str]
    refreshed_greeks_tokens: frozenset[str]
    underlying_observed_at: datetime


@dataclass(frozen=True, slots=True)
class TimelyEntryTrigger:
    candidate: ArmedEntryCandidate
    signal: MicrostructureSignal
    captured_at: datetime
    bid: Decimal
    ask: Decimal
    ltp: Decimal
    premium_chase_percent: Decimal


class TimelyEntryGuard:
    """Arms fresh flow candidates and releases only immediate option events.

    The hot path is a single dictionary lookup. Book normalization is performed
    only when a microstructure signal matches an armed target contract.
    """

    __slots__ = ("_settings", "_market_timezone", "_cutoff", "_armed")

    def __init__(
        self,
        settings: QuantMicrostructureSettings,
        *,
        market_timezone: str,
    ) -> None:
        self._settings = settings
        self._market_timezone = ZoneInfo(market_timezone)
        self._cutoff = (
            time.fromisoformat(settings.event_entry_cutoff_time)
            if settings.event_entry_cutoff_time is not None
            else None
        )
        self._armed: dict[str, ArmedEntryCandidate] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.event_driven_entry

    def reset(self) -> None:
        self._armed.clear()

    def cancel(self, underlying: str) -> None:
        self._armed.pop(underlying.upper(), None)

    def microstructure_not_before(
        self,
        captured_at: datetime,
    ) -> datetime | None:
        return captured_at if self.enabled else None

    def arm_from_decision(
        self,
        *,
        snapshot: OptionChainSnapshot,
        analytics: AnalyticsSnapshot,
        decision: SignalGateDecision,
        refreshed_quote_tokens: set[str],
        refreshed_greeks_tokens: set[str],
        underlying_observed_at: datetime,
    ) -> ArmedEntryCandidate | None:
        if not self.enabled:
            return None

        underlying = snapshot.underlying.upper()
        current = self._armed.get(underlying)
        if current is not None and current.expires_at < snapshot.captured_at:
            self._armed.pop(underlying, None)
            current = None

        if (
            decision.qualified
            or analytics.selected_strategy
            not in {
                StrategyFamily.DERIVATIVES_QUANT,
                StrategyFamily.OPTION_CHAIN_IMPULSE,
                StrategyFamily.SMC,
            }
            or analytics.signal not in {"BUY_CALL", "BUY_PUT"}
            or "target_option_liquidity_missing" not in decision.evidence
        ):
            self._armed.pop(underlying, None)
            return None

        quote = _target_quote(snapshot, analytics)
        initial_ask = (
            quote.ask
            if quote is not None and quote.ask is not None
            else quote.ltp if quote is not None else None
        )
        if quote is None or initial_ask is None or initial_ask <= 0:
            self._armed.pop(underlying, None)
            return None
        if self._after_cutoff(snapshot.captured_at):
            self._armed.pop(underlying, None)
            return None

        if (
            current is not None
            and current.target_token == quote.contract.token.token
            and current.analytics.signal == analytics.signal
        ):
            return current

        armed = ArmedEntryCandidate(
            snapshot=snapshot,
            analytics=analytics,
            armed_at=snapshot.captured_at,
            expires_at=snapshot.captured_at
            + timedelta(seconds=self._settings.candidate_ttl_seconds),
            target_token=quote.contract.token.token,
            initial_ask=initial_ask,
            refreshed_quote_tokens=frozenset(refreshed_quote_tokens),
            refreshed_greeks_tokens=frozenset(refreshed_greeks_tokens),
            underlying_observed_at=underlying_observed_at,
        )
        self._armed[underlying] = armed
        return armed

    def consider(
        self,
        *,
        tick: MarketTick,
        signal: MicrostructureSignal,
    ) -> TimelyEntryTrigger | None:
        if not self.enabled:
            return None
        candidate = self._armed.get(signal.underlying.upper())
        if candidate is None:
            return None

        captured_at = tick.received_at
        if captured_at < candidate.armed_at:
            return None
        if captured_at > candidate.expires_at or self._after_cutoff(captured_at):
            self._armed.pop(signal.underlying.upper(), None)
            return None
        if (
            signal.token.token != candidate.target_token
            or signal.side != candidate.analytics.signal
        ):
            return None

        signal_age = (captured_at - signal.captured_at).total_seconds()
        if (
            signal_age < 0
            or signal_age > self._settings.maximum_age_seconds
        ):
            return None

        book = normalize_order_book(tick)
        if book is None or tick.ltp is None or tick.ltp <= 0:
            return None
        bid = book.bids[0].price
        ask = book.asks[0].price
        chase = (
            (ask / candidate.initial_ask - Decimal("1"))
            * Decimal("100")
        ).quantize(Decimal("0.0001"))
        if (
            self._settings.minimum_candidate_premium_chase_percent is not None
            and chase
            < self._settings.minimum_candidate_premium_chase_percent
        ) or chase > self._settings.maximum_candidate_premium_chase_percent:
            self._armed.pop(signal.underlying.upper(), None)
            return None

        self._armed.pop(signal.underlying.upper(), None)
        return TimelyEntryTrigger(
            candidate=candidate,
            signal=signal,
            captured_at=captured_at,
            bid=bid,
            ask=ask,
            ltp=tick.ltp,
            premium_chase_percent=chase,
        )

    def _after_cutoff(self, captured_at: datetime) -> bool:
        if self._cutoff is None:
            return False
        local_time = captured_at.astimezone(self._market_timezone).time()
        return local_time.replace(tzinfo=None) > self._cutoff


def _target_quote(
    snapshot: OptionChainSnapshot,
    analytics: AnalyticsSnapshot,
) -> OptionQuote | None:
    target_strike = analytics.target_strike
    target_type = analytics.target_option_type
    if target_strike is None or target_type is None:
        return None
    return next(
        (
            quote
            for quote in snapshot.quotes
            if quote.contract.strike == target_strike
            and quote.contract.option_type == target_type
        ),
        None,
    )
