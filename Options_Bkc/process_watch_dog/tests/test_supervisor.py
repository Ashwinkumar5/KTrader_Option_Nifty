from __future__ import annotations

import sys
import shutil
import socket
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from process_watch_dog.config import ProcessSpec, RestartPolicy, WatchdogSettings
from process_watch_dog.control import send_control_request
from process_watch_dog.process_control import InstanceLock, process_exists
from process_watch_dog.supervisor import ManagedProcess, ProcessSupervisor


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).with_name("fixture_child.py")
TEST_TEMP_ROOT = PACKAGE_ROOT / ".test-work"


class ProcessSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TEMP_ROOT / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.supervisors: list[ProcessSupervisor] = []

    def tearDown(self) -> None:
        for supervisor in reversed(self.supervisors):
            supervisor.shutdown()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_fatal_broker_output_restarts_with_new_pid(self) -> None:
        spec = self._spec(
            "fatal_bot",
            "fatal",
            fatal_output_patterns=("BROKER CONNECTION LOST",),
            restart=self._restart_policy(maximum_restarts=2),
        )
        supervisor = self._supervisor(spec)

        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: self._state(supervisor, "fatal_bot")["launch_count"] >= 2,
        )
        state = self._state(supervisor, "fatal_bot")

        self.assertGreaterEqual(len(state["pid_history"]), 2)
        self.assertNotEqual(state["pid_history"][0], state["pid_history"][1])
        self.assertGreaterEqual(state["restart_count"], 1)

    def test_intentional_stop_does_not_restart(self) -> None:
        spec = self._spec("steady_bot", "wait")
        supervisor = self._supervisor(spec)
        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: self._state(supervisor, "steady_bot")["status"] == "running",
        )

        supervisor.stop_process("steady_bot")
        end = time.monotonic() + 0.4
        while time.monotonic() < end:
            supervisor.poll_once()
            time.sleep(0.02)
        state = self._state(supervisor, "steady_bot")

        self.assertEqual(state["status"], "stopped_by_user")
        self.assertEqual(state["launch_count"], 1)
        self.assertIsNone(state["pid"])

    def test_crash_loop_stops_after_configured_restarts(self) -> None:
        spec = self._spec(
            "crashing_bot",
            "exit",
            extra=("--exit-code", "9", "--delay", "0.01"),
            restart=self._restart_policy(maximum_restarts=2),
        )
        supervisor = self._supervisor(spec)

        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: self._state(supervisor, "crashing_bot")["status"]
            == "crash_loop",
        )
        state = self._state(supervisor, "crashing_bot")

        self.assertEqual(state["launch_count"], 3)
        self.assertEqual(state["restart_count"], 2)

    def test_processes_are_supervised_independently(self) -> None:
        steady = self._spec("steady", "wait")
        crashing = self._spec(
            "crashing",
            "exit",
            extra=("--exit-code", "7", "--delay", "0.01"),
            restart=self._restart_policy(maximum_restarts=2),
        )
        supervisor = self._supervisor(steady, crashing)
        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: self._state(supervisor, "crashing")["launch_count"] >= 2,
        )
        steady_state = self._state(supervisor, "steady")

        self.assertEqual(steady_state["launch_count"], 1)
        self.assertEqual(len(steady_state["pid_history"]), 1)
        self.assertEqual(steady_state["status"], "running")

    def test_stop_all_stops_processes_in_reverse_startup_order(self) -> None:
        supervisor = self._supervisor(
            self._spec("infrastructure", "wait"),
            self._spec("publisher", "wait"),
            self._spec("subscriber", "wait"),
        )
        started = supervisor.start_all()
        self.assertEqual(
            started["started"],
            ["infrastructure", "publisher", "subscriber"],
        )
        self._wait_for(
            supervisor,
            lambda: all(
                self._state(supervisor, process_id)["status"] == "running"
                for process_id in started["started"]
            ),
        )

        stopped = supervisor.stop_all()

        self.assertEqual(
            stopped["stopped"],
            ["subscriber", "publisher", "infrastructure"],
        )

    def test_shutdown_stops_processes_in_reverse_startup_order(self) -> None:
        supervisor = self._supervisor(
            self._spec("infrastructure", "wait"),
            self._spec("publisher", "wait"),
            self._spec("subscriber", "wait"),
        )
        supervisor.start_all()
        stop_order: list[str] = []
        stop_process = supervisor._stop_process_locked

        def record_stop(managed: ManagedProcess, *, intentional: bool) -> None:
            stop_order.append(managed.spec.process_id)
            stop_process(managed, intentional=intentional)

        with patch.object(
            supervisor,
            "_stop_process_locked",
            side_effect=record_stop,
        ):
            supervisor.shutdown()

        self.assertEqual(
            stop_order,
            ["subscriber", "publisher", "infrastructure"],
        )

    def test_missing_heartbeat_restarts_hung_process(self) -> None:
        heartbeat = self.root / "never-created.heartbeat"
        spec = self._spec(
            "hung",
            "wait",
            restart=self._restart_policy(maximum_restarts=2),
            heartbeat_file=heartbeat,
            heartbeat_timeout_seconds=0.12,
            startup_grace_seconds=0.02,
        )
        supervisor = self._supervisor(spec)

        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: self._state(supervisor, "hung")["launch_count"] >= 2,
        )
        state = self._state(supervisor, "hung")

        self.assertGreaterEqual(state["restart_count"], 1)
        self.assertIn("heartbeat_timeout", state["last_failure_reason"])

    def test_stop_terminates_descendant_process(self) -> None:
        descendant_pid_file = self.root / "descendant.pid"
        spec = self._spec(
            "tree",
            "grandchild",
            extra=("--pid-file", str(descendant_pid_file)),
        )
        supervisor = self._supervisor(spec)
        supervisor.start_all()
        self._wait_for(
            supervisor,
            lambda: descendant_pid_file.exists(),
        )
        descendant_pid = int(descendant_pid_file.read_text(encoding="utf-8"))
        self.assertTrue(process_exists(descendant_pid))

        supervisor.stop_process("tree")
        deadline = time.monotonic() + 3
        while process_exists(descendant_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        self.assertFalse(process_exists(descendant_pid))

    def test_control_interface_starts_stops_and_shuts_down(self) -> None:
        port = self._available_port()
        spec = self._spec("controlled", "wait")
        supervisor = self._supervisor_with_port(port, spec)
        runner = threading.Thread(target=supervisor.run, daemon=True)
        runner.start()
        deadline = time.monotonic() + 5
        while True:
            try:
                live = send_control_request(
                    "127.0.0.1", port, command="status", timeout_seconds=0.2
                )
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    self.fail("watchdog control server did not start")
                time.sleep(0.02)
        self.assertEqual(live["processes"][0]["status"], "running")

        stopped = send_control_request(
            "127.0.0.1",
            port,
            command="stop",
            process_id="controlled",
        )
        self.assertEqual(stopped["status"], "stopped_by_user")
        started = send_control_request(
            "127.0.0.1",
            port,
            command="start",
            process_id="controlled",
        )
        self.assertEqual(started["status"], "running")
        send_control_request("127.0.0.1", port, command="shutdown")
        runner.join(timeout=5)

        self.assertFalse(runner.is_alive())

    def test_instance_lock_prevents_duplicate_watchdog(self) -> None:
        path = self.root / "runtime" / "watchdog.lock"
        first = InstanceLock(path)
        second = InstanceLock(path)
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                second.acquire()
        finally:
            first.release()

        second.acquire()
        second.release()

    def test_console_shows_start_output_and_periodic_status(self) -> None:
        spec = self._spec("visible", "wait")
        supervisor = self._supervisor_with_port(
            0,
            spec,
            console_status_interval_seconds=0.02,
            console_show_child_output=False,
        )
        output = StringIO()
        with redirect_stdout(output):
            supervisor.start_all()
            self._wait_for(
                supervisor,
                lambda: "fixture_started" in output.getvalue()
                and "PROCESS WATCHDOG - LIVE STATUS" in output.getvalue(),
            )
            supervisor.shutdown()

        rendered = output.getvalue()
        self.assertIn("PROCESS WATCHDOG - LIVE STATUS", rendered)
        self.assertIn("profile_visible / strategy_visible", rendered)
        self.assertIn("fixture_started", rendered)
        self.assertIn("RUNNING", rendered)
        self.assertIn("Restarts 0", rendered)

    def _spec(
        self,
        process_id: str,
        mode: str,
        *,
        extra: tuple[str, ...] = (),
        restart: RestartPolicy | None = None,
        fatal_output_patterns: tuple[str, ...] = (),
        heartbeat_file: Path | None = None,
        heartbeat_timeout_seconds: float | None = None,
        startup_grace_seconds: float = 0,
    ) -> ProcessSpec:
        return ProcessSpec(
            process_id=process_id,
            enabled=True,
            profile=f"profile_{process_id}",
            strategy=f"strategy_{process_id}",
            command=(
                sys.executable,
                "-u",
                str(FIXTURE),
                "--mode",
                mode,
                *extra,
            ),
            working_directory=self.root,
            restart=restart or self._restart_policy(),
            fatal_output_patterns=fatal_output_patterns,
            heartbeat_file=heartbeat_file,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            startup_grace_seconds=startup_grace_seconds,
            log_file=self.root / "logs" / f"{process_id}.log",
        )

    def _restart_policy(self, *, maximum_restarts: int = 3) -> RestartPolicy:
        return RestartPolicy(
            restart_on_failure=True,
            restart_on_clean_exit=False,
            delay_seconds=0.03,
            maximum_delay_seconds=0.05,
            maximum_restarts=maximum_restarts,
            restart_window_seconds=2,
            stable_run_seconds=5,
            graceful_shutdown_seconds=0.3,
        )

    def _supervisor(self, *specs: ProcessSpec) -> ProcessSupervisor:
        return self._supervisor_with_port(0, *specs)

    def _supervisor_with_port(
        self,
        port: int,
        *specs: ProcessSpec,
        console_status_interval_seconds: float = 0,
        console_show_child_output: bool = False,
    ) -> ProcessSupervisor:
        settings = WatchdogSettings(
            config_path=self.root / "watchdog.json",
            project_root=self.root,
            strategy_config_path=self.root / "strategy.json",
            runtime_directory=self.root / "runtime",
            log_directory=self.root / "logs",
            poll_interval_seconds=0.01,
            control_host="127.0.0.1",
            control_port=port,
            log_max_bytes=100_000,
            log_backup_count=2,
            processes=tuple(specs),
            console_status_interval_seconds=console_status_interval_seconds,
            console_show_child_output=console_show_child_output,
        )
        supervisor = ProcessSupervisor(settings)
        self.supervisors.append(supervisor)
        return supervisor

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _wait_for(
        self,
        supervisor: ProcessSupervisor,
        predicate: Callable[[], bool],
        timeout: float = 5,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            supervisor.poll_once()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"condition not met; status={supervisor.status()}")

    @staticmethod
    def _state(supervisor: ProcessSupervisor, process_id: str) -> dict[str, object]:
        return supervisor.status(process_id)["processes"][0]


if __name__ == "__main__":
    unittest.main()
