"""Target repository kind detection."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

RepoKind = Literal["bi", "generic"]

BI_MARKERS = (
    "pbi-project.json",
    ".aim-pbi-dev",
    ".cursor/skills/powerbi-dev",
    "qa/measure-baselines.json",
)


def detect_repo_kind(target_repo: Path) -> RepoKind:
    """Infer whether a repo should use BI-specific orchestration behavior."""
    repo = Path(target_repo)
    return "bi" if any((repo / marker).exists() for marker in BI_MARKERS) else "generic"


def resolve_repo_kind(target_repo: Path, requested: str = "auto") -> RepoKind:
    """Resolve a user-supplied kind value, using marker detection for ``auto``."""
    normalized = requested.lower().strip()
    if normalized == "auto":
        return detect_repo_kind(target_repo)
    if normalized in {"bi", "generic"}:
        return normalized  # type: ignore[return-value]
    raise ValueError("kind must be one of: auto, bi, generic")
