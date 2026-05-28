"""agents-orchestrator MCP server.

Stdio MCP server that exposes the orchestrator state store to any Cursor chat.
Reads / writes only SQLite; it does **not** spawn agents. Long-running
orchestration is the daemon's job (Phase 2+) — the MCP is the chat-side window
into the same SQLite single-source-of-truth.

Phase 0e ships:
- ``start_pipeline``: register a new pipeline (status ``planned``). The Phase 0
  smoke flow is run via the CLI (``agents-orchestrator smoke``); the daemon will pick
  up planned pipelines automatically in later phases.
- ``get_status``: list pipelines + assignments, or drill into one.
- ``list_recent_events``: recent state-machine events for debugging.

Install with ``agents-orchestrator install-mcp`` (see ``install.py``); Cursor spawns
this entrypoint on-demand per chat session.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from .agents.planner import PlannerPlanError, normalize_plan
from .config import load_config
from .detect import resolve_repo_kind
from .logging_setup import configure_logging
from .state_machine import find_file_conflicts
from .worktree import WorktreeInfo, slugify_branch, teardown_worktree

log = logging.getLogger("agents_orchestrator.mcp_server")

mcp = FastMCP("agents-orchestrator")

# Module-level config + connection. FastMCP runs synchronously per request; SQLite
# WAL mode tolerates concurrent reads with the daemon's writes without locking.
_CONFIG = None
_CONN = None


def _ensure_ready():
    global _CONFIG, _CONN
    if _CONFIG is None:
        _CONFIG = load_config()
        configure_logging(_CONFIG.paths.logs_dir, process_name="agents-orchestrator-mcp")
    if _CONN is None:
        _CONN = db.connect(_CONFIG.paths.state_db)
    return _CONFIG, _CONN


def _serialize_pipeline(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "status": p["status"],
        "kind": p.get("kind") or "bi",
        "target_repo_path": p["target_repo_path"],
        "base_branch": p["base_branch"],
        "requirements_doc": p["requirements_doc"],
        "cost_usd": p["cost_usd"],
        "notes": p.get("notes"),
        "plan": p.get("plan"),
    }


def _serialize_assignment(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": a["id"],
        "pipeline_id": a["pipeline_id"],
        "title": a["title"],
        "branch": a["branch"],
        "worktree_path": a.get("worktree_path"),
        "deploy_target_name": a.get("deploy_target_name"),
        "status": a["status"],
        "depends_on": a.get("depends_on") or [],
        "files": a.get("files") or [],
        "dev_agent_id": a.get("dev_agent_id"),
        "dev_iter": a["dev_iter"],
        "qa_agent_id": a.get("qa_agent_id"),
        "qa_iter": a["qa_iter"],
        "validation_iter": a["validation_iter"],
        "pr_number": a.get("pr_number"),
        "cost_usd": a["cost_usd"],
        "started_at": a.get("started_at"),
        "last_event_at": a.get("last_event_at"),
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            # Lists, including assignments, are replaced as whole values.
            merged[key] = value
    return merged


def _normalize_or_error(pipeline: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    config, _conn = _ensure_ready()
    try:
        return normalize_plan(
            plan,
            pipeline_id=pipeline["id"],
            config=config,
            kind=pipeline.get("kind") or "bi",
        )
    except PlannerPlanError as err:
        raise ValueError(f"Invalid plan for pipeline {pipeline['id']}: {err}") from err


def _create_assignments_from_plan(
    pipeline: dict[str, Any],
    plan: dict[str, Any],
) -> list[str]:
    _config, conn = _ensure_ready()
    existing = db.list_assignments_for_pipeline(conn, pipeline["id"])
    if existing:
        return [a["id"] for a in existing]

    planned = plan.get("assignments") or []
    assignment_ids = {
        assignment["slug"]: db.new_assignment_id(pipeline["id"])
        for assignment in planned
    }
    ref_to_id: dict[str, str] = {}
    for assignment in planned:
        aid = assignment_ids[assignment["slug"]]
        for ref in (assignment["slug"], assignment["title"], assignment["branch"]):
            ref_to_id[str(ref)] = aid

    created_ids: list[str] = []
    for assignment in planned:
        aid = assignment_ids[assignment["slug"]]
        depends_on = [
            ref_to_id.get(str(dep), str(dep))
            for dep in assignment.get("depends_on", [])
        ]
        db.create_assignment(
            conn,
            pipeline_id=pipeline["id"],
            title=assignment["title"],
            branch=assignment["branch"],
            files=assignment.get("files", []),
            depends_on=depends_on,
            acceptance_criteria=assignment.get("acceptance_criteria"),
            deploy_target_name=assignment.get("deploy_target_name"),
            assignment_id=aid,
        )
        for scenario in assignment.get("scenarios", []):
            db.create_scenario(
                conn,
                assignment_id=aid,
                name=scenario["name"],
                kind=scenario["kind"],
                expected=scenario.get("expected") or {},
            )
        created_ids.append(aid)
    return created_ids


@mcp.tool()
def start_pipeline(
    requirements: str,
    target_repo_path: str,
    base_branch: str = "main",
    notes: str | None = None,
    kind: str = "auto",
) -> dict[str, Any]:
    """Register a new orchestration pipeline.

    Creates a pipeline row in the state store with status ``planned``. The
    orchestrator daemon (when running) will then pick it up to invoke the
    planner agent and begin fan-out. Returns the pipeline id so the caller can
    poll status.

    Args:
        requirements: Free-form text describing the batch of work the user
            wants done. The planner agent will decompose this into per-branch
            assignments in Phase 1+.
        target_repo_path: Absolute path to the target repo.
        base_branch: Branch from which all per-assignment branches will be cut.
            Defaults to ``main``.
        notes: Optional short note (e.g. ``"smoke"`` or ``"hotfix batch"``).
        kind: ``auto`` (default), ``bi``, or ``generic``. Auto uses repo markers
            such as ``pbi-project.json`` and ``.aim-pbi-dev``.
    """
    config, conn = _ensure_ready()
    resolved_kind = resolve_repo_kind(Path(target_repo_path), kind)
    pid = db.create_pipeline(
        conn,
        requirements_doc=requirements,
        target_repo_path=target_repo_path,
        base_branch=base_branch,
        status=db.PipelineStatus.PLANNED,
        notes=notes,
        kind=resolved_kind,
    )
    return {
        "pipeline_id": pid,
        "status": db.PipelineStatus.PLANNED,
        "kind": resolved_kind,
        "target_repo_path": target_repo_path,
        "base_branch": base_branch,
        "message": (
            f"Pipeline {pid} created. "
            "Run `agents-orchestrator plan "
            f"{pid}` to generate a planner draft, then use show_plan / approve_plan."
        ),
    }


@mcp.tool()
def show_plan(pipeline_id: str) -> dict[str, Any]:
    """Return the planner draft for a pipeline, if one exists."""
    _config, conn = _ensure_ready()
    pipeline = db.get_pipeline(conn, pipeline_id)
    if pipeline is None:
        return {"error": f"No pipeline with id {pipeline_id!r}."}
    plan = pipeline.get("plan")
    if plan is None:
        return {
            "pipeline_id": pipeline_id,
            "status": pipeline["status"],
            "plan": None,
            "message": "No planner draft has been recorded yet.",
        }
    return {
        "pipeline_id": pipeline_id,
        "status": pipeline["status"],
        "plan": plan,
    }


@mcp.tool()
def edit_plan(
    pipeline_id: str,
    patch_json: dict[str, Any],
    replace: bool = False,
) -> dict[str, Any]:
    """Edit a planner draft before approval.

    Args:
        pipeline_id: Pipeline whose draft plan should be edited.
        patch_json: Either a full replacement plan when ``replace`` is true, or
            a shallow/deep patch. Lists such as ``assignments`` replace the
            existing list.
        replace: Store ``patch_json`` as the whole plan instead of merging it
            into the existing plan.
    """
    _config, conn = _ensure_ready()
    pipeline = db.get_pipeline(conn, pipeline_id)
    if pipeline is None:
        return {"error": f"No pipeline with id {pipeline_id!r}."}
    if pipeline["status"] in {
        db.PipelineStatus.APPROVED,
        db.PipelineStatus.RUNNING,
        db.PipelineStatus.DONE,
        db.PipelineStatus.FAILED,
        db.PipelineStatus.CANCELLED,
    }:
        return {"error": f"Pipeline {pipeline_id} cannot be edited from status {pipeline['status']}."}

    current = pipeline.get("plan") or {}
    candidate = patch_json if replace else _deep_merge(current, patch_json)
    try:
        normalized = _normalize_or_error(pipeline, candidate)
    except ValueError as err:
        return {"error": str(err)}

    db.set_pipeline_plan(
        conn,
        pipeline_id,
        normalized,
        status=db.PipelineStatus.AWAITING_PLAN_APPROVAL,
    )
    return {
        "pipeline_id": pipeline_id,
        "status": db.PipelineStatus.AWAITING_PLAN_APPROVAL,
        "plan": normalized,
        "message": "Plan updated and awaiting approval.",
    }


@mcp.tool()
def approve_plan(pipeline_id: str) -> dict[str, Any]:
    """Approve a planner draft and materialize assignment rows."""
    _config, conn = _ensure_ready()
    pipeline = db.get_pipeline(conn, pipeline_id)
    if pipeline is None:
        return {"error": f"No pipeline with id {pipeline_id!r}."}
    plan = pipeline.get("plan")
    if plan is None:
        return {"error": f"Pipeline {pipeline_id} has no plan to approve."}
    if pipeline["status"] in {
        db.PipelineStatus.RUNNING,
        db.PipelineStatus.DONE,
        db.PipelineStatus.FAILED,
        db.PipelineStatus.CANCELLED,
    }:
        return {"error": f"Pipeline {pipeline_id} cannot be approved from status {pipeline['status']}."}

    try:
        normalized = _normalize_or_error(pipeline, plan)
    except ValueError as err:
        return {"error": str(err)}
    conflicts = find_file_conflicts(normalized.get("assignments", []))
    if conflicts:
        return {
            "error": "Plan has overlapping assignment files.",
            "conflicts": [
                {"path": c.path, "assignment_refs": list(c.assignment_refs)}
                for c in conflicts
            ],
        }

    db.set_pipeline_plan(conn, pipeline_id, normalized)
    assignment_ids = _create_assignments_from_plan(pipeline, normalized)
    db.update_pipeline_status(conn, pipeline_id, db.PipelineStatus.APPROVED)
    db.log_event(
        conn,
        pipeline_id=pipeline_id,
        kind="plan_approved",
        payload={"assignments": assignment_ids},
    )
    return {
        "pipeline_id": pipeline_id,
        "status": db.PipelineStatus.APPROVED,
        "assignment_ids": assignment_ids,
        "message": "Plan approved. The daemon will fan out assignments in Phase 2.",
    }


@mcp.tool()
def get_status(pipeline_id: str | None = None) -> dict[str, Any]:
    """Inspect pipeline and assignment state.

    Without arguments, returns a summary of every pipeline ordered newest first.
    With ``pipeline_id``, returns that pipeline's full record and all of its
    assignments.

    Args:
        pipeline_id: Optional pipeline id (e.g. ``p_a3f2c1``). When omitted,
            all pipelines are summarized.
    """
    _config, conn = _ensure_ready()

    if pipeline_id is None:
        pipelines = db.list_pipelines(conn)
        if not pipelines:
            return {"pipelines": [], "message": "No pipelines recorded yet."}
        return {
            "pipelines": [
                {
                    "id": p["id"],
                    "status": p["status"],
                    "kind": p.get("kind") or "bi",
                    "target_repo_path": p["target_repo_path"],
                    "created_at": p["created_at"],
                    "updated_at": p["updated_at"],
                    "notes": p.get("notes"),
                }
                for p in pipelines
            ]
        }

    pipeline = db.get_pipeline(conn, pipeline_id)
    if pipeline is None:
        return {"error": f"No pipeline with id {pipeline_id!r}."}
    assignments = db.list_assignments_for_pipeline(conn, pipeline_id)
    return {
        "pipeline": _serialize_pipeline(pipeline),
        "assignments": [_serialize_assignment(a) for a in assignments],
    }


@mcp.tool()
def list_recent_events(
    assignment_id: str | None = None,
    pipeline_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return the most recent state-machine events.

    Useful for debugging "why did this assignment fail" — events form an
    append-only audit log including status changes, agent starts/completions,
    QA reports, and cap breaches.

    Args:
        assignment_id: Filter to events for this assignment only.
        pipeline_id: Filter to events for this pipeline only.
            Ignored when ``assignment_id`` is also given.
        limit: Maximum number of events to return (newest first). Default 50.
    """
    _config, conn = _ensure_ready()
    events = db.list_events(
        conn,
        assignment_id=assignment_id,
        pipeline_id=pipeline_id,
        limit=limit,
    )
    return {
        "events": [
            {
                "id": e["id"],
                "ts": e["ts"],
                "kind": e["kind"],
                "assignment_id": e.get("assignment_id"),
                "pipeline_id": e.get("pipeline_id"),
                "payload": e.get("payload") or {},
            }
            for e in events
        ]
    }


@mcp.tool()
def pending_notifications() -> dict[str, Any]:
    """List unacknowledged notifications (validation-needed, cap breaches, etc.).

    Phase 0 mostly populates this from the smoke flow; later phases drive it
    from the orchestrator loop (validation gating, etc.). Use
    ``acknowledge_notification`` to dismiss one after handling it.
    """
    _config, conn = _ensure_ready()
    rows = db.list_unacked_notifications(conn)
    return {
        "notifications": [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "kind": r["kind"],
                "message": r["message"],
                "assignment_id": r.get("assignment_id"),
                "pipeline_id": r.get("pipeline_id"),
            }
            for r in rows
        ]
    }


@mcp.tool()
def acknowledge_notification(notification_id: int) -> dict[str, Any]:
    """Mark a notification as acknowledged so it no longer appears in
    ``pending_notifications``.

    Args:
        notification_id: Integer id returned by ``pending_notifications``.
    """
    _config, conn = _ensure_ready()
    db.acknowledge_notification(conn, notification_id)
    return {"acknowledged_id": notification_id, "ok": True}


@mcp.tool()
def pending_validations() -> dict[str, Any]:
    """List assignments that passed QA and need human validation."""
    _config, conn = _ensure_ready()
    validations: list[dict[str, Any]] = []
    for pipeline in db.list_pipelines(conn):
        for assignment in db.list_assignments_for_pipeline(conn, pipeline["id"]):
            if assignment["status"] != db.AssignmentStatus.AWAITING_VALIDATION:
                continue
            validations.append(
                {
                    "assignment_id": assignment["id"],
                    "pipeline_id": pipeline["id"],
                    "title": assignment["title"],
                    "branch": assignment["branch"],
                    "worktree_path": assignment.get("worktree_path"),
                    "deploy_target_name": assignment.get("deploy_target_name"),
                    "pr_number": assignment.get("pr_number"),
                    "validation_iter": assignment["validation_iter"],
                }
            )
    return {"validations": validations}


@mcp.tool()
def submit_validation(assignment_id: str, approved: bool, feedback: str = "") -> dict[str, Any]:
    """Submit human validation for an assignment.

    Approved assignments remain awaiting validation until ``merge_assignment`` is
    called. Rejected assignments return to the daemon for another capped dev/QA
    iteration.
    """
    _config, conn = _ensure_ready()
    assignment = db.get_assignment(conn, assignment_id)
    if assignment is None:
        return {"error": f"No assignment with id {assignment_id!r}."}
    if assignment["status"] != db.AssignmentStatus.AWAITING_VALIDATION:
        return {
            "error": (
                f"Assignment {assignment_id} is {assignment['status']}, "
                "not awaiting validation."
            )
        }

    if approved:
        db.log_event(
            conn,
            assignment_id=assignment_id,
            pipeline_id=assignment["pipeline_id"],
            kind="validation_approved",
            payload={"feedback": feedback},
        )
        return {
            "assignment_id": assignment_id,
            "approved": True,
            "message": "Validation approved. Call merge_assignment to merge and clean up.",
        }

    next_iter = (assignment.get("validation_iter") or 0) + 1
    if next_iter > _config.caps.assignment.max_validation_iterations:
        db.update_assignment(
            conn,
            assignment_id,
            status=db.AssignmentStatus.CAP_EXCEEDED,
            validation_iter=next_iter,
        )
        db.add_notification(
            conn,
            kind="cap_exceeded",
            message=f"Assignment {assignment_id} exceeded the validation iteration cap.",
            assignment_id=assignment_id,
            pipeline_id=assignment["pipeline_id"],
        )
        return {"assignment_id": assignment_id, "status": db.AssignmentStatus.CAP_EXCEEDED}

    db.update_assignment(
        conn,
        assignment_id,
        status=db.AssignmentStatus.VALIDATION_ITERATION,
        validation_iter=next_iter,
    )
    db.log_event(
        conn,
        assignment_id=assignment_id,
        pipeline_id=assignment["pipeline_id"],
        kind="validation_feedback",
        payload={"feedback": feedback, "validation_iter": next_iter},
    )
    return {
        "assignment_id": assignment_id,
        "approved": False,
        "status": db.AssignmentStatus.VALIDATION_ITERATION,
        "validation_iter": next_iter,
        "message": "Feedback recorded. The daemon will resume the developer agent.",
    }


@mcp.tool()
def merge_assignment(assignment_id: str) -> dict[str, Any]:
    """Squash-merge an approved assignment PR and clean up its worktree."""
    _config, conn = _ensure_ready()
    assignment = db.get_assignment(conn, assignment_id)
    if assignment is None:
        return {"error": f"No assignment with id {assignment_id!r}."}
    if assignment["status"] != db.AssignmentStatus.AWAITING_VALIDATION:
        return {
            "error": (
                f"Assignment {assignment_id} is {assignment['status']}, "
                "not ready to merge."
            )
        }
    if not _has_validation_approval(conn, assignment_id):
        return {"error": f"Assignment {assignment_id} has not been validation-approved."}
    if assignment.get("pr_number") is None:
        return {"error": f"Assignment {assignment_id} has no PR number recorded."}

    pipeline = db.get_pipeline(conn, assignment["pipeline_id"])
    if pipeline is None:
        return {"error": f"Pipeline {assignment['pipeline_id']} is missing."}

    db.update_assignment(conn, assignment_id, status=db.AssignmentStatus.MERGING)
    try:
        _run_gh_merge(Path(assignment["worktree_path"]), int(assignment["pr_number"]))
        _cleanup_assignment_worktree(pipeline, assignment)
    except Exception as err:
        db.update_assignment(conn, assignment_id, status=db.AssignmentStatus.FAILED)
        db.log_event(
            conn,
            assignment_id=assignment_id,
            pipeline_id=assignment["pipeline_id"],
            kind="merge_failed",
            payload={"error": str(err)},
        )
        return {"error": str(err)}

    db.update_assignment(conn, assignment_id, status=db.AssignmentStatus.DONE)
    db.log_event(
        conn,
        assignment_id=assignment_id,
        pipeline_id=assignment["pipeline_id"],
        kind="assignment_merged",
        payload={"pr_number": assignment["pr_number"]},
    )
    assignments = db.list_assignments_for_pipeline(conn, assignment["pipeline_id"])
    if all(a["status"] == db.AssignmentStatus.DONE for a in assignments):
        db.update_pipeline_status(conn, assignment["pipeline_id"], db.PipelineStatus.DONE)
    db.add_notification(
        conn,
        kind="merged",
        message=f"Assignment {assignment_id} merged and cleaned up.",
        assignment_id=assignment_id,
        pipeline_id=assignment["pipeline_id"],
    )
    return {"assignment_id": assignment_id, "status": db.AssignmentStatus.DONE, "merged": True}


def _has_validation_approval(conn, assignment_id: str) -> bool:
    return any(
        event["kind"] == "validation_approved"
        for event in db.list_events(conn, assignment_id=assignment_id, limit=50)
    )


def _run_gh_merge(worktree_path: Path, pr_number: int) -> None:
    result = subprocess.run(
        ["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "gh pr merge failed")


def _cleanup_assignment_worktree(pipeline: dict[str, Any], assignment: dict[str, Any]) -> None:
    worktree_path = assignment.get("worktree_path")
    if not worktree_path:
        return
    info = WorktreeInfo(
        repo_path=Path(pipeline["target_repo_path"]),
        base_branch=pipeline["base_branch"],
        branch=assignment["branch"],
        slug=slugify_branch(assignment["branch"]),
        worktree_path=Path(worktree_path),
        deploy_target_name=assignment.get("deploy_target_name"),
    )
    teardown_worktree(info, force=True, delete_branch=True)


def main() -> None:
    """Entry point used by Cursor (via ``~/.cursor/mcp.json``) and the
    ``agents-orchestrator-mcp`` console script."""
    _ensure_ready()
    log.info("agents-orchestrator MCP server starting (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
