from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.domain.models import (
    Exchange,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionContract,
    OptionType,
)
from app.instruments.master import InstrumentMaster
from app.optionchain.state import OptionChainState
from app.workers.market_data_worker import (
    _initialize_reference_data,
    _normalize_market_quote_payloads,
    _profile_capture_directory,
    _rotate_option_subscriptions,
    _single_enabled_strategy_name,
)


class MarketDataWorkerTests(unittest.TestCase):
    def test_routes_capture_files_to_profile_directory(self) -> None:
        self.assertEqual(
            _profile_capture_directory(Path("data"), "gamma_blast"),
            Path("data") / "gamma_blast",
        )

    def test_routes_single_strategy_capture_to_its_own_directory(self) -> None:
        self.assertEqual(
            _profile_capture_directory(
                Path("data"),
                "derivatives_only",
                strategy_name="GAMMA_EXPANSION",
            ),
            Path("data") / "derivatives_only" / "GAMMA_EXPANSION",
        )

    def test_capture_identity_requires_exactly_one_enabled_strategy(self) -> None:
        class Toggle:
            def __init__(self, enabled: bool) -> None:
                self.enabled = enabled

        self.assertEqual(
            _single_enabled_strategy_name(
                {
                    "DERIVATIVES_QUANT": Toggle(False),
                    "GAMMA_EXPANSION": Toggle(True),
                }
            ),
            "GAMMA_EXPANSION",
        )
        self.assertIsNone(
            _single_enabled_strategy_name(
                {
                    "DERIVATIVES_QUANT": Toggle(True),
                    "GAMMA_EXPANSION": Toggle(True),
                }
            )
        )

    def test_rejects_unsafe_capture_profile_directory(self) -> None:
        for profile_name in ("", ".", "..", "nested/profile"):
            with self.subTest(profile_name=profile_name):
                with self.assertRaises(ValueError):
                    _profile_capture_directory(Path("data"), profile_name)
        for strategy_name in ("", ".", "..", "nested/strategy"):
            with self.subTest(strategy_name=strategy_name):
                with self.assertRaises(ValueError):
                    _profile_capture_directory(
                        Path("data"),
                        "profile",
                        strategy_name=strategy_name,
                    )

    def test_normalize_market_quote_payloads_flattens_nested_entries(self) -> None:
        response = {
            "data": [
                {"token": "1001", "ltp": 12.0},
                {"nested": {"symbolToken": "1002", "ltp": 13.0}},
            ]
        }

        self.assertEqual(
            _normalize_market_quote_payloads(response),
            [
                ("1001", {"token": "1001", "ltp": 12.0}),
                ("1002", {"symbolToken": "1002", "ltp": 13.0}),
            ],
        )

    def test_normalize_market_quote_payloads_handles_angleone_fetched_payload(self) -> None:
        response = {
            "status": True,
            "message": "SUCCESS",
            "errorcode": "",
            "data": {
                "fetched": [
                    {
                        "exchange": "NSE",
                        "tradingSymbol": "SBIN-EQ",
                        "symbolToken": "3045",
                        "ltp": 568.2,
                        "tradeVolume": 3556150,
                        "opnInterest": 0,
                    }
                ],
                "unfetched": [],
            },
        }

        self.assertEqual(
            _normalize_market_quote_payloads(response),
            [
                (
                    "3045",
                    {
                        "exchange": "NSE",
                        "tradingSymbol": "SBIN-EQ",
                        "symbolToken": "3045",
                        "ltp": 568.2,
                        "tradeVolume": 3556150,
                        "opnInterest": 0,
                    },
                )
            ],
        )

    def test_initializes_vix_and_previous_atr_without_blocking_worker(self) -> None:
        market_date = date(2026, 7, 29)
        spot_token = InstrumentToken(
            Exchange.NSE,
            "spot",
            "NIFTY",
            "NIFTY",
            InstrumentKind.INDEX,
        )
        vix_token = InstrumentToken(
            Exchange.NSE,
            "vix",
            "INDIA_VIX",
            "India VIX",
            InstrumentKind.INDEX,
        )
        master = InstrumentMaster(
            options=(),
            spot_tokens={"NIFTY": spot_token},
            reference_tokens={"INDIA_VIX": vix_token},
        )
        state = OptionChainState(master=master)
        client = _ReferenceClient(market_date)

        status = asyncio.run(
            _initialize_reference_data(
                client=client,
                master=master,
                state=state,
                market_date=market_date,
            )
        )
        at = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
        state.update_tick(
            MarketTick(
                token=spot_token,
                exchange_timestamp=at,
                received_at=at,
                ltp=Decimal("24500"),
            )
        )
        market = state.build_underlying_market_snapshot(
            underlying="NIFTY",
            captured_at=at,
        )

        self.assertEqual(status["india_vix"]["status"], "READY")
        self.assertEqual(
            status["previous_20d_atr"]["NIFTY"]["status"],
            "READY",
        )
        self.assertEqual(market.india_vix, Decimal("13.45"))
        self.assertEqual(market.previous_20d_atr, Decimal("10.00"))

    def test_rotates_atm_subscriptions_without_accumulating_old_tokens(self) -> None:
        expiry = date(2026, 7, 30)
        old_token = InstrumentToken(
            Exchange.NFO,
            "old",
            "NIFTY",
            "NIFTY30JUL2624000CE",
            InstrumentKind.OPTION,
        )
        new_token = InstrumentToken(
            Exchange.NFO,
            "new",
            "NIFTY",
            "NIFTY30JUL2624050CE",
            InstrumentKind.OPTION,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=expiry,
            strike=Decimal("24050"),
            option_type=OptionType.CALL,
            token=new_token,
            lot_size=75,
        )
        feed = _SubscriptionFeed()
        active = {"NIFTY": {"old"}}

        asyncio.run(
            _rotate_option_subscriptions(
                feed=feed,
                recorder=None,
                token_lookup={"old": old_token, "new": new_token},
                active_tokens=active,
                underlying="NIFTY",
                spot_price=Decimal("24040"),
                atm_strike=Decimal("24050"),
                contracts=(contract,),
            )
        )

        self.assertEqual(feed.subscribed, [("new",)])
        self.assertEqual(feed.unsubscribed, [("old",)])
        self.assertEqual(active["NIFTY"], {"new"})

    def test_keeps_open_paper_contract_subscribed_during_atm_rotation(self) -> None:
        expiry = date(2026, 7, 30)
        old_token = InstrumentToken(
            Exchange.NFO,
            "old",
            "NIFTY",
            "NIFTY30JUL2624000CE",
            InstrumentKind.OPTION,
        )
        new_token = InstrumentToken(
            Exchange.NFO,
            "new",
            "NIFTY",
            "NIFTY30JUL2624050CE",
            InstrumentKind.OPTION,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=expiry,
            strike=Decimal("24050"),
            option_type=OptionType.CALL,
            token=new_token,
            lot_size=75,
        )
        feed = _SubscriptionFeed()
        active = {"NIFTY": {"old"}}

        asyncio.run(
            _rotate_option_subscriptions(
                feed=feed,
                recorder=None,
                token_lookup={"old": old_token, "new": new_token},
                active_tokens=active,
                underlying="NIFTY",
                spot_price=Decimal("24040"),
                atm_strike=Decimal("24050"),
                contracts=(contract,),
                protected_tokens={"old"},
            )
        )

        self.assertEqual(feed.subscribed, [("new",)])
        self.assertEqual(feed.unsubscribed, [])
        self.assertEqual(active["NIFTY"], {"old", "new"})

class _ReferenceClient:
    def __init__(self, market_date: date) -> None:
        self._market_date = market_date

    async def ltp_data(self, **_kwargs):
        return {"data": {"ltp": "13.45"}}

    async def historical_candles(self, _params):
        start = self._market_date - timedelta(days=21)
        return {
            "data": [
                [
                    f"{(start + timedelta(days=index)).isoformat()}"
                    "T09:15:00+05:30",
                    "100",
                    "106",
                    "96",
                    "101",
                    1000,
                ]
                for index in range(21)
            ]
        }


class _SubscriptionFeed:
    def __init__(self) -> None:
        self.subscribed: list[tuple[str, ...]] = []
        self.unsubscribed: list[tuple[str, ...]] = []

    async def subscribe(self, tokens) -> None:
        self.subscribed.append(tuple(token.token for token in tokens))

    async def unsubscribe(self, tokens) -> None:
        self.unsubscribed.append(tuple(token.token for token in tokens))

    async def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
