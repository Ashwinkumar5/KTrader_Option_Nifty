from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

SIMULATOR_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = SIMULATOR_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ktrader_simulator.config import load_settings  # noqa: E402
from ktrader_simulator.domain.models import OptionType  # noqa: E402
from ktrader_simulator.intake.ipc import (  # noqa: E402
    BotOrderSignal,
    send_buy_event,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send one offline BUY event to the running KTrader GUI."
    )
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--strike", required=True, type=_positive_decimal)
    parser.add_argument("--side", required=True, choices=("CALL", "PUT"))
    arguments = parser.parse_args()

    settings = load_settings(simulator_root=SIMULATOR_ROOT)
    if settings.broker_order_execution_enabled:
        raise SystemExit(
            "Offline test refused: BROKER_ORDER_EXECUTION_ENABLED must be false."
        )
    if not settings.bot_order_intake_enabled:
        raise SystemExit(
            "KTRADER_BOT_ORDER_INTAKE_ENABLED must be true in the simulator .env."
        )

    signal = BotOrderSignal(
        underlying=arguments.underlying.strip().upper(),
        strike=arguments.strike,
        option_type=(
            OptionType.CALL if arguments.side == "CALL" else OptionType.PUT
        ),
        captured_at=datetime.now(UTC),
    )
    try:
        reply = asyncio.run(
            send_buy_event(
                endpoint=settings.bot_ipc_endpoint,
                host=settings.bot_ipc_host,
                port=settings.bot_ipc_port,
                signal=signal,
            )
        )
    except (ConnectionError, OSError, TimeoutError) as exc:
        raise SystemExit(
            f"KTraderUI is not reachable at {settings.bot_ipc_host}:"
            f"{settings.bot_ipc_port}: {exc}"
        ) from exc
    if reply != "OK":
        raise SystemExit(f"KTraderUI rejected the event: {reply or 'no response'}")

    print(
        f"Accepted by {settings.bot_ipc_endpoint}: BUY "
        f"{signal.underlying} {signal.strike} {arguments.side}"
    )


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("strike must be numeric") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise argparse.ArgumentTypeError("strike must be positive")
    return parsed


if __name__ == "__main__":
    main()
