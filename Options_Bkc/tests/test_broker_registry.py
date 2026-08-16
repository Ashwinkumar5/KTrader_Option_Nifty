from __future__ import annotations

import unittest
from dataclasses import replace

from app.broker.registry import (
    broker_configuration_errors,
    create_broker_client,
    registered_brokers,
)
from app.core.config import BrokerName, load_settings


class BrokerRegistryTests(unittest.TestCase):
    def test_angleone_is_registered_and_constructed_from_configuration(self) -> None:
        settings = load_settings()
        client = create_broker_client(settings)
        self.assertIn("angleone", registered_brokers())
        self.assertEqual(type(client).__name__, "AngleOneClient")

    def test_uninstalled_configured_broker_has_clear_error(self) -> None:
        settings = replace(load_settings(), broker_name=BrokerName.DHAN)
        with self.assertRaisesRegex(
            ValueError,
            "Broker adapter 'dhan' is not installed",
        ):
            broker_configuration_errors(settings)

    def test_validation_is_owned_by_registered_adapter(self) -> None:
        settings = replace(
            load_settings(),
            angleone_api_key="",
            angleone_client_code="",
            angleone_password="",
            angleone_totp_secret="",
        )
        errors = broker_configuration_errors(settings)
        self.assertEqual(len(errors), 4)
        self.assertTrue(all("ANGLEONE_" in error for error in errors))

    def test_broker_specific_config_is_available_but_not_core_schema(self) -> None:
        settings = load_settings()
        self.assertEqual(
            settings.broker_config["API_KEY"],
            settings.angleone_api_key,
        )


if __name__ == "__main__":
    unittest.main()
