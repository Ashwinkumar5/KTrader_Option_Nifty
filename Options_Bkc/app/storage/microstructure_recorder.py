from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import orjson

from app.domain.models import (
    AnalyticsSnapshot,
    FutureContract,
    InstrumentToken,
    MarketTick,
    MicrostructureFeatures,
    MicrostructureSignal,
    OptionChainSnapshot,
    OptionContract,
    OptionType,
)
from app.execution.paper import PaperFill
from app.execution.risk import PositionPlan
from app.signals.gate import SignalGateDecision
from app.storage.serialization import to_ist_iso, to_jsonable

CAPTURE_SCHEMA_VERSION = 4
_STOP = object()


class JsonlMicrostructureRecorder:
    """Append-only, self-contained broker replay tape.

    Schema v4 keeps session configuration, instrument metadata, every feed tick,
    subscription changes, REST refresh timing, normalized snapshots, analytics,
    gate decisions and research-readiness state in one daily JSONL file.
    """

    def __init__(
        self,
        path: Path,
        *,
        session_id: str | None = None,
        queue_capacity: int = 8192,
        batch_size: int = 256,
        analytics_trace_enabled: bool = False,
    ) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._session_id = session_id or str(uuid4())
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._batch_size = batch_size
        self._writer_task: asyncio.Task[None] | None = None
        self._writer_error: BaseException | None = None
        self._sequence = 0
        self._records_enqueued = 0
        self._records_written = 0
        self._finished = False
        self._write_batches = 0
        self._last_write_duration_ms: float | None = None
        self._max_write_duration_ms = 0.0
        self._analytics_trace_enabled = analytics_trace_enabled
        self._analytics_trace_path: Path | None = None
        self._analytics_trace_sequence = 0
        self._analytics_trace_records_written = 0
        self._enabled_strategies: frozenset[str] | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def analytics_trace_path(self) -> Path | None:
        return self._analytics_trace_path

    def health_snapshot(self) -> dict[str, object]:
        writer_error = (
            None
            if self._writer_error is None
            else type(self._writer_error).__name__
        )
        return {
            "status": "FAILED" if writer_error else "HEALTHY",
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "records_enqueued": self._records_enqueued,
            "records_written": self._records_written,
            "records_pending": (
                self._records_enqueued - self._records_written
            ),
            "write_batches": self._write_batches,
            "last_write_duration_ms": self._last_write_duration_ms,
            "max_write_duration_ms": round(
                self._max_write_duration_ms,
                3,
            ),
            "dropped_records": 0,
            "writer_error": writer_error,
            "analytics_trace_path": (
                str(self._analytics_trace_path)
                if self._analytics_trace_path is not None
                else None
            ),
            "analytics_trace_records_written": (
                self._analytics_trace_records_written
            ),
            "analytics_trace_enabled": self._analytics_trace_enabled,
        }

    async def record_session_manifest(
        self,
        *,
        started_at,
        effective_settings: dict[str, object],
        code_revision: str,
        market_timezone: str = "Asia/Kolkata",
    ) -> None:
        if self._analytics_trace_enabled:
            self._ensure_analytics_trace_path(started_at)
        strategy_configuration = effective_settings.get(
            "strategy_configuration"
        )
        self._enabled_strategies = _enabled_strategy_names(
            strategy_configuration
        )
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "session_manifest",
                "captured_at": started_at,
                "captured_at_ist": to_ist_iso(started_at),
                "capture_schema": "broker-replay-tape",
                "capture_schema_version": CAPTURE_SCHEMA_VERSION,
                "session_id": self._session_id,
                "clock_timezone": "UTC",
                "market_timezone": market_timezone,
                "code_revision": code_revision,
                "analytics_trace_file": (
                    self._analytics_trace_path.name
                    if self._analytics_trace_path is not None
                    else None
                ),
                "effective_settings": effective_settings,
                "ordering": {
                    "record_sequence": "strictly increasing per session",
                    "market_event_time": "tick.exchange_timestamp",
                    "capture_time": "record.captured_at",
                },
                "capture_capabilities": {
                    "raw_market_events": True,
                    "best_five_depth": True,
                    "spot_session_context": True,
                    "nearest_future_context": True,
                    "normalized_option_frames": True,
                    "strategy_evidence": True,
                    "paper_shadow_only": True,
                    "analytics_trace": self._analytics_trace_enabled,
                },
            }
        )
        if self._analytics_trace_enabled:
            await self._append_analytics_trace(
                {
                    "schema_version": 2,
                    "record_type": "analytics_trace_manifest",
                    "session_id": self._session_id,
                    "started_at": started_at,
                    "started_at_ist": to_ist_iso(started_at),
                    "source_replay_tape": str(self._path),
                    "code_revision": code_revision,
                    "market_timezone": market_timezone,
                    "strategy_profile": _strategy_profile_name(
                        strategy_configuration
                    ),
                    "enabled_strategies": sorted(
                        self._enabled_strategies or ()
                    ),
                    "strategy_configuration": strategy_configuration,
                    "outcome_research": {
                        "horizon_minutes": 10,
                        "entry_price": "ask",
                        "mark_price": "bid",
                    },
                }
            )

    async def record_instrument_master(
        self,
        *,
        captured_at,
        spot_tokens: tuple[InstrumentToken, ...],
        option_contracts: tuple[OptionContract, ...],
        selected_expiries: dict[str, object],
        future_contracts: tuple[FutureContract, ...] = (),
        reference_tokens: tuple[InstrumentToken, ...] = (),
    ) -> None:
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "instrument_master",
                "captured_at": captured_at,
                "captured_at_ist": to_ist_iso(captured_at),
                "session_id": self._session_id,
                "selected_expiries": selected_expiries,
                "spot_tokens": spot_tokens,
                "option_contracts": option_contracts,
                "future_contracts": future_contracts,
                "reference_tokens": reference_tokens,
            }
        )

    async def record_subscription_change(
        self,
        *,
        captured_at,
        action: str,
        tokens: tuple[InstrumentToken, ...],
        reason: str,
        underlying: str | None = None,
        spot_price: object | None = None,
        atm_strike: object | None = None,
    ) -> None:
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "subscription_change",
                "captured_at": captured_at,
                "captured_at_ist": to_ist_iso(captured_at),
                "session_id": self._session_id,
                "action": action,
                "reason": reason,
                "underlying": underlying,
                "spot_price": spot_price,
                "atm_strike": atm_strike,
                "tokens": tokens,
            }
        )

    async def record_market_event(
        self,
        *,
        tick: MarketTick,
        features: MicrostructureFeatures | None,
        signal: MicrostructureSignal | None,
    ) -> None:
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "market_event",
                "captured_at": tick.received_at,
                "captured_at_ist": to_ist_iso(tick.received_at),
                "exchange_timestamp_ist": to_ist_iso(tick.exchange_timestamp),
                "received_at_ist": to_ist_iso(tick.received_at),
                "session_id": self._session_id,
                "event_role": (
                    "spot"
                    if tick.token.kind is not None
                    and tick.token.kind.value == "index"
                    else "future"
                    if tick.token.kind is not None
                    and tick.token.kind.value == "future"
                    else "option"
                ),
                "tick": tick,
                "features": features,
                "microstructure_signal": signal,
            }
        )

    async def record_gate_decision(
        self,
        *,
        snapshot: OptionChainSnapshot,
        decision: SignalGateDecision,
        analytics: AnalyticsSnapshot | None = None,
        frame: dict[str, object] | None = None,
        execution_signal: dict[str, object] | None = None,
    ) -> None:
        normalized_frame = _with_ist_frame_timestamps(frame)
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "gate_decision",
                "captured_at": decision.captured_at,
                "captured_at_ist": to_ist_iso(decision.captured_at),
                "snapshot_captured_at_ist": to_ist_iso(snapshot.captured_at),
                "decision_captured_at_ist": to_ist_iso(decision.captured_at),
                "session_id": self._session_id,
                "frame": normalized_frame,
                "snapshot": snapshot,
                "analytics": analytics,
                "decision": decision,
                "execution_signal": execution_signal,
            }
        )
        if self._analytics_trace_enabled:
            await self._append_analytics_trace_batch(
                _analytics_trace_records(
                    analytics=analytics,
                    decision=decision,
                    enabled_strategies=self._enabled_strategies,
                    session_id=self._session_id,
                    source_sequence=self._sequence,
                )
            )

    async def record_paper_fill(
        self,
        *,
        fill: PaperFill,
        profile: str | None,
        underlying: str | None,
        strategy: str | None,
        side: str | None,
        strike: object | None,
        option_type: OptionType | None,
        position_plan: PositionPlan | None,
        realized_pnl: object,
        open_positions: int,
        gross_exposure: object,
    ) -> None:
        """Append a simulated fill without adding synchronous tick-path I/O."""

        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "paper_fill",
                "captured_at": fill.captured_at,
                "captured_at_ist": to_ist_iso(fill.captured_at),
                "session_id": self._session_id,
                "profile": profile,
                "underlying": underlying,
                "strategy": strategy,
                "side": side,
                "strike": strike,
                "option_type": option_type,
                "fill": fill,
                "position_plan": position_plan,
                "account": {
                    "realized_pnl": realized_pnl,
                    "open_positions": open_positions,
                    "gross_exposure": gross_exposure,
                },
            }
        )

    async def finish(
        self,
        *,
        completed_at: datetime,
        processed_ticks: int,
        status: str = "completed",
        error: str | None = None,
        paper_state: dict[str, object] | None = None,
    ) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
        await self._append(
            {
                "schema_version": CAPTURE_SCHEMA_VERSION,
                "record_type": "session_end",
                "captured_at": completed_at,
                "captured_at_ist": to_ist_iso(completed_at),
                "session_id": self._session_id,
                "status": status,
                "processed_ticks": processed_ticks,
                "error": error,
                "paper_state": paper_state,
                "writer": {
                    "records_enqueued_before_session_end": (
                        self._records_enqueued
                    ),
                    "records_written_before_session_end": (
                        self._records_written
                    ),
                    "queue_capacity": self._queue.maxsize,
                    "batch_size": self._batch_size,
                    "dropped_records": 0,
                },
            },
            force_flush=True,
            allow_finished=True,
        )
        if self._analytics_trace_path is not None:
            await self._append_analytics_trace(
                {
                    "schema_version": 2,
                    "record_type": "analytics_trace_end",
                    "session_id": self._session_id,
                    "captured_at": completed_at,
                    "captured_at_ist": to_ist_iso(completed_at),
                    "status": status,
                    "processed_ticks": processed_ticks,
                    "error": error,
                }
            )
        await self.close()

    async def flush(self) -> None:
        if self._writer_task is None:
            return
        await self._queue.join()
        self._raise_writer_error()

    async def close(self) -> None:
        task = self._writer_task
        if task is None:
            return
        await self.flush()
        await self._queue.put(_STOP)
        await task
        self._writer_task = None
        self._raise_writer_error()

    async def _append(
        self,
        record: dict[str, object],
        *,
        force_flush: bool | None = None,
        allow_finished: bool = False,
    ) -> None:
        async with self._lock:
            if self._finished and not allow_finished:
                raise RuntimeError("capture recorder is already finished")
            self._raise_writer_error()
            self._ensure_writer()
            self._sequence += 1
            record["sequence"] = self._sequence
            serialized = orjson.dumps(
                to_jsonable(record),
                option=orjson.OPT_APPEND_NEWLINE,
            )
            await self._queue.put(serialized)
            self._records_enqueued += 1
        should_flush = (
            force_flush
            if force_flush is not None
            else record.get("record_type")
            in {
                "session_manifest",
                "instrument_master",
                "session_end",
            }
        )
        if should_flush:
            await self.flush()

    def _ensure_writer(self) -> None:
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer_loop())

    async def _writer_loop(self) -> None:
        stopping = False
        while not stopping:
            first = await self._queue.get()
            if first is _STOP:
                self._queue.task_done()
                break
            batch = [bytes(first)]
            while len(batch) < self._batch_size:
                try:
                    item = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is _STOP:
                    self._queue.task_done()
                    stopping = True
                    break
                batch.append(bytes(item))
            try:
                if self._writer_error is None:
                    started = asyncio.get_running_loop().time()
                    await asyncio.to_thread(self._write, b"".join(batch))
                    duration_ms = (
                        asyncio.get_running_loop().time() - started
                    ) * 1000
                    self._last_write_duration_ms = round(duration_ms, 3)
                    self._max_write_duration_ms = max(
                        self._max_write_duration_ms,
                        duration_ms,
                    )
                    self._write_batches += 1
                    self._records_written += len(batch)
            except BaseException as exc:
                self._writer_error = exc
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write(self, serialized: bytes) -> None:
        with self._path.open("ab") as handle:
            handle.write(serialized)

    def _ensure_analytics_trace_path(self, captured_at: datetime) -> None:
        if not self._analytics_trace_enabled:
            return
        if self._analytics_trace_path is not None:
            return
        captured_at_ist = to_ist_iso(captured_at)
        if captured_at_ist is None:
            raise ValueError("analytics trace timestamp is required")
        timestamp = datetime.fromisoformat(captured_at_ist).strftime(
            "%Y%m%d_%H%M%S"
        )
        session_suffix = "".join(
            character
            for character in self._session_id
            if character.isalnum()
        )[:8]
        suffix = f"_{session_suffix}" if session_suffix else ""
        self._analytics_trace_path = self._path.parent / (
            f"analytics_engine_trace_{timestamp}_IST{suffix}.jsonl"
        )

    async def _append_analytics_trace(
        self,
        record: dict[str, object],
    ) -> None:
        await self._append_analytics_trace_batch((record,))

    async def _append_analytics_trace_batch(
        self,
        records: tuple[dict[str, object], ...],
    ) -> None:
        if not self._analytics_trace_enabled or not records:
            return
        captured_at = records[0].get("captured_at")
        if self._analytics_trace_path is None:
            if not isinstance(captured_at, datetime):
                return
            self._ensure_analytics_trace_path(captured_at)
        serialized_records: list[bytes] = []
        for record in records:
            self._analytics_trace_sequence += 1
            record["trace_sequence"] = self._analytics_trace_sequence
            serialized_records.append(
                orjson.dumps(
                    to_jsonable(record),
                    option=orjson.OPT_APPEND_NEWLINE,
                )
            )
        serialized = b"".join(serialized_records)
        try:
            await asyncio.to_thread(
                self._write_analytics_trace,
                serialized,
            )
            self._analytics_trace_records_written += len(records)
        except BaseException as exc:
            self._writer_error = exc
            self._raise_writer_error()

    def _write_analytics_trace(self, serialized: bytes) -> None:
        if self._analytics_trace_path is None:
            raise RuntimeError("analytics trace path is not initialized")
        with self._analytics_trace_path.open("ab") as handle:
            handle.write(serialized)

    def _raise_writer_error(self) -> None:
        if self._writer_error is not None:
            raise RuntimeError(
                f"capture writer failed for {self._path}: "
                f"{self._writer_error}"
            ) from self._writer_error


def _analytics_trace_records(
    *,
    analytics: AnalyticsSnapshot | None,
    decision: SignalGateDecision,
    enabled_strategies: frozenset[str] | None = None,
    session_id: str,
    source_sequence: int,
) -> tuple[dict[str, object], ...]:
    diagnostics = {
        diagnostic.family.value: diagnostic
        for diagnostic in (
            analytics.strategy_diagnostics if analytics is not None else ()
        )
        if (
            enabled_strategies is None
            or diagnostic.family.value in enabled_strategies
        )
    }
    strategy_names = set(diagnostics)
    if enabled_strategies is not None:
        strategy_names.update(enabled_strategies)
    preferred_order = (
        "GAMMA_EXPANSION",
        "DERIVATIVES_QUANT",
        "LEVEL_REVERSAL",
        "BREAKOUT_MOMENTUM",
    )
    ordered_names = tuple(
        name for name in preferred_order if name in strategy_names
    ) + tuple(sorted(strategy_names - set(preferred_order)))
    proposals: list[str] = []
    for strategy_name in ordered_names:
        diagnostic = diagnostics.get(strategy_name)
        proposals.append(
            _proposal_text(
                "strategy",
                strategy_name,
                diagnostic.proposed_side if diagnostic is not None else None,
            )
        )
    for strategy_name in ordered_names:
        diagnostic = diagnostics.get(strategy_name)
        if diagnostic is None:
            continue
        proposals.extend(
            _proposal_text("feature", check.code, check.proposed_side)
            for check in diagnostic.feature_checks
        )
    return (
        {
            "schema_version": 2,
            "record_type": "strategy_feature_signals",
            "source_gate_sequence": source_sequence,
            "session_id": session_id,
            "captured_at": decision.captured_at,
            "signals": " | ".join(proposals),
        },
    )


def _proposal_text(kind: str, name: str, value: object) -> str:
    side = _directional_side(value) or "null"
    return f"{kind}={name};proposed_signal={side}"


def _directional_side(value: object) -> str | None:
    return str(value) if value in {"BUY_CALL", "BUY_PUT"} else None


def _enabled_strategy_names(
    strategy_configuration: object,
) -> frozenset[str] | None:
    if not isinstance(strategy_configuration, dict):
        return None
    profile = strategy_configuration.get("profile")
    if not isinstance(profile, dict):
        return None
    strategies = profile.get("strategies")
    if not isinstance(strategies, dict):
        return None
    return frozenset(
        str(name)
        for name, settings in strategies.items()
        if isinstance(settings, dict) and settings.get("enabled") is True
    )


def _strategy_profile_name(strategy_configuration: object) -> str | None:
    if not isinstance(strategy_configuration, dict):
        return None
    profile = strategy_configuration.get("profile")
    if not isinstance(profile, dict):
        return None
    name = profile.get("name")
    return str(name) if name is not None else None


def _with_ist_frame_timestamps(
    frame: dict[str, object] | None,
) -> dict[str, object]:
    """Copy frame metadata and add display-only IST timestamps."""
    normalized = dict(frame or {})
    for field in (
        "scheduled_for",
        "frame_started_at",
        "frame_completed_at",
        "trigger_tick_received_at",
    ):
        value = normalized.get(field)
        if isinstance(value, datetime):
            normalized[f"{field}_ist"] = to_ist_iso(value)

    spot = normalized.get("spot")
    if isinstance(spot, dict):
        normalized_spot = dict(spot)
        observed_at = normalized_spot.get("observed_at")
        if isinstance(observed_at, datetime):
            normalized_spot["observed_at_ist"] = to_ist_iso(observed_at)
        normalized["spot"] = normalized_spot
    return normalized
