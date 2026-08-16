from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.models import (
    Exchange,
    GreeksSnapshot,
    InstrumentToken,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
)
from app.greeks.strike_selector import OptimalStrikeSelector


def _quote(strike: Decimal, option_type: OptionType, delta: Decimal, volume: int) -> OptionQuote:
    contract = OptionContract(
        underlying="NIFTY",
        expiry=date(2026, 7, 30),
        strike=strike,
        option_type=option_type,
        token=InstrumentToken(
            exchange=Exchange.NFO,
            token=f"{strike}-{option_type.value}",
            symbol="NIFTY",
            trading_symbol=f"NIFTY30JUL26{strike}{option_type.value}",
        ),
    )
    return OptionQuote(
        contract=contract,
        ltp=Decimal("100"),
        oi=10000,
        volume=volume,
        bid=Decimal("99.5"),
        ask=Decimal("100.5"),
        greeks=GreeksSnapshot(
            contract=contract,
            captured_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            implied_volatility=Decimal("15"),
            delta=delta,
        ),
    )


class OptimalStrikeSelectorTests(unittest.TestCase):
    def test_rejects_extreme_delta_strikes(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24225"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            quotes=(
                _quote(Decimal("24200"), OptionType.CALL, Decimal("1.00"), 10000),
                _quote(Decimal("24250"), OptionType.CALL, Decimal("0.00"), 10000),
            ),
        )

        selected = OptimalStrikeSelector().select_optimal_strike(snapshot, "BUY_CALL")

        self.assertIsNone(selected)

    def test_selects_liquid_strike_inside_delta_band(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24225"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            quotes=(
                _quote(Decimal("24200"), OptionType.PUT, Decimal("-0.34"), 10000),
                _quote(Decimal("24250"), OptionType.PUT, Decimal("-0.52"), 10000),
                _quote(Decimal("24300"), OptionType.PUT, Decimal("-0.90"), 10000),
            ),
        )

        selected = OptimalStrikeSelector().select_optimal_strike(snapshot, "BUY_PUT")

        self.assertIsNotNone(selected)
        self.assertEqual(selected.contract.strike, Decimal("24250"))

    def test_rejects_wide_spread_even_when_delta_and_volume_are_good(self) -> None:
        quote = _quote(Decimal("24250"), OptionType.CALL, Decimal("0.50"), 10000)
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24225"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
            quotes=(
                OptionQuote(
                    contract=quote.contract,
                    ltp=quote.ltp,
                    oi=quote.oi,
                    volume=quote.volume,
                    bid=Decimal("95"),
                    ask=Decimal("105"),
                    greeks=quote.greeks,
                ),
            ),
        )

        self.assertIsNone(
            OptimalStrikeSelector().select_optimal_strike(snapshot, "BUY_CALL")
        )

    def test_expiry_day_uses_atm_fallback_when_broker_deltas_are_extreme(
        self,
    ) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24225"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 30, 9, 30, tzinfo=UTC),
            quotes=(
                _quote(
                    Decimal("24200"),
                    OptionType.CALL,
                    Decimal("0.70"),
                    10000,
                ),
                _quote(
                    Decimal("24250"),
                    OptionType.CALL,
                    Decimal("0.05"),
                    20000,
                ),
            ),
        )

        selected = OptimalStrikeSelector().select_optimal_strike(
            snapshot,
            "BUY_CALL",
            expiry_day_fallback_enabled=True,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.contract.strike, Decimal("24200"))

    def test_expiry_day_fallback_requires_explicit_profile_flag(self) -> None:
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24225"),
            atm_strike=Decimal("24200"),
            captured_at=datetime(2026, 7, 30, 9, 30, tzinfo=UTC),
            quotes=(
                _quote(
                    Decimal("24200"),
                    OptionType.CALL,
                    Decimal("0.70"),
                    10000,
                ),
            ),
        )

        selected = OptimalStrikeSelector().select_optimal_strike(
            snapshot,
            "BUY_CALL",
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
