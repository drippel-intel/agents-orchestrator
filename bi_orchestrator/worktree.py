"""Git worktree lifecycle plus per-branch ``pbi-project.json`` patching.

Each assignment gets its own sibling worktree of the target BI repo. Sibling
placement (e.g. ``Q:\\BI\\Users\\Dudi\\Developments\\qov2-wt-<slug>``) keeps the
hardcoded absolute MCP launcher paths in ``.cursor/mcp.json`` resolving correctly,
because cursor-sdk launches MCPs with the worktree as ``cwd``.

For BI parallelism we also need disjoint *deploy* targets per branch. The aim-pbi-dev
toolset reads model + report targets from ``pbi-project.json``; we clone the
``dev`` target into a new ``dev-<slug>`` target with a unique database / report
name so concurrent dev agents do not stomp on each other in the shared Power BI
workspace.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

log = logging.getLogger("bi_orchestrator.worktree")


# ---------- Slug / path helpers -----------------------------------------------

_SLUG_NONALNUM = re.compile(r"[^a-z0-9]+")


def slugify_branch(branch: str) -> str:
    """Filesystem- and SQL-Server-safe slug derived from a branch name.

    Strips namespace prefixes (``agents/p_abc/sales-yoy`` -> ``sales-yoy``),
    lowercases, collapses runs of non-alphanumerics into single underscores.
    """
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


# ---------- Worktree info object ----------------------------------------------

@dataclass
class WorktreeInfo:
    repo_path: Path
    base_branch: str
    branch: str
    slug: str
    worktree_path: Path
    deploy_target_name: str | None = None


# ---------- Git worktree operations -------------------------------------------

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
            f"git {' '.join(args)} failed in {repo_path}: {result.stderr.strip() or result.stdout.strip()}"
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
    """Create a worktree at ``worktree_path`` on ``branch``.

    - If ``branch`` does not exist, it is created from ``base_branch``.
    - If a worktree already exists at the path (e.g. daemon restart), this is a no-op.
    """
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
            worktree_path, branch, base_branch,
        )
        _run_git(
            repo_path,
            "worktree", "add", "-b", branch, str(worktree_path), base_branch,
        )


def remove_worktree(
    repo_path: Path,
    worktree_path: Path,
    *,
    force: bool = False,
    delete_branch: str | None = None,
) -> None:
    """Remove the worktree and (optionally) delete its branch.

    Safe to call when the worktree no longer exists on disk — uses ``--force`` to
    skip the cleanliness check when requested.
    """
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


# ---------- pbi-project.json patching -----------------------------------------

@dataclass
class PbiTargetPatch:
    """Result of patching ``pbi-project.json`` for a per-branch deploy target."""
    new_target_name: str
    model_database: str | None
    report_name: str | None


def patch_pbi_project_targets(
    worktree_path: Path,
    new_target_name: str,
    *,
    base_target_name: str = "dev",
    set_default: bool = True,
    project_file: str = "pbi-project.json",
) -> PbiTargetPatch:
    """Clone the model + report dev target into a uniquely named branch target.

    Reads ``<worktree_path>/<project_file>``, copies ``model.targets[<base>]`` and
    ``report.targets[<base>]`` into entries named ``<new_target_name>`` with
    suffixed ``database`` / ``reportName`` so concurrent dev agents do not
    collide in the shared Power BI workspace, and writes the file back.

    Idempotent: if the new target already exists it is left untouched.
    """
    path = worktree_path / project_file
    if not path.is_file():
        raise FileNotFoundError(f"No {project_file} in {worktree_path}")

    text = path.read_text(encoding="utf-8")
    project: dict[str, Any] = json.loads(text)

    model_db: str | None = None
    report_name: str | None = None

    model_targets = (project.setdefault("model", {})).setdefault("targets", {})
    if new_target_name in model_targets:
        log.info("Model target %s already present; not modifying", new_target_name)
        model_db = model_targets[new_target_name].get("database")
    else:
        if base_target_name not in model_targets:
            raise KeyError(
                f"model.targets has no base target '{base_target_name}'; "
                f"cannot derive a per-branch target. Available: {list(model_targets)}"
            )
        base = dict(model_targets[base_target_name])
        if "database" in base:
            base["database"] = f"{base['database']}_{new_target_name}"
            model_db = base["database"]
        model_targets[new_target_name] = base
        if set_default:
            project["model"]["defaultTarget"] = new_target_name
        log.info(
            "Patched model.targets: added '%s' with database='%s'",
            new_target_name, model_db,
        )

    report_targets = (project.setdefault("report", {})).setdefault("targets", {})
    if new_target_name in report_targets:
        log.info("Report target %s already present; not modifying", new_target_name)
        report_name = report_targets[new_target_name].get("reportName")
    elif base_target_name not in report_targets:
        log.info(
            "report.targets has no base target '%s'; skipping report patch.",
            base_target_name,
        )
    else:
        base = dict(report_targets[base_target_name])
        if "reportName" in base:
            base["reportName"] = f"{base['reportName']} [{new_target_name}]"
            report_name = base["reportName"]
        report_targets[new_target_name] = base
        if set_default:
            project["report"]["defaultTarget"] = new_target_name
        log.info(
            "Patched report.targets: added '%s' with reportName='%s'",
            new_target_name, report_name,
        )

    path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")

    return PbiTargetPatch(
        new_target_name=new_target_name,
        model_database=model_db,
        report_name=report_name,
    )


# ---------- High-level provisioning -------------------------------------------

def provision_worktree(
    config: Config,
    repo_path: Path,
    branch: str,
    *,
    base_branch: str | None = None,
    patch_pbi_project: bool = True,
    base_target_name: str = "dev",
) -> WorktreeInfo:
    """One-shot: pick worktree path, create worktree, patch pbi-project.json.

    Returns a ``WorktreeInfo`` capturing everything the dev / QA agents need.
    Idempotent — safe to call again after a daemon restart.
    """
    base_branch = base_branch or config.git.default_base_branch
    slug = slugify_branch(branch)
    worktree_path = compute_worktree_path(repo_path, slug, config)

    add_worktree(repo_path, branch, worktree_path, base_branch=base_branch)

    deploy_target_name: str | None = None
    if patch_pbi_project and (worktree_path / "pbi-project.json").is_file():
        deploy_target_name = config.deploy_target.pattern.format(
            base=base_target_name, slug=slug
        )
        patch_pbi_project_targets(
            worktree_path, deploy_target_name, base_target_name=base_target_name
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
