from __future__ import annotations

from pathlib import Path

import pytest

from agents_orchestrator.detect import detect_repo_kind, resolve_repo_kind


@pytest.mark.parametrize(
    "marker",
    [
        "pbi-project.json",
        ".aim-pbi-dev",
        ".cursor/skills/powerbi-dev",
        "qa/measure-baselines.json",
    ],
)
def test_detect_repo_kind_identifies_bi_markers(tmp_path: Path, marker: str) -> None:
    marker_path = tmp_path / marker
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    if marker_path.suffix:
        marker_path.write_text("{}", encoding="utf-8")
    else:
        marker_path.mkdir()

    assert detect_repo_kind(tmp_path) == "bi"


def test_detect_repo_kind_defaults_to_generic(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'app'\n", encoding="utf-8")

    assert detect_repo_kind(tmp_path) == "generic"


def test_resolve_repo_kind_accepts_override(tmp_path: Path) -> None:
    assert resolve_repo_kind(tmp_path, "bi") == "bi"
    assert resolve_repo_kind(tmp_path, "generic") == "generic"


def test_resolve_repo_kind_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="kind"):
        resolve_repo_kind(tmp_path, "not-a-kind")
