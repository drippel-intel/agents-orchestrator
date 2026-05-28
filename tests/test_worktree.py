"""Phase 0c tests for the worktree module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agents_orchestrator import worktree as wt
from agents_orchestrator.config import (
    AssignmentCaps,
    BiConfig,
    CapsConfig,
    Config,
    DeployTargetConfig,
    GitConfig,
    McpConfig,
    ModelsConfig,
    PathsConfig,
    PipelineCaps,
)

# ---------- Helpers -----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return out.stdout


def _make_repo(tmp_path: Path, name: str = "qov2") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _make_config(tmp_path: Path) -> Config:
    return Config(
        paths=PathsConfig(
            state_db=tmp_path / "state.db",
            logs_dir=tmp_path / "logs",
            worktree_root_kind="sibling",
        ),
        models=ModelsConfig(planner="opus-4.7", developer="opus-4.7", qa="opus-4.7"),
        caps=CapsConfig(assignment=AssignmentCaps(), pipeline=PipelineCaps()),
        bi=BiConfig(deploy_target=DeployTargetConfig(pattern="{base}-{slug}")),
        git=GitConfig(branch_pattern="agents/{pipeline}/{slug}", default_base_branch="main"),
        mcp=McpConfig(server_name="agents-orchestrator"),
    )


def _write_pbi_project(repo: Path) -> None:
    content = {
        "model": {
            "targets": {
                "dev": {
                    "server": "powerbi://api.powerbi.com/v1.0/myorg/BIS Datasets Dev",
                    "database": "BIS_qov2",
                },
                "prod": {
                    "server": "powerbi://api.powerbi.com/v1.0/myorg/BIS Datasets Prod",
                    "database": "BIS_qov2",
                },
            },
            "defaultTarget": "dev",
            "modelPath": "bis_qov2.SemanticModel",
            "format": "tmdl",
            "compatibilityLevel": 1604,
        },
        "report": {
            "pbipPath": "./QOV 2.1.pbip",
            "targets": {
                "dev": {"workspaceName": "BIS CONS", "reportName": "QOV 2.1"},
                "prod": {"workspaceName": "BIS Production"},
            },
            "defaultTarget": "dev",
        },
    }
    (repo / "pbi-project.json").write_text(json.dumps(content, indent=2), encoding="utf-8")
    _git(repo, "add", "pbi-project.json")
    _git(repo, "commit", "-q", "-m", "add pbi-project.json")


# ---------- Slug + path tests --------------------------------------------------

def test_slugify_strips_namespace_and_normalizes() -> None:
    assert wt.slugify_branch("agents/p_a3f2/sales-yoy") == "sales_yoy"
    assert wt.slugify_branch("feat/Date Dim refactor!") == "date_dim_refactor"
    assert wt.slugify_branch("main") == "main"


def test_compute_worktree_path_is_sibling(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    repo = tmp_path / "qov2"
    p = wt.compute_worktree_path(repo, "sales_yoy", config)
    assert p == tmp_path / "qov2-wt-sales_yoy"


# ---------- Worktree lifecycle -------------------------------------------------

def test_provision_then_teardown_with_new_branch(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = _make_config(tmp_path)

    info = wt.provision_worktree(
        config, repo, branch="agents/p_test/sales-yoy", patch_pbi_project=False
    )
    assert info.worktree_path.is_dir()
    assert (info.worktree_path / ".git").exists()  # worktree marker

    # The branch was created at HEAD of main.
    branches = _git(repo, "branch", "--list", "agents/p_test/sales-yoy")
    assert "agents/p_test/sales-yoy" in branches

    # Idempotent re-provision is a no-op.
    info2 = wt.provision_worktree(
        config, repo, branch="agents/p_test/sales-yoy", patch_pbi_project=False
    )
    assert info2.worktree_path == info.worktree_path

    wt.teardown_worktree(info, force=True, delete_branch=True)
    assert not info.worktree_path.exists()
    branches_after = _git(repo, "branch", "--list", "agents/p_test/sales-yoy")
    assert "agents/p_test/sales-yoy" not in branches_after


# ---------- pbi-project.json patcher ------------------------------------------

def test_patch_clones_dev_target_with_branch_suffix(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_pbi_project(repo)
    config = _make_config(tmp_path)

    info = wt.provision_worktree(
        config, repo, branch="agents/p_test/sales-yoy", patch_pbi_project=True
    )

    assert info.deploy_target_name == "dev-sales_yoy"
    project = json.loads((info.worktree_path / "pbi-project.json").read_text(encoding="utf-8"))
    model_targets = project["model"]["targets"]
    report_targets = project["report"]["targets"]

    assert "dev-sales_yoy" in model_targets
    assert model_targets["dev-sales_yoy"]["database"] == "BIS_qov2_dev-sales_yoy"
    assert model_targets["dev-sales_yoy"]["server"] == model_targets["dev"]["server"]
    assert project["model"]["defaultTarget"] == "dev-sales_yoy"

    # Existing dev / prod targets are preserved untouched.
    assert model_targets["dev"]["database"] == "BIS_qov2"
    assert model_targets["prod"]["database"] == "BIS_qov2"

    assert "dev-sales_yoy" in report_targets
    assert report_targets["dev-sales_yoy"]["reportName"] == "QOV 2.1 [dev-sales_yoy]"
    assert report_targets["dev-sales_yoy"]["workspaceName"] == "BIS CONS"
    assert project["report"]["defaultTarget"] == "dev-sales_yoy"

    wt.teardown_worktree(info, force=True)


def test_patch_is_idempotent(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_pbi_project(repo)
    config = _make_config(tmp_path)

    info = wt.provision_worktree(
        config, repo, branch="agents/p_test/sales-yoy", patch_pbi_project=True
    )
    first = (info.worktree_path / "pbi-project.json").read_text(encoding="utf-8")
    # Re-running the patcher does not double-suffix the database name.
    wt.patch_pbi_project_targets(info.worktree_path, info.deploy_target_name)  # type: ignore[arg-type]
    second = (info.worktree_path / "pbi-project.json").read_text(encoding="utf-8")
    assert first == second

    wt.teardown_worktree(info, force=True)


def test_patch_missing_base_target_raises(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = _make_config(tmp_path)
    # Write a pbi-project.json without a 'dev' target.
    (repo / "pbi-project.json").write_text(
        json.dumps({"model": {"targets": {"prod": {"database": "x"}}}, "report": {"targets": {}}}),
        encoding="utf-8",
    )
    _git(repo, "add", "pbi-project.json")
    _git(repo, "commit", "-q", "-m", "add bad pbi-project")

    with pytest.raises(KeyError, match="no base target 'dev'"):
        wt.provision_worktree(
            config, repo, branch="agents/p_test/x", patch_pbi_project=True
        )


def test_generic_kind_skips_pbi_project_patching(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write_pbi_project(repo)
    config = _make_config(tmp_path)

    info = wt.provision_worktree(
        config,
        repo,
        branch="agents/p_test/generic",
        kind="generic",
    )

    assert info.deploy_target_name is None
    project = json.loads((info.worktree_path / "pbi-project.json").read_text(encoding="utf-8"))
    assert "dev-generic" not in project["model"]["targets"]

    wt.teardown_worktree(info, force=True)
