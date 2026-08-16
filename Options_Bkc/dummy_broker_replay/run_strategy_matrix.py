from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import load_settings
from app.core.strategy_config import available_strategy_profiles
from app.storage.serialization import to_jsonable
from dummy_broker_replay.runtime_selection import (
    add_runtime_selection_arguments,
    runtime_selection_from_args,
)
from dummy_broker_replay.runner import ReplayMode, run_replay
from dummy_broker_replay.strategy_experiments import (
    enabled_strategy_names,
    generate_strategy_matrix,
    strategy_priority_names,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic EOD strategy-family ablations and optional "
            "priority permutations. Results never update production settings."
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
        default=(
            ROOT_DIR
            / "dummy_broker_replay"
            / "runs"
            / "strategy_matrices"
        ),
    )
    parser.add_argument("--matrix-id")
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--strategy-config", type=Path)
    parser.add_argument(
        "--round-trip-cost-percent",
        default="0.20",
        help="Estimated all-in round-trip transaction cost percent (default: 0.20)",
    )
    parser.add_argument(
        "--profiles",
        help="Comma-separated central strategy profile names; defaults to all profiles",
    )
    add_runtime_selection_arguments(parser)
    parser.add_argument(
        "--legacy-family-matrix",
        action="store_true",
        help="Run the retained three-family legacy ablation matrix",
    )
    parser.add_argument(
        "--include-priority-permutations",
        action="store_true",
        help=(
            "Also test FIXED_PRIORITY orders for enabled sets containing at "
            "least two strategy families."
        ),
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_strategy_matrix(args))
    except ValueError as exc:
        parser.error(str(exc))


async def run_strategy_matrix(args: argparse.Namespace) -> None:
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
    matrix_id = (
        args.matrix_id
        or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    matrix_directory = (
        args.output_root.resolve()
        / f"{source_path.stem}_{args.mode}_{matrix_id}"
    )
    matrix_directory.mkdir(parents=True, exist_ok=False)

    source_digest = _sha256_file(source_path)
    base_settings = load_settings()
    strategy_config = getattr(args, "strategy_config", None)
    if strategy_config is not None:
        base_settings = replace(
            base_settings,
            strategy_config_path=str(strategy_config.resolve()),
        )
    if getattr(args, "legacy_family_matrix", False):
        base_settings = replace(
            base_settings,
            strategy_profile="legacy_comparison",
        )
        experiments = generate_strategy_matrix(
            base_settings,
            include_priority_permutations=(
                args.include_priority_permutations
            ),
        )
    else:
        requested_profiles = tuple(
            item.strip()
            for item in str(getattr(args, "profiles", "") or "").split(",")
            if item.strip()
        )
        profile_names = (
            requested_profiles
            or available_strategy_profiles(
                base_settings.strategy_config_path or None
            )
        )
        experiments = tuple(
            (
                f"profile__{profile_name}",
                replace(
                    base_settings,
                    strategy_profile=profile_name,
                ),
            )
            for profile_name in profile_names
        )
    summaries: list[dict[str, object]] = []
    shared_event_index = matrix_directory / "event_time_index.sqlite"
    round_trip_cost_percent = getattr(
        args,
        "round_trip_cost_percent",
        "0.20",
    )
    for index, (label, settings) in enumerate(experiments, start=1):
        # Keep physical paths short on Windows. The descriptive label is saved
        # in the aggregate summary and the complete strategy configuration is
        # saved in each run manifest.
        run_id = f"c{index:03d}"
        print(f"[{index}/{len(experiments)}] {label}")
        runtime_strategies = runtime_selection.enabled_strategies
        runtime_priority = runtime_selection.strategy_priority
        if getattr(args, "legacy_family_matrix", False):
            runtime_strategies = enabled_strategy_names(settings)
            runtime_priority = strategy_priority_names(settings)
        result = await run_replay(
            source_path,
            mode=args.mode,
            output_root=matrix_directory,
            run_id=run_id,
            max_frames=args.max_frames,
            settings=settings,
            source_sha256=source_digest,
            event_index_path=shared_event_index,
            round_trip_cost_percent=round_trip_cost_percent,
            enabled_strategies=runtime_strategies,
            enabled_features=runtime_selection.enabled_features,
            minimum_book_imbalance=(
                runtime_selection.minimum_book_imbalance
            ),
            strategy_priority=runtime_priority,
        )
        row = asdict(result)
        row["experiment_label"] = label
        summaries.append(row)

    payload = {
        "schema_version": 1,
        "record_type": "strategy_experiment_matrix",
        "created_at": datetime.now(UTC),
        "source_path": source_path,
        "source_sha256": source_digest,
        "mode": args.mode,
        "max_frames": args.max_frames,
        "round_trip_cost_percent": round_trip_cost_percent,
        "runtime_selection": runtime_selection.manifest(),
        "experiment_count": len(summaries),
        "automatic_production_selection": False,
        "selection_warning": (
            "Results are research diagnostics. Do not select or deploy a "
            "winner from the same sessions used to search configurations."
        ),
        "experiments": summaries,
    }
    (matrix_directory / "matrix_summary.json").write_text(
        json.dumps(to_jsonable(payload), indent=2),
        encoding="utf-8",
    )
    print("")
    print(f"Matrix complete: {matrix_directory}")
    print(f"Experiments: {len(summaries)}")
    print("Production settings were not changed.")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
