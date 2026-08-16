from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import orjson

from app.domain.models import (
    Exchange,
    FutureContract,
    GreeksSnapshot,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    TickQuality,
    UnderlyingMarketSnapshot,
    UnderlyingReference,
)
from app.marketdata.events import (
    MARKET_DATA_SCHEMA_VERSION,
    FeedHealthSnapshot,
    FeedStatusEvent,
    MarketDataBootstrap,
    MarketDataEvent,
    MaterializedOptionChainFrame,
    RawMarketTickEvent,
    RefreshProvenance,
)
from app.storage.serialization import to_jsonable


DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES = 4 * 1024 * 1024


def encode_market_data_event(event: MarketDataEvent) -> bytes:
    if isinstance(event, RawMarketTickEvent):
        event_type = "tick"
    elif isinstance(event, MaterializedOptionChainFrame):
        event_type = "frame"
    elif isinstance(event, FeedStatusEvent):
        event_type = "status"
    else:  # pragma: no cover - exhaustive type guard
        raise TypeError(f"Unsupported market-data event: {type(event).__name__}")
    payload = to_jsonable(event)
    payload["event_type"] = event_type
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def decode_market_data_event(
    payload: bytes,
    *,
    maximum_bytes: int = DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
) -> MarketDataEvent:
    raw = _decode_object(payload, maximum_bytes=maximum_bytes)
    _require_schema(raw)
    event_type = str(raw.get("event_type") or "")
    common = {
        "handler_epoch": _required_text(raw, "handler_epoch"),
        "event_id": _required_text(raw, "event_id"),
        "published_at": _aware_datetime(raw.get("published_at")),
        "schema_version": MARKET_DATA_SCHEMA_VERSION,
    }
    if event_type == "tick":
        return RawMarketTickEvent(
            tick=parse_market_tick(_object(raw.get("tick"), "tick")),
            **common,
        )
    if event_type == "frame":
        return MaterializedOptionChainFrame(
            snapshot=parse_option_chain_snapshot(
                _object(raw.get("snapshot"), "snapshot")
            ),
            scheduled_for=_aware_datetime(raw.get("scheduled_for")),
            frame_started_at=_aware_datetime(raw.get("frame_started_at")),
            trigger_tick_received_at=_aware_datetime(
                raw.get("trigger_tick_received_at")
            ),
            spot_observed_at=_optional_aware_datetime(
                raw.get("spot_observed_at")
            ),
            window_each_side=_non_negative_int(
                raw.get("window_each_side"),
                "window_each_side",
            ),
            source_interval_ms=_positive_int(
                raw.get("source_interval_ms"),
                "source_interval_ms",
            ),
            quote_refresh=parse_refresh_provenance(
                _object(raw.get("quote_refresh"), "quote_refresh")
            ),
            greeks_refresh=parse_refresh_provenance(
                _object(raw.get("greeks_refresh"), "greeks_refresh")
            ),
            feed_health=parse_feed_health(
                _object(raw.get("feed_health"), "feed_health")
            ),
            **common,
        )
    if event_type == "status":
        return FeedStatusEvent(
            status=_required_text(raw, "status").upper(),
            reason=_optional_text(raw.get("reason")),
            **common,
        )
    raise ValueError(f"Unsupported market-data event_type: {event_type!r}")


def encode_market_data_bootstrap(value: MarketDataBootstrap) -> bytes:
    payload = to_jsonable(value)
    payload["message_type"] = "bootstrap"
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def decode_market_data_bootstrap(
    payload: bytes,
    *,
    maximum_bytes: int = DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
) -> MarketDataBootstrap:
    raw = _decode_object(payload, maximum_bytes=maximum_bytes)
    _require_schema(raw)
    if raw.get("message_type") != "bootstrap":
        raise ValueError("Expected market-data bootstrap payload")
    selected_expiries = tuple(
        (str(item[0]), _date(item[1]))
        for item in _pairs(raw.get("selected_expiries"), "selected_expiries")
    )
    reference_values = tuple(
        (str(item[0]), _decimal(item[1], "reference_values"))
        for item in _pairs(raw.get("reference_values", []), "reference_values")
    )
    previous_20d_atr = tuple(
        (str(item[0]), _positive_decimal(item[1], "previous_20d_atr"))
        for item in _pairs(raw.get("previous_20d_atr", []), "previous_20d_atr")
    )
    return MarketDataBootstrap(
        handler_epoch=_required_text(raw, "handler_epoch"),
        generated_at=_aware_datetime(raw.get("generated_at")),
        source_interval_ms=_positive_int(
            raw.get("source_interval_ms"),
            "source_interval_ms",
        ),
        option_window_each_side=_non_negative_int(
            raw.get("option_window_each_side"),
            "option_window_each_side",
        ),
        selected_expiries=selected_expiries,
        spot_tokens=tuple(
            parse_instrument_token(item)
            for item in _objects(raw.get("spot_tokens"), "spot_tokens")
        ),
        option_contracts=tuple(
            parse_option_contract(item)
            for item in _objects(
                raw.get("option_contracts"),
                "option_contracts",
            )
        ),
        future_contracts=tuple(
            parse_future_contract(item)
            for item in _objects(
                raw.get("future_contracts"),
                "future_contracts",
            )
        ),
        reference_tokens=tuple(
            parse_instrument_token(item)
            for item in _objects(
                raw.get("reference_tokens"),
                "reference_tokens",
            )
        ),
        reference_values=reference_values,
        previous_20d_atr=previous_20d_atr,
        schema_version=MARKET_DATA_SCHEMA_VERSION,
    )


def parse_instrument_token(raw: dict[str, object]) -> InstrumentToken:
    kind = raw.get("kind")
    return InstrumentToken(
        exchange=Exchange(_required_text(raw, "exchange")),
        token=_required_text(raw, "token"),
        symbol=_required_text(raw, "symbol"),
        trading_symbol=_required_text(raw, "trading_symbol"),
        kind=InstrumentKind(str(kind)) if kind is not None else None,
    )


def parse_future_contract(raw: dict[str, object]) -> FutureContract:
    return FutureContract(
        underlying=_required_text(raw, "underlying"),
        expiry=_date(raw.get("expiry")),
        token=parse_instrument_token(_object(raw.get("token"), "token")),
        lot_size=_optional_non_negative_int(raw.get("lot_size"), "lot_size"),
    )


def parse_option_contract(raw: dict[str, object]) -> OptionContract:
    return OptionContract(
        underlying=_required_text(raw, "underlying"),
        expiry=_date(raw.get("expiry")),
        strike=_positive_decimal(raw.get("strike"), "strike"),
        option_type=OptionType(_required_text(raw, "option_type")),
        token=parse_instrument_token(_object(raw.get("token"), "token")),
        lot_size=_optional_non_negative_int(raw.get("lot_size"), "lot_size"),
    )


def parse_market_tick(raw: dict[str, object]) -> MarketTick:
    raw_payload = raw.get("raw")
    if raw_payload is not None and not isinstance(raw_payload, dict):
        raise ValueError("tick.raw must be an object")
    return MarketTick(
        token=parse_instrument_token(_object(raw.get("token"), "token")),
        exchange_timestamp=_aware_datetime(raw.get("exchange_timestamp")),
        received_at=_aware_datetime(raw.get("received_at")),
        ltp=_optional_decimal(raw.get("ltp"), "ltp", non_negative=True),
        open_price=_optional_decimal(
            raw.get("open_price"),
            "open_price",
            non_negative=True,
        ),
        high_price=_optional_decimal(
            raw.get("high_price"),
            "high_price",
            non_negative=True,
        ),
        low_price=_optional_decimal(
            raw.get("low_price"),
            "low_price",
            non_negative=True,
        ),
        close_price=_optional_decimal(
            raw.get("close_price"),
            "close_price",
            non_negative=True,
        ),
        oi=_optional_non_negative_int(raw.get("oi"), "oi"),
        oi_change=_optional_int(raw.get("oi_change"), "oi_change"),
        oi_change_percent=_optional_decimal(
            raw.get("oi_change_percent"),
            "oi_change_percent",
        ),
        volume=_optional_non_negative_int(raw.get("volume"), "volume"),
        bid=_optional_decimal(raw.get("bid"), "bid", non_negative=True),
        ask=_optional_decimal(raw.get("ask"), "ask", non_negative=True),
        quality=TickQuality(str(raw.get("quality") or TickQuality.LIVE.value)),
        raw=dict(raw_payload or {}),
    )


def parse_option_chain_snapshot(
    raw: dict[str, object],
) -> OptionChainSnapshot:
    quotes = tuple(
        parse_option_quote(item)
        for item in _objects(raw.get("quotes"), "quotes")
    )
    reference_raw = raw.get("reference")
    market_raw = raw.get("market")
    return OptionChainSnapshot(
        underlying=_required_text(raw, "underlying"),
        expiry=_date(raw.get("expiry")),
        spot_price=_positive_decimal(raw.get("spot_price"), "spot_price"),
        atm_strike=_positive_decimal(raw.get("atm_strike"), "atm_strike"),
        captured_at=_aware_datetime(raw.get("captured_at")),
        quotes=quotes,
        reference=(
            parse_underlying_reference(
                _object(reference_raw, "reference")
            )
            if reference_raw is not None
            else None
        ),
        market=(
            parse_underlying_market_snapshot(
                _object(market_raw, "market")
            )
            if market_raw is not None
            else None
        ),
    )


def parse_option_quote(raw: dict[str, object]) -> OptionQuote:
    contract = parse_option_contract(
        _object(raw.get("contract"), "contract")
    )
    greeks_raw = raw.get("greeks")
    greeks = None
    if greeks_raw is not None:
        greeks_mapping = _object(greeks_raw, "greeks")
        greeks = GreeksSnapshot(
            contract=contract,
            captured_at=_aware_datetime(greeks_mapping.get("captured_at")),
            implied_volatility=_optional_decimal(
                greeks_mapping.get("implied_volatility"),
                "implied_volatility",
                non_negative=True,
            ),
            delta=_optional_decimal(greeks_mapping.get("delta"), "delta"),
            gamma=_optional_decimal(greeks_mapping.get("gamma"), "gamma"),
            theta=_optional_decimal(greeks_mapping.get("theta"), "theta"),
            vega=_optional_decimal(greeks_mapping.get("vega"), "vega"),
            source=str(greeks_mapping.get("source") or "transport"),
        )
    return OptionQuote(
        contract=contract,
        ltp=_optional_decimal(raw.get("ltp"), "ltp", non_negative=True),
        open_price=_optional_decimal(
            raw.get("open_price"), "open_price", non_negative=True
        ),
        high_price=_optional_decimal(
            raw.get("high_price"), "high_price", non_negative=True
        ),
        low_price=_optional_decimal(
            raw.get("low_price"), "low_price", non_negative=True
        ),
        close_price=_optional_decimal(
            raw.get("close_price"), "close_price", non_negative=True
        ),
        oi=_optional_non_negative_int(raw.get("oi"), "oi"),
        oi_change=_optional_int(raw.get("oi_change"), "oi_change"),
        oi_change_percent=_optional_decimal(
            raw.get("oi_change_percent"), "oi_change_percent"
        ),
        volume=_optional_non_negative_int(raw.get("volume"), "volume"),
        bid=_optional_decimal(raw.get("bid"), "bid", non_negative=True),
        ask=_optional_decimal(raw.get("ask"), "ask", non_negative=True),
        greeks=greeks,
    )


def parse_underlying_market_snapshot(
    raw: dict[str, object],
) -> UnderlyingMarketSnapshot:
    return UnderlyingMarketSnapshot(
        underlying=_required_text(raw, "underlying"),
        captured_at=_aware_datetime(raw.get("captured_at")),
        spot_observed_at=_optional_aware_datetime(raw.get("spot_observed_at")),
        open_price=_optional_decimal(raw.get("open_price"), "open_price"),
        high_price=_optional_decimal(raw.get("high_price"), "high_price"),
        low_price=_optional_decimal(raw.get("low_price"), "low_price"),
        previous_close=_optional_decimal(
            raw.get("previous_close"), "previous_close"
        ),
        future_observed_at=_optional_aware_datetime(
            raw.get("future_observed_at")
        ),
        future_price=_optional_decimal(
            raw.get("future_price"), "future_price"
        ),
        future_open=_optional_decimal(raw.get("future_open"), "future_open"),
        future_high=_optional_decimal(raw.get("future_high"), "future_high"),
        future_low=_optional_decimal(raw.get("future_low"), "future_low"),
        future_previous_close=_optional_decimal(
            raw.get("future_previous_close"),
            "future_previous_close",
        ),
        future_volume=_optional_non_negative_int(
            raw.get("future_volume"), "future_volume"
        ),
        future_oi=_optional_non_negative_int(
            raw.get("future_oi"), "future_oi"
        ),
        future_vwap=_optional_decimal(
            raw.get("future_vwap"), "future_vwap"
        ),
        basis=_optional_decimal(raw.get("basis"), "basis"),
        previous_20d_atr=_optional_decimal(
            raw.get("previous_20d_atr"),
            "previous_20d_atr",
            non_negative=True,
        ),
        previous_session_expected_move=_optional_decimal(
            raw.get("previous_session_expected_move"),
            "previous_session_expected_move",
            non_negative=True,
        ),
        market_breadth=_optional_decimal(
            raw.get("market_breadth"), "market_breadth"
        ),
        india_vix=_optional_decimal(
            raw.get("india_vix"), "india_vix", non_negative=True
        ),
    )


def parse_underlying_reference(
    raw: dict[str, object],
) -> UnderlyingReference:
    future_raw = raw.get("future_token")
    return UnderlyingReference(
        underlying=_required_text(raw, "underlying"),
        index_token=parse_instrument_token(
            _object(raw.get("index_token"), "index_token")
        ),
        future_token=(
            parse_instrument_token(
                _object(future_raw, "future_token")
            )
            if future_raw is not None
            else None
        ),
        index_price=_optional_decimal(
            raw.get("index_price"),
            "index_price",
            non_negative=True,
        ),
        future_price=_optional_decimal(
            raw.get("future_price"),
            "future_price",
            non_negative=True,
        ),
        basis=_optional_decimal(raw.get("basis"), "basis"),
    )


def parse_refresh_provenance(raw: dict[str, object]) -> RefreshProvenance:
    exchange_tokens = tuple(
        (str(pair[0]), tuple(str(token) for token in _list(pair[1], "tokens")))
        for pair in _pairs(raw.get("exchange_tokens", []), "exchange_tokens")
    )
    return RefreshProvenance(
        status=_required_text(raw, "status"),
        requested_at=_optional_aware_datetime(raw.get("requested_at")),
        responded_at=_optional_aware_datetime(raw.get("responded_at")),
        attempts=_non_negative_int(raw.get("attempts"), "attempts"),
        row_count=_non_negative_int(raw.get("row_count"), "row_count"),
        normalized_tokens=tuple(
            str(token)
            for token in _list(raw.get("normalized_tokens", []), "tokens")
        ),
        exchange_tokens=exchange_tokens,
        mode=_optional_text(raw.get("mode")),
        broker_status=(
            bool(raw["broker_status"])
            if raw.get("broker_status") is not None
            else None
        ),
        error=_optional_text(raw.get("error")),
    )


def parse_feed_health(raw: dict[str, object]) -> FeedHealthSnapshot:
    return FeedHealthSnapshot(
        status=_required_text(raw, "status").upper(),
        reason=_optional_text(raw.get("reason")),
        queue_depth=_optional_non_negative_int(
            raw.get("queue_depth"), "queue_depth"
        ),
        queue_capacity=_optional_non_negative_int(
            raw.get("queue_capacity"), "queue_capacity"
        ),
        queue_pressure_threshold=_optional_non_negative_int(
            raw.get("queue_pressure_threshold"),
            "queue_pressure_threshold",
        ),
        queue_high_watermark=_optional_non_negative_int(
            raw.get("queue_high_watermark"), "queue_high_watermark"
        ),
        received_events=_optional_non_negative_int(
            raw.get("received_events"), "received_events"
        ),
        enqueued_events=_optional_non_negative_int(
            raw.get("enqueued_events"), "enqueued_events"
        ),
        dropped_events=_optional_non_negative_int(
            raw.get("dropped_events"), "dropped_events"
        ),
        queue_pressure_events=_optional_non_negative_int(
            raw.get("queue_pressure_events"), "queue_pressure_events"
        ),
        last_received_at=_optional_aware_datetime(raw.get("last_received_at")),
        last_error=_optional_text(raw.get("last_error")),
    )


def _decode_object(payload: bytes, *, maximum_bytes: int) -> dict[str, object]:
    if not payload:
        raise ValueError("Market-data payload is empty")
    if len(payload) > maximum_bytes:
        raise ValueError(
            f"Market-data payload exceeds {maximum_bytes} bytes"
        )
    try:
        raw = orjson.loads(payload)
    except orjson.JSONDecodeError as exc:
        raise ValueError("Market-data payload is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Market-data payload root must be an object")
    return raw


def _require_schema(raw: dict[str, object]) -> None:
    version = raw.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != MARKET_DATA_SCHEMA_VERSION
    ):
        raise ValueError(
            "Unsupported market-data schema_version: "
            f"{version!r}"
        )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _objects(value: object, name: str) -> list[dict[str, object]]:
    return [
        _object(item, name)
        for item in _list(value, name)
    ]


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _pairs(value: object, name: str) -> list[list[object]]:
    pairs = _list(value, name)
    if not all(isinstance(item, list) and len(item) == 2 for item in pairs):
        raise ValueError(f"{name} must contain two-item arrays")
    return pairs


def _required_text(raw: dict[str, object], name: str) -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _aware_datetime(value: object) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Market-data timestamps must be timezone-aware")
    return parsed


def _optional_aware_datetime(value: object) -> datetime | None:
    return _aware_datetime(value) if value is not None else None


def _date(value: object) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"Invalid date: {value!r}") from exc


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _positive_decimal(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _optional_decimal(
    value: object,
    name: str,
    *,
    non_negative: bool = False,
) -> Decimal | None:
    if value is None:
        return None
    result = _decimal(value, name)
    if non_negative and result < 0:
        raise ValueError(f"{name} cannot be negative")
    return result


def _non_negative_int(value: object, name: str) -> int:
    result = _int(value, name)
    if result < 0:
        raise ValueError(f"{name} cannot be negative")
    return result


def _positive_int(value: object, name: str) -> int:
    result = _int(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _optional_non_negative_int(value: object, name: str) -> int | None:
    return None if value is None else _non_negative_int(value, name)


def _optional_int(value: object, name: str) -> int | None:
    return None if value is None else _int(value, name)


def _int(value: object, name: str) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        numeric = Decimal(str(value))
        if not numeric.is_finite() or numeric != numeric.to_integral_value():
            raise ValueError
        return int(numeric)
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
