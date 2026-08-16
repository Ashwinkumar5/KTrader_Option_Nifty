from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from ktrader_simulator.storage.ledger import JsonlTradeLedger
from ktrader_simulator.trading.engine import SimulatorEngine
from ktrader_simulator.trading.models import OrderType
from tests.test_simulator_engine import NOW, _quote, _request


def test_jsonl_ledger_recovers_positions_pending_orders_and_cash(tmp_path: Path) -> None:
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    opened = engine.submit(
        _request(request_id="filled-order"),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )
    pending = engine.submit(
        _request(
            request_id="pending-order",
            order_type=OrderType.LIMIT,
            limit_price=Decimal("80"),
        ),
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )
    assert engine.attach_pending_broker_order_id("pending-order", "broker-pending")
    engine.mark(
        {"12345": _quote(ltp="111", bid="110", ask="111.50")},
        captured_at=NOW,
    )
    expected = engine.portfolio()
    ledger_path = tmp_path / "trade_ledger.jsonl"
    ledger = JsonlTradeLedger(ledger_path, fsync=False)
    ledger.append(
        portfolio=expected,
        executions=(*opened, *pending),
        recorded_at=NOW,
    )
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write('{"truncated":')

    recovered = ledger.load_latest()

    assert recovered == expected
    assert recovered is not None
    assert recovered.positions[0].high_watermark == Decimal("110")
    assert recovered.pending_orders[0].order_id == "pending-order"
    assert recovered.pending_orders[0].broker_order_id == "broker-pending"

    restored_engine = SimulatorEngine(starting_balance=Decimal("100000"))
    restored_engine.restore(recovered)
    assert restored_engine.portfolio() == expected


def test_restore_rejects_a_ledger_from_another_starting_balance() -> None:
    source = SimulatorEngine(starting_balance=Decimal("100000"))
    target = SimulatorEngine(starting_balance=Decimal("50000"))

    with pytest.raises(ValueError, match="starting balance"):
        target.restore(source.portfolio())
