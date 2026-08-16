from __future__ import annotations

import asyncio
import json
import os
import unittest
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.strategy_config import (
    apply_runtime_strategy_selection,
    load_strategy_configuration,
)
from app.execution.signal_router import (
    CentralSignalRouter,
    CentralSignalRouterClient,
    CentralSignalRouterServer,
    JsonlSignalRouteAudit,
    SignalRouteAudit,
    SignalRouteRequest,
    SignalRouteStatus,
    StrategyRoutePolicy,
    StrategyRoutingPolicyCatalog,
    signal_route_request_from_payload,
    signal_route_request_to_payload,
)
from app.execution.simulator_ipc import (
    SimulatorDeliveryOutcome,
    SimulatorEntryPublisher,
    SimulatorEntrySignal,
)


TEST_TEMP_ROOT = (
    Path(__file__).resolve().parents[1] / ".test-tmp" / "signal-router"
)


class _FakePublisher:
    def __init__(self, results: tuple[bool, ...] = (True,)) -> None:
        self.signals: list[SimulatorEntrySignal] = []
        self._results = deque(results)

    def publish(self, signal: SimulatorEntrySignal) -> bool:
        self.signals.append(signal)
        return self._results.popleft() if self._results else True


def _signal(
    strategy: str,
    *,
    signal_id: str,
    side: str = "BUY_CALL",
) -> SimulatorEntrySignal:
    return SimulatorEntrySignal(
        underlying="NIFTY",
        strike=Decimal("24550"),
        side=side,
        captured_at=datetime(2026, 8, 11, 4, 30, tzinfo=UTC),
        profile="profile_one__runtime",
        strategy=strategy,
        signal_id=signal_id,
    )


def _policies() -> StrategyRoutingPolicyCatalog:
    return StrategyRoutingPolicyCatalog(
        {
            ("profile_one", "ALPHA"): StrategyRoutePolicy(
                enabled=True,
                publish_to_simulator=True,
            ),
            ("profile_one", "BETA"): StrategyRoutePolicy(
                enabled=True,
                publish_to_simulator=False,
            ),
            ("profile_one", "OFF"): StrategyRoutePolicy(
                enabled=False,
                publish_to_simulator=True,
            ),
        }
    )


class CentralSignalRouterTests(unittest.TestCase):
    def test_enabled_signal_is_queued_and_disabled_signal_is_log_only(self) -> None:
        publisher = _FakePublisher()
        audits: list[SignalRouteAudit] = []
        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
            audit_sink=audits.append,
        )

        queued = router.route(
            SignalRouteRequest("profile_one", _signal("ALPHA", signal_id="one"))
        )
        log_only = router.route(
            SignalRouteRequest(
                "profile_one",
                _signal("BETA", signal_id="two", side="BUY_PUT"),
            )
        )

        self.assertEqual(queued.status, SignalRouteStatus.QUEUED)
        self.assertTrue(queued.published)
        self.assertEqual(log_only.status, SignalRouteStatus.LOG_ONLY)
        self.assertFalse(log_only.published)
        self.assertIn("publish_to_simulator is false", log_only.reason)
        self.assertEqual([item.signal_id for item in publisher.signals], ["one"])
        self.assertEqual(
            [audit.result.status for audit in audits],
            [
                SignalRouteStatus.AUTHORIZED,
                SignalRouteStatus.QUEUED,
                SignalRouteStatus.LOG_ONLY,
            ],
        )

    def test_unknown_disabled_and_global_off_all_fail_closed(self) -> None:
        publisher = _FakePublisher()
        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
        )

        unknown = router.route(
            SignalRouteRequest(
                "profile_one",
                _signal("UNKNOWN", signal_id="unknown"),
            )
        )
        disabled = router.route(
            SignalRouteRequest("profile_one", _signal("OFF", signal_id="off"))
        )
        global_off = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=False,
        ).route(
            SignalRouteRequest(
                "profile_one",
                _signal("ALPHA", signal_id="global-off"),
            )
        )

        self.assertEqual(unknown.status, SignalRouteStatus.LOG_ONLY)
        self.assertEqual(disabled.status, SignalRouteStatus.LOG_ONLY)
        self.assertEqual(global_off.status, SignalRouteStatus.LOG_ONLY)
        self.assertEqual(publisher.signals, [])

    def test_duplicate_is_not_published_twice(self) -> None:
        publisher = _FakePublisher()
        audits: list[SignalRouteAudit] = []
        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
            audit_sink=audits.append,
        )
        request = SignalRouteRequest(
            "profile_one",
            _signal("ALPHA", signal_id="same-id"),
        )

        first = router.route(request)
        second = router.route(request)

        self.assertEqual(first.status, SignalRouteStatus.QUEUED)
        self.assertEqual(second.status, SignalRouteStatus.DUPLICATE)
        self.assertEqual(len(publisher.signals), 1)
        self.assertEqual(
            [audit.result.status for audit in audits],
            [
                SignalRouteStatus.AUTHORIZED,
                SignalRouteStatus.QUEUED,
                SignalRouteStatus.DUPLICATE,
            ],
        )

    def test_audit_failure_blocks_simulator_publication(self) -> None:
        publisher = _FakePublisher()

        def fail_audit(_audit: SignalRouteAudit) -> None:
            raise OSError("audit unavailable")

        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
            audit_sink=fail_audit,
        )

        with self.assertRaisesRegex(OSError, "audit unavailable"):
            router.route(
                SignalRouteRequest(
                    "profile_one",
                    _signal("ALPHA", signal_id="audit-failure"),
                )
            )

        self.assertEqual(publisher.signals, [])
        self.assertEqual(router.health_snapshot()["dedup_size"], 0)

    def test_effective_profile_mismatch_fails_closed(self) -> None:
        publisher = _FakePublisher()
        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
        )
        mismatched = _signal("ALPHA", signal_id="profile-mismatch")
        mismatched = SimulatorEntrySignal(
            underlying=mismatched.underlying,
            strike=mismatched.strike,
            side=mismatched.side,
            captured_at=mismatched.captured_at,
            profile="research_profile__runtime",
            strategy=mismatched.strategy,
            signal_id=mismatched.signal_id,
        )

        result = router.route(
            SignalRouteRequest("profile_one", mismatched)
        )

        self.assertEqual(result.status, SignalRouteStatus.LOG_ONLY)
        self.assertIn("profiles do not match", result.reason)
        self.assertEqual(publisher.signals, [])

    def test_queue_drop_can_be_retried_with_same_signal_id(self) -> None:
        publisher = _FakePublisher((False, True))
        router = CentralSignalRouter(
            policies=_policies(),
            publisher=publisher,
            simulator_enabled=True,
        )
        request = SignalRouteRequest(
            "profile_one",
            _signal("ALPHA", signal_id="retry-id"),
        )

        first = router.route(request)
        second = router.route(request)

        self.assertEqual(first.status, SignalRouteStatus.DROPPED)
        self.assertEqual(second.status, SignalRouteStatus.QUEUED)
        self.assertEqual(len(publisher.signals), 2)

    def test_protocol_round_trip_preserves_call_and_put_metadata(self) -> None:
        for side in ("BUY_CALL", "BUY_PUT"):
            request = SignalRouteRequest(
                "profile_one",
                _signal("ALPHA", signal_id=side.lower(), side=side),
            )

            restored = signal_route_request_from_payload(
                signal_route_request_to_payload(request)
            )

            self.assertEqual(restored, request)

    def test_jsonl_audit_records_route_and_final_delivery(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        path = TEST_TEMP_ROOT / f"router_audit_{os.getpid()}.jsonl"
        path.unlink(missing_ok=True)
        audit = JsonlSignalRouteAudit(path)
        try:
            signal = _signal("ALPHA", signal_id="audited")
            router = CentralSignalRouter(
                policies=_policies(),
                publisher=_FakePublisher(),
                simulator_enabled=True,
                audit_sink=audit.record,
            )
            router.route(SignalRouteRequest("profile_one", signal))
            audit.record_delivery(
                SimulatorDeliveryOutcome(
                    signal=signal,
                    status="ACCEPTED",
                    reason="OK",
                    completed_at=datetime.now(UTC),
                )
            )
        finally:
            audit.close()

        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["record_type"] for record in records],
            ["signal_route", "signal_route", "simulator_delivery"],
        )
        self.assertEqual(
            [record["status"] for record in records],
            ["AUTHORIZED", "QUEUED", "ACCEPTED"],
        )

    def test_one_server_routes_two_strategy_clients(self) -> None:
        async def exercise() -> None:
            publisher = _FakePublisher()
            audits: list[SignalRouteAudit] = []
            both_routed = asyncio.Event()

            def audit_sink(audit: SignalRouteAudit) -> None:
                audits.append(audit)
                completed = sum(
                    item.result.status != SignalRouteStatus.AUTHORIZED
                    for item in audits
                )
                if completed == 2:
                    both_routed.set()

            router = CentralSignalRouter(
                policies=_policies(),
                publisher=publisher,
                simulator_enabled=True,
                audit_sink=audit_sink,
            )
            server = CentralSignalRouterServer(router, port=0)
            await server.start()
            alpha = CentralSignalRouterClient(
                configured_profile="profile_one",
                port=server.bound_port,
                max_retries=0,
            )
            beta = CentralSignalRouterClient(
                configured_profile="profile_one",
                port=server.bound_port,
                max_retries=0,
            )
            try:
                self.assertTrue(alpha.publish(_signal("ALPHA", signal_id="alpha")))
                self.assertTrue(beta.publish(_signal("BETA", signal_id="beta")))
                await asyncio.wait_for(both_routed.wait(), timeout=1)
                await alpha.close()
                await beta.close()
            finally:
                await server.close()

            self.assertEqual([item.signal_id for item in publisher.signals], ["alpha"])
            self.assertEqual(
                {
                    audit.result.status
                    for audit in audits
                    if audit.result.status != SignalRouteStatus.AUTHORIZED
                },
                {SignalRouteStatus.QUEUED, SignalRouteStatus.LOG_ONLY},
            )
            self.assertEqual(alpha.health_snapshot()["failed"], 0)
            self.assertEqual(beta.health_snapshot()["failed"], 0)

        asyncio.run(exercise())

    def test_end_to_end_only_permitted_strategy_reaches_simulator_ui(self) -> None:
        async def exercise() -> None:
            received: list[dict[str, object]] = []
            delivered = asyncio.Event()

            async def handle_ui(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                received.append(json.loads(await reader.readline()))
                writer.write(b"OK\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                delivered.set()

            ui = await asyncio.start_server(handle_ui, "127.0.0.1", 0)
            ui_port = int(ui.sockets[0].getsockname()[1])
            publisher = SimulatorEntryPublisher(port=ui_port)
            router = CentralSignalRouter(
                policies=_policies(),
                publisher=publisher,
                simulator_enabled=True,
            )
            server = CentralSignalRouterServer(router, port=0)
            await server.start()
            client = CentralSignalRouterClient(
                configured_profile="profile_one",
                port=server.bound_port,
                max_retries=0,
            )
            try:
                self.assertTrue(
                    client.publish(_signal("ALPHA", signal_id="allowed"))
                )
                self.assertTrue(
                    client.publish(
                        _signal("BETA", signal_id="log-only", side="BUY_PUT")
                    )
                )
                await asyncio.wait_for(delivered.wait(), timeout=1)
            finally:
                await client.close()
                await publisher.close()
                await server.close()
                ui.close()
                await ui.wait_closed()

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["signal_id"], "allowed")
            self.assertEqual(received[0]["side"], "CALL")

        asyncio.run(exercise())


class StrategyPublishFlagTests(unittest.TestCase):
    def test_boolean_flag_defaults_fails_closed_and_survives_runtime_selection(
        self,
    ) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        path = TEST_TEMP_ROOT / f"strategy_config_{os.getpid()}.json"
        path.write_text(
            json.dumps(
                {
                    "active_profile": "production",
                    "profiles": {
                        "production": {
                            "strategies": {
                                "ALPHA": {
                                    "enabled": True,
                                    "priority": 10,
                                    "publish_to_simulator": True,
                                },
                                "BETA": {
                                    "enabled": True,
                                    "priority": 20,
                                },
                            },
                        },
                        "child": {
                            "extends": "production",
                            "strategies": {
                                "ALPHA": {
                                    "publish_to_simulator": False,
                                }
                            },
                        },
                        "invalid": {
                            "strategies": {
                                "ALPHA": {
                                    "enabled": True,
                                    "priority": 10,
                                    "publish_to_simulator": "Y",
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        production = load_strategy_configuration(
            path,
            profile_name="production",
        )
        runtime = apply_runtime_strategy_selection(
            production,
            enabled_strategies=("ALPHA",),
        )
        child = load_strategy_configuration(path, profile_name="child")

        self.assertTrue(
            production.profile.strategy_publishes_to_simulator("ALPHA")
        )
        self.assertFalse(
            production.profile.strategy_publishes_to_simulator("BETA")
        )
        self.assertFalse(
            production.profile.strategy_publishes_to_simulator("UNKNOWN")
        )
        self.assertTrue(
            runtime.profile.strategy_publishes_to_simulator("ALPHA")
        )
        self.assertFalse(
            child.profile.strategy_publishes_to_simulator("ALPHA")
        )
        manifest = production.manifest()
        self.assertTrue(
            manifest["profile"]["strategies"]["ALPHA"][
                "publish_to_simulator"
            ]
        )
        with self.assertRaisesRegex(ValueError, "must be boolean"):
            load_strategy_configuration(path, profile_name="invalid")


if __name__ == "__main__":
    unittest.main()
