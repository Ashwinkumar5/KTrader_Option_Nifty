from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.request import urlopen

from app.broker.interfaces import BrokerSession
from app.core.config import Settings


_T = TypeVar("_T")


class AngleOneClient:
    """Thin SmartAPI wrapper.

    Keep SmartAPI-specific payloads and SDK calls here. Downstream modules should
    receive domain models from normalizers/resolvers instead of raw broker data.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._smart_api: Any | None = None
        # SmartConnect is synchronous and does not document concurrent use.
        # Serialize SDK operations on a worker thread so broker latency never
        # blocks the asyncio market-data loop or races the shared session.
        self._sdk_lock = asyncio.Lock()
        self._sdk_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="angleone-rest",
        )

    async def login(self) -> BrokerSession:
        if not self._settings.broker_credentials_configured:
            raise RuntimeError("Angle One credentials are not configured.")

        try:
            import pyotp
            from SmartApi import SmartConnect
        except ImportError as exc:
            raise RuntimeError(
                "SmartAPI login requires smartapi-python and pyotp from requirements.txt."
            ) from exc

        runtime_timeout = max(
            0.1,
            self._settings.angleone_http_timeout_seconds,
        )
        smart_api = SmartConnect(
            api_key=self._settings.angleone_api_key,
            timeout=max(5.0, runtime_timeout),
        )
        totp = pyotp.TOTP(self._settings.angleone_totp_secret).now()
        response = await self._call_sdk(
            smart_api.generateSession,
            self._settings.angleone_client_code,
            self._settings.angleone_password,
            totp,
        )

        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict) or not data.get("jwtToken"):
            raise RuntimeError(f"Angle One login failed: {response}")

        feed_token = await self._call_sdk(smart_api.getfeedToken)
        smart_api.timeout = runtime_timeout
        self._smart_api = smart_api
        
        return BrokerSession(
            access_token=str(data["jwtToken"]),
            refresh_token=str(data["refreshToken"]) if data.get("refreshToken") else None,
            feed_token=str(feed_token),
            raw=response,
        )

    async def instrument_master(self) -> list[dict[str, object]]:
        data = await asyncio.to_thread(
            _load_instrument_master,
            self._settings.angleone_instrument_master_path,
            self._settings.angleone_instrument_master_url,
        )

        if not isinstance(data, list):
            raise RuntimeError("Angle One instrument master did not return a row list.")
        return [row for row in data if isinstance(row, dict)]

    async def market_quote(
        self,
        *,
        mode: str,
        exchange_tokens: dict[str, list[str]],
    ) -> dict[str, object]:
        smart_api = self._require_smart_api()
        
        try:            
            response = await self._call_sdk(
                smart_api.getMarketData,
                _normalize_market_quote_mode(mode),
                exchange_tokens,
            )
        except Exception as exc:
            message = str(exc).lower()
            if "couldn't parse the json response" in message or "b''" in message or "empty" in message:
                return {}
            raise

        if response in (None, "", b""):
            return {}
        return response

    async def ltp_data(
        self,
        *,
        exchange: str,
        trading_symbol: str,
        symbol_token: str,
    ) -> dict[str, object]:
        smart_api = self._require_smart_api()
        return await self._call_sdk(
            smart_api.ltpData,
            exchange,
            trading_symbol,
            symbol_token,
        )

    async def historical_oi(self, params: dict[str, object]) -> dict[str, object]:
        smart_api = self._require_smart_api()
        return await self._call_sdk(smart_api.getOIData, params)

    async def historical_candles(
        self,
        params: dict[str, object],
    ) -> dict[str, object]:
        smart_api = self._require_smart_api()
        response = await self._call_sdk(smart_api.getCandleData, params)
        return response if isinstance(response, dict) else {}

    async def option_greeks(self, params: dict[str, object]) -> dict[str, object]:
        smart_api = self._require_smart_api()
        return await self._call_sdk(smart_api.optionGreek, params)

    async def put_call_ratio(self) -> dict[str, object]:
        smart_api = self._require_smart_api()
        return await self._call_sdk(smart_api.putCallRatio)

    async def oi_buildup(self, params: dict[str, object]) -> dict[str, object]:
        smart_api = self._require_smart_api()
        return await self._call_sdk(smart_api.oIBuildup, params)

    async def _call_sdk(
        self,
        operation: Callable[..., _T],
        *args: object,
    ) -> _T:
        async with self._sdk_lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                self._sdk_executor,
                partial(operation, *args),
            )

    async def close(self) -> None:
        await asyncio.to_thread(
            self._sdk_executor.shutdown,
            wait=True,
            cancel_futures=True,
        )

    def _require_smart_api(self):
        if self._smart_api is None:
            raise RuntimeError("SmartAPI client is not initialized. Call login after credentials are configured.")
        return self._smart_api


def _normalize_market_quote_mode(mode: str | int | None) -> str:
    if isinstance(mode, int):
        return {1: "LTP", 2: "OHLC", 3: "FULL"}.get(mode, "FULL")
    if isinstance(mode, str):
        normalized = mode.strip().upper()
        if normalized in {"LTP", "OHLC", "FULL"}:
            return normalized
        if normalized in {"QUOTE", "SNAP_QUOTE"}:
            return "FULL"
        if normalized in {"1", "2", "3"}:
            return {"1": "LTP", "2": "OHLC", "3": "FULL"}[normalized]
    return "FULL"


def _load_instrument_master(path_value: str, url: str) -> object:
    if path_value:
        path = Path(path_value)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))
