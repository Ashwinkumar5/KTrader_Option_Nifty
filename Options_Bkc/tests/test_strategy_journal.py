from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.domain.models import (
    AnalyticsSnapshot,
    EvidenceFamily,
    OptionType,
    StrategyCandidate,
    StrategyEvidence,
    StrategyFamily,
)
from app.storage.strategy_journal import (
    StrategyJournal,
    journal_features,
    strategy_journal_filename,
)


_AT = datetime(2026, 8, 12, 4, 0, tzinfo=UTC)


class StrategyJournalTests(unittest.TestCase):
    def test_filename_uses_strategy_name_and_session_timestamp(self) -> None:
        self.assertEqual(
            strategy_journal_filename("DERIVATIVES_QUANT", _AT),
            "DERIVATIVES_QUANT_20260812_040000.journal.log",
        )
        with self.assertRaises(ValueError):
            strategy_journal_filename("../unsafe", _AT)
        with self.assertRaises(ValueError):
            strategy_journal_filename(
                "SMC",
                datetime(2026, 8, 12, 4, 0),
            )

    def test_features_are_readable_and_limited_to_five(self) -> None:
        features = journal_features(_analytics(), "DERIVATIVES_QUANT")

        self.assertEqual(len(features), 5)
        self.assertEqual(
            [feature.render() for feature in features],
            [
                "mandatory_structure=PASS:MANDATORY",
                "mandatory_flow=PASS:MANDATORY",
                "futures_flow=PASS:NON_MANDATORY",
                "option_premium_momentum=PASS:NON_MANDATORY",
                "iv_skew=PASS:NON_MANDATORY",
            ],
        )

    def test_records_only_target_state_changes_with_call_put_side(self) -> None:
        with tempfile.TemporaryDirectory(dir=_test_directory()) as directory:
            path = Path(directory) / "DERIVATIVES_QUANT_20260812_040000.journal.log"

            async def exercise() -> tuple[str | None, str | None, str | None]:
                journal = StrategyJournal(
                    path,
                    strategy_name="DERIVATIVES_QUANT",
                )
                await journal.start()
                first = journal.record_target(
                    analytics=_analytics(),
                    state="TRYING_TO_ACQUIRE",
                )
                duplicate = journal.record_target(
                    analytics=_analytics(),
                    state="TRYING_TO_ACQUIRE",
                )
                acquired = journal.record_target(
                    analytics=_analytics(),
                    state="ACQUIRED",
                    router_status="ROUTER_CLIENT_QUEUED",
                )
                await journal.close()
                return first, duplicate, acquired

            first, duplicate, acquired = asyncio.run(exercise())

            self.assertIsNotNone(first)
            self.assertIsNone(duplicate)
            self.assertIsNotNone(acquired)
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("STRATEGY=DERIVATIVES_QUANT", lines[0])
            self.assertIn("STRIKE=24500", lines[0])
            self.assertIn("SIDE=PUT", lines[0])
            self.assertIn("STATE=TRYING_TO_ACQUIRE", lines[0])
            self.assertIn("FEATURES=mandatory_structure=PASS:MANDATORY", lines[0])
            self.assertIn("ROUTER=ROUTER_CLIENT_QUEUED", lines[1])

    def test_ignores_candidates_without_target_or_strategy_evidence(self) -> None:
        analytics = AnalyticsSnapshot(
            underlying="NIFTY",
            captured_at=_AT,
            atm_strike=Decimal("24500"),
        )
        with tempfile.TemporaryDirectory(dir=_test_directory()) as directory:
            path = Path(directory) / "SMC_20260812_040000.journal.log"

            async def exercise() -> None:
                journal = StrategyJournal(path, strategy_name="SMC")
                await journal.start()
                self.assertIsNone(
                    journal.record_target(
                        analytics=analytics,
                        state="TRYING_TO_ACQUIRE",
                    )
                )
                await journal.close()

            asyncio.run(exercise())
            self.assertFalse(path.exists())


def _analytics() -> AnalyticsSnapshot:
    evidence = (
        StrategyEvidence(
            "futures_flow",
            EvidenceFamily.FLOW,
            "BUY_PUT",
            Decimal("0.90"),
        ),
        StrategyEvidence(
            "option_premium_momentum",
            EvidenceFamily.FLOW,
            "BUY_PUT",
            Decimal("0.80"),
        ),
        StrategyEvidence(
            "iv_skew",
            EvidenceFamily.VOLATILITY,
            "BUY_PUT",
            Decimal("0.70"),
        ),
        StrategyEvidence(
            "sixth_optional_feature",
            EvidenceFamily.POSITIONING,
            "BUY_PUT",
            Decimal("0.60"),
        ),
        StrategyEvidence(
            "mandatory_structure",
            EvidenceFamily.STRUCTURE,
            "BUY_PUT",
            Decimal("0.20"),
            mandatory=True,
        ),
        StrategyEvidence(
            "mandatory_flow",
            EvidenceFamily.FLOW,
            "BUY_PUT",
            Decimal("0.10"),
            mandatory=True,
        ),
    )
    candidate = StrategyCandidate(
        family=StrategyFamily.DERIVATIVES_QUANT,
        side="BUY_PUT",
        setup_type="DERIVATIVES_QUANT",
        reason="journal test candidate",
        evidence=evidence,
    )
    return AnalyticsSnapshot(
        underlying="NIFTY",
        captured_at=_AT,
        atm_strike=Decimal("24500"),
        signal="BUY_PUT",
        target_strike=Decimal("24500"),
        target_option_type=OptionType.PUT,
        strategy_candidates=(candidate,),
        selected_strategy=StrategyFamily.DERIVATIVES_QUANT,
    )


def _test_directory() -> Path:
    directory = Path(".test-tmp") / "strategy-journal"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
