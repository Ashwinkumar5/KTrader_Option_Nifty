from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.core.config import load_settings


class MarketDataInfrastructureSettingsTests(unittest.TestCase):
    def test_market_data_infrastructure_environment_overrides(self) -> None:
        overrides = {
            "NATS_URL": "nats://10.0.0.8:4223",
            "MARKET_DATA_SUBJECT_PREFIX": ".desk.options.v2.",
            "MARKET_DATA_BUS_QUEUE_CAPACITY": "2048",
            "MARKET_DATA_BOOTSTRAP_TIMEOUT_SECONDS": "7.5",
            "MARKET_DATA_FEED_INTERVAL_MS": "2500",
            "MARKET_DATA_FEED_TAPE_DIRECTORY": "runtime/feed-tapes",
        }

        with patch.dict(os.environ, overrides):
            settings = load_settings()

        self.assertEqual(settings.nats_url, "nats://10.0.0.8:4223")
        self.assertEqual(settings.market_data_subject_prefix, "desk.options.v2")
        self.assertEqual(settings.market_data_bus_queue_capacity, 2048)
        self.assertEqual(settings.market_data_bootstrap_timeout_seconds, 7.5)
        self.assertEqual(settings.market_data_feed_interval_ms, 2500)
        self.assertEqual(
            settings.market_data_feed_tape_directory,
            "runtime/feed-tapes",
        )


if __name__ == "__main__":
    unittest.main()
