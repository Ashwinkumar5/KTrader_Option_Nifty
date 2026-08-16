from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.core.strategy_config import QuantMicrostructureSettings
from app.domain.models import (
    AnalyticsSnapshot,
    Exchange,
    GreeksSnapshot,
    InstrumentKind,
    InstrumentToken,
    MarketTick,
    MicrostructureSignal,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    SignalSetup,
    StrategyFamily,
)
from app.signals.gate import SignalGate, SignalGateDecision, SignalGateSettings
from app.signals.timely_entry import TimelyEntryGuard


class TimelyEntryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.at = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
        self.token = InstrumentToken(
            Exchange.NFO,
            "CE1",
            "NIFTY",
            "NIFTY06AUG2625000CE",
            InstrumentKind.OPTION,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 8, 6),
            strike=Decimal("25000"),
            option_type=OptionType.CALL,
            token=self.token,
            lot_size=75,
        )
        quote = OptionQuote(
            contract=contract,
            ltp=Decimal("100"),
            bid=Decimal("99.5"),
            ask=Decimal("100"),
            volume=1000,
            oi=2000,
            greeks=GreeksSnapshot(
                contract=contract,
                captured_at=self.at,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.5"),
            ),
        )
        self.snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=contract.expiry,
            spot_price=Decimal("25000"),
            atm_strike=Decimal("25000"),
            captured_at=self.at,
            quotes=(quote,),
        )
        self.analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=self.at,
            atm_strike=Decimal("25000"),
            signal="BUY_CALL",
            signal_reason="test quant candidate",
            target_strike=Decimal("25000"),
            target_option_type=OptionType.CALL,
            selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
            setup_type=SignalSetup.DERIVATIVES_QUANT,
        )
        self.decision = SignalGateDecision(
            captured_at=self.at,
            raw_signal="BUY_CALL",
            published_signal="NEUTRAL",
            qualified=False,
            reason="only 0/1 fresh target-option liquidity confirmations",
            microstructure_signal=None,
            setup_type=SignalSetup.DERIVATIVES_QUANT,
            strong_signal="BUY_CALL",
            evidence=("target_option_liquidity_missing",),
        )

    def test_releases_matching_fresh_event_within_chase_limit(self) -> None:
        guard = self._guard()
        self._arm(guard)
        trigger_at = self.at + timedelta(seconds=2)

        trigger = guard.consider(
            tick=self._tick(trigger_at, ask="101.5"),
            signal=self._signal(trigger_at),
        )

        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.premium_chase_percent, Decimal("1.5000"))
        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="101.5"),
                signal=self._signal(trigger_at),
            )
        )

    def test_arms_option_chain_impulse_candidate(self) -> None:
        guard = self._guard()
        analytics = replace(
            self.analytics,
            selected_strategy=StrategyFamily.OPTION_CHAIN_IMPULSE,
            setup_type=SignalSetup.OPTION_CHAIN_IMPULSE,
        )

        armed = guard.arm_from_decision(
            snapshot=self.snapshot,
            analytics=analytics,
            decision=self.decision,
            refreshed_quote_tokens={self.token.token},
            refreshed_greeks_tokens={self.token.token},
            underlying_observed_at=self.at,
        )

        self.assertIsNotNone(armed)

    def test_cancels_candidate_above_two_percent_chase(self) -> None:
        guard = self._guard()
        self._arm(guard)
        trigger_at = self.at + timedelta(seconds=2)

        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="102.1"),
                signal=self._signal(trigger_at),
            )
        )
        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="101"),
                signal=self._signal(trigger_at),
            )
        )

    def test_confirmed_profile_cancels_weakening_premium(self) -> None:
        guard = TimelyEntryGuard(
            QuantMicrostructureSettings(
                event_driven_entry=True,
                candidate_ttl_seconds=10,
                maximum_age_seconds=2,
                minimum_candidate_premium_chase_percent=Decimal("0"),
                maximum_candidate_premium_chase_percent=Decimal("2"),
            ),
            market_timezone="Asia/Calcutta",
        )
        self._arm(guard)
        trigger_at = self.at + timedelta(seconds=1)

        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="99.9"),
                signal=self._signal(trigger_at),
            )
        )
        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="100.5"),
                signal=self._signal(trigger_at),
            )
        )

    def test_rejects_event_after_ten_second_candidate_window(self) -> None:
        guard = self._guard()
        self._arm(guard)
        trigger_at = self.at + timedelta(seconds=11)

        self.assertIsNone(
            guard.consider(
                tick=self._tick(trigger_at, ask="101"),
                signal=self._signal(trigger_at),
            )
        )

    def test_does_not_arm_after_market_cutoff(self) -> None:
        guard = self._guard()
        late_snapshot = OptionChainSnapshot(
            underlying=self.snapshot.underlying,
            expiry=self.snapshot.expiry,
            spot_price=self.snapshot.spot_price,
            atm_strike=self.snapshot.atm_strike,
            captured_at=datetime(2026, 8, 6, 10, 0, tzinfo=UTC),
            quotes=self.snapshot.quotes,
        )

        armed = guard.arm_from_decision(
            snapshot=late_snapshot,
            analytics=self.analytics,
            decision=self.decision,
            refreshed_quote_tokens={self.token.token},
            refreshed_greeks_tokens={self.token.token},
            underlying_observed_at=late_snapshot.captured_at,
        )

        self.assertIsNone(armed)

    def test_gate_ignores_microstructure_before_candidate_was_armed(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=60,
                max_microstructure_age_seconds=2,
                min_directional_confirmations=0,
                min_independent_confirmation_families=0,
                max_level_distance=Decimal("1000"),
                require_target_contract=True,
                quant_require_futures_confirmation=False,
                quant_min_option_confirmations=1,
            )
        )
        gate.observe_microstructure(
            self._signal(self.at - timedelta(seconds=1))
        )

        _analytics, rejected = gate.evaluate(
            snapshot=self.snapshot,
            analytics=self.analytics,
            microstructure_signal=None,
            microstructure_not_before=self.at,
        )

        self.assertFalse(rejected.qualified)
        self.assertIn("target-option", rejected.reason)

        fresh_at = self.at + timedelta(seconds=1)
        gate.observe_microstructure(self._signal(fresh_at))
        _analytics, accepted = gate.evaluate(
            snapshot=replace(self.snapshot, captured_at=fresh_at),
            analytics=replace(self.analytics, captured_at=fresh_at),
            microstructure_signal=None,
            microstructure_not_before=self.at,
        )
        self.assertTrue(accepted.qualified, accepted.reason)

    def _guard(self) -> TimelyEntryGuard:
        return TimelyEntryGuard(
            QuantMicrostructureSettings(
                event_driven_entry=True,
                candidate_ttl_seconds=10,
                maximum_age_seconds=2,
                maximum_candidate_premium_chase_percent=Decimal("2"),
                event_entry_cutoff_time="15:10:00",
            ),
            market_timezone="Asia/Calcutta",
        )

    def _arm(self, guard: TimelyEntryGuard) -> None:
        armed = guard.arm_from_decision(
            snapshot=self.snapshot,
            analytics=self.analytics,
            decision=self.decision,
            refreshed_quote_tokens={self.token.token},
            refreshed_greeks_tokens={self.token.token},
            underlying_observed_at=self.at,
        )
        self.assertIsNotNone(armed)

    def _signal(self, at: datetime) -> MicrostructureSignal:
        return MicrostructureSignal(
            token=self.token,
            underlying="NIFTY",
            side="BUY_CALL",
            captured_at=at,
            confidence=Decimal("0.8"),
            reason="fresh option liquidity",
        )

    def _tick(self, at: datetime, *, ask: str) -> MarketTick:
        return MarketTick(
            token=self.token,
            exchange_timestamp=at,
            received_at=at,
            ltp=Decimal(ask),
            raw={
                "depth": {
                    "buy": [{"price": "100", "quantity": 100}],
                    "sell": [{"price": ask, "quantity": 100}],
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
