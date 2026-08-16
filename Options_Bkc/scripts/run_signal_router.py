"""Run the single local routing gateway shared by all strategy workers."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.core.config import load_settings
from app.core.logging import configure_logging
from app.execution.signal_router import (
    CentralSignalRouter,
    CentralSignalRouterServer,
    JsonlSignalRouteAudit,
    StrategyRoutingPolicyCatalog,
)
from app.execution.simulator_ipc import SimulatorEntryPublisher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the common strategy-signal router.",
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        help="Strategy configuration used as routing authority.",
    )
    parser.add_argument(
        "--audit-path",
        type=Path,
        help="Append-only JSONL route audit path.",
    )
    return parser


async def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    strategy_config: Path | None = None
    if args.strategy_config is not None:
        strategy_config = args.strategy_config.resolve()
    elif settings.strategy_config_path:
        strategy_config = (
            Path(settings.strategy_config_path).expanduser().resolve()
        )
    audit_path = (
        args.audit_path.resolve()
        if args.audit_path is not None
        else Path(settings.signal_router_audit_path).expanduser().resolve()
    )

    policies = StrategyRoutingPolicyCatalog.from_configuration(
        strategy_config
    )
    audit = JsonlSignalRouteAudit(audit_path)
    publisher = (
        SimulatorEntryPublisher(
            endpoint=settings.simulator_ipc_endpoint,
            host=settings.simulator_ipc_host,
            port=settings.simulator_ipc_port,
            queue_capacity=settings.simulator_ipc_queue_capacity,
            timeout_seconds=settings.simulator_ipc_timeout_seconds,
            max_retries=settings.simulator_ipc_max_retries,
            on_result=audit.record_delivery,
        )
        if settings.simulator_ipc_enabled
        else None
    )
    router = CentralSignalRouter(
        policies=policies,
        publisher=publisher,
        simulator_enabled=settings.simulator_ipc_enabled,
        dedup_capacity=settings.signal_router_dedup_capacity,
        audit_sink=audit.record,
    )
    server = CentralSignalRouterServer(
        router,
        host=settings.signal_router_host,
        port=settings.signal_router_port,
        request_timeout_seconds=settings.signal_router_timeout_seconds,
    )
    try:
        await server.start()
        print(
            "SIGNAL_ROUTER_READY "
            f"host={settings.signal_router_host} "
            f"port={server.bound_port} "
            f"simulator_enabled={settings.simulator_ipc_enabled} "
            f"audit={audit.path}",
            flush=True,
        )
        await server.serve_forever()
    finally:
        await server.close()
        if publisher is not None:
            await publisher.close()
        audit.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Signal router stopped.")
