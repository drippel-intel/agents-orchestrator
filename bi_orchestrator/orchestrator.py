"""Orchestration entrypoints.

Phase 0d provides ``run_smoke`` only — a single-assignment, single-dev-agent flow
that exercises every layer end-to-end against a real BI repo:

1. Verify API key + model availability.
2. Create a pipeline + assignment in SQLite.
3. Provision a sibling worktree (and patch pbi-project.json if present).
4. Launch the developer agent with a benign smoke prompt.
5. Persist agent_id / run_id / status / final text on the assignment.
6. Mark the pipeline / assignment terminal and (optionally) tear down the worktree.

Later phases replace this with the real state-machine loop: planner, fan-out,
QA, validation, merge.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import db
from .agents.developer import run_developer_once
from .config import Config
from .sdk_utils import ApiKeyMissing, get_api_key, resolve_model
from .worktree import provision_worktree, teardown_worktree


log = logging.getLogger("bi_orchestrator.orchestrator")


SMOKE_PROMPT = (
    "This is an end-to-end smoke test from the bi-orchestrator infrastructure. "
    "Do NOT invoke any tools, do NOT modify any files, do NOT call any MCP servers. "
    "Reply with exactly the following two lines and nothing else:\n"
    "SMOKE OK\n"
    "AGENT_CWD=<your current working directory>"
)


@dataclass
class SmokeOutcome:
    pipeline_id: str
    assignment_id: str
    worktree_path: Path
    deploy_target_name: str | None
    agent_id: str | None
    run_id: str | None
    run_status: str
    final_text: str | None
    cleanup_performed: bool


def run_smoke(
    config: Config,
    target_repo: Path,
    *,
    cleanup: bool = False,
) -> SmokeOutcome:
    """End-to-end Phase 0 smoke flow. See module docstring for steps."""
    target_repo = Path(target_repo).resolve()
    if not target_repo.is_dir():
        raise FileNotFoundError(f"target_repo does not exist: {target_repo}")
    if not (target_repo / ".git").exists():
        raise RuntimeError(f"{target_repo} is not a git repository")

    api_key = get_api_key()
    resolution = resolve_model(config.models.developer, api_key=api_key)
    if resolution.fallback_used:
        log.warning(
            "Configured developer model %r not directly available; using %r.",
            resolution.requested, resolution.resolved,
        )

    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="[smoke] Phase 0 end-to-end smoke test.",
            target_repo_path=str(target_repo),
            base_branch=config.git.default_base_branch,
            notes="Phase 0d smoke",
        )
        db.update_pipeline_status(conn, pipeline_id, db.PipelineStatus.RUNNING)
        log.info("Created pipeline %s for repo %s", pipeline_id, target_repo)

        branch = config.git.branch_pattern.format(pipeline=pipeline_id, slug="smoke")
        assignment_id = db.create_assignment(
            conn,
            pipeline_id=pipeline_id,
            title="Phase 0 smoke",
            branch=branch,
        )
        log.info("Created assignment %s on branch %s", assignment_id, branch)

        info = provision_worktree(config, target_repo, branch)
        log.info(
            "Provisioned worktree at %s (deploy_target=%s)",
            info.worktree_path, info.deploy_target_name,
        )
        db.update_assignment(
            conn,
            assignment_id,
            status=db.AssignmentStatus.DEV_RUNNING,
            worktree_path=str(info.worktree_path),
            deploy_target_name=info.deploy_target_name,
            started_at=db._now(),
            dev_iter=1,
        )

        started = time.monotonic()
        outcome = run_developer_once(
            api_key=api_key,
            model=resolution.resolved,
            worktree_path=info.worktree_path,
            prompt=SMOKE_PROMPT,
        )
        elapsed = time.monotonic() - started
        log.info("Developer agent finished in %.1fs: %s", elapsed, outcome.status)

        final_status = (
            db.AssignmentStatus.DONE
            if outcome.status == "finished"
            else db.AssignmentStatus.FAILED
        )
        db.update_assignment(
            conn,
            assignment_id,
            status=final_status,
            dev_agent_id=outcome.agent_id,
            cost_usd=outcome.cost_usd or 0.0,
        )
        db.log_event(
            conn,
            assignment_id=assignment_id,
            pipeline_id=pipeline_id,
            kind="developer_run_completed",
            payload={
                "agent_id": outcome.agent_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "elapsed_seconds": round(elapsed, 1),
                "cost_usd": outcome.cost_usd,
                "error": outcome.error_message,
                "final_text_preview": (outcome.final_text or "")[:500],
            },
        )

        pipeline_status = (
            db.PipelineStatus.DONE if outcome.status == "finished" else db.PipelineStatus.FAILED
        )
        db.update_pipeline_status(conn, pipeline_id, pipeline_status)

        cleanup_performed = False
        if cleanup and outcome.status == "finished":
            log.info("Tearing down worktree at %s", info.worktree_path)
            teardown_worktree(info, force=True, delete_branch=True)
            cleanup_performed = True
        elif cleanup:
            log.warning(
                "Cleanup requested but run did not finish cleanly; leaving worktree at %s for inspection",
                info.worktree_path,
            )

        return SmokeOutcome(
            pipeline_id=pipeline_id,
            assignment_id=assignment_id,
            worktree_path=info.worktree_path,
            deploy_target_name=info.deploy_target_name,
            agent_id=outcome.agent_id,
            run_id=outcome.run_id,
            run_status=outcome.status,
            final_text=outcome.final_text,
            cleanup_performed=cleanup_performed,
        )
    finally:
        conn.close()


def cli_smoke(
    config: Config,
    target_repo: Path,
    *,
    cleanup: bool = False,
) -> int:
    """CLI wrapper for ``run_smoke`` that returns a process exit code per the
    SDK guidance: 0 = finished, 2 = run-failed, 1 = startup-failed / setup error.
    """
    try:
        outcome = run_smoke(config, target_repo, cleanup=cleanup)
    except ApiKeyMissing as err:
        log.error("%s", err)
        return 1
    except Exception:
        log.exception("Smoke run failed during setup")
        return 1

    log.info("---- Smoke summary ----")
    log.info("Pipeline:           %s", outcome.pipeline_id)
    log.info("Assignment:         %s", outcome.assignment_id)
    log.info("Worktree:           %s", outcome.worktree_path)
    log.info("Deploy target name: %s", outcome.deploy_target_name)
    log.info("Agent id:           %s", outcome.agent_id)
    log.info("Run id:             %s", outcome.run_id)
    log.info("Run status:         %s", outcome.run_status)
    log.info("Cleanup performed:  %s", outcome.cleanup_performed)
    if outcome.final_text:
        preview = outcome.final_text.strip().splitlines()[:5]
        log.info("Final text (first 5 lines):")
        for line in preview:
            log.info("  | %s", line)

    if outcome.run_status == "finished":
        return 0
    if outcome.run_status == "startup_failed":
        return 1
    return 2
