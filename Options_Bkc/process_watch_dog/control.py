from __future__ import annotations

import json
import socket
import socketserver
from threading import Thread
from typing import Any, Protocol


class SupervisorControl(Protocol):
    def status(self, process_id: str | None = None) -> dict[str, Any]: ...
    def start_process(self, process_id: str) -> dict[str, Any]: ...
    def stop_process(self, process_id: str) -> dict[str, Any]: ...
    def restart_process(self, process_id: str) -> dict[str, Any]: ...
    def start_all(self) -> dict[str, Any]: ...
    def stop_all(self) -> dict[str, Any]: ...
    def request_shutdown(self) -> None: ...


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(65_537)
            if not raw or len(raw) > 65_536:
                raise ValueError("control request is empty or too large")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("control request must be an object")
            result = self.server.dispatch(request)  # type: ignore[attr-defined]
            response = {"ok": True, "result": result}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class _ThreadingControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        supervisor: SupervisorControl,
    ) -> None:
        self.supervisor = supervisor
        super().__init__(address, _ControlHandler)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command") or "").strip().lower()
        process_id = request.get("process_id")
        if command == "status":
            return self.supervisor.status(
                str(process_id) if process_id is not None else None
            )
        if command in {"start", "stop", "restart"}:
            if not isinstance(process_id, str) or not process_id:
                raise ValueError(f"{command} requires process_id")
            return getattr(self.supervisor, f"{command}_process")(process_id)
        if command == "start-all":
            return self.supervisor.start_all()
        if command == "stop-all":
            return self.supervisor.stop_all()
        if command == "shutdown":
            self.supervisor.request_shutdown()
            return {"status": "shutdown_requested"}
        raise ValueError(f"unsupported control command: {command}")


class ControlServer:
    def __init__(self, host: str, port: int, supervisor: SupervisorControl) -> None:
        self._server = _ThreadingControlServer((host, port), supervisor)
        self._thread = Thread(
            target=self._server.serve_forever,
            name="watchdog-control",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


def send_control_request(
    host: str,
    port: int,
    *,
    command: str,
    process_id: str | None = None,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    request: dict[str, Any] = {"command": command}
    if process_id is not None:
        request["process_id"] = process_id
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            response_file = sock.makefile("rb")
            raw = response_file.readline(1_000_001)
    except OSError as exc:
        raise RuntimeError(
            f"cannot contact watchdog at {host}:{port}; is it running? ({exc})"
        ) from exc
    if not raw or len(raw) > 1_000_000:
        raise RuntimeError("invalid response from watchdog control server")
    response = json.loads(raw.decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "watchdog request failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("watchdog returned an invalid result")
    return result

