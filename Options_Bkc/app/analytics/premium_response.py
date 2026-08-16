from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.domain.models import OptionChainSnapshot, PremiumResponse


@dataclass(frozen=True)
class PremiumResponseSettings:
    max_contract_states: int = 256

    def __post_init__(self) -> None:
        if self.max_contract_states <= 0:
            raise ValueError("max_contract_states must be positive")


@dataclass
class _PremiumState:
    captured_at: datetime
    baseline_price: Decimal
    price: Decimal
    spot: Decimal
    iv: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    vega: Decimal | None
    theta: Decimal | None
    favorable_anchor_price: Decimal
    favorable_expected_change: Decimal = Decimal("0")
    directional_anchor_price: Decimal = Decimal("0")
    favorable_directional_expected_change: Decimal = Decimal("0")


class PremiumResponseTracker:
    """Incremental Greek attribution for current option-window contracts."""

    def __init__(self, settings: PremiumResponseSettings | None = None) -> None:
        self._settings = settings or PremiumResponseSettings()
        self._states: dict[str, dict[str, _PremiumState]] = {}
        self._session_dates: dict[str, date] = {}
        self._last_captured_at: dict[str, datetime] = {}

    def update(
        self,
        snapshot: OptionChainSnapshot,
    ) -> tuple[PremiumResponse, ...]:
        key = snapshot.underlying.upper()
        session_date = snapshot.captured_at.date()
        last_captured_at = self._last_captured_at.get(key)
        if (
            self._session_dates.get(key) != session_date
            or (
                last_captured_at is not None
                and snapshot.captured_at < last_captured_at
            )
        ):
            self._states[key] = {}
            self._session_dates[key] = session_date
        self._last_captured_at[key] = snapshot.captured_at
        states = self._states.setdefault(key, {})
        responses: list[PremiumResponse] = []

        for quote in snapshot.quotes:
            price = _mid_or_ltp(quote.bid, quote.ask, quote.ltp)
            if price is None:
                continue
            token = quote.contract.token.token
            greeks = quote.greeks
            current = _PremiumState(
                captured_at=snapshot.captured_at,
                baseline_price=price,
                price=price,
                spot=snapshot.spot_price,
                iv=greeks.implied_volatility if greeks else None,
                delta=greeks.delta if greeks else None,
                gamma=greeks.gamma if greeks else None,
                vega=greeks.vega if greeks else None,
                theta=greeks.theta if greeks else None,
                favorable_anchor_price=price,
                directional_anchor_price=price,
            )
            previous = states.get(token)
            if previous is None or snapshot.captured_at <= previous.captured_at:
                states[token] = current
                continue

            current.baseline_price = previous.baseline_price
            spot_change = snapshot.spot_price - previous.spot
            iv_change = (
                current.iv - previous.iv
                if current.iv is not None and previous.iv is not None
                else None
            )
            elapsed_seconds = Decimal(
                str(
                    (
                        snapshot.captured_at - previous.captured_at
                    ).total_seconds()
                )
            )
            expected = Decimal("0")
            directional_expected = Decimal("0")
            if previous.delta is not None:
                directional_expected += previous.delta * spot_change
            if previous.gamma is not None:
                directional_expected += (
                    Decimal("0.5")
                    * previous.gamma
                    * spot_change
                    * spot_change
                )
            expected += directional_expected
            if previous.vega is not None and iv_change is not None:
                expected += previous.vega * iv_change
            if previous.theta is not None:
                expected += (
                    previous.theta
                    * elapsed_seconds
                    / Decimal("86400")
                )
            change = price - previous.price
            if expected > 0:
                current.favorable_anchor_price = (
                    previous.favorable_anchor_price
                    if previous.favorable_expected_change > 0
                    else previous.price
                )
                current.favorable_expected_change = (
                    previous.favorable_expected_change + expected
                )
            favorable_actual_change = (
                price - current.favorable_anchor_price
                if current.favorable_expected_change > 0
                else None
            )
            expected_return_percent = (
                current.favorable_expected_change
                / current.favorable_anchor_price
                * Decimal("100")
                if current.favorable_expected_change > 0
                and current.favorable_anchor_price > 0
                else None
            )
            transmission_ratio = (
                favorable_actual_change
                / current.favorable_expected_change
                if favorable_actual_change is not None
                and current.favorable_expected_change > 0
                else None
            )
            if directional_expected > 0:
                current.directional_anchor_price = (
                    previous.directional_anchor_price
                    if previous.favorable_directional_expected_change > 0
                    else previous.price
                )
                current.favorable_directional_expected_change = (
                    previous.favorable_directional_expected_change
                    + directional_expected
                )
            directional_actual_change = (
                price - current.directional_anchor_price
                if current.favorable_directional_expected_change > 0
                else None
            )
            directional_expected_return_percent = (
                current.favorable_directional_expected_change
                / current.directional_anchor_price
                * Decimal("100")
                if current.favorable_directional_expected_change > 0
                and current.directional_anchor_price > 0
                else None
            )
            directional_transmission_ratio = (
                directional_actual_change
                / current.favorable_directional_expected_change
                if directional_actual_change is not None
                and current.favorable_directional_expected_change > 0
                else None
            )
            responses.append(
                PremiumResponse(
                    token=token,
                    option_type=quote.contract.option_type,
                    captured_at=snapshot.captured_at,
                    premium_change=change,
                    return_percent=(
                        (price / previous.baseline_price - Decimal("1"))
                        * Decimal("100")
                        if previous.baseline_price > 0
                        else None
                    ),
                    expected_change=expected,
                    residual_change=change - expected,
                    spot_change=spot_change,
                    iv_change=iv_change,
                    spread=(
                        quote.ask - quote.bid
                        if quote.ask is not None and quote.bid is not None
                        else None
                    ),
                    favorable_actual_change=favorable_actual_change,
                    favorable_expected_change=(
                        current.favorable_expected_change
                        if current.favorable_expected_change > 0
                        else None
                    ),
                    expected_return_percent=expected_return_percent,
                    transmission_ratio=transmission_ratio,
                    favorable_directional_actual_change=(
                        directional_actual_change
                    ),
                    favorable_directional_expected_change=(
                        current.favorable_directional_expected_change
                        if current.favorable_directional_expected_change > 0
                        else None
                    ),
                    directional_expected_return_percent=(
                        directional_expected_return_percent
                    ),
                    directional_transmission_ratio=(
                        directional_transmission_ratio
                    ),
                )
            )
            states[token] = current

        if len(states) > self._settings.max_contract_states:
            oldest = sorted(
                states,
                key=lambda token: states[token].captured_at,
            )
            for token in oldest[
                : len(states) - self._settings.max_contract_states
            ]:
                del states[token]
        return tuple(responses)

    def reset(self) -> None:
        self._states.clear()
        self._session_dates.clear()
        self._last_captured_at.clear()


def _mid_or_ltp(
    bid: Decimal | None,
    ask: Decimal | None,
    ltp: Decimal | None,
) -> Decimal | None:
    if (
        bid is not None
        and ask is not None
        and bid > 0
        and ask >= bid
    ):
        return (bid + ask) / Decimal("2")
    return ltp if ltp is not None and ltp > 0 else None
