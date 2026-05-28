"""Git worktree lifecycle for agents-orchestrator assignments.

Each assignment gets its own worktree of the target repo. BI repositories also
receive a per-branch ``pbi-project.json`` deploy target so parallel agents do not
collide in shared Power BI workspaces.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bi_provisioning import PbiTargetPatch, patch_pbi_project_targets
from .config import Config

log = logging.getLogger("agents_orchestrator.worktree")

_SLUG_NONALNUM = re.compile(r"[^a-z0-9]+")


def slugify_branch(branch: str) -> str:
    """Filesystem-safe slug derived from a branch name."""
    tail = branch.rsplit("/", 1)[-1]
    cleaned = _SLUG_NONALNUM.sub("_", tail.lower()).strip("_")
    return cleaned or "branch"


def compute_worktree_path(repo_path: Path, slug: str, config: Config) -> Path:
    """Where the orchestrator places the worktree for this branch."""
    if config.paths.worktree_root_kind == "sibling":
        return repo_path.parent / f"{repo_path.name}-wt-{slug}"
    if config.paths.worktree_dedicated_dir is None:
        raise ValueError(
            "worktree_root_kind=dedicated_dir but worktree_dedicated_dir is unset"
        )
    return config.paths.worktree_dedicated_dir / f"{repo_path.name}-wt-{slug}"


@dataclass
class WorktreeInfo:
    repo_path: Path
    base_branch: str
    branch: str
    slug: str
    worktree_path: Path
    deploy_target_name: str | None = None


def _run_git(repo_path: Path, *args: str) -> str:
    """Run ``git -C <repo> <args>`` with stderr captured into the error message."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _branch_exists(repo_path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _worktree_already_at(repo_path: Path, worktree_path: Path) -> bool:
    """Return True if git already has a worktree registered at this path."""
    out = _run_git(repo_path, "worktree", "list", "--porcelain")
    normalized = str(worktree_path).replace("\\", "/").lower()
    for line in out.splitlines():
        if line.startswith("worktree "):
            existing = line[len("worktree "):].strip().replace("\\", "/").lower()
            if existing == normalized:
                return True
    return False


def add_worktree(
    repo_path: Path,
    branch: str,
    worktree_path: Path,
    base_branch: str = "main",
) -> None:
    """Create a worktree at ``worktree_path`` on ``branch``."""
    if _worktree_already_at(repo_path, worktree_path):
        log.info("Worktree already present at %s; skipping add", worktree_path)
        return

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    if _branch_exists(repo_path, branch):
        log.info("Adding worktree at %s on existing branch %s", worktree_path, branch)
        _run_git(repo_path, "worktree", "add", str(worktree_path), branch)
    else:
        log.info(
            "Adding worktree at %s with new branch %s based on %s",
            worktree_path,
            branch,
            base_branch,
        )
        _run_git(repo_path, "worktree", "add", "-b", branch, str(worktree_path), base_branch)


def remove_worktree(
    repo_path: Path,
    worktree_path: Path,
    *,
    force: bool = False,
    delete_branch: str | None = None,
) -> None:
    """Remove the worktree and optionally delete its branch."""
    if not _worktree_already_at(repo_path, worktree_path):
        log.info("Worktree %s already absent; skipping remove", worktree_path)
    else:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))
        log.info("Removing worktree at %s (force=%s)", worktree_path, force)
        _run_git(repo_path, *args)

    if delete_branch and _branch_exists(repo_path, delete_branch):
        log.info("Deleting branch %s", delete_branch)
        _run_git(repo_path, "branch", "-D" if force else "-d", delete_branch)


def provision_worktree(
    config: Config,
    repo_path: Path,
    branch: str,
    *,
    base_branch: str | None = None,
    kind: str = "bi",
    patch_pbi_project: bool | None = None,
    base_target_name: str = "dev",
) -> WorktreeInfo:
    """Create a worktree and apply kind-specific provisioning."""
    base_branch = base_branch or config.git.default_base_branch
    slug = slugify_branch(branch)
    worktree_path = compute_worktree_path(repo_path, slug, config)

    add_worktree(repo_path, branch, worktree_path, base_branch=base_branch)

    deploy_target_name: str | None = None
    should_patch_bi = kind == "bi" and (patch_pbi_project is not False)
    if should_patch_bi and (worktree_path / "pbi-project.json").is_file():
        deploy_target_name = config.bi.deploy_target.pattern.format(
            base=base_target_name,
            slug=slug,
        )
        patch_pbi_project_targets(
            worktree_path,
            deploy_target_name,
            base_target_name=base_target_name,
        )

    return WorktreeInfo(
        repo_path=repo_path,
        base_branch=base_branch,
        branch=branch,
        slug=slug,
        worktree_path=worktree_path,
        deploy_target_name=deploy_target_name,
    )


def teardown_worktree(
    info: WorktreeInfo,
    *,
    force: bool = False,
    delete_branch: bool = False,
) -> None:
    remove_worktree(
        info.repo_path,
        info.worktree_path,
        force=force,
        delete_branch=info.branch if delete_branch else None,
    )


__all__ = [
    "PbiTargetPatch",
    "WorktreeInfo",
    "add_worktree",
    "compute_worktree_path",
    "patch_pbi_project_targets",
    "provision_worktree",
    "remove_worktree",
    "slugify_branch",
    "teardown_worktree",
]
