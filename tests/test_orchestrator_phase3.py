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
    cfg.caps.pipeline.max_parallel_dev_agents = 1
    cfg.caps.assignment.max_qa_iterations = 2
    return cfg


def _seed_assignment(config, tmp_path: Path) -> tuple[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="Do one thing.",
            target_repo_path=str(repo),
            base_branch="main",
            status=db.PipelineStatus.APPROVED,
        )
        assignment_id = db.create_assignment(
            conn,
            pipeline_id,
            title="Assignment",
            branch=f"agents/{pipeline_id}/assignment",
            files=["assignment.tmdl"],
        )
        return pipeline_id, assignment_id
    finally:
        conn.close()


def _install_common_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(orchestrator, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(
        orchestrator,
        "resolve_model",
        lambda _model, api_key=None: SimpleNamespace(resolved="test-model", fallback_used=False),
    )

    def fake_provision(config, repo_path, branch, *, base_branch=None, kind="bi", **_kwargs):
        slug = slugify_branch(branch)
        worktree_path = tmp_path / f"wt-{slug}"
        worktree_path.mkdir(exist_ok=True)
        return WorktreeInfo(
            repo_path=Path(repo_path),
            base_branch=base_branch or "main",
            branch=branch,
            slug=slug,
            worktree_path=worktree_path,
            deploy_target_name=f"dev-{slug}" if kind == "bi" else None,
        )

    monkeypatch.setattr(orchestrator, "provision_worktree", fake_provision)


def _dev_outcome(agent_id: str = "dev-agent") -> DevRunOutcome:
    return DevRunOutcome(
        agent_id=agent_id,
        run_id="dev-run",
        status="finished",
        final_text="done",
        error_message=None,
        cost_usd=0.01,
    )


def _qa_outcome(passed: bool) -> QARunOutcome:
    return QARunOutcome(
        agent_id="qa-agent",
        run_id="qa-run",
        status="finished",
        passed=passed,
        report={
            "passed": passed,
            "summary": "ok" if passed else "failed",
            "failures": [] if passed else ["lint failed"],
            "recommendations": [],
        },
        final_text='{"passed": true}' if passed else '{"passed": false}',
        error_message=None,
        cost_usd=0.01,
    )


def test_static_qa_failure_resumes_dev_then_passes(
    config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pipeline_id, assignment_id = _seed_assignment(config, tmp_path)
    _install_common_fakes(monkeypatch, tmp_path)
    qa_results = [_qa_outcome(False), _qa_outcome(True)]
    resume_calls: list[str] = []

    monkeypatch.setattr(orchestrator, "run_developer_once", lambda **_kwargs: _dev_outcome())
    monkeypatch.setattr(orchestrator, "run_static_qa_once", lambda **_kwargs: qa_results.pop(0))
    monkeypatch.setattr(orchestrator, "run_live_qa_once", lambda **_kwargs: _qa_outcome(True))

    def fake_resume_developer_once(*, prompt, **_kwargs):
        resume_calls.append(prompt)
        return _dev_outcome()

    monkeypatch.setattr(orchestrator, "resume_developer_once", fake_resume_developer_once)

    outcome = orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        assignment = db.get_assignment(conn, assignment_id)
        pipeline = db.get_pipeline(conn, pipeline_id)
    finally:
        conn.close()

    assert outcome.assignments_started == 1
    assert assignment["status"] == db.AssignmentStatus.AWAITING_VALIDATION
    assert assignment["qa_iter"] == 2
    assert assignment["dev_iter"] == 2
    assert pipeline["status"] == db.PipelineStatus.RUNNING
    assert "lint failed" in resume_calls[0]


def test_static_qa_cap_exceeded_pauses_assignment(
    config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pipeline_id, assignment_id = _seed_assignment(config, tmp_path)
    _install_common_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator, "run_developer_once", lambda **_kwargs: _dev_outcome())
    monkeypatch.setattr(orchestrator, "run_static_qa_once", lambda **_kwargs: _qa_outcome(False))
    monkeypatch.setattr(orchestrator, "run_live_qa_once", lambda **_kwargs: _qa_outcome(True))
    monkeypatch.setattr(orchestrator, "resume_developer_once", lambda **_kwargs: _dev_outcome())

    orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        assignment = db.get_assignment(conn, assignment_id)
        pipeline = db.get_pipeline(conn, pipeline_id)
        notifications = db.list_unacked_notifications(conn)
    finally:
        conn.close()

    assert assignment["status"] == db.AssignmentStatus.CAP_EXCEEDED
    assert assignment["qa_iter"] == 2
    assert pipeline["status"] == db.PipelineStatus.FAILED
    assert notifications[0]["kind"] == "cap_exceeded"


def test_generic_pipeline_skips_live_qa(config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="Fix one generic bug.",
            target_repo_path=str(repo),
            base_branch="main",
            status=db.PipelineStatus.APPROVED,
            kind="generic",
        )
        assignment_id = db.create_assignment(
            conn,
            pipeline_id,
            title="Generic assignment",
            branch=f"agents/{pipeline_id}/generic",
            files=["src/app.py"],
        )
    finally:
        conn.close()

    _install_common_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator, "run_developer_once", lambda **_kwargs: _dev_outcome())
    monkeypatch.setattr(orchestrator, "run_static_qa_once", lambda **_kwargs: _qa_outcome(True))

    def fail_live_qa(**_kwargs):
        raise AssertionError("generic QA must not run the live QA stage")

    monkeypatch.setattr(orchestrator, "run_live_qa_once", fail_live_qa)

    orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        assignment = db.get_assignment(conn, assignment_id)
        events = db.list_events(conn, assignment_id=assignment_id)
    finally:
        conn.close()

    assert assignment["status"] == db.AssignmentStatus.AWAITING_VALIDATION
    assert assignment["deploy_target_name"] is None
    assert not any(event["kind"] == "live_qa_completed" for event in events)
