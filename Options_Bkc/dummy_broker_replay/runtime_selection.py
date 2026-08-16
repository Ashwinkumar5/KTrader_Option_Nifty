from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RuntimeReplaySelection:
    enabled_strategies: tuple[str, ...] | None = None
    enabled_features: tuple[str, ...] | None = None
    minimum_book_imbalance: Decimal | None = None
    strategy_priority: tuple[str, ...] | None = None

    def replay_kwargs(self) -> dict[str, object]:
        return {
            "enabled_strategies": self.enabled_strategies,
            "enabled_features": self.enabled_features,
            "minimum_book_imbalance": self.minimum_book_imbalance,
            "strategy_priority": self.strategy_priority,
        }

    def manifest(self) -> dict[str, object]:
        return {
            "enabled_strategies": self.enabled_strategies,
            "enabled_features": self.enabled_features,
            "minimum_book_imbalance": (
                str(self.minimum_book_imbalance)
                if self.minimum_book_imbalance is not None
                else None
            ),
            "strategy_priority": self.strategy_priority,
        }


def add_runtime_selection_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_strategy_priority: bool = False,
) -> None:
    parser.add_argument(
        "--strategies",
        help=(
            "Comma-separated strategies to enable exclusively: "
            "DERIVATIVES_QUANT,GAMMA_EXPANSION,LEVEL_REVERSAL,"
            "BREAKOUT_MOMENTUM,OPTION_CHAIN_IMPULSE or SMC. "
            "GAMMA_BLAST aliases GAMMA_EXPANSION."
        ),
    )
    parser.add_argument(
        "--features",
        help=(
            "Comma-separated features to enable exclusively. GAMMA_BLAST "
            "aliases gamma_concentration."
        ),
    )
    parser.add_argument(
        "--minimum-book-imbalance",
        type=Decimal,
        help=(
            "Runtime book-imbalance threshold from 0 through 1; defaults "
            "to the selected profile."
        ),
    )
    if include_strategy_priority:
        parser.add_argument(
            "--strategy-priority",
            help=(
                "Comma-separated enabled strategies from highest to lowest "
                "priority; generated priorities are 10,20,30..."
            ),
        )


def runtime_selection_from_args(
    args: argparse.Namespace,
) -> RuntimeReplaySelection:
    minimum_book_imbalance = getattr(
        args,
        "minimum_book_imbalance",
        None,
    )
    if (
        minimum_book_imbalance is not None
        and not Decimal("0")
        <= minimum_book_imbalance
        <= Decimal("1")
    ):
        raise ValueError(
            "minimum book imbalance must be between zero and one"
        )
    return RuntimeReplaySelection(
        enabled_strategies=parse_csv_argument(
            getattr(args, "strategies", None),
            label="strategy",
        ),
        enabled_features=parse_csv_argument(
            getattr(args, "features", None),
            label="feature",
        ),
        minimum_book_imbalance=minimum_book_imbalance,
        strategy_priority=parse_csv_argument(
            getattr(args, "strategy_priority", None),
            label="strategy priority",
        ),
    )


def parse_csv_argument(
    value: str | None,
    *,
    label: str,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    if not items:
        raise ValueError(f"a supplied {label} list cannot be empty")
    return items
