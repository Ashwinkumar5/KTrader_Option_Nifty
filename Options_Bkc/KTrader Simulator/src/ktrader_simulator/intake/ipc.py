from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

from ktrader_simulator.domain.models import OptionType

_LOGGER = logging.getLogger(__name__)
_MAX_EVENT_BYTES = 2048


@dataclass(frozen=True, slots=True)
class BotOrderSignal:
    """The complete bot-to-simulator event contract."""

    underlying: str
    strike: Decimal
    option_type: OptionType
    captured_at: datetime
    signal_id: str | None = None
    profile: str | None = None
    strategy: str | None = None


class BotSignalIpcServer:
    """Local, event-driven KTraderUI endpoint with a bounded in-memory queue."""

    def __init__(
        self,
        *,
        endpoint: str,
        host: str,
        port: int,
        queue_capacity: int,
        max_age_seconds: Decimal,
    ) -> None:
        self._endpoint = endpoint
        self._host = host
        self._requested_port = port
        self._bound_port = port
        self._max_age_seconds = max_age_seconds
        self._queue: asyncio.Queue[BotOrderSignal] = asyncio.Queue(maxsize=queue_capacity)
        self._server: asyncio.AbstractServer | None = None
        self._wake: asyncio.Event | None = None

    @property
    def port(self) -> int:
        return self._bound_port

    async def start(self, wake: asyncio.Event) -> None:
        if self._server is not None:
            return
        self._wake = wake
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._requested_port,
            limit=_MAX_EVENT_BYTES,
        )
        sockets = self._server.sockets or ()
        if sockets:
            address = sockets[0].getsockname()
            if isinstance(address, tuple):
                self._bound_port = int(address[1])

    async def close(self) -> None:
        server = self._server
        self._server = None
        self._wake = None
        if server is not None:
            server.close()
            await server.wait_closed()

    def drain(self) -> tuple[BotOrderSignal, ...]:
        signals: list[BotOrderSignal] = []
        while True:
            try:
                signals.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return tuple(signals)

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        reply = b"REJECTED\n"
        accepted = False
        try:
            line = await reader.readline()
            if line and len(line) <= _MAX_EVENT_BYTES:
                signal = _signal_from_wire(line, endpoint=self._endpoint)
                _validate_age(
                    signal,
                    now=datetime.now(UTC),
                    max_age_seconds=self._max_age_seconds,
                )
                try:
                    self._queue.put_nowait(signal)
                except asyncio.QueueFull:
                    reply = b"QUEUE_FULL\n"
                else:
                    accepted = True
                    reply = b"OK\n"
        except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
            pass
        finally:
            writer.write(reply)
            with suppress(ConnectionError, OSError):
                await writer.drain()
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
        if accepted and self._wake is not None:
            self._wake.set()


class BotSignalIpcPublisher:
    """Minimal bot live-store adapter; only qualified BUY signals are transmitted."""

    def __init__(self, *, endpoint: str, host: str, port: int) -> None:
        self._endpoint = endpoint
        self._host = host
        self._port = port

    async def publish_chain_snapshot(self, _snapshot: object) -> None:
        return

    async def publish_analytics_snapshot(self, snapshot: object) -> None:
        signal = _signal_from_bot_snapshot(snapshot)
        if signal is None:
            return
        try:
            reply = await send_buy_event(
                endpoint=self._endpoint,
                host=self._host,
                port=self._port,
                signal=signal,
            )
        except (ConnectionError, OSError, TimeoutError) as exc:
            _LOGGER.warning("KTraderUI is unavailable: %s", exc)
            return
        if reply != "OK":
            _LOGGER.warning("KTraderUI rejected bot event: %s", reply)

    async def close(self) -> None:
        return


async def send_buy_event(
    *,
    endpoint: str,
    host: str,
    port: int,
    signal: BotOrderSignal,
    timeout_seconds: float = 1.0,
) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=timeout_seconds,
    )
    try:
        payload = {
            "endpoint": endpoint,
            "action": "BUY",
            "signal_id": signal.signal_id,
            "profile": signal.profile,
            "strategy": signal.strategy,
            "underlying": signal.underlying,
            "strike": str(signal.strike),
            "side": "CALL" if signal.option_type == OptionType.CALL else "PUT",
            "captured_at": signal.captured_at.isoformat(),
        }
        writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
        return reply.decode("utf-8", errors="replace").strip()
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _signal_from_wire(line: bytes, *, endpoint: str) -> BotOrderSignal:
    raw: object = json.loads(line)
    payload = _mapping(raw)
    if str(payload.get("endpoint") or "") != endpoint:
        raise ValueError("endpoint mismatch")
    if str(payload.get("action") or "").strip().upper() != "BUY":
        raise ValueError("only BUY events are supported")
    underlying = str(payload.get("underlying") or "").strip().upper()
    if not underlying:
        raise ValueError("underlying is required")
    return BotOrderSignal(
        underlying=underlying,
        strike=_positive_decimal(payload.get("strike"), "strike"),
        option_type=_option_type(payload.get("side")),
        captured_at=_timestamp(payload.get("captured_at")),
        signal_id=_optional_text(payload.get("signal_id"), "signal_id"),
        profile=_optional_text(payload.get("profile"), "profile"),
        strategy=_optional_text(payload.get("strategy"), "strategy", uppercase=True),
    )


def _signal_from_bot_snapshot(snapshot: object) -> BotOrderSignal | None:
    signal_name = _enum_value(_field(snapshot, "signal")).upper()
    if signal_name not in {"BUY_CALL", "BUY_PUT"}:
        return None
    underlying = str(_field(snapshot, "underlying") or "").strip().upper()
    strike = _optional_decimal(_field(snapshot, "target_strike"))
    if not underlying or strike is None or strike <= 0:
        return None
    option_type = OptionType.CALL if signal_name == "BUY_CALL" else OptionType.PUT
    captured_at = _timestamp(_field(snapshot, "captured_at"))
    return BotOrderSignal(
        underlying=underlying,
        strike=strike,
        option_type=option_type,
        captured_at=captured_at,
        profile=_optional_text(_field(snapshot, "strategy_profile"), "profile"),
        strategy=_optional_text(
            _enum_value(_field(snapshot, "selected_strategy")),
            "strategy",
            uppercase=True,
        ),
    )


def _validate_age(
    signal: BotOrderSignal,
    *,
    now: datetime,
    max_age_seconds: Decimal,
) -> None:
    age_seconds = (now - signal.captured_at).total_seconds()
    if age_seconds > float(max_age_seconds) or age_seconds < -5:
        raise ValueError("signal timestamp is outside the accepted window")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("event must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _enum_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "")


def _option_type(value: object) -> OptionType:
    normalized = _enum_value(value).strip().upper()
    if normalized in {"CALL", "CE"}:
        return OptionType.CALL
    if normalized in {"PUT", "PE"}:
        return OptionType.PUT
    raise ValueError("side must be CALL or PUT")


def _optional_text(
    value: object,
    name: str,
    *,
    uppercase: bool = False,
) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError(f"{name} exceeds 128 characters")
    return normalized.upper() if uppercase else normalized


def _positive_decimal(value: object, name: str) -> Decimal:
    parsed = _optional_decimal(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif value is None:
        return datetime.now(UTC)
    else:
        raise ValueError("captured_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
