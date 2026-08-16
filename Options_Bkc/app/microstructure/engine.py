from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    InstrumentKind,
    MarketTick,
    OrderBookSnapshot,
    MicrostructureFeatures,
    MicrostructureSignal,
    OptionType,
)
from app.marketdata.depth_normalizer import normalize_order_book
from app.microstructure.velocity import PremiumVelocityTracker


@dataclass(frozen=True)
class MicrostructureSettings:
    window_seconds: int
    min_events: int
    min_imbalance: Decimal
    min_velocity: Decimal
    max_spread: Decimal
    min_option_velocity_percent: Decimal | None = None
    require_directional_option_book: bool = False


@dataclass(slots=True)
class _OrderFlowState:
    """Constant-time rolling state for one instrument's best quotes."""

    previous_at: datetime
    previous_bid_price: Decimal
    previous_bid_quantity: int
    previous_ask_price: Decimal
    previous_ask_quantity: int
    observations: deque[tuple[datetime, int, int]]
    contribution_total: int = 0
    depth_total: int = 0


class _OrderFlowImbalanceTracker:
    """Depth-normalized Level-I OFI proxy from consecutive book snapshots.

    The calculation follows Cont, Kukanov and Stoikov's signed best-quote
    changes. Only one compact state and a short rolling deque are retained per
    token; updates are O(1) amortized and do not scan historical ticks.
    """

    def __init__(self, *, window_seconds: int) -> None:
        self._window = timedelta(seconds=max(1, window_seconds))
        self._states: dict[str, _OrderFlowState] = {}

    def update(
        self,
        *,
        token: str,
        captured_at: datetime,
        book: OrderBookSnapshot,
    ) -> tuple[int, int, Decimal | None]:
        bid_depth = sum(level.quantity for level in book.bids)
        ask_depth = sum(level.quantity for level in book.asks)
        best_bid = book.bids[0]
        best_ask = book.asks[0]
        state = self._states.get(token)
        if state is None or captured_at < state.previous_at:
            self._states[token] = _OrderFlowState(
                previous_at=captured_at,
                previous_bid_price=best_bid.price,
                previous_bid_quantity=best_bid.quantity,
                previous_ask_price=best_ask.price,
                previous_ask_quantity=best_ask.quantity,
                observations=deque(),
            )
            return bid_depth, ask_depth, None

        contribution = _order_flow_contribution(
            previous_bid_price=state.previous_bid_price,
            previous_bid_quantity=state.previous_bid_quantity,
            previous_ask_price=state.previous_ask_price,
            previous_ask_quantity=state.previous_ask_quantity,
            bid_price=best_bid.price,
            bid_quantity=best_bid.quantity,
            ask_price=best_ask.price,
            ask_quantity=best_ask.quantity,
        )
        depth = best_bid.quantity + best_ask.quantity
        state.observations.append((captured_at, contribution, depth))
        state.contribution_total += contribution
        state.depth_total += depth
        state.previous_at = captured_at
        state.previous_bid_price = best_bid.price
        state.previous_bid_quantity = best_bid.quantity
        state.previous_ask_price = best_ask.price
        state.previous_ask_quantity = best_ask.quantity

        cutoff = captured_at - self._window
        while state.observations and state.observations[0][0] < cutoff:
            _, expired_contribution, expired_depth = state.observations.popleft()
            state.contribution_total -= expired_contribution
            state.depth_total -= expired_depth

        observation_count = len(state.observations)
        if observation_count == 0 or state.depth_total <= 0:
            return bid_depth, ask_depth, Decimal("0")

        # Average best-quote depth is (bid + ask) / 2. Dividing rolling OFI by
        # that depth makes pressure comparable across contracts and liquidity
        # regimes while preserving the existing [-1, 1] feature contract.
        normalized = (
            Decimal(state.contribution_total)
            * Decimal(2 * observation_count)
            / Decimal(state.depth_total)
        )
        return (
            bid_depth,
            ask_depth,
            _clamp_signed_unit(normalized).quantize(Decimal("0.0001")),
        )

    def discard(self, token: str) -> None:
        self._states.pop(token, None)

    def reset(self) -> None:
        self._states.clear()


class MicrostructureEngine:
    """Produces shadow candidates only when book pressure and premium speed agree.

    The engine intentionally knows nothing about orders or positions. It measures a
    single option contract or the front NIFTY future; the signal gate decides
    whether the synchronized observations qualify a strategy candidate.
    """

    def __init__(self, settings: MicrostructureSettings) -> None:
        self._settings = settings
        self._velocity = PremiumVelocityTracker(window_seconds=settings.window_seconds)
        self._order_flow = _OrderFlowImbalanceTracker(
            window_seconds=settings.window_seconds
        )
        self._directions: dict[str, deque[tuple[datetime, str]]] = {}

    def observe(self, tick: MarketTick) -> tuple[MicrostructureFeatures | None, MicrostructureSignal | None]:
        if (
            tick.token.kind
            not in {InstrumentKind.OPTION, InstrumentKind.FUTURE}
            or tick.ltp is None
        ):
            return None, None

        velocity, event_count = self._velocity.update(
            token=tick.token.token,
            captured_at=tick.received_at,
            premium=tick.ltp,
        )
        book = normalize_order_book(tick)
        if book is None:
            self._order_flow.discard(tick.token.token)
            return (
                MicrostructureFeatures(
                    token=tick.token,
                    captured_at=tick.received_at,
                    book_imbalance=None,
                    bid_depth=0,
                    ask_depth=0,
                    spread=None,
                    premium_velocity=velocity,
                    event_count=event_count,
                    has_complete_book=False,
                ),
                None,
            )

        bid_depth, ask_depth, imbalance = self._order_flow.update(
            token=tick.token.token,
            captured_at=tick.received_at,
            book=book,
        )
        spread = book.asks[0].price - book.bids[0].price
        features = MicrostructureFeatures(
            token=tick.token,
            captured_at=tick.received_at,
            book_imbalance=imbalance,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spread=spread,
            premium_velocity=velocity,
            event_count=event_count,
            has_complete_book=True,
        )

        # OFI needs two consecutive complete books. The first book seeds the
        # baseline but must not be interpreted as directional pressure.
        if imbalance is None:
            return features, None

        is_future = tick.token.kind == InstrumentKind.FUTURE
        velocity_metric = velocity
        velocity_threshold = self._settings.min_velocity
        if (
            not is_future
            and velocity is not None
            and tick.ltp > 0
            and self._settings.min_option_velocity_percent is not None
        ):
            velocity_metric = velocity / tick.ltp * Decimal("100")
            velocity_threshold = (
                self._settings.min_option_velocity_percent
            )
        if is_future:
            if imbalance >= self._settings.min_imbalance:
                side = "BUY_CALL"
                pressure_sign = Decimal("1")
            elif imbalance <= -self._settings.min_imbalance:
                side = "BUY_PUT"
                pressure_sign = Decimal("-1")
            else:
                return features, None
        else:
            side = (
                "BUY_CALL"
                if _option_type(tick) == OptionType.CALL
                else "BUY_PUT"
            )
            # OFI is measured on the option premium itself. Positive OFI means
            # buyers are pressuring that premium, so it confirms buying for
            # both CE and PE contracts; the contract type determines the side.
            pressure_sign = Decimal("1")
        # Persistence starts as soon as the book shows directional pressure. The
        # final emission below still requires positive premium velocity, so a
        # queued-but-static book cannot create a candidate by itself.
        is_persistent = False
        require_directional_book = (
            is_future or self._settings.require_directional_option_book
        )
        book_threshold = (
            self._settings.min_imbalance
            if require_directional_book
            else -self._settings.min_imbalance
        )
        if imbalance * pressure_sign >= book_threshold:
            is_persistent = self._persistent(
                tick.token.token,
                tick.received_at,
                side,
            )
        if (
            not self._qualifies(
                features,
                pressure_sign=pressure_sign,
                require_directional_book=require_directional_book,
                velocity_metric=velocity_metric,
                velocity_threshold=velocity_threshold,
            )
            or not is_persistent
        ):
            return features, None

        velocity_scale = max(
            velocity_threshold * Decimal("4"),
            Decimal("0.01"),
        )
        velocity_confidence = min(
            Decimal("1"),
            abs(velocity_metric or Decimal("0")) / velocity_scale,
        )
        book_confidence = (
            abs(imbalance)
            if is_future
            else _clamp_unit((imbalance + Decimal("1")) / Decimal("2"))
        )
        confidence = min(
            Decimal("1"),
            book_confidence * Decimal("0.50")
            + velocity_confidence * Decimal("0.50"),
        )
        velocity_suffix = (
            ""
            if is_future
            else f"{velocity_metric:+.4f}%/s, "
        )
        return features, MicrostructureSignal(
            token=tick.token,
            underlying=tick.token.symbol,
            side=side,
            captured_at=tick.received_at,
            confidence=confidence.quantize(Decimal("0.0001")),
            reason=(
                f"{'Futures' if is_future else 'Option'} normalized OFI "
                f"{imbalance:+.4f}, price velocity {velocity:+.4f} pts/s, "
                f"{velocity_suffix}spread {spread:.2f}, confirmed across "
                f"{self._settings.min_events} events."
            ),
        )

    def reset(self) -> None:
        self._velocity.reset()
        self._order_flow.reset()
        self._directions.clear()

    def _qualifies(
        self,
        features: MicrostructureFeatures,
        *,
        pressure_sign: Decimal,
        require_directional_book: bool,
        velocity_metric: Decimal | None,
        velocity_threshold: Decimal,
    ) -> bool:
        velocity = features.premium_velocity
        imbalance = features.book_imbalance
        if (
            not features.has_complete_book
            or features.event_count < self._settings.min_events
            or velocity is None
            or velocity_metric is None
            or imbalance is None
            or features.spread is None
        ):
            return False
        return (
            imbalance * pressure_sign
            >= (
                self._settings.min_imbalance
                if require_directional_book
                else -self._settings.min_imbalance
            )
            and velocity_metric * pressure_sign >= velocity_threshold
            and features.spread <= self._settings.max_spread
        )

    def _persistent(self, token: str, captured_at: datetime, side: str) -> bool:
        history = self._directions.setdefault(token, deque())
        history.append((captured_at, side))
        cutoff = captured_at - timedelta(seconds=self._settings.window_seconds)
        while history and history[0][0] < cutoff:
            history.popleft()
        return sum(1 for _, direction in history if direction == side) >= self._settings.min_events


def _order_flow_contribution(
    *,
    previous_bid_price: Decimal,
    previous_bid_quantity: int,
    previous_ask_price: Decimal,
    previous_ask_quantity: int,
    bid_price: Decimal,
    bid_quantity: int,
    ask_price: Decimal,
    ask_quantity: int,
) -> int:
    """Return the signed best-quote event contribution defined by the paper."""

    contribution = 0
    if bid_price >= previous_bid_price:
        contribution += bid_quantity
    if bid_price <= previous_bid_price:
        contribution -= previous_bid_quantity
    if ask_price <= previous_ask_price:
        contribution -= ask_quantity
    if ask_price >= previous_ask_price:
        contribution += previous_ask_quantity
    return contribution


def _option_type(tick: MarketTick) -> OptionType:
    symbol = tick.token.trading_symbol.upper()
    return OptionType.CALL if symbol.endswith("CE") else OptionType.PUT


def _clamp_unit(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))


def _clamp_signed_unit(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("-1"), value))
