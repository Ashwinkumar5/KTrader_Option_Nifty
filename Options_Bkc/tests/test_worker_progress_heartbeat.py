from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.workers.progress_heartbeat import WorkerProgressHeartbeat


class WorkerProgressHeartbeatTests(unittest.TestCase):
    def test_idle_worker_refreshes_but_stalled_work_leaves_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker.heartbeat"

            async def exercise() -> None:
                heartbeat = WorkerProgressHeartbeat(
                    path,
                    interval_seconds=0.01,
                    stall_timeout_seconds=0.04,
                )
                await heartbeat.start()
                initial_mtime = path.stat().st_mtime_ns
                await asyncio.sleep(0.02)
                self.assertGreater(path.stat().st_mtime_ns, initial_mtime)

                heartbeat.begin_work()
                await asyncio.sleep(0.06)
                self.assertTrue(heartbeat.is_stalled)
                stalled_mtime = path.stat().st_mtime_ns
                await asyncio.sleep(0.02)
                self.assertEqual(path.stat().st_mtime_ns, stalled_mtime)

                await heartbeat.finish_work()
                self.assertGreater(path.stat().st_mtime_ns, stalled_mtime)
                await heartbeat.close()

            asyncio.run(exercise())

    def test_invalid_timing_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WorkerProgressHeartbeat(
                Path("worker.heartbeat"),
                interval_seconds=10,
                stall_timeout_seconds=10,
            )


if __name__ == "__main__":
    unittest.main()
