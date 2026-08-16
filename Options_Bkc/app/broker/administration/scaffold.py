from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


REFERENCE_BROKER = "angleone"


@dataclass(frozen=True)
class BrokerScaffoldResult:
    broker_name: str
    target_dir: Path
    copied_files: tuple[Path, ...]


def create_broker_folder(
    broker_name: str,
    *,
    broker_root: Path | None = None,
    reference_broker: str = REFERENCE_BROKER,
) -> BrokerScaffoldResult:
    normalized_name = _normalize_broker_name(broker_name)
    root = broker_root or Path(__file__).resolve().parents[1]
    source_dir = root / reference_broker
    target_dir = root / normalized_name

    if not source_dir.exists():
        raise FileNotFoundError(f"Reference broker folder not found: {source_dir}")
    if target_dir.exists():
        raise FileExistsError(f"Broker folder already exists: {target_dir}")

    copied_files: list[Path] = []
    target_dir.mkdir(parents=True)
    for source_file in source_dir.glob("*.py"):
        target_file = target_dir / source_file.name
        shutil.copy2(source_file, target_file)
        copied_files.append(target_file)

    readme = target_dir / "ONBOARDING_NOTES.md"
    readme.write_text(_notes(normalized_name, reference_broker), encoding="utf-8")
    copied_files.append(readme)

    return BrokerScaffoldResult(
        broker_name=normalized_name,
        target_dir=target_dir,
        copied_files=tuple(copied_files),
    )


def _normalize_broker_name(name: str) -> str:
    normalized = name.strip().lower().replace(" ", "_").replace("-", "_")
    if not normalized or not normalized.replace("_", "").isalnum():
        raise ValueError("broker_name must contain only letters, numbers, spaces, hyphens, or underscores")
    return normalized


def _notes(broker_name: str, reference_broker: str) -> str:
    return f"""# {broker_name} Broker Onboarding Notes

This folder was scaffolded from `{reference_broker}`.

Update these files first:

- `client.py`: login, token refresh, REST methods, SDK/API calls
- `feed.py`: websocket connection, subscribe/unsubscribe payloads, tick callback
- `instruments.py`: instrument master parsing and token resolution
- `data_map.py`: broker data-source names, websocket modes, required fields
- `protocols.py`: broker-specific protocol helpers, if needed

Keep broker-specific code inside this folder. Code outside `app/broker` should continue to consume the shared domain models from `app/domain/models.py`.
"""
