"""Offline broker/session replay for recorded option-market data.

This package is intentionally isolated from the live worker.  It imports the
production analytics components, but it never logs in to a broker or changes the
production runtime.
"""

from .runner import ReplayMode, ReplayResult, run_replay

__all__ = ["ReplayMode", "ReplayResult", "run_replay"]
