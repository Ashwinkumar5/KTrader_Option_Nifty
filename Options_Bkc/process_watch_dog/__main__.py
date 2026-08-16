from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG_PATH, ConfigurationError, load_watchdog_settings
from .control import send_control_request
from .strategy_catalog import StrategyCatalog, StrategyCatalogError
from .supervisor import ProcessSupervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="process_watch_dog",
        description="Supervise configured strategy/profile bot processes.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Watchdog JSON configuration.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("validate", help="Validate config without launching bots.")
    subparsers.add_parser("catalog", help="List profiles and enabled strategies.")
    subparsers.add_parser("run", help="Run the watchdog in the foreground.")

    status = subparsers.add_parser("status", help="Show live watchdog status.")
    status.add_argument("process_id", nargs="?")
    status.add_argument("--json", action="store_true", dest="as_json")

    for action in ("start", "stop", "restart"):
        command = subparsers.add_parser(action, help=f"{action.title()} one process.")
        command.add_argument("process_id")
    subparsers.add_parser("start-all", help="Start all enabled processes.")
    subparsers.add_parser("stop-all", help="Intentionally stop all processes.")
    subparsers.add_parser("shutdown", help="Stop bots and the watchdog.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_watchdog_settings(args.config)
        if args.action == "validate":
            print(
                f"Valid: {len(settings.processes)} process(es) expanded from "
                f"{settings.config_path}"
            )
            for spec in settings.processes:
                state = "enabled" if spec.enabled else "disabled"
                print(
                    f"- {spec.process_id}: {spec.profile} / {spec.strategy} "
                    f"[{state}]"
                )
            return 0
        if args.action == "catalog":
            _print_catalog(settings.strategy_config_path)
            return 0
        if args.action == "run":
            supervisor = ProcessSupervisor(settings)
            try:
                supervisor.run()
            except KeyboardInterrupt:
                supervisor.request_shutdown()
                supervisor.shutdown()
            return 0

        process_id = getattr(args, "process_id", None)
        result = send_control_request(
            settings.control_host,
            settings.control_port,
            command=args.action,
            process_id=process_id,
        )
        if args.action == "status":
            if args.as_json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                _print_status(result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ConfigurationError, StrategyCatalogError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


def _print_catalog(path: Path) -> None:
    catalog = StrategyCatalog.load(path)
    print(f"Strategy configuration: {catalog.source}")
    for profile in catalog.profile_names():
        enabled = tuple(
            (
                f"{item.name}[simulator]"
                if item.publish_to_simulator
                else f"{item.name}[log-only]"
            )
            for item in catalog.strategies(profile)
            if item.enabled
        )
        watchdog = "Y" if catalog.watchdog_enabled(profile) else "N"
        print(
            f"- {profile}: watchdog={watchdog}; "
            f"strategies={', '.join(enabled) if enabled else '(none enabled)'}"
        )


def _print_status(result: dict[str, Any]) -> None:
    processes = result.get("processes", [])
    if not processes:
        print("No managed processes.")
        return
    headers = ("ID", "STATUS", "PID", "PROFILE", "STRATEGY", "RESTARTS")
    rows = [
        (
            str(item.get("id", "")),
            str(item.get("status", "")),
            str(item.get("pid") or "-"),
            str(item.get("profile", "")),
            str(item.get("strategy", "")),
            str(item.get("restart_count", 0)),
        )
        for item in processes
        if isinstance(item, dict)
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


if __name__ == "__main__":
    raise SystemExit(main())
