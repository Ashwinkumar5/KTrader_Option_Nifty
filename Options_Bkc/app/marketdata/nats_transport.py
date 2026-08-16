from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from inspect import isawaitable
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

from app.domain.models import (
    GreeksSnapshot,
    InstrumentToken,
    MarketTick,
    OptionContract,
)
from app.marketdata.events import (
    MARKET_DATA_SCHEMA_VERSION,
    FeedStatusEvent,
    MarketDataBootstrap,
    MarketDataEvent,
    MaterializedOptionChainFrame,
    RawMarketTickEvent,
    RefreshProvenance,
)
from app.marketdata.feed_handler import FeedHandlerRuntime, build_token_lookup
from app.marketdata.serde import (
    DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
    decode_market_data_bootstrap,
    decode_market_data_event,
    encode_market_data_bootstrap,
    encode_market_data_event,
)
from app.optionchain.state import OptionChainState


_STOP = object()
_DEFAULT_NATS_URL = "nats://127.0.0.1:4222"
_DEFAULT_SUBJECT_PREFIX = "ktrader.marketdata.v1"
_DEFAULT_QUEUE_CAPACITY = 8192
_DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS = 15.0
_DEFAULT_CONSUMER_INTERVAL_MS = 5000
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_FLUSH_TIMEOUT_SECONDS = 5.0
_DEFAULT_DRAIN_TIMEOUT_SECONDS = 5.0
_DEFAULT_MAX_TICK_LAG_SECONDS = 5.0
_MAX_EVENT_CLOCK_LEAD_SECONDS = 5.0
_FATAL_FEED_STATUSES = frozenset(
    {
        "DATA_LOSS",
        "FAILED",
        "SESSION_RESET",
        "STOPPED",
    }
)
_NON_FATAL_FEED_STATUSES = frozenset(
    {
        "CONNECTED",
        "HEALTHY",
        "READY",
    }
)


class MarketDataTransportError(RuntimeError):
    """Base error for the fail-closed Core-NATS market-data transport."""


class MarketDataTransportFatalError(MarketDataTransportError):
    """A possible event gap was observed; the process must be restarted."""


@dataclass(frozen=True, slots=True)
class _QueuedTick:
    tick: MarketTick
    published_at: datetime
    enqueued_monotonic: float
    sequence: int


@dataclass(frozen=True, slots=True)
class _QueuedFrame:
    frame: MaterializedOptionChainFrame
    enqueued_monotonic: float
    required_tick_sequence: int


@dataclass(frozen=True, slots=True)
class NatsMarketDataSubjects:
    """Versioned Core-NATS subjects derived from one configured prefix.

    All live events intentionally share one subject so Core NATS preserves the
    publisher's tick/frame/status order. Bootstrap is request/reply and therefore
    uses a separate subject.
    """

    prefix: str
    events: str
    bootstrap: str

    @classmethod
    def from_prefix(cls, prefix: str) -> NatsMarketDataSubjects:
        normalized = _normalize_subject_prefix(prefix)
        return cls(
            prefix=normalized,
            events=f"{normalized}.events",
            bootstrap=f"{normalized}.bootstrap",
        )


class _NatsMessage(Protocol):
    data: bytes
    reply: str


class _NatsSubscription(Protocol):
    async def unsubscribe(self) -> None: ...


class _NatsClient(Protocol):
    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        reply: str = "",
        headers: dict[str, str] | None = None,
    ) -> None: ...

    async def request(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float = 0.5,
        **kwargs: object,
    ) -> _NatsMessage: ...

    async def subscribe(
        self,
        subject: str,
        *,
        cb: Callable[[_NatsMessage], Awaitable[None]],
        pending_msgs_limit: int = 524288,
        pending_bytes_limit: int = 134217728,
    ) -> _NatsSubscription: ...

    async def flush(self, timeout: float = 10.0) -> None: ...

    async def drain(self) -> None: ...

    async def close(self) -> None: ...


_NatsClientFactory: TypeAlias = Callable[..., _NatsClient | Awaitable[_NatsClient]]
_BootstrapProvider: TypeAlias = Callable[
    [], MarketDataBootstrap | Awaitable[MarketDataBootstrap]
]


class NatsMarketDataPublisher:
    """Bounded, ordered and non-blocking market-data publisher.

    The broker loop performs only serialization plus ``publish_encoded``. One
    background task owns all calls to Core NATS, preserving admission order and
    keeping network backpressure out of the broker feed callback.
    """

    def __init__(
        self,
        *,
        nats_url: str = _DEFAULT_NATS_URL,
        subject_prefix: str = _DEFAULT_SUBJECT_PREFIX,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        flush_timeout_seconds: float = _DEFAULT_FLUSH_TIMEOUT_SECONDS,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        maximum_payload_bytes: int = DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
        client_factory: _NatsClientFactory | None = None,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        if maximum_payload_bytes <= 0:
            raise ValueError("maximum_payload_bytes must be greater than zero")
        self._nats_url = _require_text(nats_url, "nats_url")
        self.subjects = NatsMarketDataSubjects.from_prefix(subject_prefix)
        self._connect_timeout_seconds = _positive_seconds(
            connect_timeout_seconds,
            "connect_timeout_seconds",
        )
        self._flush_timeout_seconds = _positive_seconds(
            flush_timeout_seconds,
            "flush_timeout_seconds",
        )
        self._drain_timeout_seconds = _positive_seconds(
            drain_timeout_seconds,
            "drain_timeout_seconds",
        )
        self._maximum_payload_bytes = maximum_payload_bytes
        self._client_factory = client_factory
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._client: _NatsClient | None = None
        self._bootstrap_subscription: _NatsSubscription | None = None
        self._bootstrap_provider: _BootstrapProvider | None = None
        self._publisher_task: asyncio.Task[None] | None = None
        self._fatal_error: BaseException | None = None
        self._started = False
        self._closing = False
        self._closed = False
        self._admitted = 0
        self._published = 0
        self._queue_high_watermark = 0

    async def start(
        self,
        bootstrap: MarketDataBootstrap | _BootstrapProvider,
    ) -> None:
        """Connect, install the bootstrap responder, and flush readiness."""

        if self._started:
            self.set_bootstrap(bootstrap)
            return
        self._raise_if_unavailable()
        self.set_bootstrap(bootstrap)
        try:
            self._client = await _connect_nats_client(
                nats_url=self._nats_url,
                name="ktrader-market-data-publisher",
                connect_timeout_seconds=self._connect_timeout_seconds,
                startup_timeout_seconds=self._connect_timeout_seconds,
                client_factory=self._client_factory,
                error_cb=self._on_nats_error,
                disconnected_cb=self._on_disconnected,
                closed_cb=self._on_closed,
            )
            self._bootstrap_subscription = await self._client.subscribe(
                self.subjects.bootstrap,
                cb=self._on_bootstrap_request,
                pending_msgs_limit=max(16, min(self._queue.maxsize, 1024)),
                pending_bytes_limit=min(
                    64 * 1024 * 1024,
                    max(
                        self._maximum_payload_bytes * 2,
                        self._maximum_payload_bytes
                        * min(self._queue.maxsize, 16),
                    ),
                ),
            )
            # The service starts this publisher only after broker reference
            # subscriptions are live. This flush makes a successful bootstrap
            # response a readiness proof even for late Core-NATS subscribers.
            await self._client.flush(timeout=self._flush_timeout_seconds)
            self._publisher_task = asyncio.create_task(
                self._run_publisher(),
                name="nats-market-data-publisher",
            )
            self._started = True
        except BaseException as exc:
            self._mark_fatal(exc)
            await self._close_client_safely()
            raise

    def set_bootstrap(
        self,
        bootstrap: MarketDataBootstrap | _BootstrapProvider,
    ) -> None:
        """Replace the request/reply bootstrap source without network I/O."""

        self._raise_if_unavailable()
        if isinstance(bootstrap, MarketDataBootstrap):
            _validate_bootstrap(bootstrap)
            self._bootstrap_provider = lambda value=bootstrap: value
            return
        if not callable(bootstrap):
            raise TypeError("bootstrap must be a MarketDataBootstrap or callable")
        self._bootstrap_provider = bootstrap

    def publish(self, event: MarketDataEvent) -> bool:
        """Encode once and attempt bounded admission without blocking."""

        return self.publish_encoded(encode_market_data_event(event))

    def publish_encoded(self, payload: bytes) -> bool:
        """Admit an already encoded event while preserving encode-once usage."""

        self._raise_if_unavailable()
        if not self._started:
            raise MarketDataTransportError("NATS market-data publisher is not started")
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not payload:
            raise ValueError("payload cannot be empty")
        if len(payload) > self._maximum_payload_bytes:
            raise ValueError(
                "market-data payload exceeds "
                f"{self._maximum_payload_bytes} bytes"
            )
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS publisher queue overflow; market-data continuity "
                    "cannot be guaranteed"
                )
            )
            return False
        self._admitted += 1
        self._queue_high_watermark = max(
            self._queue_high_watermark,
            self._queue.qsize(),
        )
        return True

    async def flush(self) -> None:
        """Wait for admitted messages and obtain a server round-trip."""

        self._raise_if_unavailable()
        if not self._started or self._client is None:
            raise MarketDataTransportError("NATS market-data publisher is not started")
        await self._wait_for_publish_queue()
        self._raise_if_unavailable()
        await self._client.flush(timeout=self._flush_timeout_seconds)
        self._raise_if_unavailable()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "status": "FAILED" if self._fatal_error is not None else "HEALTHY",
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "queue_high_watermark": self._queue_high_watermark,
            "admitted_events": self._admitted,
            "published_events": self._published,
            "last_error": _render_error(self._fatal_error),
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        cleanup_error: BaseException | None = None
        task = self._publisher_task
        if task is not None:
            try:
                await self._stop_publisher_task(task)
            except BaseException as exc:
                cleanup_error = exc
        subscription = self._bootstrap_subscription
        self._bootstrap_subscription = None
        if subscription is not None:
            try:
                await subscription.unsubscribe()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            await self._drain_client()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        finally:
            self._publisher_task = None
            self._client = None
            self._closed = True
        if cleanup_error is not None and self._fatal_error is None:
            raise MarketDataTransportError(
                "Failed to close NATS market-data publisher"
            ) from cleanup_error

    async def _run_publisher(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _STOP:
                        return
                    if not isinstance(item, bytes):  # pragma: no cover
                        raise TypeError("NATS publisher queue item must be bytes")
                    client = self._client
                    if client is None:
                        raise MarketDataTransportFatalError(
                            "NATS publisher client disappeared"
                        )
                    await client.publish(self.subjects.events, item)
                    self._published += 1
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_fatal(exc)
            raise

    async def _on_bootstrap_request(self, message: _NatsMessage) -> None:
        if self._closing or self._fatal_error is not None:
            return
        reply = str(getattr(message, "reply", "") or "")
        if not reply:
            return
        try:
            provider = self._bootstrap_provider
            if provider is None:
                raise MarketDataTransportFatalError(
                    "NATS bootstrap provider is unavailable"
                )
            value = provider()
            if isawaitable(value):
                value = await value
            if not isinstance(value, MarketDataBootstrap):
                raise TypeError(
                    "bootstrap provider must return MarketDataBootstrap"
                )
            _validate_bootstrap(value)
            client = self._client
            if client is None:
                raise MarketDataTransportFatalError(
                    "NATS publisher client is unavailable"
                )
            await client.publish(reply, encode_market_data_bootstrap(value))
        except BaseException as exc:
            self._mark_fatal(exc)

    async def _on_nats_error(self, error: BaseException) -> None:
        if not self._closing:
            self._mark_fatal(error)

    async def _on_disconnected(self) -> None:
        if not self._closing:
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS publisher disconnected; publication continuity is unknown"
                )
            )

    async def _on_closed(self) -> None:
        if not self._closing:
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS publisher connection closed unexpectedly"
                )
            )

    async def _wait_for_publish_queue(self) -> None:
        task = self._publisher_task
        if task is None:
            return
        join = asyncio.create_task(
            self._queue.join(),
            name="nats-market-data-publisher-flush",
        )
        done, _pending = await asyncio.wait(
            (join, task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done and not join.done():
            join.cancel()
            await asyncio.gather(join, return_exceptions=True)
            await task
        await join

    async def _stop_publisher_task(
        self,
        task: asyncio.Task[None],
    ) -> None:
        if task.done():
            await task
            return
        try:
            await asyncio.wait_for(
                self._wait_for_publish_queue(),
                timeout=self._drain_timeout_seconds,
            )
            self._queue.put_nowait(_STOP)
            await asyncio.wait_for(
                task,
                timeout=self._drain_timeout_seconds,
            )
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._discard_publish_queue()
            raise

    def _discard_publish_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self._queue.task_done()

    async def _drain_client(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.drain(),
                timeout=self._drain_timeout_seconds,
            )
        except BaseException:
            await self._close_client_safely()
            raise

    async def _close_client_safely(self) -> None:
        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                value = close()
                if isawaitable(value):
                    await value
            except BaseException:
                pass

    def _raise_if_unavailable(self) -> None:
        if self._closed or self._closing:
            raise MarketDataTransportError("NATS market-data publisher is closed")
        if self._fatal_error is not None:
            raise MarketDataTransportFatalError(
                "NATS market-data publisher failed"
            ) from self._fatal_error

    def _mark_fatal(self, error: BaseException) -> None:
        if self._fatal_error is None:
            self._fatal_error = error


class NatsMarketDataFeedHandler:
    """Subscriber-side feed-handler adapter with no broker capabilities."""

    is_remote_subscriber = True

    def __init__(
        self,
        *,
        nats_url: str = _DEFAULT_NATS_URL,
        subject_prefix: str = _DEFAULT_SUBJECT_PREFIX,
        queue_capacity: int = _DEFAULT_QUEUE_CAPACITY,
        bootstrap_timeout_seconds: float = (
            _DEFAULT_BOOTSTRAP_TIMEOUT_SECONDS
        ),
        consumer_interval_ms: int = _DEFAULT_CONSUMER_INTERVAL_MS,
        connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        drain_timeout_seconds: float = _DEFAULT_DRAIN_TIMEOUT_SECONDS,
        max_tick_lag_seconds: float = _DEFAULT_MAX_TICK_LAG_SECONDS,
        max_frame_lag_seconds: float | None = None,
        maximum_payload_bytes: int = DEFAULT_MAX_MARKET_DATA_PAYLOAD_BYTES,
        client_factory: _NatsClientFactory | None = None,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        if consumer_interval_ms <= 0:
            raise ValueError("consumer_interval_ms must be greater than zero")
        if maximum_payload_bytes <= 0:
            raise ValueError("maximum_payload_bytes must be greater than zero")
        self._nats_url = _require_text(nats_url, "nats_url")
        self.subjects = NatsMarketDataSubjects.from_prefix(subject_prefix)
        self._bootstrap_timeout_seconds = _positive_seconds(
            bootstrap_timeout_seconds,
            "bootstrap_timeout_seconds",
        )
        self._consumer_interval_ms = consumer_interval_ms
        self._connect_timeout_seconds = _positive_seconds(
            connect_timeout_seconds,
            "connect_timeout_seconds",
        )
        self._drain_timeout_seconds = _positive_seconds(
            drain_timeout_seconds,
            "drain_timeout_seconds",
        )
        self._max_tick_lag_seconds = _positive_seconds(
            max_tick_lag_seconds,
            "max_tick_lag_seconds",
        )
        self._max_frame_lag_seconds = (
            max(30.0, (2 * consumer_interval_ms) / 1000.0)
            if max_frame_lag_seconds is None
            else _positive_seconds(
                max_frame_lag_seconds,
                "max_frame_lag_seconds",
            )
        )
        self._maximum_payload_bytes = maximum_payload_bytes
        self._client_factory = client_factory
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._ticks: asyncio.Queue[_QueuedTick] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._frame_queues: dict[
            str,
            asyncio.Queue[_QueuedFrame],
        ] = {}
        self._client: _NatsClient | None = None
        self._event_subscription: _NatsSubscription | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._bootstrap: MarketDataBootstrap | None = None
        self._runtime: FeedHandlerRuntime | None = None
        self._handler_epoch: str | None = None
        self._fatal_error: BaseException | None = None
        self._fatal_event: asyncio.Event | None = None
        self._closing = False
        self._closed = False
        self._received = 0
        self._decoded = 0
        self._dropped = 0
        self._inbound_high_watermark = 0
        self._last_accepted_frame_at: dict[str, datetime] = {}
        self._last_event_id: str | None = None
        self._dispatched_tick_sequence = 0
        self._acknowledged_tick_sequence = 0
        self._tick_ack_event = asyncio.Event()

    @property
    def bootstrap(self) -> MarketDataBootstrap | None:
        return self._bootstrap

    async def prepare(self) -> FeedHandlerRuntime:
        if self._runtime is not None:
            self._raise_if_unavailable()
            return self._runtime
        self._raise_if_unavailable()
        self._fatal_event = asyncio.Event()
        try:
            self._client = await _connect_nats_client(
                nats_url=self._nats_url,
                name="ktrader-market-data-subscriber",
                connect_timeout_seconds=self._connect_timeout_seconds,
                startup_timeout_seconds=self._bootstrap_timeout_seconds,
                client_factory=self._client_factory,
                error_cb=self._on_nats_error,
                disconnected_cb=self._on_disconnected,
                closed_cb=self._on_closed,
            )
            pending_bytes = min(
                256 * 1024 * 1024,
                max(
                    self._maximum_payload_bytes * 2,
                    self._maximum_payload_bytes
                    * min(self._inbound.maxsize, 64),
                ),
            )
            self._event_subscription = await self._client.subscribe(
                self.subjects.events,
                cb=self._on_event_message,
                pending_msgs_limit=self._inbound.maxsize,
                pending_bytes_limit=pending_bytes,
            )
            # Subscribe+flush before request prevents a tick/frame gap between
            # acquiring bootstrap metadata and becoming an event consumer.
            await self._client.flush(timeout=self._connect_timeout_seconds)
            bootstrap = await self._request_bootstrap_with_retry()
            _validate_consumer_interval(
                source_interval_ms=bootstrap.source_interval_ms,
                consumer_interval_ms=self._consumer_interval_ms,
            )
            self._bootstrap = bootstrap
            self._handler_epoch = bootstrap.handler_epoch
            master = bootstrap.instrument_master()
            token_lookup = MappingProxyType(build_token_lookup(master))
            self._runtime = FeedHandlerRuntime(
                master=master,
                token_lookup=token_lookup,
            )
            underlying_names = {
                underlying.upper()
                for underlying, _expiry in bootstrap.selected_expiries
            }
            # Only due/downsampled frames enter these queues. Two slots allow
            # normal scheduler jitter; a third unconsumed decision frame means
            # the strategy is stalled, so dispatcher admission fails closed.
            frame_queue_capacity = min(self._inbound.maxsize, 2)
            self._frame_queues = {
                underlying: asyncio.Queue(maxsize=frame_queue_capacity)
                for underlying in sorted(underlying_names)
            }
            self._dispatcher_task = asyncio.create_task(
                self._run_dispatcher(),
                name="nats-market-data-subscriber-dispatcher",
            )
            self._raise_if_unavailable()
            return self._runtime
        except BaseException as exc:
            self._mark_fatal(exc)
            await self._close_client_safely()
            raise

    async def start(self, *, market_date: date) -> tuple[InstrumentToken, ...]:
        """Return bootstrap reference tokens; bootstrap success proves ready."""

        self._raise_if_unavailable()
        bootstrap = self._require_bootstrap()
        runtime = self._require_runtime()
        underlyings = tuple(
            underlying
            for underlying, _expiry in bootstrap.selected_expiries
        )
        tokens: list[InstrumentToken] = list(bootstrap.spot_tokens)
        for underlying in underlyings:
            future = runtime.master.nearest_future(
                underlying=underlying,
                as_of=market_date,
            )
            if future is not None:
                tokens.append(future.token)
        tokens.extend(bootstrap.reference_tokens)
        return _deduplicate_tokens(tokens)

    async def initialize_reference_data(
        self,
        *,
        state: OptionChainState,
        market_date: date,
    ) -> dict[str, object]:
        """Apply immutable bootstrap reference data without broker calls."""

        del market_date
        self._raise_if_unavailable()
        bootstrap = self._require_bootstrap()
        values = dict(bootstrap.reference_values)
        for name, value in bootstrap.reference_values:
            state.set_reference_value(name, value)
        for underlying, value in bootstrap.previous_20d_atr:
            state.set_previous_20d_atr(underlying, value)
        india_vix = values.get("INDIA_VIX")
        return {
            "india_vix": (
                {"status": "READY", "value": india_vix}
                if india_vix is not None
                else {
                    "status": "UNAVAILABLE",
                    "reason": "bootstrap_reference_missing",
                }
            ),
            "previous_20d_atr": {
                underlying: {
                    "status": "READY",
                    "value": value,
                    "periods": 20,
                }
                for underlying, value in bootstrap.previous_20d_atr
            },
        }

    def ticks(self) -> AsyncIterator[MarketTick]:
        self._raise_if_unavailable()
        self._require_runtime()
        return self._yield_ticks()

    async def next_materialized_frame(
        self,
        *,
        underlying: str | None = None,
        scheduled_for: datetime | None = None,
        consumer_interval_ms: int | None = None,
        window_each_side: int | None = None,
    ) -> MaterializedOptionChainFrame:
        """Return the next downsampled producer frame for one underlying."""

        del scheduled_for  # Producer scheduling/provenance remains authoritative.
        self._raise_if_unavailable()
        bootstrap = self._require_bootstrap()
        requested_interval = (
            self._consumer_interval_ms
            if consumer_interval_ms is None
            else consumer_interval_ms
        )
        if requested_interval != self._consumer_interval_ms:
            raise ValueError(
                "consumer_interval_ms is fixed when the NATS feed handler "
                "is constructed"
            )
        requested_window = (
            bootstrap.option_window_each_side
            if window_each_side is None
            else window_each_side
        )
        if requested_window < 0:
            raise ValueError("window_each_side cannot be negative")
        if requested_window > bootstrap.option_window_each_side:
            raise ValueError(
                "requested option window exceeds the feed-handler bootstrap "
                f"window ({requested_window} > "
                f"{bootstrap.option_window_each_side})"
            )
        key = self._resolve_underlying(underlying)
        queue = self._frame_queues[key]
        queued = await self._next_or_fatal(queue)
        if not isinstance(queued, _QueuedFrame):  # pragma: no cover
            raise TypeError("frame queue contained an invalid item")
        self._assert_queue_item_fresh(
            published_at=queued.frame.published_at,
            enqueued_monotonic=queued.enqueued_monotonic,
            maximum_lag_seconds=self._max_frame_lag_seconds,
            label="materialized frame",
        )
        await self._wait_for_tick_ack(queued.required_tick_sequence)
        self._assert_queue_item_fresh(
            published_at=queued.frame.published_at,
            enqueued_monotonic=queued.enqueued_monotonic,
            maximum_lag_seconds=self._max_frame_lag_seconds,
            label="materialized frame",
        )
        frame = queued.frame
        if requested_window < frame.window_each_side:
            frame = _narrow_frame(frame, requested_window)
        return frame

    async def subscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        del tokens
        raise MarketDataTransportError(
            "Remote market-data subscribers cannot change broker subscriptions"
        )

    async def unsubscribe(self, tokens: Iterable[InstrumentToken]) -> None:
        del tokens
        raise MarketDataTransportError(
            "Remote market-data subscribers cannot change broker subscriptions"
        )

    async def refresh_option_quotes(
        self,
        *,
        state: OptionChainState,
        contracts: tuple[OptionContract, ...],
    ) -> dict[str, object]:
        del state, contracts
        raise MarketDataTransportError(
            "Remote market-data subscribers cannot call broker quote refresh"
        )

    async def refresh_option_greeks(
        self,
        *,
        underlying: str,
        expiry: date,
        contracts: tuple[OptionContract, ...],
    ) -> tuple[dict[str, GreeksSnapshot], dict[str, object]]:
        del underlying, expiry, contracts
        raise MarketDataTransportError(
            "Remote market-data subscribers cannot call broker Greeks refresh"
        )

    def health_snapshot(self) -> dict[str, object]:
        frame_depth = sum(queue.qsize() for queue in self._frame_queues.values())
        tick_depth = self._ticks.qsize()
        backlog_depth = max(
            self._inbound.qsize(),
            tick_depth,
            frame_depth,
        )
        pressure_threshold = max(1, int(self._inbound.maxsize * 0.80))
        status = (
            "FAILED"
            if self._fatal_error is not None
            else "PRESSURE"
            if backlog_depth >= pressure_threshold
            else "HEALTHY"
        )
        return {
            "status": status,
            "reason": (
                "market_data_transport_fatal"
                if self._fatal_error is not None
                else "market_data_subscriber_backlog"
                if status == "PRESSURE"
                else None
            ),
            "queue_depth": self._inbound.qsize(),
            "queue_capacity": self._inbound.maxsize,
            "queue_high_watermark": self._inbound_high_watermark,
            "received_events": self._received,
            "enqueued_events": self._decoded,
            "dropped_events": self._dropped,
            "tick_queue_depth": tick_depth,
            "frame_queue_depth": frame_depth,
            "last_error": _render_error(self._fatal_error),
            "handler_epoch": self._handler_epoch,
            "last_event_id": self._last_event_id,
        }

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        cleanup_error: BaseException | None = None
        task = self._dispatcher_task
        if task is not None:
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            if results and isinstance(results[0], BaseException) and not isinstance(
                results[0], asyncio.CancelledError
            ):
                cleanup_error = results[0]
        subscription = self._event_subscription
        self._event_subscription = None
        if subscription is not None:
            try:
                await subscription.unsubscribe()
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        try:
            await self._drain_client()
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        finally:
            self._dispatcher_task = None
            self._client = None
            self._closed = True
        if cleanup_error is not None and self._fatal_error is None:
            raise MarketDataTransportError(
                "Failed to close NATS market-data subscriber"
            ) from cleanup_error

    async def _yield_ticks(self) -> AsyncIterator[MarketTick]:
        while True:
            queued = await self._next_or_fatal(self._ticks)
            if not isinstance(queued, _QueuedTick):  # pragma: no cover
                raise TypeError("tick queue contained an invalid item")
            self._assert_queue_item_fresh(
                published_at=queued.published_at,
                enqueued_monotonic=queued.enqueued_monotonic,
                maximum_lag_seconds=self._max_tick_lag_seconds,
                label="raw tick",
            )
            try:
                yield queued.tick
            finally:
                # The generator resumes only after the worker has completed
                # the body for this tick. A later frame cannot overtake the
                # microstructure/state work for earlier bus events.
                self._acknowledge_tick(queued.sequence)

    async def _run_dispatcher(self) -> None:
        try:
            while True:
                payload = await self._inbound.get()
                try:
                    event = decode_market_data_event(
                        payload,
                        maximum_bytes=self._maximum_payload_bytes,
                    )
                    self._enforce_event_epoch(event)
                    self._last_event_id = event.event_id
                    self._decoded += 1
                    if isinstance(event, RawMarketTickEvent):
                        self._assert_published_at_fresh(
                            event.published_at,
                            self._max_tick_lag_seconds,
                            "raw tick",
                        )
                        self._dispatched_tick_sequence += 1
                        self._put_subscriber_item(
                            self._ticks,
                            _QueuedTick(
                                tick=event.tick,
                                published_at=event.published_at,
                                enqueued_monotonic=(
                                    asyncio.get_running_loop().time()
                                ),
                                sequence=self._dispatched_tick_sequence,
                            ),
                            "tick",
                        )
                    elif isinstance(event, MaterializedOptionChainFrame):
                        self._dispatch_frame(
                            event,
                            required_tick_sequence=(
                                self._dispatched_tick_sequence
                            ),
                        )
                    elif isinstance(event, FeedStatusEvent):
                        self._handle_status(event)
                finally:
                    self._inbound.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_fatal(exc)

    async def _on_event_message(self, message: _NatsMessage) -> None:
        """NATS callback: bounded bytes admission only, never decode or block."""

        if self._closing or self._fatal_error is not None:
            return
        payload = getattr(message, "data", b"")
        if not isinstance(payload, bytes):
            try:
                payload = bytes(payload)
            except Exception as exc:
                self._mark_fatal(exc)
                return
        try:
            self._inbound.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS subscriber queue overflow; market-data continuity "
                    "cannot be guaranteed"
                )
            )
            return
        self._received += 1
        self._inbound_high_watermark = max(
            self._inbound_high_watermark,
            self._inbound.qsize(),
        )

    def _dispatch_frame(
        self,
        frame: MaterializedOptionChainFrame,
        *,
        required_tick_sequence: int | None = None,
    ) -> None:
        bootstrap = self._require_bootstrap()
        if frame.source_interval_ms != bootstrap.source_interval_ms:
            raise MarketDataTransportFatalError(
                "Materialized frame source interval changed during the session"
            )
        self._assert_published_at_fresh(
            frame.published_at,
            self._max_frame_lag_seconds,
            "materialized frame",
        )
        feed_status = frame.feed_health.status.upper()
        if feed_status in {"DATA_LOSS", "FAILED"}:
            raise MarketDataTransportFatalError(
                "Feed handler reported fatal frame health: "
                f"{feed_status}:{frame.feed_health.reason}"
            )
        underlying = frame.snapshot.underlying.upper()
        queue = self._frame_queues.get(underlying)
        if queue is None:
            raise MarketDataTransportFatalError(
                f"Received frame for unknown underlying {underlying}"
            )
        if not self._frame_is_due(frame):
            return
        self._put_subscriber_item(
            queue,
            _QueuedFrame(
                frame=frame,
                enqueued_monotonic=asyncio.get_running_loop().time(),
                required_tick_sequence=(
                    self._dispatched_tick_sequence
                    if required_tick_sequence is None
                    else required_tick_sequence
                ),
            ),
            f"{underlying} frame",
        )
        self._last_accepted_frame_at[underlying] = frame.scheduled_for

    def _frame_is_due(self, frame: MaterializedOptionChainFrame) -> bool:
        underlying = frame.snapshot.underlying.upper()
        previous = self._last_accepted_frame_at.get(underlying)
        if previous is None:
            return True
        if frame.scheduled_for <= previous:
            raise MarketDataTransportFatalError(
                f"Out-of-order materialized frame for {underlying}"
            )
        elapsed_ms = (frame.scheduled_for - previous).total_seconds() * 1000
        return elapsed_ms + 0.001 >= self._consumer_interval_ms

    def _handle_status(self, event: FeedStatusEvent) -> None:
        status = event.status.upper()
        if status in _NON_FATAL_FEED_STATUSES:
            return
        if status in _FATAL_FEED_STATUSES:
            raise MarketDataTransportFatalError(
                f"Feed handler reported {status}: {event.reason}"
            )
        raise MarketDataTransportFatalError(
            f"Feed handler reported unknown status {status!r}"
        )

    def _enforce_event_epoch(self, event: MarketDataEvent) -> None:
        expected = self._handler_epoch
        if expected is None or event.handler_epoch != expected:
            raise MarketDataTransportFatalError(
                "Market-data handler epoch changed; subscriber state must be "
                "reset by watchdog restart"
            )
        if event.schema_version != MARKET_DATA_SCHEMA_VERSION:
            raise MarketDataTransportFatalError(
                "Market-data event schema changed during the session"
            )

    async def _request_bootstrap_with_retry(self) -> MarketDataBootstrap:
        client = self._client
        if client is None:
            raise MarketDataTransportError("NATS subscriber is not connected")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._bootstrap_timeout_seconds
        last_error: BaseException | None = None
        while True:
            self._raise_if_unavailable()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise MarketDataTransportError(
                    "Timed out waiting for market-data bootstrap"
                ) from last_error
            try:
                response = await client.request(
                    self.subjects.bootstrap,
                    b"bootstrap",
                    timeout=min(0.75, remaining),
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last_error = exc
                await asyncio.sleep(min(0.10, max(0.0, remaining)))
                continue
            bootstrap = decode_market_data_bootstrap(
                response.data,
                maximum_bytes=self._maximum_payload_bytes,
            )
            _validate_bootstrap(bootstrap)
            return bootstrap

    async def _next_or_fatal(self, queue: asyncio.Queue[Any]) -> Any:
        self._raise_if_unavailable()
        fatal_event = self._fatal_event
        if fatal_event is None:
            raise MarketDataTransportError("NATS subscriber is not prepared")
        get_task = asyncio.create_task(queue.get())
        fatal_task = asyncio.create_task(fatal_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (get_task, fatal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if fatal_task in done and fatal_event.is_set():
                if not get_task.done():
                    get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                self._raise_if_unavailable()
            return await get_task
        finally:
            if not get_task.done():
                get_task.cancel()
            if not fatal_task.done():
                fatal_task.cancel()
            await asyncio.gather(
                get_task,
                fatal_task,
                return_exceptions=True,
            )

    def _put_subscriber_item(
        self,
        queue: asyncio.Queue[Any],
        item: Any,
        label: str,
    ) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise MarketDataTransportFatalError(
                f"NATS subscriber {label} queue overflow"
            ) from exc

    def _acknowledge_tick(self, sequence: int) -> None:
        if sequence <= self._acknowledged_tick_sequence:
            return
        self._acknowledged_tick_sequence = sequence
        completed = self._tick_ack_event
        self._tick_ack_event = asyncio.Event()
        completed.set()

    async def _wait_for_tick_ack(self, required_sequence: int) -> None:
        while self._acknowledged_tick_sequence < required_sequence:
            self._raise_if_unavailable()
            acknowledgement = self._tick_ack_event
            fatal_event = self._fatal_event
            if fatal_event is None:
                raise MarketDataTransportError(
                    "NATS subscriber is not prepared"
                )
            ack_task = asyncio.create_task(acknowledgement.wait())
            fatal_task = asyncio.create_task(fatal_event.wait())
            try:
                done, _pending = await asyncio.wait(
                    (ack_task, fatal_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if fatal_task in done and fatal_event.is_set():
                    self._raise_if_unavailable()
            finally:
                for task in (ack_task, fatal_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    ack_task,
                    fatal_task,
                    return_exceptions=True,
                )

    def _assert_queue_item_fresh(
        self,
        *,
        published_at: datetime,
        enqueued_monotonic: float,
        maximum_lag_seconds: float,
        label: str,
    ) -> None:
        self._assert_published_at_fresh(
            published_at,
            maximum_lag_seconds,
            label,
        )
        residence = asyncio.get_running_loop().time() - enqueued_monotonic
        if residence > maximum_lag_seconds:
            error = MarketDataTransportFatalError(
                f"NATS subscriber {label} queue backlog is stale "
                f"({residence:.3f}s > {maximum_lag_seconds:.3f}s)"
            )
            self._mark_fatal(error)
            raise error

    def _assert_published_at_fresh(
        self,
        published_at: datetime,
        maximum_lag_seconds: float,
        label: str,
    ) -> None:
        publication_age = (datetime.now(UTC) - published_at).total_seconds()
        if publication_age < -_MAX_EVENT_CLOCK_LEAD_SECONDS:
            error = MarketDataTransportFatalError(
                f"NATS subscriber {label} publication timestamp is in the "
                f"future ({-publication_age:.3f}s lead)"
            )
            self._mark_fatal(error)
            raise error
        if publication_age > maximum_lag_seconds:
            error = MarketDataTransportFatalError(
                f"NATS subscriber {label} publication is stale "
                f"({publication_age:.3f}s > {maximum_lag_seconds:.3f}s)"
            )
            self._mark_fatal(error)
            raise error

    def _resolve_underlying(self, underlying: str | None) -> str:
        if underlying is not None:
            key = underlying.strip().upper()
            if key not in self._frame_queues:
                raise ValueError(f"Unknown market-data underlying: {underlying}")
            return key
        if len(self._frame_queues) != 1:
            raise ValueError(
                "underlying is required when bootstrap contains multiple markets"
            )
        return next(iter(self._frame_queues))

    async def _on_nats_error(self, error: BaseException) -> None:
        if not self._closing:
            self._mark_fatal(error)

    async def _on_disconnected(self) -> None:
        if not self._closing:
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS subscriber disconnected; possible data loss"
                )
            )

    async def _on_closed(self) -> None:
        if not self._closing:
            self._mark_fatal(
                MarketDataTransportFatalError(
                    "NATS subscriber connection closed unexpectedly"
                )
            )

    async def _drain_client(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            await asyncio.wait_for(
                client.drain(),
                timeout=self._drain_timeout_seconds,
            )
        except BaseException:
            await self._close_client_safely()
            raise

    async def _close_client_safely(self) -> None:
        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            try:
                value = close()
                if isawaitable(value):
                    await value
            except BaseException:
                pass

    def _require_bootstrap(self) -> MarketDataBootstrap:
        if self._bootstrap is None:
            raise MarketDataTransportError("Call prepare before using bootstrap")
        return self._bootstrap

    def _require_runtime(self) -> FeedHandlerRuntime:
        if self._runtime is None:
            raise MarketDataTransportError("Call prepare before using feed handler")
        return self._runtime

    def _raise_if_unavailable(self) -> None:
        if self._closed or self._closing:
            raise MarketDataTransportError("NATS market-data subscriber is closed")
        if self._fatal_error is not None:
            raise MarketDataTransportFatalError(
                "NATS market-data subscriber failed; watchdog restart required"
            ) from self._fatal_error

    def _mark_fatal(self, error: BaseException) -> None:
        if self._fatal_error is None:
            self._fatal_error = error
            if self._fatal_event is not None:
                self._fatal_event.set()


async def _connect_nats_client(
    *,
    nats_url: str,
    name: str,
    connect_timeout_seconds: float,
    startup_timeout_seconds: float,
    client_factory: _NatsClientFactory | None,
    error_cb: Callable[[BaseException], Awaitable[None]],
    disconnected_cb: Callable[[], Awaitable[None]],
    closed_cb: Callable[[], Awaitable[None]],
) -> _NatsClient:
    """Retry only the initial connection, then permanently disable reconnect.

    Watchdog commonly starts the feed and strategy processes together, so the
    NATS server may not yet be accepting connections.  Once a connection has
    succeeded, any disconnect is fatal because Core NATS cannot prove that no
    market-data event was missed while reconnecting.
    """

    deadline = asyncio.get_running_loop().time() + _positive_seconds(
        startup_timeout_seconds,
        "startup_timeout_seconds",
    )
    last_error: BaseException | None = None
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise MarketDataTransportError(
                f"Timed out connecting {name} to Core NATS"
            ) from last_error
        attempt_timeout = min(connect_timeout_seconds, remaining, 1.0)
        established = False

        async def guarded_error_cb(error: BaseException) -> None:
            if established:
                await error_cb(error)

        async def guarded_disconnected_cb() -> None:
            if established:
                await disconnected_cb()

        async def guarded_closed_cb() -> None:
            if established:
                await closed_cb()

        options = {
            "servers": [nats_url],
            "name": name,
            "allow_reconnect": False,
            "max_reconnect_attempts": 0,
            "connect_timeout": attempt_timeout,
            "error_cb": guarded_error_cb,
            "disconnected_cb": guarded_disconnected_cb,
            "closed_cb": guarded_closed_cb,
        }
        try:
            if client_factory is None:
                try:
                    import nats
                except ImportError as exc:  # pragma: no cover - dependency
                    raise RuntimeError(
                        "Core-NATS transport requires nats-py from "
                        "requirements.txt"
                    ) from exc
                value = nats.connect(**options)
            else:
                value = client_factory(**options)
            if isawaitable(value):
                value = await asyncio.wait_for(value, timeout=attempt_timeout)
            established = True
            return value
        except asyncio.CancelledError:
            raise
        except (ImportError, RuntimeError) as exc:
            if isinstance(exc, RuntimeError) and "requires nats-py" in str(exc):
                raise
            last_error = exc
        except BaseException as exc:
            last_error = exc
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(min(0.10, remaining))


def _validate_consumer_interval(
    *,
    source_interval_ms: int,
    consumer_interval_ms: int,
) -> None:
    if source_interval_ms <= 0:
        raise MarketDataTransportFatalError(
            "Bootstrap source interval must be greater than zero"
        )
    if consumer_interval_ms < source_interval_ms:
        raise MarketDataTransportError(
            "consumer interval cannot be faster than the materialized feed "
            f"({consumer_interval_ms}ms < {source_interval_ms}ms)"
        )
    if consumer_interval_ms % source_interval_ms:
        raise MarketDataTransportError(
            "consumer interval must be an exact multiple of the materialized "
            f"feed interval ({consumer_interval_ms}ms vs {source_interval_ms}ms)"
        )


def _narrow_frame(
    frame: MaterializedOptionChainFrame,
    each_side: int,
) -> MaterializedOptionChainFrame:
    snapshot = frame.snapshot
    strikes = sorted({quote.contract.strike for quote in snapshot.quotes})
    if snapshot.atm_strike not in strikes:
        raise MarketDataTransportFatalError(
            "Materialized frame does not contain its ATM strike"
        )
    atm_index = strikes.index(snapshot.atm_strike)
    lower = max(0, atm_index - each_side)
    upper = min(len(strikes), atm_index + each_side + 1)
    selected_strikes = frozenset(strikes[lower:upper])
    quotes = tuple(
        quote
        for quote in snapshot.quotes
        if quote.contract.strike in selected_strikes
    )
    selected_tokens = frozenset(
        quote.contract.token.token for quote in quotes
    )
    narrowed_snapshot = replace(snapshot, quotes=quotes)
    return replace(
        frame,
        snapshot=narrowed_snapshot,
        window_each_side=each_side,
        quote_refresh=_narrow_refresh(frame.quote_refresh, selected_tokens),
        greeks_refresh=_narrow_refresh(frame.greeks_refresh, selected_tokens),
    )


def _narrow_refresh(
    provenance: RefreshProvenance,
    selected_tokens: frozenset[str],
) -> RefreshProvenance:
    exchange_tokens = tuple(
        (
            exchange,
            tuple(token for token in tokens if token in selected_tokens),
        )
        for exchange, tokens in provenance.exchange_tokens
    )
    return replace(
        provenance,
        normalized_tokens=tuple(
            token
            for token in provenance.normalized_tokens
            if token in selected_tokens
        ),
        exchange_tokens=tuple(
            (exchange, tokens)
            for exchange, tokens in exchange_tokens
            if tokens
        ),
    )


def _validate_bootstrap(value: MarketDataBootstrap) -> None:
    if value.schema_version != MARKET_DATA_SCHEMA_VERSION:
        raise ValueError(
            "Unsupported market-data bootstrap schema_version: "
            f"{value.schema_version}"
        )
    if not value.handler_epoch.strip():
        raise ValueError("bootstrap handler_epoch cannot be empty")
    if value.source_interval_ms <= 0:
        raise ValueError("bootstrap source_interval_ms must be positive")
    if value.option_window_each_side < 0:
        raise ValueError("bootstrap option_window_each_side cannot be negative")


def _deduplicate_tokens(
    values: Iterable[InstrumentToken],
) -> tuple[InstrumentToken, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[InstrumentToken] = []
    for token in values:
        identity = (token.exchange.value, token.token)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(token)
    return tuple(result)


def _normalize_subject_prefix(value: str) -> str:
    prefix = _require_text(value, "subject_prefix").strip(".")
    tokens = prefix.split(".")
    if not prefix or any(
        not token
        or any(character.isspace() for character in token)
        or "*" in token
        or ">" in token
        for token in tokens
    ):
        raise ValueError(f"Invalid NATS subject prefix: {value!r}")
    return prefix


def _require_text(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def _positive_seconds(value: float, name: str) -> float:
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return normalized


def _render_error(error: BaseException | None) -> str | None:
    return (
        None
        if error is None
        else f"{type(error).__name__}: {error}"
    )
