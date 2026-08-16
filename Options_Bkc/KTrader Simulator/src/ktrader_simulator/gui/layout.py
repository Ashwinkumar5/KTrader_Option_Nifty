from __future__ import annotations

from decimal import Decimal

import dearpygui.dearpygui as dpg

from ktrader_simulator.config import Settings
from ktrader_simulator.gui import tags
from ktrader_simulator.gui.bindings import (
    account_balance_text,
    broker_text,
    funds_status_text,
    reserved_balance_text,
)
from ktrader_simulator.gui.theme import (
    bind_analytics_font,
    bind_analytics_header_font,
    bind_card_title_font,
    bind_dashboard_title_font,
    bind_header_detail_font,
    bind_india_vix_value_font,
    bind_summary_detail_font,
    bind_summary_value_font,
    create_themes,
)


_MARKET_CARD_WIDTH = 250
_MARKET_PULSE_WIDTH = (_MARKET_CARD_WIDTH * 2) + 25


def build_layout(settings: Settings) -> None:
    """Build the fixed dashboard structure; callbacks are added in later phases."""

    create_themes()
    summary_width = 310
    data_width = max(640, settings.viewport_width - summary_width - 410)
    content_height = max(430, settings.top_panel_height)

    with dpg.window(
        tag=tags.MAIN_WINDOW,
        no_title_bar=True,
        no_move=True,
        no_resize=True,
        no_collapse=True,
        no_scrollbar=True,
        no_scroll_with_mouse=True,
    ):
        with dpg.child_window(
            tag=tags.HEADER_PANEL,
            height=94,
            width=-1,
            border=True,
        ):
            _build_header(settings)

        with dpg.group(horizontal=True):
            with dpg.child_window(
                tag=tags.LEFT_PANEL,
                width=summary_width,
                height=content_height,
                border=True,
            ):
                _build_summary_panel(settings)

            with dpg.child_window(
                tag=tags.DATA_PANEL,
                width=data_width,
                height=content_height,
                border=True,
            ):
                _build_option_chain_panel(settings)

            with dpg.child_window(
                tag=tags.RIGHT_PANEL,
                width=-1,
                height=content_height,
                border=True,
            ):
                _build_order_panel(settings)

        with dpg.child_window(
            tag=tags.PORTFOLIO_PANEL,
            width=-1,
            height=-1,
            border=True,
        ):
            _build_portfolio_panel()

    dpg.bind_theme(tags.GLOBAL_THEME)
    dpg.bind_item_theme(tags.BUY_CALL_BUTTON, tags.BUY_CALL_THEME)
    dpg.bind_item_theme(tags.BUY_PUT_BUTTON, tags.BUY_PUT_THEME)
    dpg.bind_item_theme(tags.PORTFOLIO_PLACEHOLDER_EXIT, tags.EXIT_THEME)
    for card in (
        tags.HEADER_PANEL,
        tags.LEFT_PANEL,
        tags.DATA_PANEL,
        tags.RIGHT_PANEL,
        tags.PORTFOLIO_PANEL,
        tags.ACCOUNT_CARD,
        tags.PNL_CARD,
        tags.METRICS_CARD,
        tags.ORDER_CARD,
        tags.RISK_CARD,
    ):
        dpg.bind_item_theme(card, tags.CARD_THEME)
    dpg.bind_item_theme(tags.CALL_ORDER_CARD, tags.CALL_CARD_THEME)
    dpg.bind_item_theme(tags.PUT_ORDER_CARD, tags.PUT_CARD_THEME)
    dpg.bind_item_theme(tags.INDEX_COMBO, tags.INDEX_COMBO_THEME)
    dpg.bind_item_theme(tags.PRICE_MODE_RADIO, tags.PRICE_MODE_RADIO_THEME)
    dpg.bind_item_theme(tags.NIFTY_CARD, tags.INDIA_VIX_CARD_THEME)
    dpg.bind_item_theme(tags.INDIA_VIX_CARD, tags.INDIA_VIX_CARD_THEME)
    for strike_tag in tags.STRIKE_TAGS:
        dpg.bind_item_theme(strike_tag, tags.STRIKE_DEFAULT_THEME)
    _apply_table_accents()


def resize_layout(settings: Settings, width: int, height: int) -> None:
    """Resize only containers on viewport events; never recompute market state."""
    summary_width = 310
    right_width = 390
    data_width = max(640, width - summary_width - right_width - 32)
    top_height = max(430, int((height - 128) * 0.62))
    dpg.configure_item(tags.LEFT_PANEL, width=summary_width, height=top_height)
    dpg.configure_item(tags.DATA_PANEL, width=data_width, height=top_height)
    dpg.configure_item(tags.RIGHT_PANEL, height=top_height)


def _build_header(settings: Settings) -> None:
    with dpg.table(
        header_row=False,
        borders_innerH=False,
        borders_innerV=False,
        borders_outerH=False,
        borders_outerV=False,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        dpg.add_table_column(width_stretch=True)
        dpg.add_table_column(width_fixed=True, init_width_or_weight=_MARKET_PULSE_WIDTH)
        with dpg.table_row():
            with dpg.table_cell():
                with dpg.group(horizontal=True):
                    title = dpg.add_text("OPTIONS TRADING BOT DASHBOARD")
                    bind_dashboard_title_font(title)
                    dpg.add_spacer(width=30)
                    broker = dpg.add_text(
                        broker_text(settings.broker_name),
                        tag=tags.CONNECTED_BROKER,
                    )
                    bind_header_detail_font(broker)
                    dpg.bind_item_theme(broker, tags.GREEN_TEXT_THEME)
                    connection_mode = dpg.add_text(
                        f"MODE: {settings.order_execution_mode.upper()}",
                        tag=tags.CONNECTION_MODE,
                    )
                    bind_header_detail_font(connection_mode)
                    dpg.bind_item_theme(
                        connection_mode,
                        tags.RED_TEXT_THEME
                        if settings.live_execution_enabled
                        else tags.YELLOW_TEXT_THEME,
                    )
                with dpg.group(horizontal=True):
                    index_label = dpg.add_text("Select Index")
                    bind_header_detail_font(index_label)
                    index_combo = dpg.add_combo(
                        items=list(settings.supported_indices),
                        default_value=settings.default_index,
                        tag=tags.INDEX_COMBO,
                        width=115,
                    )
                    bind_header_detail_font(index_combo)
                    dpg.add_spacer(width=12)
                    price_mode_label = dpg.add_text("Price Mode")
                    bind_header_detail_font(price_mode_label)
                    price_mode = dpg.add_radio_button(
                        items=["Auto", "Manual"],
                        default_value="Auto",
                        horizontal=True,
                        tag=tags.PRICE_MODE_RADIO,
                    )
                    bind_header_detail_font(price_mode)
                    refresh = dpg.add_button(
                        label="Refresh",
                        tag=tags.REFRESH_BUTTON,
                        width=82,
                    )
                    bind_header_detail_font(refresh)
            with dpg.table_cell():
                with dpg.group(horizontal=True):
                    _build_market_card(
                        card_tag=tags.NIFTY_CARD,
                        label="NIFTY 50",
                        value_tag=tags.NIFTY_VALUE,
                        status_tag=tags.NIFTY_STATUS,
                    )
                    _build_market_card(
                        card_tag=tags.INDIA_VIX_CARD,
                        label="INDIA VIX",
                        value_tag=tags.INDIA_VIX_VALUE,
                        status_tag=tags.INDIA_VIX_STATUS,
                    )


def _build_market_card(
    *,
    card_tag: str,
    label: str,
    value_tag: str,
    status_tag: str,
) -> None:
    with dpg.child_window(
        tag=card_tag,
        width=_MARKET_CARD_WIDTH,
        height=78,
        border=True,
        no_scrollbar=True,
        no_scroll_with_mouse=True,
    ):
        with dpg.group(horizontal=True):
            title = dpg.add_text(label)
            bind_card_title_font(title)
            dpg.bind_item_theme(title, tags.CYAN_TEXT_THEME)
            dpg.add_spacer(width=25)
            status = dpg.add_text("● WAITING", tag=status_tag)
            bind_summary_detail_font(status)
            dpg.bind_item_theme(status, tags.MUTED_TEXT_THEME)
        value = dpg.add_text("--", tag=value_tag)
        bind_india_vix_value_font(value)
        dpg.bind_item_theme(value, tags.YELLOW_TEXT_THEME)


def _build_summary_panel(settings: Settings) -> None:
    with dpg.child_window(tag=tags.ACCOUNT_CARD, height=122, border=True):
        title = dpg.add_text("AVAILABLE BALANCE")
        bind_card_title_font(title)
        balance = dpg.add_text(
            account_balance_text(settings.starting_balance),
            tag=tags.ACCOUNT_BALANCE,
        )
        bind_summary_value_font(balance)
        dpg.bind_item_theme(balance, tags.GREEN_TEXT_THEME)
        reserved = dpg.add_text(
            reserved_balance_text(Decimal("0")),
            tag=tags.RESERVED_BALANCE,
        )
        bind_summary_detail_font(reserved)
        dpg.bind_item_theme(reserved, tags.MUTED_TEXT_THEME)
        funds = dpg.add_text(
            funds_status_text(
                reserved=Decimal("0"),
                available=settings.starting_balance * settings.max_capital_utilization,
            ),
            tag=tags.FUNDS_STATUS,
        )
        bind_summary_detail_font(funds)
        dpg.bind_item_theme(funds, tags.MUTED_TEXT_THEME)

    with dpg.child_window(tag=tags.PNL_CARD, height=92, border=True):
        title = dpg.add_text("CURRENT P&L (CONSOLIDATED)")
        bind_card_title_font(title)
        pnl = dpg.add_text("+0.00", tag=tags.SUMMARY_CONSOLIDATED_PNL)
        bind_summary_value_font(pnl)
        dpg.bind_item_theme(pnl, tags.GREEN_TEXT_THEME)

    with dpg.child_window(tag=tags.METRICS_CARD, height=-1, border=True):
        title = dpg.add_text("CONSOLIDATED METRICS")
        bind_card_title_font(title)
        _build_analytics_summary_table()


def _build_option_chain_panel(settings: Settings) -> None:
    with dpg.group(horizontal=True):
        title = dpg.add_text("DATA HUB")
        bind_card_title_font(title)
        dpg.add_spacer(width=30)
        dpg.add_text("Underlying: --", tag=tags.UNDERLYING_PRICE)
        dpg.add_spacer(width=22)
        selected = dpg.add_text("Selected Strike: --", tag=tags.SELECTED_STRIKE)
        dpg.bind_item_theme(selected, tags.YELLOW_TEXT_THEME)

    dpg.add_separator()
    with dpg.table(
        tag=tags.OPTION_CHAIN_TABLE,
        header_row=True,
        borders_innerH=True,
        borders_outerH=True,
        borders_innerV=True,
        borders_outerV=True,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        for label in ("Call Bid", "Call Ask", "Strike (Selectable)", "Put Bid", "Put Ask"):
            dpg.add_table_column(label=label, init_width_or_weight=1.0)

        for index in range(tags.OPTION_ROW_COUNT):
            with dpg.table_row(tag=tags.OPTION_ROW_TAGS[index]):
                dpg.add_text("--", tag=tags.CALL_BID_TAGS[index])
                dpg.add_text("--", tag=tags.CALL_ASK_TAGS[index])
                dpg.add_selectable(label="--", tag=tags.STRIKE_TAGS[index])
                dpg.add_text("--", tag=tags.PUT_BID_TAGS[index])
                dpg.add_text("--", tag=tags.PUT_ASK_TAGS[index])

    dpg.add_spacer(height=6)
    analytics_title = dpg.add_text("STRIKE ANALYTICS  •  CONFIGURED REFRESH")
    dpg.bind_item_theme(analytics_title, tags.CYAN_TEXT_THEME)
    with dpg.table(
        tag=tags.ANALYTICS_TABLE,
        header_row=False,
        borders_innerH=True,
        borders_outerH=True,
        borders_innerV=True,
        borders_outerV=True,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        for _ in range(10):
            dpg.add_table_column(init_width_or_weight=1.0)
        with dpg.table_row():
            _analytics_text("CALL SIDE", tags.DARK_TEXT_THEME)
            _analytics_text("", tags.DARK_TEXT_THEME)
            _analytics_text("", tags.DARK_TEXT_THEME)
            _analytics_text("STRIKES", tags.DARK_TEXT_THEME)
            _analytics_text("PUT SIDE", tags.DARK_TEXT_THEME)
            _analytics_text("", tags.DARK_TEXT_THEME)
            _analytics_text("", tags.DARK_TEXT_THEME)
            _analytics_text("ANALYTICS", tags.DARK_TEXT_THEME)
            for _ in range(2):
                _analytics_text("", tags.DARK_TEXT_THEME)
        with dpg.table_row():
            for label, theme in (
                ("IV", tags.DARK_TEXT_THEME),
                ("VOL", tags.DARK_TEXT_THEME),
                ("OI", tags.DARK_TEXT_THEME),
                ("STRIKE", tags.DARK_TEXT_THEME),
                ("OI", tags.DARK_TEXT_THEME),
                ("VOL", tags.DARK_TEXT_THEME),
                ("IV", tags.DARK_TEXT_THEME),
                ("PCR", tags.DARK_TEXT_THEME),
                ("VOL/OI", tags.DARK_TEXT_THEME),
                ("STRADDLE", tags.DARK_TEXT_THEME),
            ):
                _analytics_text(label, theme)
        for row_index in range(tags.OPTION_ROW_COUNT):
            with dpg.table_row(tag=tags.ANALYTICS_ROW_TAGS[row_index]):
                for column, tag in enumerate(tags.ANALYTICS_CELL_TAGS[row_index]):
                    value = dpg.add_text("--", tag=tag)
                    bind_analytics_font(value)
                    dpg.bind_item_theme(value, _analytics_cell_theme(column))
def _build_analytics_summary_table() -> None:
    with dpg.table(
        tag=tags.ANALYTICS_SUMMARY_TABLE,
        header_row=True,
        borders_innerH=True,
        borders_outerH=True,
        borders_innerV=True,
        borders_outerV=True,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        dpg.add_table_column(label="METRIC", init_width_or_weight=1.5)
        dpg.add_table_column(label="VALUE", init_width_or_weight=0.65)
        dpg.add_table_column(label="STATUS", init_width_or_weight=1.4)
        for label, value_tag, status_tag in zip(
            (
                "OI PCR",
                "VOLUME PCR",
                "PUT VOL/OI",
                "CALL VOL/OI",
            ),
            tags.ANALYTICS_SUMMARY_VALUES,
            tags.ANALYTICS_SUMMARY_STATUSES,
            strict=True,
        ):
            with dpg.table_row():
                metric = dpg.add_text(label)
                value = dpg.add_text("--", tag=value_tag)
                status = dpg.add_text("NEUTRAL", tag=status_tag)
                bind_analytics_font(metric)
                bind_analytics_font(value)
                bind_analytics_font(status)
                dpg.bind_item_theme(metric, tags.ORANGE_TEXT_THEME)
                dpg.bind_item_theme(value, tags.CYAN_TEXT_THEME)
                dpg.bind_item_theme(status, tags.MUTED_TEXT_THEME)


def _analytics_text(label: str, theme: str) -> None:
    item = dpg.add_text(label)
    bind_analytics_header_font(item)
    dpg.bind_item_theme(item, theme)


def _analytics_cell_theme(column: int) -> str:
    if column <= 2:
        return tags.GREEN_TEXT_THEME
    if column == 3:
        return tags.YELLOW_TEXT_THEME
    if column <= 6:
        return tags.BLUE_TEXT_THEME
    return tags.YELLOW_TEXT_THEME


def _apply_table_accents() -> None:
    """Visual-only table treatment; no callbacks or market-path work."""
    # Low-saturation bands keep dense option-chain data calm and readable.
    call_band = (34, 92, 56, 72)
    strike_band = (100, 79, 14, 68)
    put_band = (34, 76, 128, 76)
    analytics_band = (105, 66, 20, 64)
    header_bands = (
        (216, 247, 225, 255),  # mint: CALL
        (255, 228, 151, 255),  # gold: STRIKE
        (218, 233, 255, 255),  # ice-blue: PUT
        (255, 239, 177, 255),  # warm-yellow: analytics
    )

    for column in (0, 1):
        dpg.highlight_table_column(tags.OPTION_CHAIN_TABLE, column, call_band)
    dpg.highlight_table_column(tags.OPTION_CHAIN_TABLE, 2, strike_band)
    for column in (3, 4):
        dpg.highlight_table_column(tags.OPTION_CHAIN_TABLE, column, put_band)

    for column in range(10):
        band = (
            call_band
            if column <= 2
            else strike_band
            if column == 3
            else put_band
            if column <= 6
            else analytics_band
        )
        dpg.highlight_table_column(tags.ANALYTICS_TABLE, column, band)

    for row in (0, 1):
        for column in range(10):
            color = (
                header_bands[0]
                if column <= 2
                else header_bands[1]
                if column == 3
                else header_bands[2]
                if column <= 6
                else header_bands[3]
            )
            dpg.highlight_table_cell(tags.ANALYTICS_TABLE, row, column, color)

    # The compact key/value panel is deliberately neutral: it visually closes
    # the analytics block without competing with the live strike values.
    dpg.highlight_table_column(tags.ANALYTICS_SUMMARY_TABLE, 0, (61, 45, 22, 78))
    dpg.highlight_table_column(tags.ANALYTICS_SUMMARY_TABLE, 1, (18, 71, 91, 78))


def _build_order_panel(settings: Settings) -> None:
    with dpg.child_window(tag=tags.ORDER_CARD, height=275, border=True):
        title = dpg.add_text("ORDER ENTRY PANEL")
        bind_card_title_font(title)
        mode = dpg.add_text(
            (
                "Execution: LIVE — broker routing enabled"
                if settings.live_execution_enabled
                else "Execution: SHADOW — local simulation only"
            ),
            tag=tags.ORDER_MODE,
        )
        dpg.bind_item_theme(
            mode,
            tags.RED_TEXT_THEME if settings.live_execution_enabled else tags.YELLOW_TEXT_THEME,
        )
        with dpg.group(horizontal=True):
            dpg.add_text("Order Type")
            dpg.add_combo(
                items=["Market", "Limit"],
                default_value=settings.default_order_type.title(),
                tag=tags.ORDER_TYPE_COMBO,
                width=105,
            )
        with dpg.group(horizontal=True):
            with dpg.child_window(tag=tags.CALL_ORDER_CARD, width=164, height=190, border=True):
                call_title = dpg.add_text("CALL (CE)")
                bind_card_title_font(call_title)
                call_ltp = dpg.add_text("LTP: --", tag=tags.SELECTED_CALL_LTP)
                dpg.bind_item_theme(call_ltp, tags.GREEN_TEXT_THEME)
                dpg.add_input_float(
                    default_value=float(settings.default_limit_price),
                    tag=tags.LIMIT_PRICE_INPUT,
                    width=136,
                    min_value=0.0,
                    min_clamped=True,
                    format="%.3f",
                    step=0.05,
                )
                dpg.add_text("CE Limit Price")
                dpg.add_input_int(
                    default_value=settings.default_lots,
                    tag=tags.CALL_LOTS_INPUT,
                    width=136,
                    min_value=1,
                    min_clamped=True,
                    step=1,
                )
                dpg.add_text("CE: -- | Max: --", tag=tags.CALL_ORDER_TOTAL)
                dpg.add_button(label="BUY CALL", tag=tags.BUY_CALL_BUTTON, width=136, height=30)
            with dpg.child_window(tag=tags.PUT_ORDER_CARD, width=164, height=190, border=True):
                put_title = dpg.add_text("PUT (PE)")
                bind_card_title_font(put_title)
                put_ltp = dpg.add_text("LTP: --", tag=tags.SELECTED_PUT_LTP)
                dpg.bind_item_theme(put_ltp, tags.BLUE_TEXT_THEME)
                dpg.add_input_float(
                    default_value=float(settings.default_limit_price),
                    tag=tags.PUT_LIMIT_PRICE_INPUT,
                    width=136,
                    min_value=0.0,
                    min_clamped=True,
                    format="%.3f",
                    step=0.05,
                )
                dpg.add_text("PE Limit Price")
                dpg.add_input_int(
                    default_value=settings.default_lots,
                    tag=tags.PUT_LOTS_INPUT,
                    width=136,
                    min_value=1,
                    min_clamped=True,
                    step=1,
                )
                dpg.add_text("PE: -- | Max: --", tag=tags.PUT_ORDER_TOTAL)
                dpg.add_button(label="BUY PUT", tag=tags.BUY_PUT_BUTTON, width=136, height=30)

    with dpg.child_window(tag=tags.RISK_CARD, height=-1, border=True):
        risk_title = dpg.add_text("RISK MANAGEMENT PANEL")
        bind_card_title_font(risk_title)
        _add_percent_input(
            tag=tags.TARGET_PERCENT_INPUT,
            label="Target (%)",
            value=settings.default_target_percent,
        )
        _add_percent_input(
            tag=tags.STOP_LOSS_PERCENT_INPUT,
            label="Stop Loss (%)",
            value=settings.default_stop_loss_percent,
        )
        _add_percent_input(
            tag=tags.TRAILING_SL_PERCENT_INPUT,
            label="Trailing SL (%)",
            value=settings.default_trailing_sl_percent,
        )
        status = dpg.add_text("Status: Ready", tag=tags.ORDER_STATUS)
        dpg.bind_item_theme(status, tags.MUTED_TEXT_THEME)


def _add_percent_input(*, tag: str, label: str, value: Decimal) -> None:
    with dpg.group(horizontal=True):
        dpg.add_input_float(
            default_value=float(value),
            tag=tag,
            width=120,
            min_value=0.0,
            min_clamped=True,
            format="%.3f",
            step=0.1,
            step_fast=1.0,
        )
        dpg.add_text(label)


def _build_portfolio_panel() -> None:
    title = dpg.add_text("PORTFOLIO POSITIONS")
    bind_card_title_font(title)
    # Retain the tag for backend updates without duplicating the summary P&L
    # already shown in the left-side card.
    dpg.add_text("+0.00", tag=tags.CONSOLIDATED_PNL, show=False)

    dpg.add_separator()
    with dpg.tab_bar():
        with dpg.tab(label="Open Positions"):
            _build_open_positions_table()
        with dpg.tab(label="Closed Positions"):
            _build_closed_positions_table()


def _build_open_positions_table() -> None:
    with dpg.table(
        tag=tags.PORTFOLIO_TABLE,
        header_row=True,
        borders_innerH=True,
        borders_outerH=True,
        borders_innerV=True,
        borders_outerV=True,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        columns = (
            ("Symbol", 1.3),
            ("Qty (Lots)", 1.3),
            ("Avg Cost", 1.3),
            ("Unrealized P&L", 1.3),
            ("P&L (%)", 1.0),
            ("Invested", 1.3),
            ("Current Value", 1.3),
            ("Action", 1.3),
        )
        for label, weight in columns:
            dpg.add_table_column(label=label, init_width_or_weight=weight)

        with dpg.table_row(tag=tags.PORTFOLIO_PLACEHOLDER_ROW):
            for _ in range(3):
                dpg.add_text("--")
            dpg.add_text("--", tag=tags.PORTFOLIO_PLACEHOLDER_PNL)
            dpg.add_text("--", tag=tags.PORTFOLIO_PLACEHOLDER_PNL_PERCENT)
            dpg.add_text("--")
            dpg.add_text("--")
            dpg.add_button(
                label="EXIT",
                tag=tags.PORTFOLIO_PLACEHOLDER_EXIT,
                width=80,
                enabled=False,
            )


def _build_closed_positions_table() -> None:
    with dpg.table(
        tag=tags.CLOSED_POSITIONS_TABLE,
        header_row=True,
        borders_innerH=True,
        borders_outerH=True,
        borders_innerV=True,
        borders_outerV=True,
        policy=dpg.mvTable_SizingStretchProp,
        width=-1,
    ):
        for label in (
            "Symbol",
            "Qty (Lots)",
            "Entry",
            "Exit",
            "Invested",
            "Current",
            "P&L",
            "P&L (%)",
            "Exit Reason",
        ):
            dpg.add_table_column(label=label, init_width_or_weight=1.0)
