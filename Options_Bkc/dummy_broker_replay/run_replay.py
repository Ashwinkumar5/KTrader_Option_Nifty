from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import load_settings
from app.domain.models import StrategyResolverPolicy
from dummy_broker_replay.runtime_selection import (
    add_runtime_selection_arguments,
    runtime_selection_from_args,
)
from dummy_broker_replay.runner import ReplayMode, run_replay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replay recorded option events through the current analytics and "
            "strong-signal gate without contacting a broker."
        )
    )
    parser.add_argument("path", type=Path, help="Recorded microstructure JSONL")
    parser.add_argument(
        "--mode",
        choices=[item.value for item in ReplayMode],
        default=ReplayMode.EVENT_TIME.value,
        help="event-time evaluates current logic; faithful preserves file order",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT_DIR / "dummy_broker_replay" / "runs",
        help="Parent directory for an isolated run directory",
    )
    parser.add_argument(
        "--run-id",
        help="Optional unique run identifier (defaults to current UTC time)",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        help="Stop after this many gate frames (useful for a smoke test)",
    )
    parser.add_argument(
        "--strategy-config",
        type=Path,
        help="Central strategy_config JSON file (defaults to config/strategy_config.json)",
    )
    parser.add_argument(
        "--strategy-profile",
        help="Named central strategy profile, for example derivatives_only",
    )
    add_runtime_selection_arguments(
        parser,
        include_strategy_priority=True,
    )
    parser.add_argument(
        "--resolver-policy",
        choices=[item.value for item in StrategyResolverPolicy],
        help=(
            "Candidate resolver. REGIME_EXCLUSIVE is the production-safe "
            "default; other policies are intended for research comparison."
        ),
    )
    parser.add_argument(
        "--round-trip-cost-percent",
        default="0.20",
        help="Estimated all-in round-trip transaction cost percent (default: 0.20)",
    )
    parser.add_argument(
        "--compact-output",
        action="store_true",
        help=(
            "Evaluate every frame but write detailed decision JSON only for "
            "qualified signals and paper fills/exits. Summary counters remain "
            "complete."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        settings = replace(
            settings,
            strategy_config_path=(
                str(args.strategy_config.resolve())
                if args.strategy_config is not None
                else settings.strategy_config_path
            ),
            strategy_profile=(
                args.strategy_profile or settings.strategy_profile
            ),
        )
        runtime_selection = runtime_selection_from_args(args)
        if args.resolver_policy is not None:
            settings = replace(
                settings,
                strategy_resolver_policy=args.resolver_policy,
            )
    except ValueError as exc:
        parser.error(str(exc))

    try:
        result = asyncio.run(
            run_replay(
                args.path,
                mode=args.mode,
                output_root=args.output_root,
                run_id=args.run_id,
                max_frames=args.max_frames,
                settings=settings,
                round_trip_cost_percent=args.round_trip_cost_percent,
                write_all_decisions=not args.compact_output,
                **runtime_selection.replay_kwargs(),
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    print("")
    print("Replay complete")
    print(f"Output: {result.run_directory}")
    print(f"Frames: {result.frames_processed}")
    print(f"Microstructure candidates: {result.microstructure_candidates}")
    print(f"Strong signals: {result.replay_qualified}")
    print(f"Gamma strong signals: {result.gamma_qualified}")
    print(f"Enabled strategies: {', '.join(result.enabled_strategies)}")
    print(f"Resolver: {result.resolver_policy}")


if __name__ == "__main__":
    main()
