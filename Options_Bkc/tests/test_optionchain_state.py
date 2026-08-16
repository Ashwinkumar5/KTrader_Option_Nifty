from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.models import (
    Exchange,
    FutureContract,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionContract,
    OptionType,
)
from app.instruments.master import InstrumentMaster
from app.optionchain.state import OptionChainState


def _contract(strike: Decimal, option_type: OptionType) -> OptionContract:
    return OptionContract(
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


class OptionChainStateTests(unittest.TestCase):
    def test_market_snapshot_includes_nearest_future_flow_and_basis(self) -> None:
        at = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
        spot_token = InstrumentToken(
            Exchange.NSE,
            "spot",
            "NIFTY",
            "NIFTY",
            InstrumentKind.INDEX,
        )
        future_token = InstrumentToken(
            Exchange.NFO,
            "future",
            "NIFTY",
            "NIFTY30JUL26FUT",
            InstrumentKind.FUTURE,
        )
        vix_token = InstrumentToken(
            Exchange.NSE,
            "vix",
            "INDIA_VIX",
            "India VIX",
            InstrumentKind.INDEX,
        )
        state = OptionChainState(
            master=InstrumentMaster(
                options=(),
                spot_tokens={"NIFTY": spot_token},
                futures=(
                    FutureContract(
                        "NIFTY",
                        date(2026, 7, 30),
                        future_token,
                    ),
                ),
                reference_tokens={"INDIA_VIX": vix_token},
            )
        )
        state.set_previous_20d_atr("NIFTY", Decimal("218.45"))
        state.set_reference_value("INDIA_VIX", Decimal("13.25"))
        state.update_tick(
            MarketTick(
                token=spot_token,
                exchange_timestamp=at,
                received_at=at,
                ltp=Decimal("24000"),
                open_price=Decimal("23950"),
                close_price=Decimal("23900"),
            )
        )
        state.update_tick(
            MarketTick(
                token=future_token,
                exchange_timestamp=at,
                received_at=at,
                ltp=Decimal("24020"),
                volume=120000,
                oi=800000,
            )
        )

        market = state.build_underlying_market_snapshot(
            underlying="NIFTY",
            captured_at=at,
        )

        self.assertIsNotNone(market)
        self.assertEqual(market.future_price, Decimal("24020"))
        self.assertEqual(market.future_volume, 120000)
        self.assertEqual(market.future_oi, 800000)
        self.assertEqual(market.basis, Decimal("20"))
        self.assertEqual(market.previous_20d_atr, Decimal("218.45"))
        self.assertEqual(market.india_vix, Decimal("13.25"))

        state.update_tick(
            MarketTick(
                token=vix_token,
                exchange_timestamp=at,
                received_at=at,
                ltp=Decimal("13.40"),
            )
        )
        updated = state.build_underlying_market_snapshot(
            underlying="NIFTY",
            captured_at=at,
        )
        self.assertEqual(updated.india_vix, Decimal("13.40"))

    def test_build_snapshot_uses_latest_ticks_for_selected_window(self) -> None:
        contract = _contract(Decimal("24150"), OptionType.CALL)
        state = OptionChainState(
            master=InstrumentMaster(
                options=(contract,),
                spot_tokens={},
            )
        )
        state.update_tick(
            MarketTick(
                token=contract.token,
                exchange_timestamp=datetime(2026, 7, 5, 9, 15, tzinfo=UTC),
                received_at=datetime(2026, 7, 5, 9, 15, tzinfo=UTC),
                ltp=Decimal("121.45"),
                oi=150000,
                volume=3200,
            )
        )

        snapshot = state.build_snapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24149"),
            each_side=0,
        )

        self.assertEqual(snapshot.atm_strike, Decimal("24150"))
        self.assertEqual(len(snapshot.quotes), 1)
        self.assertEqual(snapshot.quotes[0].ltp, Decimal("121.45"))
        self.assertEqual(snapshot.quotes[0].oi, 150000)

    def test_late_rest_payload_cannot_overwrite_newer_websocket_tick(self) -> None:
        contract = _contract(Decimal("24150"), OptionType.CALL)
        state = OptionChainState(
            master=InstrumentMaster(options=(contract,), spot_tokens={})
        )
        newer_at = datetime(2026, 7, 5, 9, 15, 2, tzinfo=UTC)
        older_at = datetime(2026, 7, 5, 9, 15, 1, tzinfo=UTC)
        state.update_tick(
            MarketTick(
                token=contract.token,
                exchange_timestamp=newer_at,
                received_at=newer_at,
                ltp=Decimal("125"),
            )
        )
        state.update_tick(
            MarketTick(
                token=contract.token,
                exchange_timestamp=older_at,
                received_at=newer_at,
                ltp=Decimal("120"),
            )
        )

        self.assertEqual(
            state.latest_tick(contract.token.token).ltp,
            Decimal("125"),
        )


if __name__ == "__main__":
    unittest.main()
