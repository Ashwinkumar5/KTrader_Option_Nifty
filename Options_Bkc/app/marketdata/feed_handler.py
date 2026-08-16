from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from inspect import isawaitable
from types import MappingProxyType
from typing import Mapping, Protocol

from app.broker.interfaces import (
    BrokerClient,
    MarketDataFeed,
)
from app.broker.registry import (
    build_configured_instrument_master,
    create_broker_client,
    create_market_data_feed,
)
from app.core.config import Settings
from app.domain.models import (
    GreeksSnapshot,
    InstrumentToken,
    MarketTick,
    OptionContract,
)
from app.greeks.broker import normalize_broker_greeks, option_greek_params
from app.instruments.master import InstrumentMaster
from app.marketdata.normalizer import normalize_tick
from app.marketdata.reference_data import (
    calculate_previous_atr,
    extract_ltp,
    normalize_daily_candles,
)
from app.optionchain.state import OptionChainState


@dataclass(frozen=True)
class FeedHandlerRuntime:
    """Immutable broker metadata prepared once for a worker process."""

    master: InstrumentMaster
    token_lookup: Mapping[str, InstrumentToken]


class MarketDataFeedHandler(Protocol):
    """Broker-facing boundary consumed by strategy workers."""

    async def prepare(self) -> FeedHandlerRuntime:
        """Login, load instruments, and construct the feed without opening it."""

    async def start(self, *, market_date: date) -> tuple[InstrumentToken, ...]:
        """Connect and subscribe the initial reference instruments."""

    async def initialize_reference_data(
        self,
        *,
        state: OptionChainState,
        market_date: date,
    ) -> dict[str, object]:
        """Load optional VIX and previous-session reference values."""

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        """Subscribe through the owned broker feed."""

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        """Unsubscribe through the owned broker feed."""

    def ticks(self) -> AsyncIterator[MarketTick]:
        """Yield normalized ticks from the owned broker feed."""

    async def refresh_option_quotes(
        self,
        *,
        state: OptionChainState,
        contracts: tuple[OptionContract, ...],
    ) -> dict[str, object]:
        """Refresh the selected option window through broker REST data."""

    async def refresh_option_greeks(
        self,
        *,
        underlying: str,
        expiry: date,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[dict[str, GreeksSnapshot], dict[str, object]]:
        """Refresh and normalize Greeks for the selected option window."""

    def health_snapshot(self) -> dict[str, object]:
        """Return bounded feed health without performing network I/O."""

    async def close(self) -> None:
        """Release the owned broker client resources."""


class EmbeddedMarketDataFeedHandler:
    """Current in-process broker owner behind the shared feed-handler seam.

    This class deliberately preserves today's runtime topology: each worker that
    constructs one still owns one broker session and WebSocket. It is a
    preparatory seam; the shared-process phase will add an immutable transport
    and frame contract without putting broker calls back into strategy code.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        client: BrokerClient | None = None,
        feed: MarketDataFeed | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or create_broker_client(settings)
        self._feed = feed
        self._runtime: FeedHandlerRuntime | None = None
        self._started = False
        self._closed = False

    async def prepare(self) -> FeedHandlerRuntime:
        self._ensure_open()
        if self._runtime is not None:
            return self._runtime

        session = await self._client.login()
        raw_master = list(await self._client.instrument_master())
        master = build_configured_instrument_master(
            settings=self._settings,
            rows=raw_master,
        )
        token_lookup = build_token_lookup(master)
        if self._feed is None:
            self._feed = create_market_data_feed(
                settings=self._settings,
                session=session,
                token_lookup=token_lookup,
            )
        self._runtime = FeedHandlerRuntime(
            master=master,
            token_lookup=MappingProxyType(token_lookup),
        )
        return self._runtime

    async def start(self, *, market_date: date) -> tuple[InstrumentToken, ...]:
        self._ensure_open()
        runtime = self._require_runtime()
        if self._started:
            return initial_reference_tokens(
                runtime.master,
                self._settings.default_underlyings,
                market_date,
            )
        feed = self._require_feed()
        await feed.connect()
        tokens = initial_reference_tokens(
            runtime.master,
            self._settings.default_underlyings,
            market_date,
        )
        await feed.subscribe(tokens)
        self._started = True
        return tokens

    async def initialize_reference_data(
        self,
        *,
        state: OptionChainState,
        market_date: date,
    ) -> dict[str, object]:
        self._ensure_open()
        runtime = self._require_runtime()
        return await initialize_reference_data(
            client=self._client,
            master=runtime.master,
            state=state,
            market_date=market_date,
        )

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        self._ensure_open()
        await self._require_feed().subscribe(tokens)

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        self._ensure_open()
        await self._require_feed().unsubscribe(tokens)

    def ticks(self) -> AsyncIterator[MarketTick]:
        self._ensure_open()
        return self._require_feed().ticks()

    async def refresh_option_quotes(
        self,
        *,
        state: OptionChainState,
        contracts: tuple[OptionContract, ...],
    ) -> dict[str, object]:
        self._ensure_open()
        runtime = self._require_runtime()
        return await refresh_option_quotes(
            client=self._client,
            token_lookup=runtime.token_lookup,
            state=state,
            contracts=contracts,
        )

    async def refresh_option_greeks(
        self,
        *,
        underlying: str,
        expiry: date,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[dict[str, GreeksSnapshot], dict[str, object]]:
        self._ensure_open()
        payload, refresh = await fetch_greeks(
            client=self._client,
            underlying=underlying,
            expiry=expiry,
        )
        if payload is None:
            return {}, refresh

        captured_at = refresh.get("responded_at")
        normalized = normalize_broker_greeks(
            payload,
            contracts=contracts,
            captured_at=(
                captured_at
                if isinstance(captured_at, datetime)
                else datetime.now(UTC)
            ),
        )
        refresh["normalized_tokens"] = tuple(normalized)
        refresh["normalized_token_count"] = len(normalized)
        return normalized, refresh

    def health_snapshot(self) -> dict[str, object]:
        self._ensure_open()
        return feed_health_snapshot(self._require_feed())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_feed = getattr(self._feed, "close", None)
        try:
            if callable(close_feed):
                result = close_feed()
                if isawaitable(result):
                    await result
        finally:
            close_client = getattr(self._client, "close", None)
            if callable(close_client):
                result = close_client()
                if isawaitable(result):
                    await result

    def _require_runtime(self) -> FeedHandlerRuntime:
        if self._runtime is None:
            raise RuntimeError("Call prepare before using the feed handler.")
        return self._runtime

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Market-data feed handler is closed.")

    def _require_feed(self) -> MarketDataFeed:
        if self._feed is None:
            raise RuntimeError("Call prepare before using the market-data feed.")
        return self._feed


def build_token_lookup(master: InstrumentMaster) -> dict[str, InstrumentToken]:
    tokens = {token.token: token for token in master.spot_tokens.values()}
    tokens.update(
        {token.token: token for token in master.reference_tokens.values()}
    )
    tokens.update(
        {contract.token.token: contract.token for contract in master.futures}
    )
    tokens.update(
        {contract.token.token: contract.token for contract in master.options}
    )
    return tokens


def initial_reference_tokens(
    master: InstrumentMaster,
    underlyings: tuple[str, ...],
    market_date: date,
) -> tuple[InstrumentToken, ...]:
    return (
        tuple(master.spot_tokens.values())
        + tuple(
            contract.token
            for underlying in underlyings
            for contract in (
                master.nearest_future(
                    underlying=underlying,
                    as_of=market_date,
                ),
            )
            if contract is not None
        )
        + tuple(master.reference_tokens.values())
    )


async def initialize_reference_data(
    *,
    client: BrokerClient,
    master: InstrumentMaster,
    state: OptionChainState,
    market_date: date,
) -> dict[str, object]:
    status: dict[str, object] = {
        "india_vix": {"status": "UNAVAILABLE"},
        "previous_20d_atr": {},
    }
    india_vix_token = master.reference_tokens.get("INDIA_VIX")
    if india_vix_token is not None:
        try:
            response = await client.ltp_data(
                exchange=india_vix_token.exchange.value,
                trading_symbol=india_vix_token.trading_symbol,
                symbol_token=india_vix_token.token,
            )
            india_vix = extract_ltp(response)
            if india_vix is not None:
                state.set_reference_value("INDIA_VIX", india_vix)
                status["india_vix"] = {
                    "status": "READY",
                    "token": india_vix_token.token,
                    "value": india_vix,
                }
            else:
                status["india_vix"] = {
                    "status": "UNAVAILABLE",
                    "token": india_vix_token.token,
                    "reason": "ltp_missing",
                }
        except Exception as exc:
            status["india_vix"] = {
                "status": "UNAVAILABLE",
                "token": india_vix_token.token,
                "reason": type(exc).__name__,
            }
    else:
        status["india_vix"] = {
            "status": "UNAVAILABLE",
            "reason": "instrument_token_missing",
        }

    atr_status: dict[str, object] = {}
    historical_candles = getattr(client, "historical_candles", None)
    for underlying, token in master.spot_tokens.items():
        if not callable(historical_candles):
            atr_status[underlying] = {
                "status": "UNAVAILABLE",
                "reason": "broker_historical_candles_unsupported",
            }
            continue
        try:
            response = await historical_candles(
                {
                    "exchange": token.exchange.value,
                    "symboltoken": token.token,
                    "interval": "ONE_DAY",
                    "fromdate": (
                        market_date - timedelta(days=60)
                    ).strftime("%Y-%m-%d 09:15"),
                    "todate": (
                        market_date - timedelta(days=1)
                    ).strftime("%Y-%m-%d 15:30"),
                }
            )
            candles = normalize_daily_candles(
                response,
                before_date=market_date,
            )
            atr = calculate_previous_atr(candles)
            if atr is None:
                atr_status[underlying] = {
                    "status": "UNAVAILABLE",
                    "reason": "fewer_than_21_completed_daily_candles",
                    "completed_candles": len(candles),
                }
                continue
            state.set_previous_20d_atr(underlying, atr)
            atr_status[underlying] = {
                "status": "READY",
                "value": atr,
                "periods": 20,
                "last_session": candles[-1].session_date,
            }
        except Exception as exc:
            atr_status[underlying] = {
                "status": "UNAVAILABLE",
                "reason": type(exc).__name__,
            }
    status["previous_20d_atr"] = atr_status
    return status


def feed_health_snapshot(feed: MarketDataFeed) -> dict[str, object]:
    health_method = getattr(feed, "health_snapshot", None)
    if not callable(health_method):
        return {"status": "UNAVAILABLE", "reason": None}
    try:
        snapshot = health_method()
    except Exception as exc:
        return {
            "status": "FAILED",
            "reason": "feed_health_probe_failed",
            "error_type": type(exc).__name__,
        }
    if not isinstance(snapshot, dict):
        return {
            "status": "FAILED",
            "reason": "invalid_feed_health_snapshot",
        }
    return dict(snapshot)


async def refresh_option_quotes(
    *,
    client: BrokerClient,
    token_lookup: Mapping[str, InstrumentToken],
    state: OptionChainState,
    contracts: tuple[OptionContract, ...],
) -> dict[str, object]:
    exchange_tokens: dict[str, list[str]] = {}
    for contract in contracts:
        token = contract.token
        exchange_tokens.setdefault(token.exchange.value, []).append(token.token)

    if not exchange_tokens:
        return {
            "status": "skipped",
            "requested_at": None,
            "responded_at": None,
            "mode": "FULL",
            "exchange_tokens": {},
            "row_count": 0,
            "normalized_tokens": (),
            "error": "no option contracts selected",
        }

    requested_at = datetime.now(UTC)
    try:
        response = await client.market_quote(
            mode="FULL",
            exchange_tokens=exchange_tokens,
        )
    except Exception as exc:
        print(f"Option quote refresh skipped: {exc}")
        return {
            "status": "error",
            "requested_at": requested_at,
            "responded_at": datetime.now(UTC),
            "mode": "FULL",
            "exchange_tokens": exchange_tokens,
            "row_count": 0,
            "normalized_tokens": (),
            "error": str(exc),
        }

    responded_at = datetime.now(UTC)
    normalized_payloads = normalize_market_quote_payloads(response)
    updated_tokens: list[str] = []
    for token_id, payload in normalized_payloads:
        token = token_lookup.get(token_id)
        if token is None:
            continue
        state.update_tick(
            normalize_tick(
                token=token,
                payload=payload,
                received_at=responded_at,
            )
        )
        updated_tokens.append(token_id)
    return {
        "status": "ok",
        "requested_at": requested_at,
        "responded_at": responded_at,
        "mode": "FULL",
        "exchange_tokens": exchange_tokens,
        "row_count": len(normalized_payloads),
        "normalized_tokens": tuple(updated_tokens),
        "broker_status": (
            response.get("status")
            if isinstance(response, dict)
            else None
        ),
        "error": None,
    }


async def fetch_greeks(
    *,
    client: BrokerClient,
    underlying: str,
    expiry: date,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    attempts = 3
    delay = 1.0
    requested_at = datetime.now(UTC)
    for attempt in range(attempts):
        try:
            response = await client.option_greeks(
                option_greek_params(underlying=underlying, expiry=expiry)
            )
            if isinstance(response, dict) and not response.get("status", True):
                print(
                    "Option greeks not available: "
                    f"{response.get('message')}"
                )
                return None, {
                    "status": "unavailable",
                    "requested_at": requested_at,
                    "responded_at": datetime.now(UTC),
                    "attempts": attempt + 1,
                    "underlying": underlying,
                    "expiry": expiry,
                    "row_count": 0,
                    "error": str(
                        response.get("message") or "broker status false"
                    ),
                }
            rows = response.get("data") if isinstance(response, dict) else None
            return response, {
                "status": "ok",
                "requested_at": requested_at,
                "responded_at": datetime.now(UTC),
                "attempts": attempt + 1,
                "underlying": underlying,
                "expiry": expiry,
                "row_count": len(rows) if isinstance(rows, list) else 0,
                "error": None,
            }
        except Exception as exc:
            if attempt + 1 == attempts:
                print(
                    f"Option greeks failed after {attempts} attempts: {exc}"
                )
                return None, {
                    "status": "error",
                    "requested_at": requested_at,
                    "responded_at": datetime.now(UTC),
                    "attempts": attempt + 1,
                    "underlying": underlying,
                    "expiry": expiry,
                    "row_count": 0,
                    "error": str(exc),
                }
            await asyncio.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable Greeks retry state")


def normalize_market_quote_payloads(
    response: object,
) -> list[tuple[str, dict[str, object]]]:
    if not isinstance(response, dict):
        return []

    raw_entries: list[dict[str, object]] = []
    data = response.get("data")
    if isinstance(data, dict):
        raw_entries.extend(_collect_payload_entries(data))
    elif isinstance(data, list):
        raw_entries.extend(item for item in data if isinstance(item, dict))
    else:
        raw_entries.append(response)

    results: list[tuple[str, dict[str, object]]] = []
    for entry in raw_entries:
        token_id = _extract_token_id(entry)
        if token_id is not None:
            results.append((token_id, dict(entry)))
            continue

        for key, value in entry.items():
            if not isinstance(value, dict):
                continue
            nested_token_id = _extract_token_id(value)
            if nested_token_id is not None:
                results.append((nested_token_id, dict(value)))
            elif isinstance(key, str):
                results.append((key, dict(value)))

    return results


def _collect_payload_entries(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []

    entries: list[dict[str, object]] = []
    for key in ("fetched", "data", "quotes", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            entries.append(value)

    return entries or [payload]


def _extract_token_id(payload: dict[str, object]) -> str | None:
    for key in (
        "token",
        "symbolToken",
        "symbol_token",
        "securityToken",
        "instrument_token",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int)):
            return str(value)
    return None
