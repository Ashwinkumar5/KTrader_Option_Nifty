from __future__ import annotations

import asyncio
from pathlib import Path


_STOP = object()


class MarketDataFeedTape:
    """Bounded background JSONL writer for canonical transport envelopes."""

    def __init__(self, path: Path, *, queue_capacity: int = 16384) -> None:
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        self._path = path
        self._queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._writer: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._closed = False
        self._written = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def written(self) -> int:
        return self._written

    def record_encoded(self, payload: bytes) -> bool:
        """Admit one already-encoded envelope without blocking the feed loop."""

        self._raise_if_unavailable()
        self._ensure_writer()
        try:
            self._queue.put_nowait(bytes(payload))
        except asyncio.QueueFull:
            return False
        return True

    async def close(self) -> None:
        if self._closed:
            if self._error is not None:
                raise RuntimeError("Market-data feed tape failed") from self._error
            return
        self._closed = True
        writer = self._writer
        if writer is None:
            return
        if writer.done():
            await writer
        stop_admission = asyncio.create_task(
            self._queue.put(_STOP),
            name="market-data-feed-tape-stop",
        )
        done, _pending = await asyncio.wait(
            (stop_admission, writer),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if writer in done and not stop_admission.done():
            stop_admission.cancel()
            await asyncio.gather(stop_admission, return_exceptions=True)
            await writer
        await stop_admission
        await writer
        if self._error is not None:
            raise RuntimeError("Market-data feed tape failed") from self._error

    def health_snapshot(self) -> dict[str, object]:
        return {
            "status": "FAILED" if self._error is not None else "HEALTHY",
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self._queue.maxsize,
            "written_events": self._written,
            "error": (
                f"{type(self._error).__name__}: {self._error}"
                if self._error is not None
                else None
            ),
        }

    def _ensure_writer(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(
                self._run_writer(),
                name="market-data-feed-tape-writer",
            )

    def _raise_if_unavailable(self) -> None:
        if self._closed:
            raise RuntimeError("Market-data feed tape is closed")
        if self._error is not None:
            raise RuntimeError("Market-data feed tape failed") from self._error

    async def _run_writer(self) -> None:
        try:
            while True:
                first = await self._queue.get()
                if first is _STOP:
                    self._queue.task_done()
                    break
                batch = [first]
                stop_after_batch = False
                while len(batch) < 256:
                    try:
                        item = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if item is _STOP:
                        self._queue.task_done()
                        stop_after_batch = True
                        break
                    batch.append(item)
                await asyncio.to_thread(self._append_batch, batch)
                for _ in batch:
                    self._queue.task_done()
                self._written += len(batch)
                if stop_after_batch:
                    break
        except BaseException as exc:
            self._error = exc
            raise

    def _append_batch(self, batch: list[bytes | object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as stream:
            for payload in batch:
                if not isinstance(payload, bytes):  # pragma: no cover
                    raise TypeError("Feed-tape payload must be bytes")
                stream.write(payload)
                stream.write(b"\n")
            stream.flush()
