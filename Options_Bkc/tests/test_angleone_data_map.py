from __future__ import annotations

import unittest

from app.broker.angleone.data_map import (
    REQUIRED_OPTION_CHAIN_FIELDS,
    SMARTAPI_ENDPOINTS,
    SmartApiDataSource,
    SmartApiWebSocketMode,
    required_fields_by_source,
)


class AngleOneDataMapTests(unittest.TestCase):
    def test_required_option_chain_fields_cover_onboarding_requirements(self) -> None:
        field_names = {field.name for field in REQUIRED_OPTION_CHAIN_FIELDS}

        self.assertIn("ltp", field_names)
        self.assertIn("volume", field_names)
        self.assertIn("oi", field_names)
        self.assertIn("oi_change", field_names)
        self.assertIn("implied_volatility", field_names)
        self.assertIn("delta_gamma_theta_vega", field_names)
        self.assertIn("open_high_low_close", field_names)
        self.assertIn("best_bid_ask", field_names)
        self.assertIn("exchange_timestamp", field_names)

    def test_snap_quote_mode_matches_smartapi_websocket_mode_number(self) -> None:
        self.assertEqual(SmartApiWebSocketMode.SNAP_QUOTE.code, 3)

    def test_every_required_preferred_source_has_endpoint_mapping(self) -> None:
        grouped = required_fields_by_source()

        self.assertIn(SmartApiDataSource.WEBSOCKET_SNAP_QUOTE, grouped)
        self.assertIn(SmartApiDataSource.OPTION_GREEK, grouped)
        self.assertTrue(set(grouped).issubset(set(SMARTAPI_ENDPOINTS)))


if __name__ == "__main__":
    unittest.main()
