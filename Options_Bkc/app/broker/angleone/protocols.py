from __future__ import annotations

from typing import Protocol


class AngleOne_ProtoType(Protocol):
    """Structural protocol for SmartAPI-style market quote responses."""

    status: bool
    message: str
    errorcode: str
    data: object
