from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import load_settings
from app.core.strategy_config import load_strategy_configuration
from app.domain.models import StrategyResolverPolicy
from app.storage.serialization import to_jsonable
from dummy_broker_replay.reader import RecordedSessionReader
from dummy_broker_replay.research_features import (
    DISABLED_PRICE_ACTION_FEATURES,
    RESEARCH_FEATURES,
)
from dummy_broker_replay.run_phase1_feature_research import (
    _trace_metadata,
    _trading_date,
)
from dummy_broker_replay.runner import ReplayMode, ReplayResult, run_replay
from dummy_broker_replay.validate_tape import validate_tape

PHASE1_FEATURES = RESEARCH_FEATURES
PRICE_ACTION_FEATURES = DISABLED_PRICE_ACTION_FEATURES

@dataclass(frozen=True)
class CombinationSpec:
    name: str
    description: str
    features: tuple[str, ...]
    minimum_direction_families: int
    minimum_buyability_score: Decimal
    require_futures_flow: bool = False
    require_futures_book: bool = False


PHASE2_COMBINATIONS = (
    CombinationSpec(
        name="derivatives_flow_microstructure",
        description=(
            "Option premium, futures flow, participation, basis and "
            "same-side order-book pressure."
        ),
        features=(
            "premium_response",
            "futures_flow",
            "volume_oi",
            "futures_basis",
            "order_book_imbalance",
        ),
        minimum_direction_families=3,
        minimum_buyability_score=Decimal("0.30"),
        require_futures_flow=True,
        require_futures_book=True,
    ),
    CombinationSpec(
        name="options_positioning",
        description=(
            "Broad and strike PCR, volume/OI migration, IV skew and gamma "
            "concentration."
        ),
        features=(
            "consolidated_pcr",
            "strike_pcr",
            "volume_oi",
            "iv_skew",
            "gamma_concentration",
        ),
        minimum_direction_families=2,
        minimum_buyability_score=Decimal("0.35"),
    ),
    CombinationSpec(
        name="volatility_surface_regime",
        description=(
            "Expected move, IV surface/skew, India-VIX regime and straddle "
            "expansion, normalized by ATR."
        ),
        features=(
            "expected_move",
            "iv_surface",
            "iv_skew",
            "india_vix_regime",
            "atr_normalization",
            "straddle_expansion",
        ),
        minimum_direction_families=1,
        minimum_buyability_score=Decimal("0.40"),
    ),
    CombinationSpec(
        name="gamma_expansion_core",
        description=(
            "Gamma concentration with IV, skew, expected-move, straddle and "
            "premium expansion."
        ),
        features=(
            "gamma_concentration",
            "straddle_expansion",
            "iv_surface",
            "iv_skew",
            "expected_move",
            "premium_response",
        ),
        minimum_direction_families=2,
        minimum_buyability_score=Decimal("0.45"),
    ),
    CombinationSpec(
        name="flow_confirmed_gamma",
        description=(
            "Gamma/straddle expansion confirmed by option premium, volume/OI, "
            "futures flow and order book."
        ),
        features=(
            "gamma_concentration",
            "straddle_expansion",
            "premium_response",
            "volume_oi",
            "futures_flow",
            "order_book_imbalance",
        ),
        minimum_direction_families=2,
        minimum_buyability_score=Decimal("0.40"),
        require_futures_flow=True,
        require_futures_book=True,
    ),
    CombinationSpec(
        name="cross_market_derivatives",
        description=(
            "Options pricing and positioning confirmed across futures flow, "
            "basis, skew and both books."
        ),
        features=(
            "premium_response",
            "futures_flow",
            "futures_basis",
            "volume_oi",
            "strike_pcr",
            "iv_skew",
            "order_book_imbalance",
        ),
        minimum_direction_families=3,
        minimum_buyability_score=Decimal("0.35"),
        require_futures_flow=True,
        require_futures_book=True,
    ),
    CombinationSpec(
        name="full_quant_ensemble",
        description=(
            "All approved quantitative derivatives features; retained as the "
            "complete benchmark."
        ),
        features=PHASE1_FEATURES,
        minimum_direction_families=3,
        minimum_buyability_score=Decimal("0.50"),
        require_futures_flow=False,
        require_futures_book=True,
    ),
)

_DIRECTION_COMPONENT_FEATURES = {
    "futures_flow": ("futures_flow",),
    "index_momentum": (),
    "option_premium_momentum": ("premium_response",),
    "option_volume_flow": ("volume_oi",),
    "iv_skew": ("iv_skew",),
    "oi_migration": ("volume_oi",),
    "pcr_context": ("consolidated_pcr", "strike_pcr"),
    "futures_basis": ("futures_basis",),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run EOD Phase-2 logical quantitative feature combinations. "
            "Production configuration is never modified."
        )
    )
    parser.add_argument("path", type=Path, help="Completed schema-v4 broker tape")
    parser.add_argument(
        "--mode",
        choices=[item.value for item in ReplayMode],
        default=ReplayMode.EVENT_TIME.value,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=(
            ROOT_DIR
            / "dummy_broker_replay"
            / "runs"
            / "phase2_combinations"
        ),
    )
    parser.add_argument("--phase2-id")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument(
        "--round-trip-cost-percent",
        type=Decimal,
        default=Decimal("0.20"),
        help="Replay-only estimated round-trip cost as percent of premium.",
    )
    parser.add_argument(
        "--base-strategy-config",
        type=Path,
        help="Source strategy config; defaults to config/strategy_config.json",
    )
    parser.add_argument("--base-profile", default="derivatives_only")
    parser.add_argument(
        "--combinations",
        help=(
            "Optional comma-separated combination names. Defaults to all "
            "seven logical Phase-2 combinations."
        ),
    )
    parser.add_argument(
        "--minimum-ranking-trades",
        type=int,
        default=3,
        help=(
            "Minimum completed trades required for a combination to receive "
            "a same-session research rank."
        ),
    )
    parser.add_argument(
        "--phase1-summary",
        type=Path,
        help=(
            "Optional completed Phase-1 summary to hash into provenance. "
            "It does not automatically select or remove combinations."
        ),
    )
    parser.add_argument(
        "--analytics-trace",
        "--analytical-engine-stress",
        dest="analytics_traces",
        action="append",
        type=Path,
        default=[],
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_phase2_combination_research(args)))
    except ValueError as exc:
        parser.error(str(exc))


async def run_phase2_combination_research(
    args: argparse.Namespace,
) -> int:
    source_path = args.path.resolve()
    selected = _parse_combinations(
        str(getattr(args, "combinations", "") or "")
    )
    minimum_ranking_trades = int(
        getattr(args, "minimum_ranking_trades", 3)
    )
    if minimum_ranking_trades <= 0:
        raise ValueError("minimum-ranking-trades must be positive")
    phase2_id = (
        getattr(args, "phase2_id", None)
        or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_directory = (
        args.output_root.resolve()
        / f"{source_path.stem}_phase2_{phase2_id}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)

    validation_summary, issues = validate_tape(source_path)
    schema_versions = validation_summary.get("schema_versions")
    if (
        not isinstance(schema_versions, dict)
        or int(schema_versions.get("4", 0)) <= 0
    ):
        issues.append(
            "Phase-2 combination research requires a completed schema-v4 tape"
        )
    trace_metadata = _trace_metadata(
        tuple(
            path.resolve()
            for path in getattr(args, "analytics_traces", ())
        )
    )
    phase1_metadata = _phase1_summary_metadata(
        getattr(args, "phase1_summary", None)
    )
    audit_payload = {
        "schema_version": 1,
        "record_type": "phase2_capture_audit",
        "created_at": datetime.now(UTC),
        "source_path": source_path,
        "validation_passed": not issues,
        "issues": issues,
        "capture": validation_summary,
        "analytics_traces": trace_metadata,
        "phase1_summary": phase1_metadata,
        "provenance_rule": (
            "broker tape is the causal calculation source; Phase-1 summaries "
            "and analytics traces are provenance, not replay inputs"
        ),
    }
    _write_json(output_directory / "capture_audit.json", audit_payload)
    if issues:
        (output_directory / "PHASE2_FAILED.txt").write_text(
            "Capture validation failed. Combination experiments were not run.\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n",
            encoding="utf-8",
        )
        print(
            "Phase-2 capture validation failed: "
            f"{output_directory / 'capture_audit.json'}"
        )
        return 1

    strategy_config_path = output_directory / "phase2_strategy_config.json"
    profile_names = _write_phase2_strategy_config(
        strategy_config_path,
        combinations=selected,
        base_config_path=getattr(args, "base_strategy_config", None),
        base_profile=str(
            getattr(args, "base_profile", "derivatives_only")
        ),
    )
    shared_audit = RecordedSessionReader(source_path).audit()
    shared_event_index = output_directory / "event_time_index.sqlite"
    source_sha256 = str(validation_summary["sha256"])
    trading_date = _trading_date(shared_audit.first_timestamp)
    round_trip_cost_percent = Decimal(
        str(getattr(args, "round_trip_cost_percent", "0.20"))
    )

    statuses: list[dict[str, Any]] = []
    for index, combination in enumerate(selected, start=1):
        settings = replace(
            load_settings(),
            strategy_config_path=str(strategy_config_path),
            strategy_profile=profile_names[combination.name],
            strategy_resolver_policy=(
                StrategyResolverPolicy.HIGHEST_CONFIDENCE.value
            ),
            signal_gate_min_confirmations=1,
            signal_gate_min_directional_confirmations=1,
            signal_gate_min_independent_confirmation_families=2,
            signal_gate_min_score=65.0,
            premium_transmission_enabled=(
                "premium_response" in combination.features
            ),
        )
        print(
            f"[{index}/{len(selected)}] Phase 2 combination: "
            f"{combination.name}"
        )
        result = await run_replay(
            source_path,
            mode=args.mode,
            output_root=output_directory,
            run_id=f"c{index:02d}",
            max_frames=args.max_frames,
            settings=settings,
            source_sha256=source_sha256,
            event_index_path=shared_event_index,
            session_audit=shared_audit,
            decision_file_name=(
                f"broker_tape_{combination.name}.jsonl"
            ),
            write_all_decisions=False,
            run_directory_name=f"c{index:02d}",
            round_trip_cost_percent=round_trip_cost_percent,
        )
        status = _combination_status(combination, result)
        output_jsonl = (
            result.run_directory
            / f"broker_tape_{combination.name}.jsonl"
        )
        with output_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(to_jsonable(status), separators=(",", ":"))
                + "\n"
            )
        statuses.append(status)

    ranked = _rank_statuses(
        statuses,
        minimum_trades=minimum_ranking_trades,
    )
    report = {
        "schema_version": 2,
        "record_type": "phase2_combination_research_summary",
        "created_at": datetime.now(UTC),
        "source_path": source_path,
        "source_sha256": source_sha256,
        "trading_date": trading_date,
        "mode": args.mode,
        "max_frames": args.max_frames,
        "strategies": ("DERIVATIVES_QUANT", "GAMMA_EXPANSION"),
        "disabled_strategies": (
            "LEVEL_REVERSAL",
            "BREAKOUT_MOMENTUM",
        ),
        "always_disabled_price_action_features": PRICE_ACTION_FEATURES,
        "stop_percent": "5",
        "target_percent": "10",
        "maximum_hold_minutes": 15,
        "round_trip_cost_percent": round_trip_cost_percent,
        "minimum_ranking_trades": minimum_ranking_trades,
        "combination_count": len(ranked),
        "automatic_production_selection": False,
        "selection_warning": (
            "Ranks are same-session research diagnostics. Promotion requires "
            "consistent out-of-sample results across multiple market days."
        ),
        "experiments": ranked,
    }
    _write_json(output_directory / "phase2_summary.json", report)
    _write_csv(output_directory / "phase2_summary.csv", ranked)
    (output_directory / "phase2_report.txt").write_text(
        _format_report(ranked, minimum_ranking_trades),
        encoding="utf-8",
    )
    (output_directory / "PHASE2_COMPLETE.txt").write_text(
        "Phase-2 quantitative combination research completed.\n"
        "Production strategy configuration was not changed.\n",
        encoding="utf-8",
    )
    print("")
    print(f"Phase 2 complete: {output_directory}")
    print(f"Combinations tested: {len(ranked)}")
    print("Production settings were not changed.")
    return 0


def _parse_combinations(value: str) -> tuple[CombinationSpec, ...]:
    by_name = {
        combination.name: combination
        for combination in PHASE2_COMBINATIONS
    }
    if not value.strip():
        return PHASE2_COMBINATIONS
    requested = tuple(
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    )
    unknown = tuple(
        name for name in requested if name not in by_name
    )
    if unknown:
        raise ValueError(
            "unknown Phase-2 combinations: "
            + ", ".join(unknown)
            + "; allowed: "
            + ", ".join(by_name)
        )
    if len(requested) != len(set(requested)):
        raise ValueError("Phase-2 combination list contains duplicates")
    return tuple(by_name[name] for name in requested)


def _write_phase2_strategy_config(
    path: Path,
    *,
    combinations: tuple[CombinationSpec, ...],
    base_config_path: Path | None,
    base_profile: str,
) -> dict[str, str]:
    configuration = load_strategy_configuration(
        base_config_path,
        profile_name=base_profile,
    )
    base = configuration.manifest()["profile"]
    profiles: dict[str, dict[str, Any]] = {}
    profile_names: dict[str, str] = {}
    for combination in combinations:
        _validate_combination(combination)
        profile_name = f"phase2_{combination.name}"
        profile_names[combination.name] = profile_name
        profile = deepcopy(base)
        profile["description"] = (
            "EOD-only Phase-2 combination research: "
            + combination.description
        )
        profile["strategies"] = {
            "DERIVATIVES_QUANT": {"enabled": True, "priority": 10},
            "GAMMA_EXPANSION": {"enabled": True, "priority": 20},
            "LEVEL_REVERSAL": {"enabled": False, "priority": 30},
            "BREAKOUT_MOMENTUM": {"enabled": False, "priority": 40},
        }
        enabled_features = set(combination.features)
        profile["features"] = {
            name: name in enabled_features
            for name in PHASE1_FEATURES + PRICE_ACTION_FEATURES
        }

        quant = dict(profile.get("quant") or {})
        quant["weights"] = _normalized_weights(
            dict(quant.get("weights") or {}),
            enabled_features=enabled_features,
        )
        option_chain_inputs = sum(
            bool(enabled_features.intersection(features))
            for component, features in _DIRECTION_COMPONENT_FEATURES.items()
            if component
            in {
                "option_premium_momentum",
                "option_volume_flow",
                "iv_skew",
                "oi_migration",
                "pcr_context",
            }
        )
        quant.update(
            {
                "minimum_independent_families": (
                    combination.minimum_direction_families
                ),
                "early_min_independent_families": (
                    combination.minimum_direction_families
                ),
                "minimum_horizon_agreement": 2,
                "early_min_horizon_agreement": 2,
                "early_min_option_chain_families": max(
                    1,
                    min(2, option_chain_inputs),
                ),
                "minimum_buyability_score": str(
                    combination.minimum_buyability_score
                ),
                "early_min_buyability_score": str(
                    min(
                        Decimal("0.65"),
                        combination.minimum_buyability_score
                        + Decimal("0.10"),
                    )
                ),
                "require_compression": False,
                "require_expansion_trigger": True,
                "require_futures_flow": (
                    combination.require_futures_flow
                ),
            }
        )
        profile["quant"] = quant

        order_book_enabled = (
            "order_book_imbalance" in enabled_features
        )
        microstructure = dict(profile.get("microstructure") or {})
        microstructure.update(
            {
                "require_target_option_confirmation": order_book_enabled,
                "require_futures_confirmation": (
                    order_book_enabled
                    and combination.require_futures_book
                ),
                "minimum_option_confirmations": (
                    max(
                        1,
                        int(
                            microstructure.get(
                                "minimum_option_confirmations",
                                2,
                            )
                        ),
                    )
                    if order_book_enabled
                    else 0
                ),
                "minimum_futures_confirmations": (
                    1
                    if (
                        order_book_enabled
                        and combination.require_futures_book
                    )
                    else 0
                ),
            }
        )
        profile["microstructure"] = microstructure

        execution = dict(profile.get("execution") or {})
        execution.update(
            {
                "stop_percent": "5",
                "target_percent": "10",
                "maximum_hold_minutes": 15,
            }
        )
        profile["execution"] = execution
        profiles[profile_name] = profile

    document = {
        "version": 1,
        "active_profile": profile_names[combinations[0].name],
        "research_only": True,
        "source_configuration": configuration.manifest(),
        "profiles": profiles,
    }
    _write_json(path, document)
    return profile_names


def _normalized_weights(
    base_weights: dict[str, Any],
    *,
    enabled_features: set[str],
) -> dict[str, str]:
    active: dict[str, Decimal] = {}
    for component, feature_names in _DIRECTION_COMPONENT_FEATURES.items():
        if (
            feature_names
            and enabled_features.intersection(feature_names)
        ):
            value = Decimal(str(base_weights.get(component, "0")))
            if value > 0:
                active[component] = value
    total = sum(active.values(), Decimal("0"))
    if total <= 0:
        raise ValueError(
            "Phase-2 combination has no directional quantitative component"
        )
    return {
        component: (
            str((active.get(component, Decimal("0")) / total).quantize(
                Decimal("0.000001")
            ))
        )
        for component in _DIRECTION_COMPONENT_FEATURES
    }


def _validate_combination(combination: CombinationSpec) -> None:
    if not combination.features:
        raise ValueError(f"{combination.name} has no features")
    if len(combination.features) != len(set(combination.features)):
        raise ValueError(
            f"{combination.name} contains duplicate features"
        )
    unknown = set(combination.features) - set(PHASE1_FEATURES)
    if unknown:
        raise ValueError(
            f"{combination.name} contains unknown features: "
            + ", ".join(sorted(unknown))
        )
    directional_components = sum(
        bool(set(combination.features).intersection(features))
        for features in _DIRECTION_COMPONENT_FEATURES.values()
        if features
    )
    if (
        combination.minimum_direction_families <= 0
        or combination.minimum_direction_families
        > directional_components
    ):
        raise ValueError(
            f"{combination.name} minimum direction families exceed "
            "its active directional components"
        )
    if not Decimal("0") <= combination.minimum_buyability_score <= Decimal("1"):
        raise ValueError(
            f"{combination.name} buyability score must be between zero and one"
        )
    if (
        combination.require_futures_flow
        and "futures_flow" not in combination.features
    ):
        raise ValueError(
            f"{combination.name} requires an unavailable futures-flow feature"
        )
    if (
        combination.require_futures_book
        and "order_book_imbalance" not in combination.features
    ):
        raise ValueError(
            f"{combination.name} requires an unavailable order-book feature"
        )


def _combination_status(
    combination: CombinationSpec,
    result: ReplayResult,
) -> dict[str, Any]:
    completed = (
        result.target_exits
        + result.stop_exits
        + result.time_exits
        + result.management_exits
    )
    return {
        "schema_version": 2,
        "record_type": "phase2_combination_status",
        "combination": combination.name,
        "description": combination.description,
        "features": combination.features,
        "run_directory": result.run_directory,
        "output_jsonl": (
            result.run_directory
            / f"broker_tape_{combination.name}.jsonl"
        ),
        "strategies_enabled": result.enabled_strategies,
        "qualified_signal_counts_by_strategy": (
            result.qualified_strategy_counts
        ),
        "derivatives_quant_signals": result.qualified_strategy_counts.get(
            "DERIVATIVES_QUANT",
            0,
        ),
        "gamma_expansion_signals": result.qualified_strategy_counts.get(
            "GAMMA_EXPANSION",
            0,
        ),
        "paper_outcomes_by_strategy": (
            result.paper_outcomes_by_strategy
        ),
        "signals_generated": result.replay_qualified,
        "trades_entered": result.paper_entries,
        "completed_trades": completed,
        "successful_target_hits": result.target_exits,
        "failed_stop_hits": result.stop_exits,
        "time_exits": result.time_exits,
        "management_exits": result.management_exits,
        "unresolved_at_tape_end": result.unresolved_positions,
        "target_hit_rate_percent": _rate(
            result.target_exits,
            completed,
        ),
        "stop_hit_rate_percent": _rate(
            result.stop_exits,
            completed,
        ),
        "completed_trade_return_percent": (
            result.completed_trade_return_percent
        ),
        "average_trade_return_percent": (
            result.average_trade_return_percent
        ),
        "maximum_trade_drawdown_percent": (
            result.maximum_trade_drawdown_percent
        ),
        "paper_realized_pnl": result.paper_realized_pnl,
        "round_trip_cost_percent": result.round_trip_cost_percent,
        "estimated_transaction_cost": result.estimated_transaction_cost,
        "net_completed_trade_return_percent": (
            result.net_completed_trade_return_percent
        ),
        "net_average_trade_return_percent": (
            result.net_average_trade_return_percent
        ),
        "net_maximum_trade_drawdown_percent": (
            result.net_maximum_trade_drawdown_percent
        ),
        "net_paper_realized_pnl": result.net_paper_realized_pnl,
        "average_maximum_favorable_excursion_percent": (
            result.average_maximum_favorable_excursion_percent
        ),
        "average_maximum_adverse_excursion_percent": (
            result.average_maximum_adverse_excursion_percent
        ),
        "feature_coverage": {
            feature: result.feature_coverage[feature]
            for feature in (
                combination.features
                + (
                    ("option_book", "futures_book")
                    if "order_book_imbalance" in combination.features
                    else ()
                )
            )
            if feature in result.feature_coverage
        },
        "frames_processed": result.frames_processed,
        "rejection_counts": result.rejection_counts,
        "status_definition": (
            "success=10% target first; failure=5% stop first; "
            "time/unresolved are separate"
        ),
    }


def _rank_statuses(
    statuses: list[dict[str, Any]],
    *,
    minimum_trades: int,
) -> list[dict[str, Any]]:
    eligible = [
        status
        for status in statuses
        if int(status["completed_trades"]) >= minimum_trades
    ]
    eligible.sort(
        key=lambda status: (
            -Decimal(
                str(
                    status.get(
                        "net_average_trade_return_percent",
                        status["average_trade_return_percent"],
                    )
                )
            ),
            Decimal(
                str(
                    status.get(
                        "net_maximum_trade_drawdown_percent",
                        status["maximum_trade_drawdown_percent"],
                    )
                )
            ),
            -Decimal(str(status["target_hit_rate_percent"])),
            -int(status["completed_trades"]),
            str(status["combination"]),
        )
    )
    rank_by_name = {
        str(status["combination"]): rank
        for rank, status in enumerate(eligible, start=1)
    }
    ranked: list[dict[str, Any]] = []
    for status in statuses:
        result = dict(status)
        rank = rank_by_name.get(str(status["combination"]))
        result["research_rank"] = rank
        result["ranking_eligible"] = rank is not None
        result["ranking_note"] = (
            "same-session diagnostic rank; out-of-sample validation required"
            if rank is not None
            else (
                f"requires at least {minimum_trades} completed trades; "
                f"observed {status['completed_trades']}"
            )
        )
        ranked.append(result)
    return ranked


def _phase1_summary_metadata(
    path: Path | None,
) -> dict[str, Any] | None:
    if path is None:
        return None
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"Phase-1 summary does not exist: {resolved}")
    raw = resolved.read_bytes()
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or document.get("record_type")
        != "phase1_feature_research_summary"
    ):
        raise ValueError(
            "phase1-summary is not a Phase-1 feature research summary"
        )
    return {
        "path": resolved,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source_sha256": document.get("source_sha256"),
        "feature_count": document.get("feature_count"),
    }


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (
        Decimal(numerator)
        * Decimal("100")
        / Decimal(denominator)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    columns = (
        "research_rank",
        "ranking_eligible",
        "combination",
        "features",
        "signals_generated",
        "derivatives_quant_signals",
        "gamma_expansion_signals",
        "trades_entered",
        "completed_trades",
        "successful_target_hits",
        "failed_stop_hits",
        "time_exits",
        "unresolved_at_tape_end",
        "target_hit_rate_percent",
        "completed_trade_return_percent",
        "average_trade_return_percent",
        "maximum_trade_drawdown_percent",
        "paper_realized_pnl",
        "round_trip_cost_percent",
        "estimated_transaction_cost",
        "net_completed_trade_return_percent",
        "net_average_trade_return_percent",
        "net_maximum_trade_drawdown_percent",
        "net_paper_realized_pnl",
        "feature_coverage",
        "frames_processed",
        "output_jsonl",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: str(to_jsonable(row.get(key, "")))
                    for key in columns
                }
            )


def _format_report(
    rows: list[dict[str, Any]],
    minimum_trades: int,
) -> str:
    lines = [
        "Phase-2 Quantitative Combination Research",
        "Success = +10% target before -5% stop.",
        (
            f"Research rank requires at least {minimum_trades} completed "
            "trades and is not a production recommendation."
        ),
        "",
        (
            "rank | combination | signals | trades | target | stop | time | "
            "open | target-rate | net-avg-return | net-drawdown"
        ),
        "-" * 150,
    ]
    ordered = sorted(
        rows,
        key=lambda row: (
            row["research_rank"] is None,
            row["research_rank"] or 9999,
            str(row["combination"]),
        ),
    )
    for row in ordered:
        lines.append(
            f"{row['research_rank'] or '-'} | "
            f"{row['combination']} | "
            f"{row['signals_generated']} | "
            f"{row['trades_entered']} | "
            f"{row['successful_target_hits']} | "
            f"{row['failed_stop_hits']} | "
            f"{row['time_exits']} | "
            f"{row['unresolved_at_tape_end']} | "
            f"{row['target_hit_rate_percent']}% | "
            f"{row['net_average_trade_return_percent']}% | "
            f"{row['net_maximum_trade_drawdown_percent']}%"
        )
    lines.extend(
        (
            "",
            "Production configuration was not changed.",
            "Validate promising combinations across multiple unseen days.",
        )
    )
    return "\n".join(lines) + "\n"


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(to_jsonable(value), indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
