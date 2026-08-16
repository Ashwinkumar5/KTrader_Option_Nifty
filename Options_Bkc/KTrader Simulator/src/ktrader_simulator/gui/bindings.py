from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import dearpygui.dearpygui as dpg

from ktrader_simulator.gui import tags

_CURRENCY_QUANTUM = Decimal("0.01")


def broker_text(broker_name: str, status: str = "DISCONNECTED") -> str:
    normalized = broker_name.strip().upper()
    normalized_status = status.strip().upper()
    return f"Broker: {normalized or 'UNAVAILABLE'} [{normalized_status}]"


def account_balance_text(balance: Decimal) -> str:
    """Return a compact amount for the fixed-width summary card."""
    if not balance.is_finite():
        raise ValueError("account balance must be finite")
    normalized = balance.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"₹{normalized:,.2f}"


def funds_status_text(*, reserved: Decimal, available: Decimal) -> str:
    """Return the available amount; reserved is displayed on its own row."""
    if not reserved.is_finite() or not available.is_finite():
        raise ValueError("fund balances must be finite")
    normalized_available = available.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"Available: ₹{normalized_available:,.2f}"


def reserved_balance_text(reserved: Decimal) -> str:
    if not reserved.is_finite():
        raise ValueError("reserved balance must be finite")
    normalized = reserved.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"Reserved: ₹{normalized:,.2f}"


def set_account_balance(balance: Decimal) -> None:
    """Update the balance widget; call only from the Dear PyGui main thread."""

    dpg.set_value(tags.ACCOUNT_BALANCE, account_balance_text(balance))


def set_connected_broker(broker_name: str) -> None:
    """Update the broker widget; call only from the Dear PyGui main thread."""

    dpg.set_value(tags.CONNECTED_BROKER, broker_text(broker_name))


def pnl_amount_text(amount: Decimal) -> str:
    if not amount.is_finite():
        raise ValueError("P&L amount must be finite")
    normalized = amount.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{normalized:+,.2f}"


def pnl_percent_text(percentage: Decimal) -> str:
    if not percentage.is_finite():
        raise ValueError("P&L percentage must be finite")
    normalized = percentage.quantize(_CURRENCY_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{normalized:+.2f}%"


def set_position_pnl(
    *,
    amount_item: str | int,
    percent_item: str | int,
    amount: Decimal,
    percentage: Decimal,
) -> None:
    """Update position P&L values and bind green for gains or red for losses."""

    dpg.set_value(amount_item, pnl_amount_text(amount))
    dpg.set_value(percent_item, pnl_percent_text(percentage))
    theme = tags.GREEN_TEXT_THEME if amount >= 0 else tags.RED_TEXT_THEME
    dpg.bind_item_theme(amount_item, theme)
    dpg.bind_item_theme(percent_item, theme)


def set_pnl_amount(*, item: str | int, amount: Decimal) -> None:
    """Update one P&L widget with a signed value and matching color."""

    dpg.set_value(item, pnl_amount_text(amount))
    theme = tags.GREEN_TEXT_THEME if amount >= 0 else tags.RED_TEXT_THEME
    dpg.bind_item_theme(item, theme)
