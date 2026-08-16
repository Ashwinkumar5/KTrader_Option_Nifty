from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

from process_watch_dog.config import ConfigurationError, load_watchdog_settings


TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".test-work"


class WatchdogConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_TEMP_ROOT / self._testMethodName
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.strategy_path = self.root / "strategy.json"
        self.strategy_path.write_text(
            json.dumps(
                {
                    "profiles": {
                        "profile_one": {
                            "watchdog_enable": "Y",
                            "strategies": {
                                "ALPHA": {"enabled": True, "priority": 20},
                                "BETA": {"enabled": True, "priority": 10},
                                "OFF": {"enabled": False, "priority": 30},
                            }
                        },
                        "profile_watchdog_off": {
                            "watchdog_enable": "N",
                            "strategies": {
                                "GAMMA": {"enabled": True, "priority": 1}
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_expands_only_run_process_profile_strategy_pairs(self) -> None:
        config = self._write_config(
            [
                {
                    "id": "{profile}__{strategy}",
                    "enabled": True,
                    "profiles": ["profile_one"],
                    "strategies": "enabled",
                    "command": [sys.executable, "-c", "print('{strategy}')"],
                    "working_directory": str(self.root),
                }
            ]
        )

        settings = load_watchdog_settings(config)

        self.assertEqual(
            [item.process_id for item in settings.processes],
            ["profile_one__BETA", "profile_one__ALPHA"],
        )
        self.assertTrue(
            all("OFF" not in item.command for item in settings.processes)
        )

    def test_rejects_explicit_disabled_strategy(self) -> None:
        config = self._write_config(
            [
                {
                    "id": "disabled",
                    "enabled": True,
                    "profile": "profile_one",
                    "strategies": ["OFF"],
                    "command": [sys.executable, "-c", "pass"],
                    "working_directory": str(self.root),
                }
            ]
        )

        with self.assertRaisesRegex(ConfigurationError, "is disabled"):
            load_watchdog_settings(config)

    def test_skips_profile_with_watchdog_enable_n(self) -> None:
        config = self._write_config(
            [
                {
                    "id": "{profile}__{strategy}",
                    "enabled": True,
                    "profiles": "all",
                    "strategies": "enabled",
                    "command": [sys.executable, "-c", "pass"],
                    "working_directory": str(self.root),
                }
            ]
        )

        settings = load_watchdog_settings(config)

        self.assertEqual(
            [item.process_id for item in settings.processes],
            ["profile_one__BETA", "profile_one__ALPHA"],
        )

    def test_expands_singleton_without_profile_strategy_fanout(self) -> None:
        config = self._write_config(
            [
                {
                    "id": "central_signal_router",
                    "enabled": True,
                    "singleton": True,
                    "role": "CENTRAL_SIGNAL_ROUTER",
                    "command": [sys.executable, "-c", "pass"],
                    "working_directory": str(self.root),
                }
            ]
        )

        settings = load_watchdog_settings(config)

        self.assertEqual(len(settings.processes), 1)
        process = settings.processes[0]
        self.assertEqual(process.process_id, "central_signal_router")
        self.assertEqual(process.profile, "SYSTEM")
        self.assertEqual(process.strategy, "CENTRAL_SIGNAL_ROUTER")

    def test_rejects_duplicate_expanded_ids(self) -> None:
        config = self._write_config(
            [
                {
                    "id": "same",
                    "enabled": True,
                    "profile": "profile_one",
                    "strategies": "enabled",
                    "command": [sys.executable, "-c", "pass"],
                    "working_directory": str(self.root),
                }
            ]
        )

        with self.assertRaisesRegex(ConfigurationError, "duplicate expanded"):
            load_watchdog_settings(config)

    def _write_config(self, run_process: list[dict[str, object]]) -> Path:
        path = self.root / "watchdog.json"
        path.write_text(
            json.dumps(
                {
                    "project_root": str(self.root),
                    "strategy_config": str(self.strategy_path),
                    "runtime_directory": str(self.root / "runtime"),
                    "log_directory": str(self.root / "logs"),
                    "control": {"port": 0},
                    "run_process": run_process,
                }
            ),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
