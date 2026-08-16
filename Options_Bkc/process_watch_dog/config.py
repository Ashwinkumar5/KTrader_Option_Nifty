from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .strategy_catalog import StrategyCatalog, StrategyCatalogError


DEFAULT_CONFIG_PATH = Path(__file__).with_name("watchdog_config.json")
_PROCESS_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigurationError(ValueError):
    """Raised when watchdog configuration is unsafe or inconsistent."""


@dataclass(frozen=True)
class RestartPolicy:
    restart_on_failure: bool = True
    restart_on_clean_exit: bool = False
    delay_seconds: float = 2.0
    maximum_delay_seconds: float = 60.0
    maximum_restarts: int = 5
    restart_window_seconds: float = 120.0
    stable_run_seconds: float = 300.0
    graceful_shutdown_seconds: float = 10.0


@dataclass(frozen=True)
class ProcessSpec:
    process_id: str
    enabled: bool
    profile: str
    strategy: str
    command: tuple[str, ...]
    working_directory: Path
    environment: dict[str, str] = field(default_factory=dict)
    restart: RestartPolicy = field(default_factory=RestartPolicy)
    fatal_output_patterns: tuple[str, ...] = ()
    heartbeat_file: Path | None = None
    heartbeat_timeout_seconds: float | None = None
    output_idle_timeout_seconds: float | None = None
    startup_grace_seconds: float = 30.0
    log_file: Path | None = None


@dataclass(frozen=True)
class WatchdogSettings:
    config_path: Path
    project_root: Path
    strategy_config_path: Path
    runtime_directory: Path
    log_directory: Path
    poll_interval_seconds: float
    control_host: str
    control_port: int
    log_max_bytes: int
    log_backup_count: int
    processes: tuple[ProcessSpec, ...]
    console_status_interval_seconds: float = 10.0
    console_show_child_output: bool = True

    @property
    def state_file(self) -> Path:
        return self.runtime_directory / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.runtime_directory / "watchdog.lock"


def load_watchdog_settings(
    path: str | Path = DEFAULT_CONFIG_PATH,
    *,
    validate_commands: bool = True,
) -> WatchdogSettings:
    source = Path(path).expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"watchdog configuration not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid watchdog JSON at {source}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigurationError("watchdog configuration root must be an object")

    base = source.parent
    project_root = _resolve_path(base, document.get("project_root", ".."))
    strategy_config = _resolve_path(
        base,
        document.get("strategy_config", "../config/strategy_config.json"),
    )
    runtime_directory = _resolve_path(
        base, document.get("runtime_directory", "runtime")
    )
    log_directory = _resolve_path(base, document.get("log_directory", "logs"))

    try:
        catalog = StrategyCatalog.load(strategy_config)
    except StrategyCatalogError as exc:
        raise ConfigurationError(str(exc)) from exc

    defaults = document.get("restart_defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigurationError("restart_defaults must be an object")
    control = document.get("control", {})
    if not isinstance(control, dict):
        raise ConfigurationError("control must be an object")

    raw_processes = document.get("run_process")
    if not isinstance(raw_processes, list):
        raise ConfigurationError("run_process must be an array")

    processes: list[ProcessSpec] = []
    for index, raw in enumerate(raw_processes):
        if not isinstance(raw, dict):
            raise ConfigurationError(f"run_process[{index}] must be an object")
        processes.extend(
            _expand_process_template(
                raw,
                index=index,
                base=base,
                project_root=project_root,
                strategy_config=strategy_config,
                log_directory=log_directory,
                restart_defaults=defaults,
                catalog=catalog,
                validate_commands=validate_commands,
            )
        )

    identifiers = [item.process_id for item in processes]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise ConfigurationError(
            "duplicate expanded process IDs: " + ", ".join(duplicates)
        )

    poll_interval = _positive_float(
        document.get("poll_interval_seconds", 0.5), "poll_interval_seconds"
    )
    control_port = _integer(control.get("port", 47651), "control.port", minimum=0)
    if control_port > 65535:
        raise ConfigurationError("control.port must be at most 65535")

    return WatchdogSettings(
        config_path=source,
        project_root=project_root,
        strategy_config_path=strategy_config,
        runtime_directory=runtime_directory,
        log_directory=log_directory,
        poll_interval_seconds=poll_interval,
        control_host=str(control.get("host", "127.0.0.1")),
        control_port=control_port,
        log_max_bytes=_integer(
            document.get("log_max_bytes", 5_000_000),
            "log_max_bytes",
            minimum=1,
        ),
        log_backup_count=_integer(
            document.get("log_backup_count", 5),
            "log_backup_count",
            minimum=1,
        ),
        processes=tuple(processes),
        console_status_interval_seconds=_non_negative_float(
            document.get("console_status_interval_seconds", 10),
            "console_status_interval_seconds",
        ),
        console_show_child_output=_boolean(
            document.get("console_show_child_output", True),
            "console_show_child_output",
        ),
    )


def _expand_process_template(
    raw: dict[str, Any],
    *,
    index: int,
    base: Path,
    project_root: Path,
    strategy_config: Path,
    log_directory: Path,
    restart_defaults: dict[str, Any],
    catalog: StrategyCatalog,
    validate_commands: bool,
) -> list[ProcessSpec]:
    label = f"run_process[{index}]"
    enabled = _boolean(raw.get("enabled", True), f"{label}.enabled")
    singleton = _boolean(raw.get("singleton", False), f"{label}.singleton")
    if singleton:
        profiles = ("SYSTEM",)
        strategy_selector: Any = _required_string(
            raw.get("role", "CENTRAL_SIGNAL_ROUTER"),
            f"{label}.role",
        ).upper()
    else:
        profiles = _selected_profiles(raw, label=label, catalog=catalog)
        strategy_selector = raw.get("strategies", "enabled")
    id_template = _required_string(raw.get("id"), f"{label}.id")
    command_template = raw.get("command")
    if (
        not isinstance(command_template, list)
        or not command_template
        or not all(isinstance(item, str) and item for item in command_template)
    ):
        raise ConfigurationError(f"{label}.command must be a non-empty string array")
    working_template = _required_string(
        raw.get("working_directory", "{project_root}"),
        f"{label}.working_directory",
    )
    raw_environment = raw.get("environment", {})
    if not isinstance(raw_environment, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int, float, bool))
        for key, value in raw_environment.items()
    ):
        raise ConfigurationError(f"{label}.environment must be a scalar string map")

    policy_raw = dict(restart_defaults)
    override_policy = raw.get("restart", {})
    if not isinstance(override_policy, dict):
        raise ConfigurationError(f"{label}.restart must be an object")
    policy_raw.update(override_policy)
    policy = _parse_restart_policy(policy_raw, label=f"{label}.restart")

    raw_patterns = raw.get("fatal_output_patterns", [])
    if not isinstance(raw_patterns, list) or not all(
        isinstance(item, str) and item for item in raw_patterns
    ):
        raise ConfigurationError(
            f"{label}.fatal_output_patterns must be a string array"
        )
    for pattern in raw_patterns:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ConfigurationError(
                f"invalid regex in {label}.fatal_output_patterns: {pattern!r}: {exc}"
            ) from exc

    results: list[ProcessSpec] = []
    for profile in profiles:
        strategies = (
            (strategy_selector,)
            if singleton
            else _selected_strategies(
                strategy_selector,
                profile=profile,
                label=label,
                catalog=catalog,
            )
        )
        for strategy in strategies:
            values = {
                "project_root": str(project_root),
                "strategy_config": str(strategy_config),
                "profile": profile,
                "strategy": strategy,
                "strategy_slug": strategy.lower(),
            }
            process_id = _render(id_template, values, f"{label}.id")
            if not _PROCESS_ID.fullmatch(process_id):
                raise ConfigurationError(
                    f"expanded process ID {process_id!r} may contain only letters, "
                    "numbers, dot, underscore, and hyphen"
                )
            command = tuple(
                _render(item, {**values, "process_id": process_id}, f"{label}.command")
                for item in command_template
            )
            working_directory = _rendered_path(
                base,
                _render(
                    working_template,
                    {**values, "process_id": process_id},
                    f"{label}.working_directory",
                ),
            )
            if not working_directory.is_dir():
                raise ConfigurationError(
                    f"working directory does not exist for {process_id}: "
                    f"{working_directory}"
                )
            if validate_commands:
                _validate_executable(command[0], working_directory, process_id)
            environment = {
                key: _render(
                    str(value),
                    {**values, "process_id": process_id},
                    f"{label}.environment.{key}",
                )
                for key, value in raw_environment.items()
            }
            heartbeat_file = _optional_rendered_path(
                raw.get("heartbeat_file"),
                base=working_directory,
                values={**values, "process_id": process_id},
                label=f"{label}.heartbeat_file",
            )
            heartbeat_timeout = _optional_positive_float(
                raw.get("heartbeat_timeout_seconds"),
                f"{label}.heartbeat_timeout_seconds",
            )
            if (heartbeat_file is None) != (heartbeat_timeout is None):
                raise ConfigurationError(
                    f"{label} must configure heartbeat_file and "
                    "heartbeat_timeout_seconds together"
                )
            log_value = raw.get("log_file", f"{process_id}.log")
            log_rendered = _render(
                _required_string(log_value, f"{label}.log_file"),
                {**values, "process_id": process_id},
                f"{label}.log_file",
            )
            log_file = Path(log_rendered)
            if not log_file.is_absolute():
                log_file = log_directory / log_file
            results.append(
                ProcessSpec(
                    process_id=process_id,
                    enabled=enabled,
                    profile=profile,
                    strategy=strategy,
                    command=command,
                    working_directory=working_directory,
                    environment=environment,
                    restart=policy,
                    fatal_output_patterns=tuple(raw_patterns),
                    heartbeat_file=heartbeat_file,
                    heartbeat_timeout_seconds=heartbeat_timeout,
                    output_idle_timeout_seconds=_optional_positive_float(
                        raw.get("output_idle_timeout_seconds"),
                        f"{label}.output_idle_timeout_seconds",
                    ),
                    startup_grace_seconds=_non_negative_float(
                        raw.get("startup_grace_seconds", 30),
                        f"{label}.startup_grace_seconds",
                    ),
                    log_file=log_file.resolve(),
                )
            )
    return results


def _selected_profiles(
    raw: dict[str, Any],
    *,
    label: str,
    catalog: StrategyCatalog,
) -> tuple[str, ...]:
    selector = raw.get("profiles", raw.get("profile"))
    if selector == "all":
        try:
            return catalog.watchdog_enabled_profiles()
        except StrategyCatalogError as exc:
            raise ConfigurationError(str(exc)) from exc
    if isinstance(selector, str) and selector.strip():
        profiles = (selector.strip(),)
    elif isinstance(selector, list) and selector and all(
        isinstance(item, str) and item.strip() for item in selector
    ):
        profiles = tuple(item.strip() for item in selector)
    else:
        raise ConfigurationError(
            f"{label}.profiles must be 'all', a profile name, or a string array"
        )
    known = set(catalog.profile_names())
    unknown = [item for item in profiles if item not in known]
    if unknown:
        raise ConfigurationError("unknown strategy profiles: " + ", ".join(unknown))
    try:
        return tuple(item for item in profiles if catalog.watchdog_enabled(item))
    except StrategyCatalogError as exc:
        raise ConfigurationError(str(exc)) from exc


def _selected_strategies(
    selector: Any,
    *,
    profile: str,
    label: str,
    catalog: StrategyCatalog,
) -> tuple[str, ...]:
    if selector == "enabled":
        strategies = catalog.enabled_strategies(profile)
        if not strategies:
            raise ConfigurationError(f"profile {profile!r} has no enabled strategies")
        return strategies
    if isinstance(selector, str) and selector.strip():
        raw_items = (selector.strip(),)
    elif isinstance(selector, list) and selector and all(
        isinstance(item, str) and item.strip() for item in selector
    ):
        raw_items = tuple(item.strip() for item in selector)
    else:
        raise ConfigurationError(
            f"{label}.strategies must be 'enabled', a strategy name, or a string array"
        )
    try:
        return tuple(
            catalog.validate_enabled_strategy(profile, item) for item in raw_items
        )
    except StrategyCatalogError as exc:
        raise ConfigurationError(str(exc)) from exc


def _parse_restart_policy(raw: dict[str, Any], *, label: str) -> RestartPolicy:
    delay = _non_negative_float(raw.get("delay_seconds", 2), f"{label}.delay_seconds")
    maximum_delay = _positive_float(
        raw.get("maximum_delay_seconds", 60),
        f"{label}.maximum_delay_seconds",
    )
    if maximum_delay < delay:
        raise ConfigurationError(
            f"{label}.maximum_delay_seconds cannot be less than delay_seconds"
        )
    return RestartPolicy(
        restart_on_failure=_boolean(
            raw.get("restart_on_failure", True),
            f"{label}.restart_on_failure",
        ),
        restart_on_clean_exit=_boolean(
            raw.get("restart_on_clean_exit", False),
            f"{label}.restart_on_clean_exit",
        ),
        delay_seconds=delay,
        maximum_delay_seconds=maximum_delay,
        maximum_restarts=_integer(
            raw.get("maximum_restarts", 5),
            f"{label}.maximum_restarts",
            minimum=0,
        ),
        restart_window_seconds=_positive_float(
            raw.get("restart_window_seconds", 120),
            f"{label}.restart_window_seconds",
        ),
        stable_run_seconds=_positive_float(
            raw.get("stable_run_seconds", 300),
            f"{label}.stable_run_seconds",
        ),
        graceful_shutdown_seconds=_non_negative_float(
            raw.get("graceful_shutdown_seconds", 10),
            f"{label}.graceful_shutdown_seconds",
        ),
    )


def _render(template: str, values: dict[str, str], label: str) -> str:
    try:
        rendered = template.format_map(values)
    except KeyError as exc:
        raise ConfigurationError(
            f"unknown placeholder {exc.args[0]!r} in {label}"
        ) from exc
    if not rendered:
        raise ConfigurationError(f"{label} expands to an empty value")
    return rendered


def _validate_executable(executable: str, cwd: Path, process_id: str) -> None:
    candidate = Path(executable)
    if candidate.is_absolute():
        found = candidate.is_file()
    elif candidate.parent != Path("."):
        found = (cwd / candidate).is_file()
    else:
        found = shutil.which(executable) is not None
    if not found:
        raise ConfigurationError(
            f"executable for {process_id} was not found: {executable}"
        )


def _resolve_path(base: Path, value: Any) -> Path:
    text = _required_string(value, "path")
    candidate = Path(os.path.expandvars(text)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _rendered_path(base: Path, value: str) -> Path:
    candidate = Path(os.path.expandvars(value)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _optional_rendered_path(
    value: Any,
    *,
    base: Path,
    values: dict[str, str],
    label: str,
) -> Path | None:
    if value in (None, ""):
        return None
    return _rendered_path(
        base,
        _render(_required_string(value, label), values, label),
    )


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty string")
    return value.strip()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{label} must be true or false")
    return value


def _integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise ConfigurationError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be an integer") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{label} must be at least {minimum}")
    return parsed


def _positive_float(value: Any, label: str) -> float:
    parsed = _non_negative_float(value, label)
    if parsed <= 0:
        raise ConfigurationError(f"{label} must be greater than zero")
    return parsed


def _optional_positive_float(value: Any, label: str) -> float | None:
    if value in (None, "", 0, 0.0):
        return None
    return _positive_float(value, label)


def _non_negative_float(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigurationError(f"{label} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{label} must be a number") from exc
    if parsed < 0:
        raise ConfigurationError(f"{label} cannot be negative")
    return parsed
