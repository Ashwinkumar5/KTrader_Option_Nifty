from __future__ import annotations

from pathlib import Path
from typing import Never

from dotenv import dotenv_values

from ktrader_simulator.config import Settings, load_settings
from ktrader_simulator.controller import TradingController


def test_local_environment_disables_live_broker_orders() -> None:
    environment_path = Path(__file__).resolve().parents[1] / ".env"
    values = dotenv_values(environment_path)
    live_order_flag = values.get("BROKER_ORDER_EXECUTION_ENABLED")

    assert isinstance(live_order_flag, str)
    assert live_order_flag.lower() == "false"


def test_disabled_flag_never_constructs_live_router(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})

    def forbidden_factory(_settings: Settings) -> Never:
        raise AssertionError("live router must not be constructed")

    controller = TradingController(
        settings,
        live_router_factory=forbidden_factory,
    )
    assert controller.portfolio().cash_balance == settings.starting_balance
