from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import dearpygui.dearpygui as dpg
import pytest

from ktrader_simulator.config import load_settings
from ktrader_simulator.controller import (
    AnalyticsEvent,
    ConnectionStatus,
    PortfolioEvent,
    SnapshotEvent,
    StatusEvent,
    TradingController,
)
from ktrader_simulator.domain.models import (
    ChainRow,
    Instrument,
    MarketSnapshot,
    Moneyness,
    OptionInstrument,
    OptionType,
    Quote,
)
from ktrader_simulator.gui import tags
from ktrader_simulator.gui.dashboard import DashboardBindings, _summary_metric_theme
from ktrader_simulator.gui.layout import build_layout
from ktrader_simulator.market.analytics import ChainAnalyticsEngine, MetricStatus
from ktrader_simulator.trading.engine import SimulatorEngine
from ktrader_simulator.trading.models import OrderRequest, OrderType, RiskParameters


def _option(strike: int, option_type: OptionType) -> OptionInstrument:
    return OptionInstrument(
        underlying="NIFTY",
        expiry=date(2099, 12, 31),
        strike=Decimal(strike),
        option_type=option_type,
        instrument=Instrument(
            exchange="NFO",
            token=f"{strike}-{option_type.value}",
            trading_symbol=f"NIFTY31DEC99{strike}{option_type.value}",
        ),
        lot_size=65,
    )


def _quote(instrument: OptionInstrument, price: Decimal) -> Quote:
    return Quote(
        token=instrument.instrument.token,
        ltp=price,
        bid=price - Decimal("0.50"),
        ask=price + Decimal("0.50"),
        captured_at=datetime.now(UTC),
        implied_volatility=(
            Decimal("18.25")
            if instrument.option_type == OptionType.CALL
            else Decimal("19.50")
        ),
    )


def _snapshot() -> MarketSnapshot:
    atm = Decimal("22500")
    rows: list[ChainRow] = []
    for strike_value in (22400, 22450, 22500, 22550, 22600):
        strike = Decimal(strike_value)
        call = _option(strike_value, OptionType.CALL)
        put = _option(strike_value, OptionType.PUT)
        if strike == atm:
            call_money = put_money = Moneyness.ATM
        elif strike < atm:
            call_money, put_money = Moneyness.ITM, Moneyness.OTM
        else:
            call_money, put_money = Moneyness.OTM, Moneyness.ITM
        rows.append(
            ChainRow(
                strike=strike,
                call=call,
                put=put,
                call_quote=_quote(call, Decimal("100")),
                put_quote=_quote(put, Decimal("90")),
                call_moneyness=call_money,
                put_moneyness=put_money,
            )
        )
    return MarketSnapshot(
        underlying="NIFTY",
        expiry=date(2099, 12, 31),
        spot_price=Decimal("22520.45"),
        atm_strike=atm,
        captured_at=datetime.now(UTC),
        rows=tuple(rows),
        india_vix=Decimal("13.47"),
        india_vix_sod_price=Decimal("13.05"),
        nifty_price=Decimal("22520.45"),
        nifty_sod_price=Decimal("22480.00"),
    )


def test_snapshot_event_refreshes_all_dashboard_quote_fields(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})
    controller = TradingController(settings)

    dpg.create_context()
    try:
        build_layout(settings)
        bindings = DashboardBindings(settings=settings, controller=controller)
        bindings.bind_callbacks()
        snapshot = _snapshot()
        bindings.apply_events(
            (
                StatusEvent(ConnectionStatus.CONNECTED),
                SnapshotEvent(snapshot),
            )
        )

        assert dpg.get_value(tags.CONNECTED_BROKER) == "Broker: ANGLEONE [CONNECTED]"
        assert dpg.get_value(tags.INDIA_VIX_VALUE) == "13.47"
        assert dpg.get_value(tags.INDIA_VIX_STATUS) == "▲ +0.42"
        assert dpg.get_item_theme(tags.INDIA_VIX_VALUE) == tags.GREEN_TEXT_THEME
        assert dpg.get_item_theme(tags.INDIA_VIX_STATUS) == tags.GREEN_TEXT_THEME
        assert dpg.get_value(tags.NIFTY_VALUE) == "22,520.45"
        assert dpg.get_value(tags.NIFTY_STATUS) == "▲ +40.45"
        assert dpg.get_item_theme(tags.NIFTY_VALUE) == tags.GREEN_TEXT_THEME
        assert dpg.get_value(tags.UNDERLYING_PRICE) == "Underlying Price (NIFTY): 22,520.45"
        assert dpg.get_value(tags.SELECTED_STRIKE).startswith("Selected Strike: 22500")
        assert dpg.get_value(tags.SELECTED_CALL_LTP) == "LTP: 100.00"
        assert dpg.get_value(tags.SELECTED_PUT_LTP) == "LTP: 90.00"
        assert dpg.get_value(tags.CALL_ORDER_TOTAL) == "CE: 97,500.00 | Max: 15"
        assert dpg.get_value(tags.PUT_ORDER_TOTAL) == "PE: 99,450.00 | Max: 17"
        assert dpg.get_value(tags.CALL_BID_TAGS[2]) == "99.50"
        assert dpg.get_value(tags.CALL_ASK_TAGS[2]) == "100.50"
        assert dpg.get_value(tags.PUT_BID_TAGS[2]) == "89.50"
        assert dpg.get_value(tags.PUT_ASK_TAGS[2]) == "90.50"
        assert dpg.get_item_configuration(tags.STRIKE_TAGS[2])["label"] == "22500 (ATM)"
        assert dpg.get_item_theme(tags.STRIKE_TAGS[2]) == tags.SELECTED_STRIKE_THEME
        assert dpg.get_item_theme(tags.STRIKE_TAGS[1]) == tags.STRIKE_DEFAULT_THEME
        assert dpg.get_item_configuration(tags.STRIKE_TAGS[0])["label"].endswith("CE:ITM PE:OTM")

        analytics = ChainAnalyticsEngine().build(snapshot)
        bindings.apply_events((AnalyticsEvent(analytics),))
        assert dpg.get_value(tags.ANALYTICS_CELL_TAGS[0][0]) == "18.25"
        assert dpg.get_value(tags.ANALYTICS_CELL_TAGS[0][6]) == "19.50"
        assert all(
            dpg.get_value(tag) == "NEUTRAL" for tag in tags.ANALYTICS_SUMMARY_STATUSES
        )
        assert all(
            dpg.get_item_theme(tag) == tags.YELLOW_TEXT_THEME
            for tag in (
                *tags.ANALYTICS_SUMMARY_VALUES,
                *tags.ANALYTICS_SUMMARY_STATUSES,
            )
        )

        colored_first_row = replace(
            analytics.rows[0],
            oi_pcr_status=MetricStatus.BULLISH,
            put_volume_oi_status=MetricStatus.BEARISH,
            straddle_status=MetricStatus.NEUTRAL,
        )
        bindings.apply_events(
            (
                AnalyticsEvent(
                    replace(
                        analytics,
                        rows=(colored_first_row, *analytics.rows[1:]),
                    )
                ),
            )
        )
        assert (
            dpg.get_item_theme(tags.ANALYTICS_CELL_TAGS[0][7])
            == tags.GREEN_TEXT_THEME
        )
        assert (
            dpg.get_item_theme(tags.ANALYTICS_CELL_TAGS[0][8])
            == tags.RED_TEXT_THEME
        )
        assert (
            dpg.get_item_theme(tags.ANALYTICS_CELL_TAGS[0][9])
            == tags.YELLOW_TEXT_THEME
        )

        colored_summary = replace(
            analytics,
            oi_pcr_status=MetricStatus.BULLISH,
            volume_pcr_status=MetricStatus.BEARISH,
            put_volume_oi_status="WRITING",
            call_volume_oi_status="SHORT BUILD",
        )
        bindings.apply_events((AnalyticsEvent(colored_summary),))
        for index in (0, 2):
            assert (
                dpg.get_item_theme(tags.ANALYTICS_SUMMARY_VALUES[index])
                == tags.GREEN_TEXT_THEME
            )
            assert (
                dpg.get_item_theme(tags.ANALYTICS_SUMMARY_STATUSES[index])
                == tags.GREEN_TEXT_THEME
            )
        for index in (1, 3):
            assert (
                dpg.get_item_theme(tags.ANALYTICS_SUMMARY_VALUES[index])
                == tags.RED_TEXT_THEME
            )
            assert (
                dpg.get_item_theme(tags.ANALYTICS_SUMMARY_STATUSES[index])
                == tags.RED_TEXT_THEME
            )

        writes_before_duplicate = bindings.ui_write_count
        bindings.apply_events((SnapshotEvent(snapshot),))
        assert bindings.ui_write_count == writes_before_duplicate

        bindings.apply_events(
            (
                SnapshotEvent(
                    replace(
                        snapshot,
                        nifty_price=Decimal("22450"),
                        nifty_sod_price=Decimal("22480"),
                    )
                ),
            )
        )
        assert dpg.get_value(tags.NIFTY_STATUS) == "▼ -30.00"
        assert dpg.get_item_theme(tags.NIFTY_VALUE) == tags.RED_TEXT_THEME

        bindings.apply_events(
            (
                SnapshotEvent(
                    replace(
                        snapshot,
                        nifty_price=Decimal("22480"),
                        nifty_sod_price=Decimal("22480"),
                    )
                ),
            )
        )
        assert dpg.get_value(tags.NIFTY_STATUS) == "● 0.00"
        assert dpg.get_item_theme(tags.NIFTY_VALUE) == tags.YELLOW_TEXT_THEME

        bindings.select_buy_price(OptionType.CALL)
        assert dpg.get_value(tags.ORDER_TYPE_COMBO) == "Limit"
        assert dpg.get_value(tags.LIMIT_PRICE_INPUT) == pytest.approx(99.60)

        bindings.select_buy_price(OptionType.PUT)
        assert dpg.get_value(tags.PUT_LIMIT_PRICE_INPUT) == pytest.approx(89.60)

        dpg.set_value(tags.ORDER_TYPE_COMBO, "Market")
        bindings.select_buy_price(OptionType.CALL)
        assert dpg.get_value(tags.ORDER_TYPE_COMBO) == "Market"
        bindings._on_buy_call("", None, None)
        assert "CE MARKET order queued" in dpg.get_value(tags.ORDER_STATUS)

        dpg.set_value(tags.PRICE_MODE_RADIO, "Manual")
        dpg.set_value(tags.LOTS_INPUT, 2)
        bindings._on_lots_changed("", 2, None)
        assert dpg.get_value(tags.CALL_ORDER_TOTAL) == "CE: 13,000.00 | Max: 15"

        selected_row = _snapshot().rows[2]
        engine = SimulatorEngine(starting_balance=Decimal("100000"))
        executions = engine.submit(
            OrderRequest(
                option=selected_row.call,
                order_type=OrderType.MARKET,
                lots=1,
                limit_price=None,
                risk=RiskParameters(
                    target_percent=Decimal("10"),
                    stop_loss_percent=Decimal("5"),
                    trailing_stop_percent=Decimal("5"),
                ),
                request_id="dashboard-position",
                created_at=datetime.now(UTC),
            ),
            selected_row.call_quote,
        )
        bindings.apply_events(
            (
                PortfolioEvent.from_portfolio(
                    engine.portfolio(),
                    executions=executions,
                ),
            )
        )
        assert dpg.get_value(tags.ACCOUNT_BALANCE) == "₹93,467.50"
        assert dpg.get_value(tags.RESERVED_BALANCE) == "Reserved: ₹0.00"
        assert dpg.get_value(tags.FUNDS_STATUS) == "Available: ₹93,467.50"
        assert dpg.get_value(tags.CONSOLIDATED_PNL) == "+0.00"
        assert (
            dpg.get_value("ktrader::position::dashboard-position::symbol")
            == selected_row.call.instrument.trading_symbol
        )
        assert dpg.get_value("ktrader::position::dashboard-position::invested") == "6,532.50"
        assert dpg.get_value("ktrader::position::dashboard-position::current") == "6,532.50"
        assert not dpg.get_item_configuration(tags.PORTFOLIO_PLACEHOLDER_ROW)["show"]

        pending_engine = SimulatorEngine(starting_balance=Decimal("100000"))
        pending_events = pending_engine.submit(
            OrderRequest(
                option=selected_row.call,
                order_type=OrderType.LIMIT,
                lots=1,
                limit_price=Decimal("80"),
                risk=RiskParameters(
                    target_percent=Decimal("10"),
                    stop_loss_percent=Decimal("5"),
                    trailing_stop_percent=Decimal("5"),
                ),
                request_id="dashboard-pending",
                created_at=datetime.now(UTC),
            ),
            selected_row.call_quote,
        )
        bindings.apply_events(
            (
                PortfolioEvent.from_portfolio(
                    pending_engine.portfolio(),
                    executions=pending_events,
                ),
            )
        )
        assert dpg.get_value("ktrader::pending::dashboard-pending::symbol").endswith("[LIMIT]")
        assert dpg.get_value("ktrader::pending::dashboard-pending::status") == "PENDING"
        assert dpg.get_value("ktrader::pending::dashboard-pending::invested") == "5,200.00"
        assert dpg.get_value(tags.RESERVED_BALANCE) == "Reserved: ₹5,200.00"
        assert dpg.get_value(tags.FUNDS_STATUS) == "Available: ₹94,800.00"
        pending_action = "ktrader::pending::dashboard-pending::action"
        action_configuration = dpg.get_item_configuration(pending_action)
        assert action_configuration["label"] == "EXIT"
        assert action_configuration["enabled"] is True
    finally:
        controller.stop()
        dpg.destroy_context()


@pytest.mark.parametrize(
    ("metric_index", "status", "theme"),
    (
        (2, "WRITING", tags.GREEN_TEXT_THEME),
        (2, "SHORT BUILD", tags.GREEN_TEXT_THEME),
        (2, "LONG UNWIND", tags.GREEN_TEXT_THEME),
        (2, "LONG BUILD", tags.RED_TEXT_THEME),
        (2, "SHORT COVER", tags.RED_TEXT_THEME),
        (3, "LONG BUILD", tags.GREEN_TEXT_THEME),
        (3, "SHORT COVER", tags.GREEN_TEXT_THEME),
        (3, "SHORT BUILD", tags.RED_TEXT_THEME),
        (3, "LONG UNWIND", tags.RED_TEXT_THEME),
        (3, "NEUTRAL", tags.YELLOW_TEXT_THEME),
    ),
)
def test_consolidated_flow_status_colors_are_side_aware(
    metric_index: int,
    status: str,
    theme: str,
) -> None:
    assert _summary_metric_theme(metric_index, status) == theme
