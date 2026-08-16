"""Progress-aware watchdog heartbeat for long-running strategy workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import monotonic


class WorkerProgressHeartbeat:
    """Touch a watchdog file while idle, but stop when active work stalls.

    A periodic liveness-only heartbeat would hide an async storage or strategy
    call that never returns.  This small helper instead considers an active
    tick or frame stale after ``stall_timeout_seconds``.  The watchdog then
    terminates and restarts that process using its normal heartbeat timeout.
    """

    def __init__(
        self,
        path: Path,
        *,
        stall_timeout_seconds: float,
        interval_seconds: float = 2.0,
    ) -> None:
        if stall_timeout_seconds <= 0:
            raise ValueError("stall_timeout_seconds must be greater than zero")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        if interval_seconds >= stall_timeout_seconds:
            raise ValueError(
                "interval_seconds must be smaller than stall_timeout_seconds"
            )
        self._path = path
        self._stall_timeout_seconds = stall_timeout_seconds
        self._interval_seconds = interval_seconds
        self._active_operations = 0
        self._oldest_active_started_at: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Worker progress heartbeat is closed")
        if self._task is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await self._touch()
        self._task = asyncio.create_task(
            self._run(),
            name="strategy-worker-progress-heartbeat",
        )

    def begin_work(self) -> None:
        if self._closed:
            raise RuntimeError("Worker progress heartbeat is closed")
        if self._active_operations == 0:
            self._oldest_active_started_at = monotonic()
        self._active_operations += 1

    async def finish_work(self) -> None:
        if self._active_operations <= 0:
            raise RuntimeError("Worker progress heartbeat work count underflow")
        self._active_operations -= 1
        if self._active_operations == 0:
            self._oldest_active_started_at = None
            await self._touch()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @property
    def is_stalled(self) -> bool:
        started_at = self._oldest_active_started_at
        return (
            started_at is not None
            and monotonic() - started_at >= self._stall_timeout_seconds
        )

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            if self.is_stalled:
                # Deliberately leave the heartbeat file stale.  The watchdog
                # owns restart/backoff policy and will recover the process.
                continue
            await self._touch()

    async def _touch(self) -> None:
        await asyncio.to_thread(self._path.touch)
