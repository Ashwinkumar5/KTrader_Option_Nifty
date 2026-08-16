from __future__ import annotations

from datetime import date
from decimal import Decimal

from ktrader_simulator.domain.models import OptionType
from ktrader_simulator.market.instruments import InstrumentCatalog


def _index_rows(
    underlying: str,
    *,
    spot_exchange: str,
    option_exchange: str,
    atm: int,
    interval: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "exch_seg": spot_exchange,
            "symbol": underlying,
            "name": underlying,
            "token": f"{underlying}-SPOT",
        }
    ]
    for offset in range(-3, 4):
        strike = atm + offset * interval
        for option_type in OptionType:
            rows.append(
                {
                    "exch_seg": option_exchange,
                    "instrumenttype": "OPTIDX",
                    "symbol": f"{underlying}31DEC99{strike}{option_type.value}",
                    "name": underlying,
                    "token": f"{underlying}-{strike}-{option_type.value}",
                    "expiry": "31DEC2099",
                    "strike": str(strike * 100),
                    "lotsize": "25",
                }
            )
    return rows


def test_catalog_supports_all_four_indices_and_selects_five_paired_strikes() -> None:
    rows = [
        *_index_rows(
            "NIFTY",
            spot_exchange="NSE",
            option_exchange="NFO",
            atm=22500,
            interval=50,
        ),
        *_index_rows(
            "BANKNIFTY",
            spot_exchange="NSE",
            option_exchange="NFO",
            atm=48000,
            interval=100,
        ),
        *_index_rows(
            "SENSEX",
            spot_exchange="BSE",
            option_exchange="BFO",
            atm=74000,
            interval=100,
        ),
        *_index_rows(
            "BANKEX",
            spot_exchange="BSE",
            option_exchange="BFO",
            atm=56000,
            interval=100,
        ),
    ]
    catalog = InstrumentCatalog.from_rows(
        rows,
        supported_indices=("NIFTY", "SENSEX", "BANKNIFTY", "BANKEX"),
    )

    assert set(catalog.available_indices) == {
        "NIFTY",
        "SENSEX",
        "BANKNIFTY",
        "BANKEX",
    }

    window = catalog.window(
        underlying="NIFTY",
        spot_price=Decimal("22520"),
        as_of=date(2026, 7, 31),
    )

    assert window.atm_strike == Decimal("22500")
    assert window.strikes == tuple(Decimal(value) for value in (22400, 22450, 22500, 22550, 22600))
    assert len(window.calls) == len(window.puts) == 5
    assert all(option.lot_size == 25 for option in (*window.calls, *window.puts))


def test_catalog_extracts_india_vix_reference_without_treating_it_as_an_index() -> None:
    rows = [
        *_index_rows(
            "NIFTY",
            spot_exchange="NSE",
            option_exchange="NFO",
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

    catalog = InstrumentCatalog.from_rows(rows, supported_indices=("NIFTY",))

    reference = catalog.reference_for("INDIA_VIX")
    assert reference is not None
    assert reference.exchange == "NSE"
    assert reference.token == "INDIA-VIX"
