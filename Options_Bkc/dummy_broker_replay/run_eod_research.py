from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.storage.serialization import to_jsonable
from dummy_broker_replay.runtime_selection import (
    add_runtime_selection_arguments,
    runtime_selection_from_args,
)
from dummy_broker_replay.run_strategy_matrix import run_strategy_matrix
from dummy_broker_replay.runner import ReplayMode
from dummy_broker_replay.validate_tape import validate_tape


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate one completed broker tape and, only when replay-ready, "
            "run the EOD strategy-family experiment matrix."
        )
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--mode",
        choices=[item.value for item in ReplayMode],
        default=ReplayMode.EVENT_TIME.value,
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT_DIR / "dummy_broker_replay" / "runs" / "eod",
    )
    parser.add_argument("--eod-id")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--strategy-config", type=Path)
    parser.add_argument(
        "--round-trip-cost-percent",
        default="0.20",
        help=(
            "Estimated all-in round-trip transaction cost percent "
            "(default: 0.20)"
        ),
    )
    parser.add_argument(
        "--profiles",
        help="Comma-separated central profiles; defaults to all configured profiles",
    )
    add_runtime_selection_arguments(parser)
    parser.add_argument(
        "--legacy-family-matrix",
        action="store_true",
    )
    parser.add_argument(
        "--include-priority-permutations",
        action="store_true",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Write the capture audit without running strategy experiments.",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_eod_research(args)))
    except ValueError as exc:
        parser.error(str(exc))


async def run_eod_research(args: argparse.Namespace) -> int:
    runtime_selection = runtime_selection_from_args(args)
    if (
        getattr(args, "legacy_family_matrix", False)
        and runtime_selection.enabled_strategies is not None
    ):
        raise ValueError(
            "--strategies cannot be combined with --legacy-family-matrix; "
            "the legacy matrix generates its own strategy subsets"
        )
    source_path = args.path.resolve()
    eod_id = args.eod_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    eod_directory = (
        args.output_root.resolve() / f"{source_path.stem}_{eod_id}"
    )
    eod_directory.mkdir(parents=True, exist_ok=False)

    summary, issues = validate_tape(source_path)
    schema_versions = summary.get("schema_versions")
    if (
        not isinstance(schema_versions, dict)
        or int(schema_versions.get("4", 0)) <= 0
    ):
        issues.append(
            "EOD strategy research requires a schema-v4 capture; "
            "legacy tapes remain diagnostic-only"
        )
    audit = {
        "schema_version": 1,
        "record_type": "eod_capture_audit",
        "created_at": datetime.now(UTC),
        "source_path": source_path,
        "validation_passed": not issues,
        "issues": issues,
        "summary": summary,
        "strategy_matrix_started": False,
        "runtime_selection": runtime_selection.manifest(),
        "automatic_production_selection": False,
    }
    audit_path = eod_directory / "capture_audit.json"
    _write_json(audit_path, audit)
    if issues:
        (eod_directory / "EOD_FAILED.txt").write_text(
            "Capture validation failed. Strategy experiments were not run.\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n",
            encoding="utf-8",
        )
        print(f"Capture validation failed: {audit_path}")
        return 1

    if args.validate_only:
        print(f"Capture validation passed: {audit_path}")
        return 0

    audit["strategy_matrix_started"] = True
    _write_json(audit_path, audit)
    matrix_arguments = argparse.Namespace(
        path=source_path,
        mode=args.mode,
        output_root=eod_directory / "strategy_matrix",
        matrix_id="matrix",
        max_frames=args.max_frames,
        include_priority_permutations=(
            args.include_priority_permutations
        ),
        strategy_config=args.strategy_config,
        round_trip_cost_percent=getattr(
            args,
            "round_trip_cost_percent",
            "0.20",
        ),
        profiles=getattr(args, "profiles", None),
        legacy_family_matrix=getattr(
            args,
            "legacy_family_matrix",
            False,
        ),
        strategies=getattr(args, "strategies", None),
        features=getattr(args, "features", None),
        minimum_book_imbalance=getattr(
            args,
            "minimum_book_imbalance",
            None,
        ),
    )
    await run_strategy_matrix(matrix_arguments)
    (eod_directory / "EOD_COMPLETE.txt").write_text(
        "Capture validation and strategy experiment matrix completed.\n"
        "No production configuration was changed.\n",
        encoding="utf-8",
    )
    print(f"EOD research complete: {eod_directory}")
    return 0


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(to_jsonable(value), indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
