from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from process_watch_dog.strategy_catalog import StrategyCatalog, StrategyCatalogError


TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-work"


class StrategyCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TEMP_ROOT / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_resolves_inherited_enabled_strategies(self) -> None:
        path = self.root / "strategy_config.json"
        path.write_text(
            json.dumps(
                {
                    "active_profile": "base",
                    "profiles": {
                        "base": {
                            "watchdog_enable": "Y",
                            "strategies": {
                                "ALPHA": {
                                    "enabled": True,
                                    "priority": 20,
                                    "publish_to_simulator": True,
                                },
                                "BETA": {"enabled": False, "priority": 10},
                            }
                        },
                        "child": {
                            "extends": "base",
                            "watchdog_enable": "N",
                            "strategies": {
                                "ALPHA": {"enabled": False, "priority": 20},
                                "BETA": {"enabled": True, "priority": 10},
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        catalog = StrategyCatalog.load(path)

        self.assertEqual(catalog.enabled_strategies("base"), ("ALPHA",))
        self.assertEqual(catalog.enabled_strategies("child"), ("BETA",))
        self.assertEqual(catalog.watchdog_enabled_profiles(), ("base",))
        self.assertTrue(catalog.strategies("base")[1].publish_to_simulator)
        self.assertFalse(catalog.strategies("child")[0].publish_to_simulator)

    def test_rejects_disabled_strategy(self) -> None:
        path = self.root / "strategy_config.json"
        path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "one": {
                            "watchdog_enable": "Y",
                            "strategies": {
                                "ALPHA": {"enabled": False, "priority": 1}
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        catalog = StrategyCatalog.load(path)

        with self.assertRaisesRegex(StrategyCatalogError, "is disabled"):
            catalog.validate_enabled_strategy("one", "ALPHA")

    def test_rejects_missing_watchdog_flag(self) -> None:
        path = self.root / "strategy_config.json"
        path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "one": {
                            "strategies": {
                                "ALPHA": {"enabled": True, "priority": 1}
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        catalog = StrategyCatalog.load(path)

        with self.assertRaisesRegex(StrategyCatalogError, "watchdog_enable"):
            catalog.watchdog_enabled("one")


if __name__ == "__main__":
    unittest.main()
