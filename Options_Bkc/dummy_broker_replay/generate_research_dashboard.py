from __future__ import annotations

import argparse
import csv
import html
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from app.storage.serialization import to_jsonable


PHASE1_RECORD_TYPE = "phase1_feature_research_summary"
PHASE2_RECORD_TYPE = "phase2_combination_research_summary"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a daily and rolling consolidated dashboard from completed "
            "Phase-1 and Phase-2 EOD research summaries."
        )
    )
    parser.add_argument(
        "--phase1-summary",
        required=True,
        action="append",
        type=Path,
    )
    parser.add_argument(
        "--phase2-summary",
        required=True,
        action="append",
        type=Path,
    )
    parser.add_argument("--reports-root", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument("--rolling-days", type=int, default=14)
    parser.add_argument("--minimum-cumulative-trades", type=int, default=30)
    parser.add_argument(
        "--minimum-trading-days",
        "--minimum-history-sessions",
        dest="minimum_trading_days",
        type=int,
        default=8,
    )
    args = parser.parse_args()

    try:
        output = generate_dashboard(
            phase1_summary_path=args.phase1_summary,
            phase2_summary_path=args.phase2_summary,
            reports_root=args.reports_root,
            output_directory=args.output_directory,
            rolling_days=args.rolling_days,
            minimum_cumulative_trades=args.minimum_cumulative_trades,
            minimum_trading_days=args.minimum_trading_days,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Dashboard complete: {output / 'dashboard.html'}")


def generate_dashboard(
    *,
    phase1_summary_path: Path | Iterable[Path],
    phase2_summary_path: Path | Iterable[Path],
    reports_root: Path,
    output_directory: Path,
    rolling_days: int = 14,
    minimum_cumulative_trades: int = 30,
    minimum_trading_days: int = 8,
    minimum_history_sessions: int | None = None,
    as_of_date: date | None = None,
) -> Path:
    if rolling_days <= 0:
        raise ValueError("rolling-days must be positive")
    if minimum_cumulative_trades <= 0:
        raise ValueError("minimum-cumulative-trades must be positive")
    if minimum_history_sessions is not None:
        minimum_trading_days = minimum_history_sessions
    if minimum_trading_days <= 0:
        raise ValueError("minimum-trading-days must be positive")

    phase1_paths = _summary_paths(phase1_summary_path)
    phase2_paths = _summary_paths(phase2_summary_path)
    root = reports_root.resolve()
    output = output_directory.resolve()
    phase1_documents = _deduplicate_summaries(
        _summary_with_path(path, PHASE1_RECORD_TYPE)
        for path in phase1_paths
    )
    phase2_documents = _deduplicate_summaries(
        _summary_with_path(path, PHASE2_RECORD_TYPE)
        for path in phase2_paths
    )
    phase1_sources = {
        str(document.get("source_sha256"))
        for document in phase1_documents
    }
    phase2_sources = {
        str(document.get("source_sha256"))
        for document in phase2_documents
    }
    if phase1_sources != phase2_sources:
        raise ValueError(
            "Phase-1 and Phase-2 summaries do not cover the same broker tapes"
        )

    current_date = as_of_date or datetime.now().astimezone().date()
    historical = discover_phase2_summaries(
        root,
        as_of_date=current_date,
        rolling_days=rolling_days,
    )
    historical.extend(phase2_documents)
    historical = _deduplicate_summaries(historical)

    phase1_rows = aggregate_features(phase1_documents)
    phase2_rows = aggregate_combinations(
        phase2_documents,
        minimum_cumulative_trades=minimum_cumulative_trades,
        minimum_trading_days=minimum_trading_days,
    )
    for row in phase2_rows:
        row["research_rank"] = row["rolling_rank"]
        row["maximum_trade_drawdown_percent"] = row[
            "worst_session_drawdown_percent"
        ]
    rolling_rows = aggregate_combinations(
        historical,
        minimum_cumulative_trades=minimum_cumulative_trades,
        minimum_trading_days=minimum_trading_days,
    )
    promising = [
        row
        for row in rolling_rows
        if row["research_status"] == "PROMISING"
    ]
    leader = promising[0]["combination"] if promising else None

    dashboard = {
        "schema_version": 2,
        "record_type": "consolidated_quant_research_dashboard",
        "generated_at": datetime.now(UTC),
        "as_of_date": current_date,
        "rolling_days": rolling_days,
        "batch_file_count": len(phase2_paths),
        "batch_tape_count": len(phase2_documents),
        "source_tapes": [
            document.get("source_path") for document in phase2_documents
        ],
        "source_sha256": [
            document.get("source_sha256") for document in phase2_documents
        ],
        "phase1_summaries": phase1_paths,
        "phase2_summaries": phase2_paths,
        "historical_phase2_summaries": [
            item["_summary_path"] for item in historical
        ],
        "history_tape_files": len(historical),
        "history_trading_days": len(
            {_summary_trading_date(item) for item in historical}
        ),
        "history_sessions": len(historical),
        "minimum_cumulative_trades": minimum_cumulative_trades,
        "minimum_trading_days": minimum_trading_days,
        "minimum_history_sessions": minimum_trading_days,
        "current_feature_results": phase1_rows,
        "current_combination_results": phase2_rows,
        "rolling_combination_results": rolling_rows,
        "rolling_research_leader": leader,
        "production_selection": False,
        "warning": (
            "Only cost-adjusted profitable, sample-eligible setups can become "
            "a research leader. Validate on unseen trading days before any "
            "production decision."
        ),
    }

    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "dashboard.json", dashboard)
    _write_csv(
        output / "daily_features.csv",
        phase1_rows,
        _feature_columns(),
    )
    _write_csv(
        output / "daily_combinations.csv",
        phase2_rows,
        _daily_combination_columns(),
    )
    _write_csv(
        output / f"rolling_{rolling_days}d_combinations.csv",
        rolling_rows,
        _rolling_columns(),
    )
    (output / "dashboard.html").write_text(
        _render_html(dashboard),
        encoding="utf-8",
    )
    (output / "README.txt").write_text(
        _readme_text(dashboard),
        encoding="utf-8",
    )
    return output


def _summary_paths(
    value: Path | Iterable[Path],
) -> tuple[Path, ...]:
    candidates = (value,) if isinstance(value, Path) else tuple(value)
    paths = tuple(Path(item).resolve() for item in candidates)
    if not paths:
        raise ValueError("at least one summary is required")
    return paths


def _summary_with_path(
    path: Path,
    expected_type: str,
) -> dict[str, Any]:
    document = dict(_load_summary(path, expected_type))
    document["_summary_path"] = str(path)
    return document


def discover_phase2_summaries(
    reports_root: Path,
    *,
    as_of_date: date,
    rolling_days: int,
) -> list[dict[str, Any]]:
    if not reports_root.exists():
        return []
    cutoff = as_of_date - timedelta(days=rolling_days - 1)
    summaries: list[dict[str, Any]] = []
    for path in reports_root.rglob("phase2_summary.json"):
        try:
            document = _load_summary(path, PHASE2_RECORD_TYPE)
            trading_date = date.fromisoformat(
                _summary_trading_date(document)
            )
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if cutoff <= trading_date <= as_of_date:
            item = dict(document)
            item["_summary_path"] = str(path.resolve())
            summaries.append(item)
    return _deduplicate_summaries(summaries)


def aggregate_features(
    summaries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[
        str,
        list[tuple[dict[str, Any], dict[str, Any]]],
    ] = defaultdict(list)
    for summary in summaries:
        for experiment in summary.get("experiments", []):
            if not isinstance(experiment, dict):
                continue
            name = str(experiment.get("feature", "")).strip()
            if name:
                grouped[name].append((summary, experiment))

    results: list[dict[str, Any]] = []
    for name, records in grouped.items():
        totals = {
            field: sum(
                _integer(experiment.get(field))
                for _, experiment in records
            )
            for field in (
                "signals_generated",
                "trades_entered",
                "completed_trades",
                "successful_target_hits",
                "failed_stop_hits",
                "time_exits",
                "management_exits",
                "unresolved_at_tape_end",
            )
        }
        completed = totals["completed_trades"]
        total_return = sum(
            (
                _decimal(
                    experiment.get("completed_trade_return_percent")
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        average_return = (
            total_return / Decimal(completed)
            if completed
            else Decimal("0")
        )
        maximum_drawdown = max(
            (
                _decimal(
                    experiment.get(
                        "net_maximum_trade_drawdown_percent",
                        experiment.get("maximum_trade_drawdown_percent"),
                    )
                )
                for _, experiment in records
            ),
            default=Decimal("0"),
        )
        gross_total_pnl = sum(
            (
                _decimal(experiment.get("paper_realized_pnl"))
                for _, experiment in records
            ),
            Decimal("0"),
        )
        net_total_pnl = sum(
            (
                _decimal(
                    experiment.get(
                        "net_paper_realized_pnl",
                        experiment.get("paper_realized_pnl"),
                    )
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        net_total_return = sum(
            (
                _decimal(
                    experiment.get(
                        "net_completed_trade_return_percent",
                        experiment.get("completed_trade_return_percent"),
                    )
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        net_average_return = (
            net_total_return / Decimal(completed)
            if completed
            else Decimal("0")
        )
        costs = sum(
            (
                _decimal(experiment.get("estimated_transaction_cost"))
                for _, experiment in records
            ),
            Decimal("0"),
        )
        coverage = _aggregate_feature_coverage(
            experiment.get("target_feature_coverage")
            for _, experiment in records
        )
        results.append(
            {
                "feature": name,
                "feature_role": records[0][1].get(
                    "feature_role",
                    "UNCLASSIFIED",
                ),
                "experiment_mode": records[0][1].get(
                    "experiment_mode",
                    "LEGACY_STANDALONE",
                ),
                "tape_files": len(records),
                "trading_days": len(
                    {
                        _summary_trading_date(summary)
                        for summary, _ in records
                    }
                ),
                "sessions": len(records),
                **totals,
                "target_hit_rate_percent": _percentage(
                    totals["successful_target_hits"],
                    completed,
                ),
                "stop_hit_rate_percent": _percentage(
                    totals["failed_stop_hits"],
                    completed,
                ),
                "completed_trade_return_percent": _quantize(
                    total_return
                ),
                "average_trade_return_percent": _quantize(
                    average_return
                ),
                "maximum_trade_drawdown_percent": _quantize(
                    maximum_drawdown
                ),
                "paper_realized_pnl": _quantize(gross_total_pnl),
                "net_completed_trade_return_percent": _quantize(
                    net_total_return
                ),
                "net_average_trade_return_percent": _quantize(
                    net_average_return
                ),
                "net_paper_realized_pnl": _quantize(net_total_pnl),
                "estimated_transaction_cost": _quantize(costs),
                "feature_coverage_percent": coverage["coverage_percent"],
                "coverage_available_frames": coverage["available_frames"],
                "coverage_total_frames": coverage["total_frames"],
                "delta_net_average_trade_return_percent": _quantize(
                    sum(
                        (
                            _decimal(
                                experiment.get(
                                    "delta_net_average_trade_return_percent"
                                )
                            )
                            for _, experiment in records
                        ),
                        Decimal("0"),
                    )
                    / Decimal(len(records))
                ),
                "delta_net_paper_realized_pnl": _quantize(
                    sum(
                        (
                            _decimal(
                                experiment.get(
                                    "delta_net_paper_realized_pnl"
                                )
                            )
                            for _, experiment in records
                        ),
                        Decimal("0"),
                    )
                ),
            }
        )
    results.sort(
        key=lambda row: (
            -_decimal(row["net_average_trade_return_percent"]),
            -_decimal(row["target_hit_rate_percent"]),
            _decimal(row["maximum_trade_drawdown_percent"]),
            str(row["feature"]),
        )
    )
    return results


def aggregate_combinations(
    summaries: Iterable[dict[str, Any]],
    *,
    minimum_cumulative_trades: int,
    minimum_trading_days: int | None = None,
    minimum_history_sessions: int | None = None,
) -> list[dict[str, Any]]:
    if minimum_trading_days is None:
        minimum_trading_days = minimum_history_sessions or 1
    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
        defaultdict(list)
    )
    for summary in summaries:
        for experiment in summary.get("experiments", []):
            if not isinstance(experiment, dict):
                continue
            name = str(experiment.get("combination", "")).strip()
            if name:
                grouped[name].append((summary, experiment))

    results: list[dict[str, Any]] = []
    for name, records in grouped.items():
        totals = {
            field: sum(
                _integer(experiment.get(field))
                for _, experiment in records
            )
            for field in (
                "signals_generated",
                "trades_entered",
                "completed_trades",
                "successful_target_hits",
                "failed_stop_hits",
                "time_exits",
                "management_exits",
                "unresolved_at_tape_end",
            )
        }
        completed = totals["completed_trades"]
        decisive = (
            totals["successful_target_hits"]
            + totals["failed_stop_hits"]
        )
        gross_total_return = sum(
            (
                _decimal(
                    experiment.get("completed_trade_return_percent")
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        gross_total_pnl = sum(
            (
                _decimal(experiment.get("paper_realized_pnl"))
                for _, experiment in records
            ),
            Decimal("0"),
        )
        worst_drawdown = max(
            (
                _decimal(
                    experiment.get(
                        "net_maximum_trade_drawdown_percent",
                        experiment.get(
                            "maximum_trade_drawdown_percent"
                        ),
                    )
                )
                for _, experiment in records
            ),
            default=Decimal("0"),
        )
        total_return = sum(
            (
                _decimal(
                    experiment.get(
                        "net_completed_trade_return_percent",
                        experiment.get("completed_trade_return_percent"),
                    )
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        total_pnl = sum(
            (
                _decimal(
                    experiment.get(
                        "net_paper_realized_pnl",
                        experiment.get("paper_realized_pnl"),
                    )
                )
                for _, experiment in records
            ),
            Decimal("0"),
        )
        costs = sum(
            (
                _decimal(experiment.get("estimated_transaction_cost"))
                for _, experiment in records
            ),
            Decimal("0"),
        )
        daily_returns: dict[str, Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        for summary, experiment in records:
            daily_returns[_summary_trading_date(summary)] += _decimal(
                experiment.get(
                    "net_completed_trade_return_percent",
                    experiment.get("completed_trade_return_percent"),
                )
            )
        profitable_trading_days = sum(
            value > 0 for value in daily_returns.values()
        )
        combined_coverage = _aggregate_combination_coverage(
            experiment.get("feature_coverage")
            for _, experiment in records
        )
        tape_file_count = len(records)
        trading_day_count = len(daily_returns)
        average_return = (
            total_return / Decimal(completed)
            if completed
            else Decimal("0")
        )
        target_rate = _percentage(
            totals["successful_target_hits"],
            completed,
        )
        decisive_win_rate = _percentage(
            totals["successful_target_hits"],
            decisive,
        )
        eligible = (
            completed >= minimum_cumulative_trades
            and trading_day_count >= minimum_trading_days
        )
        if not eligible:
            status = "INSUFFICIENT_SAMPLE"
        elif average_return > 0 and total_pnl > 0:
            status = "PROMISING"
        else:
            status = "NOT_PROFITABLE"
        results.append(
            {
                "rolling_rank": None,
                "ranking_eligible": eligible,
                "research_status": status,
                "combination": name,
                "features": records[0][1].get("features", ()),
                "tape_files": tape_file_count,
                "trading_days": trading_day_count,
                "sessions": tape_file_count,
                "profitable_trading_days": profitable_trading_days,
                "profitable_sessions": profitable_trading_days,
                **totals,
                "derivatives_quant_signals": sum(
                    _strategy_signal_count(
                        experiment,
                        "DERIVATIVES_QUANT",
                    )
                    for _, experiment in records
                ),
                "gamma_expansion_signals": sum(
                    _strategy_signal_count(
                        experiment,
                        "GAMMA_EXPANSION",
                    )
                    for _, experiment in records
                ),
                "target_hit_rate_percent": target_rate,
                "decisive_win_rate_percent": decisive_win_rate,
                "gross_completed_trade_return_percent": _quantize(
                    gross_total_return
                ),
                "gross_paper_realized_pnl": _quantize(
                    gross_total_pnl
                ),
                "completed_trade_return_percent": _quantize(total_return),
                "average_trade_return_percent": _quantize(
                    average_return
                ),
                "net_completed_trade_return_percent": _quantize(
                    total_return
                ),
                "net_average_trade_return_percent": _quantize(
                    average_return
                ),
                "worst_session_drawdown_percent": _quantize(
                    worst_drawdown
                ),
                "worst_trading_day_drawdown_percent": _quantize(
                    worst_drawdown
                ),
                "paper_realized_pnl": _quantize(total_pnl),
                "net_paper_realized_pnl": _quantize(total_pnl),
                "estimated_transaction_cost": _quantize(costs),
                "feature_coverage": combined_coverage,
                "low_coverage_features": tuple(
                    feature
                    for feature, item in combined_coverage.items()
                    if _decimal(item.get("coverage_percent")) < 80
                ),
            }
        )

    promising_rows = [
        row
        for row in results
        if row["research_status"] == "PROMISING"
    ]
    promising_rows.sort(
        key=lambda row: (
            -_decimal(row["average_trade_return_percent"]),
            -_decimal(row["decisive_win_rate_percent"]),
            _decimal(row["worst_session_drawdown_percent"]),
            -_integer(row["completed_trades"]),
            str(row["combination"]),
        )
    )
    for rank, row in enumerate(promising_rows, start=1):
        row["rolling_rank"] = rank
    results.sort(
        key=lambda row: (
            row["rolling_rank"] is None,
            row["rolling_rank"] or 999999,
            str(row["combination"]),
        )
    )
    return results


def _deduplicate_summaries(
    summaries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    newest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for summary in summaries:
        source_key = str(
            summary.get("source_sha256")
            or summary.get("source_path")
            or summary.get("_summary_path")
        )
        created_at = _parse_timestamp(summary.get("created_at"))
        existing = newest.get(source_key)
        if existing is None or created_at > existing[0]:
            newest[source_key] = (created_at, summary)
    return [
        item[1]
        for item in sorted(
            newest.values(),
            key=lambda value: value[0],
        )
    ]


def _load_summary(path: Path, expected_type: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"summary does not exist: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("record_type") != expected_type
    ):
        raise ValueError(
            f"{path} is not a {expected_type} document"
        )
    return document


def _parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("summary has no created_at timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _summary_trading_date(summary: dict[str, Any]) -> str:
    explicit = str(summary.get("trading_date") or "").strip()
    if explicit:
        return date.fromisoformat(explicit).isoformat()
    for field in ("source_path", "_summary_path"):
        match = re.search(
            r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)",
            str(summary.get(field) or ""),
        )
        if match:
            return date.fromisoformat(match.group(1)).isoformat()
    try:
        return (
            _parse_timestamp(summary.get("created_at"))
            .astimezone(ZoneInfo("Asia/Kolkata"))
            .date()
            .isoformat()
        )
    except ValueError:
        identity = (
            summary.get("source_sha256")
            or summary.get("source_path")
            or summary.get("_summary_path")
            or id(summary)
        )
        return f"unknown-{identity}"


def _strategy_signal_count(
    experiment: dict[str, Any],
    strategy: str,
) -> int:
    explicit_field = {
        "DERIVATIVES_QUANT": "derivatives_quant_signals",
        "GAMMA_EXPANSION": "gamma_expansion_signals",
    }.get(strategy)
    if explicit_field and explicit_field in experiment:
        return _integer(experiment.get(explicit_field))
    counts = experiment.get("qualified_signal_counts_by_strategy")
    if not isinstance(counts, dict):
        return 0
    return _integer(counts.get(strategy))


def _aggregate_feature_coverage(
    items: Iterable[object],
) -> dict[str, Decimal | int]:
    available = 0
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        available += _integer(item.get("available_frames"))
        total += _integer(item.get("total_frames"))
    return {
        "available_frames": available,
        "total_frames": total,
        "coverage_percent": _percentage(available, total),
    }


def _aggregate_combination_coverage(
    items: Iterable[object],
) -> dict[str, dict[str, Decimal | int]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for item in items:
        if not isinstance(item, dict):
            continue
        for feature, coverage in item.items():
            grouped[str(feature)].append(coverage)
    return {
        feature: _aggregate_feature_coverage(values)
        for feature, values in sorted(grouped.items())
    }


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _percentage(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return _quantize(
        Decimal(numerator) * Decimal("100") / Decimal(denominator)
    )


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(to_jsonable(payload), indent=2) + "\n",
        encoding="utf-8",
    )


def _write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    columns: tuple[str, ...],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: _csv_value(row.get(column))
                    for column in columns
                }
            )


def _csv_value(value: object) -> object:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(to_jsonable(value), separators=(",", ":"))
    return value


def _feature_columns() -> tuple[str, ...]:
    return (
        "feature",
        "feature_role",
        "experiment_mode",
        "tape_files",
        "trading_days",
        "feature_coverage_percent",
        "signals_generated",
        "trades_entered",
        "completed_trades",
        "successful_target_hits",
        "failed_stop_hits",
        "time_exits",
        "unresolved_at_tape_end",
        "target_hit_rate_percent",
        "net_average_trade_return_percent",
        "delta_net_average_trade_return_percent",
        "maximum_trade_drawdown_percent",
        "estimated_transaction_cost",
        "net_paper_realized_pnl",
    )


def _daily_combination_columns() -> tuple[str, ...]:
    return (
        "research_rank",
        "ranking_eligible",
        "research_status",
        "combination",
        "tape_files",
        "trading_days",
        "features",
        "derivatives_quant_signals",
        "gamma_expansion_signals",
        "low_coverage_features",
        "signals_generated",
        "trades_entered",
        "completed_trades",
        "successful_target_hits",
        "failed_stop_hits",
        "time_exits",
        "unresolved_at_tape_end",
        "target_hit_rate_percent",
        "net_average_trade_return_percent",
        "maximum_trade_drawdown_percent",
        "estimated_transaction_cost",
        "net_paper_realized_pnl",
    )


def _rolling_columns() -> tuple[str, ...]:
    return (
        "rolling_rank",
        "ranking_eligible",
        "research_status",
        "combination",
        "tape_files",
        "trading_days",
        "profitable_trading_days",
        "derivatives_quant_signals",
        "gamma_expansion_signals",
        "low_coverage_features",
        "signals_generated",
        "trades_entered",
        "completed_trades",
        "successful_target_hits",
        "failed_stop_hits",
        "time_exits",
        "management_exits",
        "unresolved_at_tape_end",
        "target_hit_rate_percent",
        "decisive_win_rate_percent",
        "completed_trade_return_percent",
        "net_average_trade_return_percent",
        "worst_trading_day_drawdown_percent",
        "estimated_transaction_cost",
        "net_paper_realized_pnl",
    )


def _render_html(dashboard: dict[str, Any]) -> str:
    daily_rows = dashboard["current_combination_results"]
    rolling_rows = dashboard["rolling_combination_results"]
    feature_rows = dashboard["current_feature_results"]
    leader = (
        dashboard["rolling_research_leader"]
        or "No profitable candidate"
    )
    total_daily_trades = sum(
        _integer(row.get("completed_trades")) for row in daily_rows
    )
    eligible_count = sum(
        bool(row.get("ranking_eligible")) for row in rolling_rows
    )
    style = """
:root{--ink:#172033;--muted:#667085;--line:#e6eaf0;--violet:#6d5dfc;--cyan:#08a9c9;--green:#13a471;--red:#dc4960;--amber:#e69a17}
*{box-sizing:border-box}body{font-family:Inter,Segoe UI,Arial,sans-serif;margin:0;min-height:100vh;color:var(--ink);background:radial-gradient(circle at 8% 0,#e7e5ff 0,transparent 28%),radial-gradient(circle at 94% 6%,#d9f8ff 0,transparent 24%),#f5f7fb}
.wrap{max-width:1540px;margin:auto;padding:28px 32px 42px}.hero{position:relative;overflow:hidden;padding:34px 38px 70px;border-radius:24px;color:#fff;background:linear-gradient(125deg,#111b49 0%,#4338a8 48%,#067e9d 100%);box-shadow:0 24px 55px #26348b35}
.hero:before,.hero:after{content:"";position:absolute;border-radius:50%;background:#ffffff12}.hero:before{width:270px;height:270px;right:-70px;top:-120px}.hero:after{width:180px;height:180px;right:170px;bottom:-135px}
.hero-grid{position:relative;z-index:1;display:flex;align-items:flex-end;justify-content:space-between;gap:22px}.eyebrow{font-size:12px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;color:#8de8ff}
h1{font-size:34px;line-height:1.12;margin:8px 0 10px;letter-spacing:-.7px}.hero-copy{max-width:700px;color:#dce6ff;font-size:14px;line-height:1.6}
.hero-tags{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px}.hero-tag{padding:8px 12px;border:1px solid #ffffff2e;border-radius:999px;background:#ffffff13;color:#f4f6ff;font-size:12px;backdrop-filter:blur(5px)}
.cards{position:relative;z-index:2;display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:16px;margin:-38px 20px 0}.card{position:relative;overflow:hidden;min-height:126px;padding:22px;border:1px solid #ffffff;background:#fff;border-radius:17px;box-shadow:0 12px 30px #1f2d5c1c}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:5px;background:var(--accent)}.card:after{content:"";position:absolute;width:82px;height:82px;right:-30px;top:-34px;border-radius:50%;background:var(--soft)}
.card small{display:block;color:var(--muted);font-weight:650}.card strong{display:block;margin-top:9px;font-size:25px;line-height:1.15;color:var(--accent);word-break:break-word}.card em{display:block;margin-top:9px;color:#98a2b3;font-size:11px;font-style:normal}
.violet{--accent:var(--violet);--soft:#eceaff}.cyan{--accent:var(--cyan);--soft:#ddf8fc}.green{--accent:var(--green);--soft:#ddf7ed}.amber{--accent:var(--amber);--soft:#fff2d6}
.warning{display:flex;align-items:flex-start;gap:12px;margin:20px 20px 0;padding:14px 17px;border:1px solid #f2d38f;border-radius:13px;background:linear-gradient(90deg,#fff9e9,#fffdf8);color:#7b5612;font-size:13px;line-height:1.5}.warning-mark{display:grid;place-items:center;flex:0 0 25px;height:25px;border-radius:50%;background:#f3b83c;color:#fff;font-weight:800}
.panel{margin-top:22px;padding:23px;border:1px solid #e8ebf2;border-radius:18px;background:#ffffffed;box-shadow:0 10px 28px #1c2c5c10;backdrop-filter:blur(8px)}.panel-head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:16px}
.section-label{color:var(--violet);font-size:11px;font-weight:800;letter-spacing:1.3px;text-transform:uppercase}h2{font-size:20px;margin:4px 0 0;letter-spacing:-.2px}.sub{max-width:720px;color:var(--muted);font-size:12px;line-height:1.5;text-align:right}
.table-shell{overflow:auto;border:1px solid var(--line);border-radius:13px}table{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}th,td{padding:11px 12px;border-bottom:1px solid #edf0f4;text-align:right;white-space:nowrap}
th{position:sticky;top:0;z-index:1;background:linear-gradient(180deg,#f9faff,#f2f5fb);color:#475467;font-size:10px;letter-spacing:.45px;text-transform:uppercase}th:first-child,td:first-child{text-align:left}tbody tr:nth-child(even){background:#fafbfe}tbody tr:hover{background:#f1f5ff}tbody tr:last-child td{border-bottom:0}
.name{font-weight:750;color:#243056}.key{display:block;margin-top:2px;color:#98a2b3;font-family:Consolas,monospace;font-size:10px;font-weight:400}.rank{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:50%;background:linear-gradient(135deg,#6d5dfc,#9b79ff);color:#fff;font-weight:800;box-shadow:0 4px 9px #6d5dfc44}
.status{display:inline-block;padding:5px 9px;border-radius:999px;font-size:9px;font-weight:850;letter-spacing:.55px}.status-promising{background:#dcf8eb;color:#087653}.status-weak{background:#ffe5e9;color:#b4233b}.status-pending{background:#eef1f6;color:#667085}
.good{color:#087653;font-weight:750}.bad{color:#c33149;font-weight:750}.muted{color:var(--muted)}.metric{display:inline-block;min-width:62px;padding:4px 7px;border-radius:8px;text-align:center}.metric.good{background:#e5f8ef}.metric.bad{background:#ffeaed}.metric.muted{background:#f0f2f5}
.rate{display:flex;align-items:center;justify-content:flex-end;gap:8px}.rate-track{width:70px;height:7px;overflow:hidden;border-radius:10px;background:#e8edf3}.rate-fill{display:block;height:100%;border-radius:10px;background:linear-gradient(90deg,#17b38a,#48d5aa)}.rate-fill.cyan-bar{background:linear-gradient(90deg,#08a9c9,#63d9ec)}.rate span{min-width:44px;font-weight:700}
.count-target{color:#078258;font-weight:750}.count-stop{color:#c7354d;font-weight:750}.footer{text-align:center;padding:25px 10px 0;color:#98a2b3;font-size:11px}
@media(max-width:1000px){.cards{grid-template-columns:1fr 1fr}.hero-grid{align-items:flex-start;flex-direction:column}.hero-tags{justify-content:flex-start}.sub{text-align:left}.panel-head{align-items:flex-start;flex-direction:column}}
@media(max-width:620px){.wrap{padding:12px}.hero{padding:25px 22px 62px;border-radius:18px}h1{font-size:27px}.cards{grid-template-columns:1fr;margin:-35px 10px 0}.panel{padding:14px}.warning{margin-left:10px;margin-right:10px}.rate-track{display:none}}
"""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>Quant Research Dashboard</title><style>{style}</style></head>"
        "<body><div class=\"wrap\">"
        "<section class=\"hero\"><div class=\"hero-grid\"><div>"
        "<div class=\"eyebrow\">Derivatives intelligence</div>"
        "<h1>Quant Research Dashboard</h1>"
        "<div class=\"hero-copy\">Daily feature isolation and quantitative "
        "combination replay, consolidated into a guarded rolling view.</div>"
        "</div><div class=\"hero-tags\">"
        f"<span class=\"hero-tag\">As of {_escape(dashboard['as_of_date'])}</span>"
        f"<span class=\"hero-tag\">{dashboard['rolling_days']}-day window</span>"
        "<span class=\"hero-tag\">Research only</span>"
        "</div></div></section>"
        "<div class=\"cards\">"
        f"{_card('Input tape files', dashboard['batch_file_count'], 'violet', str(dashboard['batch_tape_count']) + ' unique captures')}"
        f"{_card('Combinations', len(daily_rows), 'cyan', 'Phase-2 scenarios')}"
        f"{_card('Scenario trades', total_daily_trades, 'green', 'Across the current batch')}"
        f"{_card('Research leader', leader, 'amber', 'Sample-gated candidate')}"
        "</div>"
        "<div class=\"warning\"><span class=\"warning-mark\">!</span><span>"
        f"{_escape(dashboard['warning'])}</span></div>"
        "<section class=\"panel\"><div class=\"panel-head\"><div>"
        "<div class=\"section-label\">Two-week scorecard</div>"
        "<h2>Rolling combination comparison</h2></div>"
        f"<div class=\"sub\">{dashboard['history_tape_files']} tape files across "
        f"{dashboard['history_trading_days']} trading days. Eligible after "
        f"{dashboard['minimum_trading_days']} unique trading days and "
        f"{dashboard['minimum_cumulative_trades']} completed trades. "
        f"Eligible combinations: {eligible_count}.</div></div>"
        f"{_rolling_table(rolling_rows)}</section>"
        "<section class=\"panel\"><div class=\"panel-head\"><div>"
        "<div class=\"section-label\">Phase 2 · Current batch</div>"
        "<h2>Consolidated combination results</h2></div>"
        "<div class=\"sub\">Logical quant and derivatives feature groups "
        "aggregated across every selected tape.</div>"
        "</div>"
        f"{_daily_table(daily_rows, 'combination')}</section>"
        "<section class=\"panel\"><div class=\"panel-head\"><div>"
        "<div class=\"section-label\">Phase 1 · Current batch</div>"
        "<h2>Consolidated isolated-feature results</h2></div>"
        "<div class=\"sub\">Directional features run standalone; context, "
        "confirmation and ATR normalization use paired baseline ablations.</div></div>"
        f"{_daily_table(feature_rows, 'feature')}</section>"
        "<div class=\"footer\">Generated locally · no live strategy changes · "
        "5% stop / 10% target · cost-adjusted research model</div>"
        "</div></body></html>"
    )


def _card(
    label: str,
    value: object,
    color: str,
    caption: str,
) -> str:
    return (
        f"<div class=\"card {color}\"><small>"
        + _escape(label)
        + "</small><strong>"
        + _escape(value)
        + "</strong><em>"
        + _escape(caption)
        + "</em></div>"
    )


def _rolling_table(rows: list[dict[str, Any]]) -> str:
    headers = (
        "Rank",
        "Combination",
        "Status",
        "Tape files",
        "Trading days",
        "Trades",
        "DQ signals",
        "Gamma signals",
        "Targets",
        "Stops",
        "Target rate",
        "Decisive win rate",
        "Net avg return",
        "Worst DD",
        "Est. cost",
        "Net P&L",
        "Low coverage",
    )
    body: list[str] = []
    for row in rows:
        average = _decimal(row["net_average_trade_return_percent"])
        status = str(row["research_status"])
        body.append(
            "<tr>"
            f"<td>{_rank_cell(row['rolling_rank'])}</td>"
            f"<td>{_name_cell(row['combination'])}</td>"
            f"<td>{_status_badge(status)}</td>"
            f"<td>{_escape(row['tape_files'])}</td>"
            f"<td>{_escape(row['trading_days'])}</td>"
            f"<td>{_escape(row['completed_trades'])}</td>"
            f"<td>{_escape(row['derivatives_quant_signals'])}</td>"
            f"<td>{_escape(row['gamma_expansion_signals'])}</td>"
            f"<td class=\"count-target\">{_escape(row['successful_target_hits'])}</td>"
            f"<td class=\"count-stop\">{_escape(row['failed_stop_hits'])}</td>"
            f"<td>{_rate_bar(row['target_hit_rate_percent'], 'green')}</td>"
            f"<td>{_rate_bar(row['decisive_win_rate_percent'], 'cyan')}</td>"
            f"<td>{_return_metric(average)}</td>"
            f"<td>{_escape(row['worst_session_drawdown_percent'])}%</td>"
            f"<td>{_escape(row['estimated_transaction_cost'])}</td>"
            f"<td>{_escape(row['net_paper_realized_pnl'])}</td>"
            f"<td>{_escape(', '.join(row['low_coverage_features']) or 'none')}</td>"
            "</tr>"
        )
    return _table(headers, body)


def _daily_table(
    rows: list[dict[str, Any]],
    name_field: str,
) -> str:
    is_feature = name_field == "feature"
    headers = (
        (
            name_field.replace("_", " ").title(),
            "Role / test",
            "Tape files",
            "Trading days",
            "Coverage",
            "Signals",
            "Trades",
            "Targets",
            "Stops",
            "Target rate",
            "Net avg",
            "Delta vs baseline",
            "Net P&L",
        )
        if is_feature
        else (
            "Combination",
            "Tape files",
            "Trading days",
            "DQ signals",
            "Gamma signals",
            "Trades",
            "Targets",
            "Stops",
            "Target rate",
            "Net avg",
            "Est. cost",
            "Net P&L",
            "Low coverage",
        )
    )
    body: list[str] = []
    for row in rows:
        average = _decimal(row.get("net_average_trade_return_percent"))
        if is_feature:
            delta = _decimal(
                row.get("delta_net_average_trade_return_percent")
            )
            role = (
                f"{row.get('feature_role', 'UNCLASSIFIED')} / "
                f"{row.get('experiment_mode', 'UNKNOWN')}"
            )
            body.append(
                "<tr>"
                f"<td>{_name_cell(row.get(name_field, ''))}</td>"
                f"<td>{_escape(role)}</td>"
                f"<td>{_escape(row.get('tape_files', 0))}</td>"
                f"<td>{_escape(row.get('trading_days', 0))}</td>"
                f"<td>{_escape(row.get('feature_coverage_percent', 0))}%</td>"
                f"<td>{_escape(row.get('signals_generated', 0))}</td>"
                f"<td>{_escape(row.get('completed_trades', 0))}</td>"
                f"<td class=\"count-target\">{_escape(row.get('successful_target_hits', 0))}</td>"
                f"<td class=\"count-stop\">{_escape(row.get('failed_stop_hits', 0))}</td>"
                f"<td>{_rate_bar(row.get('target_hit_rate_percent', 0), 'green')}</td>"
                f"<td>{_return_metric(average)}</td>"
                f"<td>{_return_metric(delta)}</td>"
                f"<td>{_escape(row.get('net_paper_realized_pnl', 0))}</td>"
                "</tr>"
            )
        else:
            body.append(
                "<tr>"
                f"<td>{_name_cell(row.get(name_field, ''))}</td>"
                f"<td>{_escape(row.get('tape_files', 0))}</td>"
                f"<td>{_escape(row.get('trading_days', 0))}</td>"
                f"<td>{_escape(row.get('derivatives_quant_signals', 0))}</td>"
                f"<td>{_escape(row.get('gamma_expansion_signals', 0))}</td>"
                f"<td>{_escape(row.get('completed_trades', 0))}</td>"
                f"<td class=\"count-target\">{_escape(row.get('successful_target_hits', 0))}</td>"
                f"<td class=\"count-stop\">{_escape(row.get('failed_stop_hits', 0))}</td>"
                f"<td>{_rate_bar(row.get('target_hit_rate_percent', 0), 'green')}</td>"
                f"<td>{_return_metric(average)}</td>"
                f"<td>{_escape(row.get('estimated_transaction_cost', 0))}</td>"
                f"<td>{_escape(row.get('net_paper_realized_pnl', 0))}</td>"
                f"<td>{_escape(', '.join(row.get('low_coverage_features', ())) or 'none')}</td>"
                "</tr>"
            )
    return _table(headers, body)


def _table(headers: tuple[str, ...], body: list[str]) -> str:
    heading = "".join(f"<th>{_escape(value)}</th>" for value in headers)
    rows = "".join(body)
    if not body:
        rows = (
            f"<tr><td colspan=\"{len(headers)}\" class=\"muted\">"
            "No results available</td></tr>"
        )
    return (
        "<div class=\"table-shell\"><table><thead><tr>"
        f"{heading}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def _name_cell(value: object) -> str:
    key = str(value)
    label = key.replace("_", " ").title()
    return (
        f"<span class=\"name\">{_escape(label)}</span>"
        f"<span class=\"key\">{_escape(key)}</span>"
    )


def _rank_cell(value: object) -> str:
    if value in (None, ""):
        return "<span class=\"muted\">—</span>"
    return f"<span class=\"rank\">{_escape(value)}</span>"


def _status_badge(status: str) -> str:
    style = {
        "PROMISING": "status-promising",
        "NOT_PROFITABLE": "status-weak",
        "INSUFFICIENT_SAMPLE": "status-pending",
    }.get(status, "status-pending")
    label = status.replace("_", " ")
    return (
        f"<span class=\"status {style}\">{_escape(label)}</span>"
    )


def _rate_bar(value: object, color: str) -> str:
    rate = max(Decimal("0"), min(Decimal("100"), _decimal(value)))
    bar_class = "cyan-bar" if color == "cyan" else ""
    width = format(rate.quantize(Decimal("0.01")), "f")
    return (
        "<div class=\"rate\"><div class=\"rate-track\">"
        f"<i class=\"rate-fill {bar_class}\" style=\"width:{width}%\"></i>"
        f"</div><span>{_escape(value)}%</span></div>"
    )


def _return_metric(value: Decimal) -> str:
    tone = "good" if value > 0 else "bad" if value < 0 else "muted"
    prefix = "+" if value > 0 else ""
    return (
        f"<span class=\"metric {tone}\">{prefix}"
        f"{_escape(_quantize(value))}%</span>"
    )


def _escape(value: object) -> str:
    return html.escape(str(value))


def _readme_text(dashboard: dict[str, Any]) -> str:
    leader = (
        dashboard["rolling_research_leader"]
        or "no profitable candidate"
    )
    return (
        "EOD quantitative research dashboard\n"
        f"As-of date: {dashboard['as_of_date']}\n"
        f"Input tape files: {dashboard['batch_file_count']}\n"
        f"Unique broker tapes: {dashboard['batch_tape_count']}\n"
        f"Rolling window: {dashboard['rolling_days']} calendar days\n"
        f"Rolling tape files: {dashboard['history_tape_files']}\n"
        f"Unique trading days: {dashboard['history_trading_days']}\n"
        f"Rolling research leader: {leader}\n\n"
        "Open dashboard.html in a browser. CSV files can be opened in Excel.\n"
        "A leader is a research candidate only. Production selection is disabled.\n"
    )


if __name__ == "__main__":
    main()
