from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    ExhaustionState,
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    MomentumExhaustionContext,
    TradeManagementAction,
)
from app.execution.paper import PaperExecutionEngine
from app.execution.risk import PositionSizer, PositionSizingSettings


def _quote(
    ltp: str = "100",
    ask: str = "100",
    bid: str = "99.5",
) -> OptionQuote:
    contract = OptionContract(
        underlying="NIFTY",
        expiry=date(2026, 7, 30),
        strike=Decimal("24250"),
        option_type=OptionType.CALL,
        token=InstrumentToken(
            exchange=Exchange.NFO,
            token="CE1",
            symbol="NIFTY",
            trading_symbol="NIFTY30JUL2624250CE",
        ),
        lot_size=50,
    )
    return OptionQuote(
        contract=contract,
        ltp=Decimal(ltp),
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


class PositionSizerTests(unittest.TestCase):
    def test_exhaustion_management_exits_only_the_winning_option_side(self) -> None:
        at = datetime(2026, 7, 22, 13, 30, tzinfo=UTC)
        quote = _quote()
        plan = PositionSizer(
            PositionSizingSettings(risk_per_trade_percent=Decimal("2"))
        ).size_long_option(quote)
        self.assertIsNotNone(plan)
        engine = PaperExecutionEngine()
        self.assertIsNotNone(engine.submit(plan, at))
        marked_quote = replace(quote, ltp=Decimal("110"))
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=quote.contract.expiry,
            spot_price=Decimal("24200"),
            atm_strike=quote.contract.strike,
            captured_at=at + timedelta(seconds=15),
            quotes=(marked_quote,),
        )

        fills = engine.apply_management(
            snapshot,
            MomentumExhaustionContext(
                state=ExhaustionState.DIRECTIONAL_EXHAUSTION,
                winning_side="BUY_CALL",
                opposite_side="BUY_PUT",
                action=TradeManagementAction.EXIT_OR_TIGHTEN,
            ),
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reason, "momentum_exhaustion:DIRECTIONAL_EXHAUSTION")
        self.assertEqual(engine.open_positions, 0)

    def test_sizes_whole_lots_with_bounded_risk_and_exposure(self) -> None:
        sizer = PositionSizer(
            PositionSizingSettings(
                account_capital=Decimal("100000"),
                risk_per_trade_percent=Decimal("2"),
                max_gross_exposure=Decimal("50000"),
            )
        )

        plan = sizer.size_long_option(_quote())

        self.assertIsNotNone(plan)
        self.assertEqual(plan.lots, 8)
        self.assertEqual(plan.quantity, 400)
        self.assertEqual(plan.capital_at_risk, Decimal("2000.00"))
        self.assertEqual(plan.gross_exposure, Decimal("40000"))

    def test_paper_engine_closes_at_target_and_updates_pnl(self) -> None:
        sizer = PositionSizer(
            PositionSizingSettings(
                account_capital=Decimal("200000"),
                risk_per_trade_percent=Decimal("1"),
                max_gross_exposure=Decimal("100000"),
            )
        )
        plan = sizer.size_long_option(_quote())
        assert plan is not None
        engine = PaperExecutionEngine(max_positions=1)
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        opened = engine.submit(plan, at)
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24250"),
            atm_strike=Decimal("24250"),
            captured_at=at,
            quotes=(
                _quote(
                    ltp=str(plan.target_price),
                    bid=str(plan.target_price),
                ),
            ),
        )

        closed = engine.mark(snapshot)

        self.assertIsNotNone(opened)
        self.assertEqual(closed[0].reason, "target")
        self.assertGreater(engine.realized_pnl, Decimal("0"))
        self.assertEqual(engine.open_positions, 0)

    def test_paper_engine_uses_fifteen_minute_time_exit_at_bid(self) -> None:
        plan = PositionSizer().size_long_option(_quote())
        assert plan is not None
        engine = PaperExecutionEngine(maximum_holding_minutes=15)
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        engine.submit(plan, at)
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24250"),
            atm_strike=Decimal("24250"),
            captured_at=at + timedelta(minutes=15),
            quotes=(_quote(ltp="108", bid="107.5", ask="108.5"),),
        )

        closed = engine.mark(snapshot)

        self.assertEqual(closed[0].reason, "time_exit")
        self.assertEqual(closed[0].price, Decimal("107.5"))

    def test_paper_engine_records_bid_based_mfe_and_mae(self) -> None:
        plan = PositionSizer().size_long_option(_quote())
        assert plan is not None
        engine = PaperExecutionEngine()
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        engine.submit(plan, at)

        favorable = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24250"),
            atm_strike=Decimal("24250"),
            captured_at=at + timedelta(seconds=15),
            quotes=(_quote(ltp="105", bid="105", ask="105.5"),),
        )
        self.assertEqual(engine.mark(favorable), ())

        adverse = replace(
            favorable,
            captured_at=at + timedelta(seconds=30),
            quotes=(_quote(ltp="94", bid="94", ask="94.5"),),
        )
        closed = engine.mark(adverse)

        self.assertEqual(closed[0].reason, "stop")
        self.assertEqual(
            closed[0].maximum_favorable_excursion_percent,
            Decimal("5.0000"),
        )
        self.assertEqual(
            closed[0].maximum_adverse_excursion_percent,
            Decimal("-6.0000"),
        )

    def test_paper_engine_closes_from_websocket_bid_tick(self) -> None:
        plan = PositionSizer().size_long_option(_quote())
        assert plan is not None
        engine = PaperExecutionEngine()
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        engine.submit(plan, at)
        tick = MarketTick(
            token=replace(
                _quote().contract.token,
                kind=InstrumentKind.OPTION,
            ),
            exchange_timestamp=at + timedelta(seconds=4),
            received_at=at + timedelta(seconds=4),
            ltp=plan.target_price + Decimal("0.50"),
            bid=plan.target_price,
            ask=plan.target_price + Decimal("0.05"),
        )

        fills = engine.mark_tick(tick)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reason, "target")
        self.assertEqual(fills[0].price, plan.target_price)

    def test_paper_engine_trails_a_profitable_option_trend(self) -> None:
        plan = PositionSizer().size_long_option(_quote())
        assert plan is not None
        plan = replace(plan, target_price=Decimal("200"))
        engine = PaperExecutionEngine(
            maximum_holding_minutes=180,
            trailing_activation_percent=Decimal("5"),
            trailing_drawdown_percent=Decimal("3"),
        )
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        engine.submit(plan, at)
        favorable = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24250"),
            atm_strike=Decimal("24250"),
            captured_at=at + timedelta(seconds=30),
            quotes=(_quote(ltp="106", bid="106", ask="106.5"),),
        )
        self.assertEqual(engine.mark(favorable), ())

        pullback = replace(
            favorable,
            captured_at=at + timedelta(seconds=45),
            quotes=(_quote(ltp="102.5", bid="102.5", ask="103"),),
        )
        fills = engine.mark(pullback)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reason, "trend_trailing_exit")
        self.assertEqual(fills[0].price, Decimal("102.5"))

    def test_paper_engine_exits_when_entry_has_no_follow_through(self) -> None:
        plan = PositionSizer().size_long_option(_quote())
        assert plan is not None
        plan = replace(plan, target_price=Decimal("200"))
        engine = PaperExecutionEngine(
            maximum_holding_minutes=180,
            no_follow_through_seconds=120,
            minimum_follow_through_percent=Decimal("1"),
        )
        at = datetime(2026, 7, 22, 9, 30, tzinfo=UTC)
        engine.submit(plan, at)
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24250"),
            atm_strike=Decimal("24250"),
            captured_at=at + timedelta(seconds=120),
            quotes=(_quote(ltp="100.5", bid="100.5", ask="101"),),
        )

        fills = engine.mark(snapshot)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].reason, "no_follow_through")


if __name__ == "__main__":
    unittest.main()
