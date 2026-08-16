"""Standalone supervisor for strategy/profile bot processes."""

from .config import ConfigurationError, ProcessSpec, WatchdogSettings, load_watchdog_settings
from .supervisor import ProcessSupervisor

__all__ = [
    "ConfigurationError",
    "ProcessSpec",
    "ProcessSupervisor",
    "WatchdogSettings",
    "load_watchdog_settings",
]

