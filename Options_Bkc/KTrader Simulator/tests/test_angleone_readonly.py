from __future__ import annotations

import asyncio
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ktrader_simulator.broker import angleone as angleone_module
from ktrader_simulator.broker.angleone import (
    AngleOneReadOnlyBroker,
    _normalize_implied_volatilities,
    _normalize_quotes,
)
from ktrader_simulator.config import Settings, load_settings
from ktrader_simulator.domain.models import Instrument, OptionInstrument, OptionType


class BlockingLoginClient:
    def __init__(self, _settings: Settings) -> None:
        pass

    async def login(self) -> None:
        time.sleep(0.1)


def test_full_quote_response_extracts_ltp_and_best_bid_ask() -> None:
    response = {
        "status": True,
        "data": {
            "fetched": [
                {
                    "symbolToken": "12345",
                    "ltp": 101.25,
                    "open": 100.75,
                    "depth": {
                        "buy": [{"price": 101.20}],
                        "sell": [{"price": 101.30}],
                    },
                }
            ]
        },
    }

    quote = _normalize_quotes(response)["12345"]

    assert quote.ltp == Decimal("101.25")
    assert quote.bid == Decimal("101.2")
    assert quote.ask == Decimal("101.3")
    assert quote.session_open == Decimal("100.75")


def test_option_greek_response_maps_iv_by_strike_and_option_type() -> None:
    call = OptionInstrument(
        underlying="NIFTY",
        expiry=date(2026, 8, 13),
        strike=Decimal("24500"),
        option_type=OptionType.CALL,
        instrument=Instrument("NFO", "call-token", "NIFTY13AUG2624500CE"),
        lot_size=65,
    )
    put = OptionInstrument(
        underlying="NIFTY",
        expiry=date(2026, 8, 13),
        strike=Decimal("24500"),
        option_type=OptionType.PUT,
        instrument=Instrument("NFO", "put-token", "NIFTY13AUG2624500PE"),
        lot_size=65,
    )
    response = {
        "status": True,
        "data": [
            {
                "strikePrice": "24500.000000",
                "optionType": "CE",
                "impliedVolatility": "12.340000",
            },
            {
                "strikePrice": "24500.000000",
                "optionType": "PE",
                "impliedVolatility": "13.560000",
            },
        ],
    }

    values = _normalize_implied_volatilities(response, (call, put))

    assert values == {
        "call-token": Decimal("12.340000"),
        "put-token": Decimal("13.560000"),
    }


def test_read_only_adapter_does_not_expose_order_methods() -> None:
    assert not hasattr(AngleOneReadOnlyBroker, "place_order")
    assert not hasattr(AngleOneReadOnlyBroker, "modify_order")
    assert not hasattr(AngleOneReadOnlyBroker, "cancel_order")


def test_blocking_bot_sdk_call_is_offloaded_from_runtime_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(
        simulator_root=simulator_root,
        environ={
            "ANGLEONE_API_KEY": "test-key",
            "ANGLEONE_CLIENT_CODE": "test-client",
            "ANGLEONE_PASSWORD": "test-password",
            "ANGLEONE_TOTP_SECRET": "test-totp",
        },
    )

    def client_factory(_settings: Settings) -> type[BlockingLoginClient]:
        return BlockingLoginClient

    monkeypatch.setattr(angleone_module, "_existing_angleone_client", client_factory)
    broker = AngleOneReadOnlyBroker(settings)

    async def runtime_remained_responsive() -> bool:
        login = asyncio.create_task(broker.connect())
        await asyncio.sleep(0.01)
        responsive = not login.done()
        await login
        return responsive

    assert asyncio.run(runtime_remained_responsive())
