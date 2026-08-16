from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.models import Exchange, InstrumentKind, InstrumentToken
from app.marketdata.normalizer import normalize_tick


class MarketDataNormalizerTests(unittest.TestCase):
    def test_normalize_option_tick_keeps_oi_change_and_price_fields(self) -> None:
        token = InstrumentToken(
            exchange=Exchange.NFO,
            token="12345",
            symbol="NIFTY",
            trading_symbol="NIFTY30JUL2624150CE",
            kind=InstrumentKind.OPTION,
        )

        tick = normalize_tick(
            token=token,
            payload={
                "ltp": "121.45",
                "open": "118.00",
                "high": "125.30",
                "low": "116.75",
                "close": "119.90",
                "oi": "150000",
                "oiChange": "12000",
                "oiChangePercent": "8.69",
                "volume": "3200",
                "best_bid": "121.40",
                "best_ask": "121.55",
            },
            received_at=datetime(2026, 7, 4, 9, 30, tzinfo=UTC),
        )

        self.assertEqual(tick.ltp, Decimal("121.45"))
        self.assertEqual(tick.open_price, Decimal("118.00"))
        self.assertEqual(tick.high_price, Decimal("125.30"))
        self.assertEqual(tick.low_price, Decimal("116.75"))
        self.assertEqual(tick.close_price, Decimal("119.90"))
        self.assertEqual(tick.oi, 150000)
        self.assertEqual(tick.oi_change, 12000)
        self.assertEqual(tick.oi_change_percent, Decimal("8.69"))
        self.assertEqual(tick.volume, 3200)
        self.assertEqual(tick.bid, Decimal("121.40"))
        self.assertEqual(tick.ask, Decimal("121.55"))

    def test_normalize_angleone_payload_maps_tradevolume_and_opninterest(self) -> None:
        token = InstrumentToken(
            exchange=Exchange.NSE,
            token="3045",
            symbol="SBIN",
            trading_symbol="SBIN-EQ",
            kind=InstrumentKind.INDEX,
        )

        tick = normalize_tick(
            token=token,
            payload={
                "ltp": "568.2",
                "tradeVolume": "3556150",
                "opnInterest": "0",
            },
            received_at=datetime(2026, 7, 10, 10, 46, tzinfo=UTC),
        )

        self.assertEqual(tick.ltp, Decimal("568.2"))
        self.assertEqual(tick.volume, 3556150)
        self.assertEqual(tick.oi, 0)

    def test_normalize_angleone_full_quote_maps_depth_and_exchange_time(self) -> None:
        token = InstrumentToken(
            exchange=Exchange.NSE,
            token="3045",
            symbol="SBIN",
            trading_symbol="SBIN-EQ",
            kind=InstrumentKind.INDEX,
        )

        tick = normalize_tick(
            token=token,
            payload={
                "symbolToken": "3045",
                "ltp": 568.2,
                "open": 567.4,
                "high": 569.35,
                "low": 566.1,
                "close": 567.4,
                "exchFeedTime": "21-Jun-2023 10:46:10",
                "tradeVolume": 3556150,
                "opnInterest": 0,
                "depth": {
                    "buy": [{"price": 568.2, "quantity": 511, "orders": 2}],
                    "sell": [{"price": 568.25, "quantity": 3348, "orders": 5}],
                },
            },
            received_at=datetime(2026, 7, 10, 10, 46, tzinfo=UTC),
        )

        self.assertEqual(tick.exchange_timestamp.year, 2023)
        self.assertEqual(tick.ltp, Decimal("568.2"))
        self.assertEqual(tick.bid, Decimal("568.2"))
        self.assertEqual(tick.ask, Decimal("568.25"))

    def test_normalize_websocket_snapquote_converts_paise_prices(self) -> None:
        token = InstrumentToken(
            exchange=Exchange.NFO,
            token="58662",
            symbol="NIFTY",
            trading_symbol="NIFTY30JUL2624150CE",
            kind=InstrumentKind.OPTION,
        )

        tick = normalize_tick(
            token=token,
            payload={
                "token": "58662",
                "exchange_timestamp": 1783668600000,
                "last_traded_price": 12145,
                "open_price": 11800,
                "high_price": 12530,
                "low_price": 11675,
                "closed_price": 11990,
                "volume_trade_for_the_day": 3200,
                "open_interest": 150000,
            },
            received_at=datetime(2026, 7, 10, 10, 46, tzinfo=UTC),
        )

        self.assertEqual(tick.ltp, Decimal("121.45"))
        self.assertEqual(tick.open_price, Decimal("118"))
        self.assertEqual(tick.close_price, Decimal("119.9"))
        self.assertEqual(tick.volume, 3200)
        self.assertEqual(tick.oi, 150000)

    def test_normalize_websocket_snapquote_maps_day_ohlc_keys(self) -> None:
        token = InstrumentToken(
            exchange=Exchange.NSE,
            token="99926000",
            symbol="NIFTY",
            trading_symbol="Nifty 50",
            kind=InstrumentKind.INDEX,
        )

        tick = normalize_tick(
            token=token,
            payload={
                "token": "99926000",
                "exchange_timestamp": 1785124158000,
                "last_traded_price": 2393150,
                "open_price_of_the_day": 2392840,
                "high_price_of_the_day": 2396500,
                "low_price_of_the_day": 2389000,
                "closed_price": 2376745,
            },
            received_at=datetime(2026, 7, 27, 3, 49, 18, tzinfo=UTC),
        )

        self.assertEqual(tick.open_price, Decimal("23928.4"))
        self.assertEqual(tick.high_price, Decimal("23965"))
        self.assertEqual(tick.low_price, Decimal("23890"))
        self.assertEqual(tick.close_price, Decimal("23767.45"))


if __name__ == "__main__":
    unittest.main()
