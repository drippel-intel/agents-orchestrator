"""Orchestration entrypoints.

Phase 0d provides ``run_smoke`` only — a single-assignment, single-dev-agent flow
that exercises every layer end-to-end against a real target repo:

1. Verify API key + model availability.
2. Create a pipeline + assignment in SQLite.
3. Provision a sibling worktree (and patch pbi-project.json for BI repos).
4. Launch the developer agent with a benign smoke prompt.
5. Persist agent_id / run_id / status / final text on the assignment.
6. Mark the pipeline / assignment terminal and (optionally) tear down the worktree.

Later phases replace this with the real state-machine loop: planner, fan-out,
QA, validation, merge.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .agents.developer import render_developer_prompt, resume_developer_once, run_developer_once
from .agents.planner import render_planner_prompt, run_planner_once
from .agents.qa import (
    render_developer_qa_feedback_prompt,
    run_live_qa_once,
    run_static_qa_once,
)
from .config import Config
from .detect import detect_repo_kind
from .qa_strategies import get_strategy
from .sdk_utils import ApiKeyMissing, get_api_key, resolve_model
from .state_machine import (
    all_assignments_done,
    any_assignment_failed,
    find_file_conflicts,
    ready_planned_assignments,
)
from .worktree import provision_worktree, teardown_worktree

log = logging.getLogger("agents_orchestrator.orchestrator")


SMOKE_PROMPT = (
    "This is an end-to-end smoke test from the agents-orchestrator infrastructure. "
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


@dataclass
class PlannerOutcome:
    pipeline_id: str
    agent_id: str | None
    run_id: str | None
    run_status: str
    plan: dict | None
    final_text: str | None


@dataclass
class DaemonTickOutcome:
    pipelines_seen: int
    assignments_started: int
    assignments_finished: int


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
    kind = detect_repo_kind(target_repo)

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
            kind=kind,
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

        info = provision_worktree(config, target_repo, branch, kind=kind)
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


def run_planner_for_pipeline(config: Config, pipeline_id: str) -> PlannerOutcome:
    """Run the Phase 1 planner for an existing pipeline and persist its draft."""
    api_key = get_api_key()
    resolution = resolve_model(config.models.planner, api_key=api_key)
    if resolution.fallback_used:
        log.warning(
            "Configured planner model %r not directly available; using %r.",
            resolution.requested, resolution.resolved,
        )

    conn = db.connect(config.paths.state_db)
    try:
        pipeline = db.get_pipeline(conn, pipeline_id)
        if pipeline is None:
            raise ValueError(f"No pipeline with id {pipeline_id!r}.")
        target_repo = Path(pipeline["target_repo_path"]).resolve()
        if not target_repo.is_dir():
            raise FileNotFoundError(f"target_repo does not exist: {target_repo}")
        kind = pipeline.get("kind") or "bi"

        prompt = render_planner_prompt(
            requirements=pipeline["requirements_doc"],
            target_repo_path=str(target_repo),
            base_branch=pipeline["base_branch"],
            pipeline_id=pipeline_id,
            config=config,
            kind=kind,
        )
        outcome = run_planner_once(
            api_key=api_key,
            model=resolution.resolved,
            target_repo_path=target_repo,
            prompt=prompt,
            pipeline_id=pipeline_id,
            config=config,
            kind=kind,
        )
        if outcome.plan is not None and outcome.status == "finished":
            db.set_pipeline_plan(
                conn,
                pipeline_id,
                outcome.plan,
                status=db.PipelineStatus.AWAITING_PLAN_APPROVAL,
            )
            db.add_notification(
                conn,
                kind="plan_ready",
                message=f"Pipeline {pipeline_id} has a planner draft awaiting approval.",
                pipeline_id=pipeline_id,
            )
        else:
            db.update_pipeline_status(conn, pipeline_id, db.PipelineStatus.FAILED)
        db.log_event(
            conn,
            pipeline_id=pipeline_id,
            kind="planner_run_completed",
            payload={
                "agent_id": outcome.agent_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "error": outcome.error_message,
                "cost_usd": outcome.cost_usd,
            },
        )
        return PlannerOutcome(
            pipeline_id=pipeline_id,
            agent_id=outcome.agent_id,
            run_id=outcome.run_id,
            run_status=outcome.status,
            plan=outcome.plan,
            final_text=outcome.final_text,
        )
    finally:
        conn.close()


def run_daemon_once(config: Config) -> DaemonTickOutcome:
    """Run one Phase 2 scheduling tick.

    A tick starts at most ``max_parallel_dev_agents`` ready developer assignments
    per pipeline and waits for those runs to finish. The long-running CLI daemon
    calls this repeatedly; tests can call it once for deterministic scheduling.
    """
    conn = db.connect(config.paths.state_db)
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pipelines_seen = 0
    try:
        for pipeline in db.list_pipelines(conn):
            if pipeline["status"] not in {db.PipelineStatus.APPROVED, db.PipelineStatus.RUNNING}:
                continue
            pipelines_seen += 1
            assignments = db.list_assignments_for_pipeline(conn, pipeline["id"])
            conflicts = find_file_conflicts(assignments)
            if conflicts:
                db.update_pipeline_status(conn, pipeline["id"], db.PipelineStatus.FAILED)
                db.add_notification(
                    conn,
                    kind="error",
                    message=(
                        f"Pipeline {pipeline['id']} has overlapping assignment files: "
                        + ", ".join(c.path for c in conflicts)
                    ),
                    pipeline_id=pipeline["id"],
                )
                continue
            if any_assignment_failed(assignments):
                db.update_pipeline_status(conn, pipeline["id"], db.PipelineStatus.FAILED)
                continue
            if all_assignments_done(assignments):
                db.update_pipeline_status(conn, pipeline["id"], db.PipelineStatus.DONE)
                continue

            ready = ready_planned_assignments(
                assignments,
                max_parallel=config.caps.pipeline.max_parallel_dev_agents,
            )
            if ready and pipeline["status"] == db.PipelineStatus.APPROVED:
                db.update_pipeline_status(conn, pipeline["id"], db.PipelineStatus.RUNNING)
            jobs.extend((pipeline, assignment) for assignment in ready)
    finally:
        conn.close()

    if not jobs:
        return DaemonTickOutcome(
            pipelines_seen=pipelines_seen,
            assignments_started=0,
            assignments_finished=0,
        )

    max_workers = min(len(jobs), max(1, config.caps.pipeline.max_parallel_dev_agents))
    finished = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(_run_developer_assignment, config, pipeline, assignment)
            for pipeline, assignment in jobs
        ]
        for future in as_completed(futures):
            future.result()
            finished += 1
    _finalize_pipeline_statuses(config, {pipeline["id"] for pipeline, _assignment in jobs})
    return DaemonTickOutcome(
        pipelines_seen=pipelines_seen,
        assignments_started=len(jobs),
        assignments_finished=finished,
    )


def run_daemon(
    config: Config,
    *,
    once: bool = False,
    sleep_seconds: float = 5.0,
) -> int:
    while True:
        outcome = run_daemon_once(config)
        log.info(
            "daemon tick: pipelines=%s started=%s finished=%s",
            outcome.pipelines_seen,
            outcome.assignments_started,
            outcome.assignments_finished,
        )
        if once:
            return 0
        time.sleep(sleep_seconds)


def _run_developer_assignment(
    config: Config,
    pipeline: dict[str, Any],
    assignment: dict[str, Any],
) -> None:
    assignment_id = assignment["id"]
    target_repo = Path(pipeline["target_repo_path"]).resolve()
    kind = pipeline.get("kind") or "bi"
    qa_strategy = get_strategy(kind)
    api_key = get_api_key()
    resolution = resolve_model(config.models.developer, api_key=api_key)
    qa_resolution = resolve_model(config.models.qa, api_key=api_key)
    info = provision_worktree(
        config,
        target_repo,
        assignment["branch"],
        base_branch=pipeline["base_branch"],
        kind=kind,
    )
    prompt = render_developer_prompt(
        title=assignment["title"],
        requirements=pipeline["requirements_doc"],
        files=assignment.get("files") or [],
        acceptance_criteria=assignment.get("acceptance_criteria"),
        deploy_target_name=info.deploy_target_name or assignment.get("deploy_target_name"),
        kind=kind,
    )

    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(
            conn,
            assignment_id,
            status=db.AssignmentStatus.DEV_RUNNING,
            worktree_path=str(info.worktree_path),
            deploy_target_name=info.deploy_target_name or assignment.get("deploy_target_name"),
            started_at=db._now(),
            dev_iter=(assignment.get("dev_iter") or 0) + 1,
        )
    finally:
        conn.close()

    def persist_started(agent_id: str | None, run_id: str | None) -> None:
        thread_conn = db.connect(config.paths.state_db)
        try:
            db.update_assignment(thread_conn, assignment_id, dev_agent_id=agent_id)
            db.log_event(
                thread_conn,
                assignment_id=assignment_id,
                pipeline_id=pipeline["id"],
                kind="developer_run_started",
                payload={"agent_id": agent_id, "run_id": run_id},
            )
        finally:
            thread_conn.close()

    dev_started = time.monotonic()
    if assignment["status"] == db.AssignmentStatus.VALIDATION_ITERATION and assignment.get("dev_agent_id"):
        dev_outcome = resume_developer_once(
            api_key=api_key,
            agent_id=assignment["dev_agent_id"],
            prompt=_latest_validation_feedback(config, assignment_id),
            on_run_started=persist_started,
        )
    else:
        dev_outcome = run_developer_once(
            api_key=api_key,
            model=resolution.resolved,
            worktree_path=info.worktree_path,
            prompt=prompt,
            on_run_started=persist_started,
        )
    _record_developer_completion(
        config,
        assignment_id=assignment_id,
        pipeline_id=pipeline["id"],
        outcome=dev_outcome,
        elapsed_seconds=time.monotonic() - dev_started,
    )
    if dev_outcome.status != "finished":
        _set_assignment_terminal(config, assignment_id, db.AssignmentStatus.FAILED)
        return

    qa_iter = assignment.get("qa_iter") or 0
    dev_agent_id = dev_outcome.agent_id or assignment.get("dev_agent_id")
    while True:
        qa_iter += 1
        conn = db.connect(config.paths.state_db)
        try:
            db.update_assignment(
                conn,
                assignment_id,
                status=db.AssignmentStatus.STATIC_QA_RUNNING,
                qa_iter=qa_iter,
            )
        finally:
            conn.close()

        qa_prompt = qa_strategy.render_static_prompt(
            title=assignment["title"],
            files=assignment.get("files") or [],
            acceptance_criteria=assignment.get("acceptance_criteria"),
            deploy_target_name=info.deploy_target_name or assignment.get("deploy_target_name"),
        )
        qa_started = time.monotonic()
        qa_outcome = run_static_qa_once(
            api_key=api_key,
            model=qa_resolution.resolved,
            worktree_path=info.worktree_path,
            prompt=qa_prompt,
        )
        _record_qa_completion(
            config,
            assignment_id=assignment_id,
            pipeline_id=pipeline["id"],
            outcome=qa_outcome,
            elapsed_seconds=time.monotonic() - qa_started,
            event_kind="static_qa_completed",
        )
        failed_report = qa_outcome.report
        if qa_outcome.status == "finished" and qa_outcome.passed:
            if not qa_strategy.has_live_qa:
                _mark_assignment_awaiting_validation(config, assignment_id, pipeline["id"])
                return
            conn = db.connect(config.paths.state_db)
            try:
                db.update_assignment(
                    conn,
                    assignment_id,
                    status=db.AssignmentStatus.LIVE_QA_RUNNING,
                )
            finally:
                conn.close()
            live_prompt = qa_strategy.render_live_prompt(
                title=assignment["title"],
                deploy_target_name=info.deploy_target_name or assignment.get("deploy_target_name"),
                scenarios=_list_assignment_scenarios(config, assignment_id),
            )
            live_started = time.monotonic()
            live_outcome = run_live_qa_once(
                api_key=api_key,
                model=qa_resolution.resolved,
                worktree_path=info.worktree_path,
                prompt=live_prompt,
            )
            _record_qa_completion(
                config,
                assignment_id=assignment_id,
                pipeline_id=pipeline["id"],
                outcome=live_outcome,
                elapsed_seconds=time.monotonic() - live_started,
                event_kind="live_qa_completed",
            )
            failed_report = live_outcome.report
            if live_outcome.status == "finished" and live_outcome.passed:
                _mark_assignment_awaiting_validation(config, assignment_id, pipeline["id"])
                return

        if qa_iter >= config.caps.assignment.max_qa_iterations:
            conn = db.connect(config.paths.state_db)
            try:
                db.update_assignment(conn, assignment_id, status=db.AssignmentStatus.CAP_EXCEEDED)
                db.add_notification(
                    conn,
                    kind="cap_exceeded",
                    message=(
                        f"Assignment {assignment_id} reached the static QA iteration cap "
                        f"({config.caps.assignment.max_qa_iterations})."
                    ),
                    assignment_id=assignment_id,
                    pipeline_id=pipeline["id"],
                )
            finally:
                conn.close()
            return

        if not dev_agent_id:
            _set_assignment_terminal(config, assignment_id, db.AssignmentStatus.FAILED)
            return

        feedback_prompt = render_developer_qa_feedback_prompt(failed_report or {})
        conn = db.connect(config.paths.state_db)
        try:
            current = db.get_assignment(conn, assignment_id) or assignment
            db.update_assignment(
                conn,
                assignment_id,
                status=db.AssignmentStatus.DEV_RUNNING,
                dev_iter=(current.get("dev_iter") or 0) + 1,
            )
        finally:
            conn.close()
        dev_started = time.monotonic()
        dev_outcome = resume_developer_once(
            api_key=api_key,
            agent_id=dev_agent_id,
            prompt=feedback_prompt,
            on_run_started=persist_started,
        )
        _record_developer_completion(
            config,
            assignment_id=assignment_id,
            pipeline_id=pipeline["id"],
            outcome=dev_outcome,
            elapsed_seconds=time.monotonic() - dev_started,
        )
        if dev_outcome.status != "finished":
            _set_assignment_terminal(config, assignment_id, db.AssignmentStatus.FAILED)
            return


def _record_developer_completion(
    config: Config,
    *,
    assignment_id: str,
    pipeline_id: str,
    outcome: Any,
    elapsed_seconds: float,
) -> None:
    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(
            conn,
            assignment_id=assignment_id,
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
                "elapsed_seconds": round(elapsed_seconds, 1),
                "cost_usd": outcome.cost_usd,
                "error": outcome.error_message,
                "final_text_preview": (outcome.final_text or "")[:500],
            },
        )
    finally:
        conn.close()


def _record_qa_completion(
    config: Config,
    *,
    assignment_id: str,
    pipeline_id: str,
    outcome: Any,
    elapsed_seconds: float,
    event_kind: str,
) -> None:
    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(conn, assignment_id, qa_agent_id=outcome.agent_id)
        db.log_event(
            conn,
            assignment_id=assignment_id,
            pipeline_id=pipeline_id,
            kind=event_kind,
            payload={
                "agent_id": outcome.agent_id,
                "run_id": outcome.run_id,
                "status": outcome.status,
                "passed": outcome.passed,
                "report": outcome.report,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "cost_usd": outcome.cost_usd,
                "error": outcome.error_message,
            },
        )
    finally:
        conn.close()


def _set_assignment_terminal(config: Config, assignment_id: str, status: str) -> None:
    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(conn, assignment_id, status=status)
    finally:
        conn.close()


def _mark_assignment_awaiting_validation(
    config: Config,
    assignment_id: str,
    pipeline_id: str,
) -> None:
    conn = db.connect(config.paths.state_db)
    try:
        db.update_assignment(conn, assignment_id, status=db.AssignmentStatus.AWAITING_VALIDATION)
        db.add_notification(
            conn,
            kind="validation_needed",
            message=f"Assignment {assignment_id} passed QA and is awaiting validation.",
            assignment_id=assignment_id,
            pipeline_id=pipeline_id,
        )
    finally:
        conn.close()


def _list_assignment_scenarios(config: Config, assignment_id: str) -> list[dict[str, Any]]:
    conn = db.connect(config.paths.state_db)
    try:
        return db.list_scenarios_for_assignment(conn, assignment_id)
    finally:
        conn.close()


def _latest_validation_feedback(config: Config, assignment_id: str) -> str:
    conn = db.connect(config.paths.state_db)
    try:
        events = db.list_events(conn, assignment_id=assignment_id, limit=20)
    finally:
        conn.close()
    for event in events:
        if event["kind"] == "validation_feedback":
            payload = event.get("payload") or {}
            feedback = payload.get("feedback") or "Validation requested another iteration."
            return (
                "Human validation requested changes. Address the feedback in the current "
                f"worktree, then summarize the fix.\n\nFeedback:\n{feedback}"
            )
    return "Human validation requested another iteration. Inspect the assignment and continue."


def _finalize_pipeline_statuses(config: Config, pipeline_ids: set[str]) -> None:
    conn = db.connect(config.paths.state_db)
    try:
        for pipeline_id in pipeline_ids:
            assignments = db.list_assignments_for_pipeline(conn, pipeline_id)
            if any_assignment_failed(assignments):
                db.update_pipeline_status(conn, pipeline_id, db.PipelineStatus.FAILED)
            elif all_assignments_done(assignments):
                db.update_pipeline_status(conn, pipeline_id, db.PipelineStatus.DONE)
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


def cli_plan(config: Config, pipeline_id: str) -> int:
    """CLI wrapper for the Phase 1 planner."""
    try:
        outcome = run_planner_for_pipeline(config, pipeline_id)
    except ApiKeyMissing as err:
        log.error("%s", err)
        return 1
    except Exception:
        log.exception("Planner run failed during setup")
        return 1

    log.info("---- Planner summary ----")
    log.info("Pipeline:   %s", outcome.pipeline_id)
    log.info("Agent id:   %s", outcome.agent_id)
    log.info("Run id:     %s", outcome.run_id)
    log.info("Run status: %s", outcome.run_status)
    if outcome.plan:
        log.info("Assignments planned: %d", len(outcome.plan.get("assignments", [])))
    if outcome.run_status == "finished" and outcome.plan:
        return 0
    if outcome.run_status == "startup_failed":
        return 1
    return 2
