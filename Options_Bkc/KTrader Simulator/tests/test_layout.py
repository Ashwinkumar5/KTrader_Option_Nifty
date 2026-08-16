from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import dearpygui.dearpygui as dpg

from ktrader_simulator.config import load_settings
from ktrader_simulator.gui import tags
from ktrader_simulator.gui.bindings import (
    set_account_balance,
    set_connected_broker,
    set_position_pnl,
)
from ktrader_simulator.gui.layout import build_layout


def test_dashboard_builds_with_stable_binding_tags(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})

    dpg.create_context()
    try:
        build_layout(settings)

        for tag in tags.REQUIRED_LAYOUT_TAGS:
            assert dpg.does_item_exist(tag), tag
        assert all(dpg.does_item_exist(tag) for tag in tags.STRIKE_TAGS)
        assert len(tags.STRIKE_TAGS) == 5
        assert dpg.get_value(tags.INDEX_COMBO) == settings.default_index
        assert dpg.get_value(tags.LOTS_INPUT) == settings.default_lots
        assert dpg.get_value(tags.CALL_ORDER_TOTAL) == "CE: -- | Max: --"
        assert dpg.get_value(tags.PUT_ORDER_TOTAL) == "PE: -- | Max: --"
        assert dpg.get_value(tags.ORDER_TYPE_COMBO) == settings.default_order_type.title()
        assert dpg.get_value(tags.TARGET_PERCENT_INPUT) == float(settings.default_target_percent)
        assert dpg.get_value(tags.STOP_LOSS_PERCENT_INPUT) == float(
            settings.default_stop_loss_percent
        )
        assert dpg.get_value(tags.TRAILING_SL_PERCENT_INPUT) == float(
            settings.default_trailing_sl_percent
        )
        assert dpg.get_value(tags.CONNECTED_BROKER) == "Broker: ANGLEONE [DISCONNECTED]"
        assert dpg.get_value(tags.INDIA_VIX_VALUE) == "--"
        assert dpg.get_value(tags.INDIA_VIX_STATUS) == "● WAITING"
        assert dpg.get_value(tags.NIFTY_VALUE) == "--"
        assert dpg.get_value(tags.NIFTY_STATUS) == "● WAITING"
        assert dpg.get_value(tags.ACCOUNT_BALANCE) == "₹100,000.00"
        assert dpg.get_value(tags.RESERVED_BALANCE) == "Reserved: ₹0.00"
        assert dpg.get_value(tags.FUNDS_STATUS) == "Available: ₹100,000.00"

        set_account_balance(Decimal("87250.50"))
        set_connected_broker("angleone")

        assert dpg.get_value(tags.ACCOUNT_BALANCE) == "₹87,250.50"
        assert dpg.get_value(tags.CONNECTED_BROKER) == "Broker: ANGLEONE [DISCONNECTED]"

        set_position_pnl(
            amount_item=tags.PORTFOLIO_PLACEHOLDER_PNL,
            percent_item=tags.PORTFOLIO_PLACEHOLDER_PNL_PERCENT,
            amount=Decimal("600"),
            percentage=Decimal("4.615"),
        )
        assert dpg.get_value(tags.PORTFOLIO_PLACEHOLDER_PNL) == "+600.00"
        assert dpg.get_value(tags.PORTFOLIO_PLACEHOLDER_PNL_PERCENT) == "+4.62%"

        set_position_pnl(
            amount_item=tags.PORTFOLIO_PLACEHOLDER_PNL,
            percent_item=tags.PORTFOLIO_PLACEHOLDER_PNL_PERCENT,
            amount=Decimal("-125.50"),
            percentage=Decimal("-1.25"),
        )
        assert dpg.get_value(tags.PORTFOLIO_PLACEHOLDER_PNL) == "-125.50"
        assert dpg.get_value(tags.PORTFOLIO_PLACEHOLDER_PNL_PERCENT) == "-1.25%"
    finally:
        dpg.destroy_context()
