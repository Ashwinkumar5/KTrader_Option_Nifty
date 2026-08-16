from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.core.strategy_config import (
    StrategyProfile,
    available_strategy_profiles,
    load_strategy_configuration,
)
from app.execution.simulator_ipc import (
    SimulatorDeliveryOutcome,
    SimulatorEntrySignal,
)


_LOGGER = logging.getLogger(__name__)
_ROUTER_ENDPOINT = "CentralSignalRouter"
_PROTOCOL_VERSION = 1
_MAX_REQUEST_BYTES = 4096
_MAX_REPLY_BYTES = 1024


class SignalRouteStatus(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    QUEUED = "QUEUED"
    LOG_ONLY = "LOG_ONLY"
    DUPLICATE = "DUPLICATE"
    DROPPED = "DROPPED"


@dataclass(frozen=True, slots=True)
class SignalRouteRequest:
    configured_profile: str
    signal: SimulatorEntrySignal

    def __post_init__(self) -> None:
        profile = self.configured_profile.strip()
        if not profile:
            raise ValueError("configured profile is required")
        if len(profile) > 128:
            raise ValueError("configured profile exceeds 128 characters")
        object.__setattr__(self, "configured_profile", profile)


@dataclass(frozen=True, slots=True)
class SignalRouteResult:
    signal_id: str
    status: SignalRouteStatus
    reason: str
    published: bool


@dataclass(frozen=True, slots=True)
class SignalRouteAudit:
    routed_at: datetime
    request: SignalRouteRequest
    result: SignalRouteResult


class EntrySignalPublisher(Protocol):
    def publish(self, signal: SimulatorEntrySignal) -> bool:
        """Queue a signal without waiting for transport I/O."""


@dataclass(frozen=True, slots=True)
class StrategyRoutePolicy:
    enabled: bool
    publish_to_simulator: bool


class StrategyRoutingPolicyCatalog:
    """Immutable routing policy keyed by configured profile and strategy."""

    def __init__(
        self,
        policies: Mapping[tuple[str, str], StrategyRoutePolicy],
    ) -> None:
        self._policies = dict(policies)

    @classmethod
    def from_configuration(
        cls,
        path: str | Path | None = None,
    ) -> StrategyRoutingPolicyCatalog:
        policies: dict[tuple[str, str], StrategyRoutePolicy] = {}
        for profile_name in available_strategy_profiles(path):
            profile = load_strategy_configuration(
                path,
                profile_name=profile_name,
            ).profile
            policies.update(_profile_policies(profile_name, profile))
        return cls(policies)

    def policy_for(
        self,
        configured_profile: str,
        strategy: str | None,
    ) -> StrategyRoutePolicy | None:
        if strategy is None:
            return None
        return self._policies.get(
            (configured_profile, _canonical_strategy_name(strategy))
        )


class CentralSignalRouter:
    """One fail-closed routing authority shared by every strategy process."""

    def __init__(
        self,
        *,
        policies: StrategyRoutingPolicyCatalog,
        publisher: EntrySignalPublisher | None,
        simulator_enabled: bool,
        dedup_capacity: int = 4096,
        audit_sink: Callable[[SignalRouteAudit], None] | None = None,
    ) -> None:
        self._policies = policies
        self._publisher = publisher
        self._simulator_enabled = simulator_enabled
        self._dedup_capacity = max(dedup_capacity, 1)
        self._audit_sink = audit_sink
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()
        self._routed = 0
        self._queued = 0
        self._log_only = 0
        self._duplicates = 0
        self._dropped = 0

    def route(self, request: SignalRouteRequest) -> SignalRouteResult:
        """Apply policy and enqueue in constant time; never perform network I/O."""

        signal = request.signal
        signal_id = signal.signal_id
        if signal_id is None:  # Defensive; SimulatorEntrySignal creates one.
            raise ValueError("signal_id is required")

        if not _effective_profile_matches(
            request.configured_profile,
            signal.profile,
        ):
            return self._complete(
                request,
                SignalRouteStatus.LOG_ONLY,
                "configured and effective profiles do not match",
                published=False,
            )

        policy = self._policies.policy_for(
            request.configured_profile,
            signal.strategy,
        )
        if policy is None:
            return self._complete(
                request,
                SignalRouteStatus.LOG_ONLY,
                "unknown profile or strategy; publishing fails closed",
                published=False,
            )
        if not policy.enabled:
            return self._complete(
                request,
                SignalRouteStatus.LOG_ONLY,
                "strategy is disabled in the configured profile",
                published=False,
            )
        if not policy.publish_to_simulator:
            return self._complete(
                request,
                SignalRouteStatus.LOG_ONLY,
                "publish_to_simulator is false",
                published=False,
            )
        if not self._simulator_enabled or self._publisher is None:
            return self._complete(
                request,
                SignalRouteStatus.LOG_ONLY,
                "global Simulator UI publishing is disabled",
                published=False,
            )
        if signal_id in self._seen:
            return self._complete(
                request,
                SignalRouteStatus.DUPLICATE,
                "signal_id was already routed",
                published=False,
            )

        self._remember(signal_id)
        authorized = SignalRouteResult(
            signal_id=signal_id,
            status=SignalRouteStatus.AUTHORIZED,
            reason="routing policy authorized Simulator UI publishing",
            published=False,
        )
        try:
            self._record_audit(request, authorized)
        except Exception:
            self._forget(signal_id)
            raise
        try:
            queued = self._publisher.publish(signal)
        except Exception as exc:
            self._forget(signal_id)
            return self._complete(
                request,
                SignalRouteStatus.DROPPED,
                f"Simulator UI publisher failed: {type(exc).__name__}",
                published=False,
            )
        if not queued:
            # Queue pressure is retryable; do not poison idempotency state.
            self._forget(signal_id)
            return self._complete(
                request,
                SignalRouteStatus.DROPPED,
                "Simulator UI publisher queue rejected the signal",
                published=False,
            )
        return self._complete(
            request,
            SignalRouteStatus.QUEUED,
            "queued for Simulator UI delivery",
            published=True,
        )

    def health_snapshot(self) -> dict[str, int | bool]:
        return {
            "simulator_enabled": self._simulator_enabled,
            "dedup_size": len(self._seen),
            "routed": self._routed,
            "queued": self._queued,
            "log_only": self._log_only,
            "duplicates": self._duplicates,
            "dropped": self._dropped,
        }

    def _remember(self, signal_id: str) -> None:
        self._seen.add(signal_id)
        self._seen_order.append(signal_id)
        while len(self._seen_order) > self._dedup_capacity:
            expired = self._seen_order.popleft()
            self._seen.discard(expired)

    def _forget(self, signal_id: str) -> None:
        if signal_id not in self._seen:
            return
        self._seen.discard(signal_id)
        with suppress(ValueError):
            self._seen_order.remove(signal_id)

    def _complete(
        self,
        request: SignalRouteRequest,
        status: SignalRouteStatus,
        reason: str,
        *,
        published: bool,
    ) -> SignalRouteResult:
        signal_id = request.signal.signal_id
        if signal_id is None:
            raise ValueError("signal_id is required")
        result = SignalRouteResult(
            signal_id=signal_id,
            status=status,
            reason=reason,
            published=published,
        )
        self._record_audit(request, result)
        self._routed += 1
        if status == SignalRouteStatus.QUEUED:
            self._queued += 1
        elif status == SignalRouteStatus.LOG_ONLY:
            self._log_only += 1
        elif status == SignalRouteStatus.DUPLICATE:
            self._duplicates += 1
        elif status == SignalRouteStatus.DROPPED:
            self._dropped += 1

        return result

    def _record_audit(
        self,
        request: SignalRouteRequest,
        result: SignalRouteResult,
    ) -> None:
        audit = SignalRouteAudit(
            routed_at=datetime.now(UTC),
            request=request,
            result=result,
        )
        if self._audit_sink is not None:
            self._audit_sink(audit)
        _LOGGER.info(
            "signal_route id=%s profile=%s strategy=%s status=%s reason=%s",
            result.signal_id,
            request.configured_profile,
            request.signal.strategy,
            result.status.value,
            result.reason,
        )


class JsonlSignalRouteAudit:
    """Line-buffered authoritative audit owned only by the router process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)

    def record(self, audit: SignalRouteAudit) -> None:
        signal = audit.request.signal
        payload = {
            "record_type": "signal_route",
            "routed_at": audit.routed_at.isoformat(),
            "configured_profile": audit.request.configured_profile,
            "signal_id": signal.signal_id,
            "effective_profile": signal.profile,
            "strategy": signal.strategy,
            "underlying": signal.underlying,
            "strike": str(signal.strike),
            "side": signal.side,
            "captured_at": signal.captured_at.isoformat(),
            "status": audit.result.status.value,
            "reason": audit.result.reason,
            "published": audit.result.published,
        }
        self._handle.write(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        )

    def record_delivery(self, outcome: SimulatorDeliveryOutcome) -> None:
        signal = outcome.signal
        payload = {
            "record_type": "simulator_delivery",
            "completed_at": outcome.completed_at.isoformat(),
            "signal_id": signal.signal_id,
            "effective_profile": signal.profile,
            "strategy": signal.strategy,
            "underlying": signal.underlying,
            "strike": str(signal.strike),
            "side": signal.side,
            "captured_at": signal.captured_at.isoformat(),
            "status": outcome.status,
            "reason": outcome.reason,
        }
        self._handle.write(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        )

    def close(self) -> None:
        self._handle.close()


class CentralSignalRouterServer:
    """Small loopback TCP service hosting the shared routing authority."""

    def __init__(
        self,
        router: CentralSignalRouter,
        *,
        host: str = "127.0.0.1",
        port: int = 47820,
        request_timeout_seconds: float = 0.50,
    ) -> None:
        self._router = router
        self._host = host
        self._port = port
        self._request_timeout_seconds = max(request_timeout_seconds, 0.05)
        self._server: asyncio.Server | None = None

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("central signal router server is not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
            limit=_MAX_REQUEST_BYTES + 1,
        )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        if self._server is None:
            raise RuntimeError("central signal router server failed to start")
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(
                reader.readline(),
                timeout=self._request_timeout_seconds,
            )
            if not raw or len(raw) > _MAX_REQUEST_BYTES:
                raise SignalRouterProtocolError("invalid request size")
            payload = json.loads(raw)
            request = signal_route_request_from_payload(payload)
            result = self._router.route(request)
            reply = {
                "signal_id": result.signal_id,
                "status": result.status.value,
                "reason": result.reason,
                "published": result.published,
            }
        except (
            SignalRouterProtocolError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            TimeoutError,
        ) as exc:
            _LOGGER.warning("signal router rejected request: %s", exc)
            reply = {
                "status": "REJECTED",
                "reason": str(exc)[:256],
                "published": False,
            }
        except Exception:
            _LOGGER.exception("signal router request failed")
            reply = {
                "status": "FAILED",
                "reason": "internal router error",
                "published": False,
            }
        try:
            writer.write(
                json.dumps(reply, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            await asyncio.wait_for(
                writer.drain(),
                timeout=self._request_timeout_seconds,
            )
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()


class CentralSignalRouterClient:
    """Bounded non-blocking client used by each strategy worker."""

    def __init__(
        self,
        *,
        configured_profile: str,
        host: str = "127.0.0.1",
        port: int = 47820,
        queue_capacity: int = 256,
        timeout_seconds: float = 0.50,
        max_retries: int = 5,
    ) -> None:
        normalized_profile = configured_profile.strip()
        if not normalized_profile:
            raise ValueError("configured profile is required")
        self._configured_profile = normalized_profile
        self._host = host
        self._port = port
        self._timeout_seconds = max(timeout_seconds, 0.05)
        self._max_retries = max(max_retries, 0)
        self._queue: asyncio.Queue[SimulatorEntrySignal] = asyncio.Queue(
            maxsize=max(queue_capacity, 1)
        )
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._queued = 0
        self._replies: dict[str, int] = {}
        self._failed = 0
        self._dropped = 0

    def publish(self, signal: SimulatorEntrySignal) -> bool:
        if self._closed:
            return False
        self._ensure_started()
        try:
            self._queue.put_nowait(signal)
        except asyncio.QueueFull:
            self._dropped += 1
            _LOGGER.error(
                "central signal router client queue full; dropped id=%s",
                signal.signal_id,
            )
            return False
        self._queued += 1
        return True

    def health_snapshot(self) -> dict[str, object]:
        return {
            "host": self._host,
            "port": self._port,
            "configured_profile": self._configured_profile,
            "queue_depth": self._queue.qsize(),
            "queued": self._queued,
            "replies": dict(self._replies),
            "failed": self._failed,
            "dropped": self._dropped,
        }

    async def close(self, *, drain_timeout_seconds: float = 7.0) -> None:
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
                name="central-signal-router-client",
            )

    async def _run(self) -> None:
        while True:
            signal = await self._queue.get()
            try:
                reply = await self._send_with_retry(signal)
                status = str(reply.get("status") or "INVALID_REPLY").upper()
                self._replies[status] = self._replies.get(status, 0) + 1
                log = (
                    _LOGGER.info
                    if status in {"QUEUED", "LOG_ONLY"}
                    else _LOGGER.warning
                )
                log(
                    "central signal router reply id=%s status=%s reason=%s",
                    signal.signal_id,
                    status,
                    reply.get("reason"),
                )
            except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
                self._failed += 1
                _LOGGER.error(
                    "central signal router delivery failed id=%s: %s",
                    signal.signal_id,
                    exc,
                )
            finally:
                self._queue.task_done()

    async def _send_with_retry(
        self,
        signal: SimulatorEntrySignal,
    ) -> dict[str, object]:
        request = SignalRouteRequest(
            configured_profile=self._configured_profile,
            signal=signal,
        )
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await _send_route_request(
                    host=self._host,
                    port=self._port,
                    timeout_seconds=self._timeout_seconds,
                    request=request,
                )
            except (ConnectionError, OSError, TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(min(0.25 * (2**attempt), 1.0))
        if last_error is None:
            raise RuntimeError("signal router retry loop did not run")
        raise last_error


class SignalRouterProtocolError(ValueError):
    pass


def signal_route_request_to_payload(
    request: SignalRouteRequest,
) -> dict[str, object]:
    signal = request.signal
    return {
        "endpoint": _ROUTER_ENDPOINT,
        "version": _PROTOCOL_VERSION,
        "configured_profile": request.configured_profile,
        "signal": {
            "signal_id": signal.signal_id,
            "profile": signal.profile,
            "strategy": signal.strategy,
            "underlying": signal.underlying,
            "strike": str(signal.strike),
            "side": signal.side,
            "captured_at": signal.captured_at.isoformat(),
        },
    }


def signal_route_request_from_payload(
    payload: object,
) -> SignalRouteRequest:
    if not isinstance(payload, dict):
        raise SignalRouterProtocolError("request must be a JSON object")
    if payload.get("endpoint") != _ROUTER_ENDPOINT:
        raise SignalRouterProtocolError("unknown router endpoint")
    if payload.get("version") != _PROTOCOL_VERSION:
        raise SignalRouterProtocolError("unsupported router protocol version")
    raw_signal = payload.get("signal")
    if not isinstance(raw_signal, dict):
        raise SignalRouterProtocolError("signal must be a JSON object")
    captured_at = datetime.fromisoformat(
        _required_text(raw_signal.get("captured_at"), "captured_at")
    )
    if captured_at.tzinfo is None:
        raise SignalRouterProtocolError("captured_at must include a timezone")
    return SignalRouteRequest(
        configured_profile=_required_text(
            payload.get("configured_profile"),
            "configured_profile",
        ),
        signal=SimulatorEntrySignal(
            signal_id=_required_text(raw_signal.get("signal_id"), "signal_id"),
            profile=_optional_text(raw_signal.get("profile"), "profile"),
            strategy=_required_text(raw_signal.get("strategy"), "strategy"),
            underlying=_required_text(raw_signal.get("underlying"), "underlying"),
            strike=_positive_decimal(raw_signal.get("strike"), "strike"),
            side=_required_text(raw_signal.get("side"), "side"),
            captured_at=captured_at,
        ),
    )


async def _send_route_request(
    *,
    host: str,
    port: int,
    timeout_seconds: float,
    request: SignalRouteRequest,
) -> dict[str, object]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=timeout_seconds,
    )
    try:
        payload = signal_route_request_to_payload(request)
        writer.write(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)
        raw_reply = await asyncio.wait_for(
            reader.readline(),
            timeout=timeout_seconds,
        )
        if not raw_reply or len(raw_reply) > _MAX_REPLY_BYTES:
            raise ValueError("invalid central signal router reply")
        reply = json.loads(raw_reply)
        if not isinstance(reply, dict):
            raise ValueError("central signal router reply must be an object")
        return reply
    finally:
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()


def _profile_policies(
    profile_name: str,
    profile: StrategyProfile,
) -> dict[tuple[str, str], StrategyRoutePolicy]:
    return {
        (profile_name, strategy): StrategyRoutePolicy(
            enabled=toggle.enabled,
            publish_to_simulator=toggle.publish_to_simulator,
        )
        for strategy, toggle in profile.strategies.items()
    }


def _canonical_strategy_name(value: str) -> str:
    normalized = value.strip().upper()
    return {
        "BREAKOUT": "BREAKOUT_MOMENTUM",
        "GAMMA": "GAMMA_EXPANSION",
    }.get(normalized, normalized)


def _effective_profile_matches(
    configured_profile: str,
    effective_profile: str | None,
) -> bool:
    if effective_profile is None:
        return False
    return effective_profile in {
        configured_profile,
        f"{configured_profile}__runtime",
    }


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalRouterProtocolError(f"{field_name} must be non-empty text")
    normalized = value.strip()
    if len(normalized) > 128:
        raise SignalRouterProtocolError(f"{field_name} exceeds 128 characters")
    return normalized


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _positive_decimal(value: object, field_name: str) -> Decimal:
    text = _required_text(value, field_name)
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise SignalRouterProtocolError(
            f"{field_name} must be a decimal number"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise SignalRouterProtocolError(f"{field_name} must be positive")
    return parsed
