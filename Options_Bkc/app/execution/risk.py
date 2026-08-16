from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.domain.models import OptionQuote, OptionType


@dataclass(frozen=True)
class PositionSizingSettings:
    account_capital: Decimal = Decimal("100000")
    risk_per_trade_percent: Decimal = Decimal("0.50")
    max_gross_exposure: Decimal = Decimal("100000")
    option_stop_loss_fraction: Decimal = Decimal("0.05")
    reward_risk_multiple: Decimal = Decimal("2")


@dataclass(frozen=True)
class PositionPlan:
    token: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    lot_size: int
    lots: int
    quantity: int
    capital_at_risk: Decimal
    gross_exposure: Decimal
    option_type: OptionType | None = None


class PositionSizer:
    """Size a long-option paper order from a fixed account-risk budget."""

    def __init__(self, settings: PositionSizingSettings | None = None) -> None:
        self._settings = settings or PositionSizingSettings()

    def size_long_option(self, quote: OptionQuote) -> PositionPlan | None:
        entry = quote.ask
        lot_size = quote.contract.lot_size
        if entry is None or entry <= 0 or lot_size is None or lot_size <= 0:
            return None

        stop = (entry * (Decimal("1") - self._settings.option_stop_loss_fraction)).quantize(
            Decimal("0.05")
        )
        risk_per_unit = entry - stop
        if risk_per_unit <= 0:
            return None

        risk_budget = (
            self._settings.account_capital
            * self._settings.risk_per_trade_percent
            / Decimal("100")
        )
        risk_per_lot = risk_per_unit * Decimal(lot_size)
        exposure_per_lot = entry * Decimal(lot_size)
        lots_by_risk = int(
            (risk_budget / risk_per_lot).to_integral_value(rounding=ROUND_FLOOR)
        )
        lots_by_exposure = int(
            (
                self._settings.max_gross_exposure / exposure_per_lot
            ).to_integral_value(rounding=ROUND_FLOOR)
        )
        lots = min(lots_by_risk, lots_by_exposure)
        if lots <= 0:
            return None

        quantity = lots * lot_size
        target = (
            entry + risk_per_unit * self._settings.reward_risk_multiple
        ).quantize(Decimal("0.05"))
        return PositionPlan(
            token=quote.contract.token.token,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            lot_size=lot_size,
            lots=lots,
            quantity=quantity,
            capital_at_risk=risk_per_unit * Decimal(quantity),
            gross_exposure=entry * Decimal(quantity),
            option_type=quote.contract.option_type,
        )
