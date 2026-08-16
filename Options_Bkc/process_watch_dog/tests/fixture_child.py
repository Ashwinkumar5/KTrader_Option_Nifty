from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("wait", "exit", "fatal", "heartbeat", "grandchild"),
        required=True,
    )
    parser.add_argument("--exit-code", type=int, default=1)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--counter-file", type=Path)
    parser.add_argument("--heartbeat-file", type=Path)
    parser.add_argument("--pid-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    _install_stop_handlers()
    if args.counter_file is not None:
        _increment_counter(args.counter_file)
    print(f"fixture_started pid={os.getpid()} mode={args.mode}", flush=True)
    if args.mode == "exit":
        time.sleep(args.delay)
        return args.exit_code
    if args.mode == "fatal":
        print("BROKER CONNECTION LOST", flush=True)
        _wait_forever()
    if args.mode == "heartbeat":
        if args.heartbeat_file is None:
            raise ValueError("--heartbeat-file is required")
        while True:
            args.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
            args.heartbeat_file.touch()
            time.sleep(max(args.delay, 0.01))
    if args.mode == "grandchild":
        if args.pid_file is None:
            raise ValueError("--pid-file is required")
        child = subprocess.Popen(
            [sys.executable, "-u", __file__, "--mode", "wait"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
        args.pid_file.write_text(str(child.pid), encoding="utf-8")
        _wait_forever()
    _wait_forever()
    return 0


def _increment_counter(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = int(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        current = 0
    path.write_text(str(current + 1), encoding="utf-8")


def _install_stop_handlers() -> None:
    def stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)


def _wait_forever() -> None:
    while True:
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())

