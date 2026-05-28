"""QA prompt strategies for supported repository kinds."""

from __future__ import annotations

from .base import QAStrategy
from .bi import BIQAStrategy
from .generic import GenericQAStrategy


def get_strategy(kind: str) -> QAStrategy:
    normalized = (kind or "bi").lower()
    if normalized == "bi":
        return BIQAStrategy()
    if normalized == "generic":
        return GenericQAStrategy()
    raise ValueError(f"Unknown repository kind: {kind!r}")


__all__ = ["BIQAStrategy", "GenericQAStrategy", "QAStrategy", "get_strategy"]
