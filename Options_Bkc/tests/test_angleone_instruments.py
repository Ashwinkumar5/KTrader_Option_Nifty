from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.broker.angleone.instruments import build_instrument_master
from app.domain.models import OptionType


class AngleOneInstrumentParserTests(unittest.TestCase):
    def test_build_instrument_master_parses_index_and_options(self) -> None:
        master = build_instrument_master(
            [
                {
                    "exch_seg": "NSE",
                    "token": "99926000",
                    "symbol": "Nifty 50",
                    "name": "NIFTY",
                },
                {
                    "exch_seg": "NSE",
                    "token": "99926017",
                    "symbol": "India VIX",
                    "name": "India VIX",
                },
                {
                    "exch_seg": "NFO",
                    "token": "future-1",
                    "symbol": "NIFTY30JUL26FUT",
                    "name": "NIFTY",
                    "expiry": "30JUL2026",
                    "lotsize": "75",
                    "instrumenttype": "FUTIDX",
                },
                {
                    "exch_seg": "NFO",
                    "token": "12345",
                    "symbol": "NIFTY30JUL2624150CE",
                    "name": "NIFTY",
                    "expiry": "30JUL2026",
                    "strike": "2415000",
                    "lotsize": "75",
                    "instrumenttype": "OPTIDX",
                },
            ],
            underlyings=("NIFTY", "BANKNIFTY"),
        )

        self.assertEqual(master.spot_tokens["NIFTY"].token, "99926000")
        self.assertEqual(
            master.reference_tokens["INDIA_VIX"].token,
            "99926017",
        )
        self.assertEqual(len(master.options), 1)
        contract = master.options[0]
        self.assertEqual(contract.expiry, date(2026, 7, 30))
        self.assertEqual(contract.strike, Decimal("24150"))
        self.assertEqual(contract.option_type, OptionType.CALL)
        self.assertEqual(contract.lot_size, 75)
        self.assertEqual(len(master.futures), 1)
        self.assertEqual(master.futures[0].token.token, "future-1")

    def test_build_instrument_master_accepts_master_file_segment_names(self) -> None:
        master = build_instrument_master(
            [
                {
                    "exch_seg": "nse_cm",
                    "token": "99926000",
                    "symbol": "Nifty 50",
                    "name": "NIFTY",
                    "instrumenttype": "",
                },
                {
                    "exch_seg": "nse_fo",
                    "token": "12345",
                    "symbol": "NIFTY30JUL2624150PE",
                    "name": "NIFTY",
                    "expiry": "30JUL2026",
                    "strike": "2415000",
                    "lotsize": "75",
                    "instrumenttype": "PE",
                },
            ],
            underlyings=("NIFTY",),
        )

        self.assertEqual(master.spot_tokens["NIFTY"].token, "99926000")
        self.assertEqual(master.options[0].option_type, OptionType.PUT)

    def test_nifty_does_not_capture_finnifty_contracts(self) -> None:
        master = build_instrument_master(
            [
                {
                    "exch_seg": "NFO",
                    "token": "bad",
                    "symbol": "FINNIFTY30JUL2624200CE",
                    "name": "FINNIFTY",
                    "expiry": "30JUL2026",
                    "strike": "2420000",
                    "lotsize": "60",
                    "instrumenttype": "OPTIDX",
                },
                {
                    "exch_seg": "NFO",
                    "token": "good",
                    "symbol": "NIFTY30JUL2624200CE",
                    "name": "NIFTY",
                    "expiry": "30JUL2026",
                    "strike": "2420000",
                    "lotsize": "65",
                    "instrumenttype": "OPTIDX",
                },
            ],
            underlyings=("NIFTY",),
        )

        self.assertEqual([contract.token.token for contract in master.options], ["good"])


if __name__ == "__main__":
    unittest.main()
