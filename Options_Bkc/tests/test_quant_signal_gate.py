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
    InstrumentKind,
    InstrumentToken,
    MarketRegime,
    MicrostructureSignal,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionType,
    SignalSetup,
    StrategyEvidence,
    StrategyFamily,
)
from app.signals.gate import SignalGate, SignalGateSettings


def _micro(
    *,
    token: InstrumentToken,
    at: datetime,
    side: str = "BUY_CALL",
) -> MicrostructureSignal:
    return MicrostructureSignal(
        token=token,
        underlying="NIFTY",
        side=side,
        captured_at=at,
        confidence=Decimal("0.8"),
        reason="test liquidity pressure",
    )


class QuantSignalGateTests(unittest.TestCase):
    def test_quant_cooldown_blocks_same_side_but_not_opposite_side(self) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        tokens = {
            "BUY_CALL": InstrumentToken(
                Exchange.NFO,
                "CE1",
                "NIFTY",
                "NIFTY30JUL2625000CE",
                InstrumentKind.OPTION,
            ),
            "BUY_PUT": InstrumentToken(
                Exchange.NFO,
                "PE1",
                "NIFTY",
                "NIFTY30JUL2625000PE",
                InstrumentKind.OPTION,
            ),
        }
        contracts = {
            side: OptionContract(
                underlying="NIFTY",
                expiry=date(2026, 7, 30),
                strike=Decimal("25000"),
                option_type=(
                    OptionType.CALL if side == "BUY_CALL" else OptionType.PUT
                ),
                token=token,
                lot_size=75,
            )
            for side, token in tokens.items()
        }
        quotes = tuple(
            OptionQuote(
                contract=contract,
                ltp=Decimal("100"),
                oi=10_000,
                volume=20_000,
                bid=Decimal("99.5"),
                ask=Decimal("100"),
                greeks=GreeksSnapshot(
                    contract=contract,
                    captured_at=at,
                    implied_volatility=Decimal("15"),
                    delta=(
                        Decimal("0.5")
                        if side == "BUY_CALL"
                        else Decimal("-0.5")
                    ),
                ),
            )
            for side, contract in contracts.items()
        )
        snapshot = OptionChainSnapshot(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            spot_price=Decimal("25000"),
            atm_strike=Decimal("25000"),
            captured_at=at,
            quotes=quotes,
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=5,
                quant_cooldown_seconds=900,
                quant_min_option_confirmations=1,
                quant_require_futures_confirmation=False,
                profile_min_directional_confirmations=1,
                profile_min_independent_confirmation_families=1,
            )
        )

        def analytics(side: str, captured_at: datetime) -> AnalyticsSnapshot:
            return AnalyticsSnapshot(
                underlying="NIFTY",
                captured_at=captured_at,
                atm_strike=Decimal("25000"),
                signal=side,
                signal_reason="confirmed derivatives positioning",
                target_strike=Decimal("25000"),
                target_option_type=contracts[side].option_type,
                setup_type=SignalSetup.DERIVATIVES_QUANT,
                selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
                directional_confirmations=("structure:DERIVATIVES_QUANT",),
                directional_evidence=(
                    StrategyEvidence(
                        "cross_strike_option_positioning",
                        EvidenceFamily.POSITIONING,
                        side,
                        Decimal("0.8"),
                    ),
                ),
            )

        gate.observe_microstructure(_micro(token=tokens["BUY_CALL"], at=at))
        _gated, first_call = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics("BUY_CALL", at),
            microstructure_signal=None,
        )
        later = at + timedelta(seconds=15)
        later_snapshot = replace(snapshot, captured_at=later)
        gate.observe_microstructure(
            _micro(token=tokens["BUY_CALL"], at=later)
        )
        _gated, repeated_call = gate.evaluate(
            snapshot=later_snapshot,
            analytics=analytics("BUY_CALL", later),
            microstructure_signal=None,
        )
        gate.observe_microstructure(
            _micro(token=tokens["BUY_PUT"], at=later, side="BUY_PUT")
        )
        _gated, opposite_put = gate.evaluate(
            snapshot=later_snapshot,
            analytics=analytics("BUY_PUT", later),
            microstructure_signal=None,
        )

        self.assertTrue(first_call.qualified, first_call.reason)
        self.assertFalse(repeated_call.qualified)
        self.assertIn("cooldown", repeated_call.reason)
        self.assertTrue(opposite_put.qualified, opposite_put.reason)

    def test_quant_profile_can_use_its_own_direction_gate(self) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        option_token = InstrumentToken(
            Exchange.NFO,
            "CE1",
            "NIFTY",
            "NIFTY30JUL2625000CE",
            InstrumentKind.OPTION,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("25000"),
            option_type=OptionType.CALL,
            token=option_token,
            lot_size=75,
        )
        quote = OptionQuote(
            contract=contract,
            ltp=Decimal("100"),
            oi=10000,
            volume=20000,
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
            expiry=contract.expiry,
            spot_price=Decimal("25000"),
            atm_strike=Decimal("25000"),
            captured_at=at,
            quotes=(quote,),
        )
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("25000"),
            signal="BUY_CALL",
            signal_reason="directional long-premium momentum aligned",
            target_strike=Decimal("25000"),
            target_option_type=OptionType.CALL,
            setup_type=SignalSetup.DERIVATIVES_QUANT,
            selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
            directional_confirmations=("structure:DERIVATIVES_QUANT",),
            directional_evidence=(
                StrategyEvidence(
                    "index_momentum",
                    EvidenceFamily.FLOW,
                    "BUY_CALL",
                    Decimal("0.8"),
                ),
            ),
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=3,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=5,
                mode="shadow",
                min_directional_confirmations=2,
                min_independent_confirmation_families=2,
                require_target_contract=True,
                quant_min_option_confirmations=1,
                quant_require_futures_confirmation=False,
                profile_min_directional_confirmations=1,
                profile_min_independent_confirmation_families=1,
            )
        )
        gate.observe_microstructure(_micro(token=option_token, at=at))

        _gated, decision = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertTrue(decision.qualified, decision.reason)

    def test_requires_exact_option_and_futures_book_confirmations(self) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        option_token = InstrumentToken(
            Exchange.NFO,
            "CE1",
            "NIFTY",
            "NIFTY30JUL2625000CE",
            InstrumentKind.OPTION,
        )
        future_token = InstrumentToken(
            Exchange.NFO,
            "FUT1",
            "NIFTY",
            "NIFTY30JUL26FUT",
            InstrumentKind.FUTURE,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("25000"),
            option_type=OptionType.CALL,
            token=option_token,
            lot_size=75,
        )
        quote = OptionQuote(
            contract=contract,
            ltp=Decimal("100"),
            oi=10000,
            volume=20000,
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
            expiry=contract.expiry,
            spot_price=Decimal("25000"),
            atm_strike=Decimal("25000"),
            captured_at=at,
            quotes=(quote,),
        )
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("25000"),
            signal="BUY_CALL",
            signal_reason="DERIVATIVES QUANT aligned",
            target_strike=Decimal("25000"),
            target_option_type=OptionType.CALL,
            setup_type=SignalSetup.DERIVATIVES_QUANT,
            selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
            market_regime=MarketRegime.TREND_BREAKOUT,
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=5,
                mode="shadow",
                require_target_contract=True,
                require_regime_match=True,
                quant_min_option_confirmations=2,
                quant_min_futures_confirmations=2,
            )
        )
        gate.observe_microstructure(
            _micro(token=option_token, at=at - timedelta(seconds=1))
        )
        gate.observe_microstructure(_micro(token=option_token, at=at))

        _gated, missing_future = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertFalse(missing_future.qualified)
        self.assertIn("futures order-book", missing_future.reason)

        gate.observe_microstructure(
            _micro(token=future_token, at=at - timedelta(seconds=1))
        )
        gate.observe_microstructure(_micro(token=future_token, at=at))
        _gated, qualified = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertTrue(qualified.qualified)
        self.assertEqual(qualified.confirmation_count, 4)


    def test_unknown_market_regime_is_rejected_when_regime_match_required(
        self,
    ) -> None:
        at = datetime(2026, 7, 28, 4, 30, tzinfo=UTC)
        option_token = InstrumentToken(
            Exchange.NFO,
            "CE1",
            "NIFTY",
            "NIFTY30JUL2625000CE",
            InstrumentKind.OPTION,
        )
        contract = OptionContract(
            underlying="NIFTY",
            expiry=date(2026, 7, 30),
            strike=Decimal("25000"),
            option_type=OptionType.CALL,
            token=option_token,
            lot_size=75,
        )
        quote = OptionQuote(
            contract=contract,
            ltp=Decimal("100"),
            oi=10000,
            volume=20000,
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
            expiry=contract.expiry,
            spot_price=Decimal("25000"),
            atm_strike=Decimal("25000"),
            captured_at=at,
            quotes=(quote,),
        )
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=at,
            atm_strike=Decimal("25000"),
            signal="BUY_CALL",
            signal_reason="DERIVATIVES QUANT aligned",
            target_strike=Decimal("25000"),
            target_option_type=OptionType.CALL,
            setup_type=SignalSetup.DERIVATIVES_QUANT,
            selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
            market_regime=MarketRegime.UNKNOWN,
        )
        gate = SignalGate(
            SignalGateSettings(
                min_confirmations=1,
                cooldown_seconds=60,
                max_level_distance=Decimal("10"),
                max_microstructure_age_seconds=5,
                mode="shadow",
                require_regime_match=True,
            )
        )
        _gated, decision = gate.evaluate(
            snapshot=snapshot,
            analytics=analytics,
            microstructure_signal=None,
        )

        self.assertFalse(decision.qualified)
        self.assertIn("regime", decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
