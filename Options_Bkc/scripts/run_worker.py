"""Command line script to start the market data worker.

Run with:
    python scripts/run_worker.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

# Ensure the repository root is on sys.path so the app package can be imported
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.core.config import load_settings
from app.core.logging import configure_logging
from app.broker.registry import broker_configuration_errors
from app.workers.market_data_worker import run_market_data_worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start the live market-data and strategy worker."
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        help=(
            "Strategy configuration JSON. Defaults to STRATEGY_CONFIG_PATH "
            "or config/strategy_config.json."
        ),
    )
    parser.add_argument(
        "--strategy-profile",
        help=(
            "Base strategy profile. Defaults to STRATEGY_PROFILE or "
            "derivatives_only."
        ),
    )
    parser.add_argument(
        "--strategies",
        help=(
            "Comma-separated strategies to enable exclusively, for example "
            "SMC, OPTION_CHAIN_IMPULSE or "
            "DERIVATIVES_QUANT,GAMMA_EXPANSION. "
            "GAMMA_BLAST is accepted as an alias for GAMMA_EXPANSION."
        ),
    )
    parser.add_argument(
        "--features",
        help=(
            "Comma-separated features to enable exclusively, for example "
            "gamma_concentration,iv_skew,order_book_imbalance. "
            "gamma_blast is accepted as an alias for gamma_concentration."
        ),
    )
    parser.add_argument(
        "--minimum-book-imbalance",
        type=Decimal,
        help=(
            "Optional runtime book-imbalance threshold from 0 through 1. "
            "Defaults to the selected profile."
        ),
    )
    parser.add_argument(
        "--snapshot-interval-ms",
        type=_positive_integer,
        help=(
            "Strategy-frame interval in milliseconds. Defaults to "
            "SNAPSHOT_INTERVAL_MS."
        ),
    )
    parser.add_argument(
        "--market-data-mode",
        choices=("embedded", "nats-subscriber"),
        default="embedded",
        help=(
            "embedded owns a broker session; nats-subscriber consumes the "
            "singleton feed service. Defaults to embedded."
        ),
    )
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        help=(
            "Optional watchdog heartbeat path. It stays fresh while the worker "
            "is idle or completing work, and becomes stale when active work hangs."
        ),
    )
    parser.add_argument(
        "--heartbeat-stall-seconds",
        type=_positive_float,
        default=10.0,
        help=(
            "Active-work duration after which heartbeat updates stop. "
            "Must be shorter than the watchdog heartbeat timeout."
        ),
    )
    return parser


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _csv_selection(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError("a supplied strategy or feature list cannot be empty")
    return items


def _nats_strategy_selection(
    selection: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Require one configured strategy per subscriber process.

    The watchdog expands one strategy process for every enabled strategy in a
    profile.  Keeping the same unit here preserves isolated state, tape, and
    routing policy while every process shares the one market-data feed.
    """

    selected = tuple(item.upper() for item in (selection or ()))
    if len(selected) != 1:
        raise ValueError(
            "nats-subscriber requires exactly one --strategies value; "
            "the watchdog starts one subscriber process per configured strategy"
        )
    return selected


async def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    settings = replace(
        settings,
        strategy_config_path=(
            str(args.strategy_config.resolve())
            if args.strategy_config is not None
            else settings.strategy_config_path
        ),
        strategy_profile=(
            args.strategy_profile or settings.strategy_profile
        ),
        snapshot_interval_ms=(
            args.snapshot_interval_ms or settings.snapshot_interval_ms
        ),
    )
    try:
        enabled_strategies = _csv_selection(args.strategies)
        enabled_features = _csv_selection(args.features)
    except ValueError as exc:
        parser.error(str(exc))
    configure_logging(settings.log_level)

    print("==================================================")
    print("      Starting Market Data Background Worker       ")
    print("==================================================")
    print(f"Broker Name: {settings.broker_name}")
    print(f"Underlyings to Track: {', '.join(settings.default_underlyings)}")
    print(f"Option Window: {settings.option_window_each_side} strikes on each side")
    print(f"Snapshot Interval: {settings.snapshot_interval_ms} ms")
    print(f"Market Data Mode: {args.market_data_mode}")
    if args.heartbeat_file is not None:
        print(
            "Watchdog Heartbeat: "
            f"{args.heartbeat_file} (stall={args.heartbeat_stall_seconds:g}s)"
        )
    print(f"Microstructure: {settings.microstructure_mode.upper()} mode (no broker order placement)")
    print(f"Strategy Profile: {settings.strategy_profile}")
    if enabled_strategies is not None:
        print(f"Strategy Override: {', '.join(enabled_strategies)}")
    if enabled_features is not None:
        print(f"Feature Override: {', '.join(enabled_features)}")
    if args.minimum_book_imbalance is not None:
        print(
            "Book Imbalance Override: "
            f"{args.minimum_book_imbalance}"
        )
    print("--------------------------------------------------")

    feed_handler = None
    if args.market_data_mode == "embedded":
        try:
            configuration_errors = broker_configuration_errors(settings)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            sys.exit(1)
        if configuration_errors:
            print(
                f"ERROR: {settings.broker_name} broker configuration is incomplete:"
            )
            for error in configuration_errors:
                print(f"- {error}")
            sys.exit(1)
        print("Initializing Broker Session and WebSocket connection...")
    else:
        try:
            _nats_strategy_selection(enabled_strategies)
        except ValueError as exc:
            parser.error(str(exc))
        from app.marketdata.nats_transport import NatsMarketDataFeedHandler

        feed_handler = NatsMarketDataFeedHandler(
            nats_url=settings.nats_url,
            subject_prefix=settings.market_data_subject_prefix,
            queue_capacity=settings.market_data_bus_queue_capacity,
            bootstrap_timeout_seconds=(
                settings.market_data_bootstrap_timeout_seconds
            ),
            consumer_interval_ms=settings.snapshot_interval_ms,
            max_tick_lag_seconds=float(
                settings.signal_gate_max_underlying_age_seconds
            ),
            max_frame_lag_seconds=float(
                settings.signal_gate_max_underlying_age_seconds
            ),
        )
        print(
            "Connecting to singleton market-data feed over NATS "
            f"({settings.nats_url})..."
        )
    try:
        await run_market_data_worker(
            settings=settings,
            feed_handler=feed_handler,
            enabled_strategies=enabled_strategies,
            enabled_features=enabled_features,
            minimum_book_imbalance=args.minimum_book_imbalance,
            heartbeat_file=args.heartbeat_file,
            heartbeat_stall_timeout_seconds=args.heartbeat_stall_seconds,
        )
    except KeyboardInterrupt:
        print("\nWorker stopped by user.")
    except Exception as e:
        print(f"\nCRITICAL ERROR: Failed to run market data worker: {e}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
