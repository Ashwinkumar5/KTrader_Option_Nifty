from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import load_settings
from app.core.strategy_config import load_strategy_configuration
from app.domain.models import StrategyResolverPolicy
from app.storage.serialization import to_jsonable
from dummy_broker_replay.reader import RecordedSessionReader
from dummy_broker_replay.research_features import (
    DIRECTIONAL_FEATURES,
    DISABLED_PRICE_ACTION_FEATURES,
    PAIRED_BASELINE_FEATURES,
    RESEARCH_FEATURES,
    experiment_mode,
    feature_role,
)
from dummy_broker_replay.runner import ReplayMode, ReplayResult, run_replay
from dummy_broker_replay.validate_tape import validate_tape


PHASE1_FEATURES = RESEARCH_FEATURES
PRICE_ACTION_FEATURES = DISABLED_PRICE_ACTION_FEATURES
PAIRED_BASELINE_PROFILE = "phase1_paired_baseline"

_DIRECTION_WEIGHTS: dict[str, dict[str, str]] = {
    "premium_response": {"option_premium_momentum": "1.0"},
    "futures_flow": {"futures_flow": "1.0"},
    "consolidated_pcr": {"pcr_context": "1.0"},
    "strike_pcr": {"pcr_context": "1.0"},
    "volume_oi": {
        "option_volume_flow": "0.65",
        "oi_migration": "0.35",
    },
    "iv_skew": {"iv_skew": "1.0"},
    "futures_basis": {"futures_basis": "1.0"},
}

_ALL_DIRECTION_INPUTS = (
    "futures_flow",
    "index_momentum",
    "option_premium_momentum",
    "option_volume_flow",
    "iv_skew",
    "oi_migration",
    "pcr_context",
    "futures_basis",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run EOD Phase-1 one-feature-at-a-time research for "
            "DERIVATIVES_QUANT and GAMMA_EXPANSION. Production configuration "
            "is never modified."
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
            / "phase1_features"
        ),
    )
    parser.add_argument("--phase1-id")
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
    parser.add_argument(
        "--base-profile",
        default="derivatives_only",
        help="Profile supplying thresholds not explicitly fixed by Phase 1",
    )
    parser.add_argument(
        "--features",
        help=(
            "Optional comma-separated subset. Defaults to all approved "
            "quantitative Phase-1 features."
        ),
    )
    parser.add_argument(
        "--analytics-trace",
        "--analytical-engine-stress",
        dest="analytics_traces",
        action="append",
        type=Path,
        default=[],
        help=(
            "Optional analytics trace/stress JSONL recorded with the tape. "
            "It is hashed into the audit trail; broker tape replay remains "
            "the causal source of results."
        ),
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_phase1_feature_research(args)))
    except ValueError as exc:
        parser.error(str(exc))


async def run_phase1_feature_research(args: argparse.Namespace) -> int:
    source_path = args.path.resolve()
    selected_features = _parse_features(
        str(getattr(args, "features", "") or "")
    )
    phase1_id = (
        getattr(args, "phase1_id", None)
        or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    output_root = args.output_root.resolve()
    output_directory = (
        output_root / f"{source_path.stem}_phase1_{phase1_id}"
    )
    output_directory.mkdir(parents=True, exist_ok=False)

    validation_summary, issues = validate_tape(source_path)
    schema_versions = validation_summary.get("schema_versions")
    if (
        not isinstance(schema_versions, dict)
        or int(schema_versions.get("4", 0)) <= 0
    ):
        issues.append(
            "Phase-1 feature research requires a completed schema-v4 tape"
        )
    trace_metadata = _trace_metadata(
        tuple(
            path.resolve()
            for path in getattr(args, "analytics_traces", ())
        )
    )
    audit_payload = {
        "schema_version": 1,
        "record_type": "phase1_capture_audit",
        "created_at": datetime.now(UTC),
        "source_path": source_path,
        "validation_passed": not issues,
        "issues": issues,
        "capture": validation_summary,
        "analytics_traces": trace_metadata,
        "analytics_trace_role": (
            "supplementary provenance only; outcomes are recalculated "
            "causally from the broker tape"
        ),
    }
    _write_json(output_directory / "capture_audit.json", audit_payload)
    if issues:
        (output_directory / "PHASE1_FAILED.txt").write_text(
            "Capture validation failed. Feature experiments were not run.\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n",
            encoding="utf-8",
        )
        print(
            "Phase-1 capture validation failed: "
            f"{output_directory / 'capture_audit.json'}"
        )
        return 1

    strategy_config_path = output_directory / "phase1_strategy_config.json"
    profile_names = _write_phase1_strategy_config(
        strategy_config_path,
        selected_features=selected_features,
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

    results: list[dict[str, Any]] = []
    paired_baseline_status: dict[str, Any] | None = None
    paired_features = tuple(
        feature
        for feature in selected_features
        if experiment_mode(feature) == "PAIRED_ABLATION"
    )
    if paired_features:
        baseline_settings = replace(
            load_settings(),
            strategy_config_path=str(strategy_config_path),
            strategy_profile=PAIRED_BASELINE_PROFILE,
            strategy_resolver_policy=(
                StrategyResolverPolicy.HIGHEST_CONFIDENCE.value
            ),
            signal_gate_min_directional_confirmations=1,
            signal_gate_min_independent_confirmation_families=1,
            signal_gate_min_score=60.0,
            premium_transmission_enabled=False,
        )
        print("[baseline] Phase 1 paired directional baseline")
        baseline_result = await run_replay(
            source_path,
            mode=args.mode,
            output_root=output_directory,
            run_id="b00",
            max_frames=args.max_frames,
            settings=baseline_settings,
            source_sha256=source_sha256,
            event_index_path=shared_event_index,
            session_audit=shared_audit,
            decision_file_name="broker_tape_paired_baseline.jsonl",
            write_all_decisions=False,
            run_directory_name="b00",
            round_trip_cost_percent=round_trip_cost_percent,
        )
        paired_baseline_status = _feature_status(
            "paired_baseline",
            baseline_result,
            role="BASELINE",
            mode="BASELINE",
        )
        baseline_path = (
            baseline_result.run_directory
            / "broker_tape_paired_baseline.jsonl"
        )
        with baseline_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    to_jsonable(paired_baseline_status),
                    separators=(",", ":"),
                )
                + "\n"
            )

    for index, feature in enumerate(selected_features, start=1):
        profile_name = profile_names[feature]
        settings = replace(
            load_settings(),
            strategy_config_path=str(strategy_config_path),
            strategy_profile=profile_name,
            strategy_resolver_policy=(
                StrategyResolverPolicy.HIGHEST_CONFIDENCE.value
            ),
            signal_gate_min_directional_confirmations=1,
            signal_gate_min_independent_confirmation_families=1,
            signal_gate_min_score=60.0,
            premium_transmission_enabled=False,
        )
        print(
            f"[{index}/{len(selected_features)}] Phase 1 feature: "
            f"{feature}"
        )
        result = await run_replay(
            source_path,
            mode=args.mode,
            output_root=output_directory,
            run_id=f"f{index:02d}",
            max_frames=args.max_frames,
            settings=settings,
            source_sha256=source_sha256,
            event_index_path=shared_event_index,
            session_audit=shared_audit,
            decision_file_name=f"broker_tape_{feature}.jsonl",
            write_all_decisions=False,
            run_directory_name=f"f{index:02d}",
            round_trip_cost_percent=round_trip_cost_percent,
        )
        status = _feature_status(
            feature,
            result,
            role=feature_role(feature),
            mode=experiment_mode(feature),
            baseline=(
                paired_baseline_status
                if experiment_mode(feature) == "PAIRED_ABLATION"
                else None
            ),
        )
        status_path = (
            result.run_directory / f"broker_tape_{feature}.jsonl"
        )
        with status_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(to_jsonable(status), separators=(",", ":"))
                + "\n"
            )
        results.append(status)

    report = {
        "schema_version": 2,
        "record_type": "phase1_feature_research_summary",
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
        "feature_count": len(results),
        "feature_taxonomy": {
            feature: feature_role(feature)
            for feature in PHASE1_FEATURES
        },
        "paired_baseline_features": PAIRED_BASELINE_FEATURES,
        "paired_baseline": paired_baseline_status,
        "automatic_production_selection": False,
        "selection_warning": (
            "Directional features run standalone. Context, confirmation and "
            "normalization features are measured as on/off additions to the "
            "same directional baseline. Do not promote from in-sample data."
        ),
        "experiments": results,
    }
    _write_json(output_directory / "phase1_summary.json", report)
    _write_csv(output_directory / "phase1_summary.csv", results)
    (output_directory / "phase1_report.txt").write_text(
        _format_report(results),
        encoding="utf-8",
    )
    (output_directory / "PHASE1_COMPLETE.txt").write_text(
        "Phase-1 quantitative feature research completed.\n"
        "Production strategy configuration was not changed.\n",
        encoding="utf-8",
    )
    print("")
    print(f"Phase 1 complete: {output_directory}")
    print(f"Features tested: {len(results)}")
    print("Production settings were not changed.")
    return 0


def _parse_features(value: str) -> tuple[str, ...]:
    if not value.strip():
        return PHASE1_FEATURES
    requested = tuple(
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    )
    unknown = tuple(
        item for item in requested if item not in PHASE1_FEATURES
    )
    if unknown:
        raise ValueError(
            "unknown Phase-1 features: "
            + ", ".join(unknown)
            + "; allowed: "
            + ", ".join(PHASE1_FEATURES)
        )
    if len(requested) != len(set(requested)):
        raise ValueError("Phase-1 feature list contains duplicates")
    return requested


def _write_phase1_strategy_config(
    path: Path,
    *,
    selected_features: tuple[str, ...],
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
    needs_paired_baseline = any(
        experiment_mode(feature) == "PAIRED_ABLATION"
        for feature in selected_features
    )
    if needs_paired_baseline:
        profiles[PAIRED_BASELINE_PROFILE] = _phase1_profile(
            base,
            enabled_features=PAIRED_BASELINE_FEATURES,
            description=(
                "EOD-only shared directional baseline for paired Phase-1 "
                "ablations; never approved for live use."
            ),
        )
    for feature in selected_features:
        profile_name = f"phase1_{feature}"
        profile_names[feature] = profile_name
        enabled_features = (
            (feature,)
            if feature in DIRECTIONAL_FEATURES
            else tuple(dict.fromkeys(PAIRED_BASELINE_FEATURES + (feature,)))
        )
        profile = _phase1_profile(
            base,
            enabled_features=enabled_features,
            description=(
                "EOD-only Phase-1 "
                f"{experiment_mode(feature).lower()} profile for {feature}; "
                "never approved for live use."
            ),
        )
        profiles[profile_name] = profile

    document = {
        "version": 1,
        "active_profile": profile_names[selected_features[0]],
        "research_only": True,
        "source_configuration": configuration.manifest(),
        "profiles": profiles,
    }
    _write_json(path, document)
    return profile_names


def _phase1_profile(
    base: dict[str, Any],
    *,
    enabled_features: tuple[str, ...],
    description: str,
) -> dict[str, Any]:
    profile = deepcopy(base)
    profile["description"] = description
    profile["strategies"] = {
        "DERIVATIVES_QUANT": {"enabled": True, "priority": 10},
        "GAMMA_EXPANSION": {"enabled": True, "priority": 20},
        "LEVEL_REVERSAL": {"enabled": False, "priority": 30},
        "BREAKOUT_MOMENTUM": {"enabled": False, "priority": 40},
    }
    enabled = set(enabled_features)
    profile["features"] = {
        name: name in enabled
        for name in PHASE1_FEATURES + PRICE_ACTION_FEATURES
    }

    active_weights: dict[str, Decimal] = {}
    for feature in enabled_features:
        for component, value in _DIRECTION_WEIGHTS.get(feature, {}).items():
            active_weights[component] = (
                active_weights.get(component, Decimal("0"))
                + Decimal(value)
            )
    total_weight = sum(active_weights.values(), Decimal("0"))
    if total_weight <= 0:
        raise ValueError("Phase-1 profile requires a directional feature")
    weights = {
        name: str(
            (
                active_weights.get(name, Decimal("0"))
                / total_weight
            ).quantize(Decimal("0.000001"))
        )
        for name in _ALL_DIRECTION_INPUTS
    }
    quant = dict(profile.get("quant") or {})
    quant.update(
        {
            "weights": weights,
            "minimum_direction_score": "0.18",
            "warmup_direction_score": "0.18",
            "early_direction_score": "0.12",
            "minimum_independent_families": 1,
            "minimum_horizon_agreement": 1,
            "early_min_independent_families": 1,
            "early_min_horizon_agreement": 1,
            "early_min_option_chain_families": 1,
            "minimum_buyability_score": "0",
            "early_min_buyability_score": "0",
            "early_score_persistence_frames": 1,
            "maximum_leg_chase_percent": "1000",
            "early_max_leg_chase_percent": "1000",
            "maximum_iv_rank": "100",
            "require_compression": False,
            "require_expansion_trigger": False,
            "require_futures_flow": False,
        }
    )
    profile["quant"] = quant

    order_book_enabled = "order_book_imbalance" in enabled
    microstructure = dict(profile.get("microstructure") or {})
    microstructure.update(
        {
            "require_target_option_confirmation": order_book_enabled,
            "require_futures_confirmation": False,
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
            "minimum_futures_confirmations": 0,
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
    return profile


def _feature_status(
    feature: str,
    result: ReplayResult,
    *,
    role: str,
    mode: str,
    baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed = (
        result.target_exits
        + result.stop_exits
        + result.time_exits
        + result.management_exits
    )
    target_rate = _rate(result.target_exits, completed)
    stop_rate = _rate(result.stop_exits, completed)
    status = {
        "schema_version": 2,
        "record_type": "phase1_feature_status",
        "feature": feature,
        "feature_role": role,
        "experiment_mode": mode,
        "run_directory": result.run_directory,
        "output_jsonl": (
            result.run_directory / f"broker_tape_{feature}.jsonl"
        ),
        "strategies_enabled": result.enabled_strategies,
        "selected_strategy_counts": result.selected_strategy_counts,
        "qualified_signal_counts_by_strategy": (
            result.qualified_strategy_counts
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
        "target_hit_rate_percent": target_rate,
        "stop_hit_rate_percent": stop_rate,
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
        "feature_coverage": result.feature_coverage,
        "target_feature_coverage": result.feature_coverage.get(feature),
        "frames_processed": result.frames_processed,
        "rejection_counts": result.rejection_counts,
        "status_definition": (
            "success=10% target first; failure=5% stop first; "
            "time/unresolved are reported separately"
        ),
        "jsonl_content": (
            "qualified signals, paper entries/exits, and final feature status; "
            "all rejected frames remain aggregated in rejection_counts"
        ),
    }
    if baseline is not None:
        status["paired_baseline_features"] = PAIRED_BASELINE_FEATURES
        status["baseline_completed_trades"] = baseline["completed_trades"]
        status["baseline_net_average_trade_return_percent"] = baseline[
            "net_average_trade_return_percent"
        ]
        status["baseline_net_paper_realized_pnl"] = baseline[
            "net_paper_realized_pnl"
        ]
        status["delta_completed_trades"] = (
            completed - int(baseline["completed_trades"])
        )
        status["delta_net_average_trade_return_percent"] = (
            result.net_average_trade_return_percent
            - Decimal(
                str(baseline["net_average_trade_return_percent"])
            )
        )
        status["delta_net_paper_realized_pnl"] = (
            result.net_paper_realized_pnl
            - Decimal(str(baseline["net_paper_realized_pnl"]))
        )
    return status


def _trading_date(timestamp: datetime | None) -> str | None:
    if timestamp is None:
        return None
    value = timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (
        Decimal(numerator)
        * Decimal("100")
        / Decimal(denominator)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _trace_metadata(paths: tuple[Path, ...]) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            raise ValueError(f"analytics trace does not exist: {path}")
        line_count = 0
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for line in handle:
                digest.update(line)
                if line.strip():
                    line_count += 1
        metadata.append(
            {
                "path": path,
                "size_bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
                "records": line_count,
            }
        )
    return metadata


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    columns = (
        "feature",
        "feature_role",
        "experiment_mode",
        "signals_generated",
        "trades_entered",
        "completed_trades",
        "successful_target_hits",
        "failed_stop_hits",
        "time_exits",
        "management_exits",
        "unresolved_at_tape_end",
        "target_hit_rate_percent",
        "stop_hit_rate_percent",
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
        "target_feature_coverage",
        "delta_net_average_trade_return_percent",
        "delta_net_paper_realized_pnl",
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


def _format_report(rows: list[dict[str, Any]]) -> str:
    lines = [
        "Phase-1 Quantitative Feature Research",
        "Success = +10% target before -5% stop.",
        "Time exits and positions open at tape end are not counted as stops.",
        "",
        (
            "feature | signals | trades | target | stop | time | open | "
            "target-rate | net-avg-return | delta-vs-baseline | coverage"
        ),
        "-" * 112,
    ]
    for row in rows:
        lines.append(
            f"{row['feature']} ({row['feature_role']}/"
            f"{row['experiment_mode']}) | "
            f"{row['signals_generated']} | "
            f"{row['trades_entered']} | "
            f"{row['successful_target_hits']} | "
            f"{row['failed_stop_hits']} | "
            f"{row['time_exits']} | "
            f"{row['unresolved_at_tape_end']} | "
            f"{row['target_hit_rate_percent']}% | "
            f"{row['net_average_trade_return_percent']}% | "
            f"{row.get('delta_net_average_trade_return_percent', '-')} | "
            f"{(row.get('target_feature_coverage') or {}).get('coverage_percent', 0)}%"
        )
    lines.extend(
        (
            "",
            "Research output only. Production configuration was not changed.",
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
