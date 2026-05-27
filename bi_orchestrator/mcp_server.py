"""bi-orchestrator MCP server.

Stdio MCP server that exposes the orchestrator state store to any Cursor chat.
Reads / writes only SQLite; it does **not** spawn agents. Long-running
orchestration is the daemon's job (Phase 2+) — the MCP is the chat-side window
into the same SQLite single-source-of-truth.

Phase 0e ships:
- ``start_pipeline``: register a new pipeline (status ``planned``). The Phase 0
  smoke flow is run via the CLI (``bi-orchestrator smoke``); the daemon will pick
  up planned pipelines automatically in later phases.
- ``get_status``: list pipelines + assignments, or drill into one.
- ``list_recent_events``: recent state-machine events for debugging.

Install with ``bi-orchestrator install-mcp`` (see ``install.py``); Cursor spawns
this entrypoint on-demand per chat session.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import db
from .config import load_config
from .logging_setup import configure_logging


log = logging.getLogger("bi_orchestrator.mcp_server")

mcp = FastMCP("bi-orchestrator")

# Module-level config + connection. FastMCP runs synchronously per request; SQLite
# WAL mode tolerates concurrent reads with the daemon's writes without locking.
_CONFIG = None
_CONN = None


def _ensure_ready():
    global _CONFIG, _CONN
    if _CONFIG is None:
        _CONFIG = load_config()
        configure_logging(_CONFIG.paths.logs_dir, process_name="bi-orchestrator-mcp")
    if _CONN is None:
        _CONN = db.connect(_CONFIG.paths.state_db)
    return _CONFIG, _CONN


def _serialize_pipeline(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": p["id"],
        "created_at": p["created_at"],
        "updated_at": p["updated_at"],
        "status": p["status"],
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


@mcp.tool()
def start_pipeline(
    requirements: str,
    target_repo_path: str,
    base_branch: str = "main",
    notes: str | None = None,
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
        target_repo_path: Absolute path to the target BI repo (must be a git
            repository whose ``.cursor/mcp.json`` references the aim-pbi-dev
            toolchain). Example: ``Q:\\BI\\Users\\Dudi\\Developments\\qov2``.
        base_branch: Branch from which all per-assignment branches will be cut.
            Defaults to ``main``.
        notes: Optional short note (e.g. ``"smoke"`` or ``"hotfix batch"``).
    """
    config, conn = _ensure_ready()
    pid = db.create_pipeline(
        conn,
        requirements_doc=requirements,
        target_repo_path=target_repo_path,
        base_branch=base_branch,
        status=db.PipelineStatus.PLANNED,
        notes=notes,
    )
    return {
        "pipeline_id": pid,
        "status": db.PipelineStatus.PLANNED,
        "target_repo_path": target_repo_path,
        "base_branch": base_branch,
        "message": (
            f"Pipeline {pid} created. "
            "In Phase 0 the planner does not run automatically — use the CLI "
            "`bi-orchestrator smoke --target-repo <path>` to exercise the agent end-to-end."
        ),
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


def main() -> None:
    """Entry point used by Cursor (via ``~/.cursor/mcp.json``) and the
    ``bi-orchestrator-mcp`` console script."""
    _ensure_ready()
    log.info("bi-orchestrator MCP server starting (stdio)")
    mcp.run()


if __name__ == "__main__":
    main()
