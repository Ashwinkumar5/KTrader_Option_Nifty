from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from decimal import Decimal

from app.execution.simulator_ipc import (
    SimulatorDeliveryOutcome,
    SimulatorEntryPublisher,
    SimulatorEntrySignal,
)


class SimulatorEntryPublisherTests(unittest.TestCase):
    def test_qualified_entry_is_sent_using_ktrader_contract(self) -> None:
        async def exercise() -> None:
            received: list[dict[str, object]] = []
            outcomes: list[SimulatorDeliveryOutcome] = []
            delivered = asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                received.append(json.loads(await reader.readline()))
                writer.write(b"OK\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                delivered.set()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            publisher = SimulatorEntryPublisher(
                port=port,
                on_result=outcomes.append,
            )
            captured_at = datetime(2026, 8, 4, 4, 15, tzinfo=UTC)
            signal = SimulatorEntrySignal(
                underlying="NIFTY",
                strike=Decimal("24550"),
                side="BUY_PUT",
                captured_at=captured_at,
                profile="cross_strike_confirmed_impulse_research",
                strategy="OPTION_CHAIN_IMPULSE",
            )
            try:
                queued = publisher.publish(signal)
                self.assertTrue(queued)
                await asyncio.wait_for(delivered.wait(), timeout=1)
                await publisher.close()
            finally:
                server.close()
                await server.wait_closed()

            self.assertEqual(
                received,
                [
                    {
                        "endpoint": "KTraderUI",
                        "action": "BUY",
                        "signal_id": signal.signal_id,
                        "profile": "cross_strike_confirmed_impulse_research",
                        "strategy": "OPTION_CHAIN_IMPULSE",
                        "underlying": "NIFTY",
                        "strike": "24550",
                        "side": "PUT",
                        "captured_at": captured_at.isoformat(),
                    }
                ],
            )
            self.assertEqual(publisher.health_snapshot()["accepted"], 1)
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0].status, "ACCEPTED")
            self.assertEqual(outcomes[0].signal.signal_id, signal.signal_id)

        asyncio.run(exercise())

    def test_invalid_side_is_rejected_before_queueing(self) -> None:
        with self.assertRaises(ValueError):
            SimulatorEntrySignal(
                underlying="NIFTY",
                strike=Decimal("24550"),
                side="NEUTRAL",
                captured_at=datetime.now(UTC),
            )

    def test_connection_retry_reuses_the_same_signal_id(self) -> None:
        async def exercise() -> None:
            received: list[dict[str, object]] = []
            delivered = asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                received.append(json.loads(await reader.readline()))
                if len(received) == 1:
                    writer.close()
                    await writer.wait_closed()
                    return
                writer.write(b"OK\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                delivered.set()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            publisher = SimulatorEntryPublisher(
                port=port,
                max_retries=1,
            )
            signal = SimulatorEntrySignal(
                underlying="NIFTY",
                strike=Decimal("24550"),
                side="BUY_CALL",
                captured_at=datetime(2026, 8, 11, 4, 15, tzinfo=UTC),
                profile="derivatives_only__runtime",
                strategy="DERIVATIVES_QUANT",
                signal_id="stable-retry-id",
            )
            try:
                self.assertTrue(publisher.publish(signal))
                await asyncio.wait_for(delivered.wait(), timeout=1)
            finally:
                await publisher.close()
                server.close()
                await server.wait_closed()

            self.assertEqual(len(received), 2)
            self.assertEqual(
                [item["signal_id"] for item in received],
                ["stable-retry-id", "stable-retry-id"],
            )
            self.assertEqual(publisher.health_snapshot()["accepted"], 1)
            self.assertEqual(publisher.health_snapshot()["failed"], 0)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
