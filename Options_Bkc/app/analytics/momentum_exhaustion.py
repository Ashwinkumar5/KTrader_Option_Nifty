from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.domain.models import (
    ExhaustionState,
    ExpectedMoveContext,
    MomentumExhaustionContext,
    OptionChainSnapshot,
    OptionType,
    PremiumResponse,
    TradeManagementAction,
)


@dataclass(frozen=True)
class MomentumExhaustionSettings:
    earliest_time: time = time(13, 15)
    minimum_premium_return_percent: Decimal = Decimal("75")
    minimum_move_utilization: Decimal = Decimal("0.80")
    residual_failure_points: Decimal = Decimal("0.50")
    maximum_spread_ratio: Decimal = Decimal("0.03")
    market_timezone: str = "Asia/Kolkata"


class MomentumExhaustionTracker:
    """Emit management advisories; never create an opposite entry by itself."""

    def __init__(
        self,
        settings: MomentumExhaustionSettings | None = None,
    ) -> None:
        self._settings = settings or MomentumExhaustionSettings()
        self._timezone = ZoneInfo(self._settings.market_timezone)

    def update(
        self,
        *,
        snapshot: OptionChainSnapshot,
        expected_move: ExpectedMoveContext,
        responses: tuple[PremiumResponse, ...],
    ) -> MomentumExhaustionContext:
        market_time = snapshot.captured_at.astimezone(self._timezone).time()
        if market_time < self._settings.earliest_time:
            return MomentumExhaustionContext(
                reason="afternoon exhaustion window has not opened"
            )
        if (
            not expected_move.available
            or expected_move.utilization is None
            or expected_move.utilization
            < self._settings.minimum_move_utilization
        ):
            return MomentumExhaustionContext(
                reason="expected-move utilization is below exhaustion threshold"
            )

        eligible = [
            item
            for item in responses
            if item.return_percent is not None
            and item.return_percent
            >= self._settings.minimum_premium_return_percent
        ]
        if not eligible:
            return MomentumExhaustionContext(
                reason="no option has a sufficiently extended session return"
            )
        winning = max(
            eligible,
            key=lambda item: item.return_percent or Decimal("0"),
        )
        side = (
            "BUY_CALL"
            if winning.option_type == OptionType.CALL
            else "BUY_PUT"
        )
        opposite = "BUY_PUT" if side == "BUY_CALL" else "BUY_CALL"

        quote = next(
            (
                item
                for item in snapshot.quotes
                if item.contract.token.token == winning.token
            ),
            None,
        )
        mid = (
            (quote.bid + quote.ask) / Decimal("2")
            if quote is not None
            and quote.bid is not None
            and quote.ask is not None
            and quote.bid > 0
            and quote.ask >= quote.bid
            else None
        )
        if (
            winning.spread is not None
            and mid is not None
            and mid > 0
            and winning.spread / mid > self._settings.maximum_spread_ratio
        ):
            return MomentumExhaustionContext(
                state=ExhaustionState.LIQUIDITY_DISTORTION,
                winning_side=side,
                opposite_side=opposite,
                action=TradeManagementAction.TIGHTEN_STOP,
                reason="extended premium has developed an excessive spread",
            )

        if (
            winning.iv_change is not None
            and winning.iv_change < 0
            and winning.residual_change
            <= -self._settings.residual_failure_points
        ):
            return MomentumExhaustionContext(
                state=ExhaustionState.IV_CRUSH_ONLY,
                winning_side=side,
                opposite_side=opposite,
                action=TradeManagementAction.TIGHTEN_STOP,
                reason=(
                    "winning premium is underperforming its Greek response "
                    "while IV contracts"
                ),
            )

        if (
            winning.premium_change <= 0
            and winning.residual_change
            <= -self._settings.residual_failure_points
        ):
            return MomentumExhaustionContext(
                state=ExhaustionState.DIRECTIONAL_EXHAUSTION,
                winning_side=side,
                opposite_side=opposite,
                action=TradeManagementAction.EXIT_OR_TIGHTEN,
                reason=(
                    "extended winning premium stopped advancing and "
                    "underperformed its Greek-implied response"
                ),
            )
        return MomentumExhaustionContext(
            state=ExhaustionState.EARLY_WARNING,
            winning_side=side,
            opposite_side=opposite,
            action=TradeManagementAction.TIGHTEN_STOP,
            reason="expected move and premium extension warrant tighter risk",
        )
