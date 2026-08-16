from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.domain.models import (
    AnalyticsSnapshot,
    EvidenceFamily,
    Exchange,
    GreeksSnapshot,
    InstrumentToken,
    MarketRegime,
    MicrostructureSignal,
    OptionContract,
    OptionQuote,
    OptionType,
    OptionChainSnapshot,
    SignalSetup,
    StrategyEvidence,
    SupportResistanceLevel,
)
from app.signals.gate import SignalGate, SignalGateSettings


def _snapshot(at: datetime, spot: str) -> OptionChainSnapshot:
    return OptionChainSnapshot(
        underlying="NIFTY",
        expiry=date(2026, 7, 30),
        spot_price=Decimal(spot),
        atm_strike=Decimal("24250"),
        captured_at=at,
        quotes=(),
    )


def _analytics() -> AnalyticsSnapshot:
    return AnalyticsSnapshot(
        underlying="NIFTY",
        captured_at=datetime(2026, 7, 21, 9, 30, tzinfo=UTC),
        atm_strike=Decimal("24250"),
        signal="BUY_CALL",
        signal_reason="EXHAUSTION REVERSAL: Capitulation flow detected at Support.",
        support_levels=(SupportResistanceLevel(Decimal("24200"), OptionType.PUT, 100),),
    )


def _micro(at: datetime) -> MicrostructureSignal:
    return MicrostructureSignal(
        token=InstrumentToken(Exchange.NFO, "111", "NIFTY", "NIFTY24JUL2624250CE"),
        underlying="NIFTY",
        side="BUY_CALL",
        captured_at=at,
        confidence=Decimal("0.8"),
        reason="test",
    )


class SignalGateTests(unittest.TestCase):
    def test_preflight_rejects_incomplete_frame_before_strategy_analysis(self) -> None:
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        snapshot = _snapshot(at, "24205")
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                require_complete_chain=True,
            )
        )

        error = gate.preflight_data(
            snapshot=snapshot,
            underlying_observed_at=at,
            refreshed_quote_tokens=set(),
            refreshed_greeks_tokens=set(),
        )
        analytics, decision = gate.reject_preflight(
            snapshot=snapshot,
            reason=error or "",
        )

        self.assertIn("incomplete option chain", error or "")
        self.assertFalse(decision.qualified)
        self.assertEqual(analytics.strategy_candidates, ())

    def test_requires_two_independent_confirmation_families(self) -> None:
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                min_independent_confirmation_families=2,
            )
        )
        analytics = replace(
            _analytics(),
            directional_evidence=(
                StrategyEvidence(
                    "pcr_context",
                    EvidenceFamily.POSITIONING,
                    "BUY_CALL",
                    Decimal("0.8"),
                ),
                StrategyEvidence(
                    "boundary_oi_growth",
                    EvidenceFamily.POSITIONING,
                    "BUY_CALL",
                    Decimal("0.7"),
                ),
            ),
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("1/2 independent confirmation families", decision.reason)

    def test_accepts_positioning_and_volatility_as_independent_families(self) -> None:
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                min_independent_confirmation_families=2,
            )
        )
        analytics = replace(
            _analytics(),
            directional_evidence=(
                StrategyEvidence(
                    "pcr_context",
                    EvidenceFamily.POSITIONING,
                    "BUY_CALL",
                    Decimal("0.8"),
                ),
                StrategyEvidence(
                    "iv_skew",
                    EvidenceFamily.VOLATILITY,
                    "BUY_CALL",
                    Decimal("0.7"),
                ),
            ),
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertTrue(decision.qualified)

    def test_rejects_exhaustion_away_from_its_support_level(self) -> None:
        gate = SignalGate(
            SignalGateSettings(1, 60, Decimal("20"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24250"), analytics=_analytics(), microstructure_signal=_micro(at)
        )

        self.assertFalse(decision.qualified)
        self.assertEqual(gated.signal, "NEUTRAL")
        self.assertIn("lacks a valid activation zone", decision.reason)

    def test_qualifies_only_after_confirmation_and_keeps_shadow_mode_neutral(self) -> None:
        gate = SignalGate(
            SignalGateSettings(2, 60, Decimal("20"), 3, "shadow")
        )
        start = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        first, first_decision = gate.evaluate(
            snapshot=_snapshot(start, "24205"), analytics=_analytics(), microstructure_signal=_micro(start)
        )
        second, second_decision = gate.evaluate(
            snapshot=_snapshot(start + timedelta(seconds=1), "24205"),
            analytics=replace(
                _analytics(),
                captured_at=start + timedelta(seconds=1),
            ),
            microstructure_signal=_micro(start + timedelta(seconds=1)),
        )

        self.assertFalse(first_decision.qualified)
        self.assertTrue(second_decision.qualified)
        self.assertEqual(first.signal, "NEUTRAL")
        self.assertEqual(second.signal, "NEUTRAL")

    def test_rejects_momentum_call_too_close_to_unbroken_resistance(self) -> None:
        gate = SignalGate(
            SignalGateSettings(1, 60, Decimal("10"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24250"),
            signal="BUY_CALL",
            signal_reason="GAMMA CALL EXPANSION: Gamma blast up detected.",
            resistance_levels=(SupportResistanceLevel(Decimal("24250"), OptionType.CALL, 100),),
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24245"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("too close to unbroken resistance", decision.reason)

    def test_gamma_profile_uses_its_own_gate_without_price_levels(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=3,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=3,
                mode="shadow",
                min_directional_confirmations=2,
                min_independent_confirmation_families=2,
                profile_min_confirmations=1,
                profile_min_directional_confirmations=1,
                profile_min_independent_confirmation_families=1,
                gamma_require_structural_room=False,
            )
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24250"),
            signal="BUY_CALL",
            signal_reason="GAMMA CALL EXPANSION: Gamma blast up detected.",
            setup_type=SignalSetup.MOMENTUM_EXPANSION,
            directional_confirmations=("gamma_expansion",),
            directional_evidence=(
                StrategyEvidence(
                    "iv_skew_expansion",
                    EvidenceFamily.VOLATILITY,
                    "BUY_CALL",
                    Decimal("0.8"),
                ),
            ),
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24245"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertTrue(decision.qualified, decision.reason)
        self.assertEqual(decision.strong_signal, "BUY_CALL")

    def test_gamma_profile_requires_an_executable_target_option(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=3,
                mode="shadow",
                gamma_require_target_option_confirmation=True,
                gamma_require_structural_room=False,
            )
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24250"),
            signal="BUY_CALL",
            signal_reason="GAMMA CALL EXPANSION: Gamma blast up detected.",
            setup_type=SignalSetup.MOMENTUM_EXPANSION,
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24245"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("no executable target contract", decision.reason)

    def test_qualifies_intrarange_gamma_put_with_room_to_support(self) -> None:
        gate = SignalGate(
            SignalGateSettings(1, 60, Decimal("10"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24250"),
            signal="BUY_PUT",
            signal_reason="GAMMA PUT EXPANSION: Gamma blast down detected.",
            support_levels=(SupportResistanceLevel(Decimal("24200"), OptionType.PUT, 100),),
        )
        micro = MicrostructureSignal(
            token=InstrumentToken(Exchange.NFO, "222", "NIFTY", "NIFTY24JUL2624250PE"),
            underlying="NIFTY",
            side="BUY_PUT",
            captured_at=at,
            confidence=Decimal("0.8"),
            reason="test",
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24250"),
            analytics=analytics,
            microstructure_signal=micro,
        )

        self.assertTrue(decision.qualified)
        self.assertEqual(decision.setup_type, SignalSetup.MOMENTUM_EXPANSION)
        self.assertEqual(decision.strong_signal, "BUY_PUT")
        self.assertEqual(decision.published_signal, "NEUTRAL")

    def test_gamma_can_skip_microstructure_when_feature_is_disabled(
        self,
    ) -> None:
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("10"),
                3,
                "shadow",
                min_signal_score=Decimal("0"),
                gamma_require_microstructure_confirmation=False,
            )
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("24250"),
            signal="BUY_PUT",
            signal_reason="GAMMA PUT EXPANSION: Gamma blast down detected.",
            support_levels=(
                SupportResistanceLevel(
                    Decimal("24200"),
                    OptionType.PUT,
                    100,
                ),
            ),
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24250"),
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertTrue(decision.qualified)
        self.assertIsNone(decision.microstructure_signal)
        self.assertEqual(decision.setup_type, SignalSetup.MOMENTUM_EXPANSION)

    def test_reports_stale_microstructure_before_location_rejection(self) -> None:
        gate = SignalGate(
            SignalGateSettings(1, 60, Decimal("20"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24250"),
            analytics=_analytics(),
            microstructure_signal=_micro(at - timedelta(seconds=10)),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("microstructure confirmation is stale", decision.reason)

    def test_rejects_fresh_opposite_microstructure_even_with_matching_events(self) -> None:
        gate = SignalGate(
            SignalGateSettings(2, 60, Decimal("10"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        gate.observe_microstructure(_micro(at - timedelta(seconds=2)))
        gate.observe_microstructure(_micro(at - timedelta(seconds=1)))
        opposing = MicrostructureSignal(
            token=InstrumentToken(
                Exchange.NFO, "222", "NIFTY", "NIFTY24JUL2624250PE"
            ),
            underlying="NIFTY",
            side="BUY_PUT",
            captured_at=at,
            confidence=Decimal("0.8"),
            reason="test",
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=_analytics(),
            microstructure_signal=opposing,
        )

        self.assertFalse(decision.qualified)
        self.assertIn("fresh microstructure conflict", decision.reason)

    def test_rejects_chain_contaminated_with_finnifty_contract(self) -> None:
        gate = SignalGate(
            SignalGateSettings(1, 60, Decimal("10"), 3, "shadow")
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        contaminated = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24205"),
            atm_strike=Decimal("24200"),
            captured_at=at,
            quotes=(
                OptionQuote(
                    contract=OptionContract(
                        underlying="NIFTY",
                        expiry=date(2026, 7, 30),
                        strike=Decimal("24200"),
                        option_type=OptionType.CALL,
                        token=InstrumentToken(
                            Exchange.NFO,
                            "333",
                            "NIFTY",
                            "FINNIFTY30JUL2624200CE",
                        ),
                    )
                ),
            ),
        )

        _gated, decision = gate.evaluate(
            snapshot=contaminated,
            analytics=_analytics(),
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("chain contamination", decision.reason)

    def test_rejects_strategy_incompatible_with_regime(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                require_regime_match=True,
            )
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        analytics = replace(
            _analytics(),
            market_regime=MarketRegime.TREND_BREAKOUT,
        )

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("incompatible", decision.reason)

    def test_rejects_microstructure_from_a_different_target_contract(self) -> None:
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("24200"),
            option_type=OptionType.CALL,
            token=InstrumentToken(
                Exchange.NFO,
                "TARGET",
                "NIFTY",
                "NIFTY30JUL2624200CE",
            ),
            lot_size=50,
        )
        quote = OptionQuote(
            contract=contract,
            ltp=Decimal("100"),
            oi=10000,
            volume=10000,
            bid=Decimal("99.5"),
            ask=Decimal("100"),
            greeks=GreeksSnapshot(
                contract=contract,
                captured_at=at,
                implied_volatility=Decimal("15"),
                delta=Decimal("0.5"),
            ),
        )
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("24205"),
            atm_strike=Decimal("24200"),
            captured_at=at,
            quotes=(quote,),
        )
        analytics = replace(
            _analytics(),
            target_strike=Decimal("24200"),
            target_option_type=OptionType.CALL,
        )
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                require_target_contract=True,
            )
        )

        _gated, decision = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("fresh microstructure confirmations", decision.reason)

    def test_daily_loss_limit_blocks_new_signal(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                max_daily_loss=Decimal("1000"),
            )
        )
        gate.update_risk_state(
            realized_pnl=Decimal("-1000"),
            open_positions=0,
            gross_exposure=Decimal("0"),
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=_analytics(),
            microstructure_signal=_micro(at),
        )

        self.assertFalse(decision.qualified)
        self.assertIn("daily-loss limit", decision.reason)

    def test_rejects_incomplete_chain_before_microstructure(self) -> None:
        gate = SignalGate(
            SignalGateSettings(
                1,
                60,
                Decimal("20"),
                3,
                "shadow",
                require_complete_chain=True,
            )
        )
        at = datetime(2026, 7, 21, 9, 30, tzinfo=UTC)

        _gated, decision = gate.evaluate(
            snapshot=_snapshot(at, "24205"),
            analytics=_analytics(),
            microstructure_signal=_micro(at),
            underlying_observed_at=at,
        )

        self.assertFalse(decision.qualified)
        self.assertIn("incomplete option chain", decision.reason)
