from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from ktrader_simulator.broker.angleone_orders import (
    AngleOneLiveOrderRouter,
    LiveOrderError,
)
from ktrader_simulator.config import load_settings
from ktrader_simulator.trading.engine import SimulatorEngine
from tests.test_simulator_engine import NOW, _quote, _request


class FakeSmartApi:
    def __init__(self) -> None:
        self.payloads: list[dict[str, str]] = []
        self.cancellations: list[tuple[str, str]] = []

    def placeOrderFullResponse(self, payload: dict[str, str]) -> dict[str, object]:
        self.payloads.append(payload)
        return {"status": True, "data": {"orderid": f"broker-{len(self.payloads)}"}}

    def orderBook(self) -> dict[str, object]:
        return {"status": True, "data": []}

    def rmsLimit(self) -> dict[str, object]:
        return {"status": True, "data": {"availablecash": "87654.25"}}

    def cancelOrder(self, order_id: str, variety: str) -> dict[str, object]:
        self.cancellations.append((order_id, variety))
        return {"status": True, "data": {"orderid": order_id}}


def test_live_router_builds_documented_buy_sell_payloads(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})
    smart_api = FakeSmartApi()
    router = AngleOneLiveOrderRouter(settings, smart_api=smart_api)
    request = _request(request_id="route-order-1")

    buy_id = asyncio.run(router.place_entry(request, lots=1))
    engine = SimulatorEngine(starting_balance=Decimal("100000"))
    engine.submit(
        request,
        _quote(ltp="100", bid="99.50", ask="100.50"),
        captured_at=NOW,
    )
    sell_id = asyncio.run(router.exit_position(engine.portfolio().positions[0]))
    cancelled_id = asyncio.run(router.cancel_order("broker-pending"))
    balance = asyncio.run(router.available_balance())

    assert buy_id == "broker-1"
    assert sell_id == "broker-2"
    assert cancelled_id == "broker-pending"
    assert balance == Decimal("87654.25")
    assert smart_api.payloads[0]["transactiontype"] == "BUY"
    assert smart_api.payloads[0]["exchange"] == "NFO"
    assert smart_api.payloads[0]["ordertype"] == "MARKET"
    assert smart_api.payloads[0]["quantity"] == "65"
    assert smart_api.payloads[0]["producttype"] == "INTRADAY"
    assert smart_api.payloads[1]["transactiontype"] == "SELL"
    assert smart_api.cancellations == [("broker-pending", settings.broker_order_variety)]
    assert len(smart_api.payloads[0]["ordertag"]) <= 20


class RejectingSmartApi(FakeSmartApi):
    def placeOrderFullResponse(self, payload: dict[str, str]) -> dict[str, object]:
        self.payloads.append(payload)
        return {"status": False, "message": "rejected by test broker"}


def test_live_router_surfaces_negative_broker_acknowledgement(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})
    router = AngleOneLiveOrderRouter(settings, smart_api=RejectingSmartApi())

    with pytest.raises(LiveOrderError, match="rejected by test broker"):
        asyncio.run(router.place_entry(_request(), lots=1))
