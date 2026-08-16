from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Protocol

from ktrader_simulator.domain.models import Instrument, OptionInstrument, Quote
from ktrader_simulator.trading.models import OrderRequest, Position


class ReadOnlyBroker(Protocol):
    async def connect(self) -> None:
        """Authenticate a read-only market-data session."""

    async def instrument_master(self) -> Sequence[Mapping[str, object]]:
        """Return broker instrument rows."""

    async def quotes(self, instruments: tuple[Instrument, ...]) -> Mapping[str, Quote]:
        """Return a normalized quote snapshot keyed by instrument token."""

    async def implied_volatilities(
        self,
        *,
        underlying: str,
        expiry: date,
        options: tuple[OptionInstrument, ...],
    ) -> Mapping[str, Decimal]:
        """Return broker IV values keyed by option token."""


class LiveOrderRouter(Protocol):
    async def connect(self) -> None:
        """Authenticate the explicitly enabled live-order session."""

    async def place_entry(self, request: OrderRequest, *, lots: int) -> str:
        """Submit one BUY and return the broker order ID."""

    async def exit_position(self, position: Position) -> str:
        """Submit one market SELL and return the broker order ID."""

    async def cancel_order(self, broker_order_id: str) -> str:
        """Cancel one pending broker order and return its broker order ID."""

    async def available_balance(self) -> Decimal | None:
        """Return broker available cash when provided by the RMS endpoint."""
