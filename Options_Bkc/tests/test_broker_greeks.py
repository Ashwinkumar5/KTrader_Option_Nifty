from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from app.domain.models import Exchange, InstrumentToken, OptionContract, OptionType
from app.greeks.broker import normalize_broker_greeks, option_greek_params


class BrokerGreeksTests(unittest.TestCase):
    def test_normalize_broker_greeks_matches_contract_by_trading_symbol(self) -> None:
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("24150"),
            option_type=OptionType.CALL,
            token=InstrumentToken(
                exchange=Exchange.NFO,
                token="12345",
                symbol="NIFTY",
                trading_symbol="NIFTY30JUL2624150CE",
            ),
        )

        snapshots = normalize_broker_greeks(
            {
                "data": [
                    {
                        "tradingSymbol": "NIFTY30JUL2624150CE",
                        "impliedVolatility": "14.25",
                        "delta": "0.52",
                        "gamma": "0.001",
                        "theta": "-3.2",
                        "vega": "10.4",
                    }
                ]
            },
            contracts=(contract,),
        )

        self.assertEqual(snapshots["12345"].implied_volatility, Decimal("14.25"))
        self.assertEqual(snapshots["12345"].delta, Decimal("0.52"))

    def test_option_greek_params_use_smartapi_expiry_format(self) -> None:
        self.assertEqual(
            option_greek_params(underlying="nifty", expiry=date(2026, 7, 30)),
            {"name": "NIFTY", "expirydate": "30JUL2026"},
        )


if __name__ == "__main__":
    unittest.main()
