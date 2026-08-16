from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .config import ProcessSpec, WatchdogSettings
from .control import ControlServer
from .process_control import (
    InstanceLock,
    ProcessJob,
    popen_platform_options,
    terminate_process_tree,
)


@dataclass(frozen=True)
class OutputEvent:
    process_id: str
    stream: str
    line: str


@dataclass
class ManagedProcess:
    spec: ProcessSpec
    status: str = "disabled"
    process: subprocess.Popen[str] | None = None
    job: ProcessJob | None = None
    desired_running: bool = False
    manual_stop: bool = False
    restart_requested: bool = False
    scheduled_start_at: float | None = None
    launched_at: float | None = None
    launched_epoch: float | None = None
    last_output_at: float | None = None
    last_heartbeat_at: float | None = None
    pending_failure_reason: str | None = None
    last_failure_reason: str | None = None
    last_exit_code: int | None = None
    last_started_at: str | None = None
    last_exited_at: str | None = None
    launch_count: int = 0
    restart_count: int = 0
    failure_streak: int = 0
    restart_history: deque[float] = field(default_factory=deque)
    pid_history: list[int] = field(default_factory=list)
    last_output_line: str | None = None
    last_event: str = "Waiting to start"


class ProcessSupervisor:
    def __init__(self, settings: WatchdogSettings) -> None:
        self.settings = settings
        self._managed = {
            spec.process_id: ManagedProcess(
                spec=spec,
                status="stopped_by_user" if spec.enabled else "disabled",
            )
            for spec in settings.processes
        }
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._output_events: queue.Queue[OutputEvent] = queue.Queue()
        self._instance_lock = InstanceLock(settings.lock_file)
        self._control_server: ControlServer | None = None
        self._shutdown_complete = False
        self._logger = self._build_logger()
        self._next_console_status_at = time.monotonic()

    def run(self) -> None:
        self._instance_lock.acquire()
        try:
            if self.settings.control_port:
                self._control_server = ControlServer(
                    self.settings.control_host,
                    self.settings.control_port,
                    self,
                )
                self._control_server.start()
            self._logger.info(
                "watchdog_started pid=%s config=%s registered=%s",
                os.getpid(),
                self.settings.config_path,
                len(self._managed),
            )
            self.start_all()
            with self._lock:
                now = time.monotonic()
                self._print_console_status_locked(now)
                self._schedule_next_console_status(now)
            while not self._stop_event.wait(self.settings.poll_interval_seconds):
                self.poll_once()
        finally:
            self.shutdown()

    def request_shutdown(self) -> None:
        self._stop_event.set()

    def start_all(self) -> dict[str, Any]:
        started: list[str] = []
        with self._lock:
            for process_id, managed in self._managed.items():
                if not managed.spec.enabled:
                    continue
                if managed.process is None and not managed.desired_running:
                    self._start_process_locked(managed, reset_failures=True)
                    started.append(process_id)
            self._persist_state_locked()
        return {"started": started}

    def stop_all(self) -> dict[str, Any]:
        stopped: list[str] = []
        with self._lock:
            # Stop dependants before the infrastructure they consume. The
            # dictionary preserves the configured startup order, so reversing
            # it gives us a deterministic and quiet shutdown sequence.
            for process_id, managed in reversed(tuple(self._managed.items())):
                if managed.process is not None or managed.desired_running:
                    self._stop_process_locked(managed, intentional=True)
                    stopped.append(process_id)
            self._persist_state_locked()
        return {"stopped": stopped}

    def start_process(self, process_id: str) -> dict[str, Any]:
        with self._lock:
            managed = self._require_process(process_id)
            if not managed.spec.enabled:
                raise ValueError(f"process {process_id} is disabled in run_process")
            if managed.process is not None:
                return {"process_id": process_id, "status": managed.status}
            self._start_process_locked(managed, reset_failures=True)
            self._persist_state_locked()
            return self._one_status_locked(managed)

    def stop_process(self, process_id: str) -> dict[str, Any]:
        with self._lock:
            managed = self._require_process(process_id)
            self._stop_process_locked(managed, intentional=True)
            self._observe_exit_locked(managed, time.monotonic())
            self._persist_state_locked()
            return self._one_status_locked(managed)

    def restart_process(self, process_id: str) -> dict[str, Any]:
        with self._lock:
            managed = self._require_process(process_id)
            if not managed.spec.enabled:
                raise ValueError(f"process {process_id} is disabled in run_process")
            managed.desired_running = True
            managed.manual_stop = False
            managed.restart_requested = True
            managed.restart_history.clear()
            managed.failure_streak = 0
            if managed.process is not None:
                self._terminate_locked(managed, reason="manual_restart")
                self._observe_exit_locked(managed, time.monotonic())
            else:
                managed.restart_requested = False
                self._spawn_locked(managed)
            self._persist_state_locked()
            return self._one_status_locked(managed)

    def status(self, process_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if process_id is not None:
                return {"processes": [self._one_status_locked(self._require_process(process_id))]}
            return {
                "watchdog_pid": os.getpid(),
                "shutdown_requested": self._stop_event.is_set(),
                "processes": [
                    self._one_status_locked(self._managed[key])
                    for key in sorted(self._managed)
                ],
            }

    def poll_once(self) -> None:
        with self._lock:
            changed = self._drain_output_locked()
            now = time.monotonic()
            for managed in self._managed.values():
                if managed.process is not None:
                    if managed.process.poll() is not None:
                        self._handle_exit_locked(managed, now)
                        changed = True
                        continue
                    reason = self._health_failure_locked(managed, now)
                    if reason is not None:
                        self._logger.error(
                            "process_health_failed id=%s pid=%s reason=%s",
                            managed.spec.process_id,
                            managed.process.pid,
                            reason,
                        )
                        managed.last_event = f"Inactive: {reason}; restarting"
                        managed.pending_failure_reason = reason
                        self._terminate_locked(managed, reason=reason)
                        self._observe_exit_locked(managed, now)
                        changed = True
                elif (
                    managed.desired_running
                    and managed.scheduled_start_at is not None
                    and now >= managed.scheduled_start_at
                ):
                    self._spawn_locked(managed)
                    changed = True
            if self._console_status_due(now):
                self._print_console_status_locked(now)
                self._schedule_next_console_status(now)
            if changed:
                self._persist_state_locked()

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown_complete:
                return
            self._stop_event.set()
            for managed in reversed(tuple(self._managed.values())):
                if managed.process is not None:
                    self._stop_process_locked(managed, intentional=True)
                    self._observe_exit_locked(managed, time.monotonic())
            self._drain_output_locked()
            self._persist_state_locked()
            self._shutdown_complete = True
        if self._control_server is not None:
            self._control_server.close()
            self._control_server = None
        self._logger.info("watchdog_stopped pid=%s", os.getpid())
        self._clear_console()
        self._console("PROCESS WATCHDOG STOPPED - all managed processes are stopped.")
        for handler in tuple(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
        self._instance_lock.release()

    def _start_process_locked(
        self,
        managed: ManagedProcess,
        *,
        reset_failures: bool,
    ) -> None:
        managed.desired_running = True
        managed.manual_stop = False
        managed.restart_requested = False
        managed.scheduled_start_at = None
        if reset_failures:
            managed.restart_history.clear()
            managed.failure_streak = 0
            managed.pending_failure_reason = None
        self._spawn_locked(managed)

    def _spawn_locked(self, managed: ManagedProcess) -> None:
        if managed.process is not None:
            return
        spec = managed.spec
        if spec.log_file is not None:
            spec.log_file.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(spec.environment)
        managed.status = "starting"
        managed.scheduled_start_at = None
        try:
            process = subprocess.Popen(
                list(spec.command),
                cwd=spec.working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **popen_platform_options(),
            )
        except OSError as exc:
            managed.last_failure_reason = f"spawn_error:{type(exc).__name__}:{exc}"
            managed.pending_failure_reason = managed.last_failure_reason
            self._logger.exception(
                "process_spawn_failed id=%s command=%r",
                spec.process_id,
                spec.command,
            )
            self._schedule_restart_locked(managed, time.monotonic())
            return

        managed.process = process
        managed.job = ProcessJob.attach(process)
        managed.status = "running"
        managed.launched_at = time.monotonic()
        managed.launched_epoch = time.time()
        managed.last_output_at = managed.launched_at
        managed.last_heartbeat_at = None
        managed.pending_failure_reason = None
        managed.last_exit_code = None
        managed.last_started_at = _utc_now()
        managed.launch_count += 1
        managed.pid_history.append(process.pid)
        if len(managed.pid_history) > 20:
            del managed.pid_history[:-20]
        self._start_output_reader(spec.process_id, "stdout", process.stdout)
        self._start_output_reader(spec.process_id, "stderr", process.stderr)
        self._logger.info(
            "process_started id=%s pid=%s profile=%s strategy=%s command=%r",
            spec.process_id,
            process.pid,
            spec.profile,
            spec.strategy,
            spec.command,
        )
        managed.last_event = f"Started with PID {process.pid}"

    def _stop_process_locked(
        self,
        managed: ManagedProcess,
        *,
        intentional: bool,
    ) -> None:
        managed.desired_running = False
        managed.manual_stop = intentional
        managed.restart_requested = False
        managed.scheduled_start_at = None
        if managed.process is None:
            managed.status = "stopped_by_user" if intentional else "failed"
            return
        self._terminate_locked(
            managed,
            reason="stopped_by_user" if intentional else "stopped",
        )

    def _terminate_locked(self, managed: ManagedProcess, *, reason: str) -> None:
        process = managed.process
        if process is None:
            return
        managed.status = "stopping"
        self._logger.info(
            "process_stopping id=%s pid=%s reason=%s",
            managed.spec.process_id,
            process.pid,
            reason,
        )
        managed.last_event = f"Stopping PID {process.pid}: {reason}"
        terminate_process_tree(
            process,
            job=managed.job,
            graceful_timeout_seconds=managed.spec.restart.graceful_shutdown_seconds,
        )

    def _observe_exit_locked(self, managed: ManagedProcess, now: float) -> None:
        if managed.process is not None and managed.process.poll() is not None:
            self._handle_exit_locked(managed, now)

    def _handle_exit_locked(self, managed: ManagedProcess, now: float) -> None:
        process = managed.process
        if process is None:
            return
        exit_code = process.poll()
        if exit_code is None:
            return
        uptime = (
            max(0.0, now - managed.launched_at)
            if managed.launched_at is not None
            else 0.0
        )
        if managed.job is not None:
            managed.job.close()
        managed.job = None
        managed.process = None
        managed.last_exit_code = exit_code
        managed.last_exited_at = _utc_now()
        if uptime >= managed.spec.restart.stable_run_seconds:
            managed.failure_streak = 0
            managed.restart_history.clear()

        self._logger.warning(
            "process_exited id=%s pid=%s exit_code=%s uptime_seconds=%.3f reason=%s",
            managed.spec.process_id,
            process.pid,
            exit_code,
            uptime,
            managed.pending_failure_reason,
        )
        managed.last_event = (
            f"Exited PID {process.pid}, code {exit_code}: "
            f"{managed.pending_failure_reason or 'process_exit'}"
        )

        if managed.restart_requested:
            managed.restart_requested = False
            managed.status = "backoff"
            managed.scheduled_start_at = now
            managed.pending_failure_reason = None
            return
        if self._stop_event.is_set() or managed.manual_stop or not managed.desired_running:
            managed.status = "stopped_by_user"
            managed.pending_failure_reason = None
            return

        forced_failure = managed.pending_failure_reason is not None
        failure_reason = managed.pending_failure_reason or f"exit_code:{exit_code}"
        managed.last_failure_reason = failure_reason
        managed.pending_failure_reason = None
        should_restart = (
            managed.spec.restart.restart_on_failure
            if forced_failure or exit_code != 0
            else managed.spec.restart.restart_on_clean_exit
        )
        if should_restart:
            self._schedule_restart_locked(managed, now)
        else:
            managed.desired_running = False
            managed.status = (
                "failed" if forced_failure or exit_code != 0 else "completed"
            )

    def _schedule_restart_locked(self, managed: ManagedProcess, now: float) -> None:
        policy = managed.spec.restart
        cutoff = now - policy.restart_window_seconds
        while managed.restart_history and managed.restart_history[0] < cutoff:
            managed.restart_history.popleft()
        if len(managed.restart_history) >= policy.maximum_restarts:
            managed.status = "crash_loop"
            managed.desired_running = False
            managed.scheduled_start_at = None
            self._logger.error(
                "process_crash_loop id=%s restarts=%s window_seconds=%s",
                managed.spec.process_id,
                len(managed.restart_history),
                policy.restart_window_seconds,
            )
            managed.last_event = (
                f"Crash loop: stopped after {len(managed.restart_history)} "
                "rapid restart(s)"
            )
            return
        delay = min(
            policy.delay_seconds * (2**managed.failure_streak),
            policy.maximum_delay_seconds,
        )
        managed.failure_streak += 1
        managed.restart_history.append(now)
        managed.restart_count += 1
        managed.status = "backoff"
        managed.scheduled_start_at = now + delay
        self._logger.warning(
            "process_restart_scheduled id=%s attempt=%s delay_seconds=%.3f reason=%s",
            managed.spec.process_id,
            managed.restart_count,
            delay,
            managed.last_failure_reason,
        )
        managed.last_event = (
            f"Restarting in {delay:.1f}s: {managed.last_failure_reason}"
        )

    def _drain_output_locked(self) -> bool:
        changed = False
        while True:
            try:
                event = self._output_events.get_nowait()
            except queue.Empty:
                break
            managed = self._managed.get(event.process_id)
            if managed is None:
                continue
            changed = True
            managed.last_output_at = time.monotonic()
            managed.last_output_line = event.line
            self._append_process_log(managed, event)
            if self.settings.console_show_child_output:
                self._console(
                    f"[BOT][{event.process_id}][{event.stream}] {event.line}"
                )
            if managed.process is None or managed.pending_failure_reason is not None:
                continue
            for pattern in managed.spec.fatal_output_patterns:
                if re.search(pattern, event.line, re.IGNORECASE):
                    managed.pending_failure_reason = f"fatal_output:{pattern}"
                    self._logger.error(
                        "fatal_output_detected id=%s pid=%s pattern=%r line=%r",
                        managed.spec.process_id,
                        managed.process.pid,
                        pattern,
                        event.line[:500],
                    )
                    managed.last_event = (
                        f"Broker failure matched {pattern!r}; restarting"
                    )
                    self._terminate_locked(managed, reason=managed.pending_failure_reason)
                    break
        return changed

    def _health_failure_locked(
        self,
        managed: ManagedProcess,
        now: float,
    ) -> str | None:
        if managed.launched_at is None:
            return None
        running_for = now - managed.launched_at
        if running_for < managed.spec.startup_grace_seconds:
            return None

        heartbeat_file = managed.spec.heartbeat_file
        heartbeat_timeout = managed.spec.heartbeat_timeout_seconds
        if heartbeat_file is not None and heartbeat_timeout is not None:
            try:
                modified_epoch = heartbeat_file.stat().st_mtime
            except OSError:
                modified_epoch = None
            if (
                modified_epoch is not None
                and managed.launched_epoch is not None
                and modified_epoch >= managed.launched_epoch - 0.5
            ):
                observed = managed.launched_at + (modified_epoch - managed.launched_epoch)
                managed.last_heartbeat_at = max(
                    managed.last_heartbeat_at or managed.launched_at,
                    observed,
                )
            heartbeat_reference = managed.last_heartbeat_at or managed.launched_at
            if now - heartbeat_reference > heartbeat_timeout:
                return f"heartbeat_timeout:{heartbeat_timeout:g}s"

        output_timeout = managed.spec.output_idle_timeout_seconds
        if output_timeout is not None:
            output_reference = managed.last_output_at or managed.launched_at
            if now - output_reference > output_timeout:
                return f"output_idle_timeout:{output_timeout:g}s"
        return None

    def _start_output_reader(
        self,
        process_id: str,
        stream_name: str,
        stream: TextIO | None,
    ) -> None:
        if stream is None:
            return

        def read_output() -> None:
            try:
                for line in iter(stream.readline, ""):
                    self._output_events.put(
                        OutputEvent(
                            process_id=process_id,
                            stream=stream_name,
                            line=line.rstrip("\r\n"),
                        )
                    )
            finally:
                stream.close()

        threading.Thread(
            target=read_output,
            name=f"watchdog-{process_id}-{stream_name}",
            daemon=True,
        ).start()

    def _append_process_log(self, managed: ManagedProcess, event: OutputEvent) -> None:
        path = managed.spec.log_file
        if path is None:
            return
        message = f"{_utc_now()} [{event.stream}] {event.line}\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(message.encode("utf-8")) > self.settings.log_max_bytes:
                _rotate_file(path, self.settings.log_backup_count)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(message)
        except OSError as exc:
            self._logger.error(
                "process_log_write_failed id=%s path=%s error=%s",
                managed.spec.process_id,
                path,
                exc,
            )

    def _one_status_locked(self, managed: ManagedProcess) -> dict[str, Any]:
        pid = managed.process.pid if managed.process is not None else None
        uptime = (
            max(0.0, time.monotonic() - managed.launched_at)
            if managed.process is not None and managed.launched_at is not None
            else None
        )
        restart_in = (
            max(0.0, managed.scheduled_start_at - time.monotonic())
            if managed.scheduled_start_at is not None
            else None
        )
        return {
            "id": managed.spec.process_id,
            "enabled": managed.spec.enabled,
            "profile": managed.spec.profile,
            "strategy": managed.spec.strategy,
            "status": managed.status,
            "pid": pid,
            "uptime_seconds": round(uptime, 3) if uptime is not None else None,
            "restart_in_seconds": (
                round(restart_in, 3) if restart_in is not None else None
            ),
            "launch_count": managed.launch_count,
            "restart_count": managed.restart_count,
            "last_exit_code": managed.last_exit_code,
            "last_failure_reason": managed.last_failure_reason,
            "last_started_at": managed.last_started_at,
            "last_exited_at": managed.last_exited_at,
            "pid_history": list(managed.pid_history),
            "command": list(managed.spec.command),
        }

    def _print_console_status_locked(self, now: float) -> None:
        if self.settings.console_status_interval_seconds <= 0:
            return
        self._clear_console()
        running = sum(
            1
            for item in self._managed.values()
            if item.process is not None and item.process.poll() is None
        )
        expected = sum(1 for item in self._managed.values() if item.desired_running)
        self._console("PROCESS WATCHDOG - LIVE STATUS")
        self._console(
            f"Updated: {_utc_now()} | Watchdog PID: {os.getpid()} | "
            f"Running: {running}/{expected} | Registered: {len(self._managed)}"
        )
        self._console("=" * 100)
        for process_id in sorted(self._managed):
            managed = self._managed[process_id]
            pid = managed.process.pid if managed.process is not None else "-"
            uptime = (
                _duration(now - managed.launched_at)
                if managed.process is not None and managed.launched_at is not None
                else "-"
            )
            if managed.process is not None and managed.last_output_at is not None:
                output_age = f"{max(0.0, now - managed.last_output_at):.0f}s ago"
            else:
                output_age = "-"
            self._console(
                f"[{managed.status.upper():15}] PID {str(pid):>6} | "
                f"Uptime {uptime:>8} | Output {output_age:>8} | "
                f"Restarts {managed.restart_count}"
            )
            self._console(
                f"  {managed.spec.profile} / {managed.spec.strategy}"
            )
            self._console(
                f"  Process: {process_id}"
            )
            self._console(
                f"  Latest:  {_shorten(managed.last_output_line or '(no bot output yet)', 88)}"
            )
            self._console(
                f"  Event:   {_shorten(managed.last_event, 88)}"
            )
            self._console("-" * 100)
        self._console(
            "Bot output continues in process_watch_dog\\logs. "
            "This screen refreshes automatically; Ctrl+C stops all bots."
        )

    def _console_status_due(self, now: float) -> bool:
        return (
            self.settings.console_status_interval_seconds > 0
            and now >= self._next_console_status_at
        )

    def _schedule_next_console_status(self, now: float) -> None:
        interval = self.settings.console_status_interval_seconds
        self._next_console_status_at = now + interval if interval > 0 else float("inf")

    @staticmethod
    def _console(message: str) -> None:
        try:
            print(message, flush=True)
        except (BrokenPipeError, OSError):
            pass

    @staticmethod
    def _clear_console() -> None:
        if not sys.stdout.isatty():
            return
        if os.name == "nt":
            os.system("cls")
        else:
            print("\033[2J\033[H", end="", flush=True)

    def _persist_state_locked(self) -> None:
        self.settings.runtime_directory.mkdir(parents=True, exist_ok=True)
        payload = self.status()
        payload["updated_at"] = _utc_now()
        temporary = self.settings.state_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.settings.state_file)

    def _require_process(self, process_id: str) -> ManagedProcess:
        try:
            return self._managed[process_id]
        except KeyError as exc:
            raise ValueError(f"unknown managed process: {process_id}") from exc

    def _build_logger(self) -> logging.Logger:
        self.settings.log_directory.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"process_watch_dog.{id(self)}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.settings.log_directory / "watchdog.log",
            maxBytes=self.settings.log_max_bytes,
            backupCount=self.settings.log_backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        return logger


def _rotate_file(path: Path, backups: int) -> None:
    oldest = path.with_name(f"{path.name}.{backups}")
    oldest.unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def _shorten(value: str, width: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= width:
        return normalized
    return normalized[: width - 3] + "..."
