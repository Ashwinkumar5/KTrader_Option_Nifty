from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

_LOGGER = logging.getLogger(__name__)
_MAX_REPLY_BYTES = 64


@dataclass(frozen=True, slots=True)
class SimulatorEntrySignal:
    underlying: str
    strike: Decimal
    side: str
    captured_at: datetime
    profile: str | None = None
    strategy: str | None = None
    signal_id: str | None = None

    def __post_init__(self) -> None:
        normalized_side = self.side.strip().upper()
        if normalized_side not in {"BUY_CALL", "BUY_PUT"}:
            raise ValueError("simulator entry side must be BUY_CALL or BUY_PUT")
        if not self.underlying.strip() or self.strike <= 0:
            raise ValueError("simulator entry requires underlying and positive strike")
        object.__setattr__(self, "side", normalized_side)
        object.__setattr__(self, "underlying", self.underlying.strip().upper())
        profile = _optional_name(self.profile)
        strategy = _optional_name(self.strategy, uppercase=True)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "strategy", strategy)
        signal_id = _optional_name(self.signal_id)
        if signal_id is None:
            identity = ":".join(
                (
                    profile or "NONE",
                    strategy or "NONE",
                    self.underlying,
                    format(self.strike, "f"),
                    normalized_side,
                    self.captured_at.isoformat(),
                )
            )
            signal_id = "bot-" + sha256(identity.encode("utf-8")).hexdigest()[:28]
        object.__setattr__(self, "signal_id", signal_id)


@dataclass(frozen=True, slots=True)
class SimulatorDeliveryOutcome:
    signal: SimulatorEntrySignal
    status: str
    reason: str
    completed_at: datetime


class SimulatorEntryPublisher:
    """Non-blocking, bounded publisher for qualified KTraderUI entries."""

    def __init__(
        self,
        *,
        endpoint: str = "KTraderUI",
        host: str = "127.0.0.1",
        port: int = 47821,
        queue_capacity: int = 64,
        timeout_seconds: float = 0.50,
        max_retries: int = 2,
        on_result: Callable[[SimulatorDeliveryOutcome], None] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._host = host
        self._port = port
        self._timeout_seconds = max(timeout_seconds, 0.05)
        self._max_retries = max(max_retries, 0)
        self._on_result = on_result
        self._queue: asyncio.Queue[SimulatorEntrySignal] = asyncio.Queue(
            maxsize=max(queue_capacity, 1)
        )
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._queued = 0
        self._accepted = 0
        self._rejected = 0
        self._failed = 0
        self._dropped = 0

    def publish(self, signal: SimulatorEntrySignal) -> bool:
        """Queue an entry in constant time; network I/O runs separately."""

        if self._closed:
            return False
        self._ensure_started()
        try:
            self._queue.put_nowait(signal)
        except asyncio.QueueFull:
            self._dropped += 1
            _LOGGER.error(
                "KTraderUI entry queue is full; dropped %s %s %s",
                signal.underlying,
                signal.strike,
                signal.side,
            )
            return False
        self._queued += 1
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {
            "endpoint": self._endpoint,
            "host": self._host,
            "port": self._port,
            "queue_depth": self._queue.qsize(),
            "queued": self._queued,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "failed": self._failed,
            "dropped": self._dropped,
        }

    async def close(self, *, drain_timeout_seconds: float = 3.0) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(
                self._queue.join(),
                timeout=max(drain_timeout_seconds, 0.05),
            )
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    def _ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="ktrader-simulator-entry-publisher",
            )

    async def _run(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                reply = await self._send_with_retry(signal)
                if reply == "OK":
                    self._accepted += 1
                    _LOGGER.info(
                        "KTraderUI accepted entry: %s %s %s",
                        signal.underlying,
                        signal.strike,
                        signal.side,
                    )
                    self._notify_result(signal, "ACCEPTED", reply)
                else:
                    self._rejected += 1
                    _LOGGER.warning(
                        "KTraderUI rejected entry %s %s %s: %s",
                        signal.underlying,
                        signal.strike,
                        signal.side,
                        reply,
                    )
                    self._notify_result(signal, "REJECTED", reply)
            except (ConnectionError, OSError, TimeoutError) as exc:
                self._failed += 1
                _LOGGER.warning(
                    "KTraderUI entry failed for %s %s %s: %s",
                    signal.underlying,
                    signal.strike,
                    signal.side,
                    exc,
                )
                self._notify_result(signal, "FAILED", str(exc))
            finally:
                self._queue.task_done()

    async def _send_with_retry(self, signal: SimulatorEntrySignal) -> str:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await _send_entry(
                    endpoint=self._endpoint,
                    host=self._host,
                    port=self._port,
                    timeout_seconds=self._timeout_seconds,
                    signal=signal,
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(min(0.10 * (2**attempt), 0.50))
        if last_error is None:
            raise RuntimeError("Simulator UI retry loop did not run")
        raise last_error

    def _notify_result(
        self,
        signal: SimulatorEntrySignal,
        status: str,
        reason: str,
    ) -> None:
        callback = self._on_result
        if callback is None:
            return
        try:
            callback(
                SimulatorDeliveryOutcome(
                    signal=signal,
                    status=status,
                    reason=reason,
                    completed_at=datetime.now(UTC),
                )
            )
        except Exception:
            _LOGGER.exception(
                "KTraderUI delivery audit failed for %s",
                signal.signal_id,
            )


async def _send_entry(
    *,
    endpoint: str,
    host: str,
    port: int,
    timeout_seconds: float,
    signal: SimulatorEntrySignal,
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
            "side": "CALL" if signal.side == "BUY_CALL" else "PUT",
            "captured_at": signal.captured_at.isoformat(),
        }
        writer.write(
            json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        reply = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout_seconds,
        )
        if not reply:
            raise ConnectionError(
                "KTraderUI closed without an acknowledgement"
            )
        if len(reply) > _MAX_REPLY_BYTES:
            return "INVALID_REPLY"
        return reply.decode("utf-8", errors="replace").strip().upper()
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _optional_name(value: str | None, *, uppercase: bool = False) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError("simulator entry metadata exceeds 128 characters")
    return normalized.upper() if uppercase else normalized
