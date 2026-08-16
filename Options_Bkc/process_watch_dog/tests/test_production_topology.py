from __future__ import annotations

import unittest
from pathlib import Path

from process_watch_dog.config import load_watchdog_settings
from process_watch_dog.strategy_catalog import StrategyCatalog


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_CONFIG = PROJECT_ROOT / "process_watch_dog" / "watchdog_config.json"


class ProductionWatchdogTopologyTests(unittest.TestCase):
    def test_all_watchdog_enabled_strategies_use_shared_feed_subscribers(self) -> None:
        catalog = StrategyCatalog.load(
            PROJECT_ROOT / "config" / "strategy_config.json"
        )
        expected_pairs = tuple(
            (profile, strategy)
            for profile in catalog.watchdog_enabled_profiles()
            for strategy in catalog.enabled_strategies(profile)
        )
        settings = load_watchdog_settings(
            WATCHDOG_CONFIG,
            validate_commands=False,
        )

        self.assertEqual(
            [process.process_id for process in settings.processes],
            (
                [
                    "local_nats_server",
                    "mkt_data_feed_handler",
                    "central_signal_router",
                ]
                + [
                    f"{profile}__{strategy}__nats_subscriber"
                    for profile, strategy in expected_pairs
                ]
            ),
        )

        by_id = {
            process.process_id: process
            for process in settings.processes
        }
        feed = by_id["mkt_data_feed_handler"]
        self.assertIsNotNone(feed.heartbeat_file)
        self.assertIn(str(feed.heartbeat_file), feed.command)

        subscribers = tuple(
            process
            for process in settings.processes
            if process.profile != "SYSTEM"
        )
        self.assertEqual(
            tuple((item.profile, item.strategy) for item in subscribers),
            expected_pairs,
        )
        for subscriber in subscribers:
            self.assertEqual(
                subscriber.command[
                    subscriber.command.index("--market-data-mode") + 1
                ],
                "nats-subscriber",
            )
            self.assertEqual(
                subscriber.command[
                    subscriber.command.index("--snapshot-interval-ms") + 1
                ],
                "15000",
            )
            self.assertIsNotNone(subscriber.heartbeat_file)
            self.assertIn(str(subscriber.heartbeat_file), subscriber.command)


if __name__ == "__main__":
    unittest.main()
