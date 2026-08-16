from __future__ import annotations

import unittest
from dataclasses import replace
from time import perf_counter, sleep
from unittest.mock import MagicMock

from app.broker.angleone.client import AngleOneClient, _normalize_market_quote_mode
from app.core.config import Settings


class AngleOneClientTests(unittest.TestCase):
    def test_credentials_property_requires_every_login_value(self) -> None:
        settings = _settings()

        self.assertTrue(settings.broker_credentials_configured)
        self.assertFalse(
            replace(settings, angleone_totp_secret="").broker_credentials_configured
        )

    def test_normalizes_market_quote_mode_to_rest_mode(self) -> None:
        self.assertEqual(_normalize_market_quote_mode("FULL"), "FULL")
        self.assertEqual(_normalize_market_quote_mode("OHLC"), "OHLC")
        self.assertEqual(_normalize_market_quote_mode("LTP"), "LTP")
        self.assertEqual(_normalize_market_quote_mode("SNAP_QUOTE"), "FULL")
        self.assertEqual(_normalize_market_quote_mode("3"), "FULL")
        self.assertEqual(_normalize_market_quote_mode(1), "LTP")

    def test_market_quote_uses_rest_mode_string(self) -> None:
        settings = _settings()
        client = AngleOneClient(settings)
        smart_api = MagicMock()
        smart_api.getMarketData.return_value = {"data": []}
        client._smart_api = smart_api

        import asyncio

        asyncio.run(client.market_quote(mode="FULL", exchange_tokens={"NFO": ["123"]}))

        smart_api.getMarketData.assert_called_once_with("FULL", {"NFO": ["123"]})

    def test_historical_candles_uses_smartapi_candle_endpoint(self) -> None:
        settings = _settings()
        client = AngleOneClient(settings)
        smart_api = MagicMock()
        smart_api.getCandleData.return_value = {"data": []}
        client._smart_api = smart_api
        params = {
            "exchange": "NSE",
            "symboltoken": "99926000",
            "interval": "ONE_DAY",
        }

        import asyncio

        response = asyncio.run(client.historical_candles(params))

        self.assertEqual(response, {"data": []})
        smart_api.getCandleData.assert_called_once_with(params)

    def test_blocking_sdk_call_does_not_block_asyncio_loop(self) -> None:
        settings = _settings()
        client = AngleOneClient(settings)
        smart_api = MagicMock()

        def slow_quote(*_args):
            sleep(0.25)
            return {"data": []}

        smart_api.getMarketData.side_effect = slow_quote
        client._smart_api = smart_api

        async def exercise() -> None:
            started = perf_counter()
            task = asyncio.create_task(
                client.market_quote(
                    mode="FULL",
                    exchange_tokens={"NFO": ["123"]},
                )
            )
            await asyncio.sleep(0.02)
            self.assertLess(perf_counter() - started, 0.15)
            self.assertFalse(task.done())
            await task

        import asyncio

        asyncio.run(exercise())


def _settings() -> Settings:
    return Settings(
            app_name="test",
            app_env="test",
            log_level="INFO",
            angleone_api_key="key",
            angleone_client_code="code",
            angleone_password="pass",
            angleone_totp_secret="totp",
            angleone_instrument_master_url="",
            angleone_instrument_master_path="",
            redis_url="",
            database_url="",
            local_storage_dir="data",
            default_underlyings=("NIFTY",),
            option_window_each_side=4,
            snapshot_interval_ms=1000,
            storage_backend="jsonl",
            broker_name="angleone",
            market_data_price_source="websocket_snap_quote",
            market_data_oi_source="websocket_snap_quote",
            market_data_greeks_source="option_greek",
            market_data_ws_mode="SNAP_QUOTE",
            option_greeks_enabled=False,
            broker_pcr_enabled=True,
            broker_oi_buildup_enabled=False,
        )


if __name__ == "__main__":
    unittest.main()
