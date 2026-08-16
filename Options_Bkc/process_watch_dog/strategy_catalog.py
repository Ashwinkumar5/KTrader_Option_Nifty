from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StrategyCatalogError(ValueError):
    """Raised when the strategy configuration cannot be resolved safely."""


@dataclass(frozen=True)
class StrategyState:
    name: str
    enabled: bool
    priority: int
    publish_to_simulator: bool = False


class StrategyCatalog:
    """Read-only view of profiles in the bot's strategy_config.json."""

    def __init__(self, source: Path, document: dict[str, Any]) -> None:
        self.source = source
        profiles = document.get("profiles")
        if not isinstance(profiles, dict) or not profiles:
            raise StrategyCatalogError("strategy configuration has no profiles")
        self._profiles = profiles
        self.active_profile = str(document.get("active_profile") or "").strip()

    @classmethod
    def load(cls, path: str | Path) -> "StrategyCatalog":
        source = Path(path).expanduser().resolve()
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StrategyCatalogError(
                f"strategy configuration not found: {source}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise StrategyCatalogError(
                f"invalid strategy configuration JSON at {source}: {exc}"
            ) from exc
        if not isinstance(document, dict):
            raise StrategyCatalogError("strategy configuration root must be an object")
        return cls(source, document)

    def profile_names(self) -> tuple[str, ...]:
        return tuple(str(name) for name in self._profiles)

    def watchdog_enabled(self, profile: str) -> bool:
        raw = self._profiles.get(profile)
        if not isinstance(raw, dict):
            raise StrategyCatalogError(f"unknown strategy profile: {profile}")
        value = str(raw.get("watchdog_enable") or "").strip().upper()
        if value not in {"Y", "N"}:
            raise StrategyCatalogError(
                f"profile {profile!r} must define watchdog_enable as 'Y' or 'N'"
            )
        return value == "Y"

    def watchdog_enabled_profiles(self) -> tuple[str, ...]:
        return tuple(name for name in self.profile_names() if self.watchdog_enabled(name))

    def strategies(self, profile: str) -> tuple[StrategyState, ...]:
        resolved = self._resolve_profile(profile, ())
        raw_strategies = resolved.get("strategies")
        if not isinstance(raw_strategies, dict) or not raw_strategies:
            raise StrategyCatalogError(f"profile {profile} has no strategies")
        parsed: list[StrategyState] = []
        for raw_name, raw_state in raw_strategies.items():
            if not isinstance(raw_state, dict):
                raise StrategyCatalogError(
                    f"strategy {raw_name} in profile {profile} must be an object"
                )
            try:
                priority = int(raw_state.get("priority", 100))
            except (TypeError, ValueError) as exc:
                raise StrategyCatalogError(
                    f"strategy {raw_name} in profile {profile} has invalid priority"
                ) from exc
            publish_to_simulator = raw_state.get(
                "publish_to_simulator",
                False,
            )
            if not isinstance(publish_to_simulator, bool):
                raise StrategyCatalogError(
                    f"strategy {raw_name} in profile {profile} "
                    "publish_to_simulator must be boolean"
                )
            parsed.append(
                StrategyState(
                    name=str(raw_name).strip().upper(),
                    enabled=bool(raw_state.get("enabled", False)),
                    priority=priority,
                    publish_to_simulator=publish_to_simulator,
                )
            )
        parsed.sort(key=lambda item: (item.priority, item.name))
        return tuple(parsed)

    def enabled_strategies(self, profile: str) -> tuple[str, ...]:
        return tuple(item.name for item in self.strategies(profile) if item.enabled)

    def validate_enabled_strategy(self, profile: str, strategy: str) -> str:
        normalized = strategy.strip().upper()
        states = {item.name: item for item in self.strategies(profile)}
        state = states.get(normalized)
        if state is None:
            raise StrategyCatalogError(
                f"unknown strategy {normalized!r} for profile {profile!r}"
            )
        if not state.enabled:
            raise StrategyCatalogError(
                f"strategy {normalized!r} is disabled in profile {profile!r}"
            )
        return normalized

    def _resolve_profile(
        self,
        name: str,
        stack: tuple[str, ...],
    ) -> dict[str, Any]:
        if name in stack:
            chain = " -> ".join(stack + (name,))
            raise StrategyCatalogError(f"cyclic strategy profile inheritance: {chain}")
        raw = self._profiles.get(name)
        if not isinstance(raw, dict):
            raise StrategyCatalogError(f"unknown strategy profile: {name}")
        parent_name = raw.get("extends")
        if not parent_name:
            return dict(raw)
        parent = self._resolve_profile(str(parent_name), stack + (name,))
        child = {key: value for key, value in raw.items() if key != "extends"}
        return _deep_merge(parent, child)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
