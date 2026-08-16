from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domain.models import InstrumentToken, MarketTick, OptionContract
from app.instruments.master import available_expiries
from app.marketdata.events import (
    FeedStatusEvent,
    MarketDataBootstrap,
    MarketDataEvent,
    RawMarketTickEvent,
)
from app.marketdata.feed_handler import MarketDataFeedHandler
from app.marketdata.feed_tape import MarketDataFeedTape
from app.marketdata.frame_materializer import materialize_option_chain_frame
from app.marketdata.serde import (
    encode_market_data_bootstrap,
    encode_market_data_event,
)
from app.optionchain.atm import select_option_window
from app.optionchain.state import OptionChainState


class MarketDataEventPublisher(Protocol):
    async def start(
        self,
        bootstrap: MarketDataBootstrap | Callable[[], MarketDataBootstrap],
    ) -> None: ...

    def publish_encoded(self, payload: bytes) -> bool: ...

    async def flush(self) -> None: ...

    def health_snapshot(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


class FeedTapeWriter(Protocol):
    def record_encoded(self, payload: bytes) -> bool: ...

    def health_snapshot(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


async def run_market_data_feed_service(
    *,
    settings: Settings,
    feed_handler: MarketDataFeedHandler,
    publisher: MarketDataEventPublisher,
    tape: FeedTapeWriter,
    heartbeat_file: Path | None = None,
    max_ticks: int | None = None,
) -> None:
    """Own one broker session and fan out canonical ticks and enriched frames."""

    handler_epoch = uuid4().hex
    heartbeat_task: asyncio.Task[None] | None = None
    snapshot_tasks: dict[str, asyncio.Task[None]] = {}
    started_publisher = False
    status = "completed"
    terminal_error: str | None = None
    processed = 0
    monitored_ticks = None

    def next_event_id() -> str:
        # IDs provide correlation/deduplication only. They deliberately carry
        # no ordering promise; the single NATS subject preserves publication
        # order and broker sequence tracking is outside this phase.
        return uuid4().hex

    def publish_and_record(event: MarketDataEvent) -> None:
        payload = encode_market_data_event(event)
        if not publisher.publish_encoded(payload):
            raise RuntimeError("NATS market-data publisher queue is full")
        # The stated source contract is fan-out admission first, canonical tape
        # admission second. Failure of either is fatal; the service never keeps
        # trading through a known transport or replay-data gap.
        if not tape.record_encoded(payload):
            raise RuntimeError("Canonical market-data tape queue is full")

    try:
        runtime = await feed_handler.prepare()
        master = runtime.master
        state = OptionChainState(master=master)
        market_date = datetime.now(
            ZoneInfo(settings.market_timezone)
        ).date()
        reference_status = await feed_handler.initialize_reference_data(
            state=state,
            market_date=market_date,
        )
        expiries = {
            underlying: expiries_for_underlying[0]
            for underlying in settings.default_underlyings
            for expiries_for_underlying in (
                available_expiries(master.options, underlying),
            )
            if expiries_for_underlying
        }
        if not expiries:
            raise RuntimeError("No current option expiry is available")
        bootstrap = _build_bootstrap(
            handler_epoch=handler_epoch,
            settings=settings,
            master=master,
            expiries=expiries,
            reference_status=reference_status,
        )
        reference_tokens = await feed_handler.start(market_date=market_date)
        # The bootstrap responder becomes visible only after the broker socket
        # and reference subscription are ready. A late Core-NATS subscriber can
        # therefore use a successful bootstrap response as its readiness proof
        # even if it did not observe the informational READY event.
        await publisher.start(bootstrap)
        started_publisher = True
        if not tape.record_encoded(encode_market_data_bootstrap(bootstrap)):
            raise RuntimeError("Canonical market-data tape queue is full")
        ready = FeedStatusEvent(
            handler_epoch=handler_epoch,
            event_id=next_event_id(),
            published_at=datetime.now(UTC),
            status="READY",
            reason=(
                f"initial_reference_tokens={len(reference_tokens)}"
            ),
        )
        publish_and_record(ready)
        await publisher.flush()
        if heartbeat_file is not None:
            heartbeat_task = asyncio.create_task(
                _heartbeat(
                    heartbeat_file,
                    health_provider=feed_handler.health_snapshot,
                    tape_health_provider=tape.health_snapshot,
                    publisher_health_provider=publisher.health_snapshot,
                ),
                name="market-data-feed-heartbeat",
            )
        print(
            "MKT_DATA_FEED_HANDLER_READY "
            f"epoch={handler_epoch} interval_ms="
            f"{settings.market_data_feed_interval_ms}"
        )

        spot_prices: dict[str, Decimal] = {}
        spot_observed_at: dict[str, datetime] = {}
        option_windows: dict[
            str,
            tuple[Decimal, tuple[OptionContract, ...]],
        ] = {}
        active_option_tokens: dict[str, set[str]] = {}
        last_snapshot_at: dict[str, datetime] = {}

        monitored_ticks = _monitored_ticks(
            feed_handler.ticks(),
            snapshot_tasks=snapshot_tasks,
            heartbeat_task=heartbeat_task,
        )
        async for tick in monitored_ticks:
            current_market_date = datetime.now(
                ZoneInfo(settings.market_timezone)
            ).date()
            if current_market_date != market_date:
                raise RuntimeError(
                    "Market session date changed; restart the feed handler "
                    "to refresh expiry, bootstrap and tape ownership"
                )
            publish_and_record(
                RawMarketTickEvent(
                    handler_epoch=handler_epoch,
                    event_id=next_event_id(),
                    published_at=datetime.now(UTC),
                    tick=tick,
                )
            )
            state.update_tick(tick)
            spot_underlying = _update_spot(
                tick=tick,
                spot_tokens=master.spot_tokens,
                spot_prices=spot_prices,
                spot_observed_at=spot_observed_at,
            )

            for underlying, spot_price in tuple(spot_prices.items()):
                expiry = expiries.get(underlying)
                if expiry is None:
                    continue
                cached_window = option_windows.get(underlying)
                if cached_window is None or spot_underlying == underlying:
                    atm, selected = select_option_window(
                        master=master,
                        underlying=underlying,
                        expiry=expiry,
                        spot_price=spot_price,
                        each_side=settings.option_window_each_side,
                    )
                    if cached_window is None or cached_window[0] != atm:
                        contracts = tuple(selected)
                        await _rotate_subscriptions(
                            feed_handler=feed_handler,
                            token_lookup=runtime.token_lookup,
                            active_tokens=active_option_tokens,
                            underlying=underlying,
                            contracts=contracts,
                        )
                        option_windows[underlying] = (atm, contracts)
                        cached_window = option_windows[underlying]
                if cached_window is None:
                    continue
                _atm, contracts = cached_window

                now = datetime.now(UTC)
                previous_snapshot = last_snapshot_at.get(underlying)
                running = snapshot_tasks.get(underlying)
                due = (
                    previous_snapshot is None
                    or (
                        (now - previous_snapshot).total_seconds() * 1000
                        >= settings.market_data_feed_interval_ms
                    )
                )
                if due and running is None:
                    scheduled_for = (
                        now
                        if previous_snapshot is None
                        else previous_snapshot
                        + timedelta(
                            milliseconds=(
                                settings.market_data_feed_interval_ms
                            )
                        )
                    )
                    last_snapshot_at[underlying] = now
                    snapshot_tasks[underlying] = asyncio.create_task(
                        _materialize_and_publish(
                            feed_handler=feed_handler,
                            state=state,
                            underlying=underlying,
                            expiry=expiry,
                            fallback_spot_price=spot_price,
                            contracts=contracts,
                            settings=settings,
                            handler_epoch=handler_epoch,
                            event_id=next_event_id(),
                            scheduled_for=scheduled_for,
                            frame_started_at=now,
                            trigger_tick_received_at=tick.received_at,
                            spot_price_provider=(
                                lambda key=underlying: spot_prices.get(key)
                            ),
                            spot_observed_at_provider=(
                                lambda key=underlying: (
                                    spot_observed_at.get(key)
                                )
                            ),
                            publish_and_record=publish_and_record,
                        ),
                        name=f"materialize-{underlying}",
                    )

            processed += 1
            if max_ticks is not None and processed >= max_ticks:
                break
            await asyncio.sleep(0)

        await monitored_ticks.aclose()
        await _settle_frame_tasks(snapshot_tasks, cancel=False)
    except asyncio.CancelledError:
        status = "cancelled"
        terminal_error = "CancelledError"
        raise
    except BaseException as exc:
        status = "failed"
        terminal_error = type(exc).__name__
        if started_publisher and not isinstance(exc, asyncio.CancelledError):
            try:
                publish_and_record(
                    FeedStatusEvent(
                        handler_epoch=handler_epoch,
                        event_id=next_event_id(),
                        published_at=datetime.now(UTC),
                        status="FAILED",
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
                await publisher.flush()
            except Exception:
                pass
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if monitored_ticks is not None:
            try:
                await monitored_ticks.aclose()
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            await _settle_frame_tasks(snapshot_tasks, cancel=True)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        for close in (tape.close, publisher.close, feed_handler.close):
            try:
                await close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors and status == "completed":
            raise RuntimeError(
                "Market-data feed service cleanup failed: "
                f"{type(cleanup_errors[0]).__name__}"
            ) from cleanup_errors[0]
        if terminal_error:
            print(
                "MKT_DATA_FEED_HANDLER_STOPPED "
                f"status={status} error={terminal_error}"
            )


async def _materialize_and_publish(
    *,
    feed_handler: MarketDataFeedHandler,
    state: OptionChainState,
    underlying: str,
    expiry,
    fallback_spot_price: Decimal,
    contracts: tuple[OptionContract, ...],
    settings: Settings,
    handler_epoch: str,
    event_id: str,
    scheduled_for: datetime,
    frame_started_at: datetime,
    trigger_tick_received_at: datetime,
    spot_price_provider: Callable[[], Decimal | None],
    spot_observed_at_provider: Callable[[], datetime | None],
    publish_and_record: Callable[[MarketDataEvent], None],
) -> None:
    frame = await materialize_option_chain_frame(
        feed_handler=feed_handler,
        state=state,
        underlying=underlying,
        expiry=expiry,
        fallback_spot_price=fallback_spot_price,
        contracts=contracts,
        option_window_each_side=settings.option_window_each_side,
        option_greeks_enabled=settings.option_greeks_enabled,
        source_interval_ms=settings.market_data_feed_interval_ms,
        scheduled_for=scheduled_for,
        frame_started_at=frame_started_at,
        trigger_tick_received_at=trigger_tick_received_at,
        spot_observed_at=spot_observed_at_provider(),
        spot_price_provider=spot_price_provider,
        spot_observed_at_provider=spot_observed_at_provider,
        feed_health_provider=feed_handler.health_snapshot,
        handler_epoch=handler_epoch,
        event_id=event_id,
    )
    publish_and_record(frame)


def _build_bootstrap(
    *,
    handler_epoch: str,
    settings: Settings,
    master,
    expiries: dict[str, date],
    reference_status: dict[str, object],
) -> MarketDataBootstrap:
    relevant_options = tuple(
        contract
        for contract in master.options
        if contract.underlying in expiries
        and contract.expiry == expiries[contract.underlying]
    )
    india_vix = reference_status.get("india_vix")
    reference_values: list[tuple[str, Decimal]] = []
    if isinstance(india_vix, dict) and india_vix.get("value") is not None:
        reference_values.append(
            ("INDIA_VIX", Decimal(str(india_vix["value"])))
        )
    atr_values: list[tuple[str, Decimal]] = []
    raw_atr = reference_status.get("previous_20d_atr")
    if isinstance(raw_atr, dict):
        for underlying, detail in sorted(raw_atr.items()):
            if isinstance(detail, dict) and detail.get("value") is not None:
                atr_values.append(
                    (str(underlying), Decimal(str(detail["value"])))
                )
    return MarketDataBootstrap(
        handler_epoch=handler_epoch,
        generated_at=datetime.now(UTC),
        source_interval_ms=settings.market_data_feed_interval_ms,
        option_window_each_side=settings.option_window_each_side,
        selected_expiries=tuple(
            (underlying, expiry)
            for underlying, expiry in sorted(expiries.items())
        ),
        spot_tokens=tuple(master.spot_tokens.values()),
        option_contracts=relevant_options,
        future_contracts=master.futures,
        reference_tokens=tuple(master.reference_tokens.values()),
        reference_values=tuple(reference_values),
        previous_20d_atr=tuple(atr_values),
    )


def _update_spot(
    *,
    tick: MarketTick,
    spot_tokens: dict[str, InstrumentToken],
    spot_prices: dict[str, Decimal],
    spot_observed_at: dict[str, datetime],
) -> str | None:
    if tick.ltp is None:
        return None
    for underlying, token in spot_tokens.items():
        if tick.token.token == token.token:
            spot_prices[underlying] = tick.ltp
            spot_observed_at[underlying] = tick.received_at
            return underlying
    return None


async def _rotate_subscriptions(
    *,
    feed_handler: MarketDataFeedHandler,
    token_lookup,
    active_tokens: dict[str, set[str]],
    underlying: str,
    contracts: tuple[OptionContract, ...],
) -> None:
    selected = {contract.token.token for contract in contracts}
    previous = active_tokens.get(underlying, set())
    additions = tuple(
        contract.token
        for contract in contracts
        if contract.token.token not in previous
    )
    stale = tuple(
        token_lookup[token]
        for token in sorted(previous - selected)
        if token in token_lookup
    )
    # Add before remove so an ATM rotation never creates a subscription gap.
    if additions:
        await feed_handler.subscribe(additions)
    if stale:
        await feed_handler.unsubscribe(stale)
    active_tokens[underlying] = selected


async def _heartbeat(
    path: Path,
    *,
    health_provider: Callable[[], dict[str, object]],
    tape_health_provider: Callable[[], dict[str, object]],
    publisher_health_provider: Callable[[], dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        feed_health = health_provider()
        tape_health = tape_health_provider()
        publisher_health = publisher_health_provider()
        feed_status = str(feed_health.get("status") or "").upper()
        tape_status = str(tape_health.get("status") or "").upper()
        publisher_status = str(
            publisher_health.get("status") or ""
        ).upper()
        if feed_status not in {"HEALTHY", "CONNECTED"}:
            raise RuntimeError(
                f"Market-data heartbeat feed status is {feed_status or 'UNKNOWN'}"
            )
        if tape_status != "HEALTHY":
            raise RuntimeError(
                f"Market-data heartbeat tape status is {tape_status or 'UNKNOWN'}"
            )
        if publisher_status != "HEALTHY":
            raise RuntimeError(
                "Market-data heartbeat publisher status is "
                f"{publisher_status or 'UNKNOWN'}"
            )
        await asyncio.to_thread(path.touch)
        await asyncio.sleep(2)


async def _monitored_ticks(
    ticks,
    *,
    snapshot_tasks: dict[str, asyncio.Task[None]],
    heartbeat_task: asyncio.Task[None] | None,
):
    """Yield ticks while surfacing background failures without another tick."""

    iterator = ticks.__aiter__()
    next_tick = asyncio.create_task(
        anext(iterator),
        name="market-data-feed-next-tick",
    )
    try:
        while True:
            watched: set[asyncio.Task] = {next_tick}
            watched.update(snapshot_tasks.values())
            if heartbeat_task is not None:
                watched.add(heartbeat_task)
            done, _pending = await asyncio.wait(
                watched,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task is not None and heartbeat_task in done:
                heartbeat_task.result()
                raise RuntimeError(
                    "Market-data heartbeat stopped unexpectedly"
                )
            for underlying, task in tuple(snapshot_tasks.items()):
                if task in done:
                    del snapshot_tasks[underlying]
                    task.result()
            if next_tick not in done:
                continue
            try:
                tick = next_tick.result()
            except StopAsyncIteration:
                return
            next_tick = asyncio.create_task(
                anext(iterator),
                name="market-data-feed-next-tick",
            )
            yield tick
    finally:
        if not next_tick.done():
            next_tick.cancel()
        await asyncio.gather(next_tick, return_exceptions=True)
        close_iterator = getattr(iterator, "aclose", None)
        if callable(close_iterator):
            await close_iterator()


def _raise_completed_frame_errors(
    tasks: dict[str, asyncio.Task[None]],
) -> None:
    for underlying, task in tuple(tasks.items()):
        if task.done():
            del tasks[underlying]
            task.result()


async def _settle_frame_tasks(
    tasks: dict[str, asyncio.Task[None]],
    *,
    cancel: bool,
) -> None:
    pending = tuple(tasks.values())
    tasks.clear()
    if cancel:
        for task in pending:
            if not task.done():
                task.cancel()
    if not pending:
        return
    results = await asyncio.gather(*pending, return_exceptions=True)
    if not cancel:
        for result in results:
            if isinstance(result, BaseException):
                raise result
