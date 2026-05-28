from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bi_orchestrator import db, orchestrator
from bi_orchestrator.agents.developer import DevRunOutcome
from bi_orchestrator.agents.qa import QARunOutcome
from bi_orchestrator.config import load_config
from bi_orchestrator.worktree import WorktreeInfo, slugify_branch


@pytest.fixture()
def config(tmp_path: Path):
    cfg = load_config()
    cfg.paths.state_db = tmp_path / "state.db"
    cfg.paths.logs_dir = tmp_path / "logs"
    cfg.caps.pipeline.max_parallel_dev_agents = 1
    cfg.caps.assignment.max_qa_iterations = 3
    return cfg


def _dev_outcome() -> DevRunOutcome:
    return DevRunOutcome(
        agent_id="dev-agent",
        run_id="dev-run",
        status="finished",
        final_text="done",
        error_message=None,
        cost_usd=0.01,
    )


def _qa_outcome(passed: bool, summary: str) -> QARunOutcome:
    return QARunOutcome(
        agent_id="qa-agent",
        run_id="qa-run",
        status="finished",
        passed=passed,
        report={
            "passed": passed,
            "summary": summary,
            "failures": [] if passed else [summary],
            "recommendations": [],
        },
        final_text='{"passed": true}' if passed else '{"passed": false}',
        error_message=None,
        cost_usd=0.01,
    )


def test_live_qa_failure_resumes_dev_then_passes(
    config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    conn = db.connect(config.paths.state_db)
    try:
        pipeline_id = db.create_pipeline(
            conn,
            requirements_doc="Live QA requirement.",
            target_repo_path=str(repo),
            base_branch="main",
            status=db.PipelineStatus.APPROVED,
        )
        assignment_id = db.create_assignment(
            conn,
            pipeline_id,
            title="Live assignment",
            branch=f"agents/{pipeline_id}/live",
            files=["live.tmdl"],
            deploy_target_name="dev-live",
        )
        db.create_scenario(
            conn,
            assignment_id,
            name="Measure baseline",
            kind="dax_assertion",
            expected={"measure": "Sales", "value": 100},
        )
    finally:
        conn.close()

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
            deploy_target_name="dev-live",
        )

    live_prompts: list[str] = []
    live_results = [_qa_outcome(False, "regression failed"), _qa_outcome(True, "live ok")]
    resume_prompts: list[str] = []
    monkeypatch.setattr(orchestrator, "provision_worktree", fake_provision)
    monkeypatch.setattr(orchestrator, "run_developer_once", lambda **_kwargs: _dev_outcome())
    monkeypatch.setattr(orchestrator, "run_static_qa_once", lambda **_kwargs: _qa_outcome(True, "static ok"))

    def fake_live_qa_once(*, prompt, **_kwargs):
        live_prompts.append(prompt)
        return live_results.pop(0)

    def fake_resume(*, prompt, **_kwargs):
        resume_prompts.append(prompt)
        return _dev_outcome()

    monkeypatch.setattr(orchestrator, "run_live_qa_once", fake_live_qa_once)
    monkeypatch.setattr(orchestrator, "resume_developer_once", fake_resume)

    orchestrator.run_daemon_once(config)

    conn = db.connect(config.paths.state_db)
    try:
        assignment = db.get_assignment(conn, assignment_id)
        events = db.list_events(conn, assignment_id=assignment_id)
    finally:
        conn.close()

    assert assignment["status"] == db.AssignmentStatus.AWAITING_VALIDATION
    assert assignment["qa_iter"] == 2
    assert "dev-live" in live_prompts[0]
    assert "Measure baseline" in live_prompts[0]
    assert "regression failed" in resume_prompts[0]
    assert any(event["kind"] == "live_qa_completed" for event in events)
