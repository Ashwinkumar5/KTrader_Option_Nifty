from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from ktrader_simulator.domain.models import OptionType
from ktrader_simulator.intake.ipc import (
    BotOrderSignal,
    BotSignalIpcPublisher,
    BotSignalIpcServer,
    send_buy_event,
)


def _signal() -> BotOrderSignal:
    return BotOrderSignal(
        underlying="NIFTY",
        strike=Decimal("24500"),
        option_type=OptionType.CALL,
        captured_at=datetime.now(UTC),
        signal_id="bot-test-signal-1",
        profile="cross_strike_confirmed_impulse_research",
        strategy="OPTION_CHAIN_IMPULSE",
    )


def _server() -> BotSignalIpcServer:
    return BotSignalIpcServer(
        endpoint="KTraderUI",
        host="127.0.0.1",
        port=0,
        queue_capacity=16,
        max_age_seconds=Decimal("30"),
    )


def test_buy_event_is_queued_and_wakes_the_consumer() -> None:
    async def exercise() -> None:
        server = _server()
        wake = asyncio.Event()
        await server.start(wake)
        try:
            signal = _signal()
            reply = await send_buy_event(
                endpoint="KTraderUI",
                host="127.0.0.1",
                port=server.port,
                signal=signal,
            )

            await asyncio.wait_for(wake.wait(), timeout=0.5)
            assert reply == "OK"
            assert server.drain() == (signal,)
        finally:
            await server.close()

    asyncio.run(exercise())


def test_live_store_publishes_only_qualified_bot_signals() -> None:
    async def exercise() -> None:
        server = _server()
        wake = asyncio.Event()
        await server.start(wake)
        publisher = BotSignalIpcPublisher(
            endpoint="KTraderUI",
            host="127.0.0.1",
            port=server.port,
        )
        try:
            await publisher.publish_chain_snapshot({"ignored": True})
            await publisher.publish_analytics_snapshot(
                {
                    "underlying": "NIFTY",
                    "captured_at": datetime.now(UTC),
                    "signal": "HOLD",
                    "target_strike": "24500",
                }
            )
            assert server.drain() == ()

            await publisher.publish_analytics_snapshot(
                {
                    "underlying": "NIFTY",
                    "captured_at": datetime.now(UTC),
                    "signal": "BUY_PUT",
                    "target_strike": "24500",
                }
            )
            await asyncio.wait_for(wake.wait(), timeout=0.5)
            signals = server.drain()

            assert len(signals) == 1
            assert signals[0].strike == Decimal("24500")
            assert signals[0].option_type == OptionType.PUT
        finally:
            await publisher.close()
            await server.close()

    asyncio.run(exercise())
