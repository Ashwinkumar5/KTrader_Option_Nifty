from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from importlib import import_module
from typing import Any, cast

from ktrader_simulator.config import ConfigurationError, load_settings


async def run() -> None:
    simulator_settings = load_settings()
    if not simulator_settings.bot_order_intake_enabled:
        raise ConfigurationError("KTRADER_BOT_ORDER_INTAKE_ENABLED must be true")

    bot_root = str(simulator_settings.bot_root)
    if bot_root not in sys.path:
        sys.path.insert(0, bot_root)

    bot_config = import_module("app.core.config")
    bot_logging = import_module("app.core.logging")
    bot_registry = import_module("app.broker.registry")
    bot_worker = import_module("app.workers.market_data_worker")
    load_bot_settings = cast(Callable[[], object], bot_config.load_settings)
    configure_logging = cast(Callable[[str], None], bot_logging.configure_logging)
    configuration_errors = cast(
        Callable[[object], Sequence[str]],
        bot_registry.broker_configuration_errors,
    )
    run_worker = cast(Callable[..., Awaitable[None]], bot_worker.run_market_data_worker)

    settings = replace(
        cast(Any, load_bot_settings()),
        simulator_ipc_enabled=True,
        simulator_ipc_endpoint=simulator_settings.bot_ipc_endpoint,
        simulator_ipc_host=simulator_settings.bot_ipc_host,
        simulator_ipc_port=simulator_settings.bot_ipc_port,
    )
    log_level = str(getattr(settings, "log_level", "INFO"))
    configure_logging(log_level)
    errors = tuple(configuration_errors(settings))
    if errors:
        raise RuntimeError("Bot configuration is incomplete: " + "; ".join(errors))

    print(
        f"Bot signal IPC enabled: {simulator_settings.bot_ipc_endpoint} "
        f"at {simulator_settings.bot_ipc_host}:{simulator_settings.bot_ipc_port}"
    )
    print(
        "Central signal router required at "
        f"{getattr(settings, 'signal_router_host', '127.0.0.1')}:"
        f"{getattr(settings, 'signal_router_port', 47820)}"
    )
    await run_worker(settings=settings)


def main() -> None:
    try:
        asyncio.run(run())
    except (ConfigurationError, RuntimeError) as exc:
        raise SystemExit(f"Bot IPC runner failed: {exc}") from exc


if __name__ == "__main__":
    main()
