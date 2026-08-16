from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.domain.models import Exchange, InstrumentToken, OptionContract, OptionType
from app.instruments.master import InstrumentMaster
from app.optionchain.atm import build_strike_window, round_to_nearest_strike, select_option_window


def _contract(underlying: str, strike: Decimal, option_type: OptionType) -> OptionContract:
    return OptionContract(
        underlying=underlying,
        expiry=date(2026, 7, 30),
        strike=strike,
        option_type=option_type,
        token=InstrumentToken(
            exchange=Exchange.NFO,
            token=f"{underlying}-{strike}-{option_type.value}",
            symbol=underlying,
            trading_symbol=f"{underlying}{strike}{option_type.value}",
        ),
    )


class OptionChainSelectionTests(unittest.TestCase):
    def test_round_to_nearest_nifty_strike(self) -> None:
        self.assertEqual(round_to_nearest_strike(Decimal("24124"), Decimal("50")), Decimal("24100"))
        self.assertEqual(round_to_nearest_strike(Decimal("24125"), Decimal("50")), Decimal("24150"))

    def test_build_strike_window_has_atm_plus_four_each_side(self) -> None:
        strikes = build_strike_window(
            atm_strike=Decimal("24150"),
            strike_interval=Decimal("50"),
            each_side=4,
        )
        self.assertEqual(len(strikes), 9)
        self.assertEqual(strikes[0], Decimal("23950"))
        self.assertEqual(strikes[4], Decimal("24150"))
        self.assertEqual(strikes[-1], Decimal("24350"))

    def test_select_option_window_returns_ce_and_pe_for_each_strike(self) -> None:
        strikes = build_strike_window(
            atm_strike=Decimal("24150"),
            strike_interval=Decimal("50"),
            each_side=4,
        )
        options = tuple(
            _contract("NIFTY", strike, option_type)
            for strike in strikes
            for option_type in (OptionType.CALL, OptionType.PUT)
        )
        master = InstrumentMaster(options=options, spot_tokens={})

        atm, selected = select_option_window(
            master=master,
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24149"),
            each_side=4,
        )

        self.assertEqual(atm, Decimal("24150"))
        self.assertEqual(len(selected), 18)
        self.assertEqual({contract.option_type for contract in selected}, {OptionType.CALL, OptionType.PUT})

    def test_select_option_window_matches_scaled_strike_values(self) -> None:
        contract = _contract("NIFTY", Decimal("24400"), OptionType.CALL)
        master = InstrumentMaster(options=(contract,), spot_tokens={})

        atm, selected = select_option_window(
            master=master,
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("2438385"),
            each_side=0,
        )

        self.assertEqual(atm, Decimal("24400"))
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].strike, Decimal("24400"))


if __name__ == "__main__":
    unittest.main()
