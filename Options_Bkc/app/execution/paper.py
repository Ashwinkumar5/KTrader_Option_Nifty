from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    MarketTick,
    MomentumExhaustionContext,
    OptionChainSnapshot,
    OptionType,
    TradeManagementAction,
)
from app.execution.risk import PositionPlan
from app.marketdata.depth_normalizer import normalize_order_book


@dataclass(frozen=True)
class PaperFill:
    token: str
    action: str
    price: Decimal
    quantity: int
    captured_at: datetime
    reason: str
    realized_pnl: Decimal = Decimal("0")
    maximum_favorable_excursion_percent: Decimal = Decimal("0")
    maximum_adverse_excursion_percent: Decimal = Decimal("0")


@dataclass
class _PaperPosition:
    plan: PositionPlan
    opened_at: datetime
    highest_mark: Decimal
    lowest_mark: Decimal
    latest_mark: Decimal


class PaperExecutionEngine:
    """Deterministic long-option simulator; it never contacts a live broker."""

    def __init__(
        self,
        *,
        max_positions: int = 1,
        maximum_holding_minutes: int = 15,
        trailing_activation_percent: Decimal | None = None,
        trailing_drawdown_percent: Decimal | None = None,
        no_follow_through_seconds: int | None = None,
        minimum_follow_through_percent: Decimal | None = None,
    ) -> None:
        self._max_positions = max(1, max_positions)
        self._maximum_holding = timedelta(
            minutes=max(1, maximum_holding_minutes)
        )
        self._trailing_activation_percent = trailing_activation_percent
        self._trailing_drawdown_percent = trailing_drawdown_percent
        self._no_follow_through = (
            timedelta(seconds=no_follow_through_seconds)
            if no_follow_through_seconds is not None
            else None
        )
        self._minimum_follow_through_percent = (
            minimum_follow_through_percent
        )
        self._positions: dict[str, _PaperPosition] = {}
        self._realized_pnl = Decimal("0")

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def open_positions(self) -> int:
        return len(self._positions)

    @property
    def gross_exposure(self) -> Decimal:
        return sum(
            (
                position.plan.gross_exposure
                for position in self._positions.values()
            ),
            Decimal("0"),
        )

    def submit(self, plan: PositionPlan, captured_at: datetime) -> PaperFill | None:
        if (
            plan.token in self._positions
            or len(self._positions) >= self._max_positions
        ):
            return None
        self._positions[plan.token] = _PaperPosition(
            plan=plan,
            opened_at=captured_at,
            highest_mark=plan.entry_price,
            lowest_mark=plan.entry_price,
            latest_mark=plan.entry_price,
        )
        return PaperFill(
            token=plan.token,
            action="BUY",
            price=plan.entry_price,
            quantity=plan.quantity,
            captured_at=captured_at,
            reason="qualified strong signal",
        )

    def mark(self, snapshot: OptionChainSnapshot) -> tuple[PaperFill, ...]:
        prices = {
            quote.contract.token.token: (
                quote.bid if quote.bid is not None and quote.bid > 0 else quote.ltp
            )
            for quote in snapshot.quotes
            if quote.ltp is not None
        }
        return self._mark_prices(prices, snapshot.captured_at)

    def mark_tick(self, tick: MarketTick) -> tuple[PaperFill, ...]:
        """Mark an open option on every replay tick using executable bid."""

        if tick.token.token not in self._positions:
            return ()
        book = normalize_order_book(tick)
        best_bid = (
            book.bids[0].price
            if book is not None and book.bids
            else tick.bid
        )
        current = (
            best_bid
            if best_bid is not None and best_bid > 0
            else tick.ltp
        )
        if current is None or current <= 0:
            return ()
        return self._mark_prices(
            {tick.token.token: current},
            tick.received_at,
        )

    def close_all(
        self,
        captured_at: datetime,
        *,
        reason: str,
    ) -> tuple[PaperFill, ...]:
        """Close replay positions at their latest executable mark."""

        return tuple(
            self._close_position(
                token,
                position,
                current=position.latest_mark,
                captured_at=captured_at,
                reason=reason,
            )
            for token, position in tuple(self._positions.items())
        )

    def _mark_prices(
        self,
        prices: dict[str, Decimal | None],
        captured_at: datetime,
    ) -> tuple[PaperFill, ...]:
        fills: list[PaperFill] = []
        for token, position in tuple(self._positions.items()):
            current = prices.get(token)
            if current is None or current <= 0:
                continue
            position.latest_mark = current
            position.highest_mark = max(position.highest_mark, current)
            position.lowest_mark = min(position.lowest_mark, current)
            highest_return = _return_percent(
                position.plan.entry_price,
                position.highest_mark,
            )
            if current <= position.plan.stop_price:
                reason = "stop"
            elif current >= position.plan.target_price:
                reason = "target"
            elif self._trailing_exit(position, current, highest_return):
                reason = "trend_trailing_exit"
            elif (
                self._no_follow_through is not None
                and self._minimum_follow_through_percent is not None
                and captured_at - position.opened_at
                >= self._no_follow_through
                and highest_return
                < self._minimum_follow_through_percent
            ):
                reason = "no_follow_through"
            elif captured_at - position.opened_at >= self._maximum_holding:
                reason = "time_exit"
            else:
                continue
            fills.append(
                self._close_position(
                    token,
                    position,
                    current=current,
                    captured_at=captured_at,
                    reason=reason,
                )
            )
        return tuple(fills)

    def _trailing_exit(
        self,
        position: _PaperPosition,
        current: Decimal,
        highest_return: Decimal,
    ) -> bool:
        if (
            self._trailing_activation_percent is None
            or self._trailing_drawdown_percent is None
            or highest_return < self._trailing_activation_percent
            or position.highest_mark <= 0
        ):
            return False
        drawdown_from_peak = (
            (position.highest_mark - current)
            / position.highest_mark
            * Decimal("100")
        )
        return drawdown_from_peak >= self._trailing_drawdown_percent

    def _close_position(
        self,
        token: str,
        position: _PaperPosition,
        *,
        current: Decimal,
        captured_at: datetime,
        reason: str,
    ) -> PaperFill:
        pnl = (
            current - position.plan.entry_price
        ) * Decimal(position.plan.quantity)
        self._realized_pnl += pnl
        del self._positions[token]
        return PaperFill(
            token=token,
            action="SELL",
            price=current,
            quantity=position.plan.quantity,
            captured_at=captured_at,
            reason=reason,
            realized_pnl=pnl,
            maximum_favorable_excursion_percent=_return_percent(
                position.plan.entry_price,
                position.highest_mark,
            ),
            maximum_adverse_excursion_percent=_return_percent(
                position.plan.entry_price,
                position.lowest_mark,
            ),
        )

    def apply_management(
        self,
        snapshot: OptionChainSnapshot,
        context: MomentumExhaustionContext | None,
    ) -> tuple[PaperFill, ...]:
        if context is None or context.winning_side is None:
            return ()
        option_type = (
            OptionType.CALL
            if context.winning_side == "BUY_CALL"
            else OptionType.PUT
        )
        prices = {
            quote.contract.token.token: (
                quote.bid
                if quote.bid is not None and quote.bid > 0
                else quote.ltp
            )
            for quote in snapshot.quotes
            if quote.ltp is not None
        }
        fills: list[PaperFill] = []
        for token, position in tuple(self._positions.items()):
            if position.plan.option_type != option_type:
                continue
            current = prices.get(token)
            if current is None:
                continue
            position.latest_mark = current
            position.highest_mark = max(position.highest_mark, current)
            position.lowest_mark = min(position.lowest_mark, current)
            if context.action == TradeManagementAction.TIGHTEN_STOP:
                position.plan = replace(
                    position.plan,
                    stop_price=max(
                        position.plan.stop_price,
                        position.plan.entry_price,
                    ),
                )
                continue
            if context.action != TradeManagementAction.EXIT_OR_TIGHTEN:
                continue
            pnl = (
                current - position.plan.entry_price
            ) * Decimal(position.plan.quantity)
            self._realized_pnl += pnl
            del self._positions[token]
            fills.append(
                PaperFill(
                    token=token,
                    action="SELL",
                    price=current,
                    quantity=position.plan.quantity,
                    captured_at=snapshot.captured_at,
                    reason=f"momentum_exhaustion:{context.state.value}",
                    realized_pnl=pnl,
                    maximum_favorable_excursion_percent=(
                        _return_percent(
                            position.plan.entry_price,
                            position.highest_mark,
                        )
                    ),
                    maximum_adverse_excursion_percent=(
                        _return_percent(
                            position.plan.entry_price,
                            position.lowest_mark,
                        )
                    ),
                )
            )
        return tuple(fills)


def _return_percent(entry: Decimal, mark: Decimal) -> Decimal:
    if entry <= 0:
        return Decimal("0")
    return (
        (mark - entry) / entry * Decimal("100")
    ).quantize(Decimal("0.0001"))
