from __future__ import annotations

from pathlib import Path

import pytest

from agents_orchestrator import db, mcp_server
from agents_orchestrator.config import load_config


@pytest.fixture()
def mcp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = load_config()
    cfg.paths.state_db = tmp_path / "state.db"
    cfg.paths.logs_dir = tmp_path / "logs"
    conn = db.connect(cfg.paths.state_db)
    old_config = mcp_server._CONFIG
    old_conn = mcp_server._CONN
    mcp_server._CONFIG = cfg
    mcp_server._CONN = conn
    monkeypatch.setattr(mcp_server, "_run_gh_merge", lambda worktree_path, pr_number: None)
    monkeypatch.setattr(mcp_server, "_cleanup_assignment_worktree", lambda pipeline, assignment: None)
    try:
        yield conn
    finally:
        mcp_server._CONFIG = old_config
        mcp_server._CONN = old_conn
        conn.close()


def _seed_validation_assignment(conn, tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "repo-wt-assignment"
    worktree.mkdir()
    pipeline_id = db.create_pipeline(
        conn,
        requirements_doc="Validate me.",
        target_repo_path=str(repo),
        base_branch="main",
        status=db.PipelineStatus.RUNNING,
    )
    assignment_id = db.create_assignment(
        conn,
        pipeline_id,
        title="Ready assignment",
        branch=f"agents/{pipeline_id}/ready",
        status=db.AssignmentStatus.AWAITING_VALIDATION,
        deploy_target_name="dev-ready",
    )
    db.update_assignment(
        conn,
        assignment_id,
        worktree_path=str(worktree),
        pr_number=123,
    )
    return pipeline_id, assignment_id


def test_pending_validations_and_rejection(mcp_state, tmp_path: Path) -> None:
    _pipeline_id, assignment_id = _seed_validation_assignment(mcp_state, tmp_path)

    pending = mcp_server.pending_validations()
    rejected = mcp_server.submit_validation(
        assignment_id,
        approved=False,
        feedback="Please adjust the acceptance wording.",
    )
    assignment = db.get_assignment(mcp_state, assignment_id)
    events = db.list_events(mcp_state, assignment_id=assignment_id)

    assert pending["validations"][0]["assignment_id"] == assignment_id
    assert rejected["status"] == db.AssignmentStatus.VALIDATION_ITERATION
    assert assignment["validation_iter"] == 1
    assert any(event["kind"] == "validation_feedback" for event in events)


def test_merge_assignment_requires_approval_then_marks_done(mcp_state, tmp_path: Path) -> None:
    pipeline_id, assignment_id = _seed_validation_assignment(mcp_state, tmp_path)

    without_approval = mcp_server.merge_assignment(assignment_id)
    approved = mcp_server.submit_validation(assignment_id, approved=True, feedback="Looks good.")
    merged = mcp_server.merge_assignment(assignment_id)
    assignment = db.get_assignment(mcp_state, assignment_id)
    pipeline = db.get_pipeline(mcp_state, pipeline_id)

    assert "not been validation-approved" in without_approval["error"]
    assert approved["approved"] is True
    assert merged["merged"] is True
    assert assignment["status"] == db.AssignmentStatus.DONE
    assert pipeline["status"] == db.PipelineStatus.DONE
