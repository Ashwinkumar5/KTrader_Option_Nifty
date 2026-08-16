from __future__ import annotations

import unittest
from decimal import Decimal

from app.analytics.regime import MarketRegimeClassifier, RegimeSettings
from app.domain.models import MarketRegime


class MarketRegimeClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = MarketRegimeClassifier(
            RegimeSettings(
                window_size=5,
                min_trend_displacement_points=Decimal("10"),
            )
        )

    def classify(
        self,
        spot: str,
        *,
        iv_rank: str = "50",
        unstable: bool = False,
        gamma: bool = False,
    ) -> MarketRegime:
        return self.classifier.classify(
            underlying="NIFTY",
            spot=Decimal(spot),
            support=Decimal("24700"),
            resistance=Decimal("24850"),
            iv_rank=Decimal(iv_rank),
            unstable_high_vol=unstable,
            gamma_coiled=gamma,
        )

    def test_routes_inside_range_to_range_regime(self) -> None:
        self.assertEqual(self.classify("24750"), MarketRegime.RANGE)

    def test_routes_level_break_to_trend_breakout(self) -> None:
        self.assertEqual(self.classify("24855"), MarketRegime.TREND_BREAKOUT)

    def test_routes_gamma_coil_to_compression(self) -> None:
        self.assertEqual(
            self.classify("24750", iv_rank="20", gamma=True),
            MarketRegime.COMPRESSION,
        )

    def test_directional_leg_inside_intact_range_remains_range_rotation(self) -> None:
        for spot in ("24710", "24725", "24740", "24755"):
            self.classify(spot)

        self.assertEqual(self.classify("24770"), MarketRegime.RANGE)

    def test_unstable_high_vol_has_highest_priority(self) -> None:
        self.assertEqual(
            self.classify("24855", unstable=True, gamma=True),
            MarketRegime.UNSTABLE_HIGH_VOL,
        )


if __name__ == "__main__":
    unittest.main()
