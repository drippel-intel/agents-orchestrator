from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agents_orchestrator import db, orchestrator
from agents_orchestrator.agents.developer import DevRunOutcome
from agents_orchestrator.agents.qa import QARunOutcome
from agents_orchestrator.config import load_config
from agents_orchestrator.worktree import WorktreeInfo, slugify_branch


@pytest.fixture()
def config(tmp_path: Path):
    cfg = load_config()
    cfg.paths.state_db = tmp_path / "state.db"
    cfg.paths.logs_dir = tmp_path / "logs"
    cfg.caps.pipeline.max_parallel_dev_agents = 2
    return cfg


@pytest.fixture()
def daemon_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    started: list[str] = []

    monkeypatch.setattr(orchestrator, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(
        orchestrator,
        "resolve_model",
        lambda _model, api_key=None: SimpleNamespace(resolved="test-model", fallback_used=False),
    )

    def fake_provision(config, repo_path, branch, *, base_branch=None, **_kwargs):
        slug = slugify_branch(branch)
        worktree_path = tmp_path / f"wt-{slug}"
        worktree_path.mkdir(exist_ok=True)
        return WorktreeInfo(
            repo_path=Path(repo_path),
            base_branch=base_branch or "main",
            branch=branch,
            slug=slug,
            worktree_path=worktree_path,
            deploy_target_name=f"dev-{slug}",
        )

    def fake_run_developer_once(*, worktree_path, on_run_started=None, **_kwargs):
        assignment_ref = Path(worktree_path).name
        started.append(assignment_ref)
        if on_run_started is not None:
            on_run_started(f"agent-{assignment_ref}", f"run-{assignment_ref}")
        return DevRunOutcome(
            agent_id=f"agent-{assignment_ref}",
            run_id=f"run-{assignment_ref}",
            status="finished",
            final_text="done",
            error_message=None,
            cost_usd=0.01,
        )

    def fake_run_static_qa_once(**_kwargs):
        return QARunOutcome(
            agent_id="qa-agent",
            run_id="qa-run",
            status="finished",
            passed=True,
            report={"passed": True, "summary": "ok", "failures": [], "recommendations": []},
            final_text='{"passed": true}',
            error_message=None,
            cost_usd=0.01,
        )

    monkeypatch.setattr(orchestrator, "provision_worktree", fake_provision)
    monkeypatch.setattr(orchestrator, "run_developer_once", fake_run_developer_once)
    monkeypatch.setattr(orchestrator, "run_static_qa_once", fake_run_static_qa_once)
    monkeypatch.setattr(orchestrator, "run_live_qa_once", fake_run_static_qa_once)
    return started


def test_daemon_once_respects_parallel_cap(config, daemon_fakes, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="Do three independent things.",
            target_repo_path=str(repo),
            base_branch="main",
            status=db.PipelineStatus.APPROVED,
        )
        for idx in range(3):
            db.create_assignment(
                conn,
                pipeline_id,
                title=f"Assignment {idx}",
                branch=f"agents/{pipeline_id}/a{idx}",
                files=[f"file{idx}.tmdl"],
            )
    finally:
        conn.close()

    outcome = orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        assignments = db.list_assignments_for_pipeline(conn, pipeline_id)
        statuses = [a["status"] for a in assignments]
        pipeline = db.get_pipeline(conn, pipeline_id)
    finally:
        conn.close()

    assert outcome.assignments_started == 2
    assert statuses.count(db.AssignmentStatus.AWAITING_VALIDATION) == 2
    assert statuses.count(db.AssignmentStatus.PLANNED) == 1
    assert pipeline["status"] == db.PipelineStatus.RUNNING


def test_daemon_waits_for_dependencies(config, daemon_fakes, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="Parent then child.",
            target_repo_path=str(repo),
            base_branch="main",
            status=db.PipelineStatus.APPROVED,
        )
        parent_id = db.create_assignment(
            conn,
            pipeline_id,
            title="Parent",
            branch=f"agents/{pipeline_id}/parent",
            files=["parent.tmdl"],
        )
        child_id = db.create_assignment(
            conn,
            pipeline_id,
            title="Child",
            branch=f"agents/{pipeline_id}/child",
            files=["child.tmdl"],
            depends_on=[parent_id],
        )
    finally:
        conn.close()

    first = orchestrator.run_daemon_once(config)
    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(conn, parent_id, status=db.AssignmentStatus.DONE)
    finally:
        conn.close()
    second = orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        child = db.get_assignment(conn, child_id)
        pipeline = db.get_pipeline(conn, pipeline_id)
    finally:
        conn.close()

    assert first.assignments_started == 1
    assert second.assignments_started == 1
    assert child["status"] == db.AssignmentStatus.AWAITING_VALIDATION
    assert pipeline["status"] == db.PipelineStatus.RUNNING
