from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ktrader_simulator.config import Settings, load_settings
from ktrader_simulator.domain.models import (
    Instrument,
    MarketSnapshot,
    Moneyness,
    OptionInstrument,
    OptionType,
    Quote,
)
from ktrader_simulator.market.snapshots import MarketSnapshotService
from tests.test_instruments import _index_rows


class FakeReadOnlyBroker:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._rows = rows
        self.quote_requests: list[tuple[Instrument, ...]] = []
        self.iv_requests: list[tuple[str, date, tuple[OptionInstrument, ...]]] = []

    async def connect(self) -> None:
        return None

    async def instrument_master(self) -> Sequence[Mapping[str, object]]:
        return self._rows

    async def quotes(self, instruments: tuple[Instrument, ...]) -> Mapping[str, Quote]:
        self.quote_requests.append(instruments)
        captured_at = datetime.now(UTC)
        quotes: dict[str, Quote] = {}
        for instrument in instruments:
            session_open = None
            if instrument.token.endswith("-SPOT"):
                ltp = Decimal("22520")
                bid = ask = None
                session_open = Decimal("22480")
            elif instrument.token == "INDIA-VIX":
                ltp = Decimal("14.25")
                bid = Decimal("14.20")
                ask = Decimal("14.30")
                session_open = Decimal("13.80")
            else:
                strike = Decimal(instrument.token.split("-")[1])
                ltp = Decimal("100") + (strike - Decimal("22500")) / Decimal("10")
                bid = ltp - Decimal("0.50")
                ask = ltp + Decimal("0.50")
            quotes[instrument.token] = Quote(
                token=instrument.token,
                ltp=ltp,
                bid=bid,
                ask=ask,
                captured_at=captured_at,
                session_open=session_open,
            )
        return quotes

    async def implied_volatilities(
        self,
        *,
        underlying: str,
        expiry: date,
        options: tuple[OptionInstrument, ...],
    ) -> Mapping[str, Decimal]:
        self.iv_requests.append((underlying, expiry, options))
        return {
            option.instrument.token: (
                Decimal("18.25")
                if option.option_type == OptionType.CALL
                else Decimal("19.50")
            )
            for option in options
        }


async def _snapshot(settings: Settings) -> tuple[FakeReadOnlyBroker, MarketSnapshot]:
    rows = _index_rows(
        "NIFTY",
        spot_exchange="NSE",
        option_exchange="NFO",
        atm=22500,
        interval=50,
    )
    rows.append(
        {
            "exch_seg": "NSE",
            "symbol": "India VIX",
            "name": "INDIA VIX INDEX",
            "token": "INDIA-VIX",
        }
    )
    broker = FakeReadOnlyBroker(rows)
    service = await MarketSnapshotService.create(broker=broker, settings=settings)
    snapshot = await service.snapshot("NIFTY")
    return broker, snapshot


def test_snapshot_extracts_atm_bid_ask_and_moneyness(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})

    broker, snapshot = asyncio.run(_snapshot(settings))

    assert snapshot.spot_price == Decimal("22520")
    assert snapshot.india_vix == Decimal("14.25")
    assert snapshot.india_vix_sod_price == Decimal("13.80")
    assert snapshot.nifty_price == Decimal("22520")
    assert snapshot.nifty_sod_price == Decimal("22480")
    assert snapshot.atm_strike == Decimal("22500")
    assert len(snapshot.rows) == 5
    assert snapshot.rows[0].call_moneyness == Moneyness.ITM
    assert snapshot.rows[0].put_moneyness == Moneyness.OTM
    assert snapshot.rows[2].call_moneyness == Moneyness.ATM
    assert snapshot.rows[4].call_moneyness == Moneyness.OTM
    assert snapshot.rows[4].put_moneyness == Moneyness.ITM
    assert snapshot.rows[2].call_quote is not None
    assert snapshot.rows[2].call_quote.bid == Decimal("99.50")
    assert len(broker.quote_requests) == 2
    assert any(
        instrument.token == "INDIA-VIX" for instrument in broker.quote_requests[1]
    )
    assert all(
        instrument.token != "NIFTY-SPOT" for instrument in broker.quote_requests[1]
    )


def test_non_nifty_snapshot_keeps_nifty_in_same_bulk_quote(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})
    rows = [
        *_index_rows(
            "NIFTY",
            spot_exchange="NSE",
            option_exchange="NFO",
            atm=22500,
            interval=50,
        ),
        *_index_rows(
            "SENSEX",
            spot_exchange="BSE",
            option_exchange="BFO",
            atm=22500,
            interval=50,
        ),
        {
            "exch_seg": "NSE",
            "symbol": "India VIX",
            "name": "INDIA VIX INDEX",
            "token": "INDIA-VIX",
        },
    ]
    broker = FakeReadOnlyBroker(rows)

    async def fetch() -> MarketSnapshot:
        service = await MarketSnapshotService.create(broker=broker, settings=settings)
        return await service.snapshot("SENSEX")

    snapshot = asyncio.run(fetch())

    assert snapshot.nifty_price == Decimal("22520")
    assert len(broker.quote_requests) == 2
    assert any(
        instrument.token == "NIFTY-SPOT" for instrument in broker.quote_requests[1]
    )


def test_greeks_enrich_only_the_analytics_snapshot(tmp_path: Path) -> None:
    simulator_root = tmp_path / "KTrader Simulator"
    simulator_root.mkdir()
    settings = load_settings(simulator_root=simulator_root, environ={})
    rows = _index_rows(
        "NIFTY",
        spot_exchange="NSE",
        option_exchange="NFO",
        atm=22500,
        interval=50,
    )
    broker = FakeReadOnlyBroker(rows)

    async def fetch() -> tuple[MarketSnapshot, MarketSnapshot]:
        service = await MarketSnapshotService.create(broker=broker, settings=settings)
        snapshot = await service.snapshot("NIFTY")
        return snapshot, await service.with_implied_volatilities(snapshot)

    original, enriched = asyncio.run(fetch())

    assert original.rows[2].call_quote is not None
    assert original.rows[2].call_quote.implied_volatility is None
    assert enriched.rows[2].call_quote is not None
    assert enriched.rows[2].put_quote is not None
    assert enriched.rows[2].call_quote.implied_volatility == Decimal("18.25")
    assert enriched.rows[2].put_quote.implied_volatility == Decimal("19.50")
    assert len(broker.iv_requests) == 1
