from __future__ import annotations

from decimal import Decimal

from app.broker.interfaces import BrokerSession

from .serde import contract_matches_underlying


class RecordedBrokerClient:
    """Offline broker adapter backed by the currently selected recorded frame."""

    def __init__(
        self,
        contracts: tuple[dict[str, object], ...],
        *,
        spot_tokens: tuple[dict[str, object], ...] = (),
        future_contracts: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._contracts = contracts
        self._spot_tokens = spot_tokens
        self._future_contracts = future_contracts
        self._snapshot: dict[str, object] | None = None

    def set_frame(self, snapshot: dict[str, object]) -> None:
        self._snapshot = snapshot

    async def login(self) -> BrokerSession:
        return BrokerSession(
            access_token="offline-replay",
            refresh_token=None,
            feed_token="offline-replay",
            raw={"mode": "offline-replay"},
        )

    async def instrument_master(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        underlyings: set[str] = set()
        for contract in self._contracts:
            underlying = str(contract["underlying"])
            underlyings.add(underlying)
            if not contract_matches_underlying(contract):
                continue
            token = _object(contract.get("token"))
            rows.append(
                {
                    "token": str(token["token"]),
                    "symbol": str(token["trading_symbol"]),
                    "name": underlying,
                    "expiry": str(contract["expiry"]),
                    "strike": str(contract["strike"]),
                    "lotsize": contract.get("lot_size"),
                    "instrumenttype": "OPTIDX",
                    "optiontype": str(contract["option_type"]),
                    "exch_seg": str(token["exchange"]),
                }
            )
        for contract in self._future_contracts:
            underlying = str(contract["underlying"])
            underlyings.add(underlying)
            token = _object(contract.get("token"))
            rows.append(
                {
                    "token": str(token["token"]),
                    "symbol": str(token["trading_symbol"]),
                    "name": underlying,
                    "expiry": str(contract["expiry"]),
                    "lotsize": contract.get("lot_size"),
                    "instrumenttype": "FUTIDX",
                    "exch_seg": str(token["exchange"]),
                }
            )
        recorded_spot_underlyings: set[str] = set()
        for token in self._spot_tokens:
            underlying = str(token.get("symbol") or "")
            if not underlying:
                continue
            underlyings.add(underlying)
            recorded_spot_underlyings.add(underlying)
            rows.append(
                {
                    "token": str(token["token"]),
                    "symbol": str(token["trading_symbol"]),
                    "name": underlying,
                    "instrumenttype": "INDEX",
                    "exch_seg": str(token["exchange"]),
                }
            )
        for underlying in sorted(underlyings):
            if underlying in recorded_spot_underlyings:
                continue
            rows.append(
                {
                    "token": f"DUMMY_SPOT_{underlying}",
                    "symbol": underlying,
                    "name": underlying,
                    "instrumenttype": "INDEX",
                    "exch_seg": "NSE",
                }
            )
        return rows

    async def market_quote(
        self,
        *,
        mode: str,
        exchange_tokens: dict[str, list[str]],
    ) -> dict[str, object]:
        snapshot = self._require_frame()
        wanted = {
            str(token)
            for tokens in exchange_tokens.values()
            for token in tokens
        }
        fetched: list[dict[str, object]] = []
        for quote in _objects(snapshot.get("quotes")):
            contract = _object(quote.get("contract"))
            token = _object(contract.get("token"))
            token_id = str(token["token"])
            if token_id not in wanted:
                continue
            fetched.append(
                {
                    "token": token_id,
                    "tradingSymbol": token["trading_symbol"],
                    "ltp": quote.get("ltp"),
                    "open": quote.get("open_price"),
                    "high": quote.get("high_price"),
                    "low": quote.get("low_price"),
                    "close": quote.get("close_price"),
                    "oi": quote.get("oi"),
                    "oi_change": quote.get("oi_change"),
                    "oi_change_percent": quote.get("oi_change_percent"),
                    "volume": quote.get("volume"),
                    "bid": quote.get("bid"),
                    "ask": quote.get("ask"),
                }
            )
        return {"status": True, "data": {"fetched": fetched}}

    async def option_greeks(self, params: dict[str, object]) -> dict[str, object]:
        snapshot = self._require_frame()
        rows: list[dict[str, object]] = []
        for quote in _objects(snapshot.get("quotes")):
            greeks = quote.get("greeks")
            if not isinstance(greeks, dict):
                continue
            contract = _object(quote.get("contract"))
            token = _object(contract.get("token"))
            rows.append(
                {
                    "tradingSymbol": token["trading_symbol"],
                    "strikePrice": contract["strike"],
                    "optionType": contract["option_type"],
                    "impliedVolatility": greeks.get("implied_volatility"),
                    "delta": greeks.get("delta"),
                    "gamma": greeks.get("gamma"),
                    "theta": greeks.get("theta"),
                    "vega": greeks.get("vega"),
                }
            )
        return {"status": True, "data": rows}

    async def ltp_data(
        self,
        *,
        exchange: str,
        trading_symbol: str,
        symbol_token: str,
    ) -> dict[str, object]:
        snapshot = self._require_frame()
        if symbol_token.startswith("DUMMY_SPOT_"):
            return {"status": True, "data": {"ltp": snapshot["spot_price"]}}
        response = await self.market_quote(
            mode="LTP",
            exchange_tokens={exchange: [symbol_token]},
        )
        fetched = _object(response["data"]).get("fetched")
        row = fetched[0] if isinstance(fetched, list) and fetched else {}
        return {"status": True, "data": row}

    async def historical_oi(self, params: dict[str, object]) -> dict[str, object]:
        return {"status": True, "data": []}

    async def historical_candles(
        self,
        params: dict[str, object],
    ) -> dict[str, object]:
        return {"status": True, "data": []}

    async def put_call_ratio(self) -> dict[str, object]:
        return {"status": True, "data": []}

    async def oi_buildup(self, params: dict[str, object]) -> dict[str, object]:
        return {"status": True, "data": []}

    def _require_frame(self) -> dict[str, object]:
        if self._snapshot is None:
            raise RuntimeError("No replay frame has been selected")
        return self._snapshot


def quote_rows(response: dict[str, object]) -> list[dict[str, object]]:
    data = response.get("data")
    if not isinstance(data, dict):
        return []
    return _objects(data.get("fetched"))


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected object, got {type(value).__name__}")
    return value


def _objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
