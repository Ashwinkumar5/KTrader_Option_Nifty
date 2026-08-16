from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ktrader_simulator.domain.models import Instrument, OptionInstrument, OptionType, Quote
from ktrader_simulator.storage.ledger import JsonlTradeJournal
from ktrader_simulator.trading.engine import SimulatorEngine
from ktrader_simulator.trading.models import (
    ExecutionEvent,
    ExecutionEventKind,
    ExitReason,
    OrderRequest,
    OrderSource,
    OrderType,
    RiskParameters,
)

NOW = datetime(2026, 8, 1, 4, 0, tzinfo=UTC)


def _option() -> OptionInstrument:
    return OptionInstrument(
        underlying="NIFTY",
        expiry=date(2026, 8, 4),
        strike=Decimal("24500"),
        option_type=OptionType.CALL,
        instrument=Instrument(
            exchange="NFO",
            token="12345",
            trading_symbol="NIFTY04AUG2624500CE",
        ),
        lot_size=65,
    )


def _quote(*, ltp: str, bid: str, ask: str) -> Quote:
    return Quote(
        token="12345",
        ltp=Decimal(ltp),
        bid=Decimal(bid),
        ask=Decimal(ask),
        captured_at=NOW,
    )


def _request(
    *,
    order_type: OrderType = OrderType.MARKET,
    lots: int | None = 1,
    limit_price: Decimal | None = None,
    risk: RiskParameters | None = None,
    request_id: str = "order-1",
) -> OrderRequest:
    return OrderRequest(
        option=_option(),
        order_type=order_type,
        lots=lots,
        limit_price=limit_price,
        risk=risk or RiskParameters(),
        source=OrderSource.GUI,
        request_id=request_id,
        created_at=NOW,
    )


def test_market_buy_opens_position_and_deducts_cash() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))

    events = engine.submit(
        _request(),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )
    portfolio = engine.portfolio()

    assert events[0].kind == ExecutionEventKind.POSITION_OPENED
    assert events[0].price == Decimal("100.50")
    assert portfolio.cash_balance == Decimal("93467.50")
    assert portfolio.positions[0].quantity == 65
    assert portfolio.equity == Decimal("100000.00")


def test_limit_order_waits_then_fills_at_best_ask() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    request = _request(
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99.60"),
    )

    submitted = engine.submit(
        request,
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )
    assert submitted[0].kind == ExecutionEventKind.ORDER_PENDING
    assert len(engine.portfolio().pending_orders) == 1

    filled = engine.mark(
        {"12345": _quote(ltp="99", bid="98.40", ask="98.50")},
        captured_at=NOW + timedelta(seconds=1),
    )
    assert filled[0].kind == ExecutionEventKind.POSITION_OPENED
    assert filled[0].price == Decimal("98.50")
    assert not engine.portfolio().pending_orders


def test_pending_order_can_be_cancelled_and_releases_reserved_cash() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("10000"))
    engine.submit(
        _request(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("99.60"),
            request_id="pending-order",
        ),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )

    cancelled = engine.cancel_pending("pending-order", captured_at=NOW)
    opened = engine.submit(
        _request(request_id="replacement-order"),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )

    assert cancelled[0].kind == ExecutionEventKind.ORDER_CANCELLED
    assert not engine.portfolio().pending_orders
    assert opened[0].kind == ExecutionEventKind.POSITION_OPENED


def test_insufficient_balance_rejects_without_mutating_account() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("1000"))

    events = engine.submit(
        _request(),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )

    assert events[0].kind == ExecutionEventKind.ORDER_REJECTED
    assert engine.portfolio().cash_balance == Decimal("1000")
    assert not engine.portfolio().positions


def test_automatic_sizing_uses_maximum_affordable_whole_lots() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("20000"))

    events = engine.submit(
        _request(lots=None),
        _quote(ltp="100", bid="99.50", ask="100"),
        captured_at=NOW,
    )

    assert events[0].kind == ExecutionEventKind.POSITION_OPENED
    assert engine.portfolio().positions[0].lots == 3
    assert engine.portfolio().cash_balance == Decimal("500")


def test_mark_updates_unrealized_pnl_and_manual_exit_realizes_it() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    engine.submit(
        _request(),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )

    engine.mark(
        {"12345": _quote(ltp="111", bid="110", ask="111.50")},
        captured_at=NOW + timedelta(seconds=1),
    )
    marked = engine.portfolio()
    assert marked.unrealized_pnl == Decimal("617.50")

    events = engine.exit_position(
        "order-1",
        _quote(ltp="111", bid="110", ask="111.50"),
        captured_at=NOW + timedelta(seconds=2),
    )
    exited = engine.portfolio()
    assert events[0].exit_reason == ExitReason.MANUAL
    assert events[0].realized_pnl == Decimal("617.50")
    assert exited.cash_balance == Decimal("100617.50")
    assert exited.realized_pnl == Decimal("617.50")
    assert not exited.positions
    assert exited.closed_positions[0].exit_reason == ExitReason.MANUAL
    assert exited.closed_positions[0].realized_pnl == Decimal("617.50")


def test_profit_lock_moves_stop_to_entry_plus_fifteen_paise() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    engine.submit(
        _request(risk=RiskParameters(), request_id="profit-lock"),
        _quote(ltp="100", bid="99.50", ask="100"),
        captured_at=NOW,
    )
    engine.mark(
        {"12345": _quote(ltp="103", bid="102", ask="103.50")},
        captured_at=NOW + timedelta(seconds=1),
    )
    events = engine.mark(
        {"12345": _quote(ltp="100.10", bid="100.10", ask="100.50")},
        captured_at=NOW + timedelta(seconds=2),
    )
    assert events[0].exit_reason == ExitReason.STOP_LOSS
    assert events[0].price == Decimal("100.10")


def test_daily_reset_preserves_cash_and_clears_realized_history() -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    engine.submit(_request(), _quote(ltp="100", bid="99.50", ask="100"), captured_at=NOW)
    engine.exit_position("order-1", _quote(ltp="110", bid="110", ask="110"), captured_at=NOW)
    cash = engine.portfolio().cash_balance
    engine.reset_daily_pnl()
    portfolio = engine.portfolio()
    assert portfolio.cash_balance == cash
    assert portfolio.realized_pnl == Decimal("0")
    assert not portfolio.closed_positions


def test_eod_journal_contains_closed_position_final_state(tmp_path: Path) -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    engine.submit(_request(), _quote(ltp="100", bid="99.50", ask="100"), captured_at=NOW)
    engine.exit_position("order-1", _quote(ltp="110", bid="110", ask="110"), captured_at=NOW)
    journal = JsonlTradeJournal(tmp_path / "trade_journal", fsync=False)
    path = journal.write(session_date=NOW.date(), portfolio=engine.portfolio())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "2026-08-01.journal"
    assert payload["portfolio"]["closed_positions"][0]["exit_reason"] == "MANUAL"


def test_target_stop_and_trailing_stop_are_percentage_based() -> None:
    cases = (
        (
            RiskParameters(target_percent=Decimal("10")),
            (_quote(ltp="112", bid="111", ask="112.50"),),
            ExitReason.TARGET,
        ),
        (
            RiskParameters(stop_loss_percent=Decimal("5")),
            (_quote(ltp="95", bid="95", ask="95.50"),),
            ExitReason.STOP_LOSS,
        ),
        (
            RiskParameters(trailing_stop_percent=Decimal("5")),
            (
                _quote(ltp="121", bid="120", ask="121.50"),
                _quote(ltp="114", bid="113", ask="114.50"),
            ),
            ExitReason.TRAILING_STOP,
        ),
    )
    for index, (risk, quotes, expected_reason) in enumerate(cases):
        engine = SimulatorEngine(starting_balance=Decimal("100000"))
        engine.submit(
            _request(risk=risk, request_id=f"order-{index}"),
            _quote(ltp="100", bid="99.50", ask="100"),
            captured_at=NOW,
        )
        events: tuple[ExecutionEvent, ...] = ()
        for offset, quote in enumerate(quotes, start=1):
            events = engine.mark(
                {"12345": quote},
                captured_at=NOW + timedelta(seconds=offset),
            )
        assert events[0].exit_reason == expected_reason
        assert not engine.portfolio().positions
