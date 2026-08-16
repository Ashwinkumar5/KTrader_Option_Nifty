from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from app.domain.models import OptionContract
from app.marketdata.events import (
    FeedHealthSnapshot,
    MaterializedOptionChainFrame,
    RefreshProvenance,
)
from app.marketdata.feed_handler import MarketDataFeedHandler
from app.optionchain.state import OptionChainState


async def materialize_option_chain_frame(
    *,
    feed_handler: MarketDataFeedHandler,
    state: OptionChainState,
    underlying: str,
    expiry: date,
    fallback_spot_price: Decimal,
    contracts: tuple[OptionContract, ...],
    option_window_each_side: int,
    option_greeks_enabled: bool,
    source_interval_ms: int,
    scheduled_for: datetime,
    frame_started_at: datetime,
    trigger_tick_received_at: datetime,
    spot_observed_at: datetime | None,
    spot_price_provider: Callable[[], Decimal | None] | None = None,
    spot_observed_at_provider: Callable[[], datetime | None] | None = None,
    feed_health_provider: Callable[[], dict[str, object]] | None = None,
    handler_epoch: str = "embedded",
    event_id: str | None = None,
) -> MaterializedOptionChainFrame:
    """Build one atomic broker-enriched frame before strategy evaluation.

    This is the acquisition/decision boundary shared by the embedded worker and
    the Phase-2 publisher. Broker REST, canonical state mutation and snapshot
    construction all finish before the immutable frame is returned.
    """

    quote_refresh = await feed_handler.refresh_option_quotes(
        state=state,
        contracts=contracts,
    )

    greeks_refresh: dict[str, object] = {
        "status": "disabled",
        "requested_at": None,
        "responded_at": None,
        "attempts": 0,
        "row_count": 0,
        "normalized_tokens": (),
    }
    if option_greeks_enabled:
        current_greeks, greeks_refresh = (
            await feed_handler.refresh_option_greeks(
                underlying=underlying,
                expiry=expiry,
                contracts=contracts,
            )
        )
        if current_greeks:
            state.update_greeks(current_greeks)

    snapshot_captured_at = datetime.now(UTC)
    current_spot_price = (
        spot_price_provider()
        if spot_price_provider is not None
        else None
    ) or fallback_spot_price
    current_spot_observed_at = (
        spot_observed_at_provider()
        if spot_observed_at_provider is not None
        else None
    ) or spot_observed_at
    market = state.build_underlying_market_snapshot(
        underlying=underlying,
        captured_at=snapshot_captured_at,
    )
    snapshot = state.build_snapshot(
        underlying=underlying,
        expiry=expiry,
        spot_price=current_spot_price,
        each_side=option_window_each_side,
        captured_at=snapshot_captured_at,
        market=market,
    )
    feed_health = (
        feed_health_provider()
        if feed_health_provider is not None
        else None
    )
    return MaterializedOptionChainFrame(
        handler_epoch=handler_epoch,
        event_id=event_id or uuid4().hex,
        published_at=datetime.now(UTC),
        snapshot=snapshot,
        scheduled_for=scheduled_for,
        frame_started_at=frame_started_at,
        trigger_tick_received_at=trigger_tick_received_at,
        spot_observed_at=current_spot_observed_at,
        window_each_side=option_window_each_side,
        source_interval_ms=source_interval_ms,
        quote_refresh=RefreshProvenance.from_mapping(quote_refresh),
        greeks_refresh=RefreshProvenance.from_mapping(greeks_refresh),
        feed_health=FeedHealthSnapshot.from_mapping(feed_health),
    )
