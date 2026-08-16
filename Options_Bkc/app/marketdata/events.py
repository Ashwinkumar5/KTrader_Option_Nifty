from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.domain.models import (
    FutureContract,
    InstrumentToken,
    MarketTick,
    OptionChainSnapshot,
    OptionContract,
)
from app.instruments.master import InstrumentMaster


MARKET_DATA_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RefreshProvenance:
    status: str
    requested_at: datetime | None
    responded_at: datetime | None
    attempts: int
    row_count: int
    normalized_tokens: tuple[str, ...] = ()
    exchange_tokens: tuple[tuple[str, tuple[str, ...]], ...] = ()
    mode: str | None = None
    broker_status: bool | None = None
    error: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, object],
    ) -> RefreshProvenance:
        raw_exchange_tokens = value.get("exchange_tokens")
        exchange_tokens = (
            tuple(
                (str(exchange), tuple(str(token) for token in tokens))
                for exchange, tokens in sorted(raw_exchange_tokens.items())
                if isinstance(tokens, (list, tuple))
            )
            if isinstance(raw_exchange_tokens, dict)
            else ()
        )
        return cls(
            status=str(value.get("status") or "unknown"),
            requested_at=_datetime_or_none(value.get("requested_at")),
            responded_at=_datetime_or_none(value.get("responded_at")),
            attempts=int(value.get("attempts") or 0),
            row_count=int(value.get("row_count") or 0),
            normalized_tokens=tuple(
                str(token)
                for token in value.get("normalized_tokens", ())
            ),
            exchange_tokens=exchange_tokens,
            mode=(
                str(value["mode"])
                if value.get("mode") is not None
                else None
            ),
            broker_status=(
                bool(value["broker_status"])
                if value.get("broker_status") is not None
                else None
            ),
            error=(
                str(value["error"])
                if value.get("error") is not None
                else None
            ),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "requested_at": self.requested_at,
            "responded_at": self.responded_at,
            "attempts": self.attempts,
            "row_count": self.row_count,
            "normalized_tokens": self.normalized_tokens,
            "exchange_tokens": {
                exchange: list(tokens)
                for exchange, tokens in self.exchange_tokens
            },
            "mode": self.mode,
            "broker_status": self.broker_status,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class FeedHealthSnapshot:
    status: str
    reason: str | None = None
    queue_depth: int | None = None
    queue_capacity: int | None = None
    queue_pressure_threshold: int | None = None
    queue_high_watermark: int | None = None
    received_events: int | None = None
    enqueued_events: int | None = None
    dropped_events: int | None = None
    queue_pressure_events: int | None = None
    last_received_at: datetime | None = None
    last_error: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, object] | None,
    ) -> FeedHealthSnapshot:
        raw = value or {}
        return cls(
            status=str(raw.get("status") or "UNAVAILABLE").upper(),
            reason=_string_or_none(raw.get("reason")),
            queue_depth=_int_or_none(raw.get("queue_depth")),
            queue_capacity=_int_or_none(raw.get("queue_capacity")),
            queue_pressure_threshold=_int_or_none(
                raw.get("queue_pressure_threshold")
            ),
            queue_high_watermark=_int_or_none(
                raw.get("queue_high_watermark")
            ),
            received_events=_int_or_none(raw.get("received_events")),
            enqueued_events=_int_or_none(raw.get("enqueued_events")),
            dropped_events=_int_or_none(raw.get("dropped_events")),
            queue_pressure_events=_int_or_none(
                raw.get("queue_pressure_events")
            ),
            last_received_at=_datetime_or_none(raw.get("last_received_at")),
            last_error=_string_or_none(raw.get("last_error")),
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "queue_depth": self.queue_depth,
            "queue_capacity": self.queue_capacity,
            "queue_pressure_threshold": self.queue_pressure_threshold,
            "queue_high_watermark": self.queue_high_watermark,
            "received_events": self.received_events,
            "enqueued_events": self.enqueued_events,
            "dropped_events": self.dropped_events,
            "queue_pressure_events": self.queue_pressure_events,
            "last_received_at": self.last_received_at,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class RawMarketTickEvent:
    handler_epoch: str
    event_id: str
    published_at: datetime
    tick: MarketTick
    schema_version: int = MARKET_DATA_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class MaterializedOptionChainFrame:
    handler_epoch: str
    event_id: str
    published_at: datetime
    snapshot: OptionChainSnapshot
    scheduled_for: datetime
    frame_started_at: datetime
    trigger_tick_received_at: datetime
    spot_observed_at: datetime | None
    window_each_side: int
    source_interval_ms: int
    quote_refresh: RefreshProvenance
    greeks_refresh: RefreshProvenance
    feed_health: FeedHealthSnapshot
    schema_version: int = MARKET_DATA_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class FeedStatusEvent:
    handler_epoch: str
    event_id: str
    published_at: datetime
    status: str
    reason: str | None = None
    schema_version: int = MARKET_DATA_SCHEMA_VERSION


MarketDataEvent = (
    RawMarketTickEvent
    | MaterializedOptionChainFrame
    | FeedStatusEvent
)


@dataclass(frozen=True, slots=True)
class MarketDataBootstrap:
    handler_epoch: str
    generated_at: datetime
    source_interval_ms: int
    option_window_each_side: int
    selected_expiries: tuple[tuple[str, date], ...]
    spot_tokens: tuple[InstrumentToken, ...]
    option_contracts: tuple[OptionContract, ...]
    future_contracts: tuple[FutureContract, ...]
    reference_tokens: tuple[InstrumentToken, ...]
    reference_values: tuple[tuple[str, Decimal], ...] = ()
    previous_20d_atr: tuple[tuple[str, Decimal], ...] = ()
    schema_version: int = MARKET_DATA_SCHEMA_VERSION

    def instrument_master(self) -> InstrumentMaster:
        return InstrumentMaster(
            options=self.option_contracts,
            spot_tokens={token.symbol: token for token in self.spot_tokens},
            futures=self.future_contracts,
            reference_tokens={
                token.symbol: token for token in self.reference_tokens
            },
        )

    def expiry_map(self) -> dict[str, date]:
        return dict(self.selected_expiries)


def _datetime_or_none(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _string_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _int_or_none(value: object) -> int | None:
    return int(value) if value is not None else None
