"""Run the singleton broker-owning market-data feed service."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.broker.registry import broker_configuration_errors
from app.core.config import load_settings
from app.core.logging import configure_logging
from app.marketdata.feed_handler import EmbeddedMarketDataFeedHandler
from app.marketdata.feed_tape import MarketDataFeedTape
from app.marketdata.nats_transport import NatsMarketDataPublisher
from app.workers.market_data_feed_service import run_market_data_feed_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start the singleton broker market-data publisher."
    )
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        help="Optional watchdog heartbeat file.",
    )
    parser.add_argument(
        "--max-ticks",
        type=_positive_integer,
        help=argparse.SUPPRESS,
    )
    return parser


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    errors = broker_configuration_errors(settings)
    if errors:
        details = "; ".join(errors)
        raise RuntimeError(
            f"{settings.broker_name} broker configuration is incomplete: "
            f"{details}"
        )

    market_date = datetime.now(
        ZoneInfo(settings.market_timezone)
    ).date().isoformat()
    tape_path = (
        Path(settings.market_data_feed_tape_directory)
        / f"market_data_feed_{market_date}.jsonl"
    )
    feed_handler = EmbeddedMarketDataFeedHandler(settings=settings)
    publisher = NatsMarketDataPublisher(
        nats_url=settings.nats_url,
        subject_prefix=settings.market_data_subject_prefix,
        queue_capacity=settings.market_data_bus_queue_capacity,
        connect_timeout_seconds=(
            settings.market_data_bootstrap_timeout_seconds
        ),
    )
    tape = MarketDataFeedTape(
        tape_path,
        queue_capacity=settings.market_data_bus_queue_capacity * 2,
    )
    print(
        "Starting singleton market-data feed handler: "
        f"NATS={settings.nats_url}, "
        f"interval={settings.market_data_feed_interval_ms}ms, "
        f"tape={tape_path}"
    )
    await run_market_data_feed_service(
        settings=settings,
        feed_handler=feed_handler,
        publisher=publisher,
        tape=tape,
        heartbeat_file=args.heartbeat_file,
        max_ticks=args.max_ticks,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Market-data feed handler stopped by user.")
    except Exception as exc:
        print(
            "MKT_DATA_FEED_HANDLER_FATAL "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise
