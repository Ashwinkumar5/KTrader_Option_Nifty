from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

import dearpygui.dearpygui as dpg

from ktrader_simulator.config import Settings
from ktrader_simulator.controller import (
    AnalyticsEvent,
    ConnectionStatus,
    ControllerEvent,
    NoticeEvent,
    PortfolioEvent,
    PositionPnl,
    SnapshotEvent,
    StatusEvent,
    TradingController,
)
from ktrader_simulator.domain.models import (
    ChainRow,
    MarketSnapshot,
    OptionType,
    Quote,
    format_strike,
)
from ktrader_simulator.gui import tags
from ktrader_simulator.gui.bindings import (
    account_balance_text,
    broker_text,
    funds_status_text,
    pnl_amount_text,
    pnl_percent_text,
    reserved_balance_text,
)
from ktrader_simulator.gui.writes import UiWriteCache
from ktrader_simulator.market.analytics import ChainAnalyticsSnapshot
from ktrader_simulator.trading.models import (
    ClosedPosition,
    ExecutionEventKind,
    OrderRequest,
    OrderSource,
    OrderType,
    PendingOrder,
    PortfolioSnapshot,
    Position,
    RiskParameters,
)


@dataclass(frozen=True, slots=True)
class _PositionWidgets:
    row: str
    symbol: str
    lots: str
    average: str
    pnl: str
    pnl_percent: str
    invested: str
    current: str
    exit_button: str


@dataclass(frozen=True, slots=True)
class _PendingOrderWidgets:
    row: str
    symbol: str
    lots: str
    limit: str
    status: str
    pnl_percent: str
    invested: str
    current: str
    action: str


class DashboardBindings:
    """Apply immutable backend events on the Dear PyGui main thread."""

    def __init__(self, *, settings: Settings, controller: TradingController) -> None:
        self._settings = settings
        self._controller = controller
        self._selected_strike: Decimal | None = None
        self._snapshot: MarketSnapshot | None = None
        self._position_widgets: dict[str, _PositionWidgets] = {}
        self._pending_widgets: dict[str, _PendingOrderWidgets] = {}
        self._closed_rows: dict[str, str] = {}
        self._available_balance = settings.starting_balance * settings.max_capital_utilization
        self._writes = UiWriteCache()

    def bind_callbacks(self) -> None:
        dpg.configure_item(tags.INDEX_COMBO, callback=self._on_index_changed)
        dpg.configure_item(tags.BUY_CALL_BUTTON, callback=self._on_buy_call)
        dpg.configure_item(tags.BUY_PUT_BUTTON, callback=self._on_buy_put)
        dpg.configure_item(tags.LOTS_INPUT, callback=self._on_lots_changed)
        dpg.configure_item(tags.PUT_LOTS_INPUT, callback=self._on_lots_changed)
        dpg.configure_item(tags.PRICE_MODE_RADIO, callback=self._on_price_mode_changed)
        dpg.configure_item(tags.REFRESH_BUTTON, callback=self._on_refresh_requested)
        for strike_tag in tags.STRIKE_TAGS:
            dpg.configure_item(strike_tag, callback=self._on_strike_selected)

    @property
    def ui_write_count(self) -> int:
        """Expose cumulative widget writes for lightweight diagnostics and tests."""

        return self._writes.write_count

    def apply_events(self, events: tuple[ControllerEvent, ...]) -> None:
        for event in events:
            if isinstance(event, SnapshotEvent):
                self._apply_snapshot(event.snapshot)
            elif isinstance(event, AnalyticsEvent):
                self._apply_analytics(event.snapshot)
            elif isinstance(event, PortfolioEvent):
                self._apply_portfolio(event)
            elif isinstance(event, NoticeEvent):
                self._set_order_status(event.message, error=event.error)
            elif isinstance(event, StatusEvent):
                self._apply_status(event)

    def _apply_status(self, event: StatusEvent) -> None:
        self._writes.set_value(
            tags.CONNECTED_BROKER,
            broker_text(self._settings.broker_name, event.status.value),
        )
        if event.status == ConnectionStatus.CONNECTED:
            theme = tags.GREEN_TEXT_THEME
        elif event.status == ConnectionStatus.ERROR:
            theme = tags.RED_TEXT_THEME
        else:
            theme = tags.MUTED_TEXT_THEME
        self._writes.bind_theme(tags.CONNECTED_BROKER, theme)

    def _apply_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._snapshot = snapshot
        self._apply_sod_card(
            price=snapshot.nifty_price,
            session_open=snapshot.nifty_sod_price,
            value_tag=tags.NIFTY_VALUE,
            status_tag=tags.NIFTY_STATUS,
        )
        self._apply_sod_card(
            price=snapshot.india_vix,
            session_open=snapshot.india_vix_sod_price,
            value_tag=tags.INDIA_VIX_VALUE,
            status_tag=tags.INDIA_VIX_STATUS,
        )
        strikes = tuple(row.strike for row in snapshot.rows)
        if self._selected_strike not in strikes:
            self._selected_strike = snapshot.atm_strike

        self._writes.set_value(
            tags.UNDERLYING_PRICE,
            f"Underlying Price ({snapshot.underlying}): {_price(snapshot.spot_price)}",
        )
        self._refresh_selected_strike_text()

        for index, row in enumerate(snapshot.rows):
            self._writes.set_value(tags.CALL_BID_TAGS[index], _bid(row.call_quote))
            self._writes.set_value(tags.CALL_ASK_TAGS[index], _ask(row.call_quote))
            self._writes.configure(
                tags.STRIKE_TAGS[index],
                {"label": row.strike_label, "user_data": row.strike},
            )
            self._writes.set_value(
                tags.STRIKE_TAGS[index],
                row.strike == self._selected_strike,
            )
            self._set_strike_theme(
                tags.STRIKE_TAGS[index],
                selected=row.strike == self._selected_strike,
            )
            self._writes.set_value(tags.PUT_BID_TAGS[index], _bid(row.put_quote))
            self._writes.set_value(tags.PUT_ASK_TAGS[index], _ask(row.put_quote))
        self._refresh_selected_option_ltps()

    def _apply_sod_card(
        self,
        *,
        price: Decimal | None,
        session_open: Decimal | None,
        value_tag: str,
        status_tag: str,
    ) -> None:
        if price is None:
            value = "--"
            status = "● UNAVAILABLE"
            theme = tags.RED_TEXT_THEME
        elif session_open is None:
            value = _price(price)
            status = "● SOD N/A"
            theme = tags.YELLOW_TEXT_THEME
        elif price > session_open:
            value = _price(price)
            status = f"▲ +{_price(price - session_open)}"
            theme = tags.GREEN_TEXT_THEME
        elif price < session_open:
            value = _price(price)
            status = f"▼ {_price(price - session_open)}"
            theme = tags.RED_TEXT_THEME
        else:
            value = _price(price)
            status = "● 0.00"
            theme = tags.YELLOW_TEXT_THEME
        self._writes.set_value(value_tag, value)
        self._writes.set_value(status_tag, status)
        self._writes.bind_theme(value_tag, theme)
        self._writes.bind_theme(status_tag, theme)

    def _apply_analytics(self, snapshot: ChainAnalyticsSnapshot) -> None:
        for index, row in enumerate(snapshot.rows):
            values = (
                _metric(row.call_iv),
                _compact_metric(row.call_volume),
                _compact_metric(row.call_oi),
                format_strike(row.strike),
                _compact_metric(row.put_oi),
                _compact_metric(row.put_volume),
                _metric(row.put_iv),
                _metric(row.oi_pcr),
                _metric(row.put_volume_oi),
                _metric(row.straddle),
            )
            for tag, value in zip(tags.ANALYTICS_CELL_TAGS[index], values, strict=True):
                self._writes.set_value(tag, value)
            for tag, status in zip(
                tags.ANALYTICS_CELL_TAGS[index][7:],
                (
                    row.oi_pcr_status,
                    row.put_volume_oi_status,
                    row.straddle_status,
                ),
                strict=True,
            ):
                self._writes.bind_theme(tag, _metric_status_theme(status))
        for tag, metric_value in zip(
            tags.ANALYTICS_SUMMARY_VALUES,
            (
                snapshot.oi_pcr,
                snapshot.volume_pcr,
                snapshot.put_volume_oi,
                snapshot.call_volume_oi,
            ),
            strict=True,
        ):
            self._writes.set_value(tag, _metric(metric_value))
        summary_statuses = (
            snapshot.oi_pcr_status,
            snapshot.volume_pcr_status,
            snapshot.put_volume_oi_status,
            snapshot.call_volume_oi_status,
        )
        for index, (value_tag, status_tag, status) in enumerate(
            zip(
                tags.ANALYTICS_SUMMARY_VALUES,
                tags.ANALYTICS_SUMMARY_STATUSES,
                summary_statuses,
                strict=True,
            )
        ):
            self._writes.set_value(status_tag, str(status))
            theme = _summary_metric_theme(index, status)
            self._writes.bind_theme(value_tag, theme)
            self._writes.bind_theme(status_tag, theme)

    def select_buy_price(self, option_type: OptionType) -> bool:
        """Prefill a limit buy at selected bid plus the configured offset."""

        row = self._selected_row()
        if row is None:
            return False
        quote = row.call_quote if option_type == OptionType.CALL else row.put_quote
        if quote is None or quote.bid is None or quote.bid <= 0:
            return False
        if str(dpg.get_value(tags.PRICE_MODE_RADIO)) == "Manual":
            return True
        limit_price = (quote.bid + self._settings.default_buy_price_offset).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        price_tag = (
            tags.CALL_LIMIT_PRICE_INPUT
            if option_type == OptionType.CALL
            else tags.PUT_LIMIT_PRICE_INPUT
        )
        dpg.set_value(price_tag, float(limit_price))
        return True

    def _on_index_changed(
        self,
        _sender: str | int,
        app_data: object,
        _user_data: object,
    ) -> None:
        selected = str(app_data).strip().upper()
        self._selected_strike = None
        self._refresh_selected_strike_text()
        self._refresh_selected_option_ltps()
        self._controller.select_index(selected)

    def _on_refresh_requested(
        self,
        _sender: str | int,
        _app_data: object,
        _user_data: object,
    ) -> None:
        self._controller.refresh_market_data()
        self._set_order_status("Market refresh requested", error=False)

    def _on_strike_selected(
        self,
        sender: str | int,
        _app_data: object,
        user_data: object,
    ) -> None:
        if not isinstance(user_data, Decimal):
            return
        self._selected_strike = user_data
        self._writes.invalidate_value(sender)
        snapshot = self._snapshot
        if snapshot is None:
            return
        for strike_tag, row in zip(tags.STRIKE_TAGS, snapshot.rows, strict=True):
            is_selected = row.strike == self._selected_strike
            self._writes.set_value(strike_tag, is_selected)
            self._set_strike_theme(strike_tag, selected=is_selected)
        self._refresh_selected_strike_text()
        self._refresh_selected_option_ltps()

    def _on_lots_changed(
        self,
        _sender: str | int,
        _app_data: object,
        _user_data: object,
    ) -> None:
        self._refresh_order_total()

    def _on_price_mode_changed(
        self,
        _sender: str | int,
        _app_data: object,
        _user_data: object,
    ) -> None:
        if str(dpg.get_value(tags.PRICE_MODE_RADIO)) == "Auto":
            self.select_buy_price(OptionType.CALL)
            self.select_buy_price(OptionType.PUT)

    def _on_buy_call(
        self,
        _sender: str | int,
        _app_data: object,
        _user_data: object,
    ) -> None:
        self._submit_selected_order(OptionType.CALL)

    def _on_buy_put(
        self,
        _sender: str | int,
        _app_data: object,
        _user_data: object,
    ) -> None:
        self._submit_selected_order(OptionType.PUT)

    def _submit_selected_order(self, option_type: OptionType) -> None:
        row = self._selected_row()
        if row is None:
            self._set_order_status("Select a live strike before placing an order", error=True)
            return
        price_prefilled = self.select_buy_price(option_type)
        try:
            order_type = OrderType(str(dpg.get_value(tags.ORDER_TYPE_COMBO)).upper())
            if order_type == OrderType.LIMIT and not price_prefilled:
                raise ValueError("selected option has no valid bid")
            lots_tag = (
                tags.CALL_LOTS_INPUT
                if option_type == OptionType.CALL
                else tags.PUT_LOTS_INPUT
            )
            lots = _positive_lots(dpg.get_value(lots_tag))
            if lots is None:
                raise ValueError("lots must be at least 1")
            limit_price = (
                _input_decimal(
                    tags.CALL_LIMIT_PRICE_INPUT
                    if option_type == OptionType.CALL
                    else tags.PUT_LIMIT_PRICE_INPUT,
                    quantum=Decimal("0.01"),
                )
                if order_type == OrderType.LIMIT
                else None
            )
            risk = RiskParameters(
                target_percent=_input_decimal(
                    tags.TARGET_PERCENT_INPUT,
                    quantum=Decimal("0.001"),
                ),
                stop_loss_percent=_input_decimal(
                    tags.STOP_LOSS_PERCENT_INPUT,
                    quantum=Decimal("0.001"),
                ),
                trailing_stop_percent=_input_decimal(
                    tags.TRAILING_SL_PERCENT_INPUT,
                    quantum=Decimal("0.001"),
                ),
            )
            option = row.call if option_type == OptionType.CALL else row.put
            request = OrderRequest(
                option=option,
                order_type=order_type,
                lots=lots,
                limit_price=limit_price,
                risk=risk,
                source=OrderSource.GUI,
            )
        except (ArithmeticError, TypeError, ValueError) as exc:
            self._set_order_status(f"Order rejected: {exc}", error=True)
            return
        if not self._controller.submit_order(request):
            self._set_order_status("Order queue is full; try again", error=True)
            return
        self._set_order_status(
            f"{option_type.value} {order_type.value} order queued",
            error=False,
        )

    def _refresh_selected_strike_text(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or self._selected_strike is None:
            self._writes.set_value(tags.SELECTED_STRIKE, "Selected Strike: --")
            return
        strike = format_strike(self._selected_strike)
        self._writes.set_value(
            tags.SELECTED_STRIKE,
            f"Selected Strike: {strike} | Expiry: {snapshot.expiry:%d %b %Y}",
        )

    def _refresh_selected_option_ltps(self) -> None:
        row = self._selected_row()
        if row is None:
            call_ltp = put_ltp = "--"
        else:
            call_ltp = _price(row.call_quote.ltp if row.call_quote else None)
            put_ltp = _price(row.put_quote.ltp if row.put_quote else None)
        self._writes.set_value(tags.SELECTED_CALL_LTP, f"LTP: {call_ltp}")
        self._writes.set_value(tags.SELECTED_PUT_LTP, f"LTP: {put_ltp}")
        if str(dpg.get_value(tags.PRICE_MODE_RADIO)) == "Auto":
            self.select_buy_price(OptionType.CALL)
            self.select_buy_price(OptionType.PUT)
        self._refresh_order_total()

    def _refresh_order_total(self) -> None:
        row = self._selected_row()
        if row is None:
            self._writes.set_value(tags.CALL_ORDER_TOTAL, "CE: -- | Max: --")
            self._writes.set_value(tags.PUT_ORDER_TOTAL, "PE: -- | Max: --")
            return
        self._refresh_side_total(OptionType.CALL, row.call_quote, row.call.lot_size)
        self._refresh_side_total(OptionType.PUT, row.put_quote, row.put.lot_size)

    def _refresh_side_total(
        self,
        option_type: OptionType,
        quote: Quote | None,
        lot_size: int,
    ) -> None:
        lots_tag = (
            tags.CALL_LOTS_INPUT
            if option_type == OptionType.CALL
            else tags.PUT_LOTS_INPUT
        )
        total_tag = (
            tags.CALL_ORDER_TOTAL if option_type == OptionType.CALL else tags.PUT_ORDER_TOTAL
        )
        lots = _positive_lots(dpg.get_value(lots_tag))
        price = quote.ltp if quote is not None else None
        total = (
            "--"
            if lots is None
            else _total_price(quote=quote, lot_size=lot_size, lots=lots)
        )
        maximum = (
            0
            if price is None or price <= 0
            else int(self._available_balance // (price * lot_size))
        )
        if str(dpg.get_value(tags.PRICE_MODE_RADIO)) == "Auto" and maximum > 0:
            self._writes.set_value(lots_tag, maximum)
            lots = maximum
            total = _total_price(quote=quote, lot_size=lot_size, lots=lots)
        self._writes.set_value(total_tag, f"{option_type.value}: {total} | Max: {maximum}")

    def _apply_portfolio(self, event: PortfolioEvent) -> None:
        portfolio = event.portfolio
        balance = (
            event.account_balance if event.account_balance is not None else portfolio.cash_balance
        )
        self._writes.set_value(tags.ACCOUNT_BALANCE, account_balance_text(balance))
        self._writes.set_value(
            tags.RESERVED_BALANCE,
            reserved_balance_text(event.reserved_balance),
        )
        self._writes.set_value(
            tags.FUNDS_STATUS,
            funds_status_text(
                reserved=event.reserved_balance,
                available=event.available_balance,
            ),
        )
        self._available_balance = event.available_balance
        self._set_pnl_amount(tags.CONSOLIDATED_PNL, event.total_pnl)
        self._set_pnl_amount(tags.SUMMARY_CONSOLIDATED_PNL, event.total_pnl)
        self._refresh_position_rows(portfolio, event.position_pnl)
        self._refresh_closed_rows(portfolio.closed_positions)
        self._refresh_order_total()
        if event.executions:
            latest = event.executions[-1]
            self._set_order_status(
                latest.message,
                error=latest.kind == ExecutionEventKind.ORDER_REJECTED,
                success=latest.kind
                in {
                    ExecutionEventKind.POSITION_OPENED,
                    ExecutionEventKind.POSITION_EXITED,
                },
            )

    def _refresh_position_rows(
        self,
        portfolio: PortfolioSnapshot,
        position_pnl: Mapping[str, PositionPnl],
    ) -> None:
        current_ids = {position.position_id for position in portfolio.positions}
        for position_id in tuple(self._position_widgets):
            if position_id in current_ids:
                continue
            widgets = self._position_widgets.pop(position_id)
            if dpg.does_item_exist(widgets.row):
                dpg.delete_item(widgets.row)

        for position in portfolio.positions:
            row_widgets = self._position_widgets.get(position.position_id)
            if row_widgets is None:
                row_widgets = self._create_position_row(position)
                self._position_widgets[position.position_id] = row_widgets
            self._update_position_row(
                row_widgets,
                position,
                position_pnl[position.position_id],
            )

        pending_ids = {pending.order_id for pending in portfolio.pending_orders}
        for order_id in tuple(self._pending_widgets):
            if order_id in pending_ids:
                continue
            pending_row_widgets = self._pending_widgets.pop(order_id)
            if dpg.does_item_exist(pending_row_widgets.row):
                dpg.delete_item(pending_row_widgets.row)

        for pending in portfolio.pending_orders:
            pending_widgets = self._pending_widgets.get(pending.order_id)
            if pending_widgets is None:
                pending_widgets = self._create_pending_row(pending)
                self._pending_widgets[pending.order_id] = pending_widgets
            self._update_pending_row(pending_widgets, pending)

        self._writes.configure(
            tags.PORTFOLIO_PLACEHOLDER_ROW,
            {"show": not portfolio.positions and not portfolio.pending_orders},
        )

    def _refresh_closed_rows(self, closed_positions: tuple[ClosedPosition, ...]) -> None:
        current_ids = {closed.position.position_id for closed in closed_positions}
        for position_id in tuple(self._closed_rows):
            if position_id not in current_ids:
                row = self._closed_rows.pop(position_id)
                if dpg.does_item_exist(row):
                    dpg.delete_item(row)
        for closed in closed_positions:
            position = closed.position
            if position.position_id in self._closed_rows:
                continue
            row_tag = f"ktrader::closed::{position.position_id}"
            with dpg.table_row(parent=tags.CLOSED_POSITIONS_TABLE, tag=row_tag):
                dpg.add_text(position.option.instrument.trading_symbol)
                dpg.add_text(f"+{position.lots}")
                dpg.add_text(_price(position.entry_price))
                dpg.add_text(_price(position.current_price))
                dpg.add_text(_price(position.cost))
                dpg.add_text(_price(position.market_value))
                pnl = dpg.add_text(pnl_amount_text(closed.realized_pnl))
                percent = dpg.add_text(pnl_percent_text(position.pnl_percent))
                dpg.add_text(closed.exit_reason.value)
            theme = tags.GREEN_TEXT_THEME if closed.realized_pnl >= 0 else tags.RED_TEXT_THEME
            dpg.bind_item_theme(pnl, theme)
            dpg.bind_item_theme(percent, theme)
            self._closed_rows[position.position_id] = row_tag

    def _create_position_row(self, position: Position) -> _PositionWidgets:
        base = f"ktrader::position::{position.position_id}"
        widgets = _PositionWidgets(
            row=f"{base}::row",
            symbol=f"{base}::symbol",
            lots=f"{base}::lots",
            average=f"{base}::average",
            pnl=f"{base}::pnl",
            pnl_percent=f"{base}::pnl_percent",
            invested=f"{base}::invested",
            current=f"{base}::current",
            exit_button=f"{base}::exit",
        )
        with dpg.table_row(parent=tags.PORTFOLIO_TABLE, tag=widgets.row):
            dpg.add_text("--", tag=widgets.symbol)
            dpg.add_text("--", tag=widgets.lots)
            dpg.add_text("--", tag=widgets.average)
            dpg.add_text("--", tag=widgets.pnl)
            dpg.add_text("--", tag=widgets.pnl_percent)
            dpg.add_text("--", tag=widgets.invested)
            dpg.add_text("--", tag=widgets.current)
            dpg.add_button(
                label="EXIT",
                tag=widgets.exit_button,
                width=80,
                callback=self._on_exit_position,
                user_data=position.position_id,
            )
        dpg.bind_item_theme(widgets.exit_button, tags.EXIT_THEME)
        return widgets

    def _create_pending_row(self, pending: PendingOrder) -> _PendingOrderWidgets:
        base = f"ktrader::pending::{pending.order_id}"
        widgets = _PendingOrderWidgets(
            row=f"{base}::row",
            symbol=f"{base}::symbol",
            lots=f"{base}::lots",
            limit=f"{base}::limit",
            status=f"{base}::status",
            pnl_percent=f"{base}::pnl_percent",
            invested=f"{base}::invested",
            current=f"{base}::current",
            action=f"{base}::action",
        )
        with dpg.table_row(parent=tags.PORTFOLIO_TABLE, tag=widgets.row):
            dpg.add_text("--", tag=widgets.symbol)
            dpg.add_text("--", tag=widgets.lots)
            dpg.add_text("--", tag=widgets.limit)
            dpg.add_text("PENDING", tag=widgets.status)
            dpg.add_text("--", tag=widgets.pnl_percent)
            dpg.add_text("--", tag=widgets.invested)
            dpg.add_text("--", tag=widgets.current)
            dpg.add_button(
                label="EXIT",
                tag=widgets.action,
                width=80,
                callback=self._on_exit_position,
                user_data=pending.order_id,
            )
        dpg.bind_item_theme(widgets.status, tags.ORANGE_TEXT_THEME)
        dpg.bind_item_theme(widgets.action, tags.EXIT_THEME)
        return widgets

    def _update_pending_row(
        self,
        widgets: _PendingOrderWidgets,
        pending: PendingOrder,
    ) -> None:
        request = pending.request
        self._writes.set_value(
            widgets.symbol,
            f"{request.option.instrument.trading_symbol} [LIMIT]",
        )
        self._writes.set_value(widgets.lots, f"+{pending.lots}")
        self._writes.set_value(widgets.limit, _price(request.limit_price))
        self._writes.set_value(widgets.status, "PENDING")
        self._writes.set_value(widgets.invested, _price(pending.reserved_cash))
        self._writes.set_value(widgets.current, "--")

    def _update_position_row(
        self,
        widgets: _PositionWidgets,
        position: Position,
        pnl: PositionPnl,
    ) -> None:
        self._writes.set_value(
            widgets.symbol,
            position.option.instrument.trading_symbol,
        )
        self._writes.set_value(widgets.lots, f"+{position.lots}")
        self._writes.set_value(widgets.average, _price(position.entry_price))
        self._writes.set_value(widgets.pnl, pnl_amount_text(pnl.amount))
        self._writes.set_value(
            widgets.pnl_percent,
            pnl_percent_text(pnl.percentage),
        )
        self._writes.set_value(widgets.invested, _price(position.cost))
        self._writes.set_value(widgets.current, _price(position.market_value))
        theme = tags.GREEN_TEXT_THEME if pnl.amount >= 0 else tags.RED_TEXT_THEME
        self._writes.bind_theme(widgets.pnl, theme)
        self._writes.bind_theme(widgets.pnl_percent, theme)

    def _on_exit_position(
        self,
        _sender: str | int,
        _app_data: object,
        user_data: object,
    ) -> None:
        if not isinstance(user_data, str) or not self._controller.exit_position(user_data):
            self._set_order_status("Unable to queue exit", error=True)
            return
        self._set_order_status("Exit/cancel queued", error=False)

    def _set_order_status(
        self,
        message: str,
        *,
        error: bool,
        success: bool = False,
    ) -> None:
        display_message = message.strip()
        if display_message.lower().startswith("order rejected:"):
            display_message = "Rejected:" + display_message[len("Order rejected:") :]
        display_message = display_message.replace(
            "insufficient available balance",
            "insufficient balance",
        )
        self._writes.set_value(tags.ORDER_STATUS, f"Status: {display_message}")
        if error:
            theme = tags.RED_TEXT_THEME
        elif success:
            theme = tags.GREEN_TEXT_THEME
        else:
            theme = tags.ORANGE_TEXT_THEME
        self._writes.bind_theme(tags.ORDER_STATUS, theme)

    def _set_strike_theme(self, strike_tag: str, *, selected: bool) -> None:
        theme = tags.SELECTED_STRIKE_THEME if selected else tags.STRIKE_DEFAULT_THEME
        self._writes.bind_theme(strike_tag, theme)

    def _set_pnl_amount(self, item: str | int, amount: Decimal) -> None:
        self._writes.set_value(item, pnl_amount_text(amount))
        theme = tags.GREEN_TEXT_THEME if amount >= 0 else tags.RED_TEXT_THEME
        self._writes.bind_theme(item, theme)

    def _selected_row(self) -> ChainRow | None:
        snapshot = self._snapshot
        if snapshot is None or self._selected_strike is None:
            return None
        return next(
            (row for row in snapshot.rows if row.strike == self._selected_strike),
            None,
        )


def _price(value: Decimal | None) -> str:
    return "--" if value is None else f"{value:,.2f}"


def _metric(value: Decimal | None) -> str:
    return "--" if value is None else f"{value:,.2f}"


def _metric_status_theme(status: object) -> str:
    normalized = str(status).upper()
    if normalized == "BULLISH":
        return tags.GREEN_TEXT_THEME
    if normalized == "BEARISH":
        return tags.RED_TEXT_THEME
    return tags.YELLOW_TEXT_THEME


def _summary_metric_theme(metric_index: int, status: object) -> str:
    if metric_index < 2:
        return _metric_status_theme(status)

    normalized = " ".join(str(status).upper().replace("_", " ").split())
    if metric_index == 2:  # Put-side activity, interpreted for the underlying.
        bullish = {"WRITING", "SHORT BUILD", "LONG UNWIND", "LONG UNWINDING"}
        bearish = {
            "LONG BUILD",
            "SHORT COVER",
            "SHORT COVERING",
            "SHORT UNWIND",
            "SHORT UNWINDING",
        }
    else:  # Call-side activity, interpreted for the underlying.
        bullish = {
            "LONG BUILD",
            "SHORT COVER",
            "SHORT COVERING",
            "SHORT UNWIND",
            "SHORT UNWINDING",
        }
        bearish = {"WRITING", "SHORT BUILD", "LONG UNWIND", "LONG UNWINDING"}
    if normalized in bullish:
        return tags.GREEN_TEXT_THEME
    if normalized in bearish:
        return tags.RED_TEXT_THEME
    return tags.YELLOW_TEXT_THEME


def _compact_metric(value: Decimal | None) -> str:
    """Keep high-volume/OI figures fully visible in narrow analytics columns."""
    if value is None:
        return "--"
    absolute = abs(value)
    if absolute >= Decimal("10000000"):
        return f"{value / Decimal('10000000'):,.2f}Cr"
    if absolute >= Decimal("1000000"):
        return f"{value / Decimal('1000000'):,.2f}M"
    if absolute >= Decimal("1000"):
        return f"{value / Decimal('1000'):,.2f}K"
    return f"{value:,.0f}"


def _bid(quote: Quote | None) -> str:
    return _price(quote.bid if quote is not None else None)


def _ask(quote: Quote | None) -> str:
    return _price(quote.ask if quote is not None else None)


def _positive_lots(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _total_price(*, quote: Quote | None, lot_size: int, lots: int) -> str:
    if quote is None or quote.ltp is None or quote.ltp < 0:
        return "--"
    return _price(quote.ltp * lot_size * lots)


def _risk_text(risk: RiskParameters) -> str:
    def percentage(value: Decimal) -> str:
        return "OFF" if value <= 0 else f"{format(value.normalize(), 'f')}%"

    return " / ".join(
        (
            percentage(risk.target_percent),
            percentage(risk.stop_loss_percent),
            percentage(risk.trailing_stop_percent),
        )
    )


def _input_decimal(tag: str, *, quantum: Decimal) -> Decimal:
    value = dpg.get_value(tag)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("input must be numeric")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("input must be a non-negative finite number")
    return parsed.quantize(quantum, rounding=ROUND_HALF_UP)
