"""Phase 0b smoke tests for the SQLite DAO.

Exercises migrations, pipeline / assignment creation, status updates, events,
notifications, and the reboot-recovery query.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bi_orchestrator import db


@pytest.fixture()
def conn(tmp_path: Path):
    state_db = tmp_path / "state.db"
    connection = db.connect(state_db)
    yield connection
    connection.close()


def test_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    c1 = db.connect(state_db)
    rows = c1.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [db.SCHEMA_LATEST]
    c1.close()

    c2 = db.connect(state_db)
    rows = c2.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r["version"] for r in rows] == [db.SCHEMA_LATEST]
    c2.close()


def test_pipeline_lifecycle(conn: sqlite3.Connection) -> None:
    pid = db.create_pipeline(
        conn,
        requirements_doc="Add YoY measures to Sales model.",
        target_repo_path=r"Q:\BI\Users\Dudi\Developments\qov2",
        base_branch="main",
    )
    assert pid.startswith("p_")

    pipeline = db.get_pipeline(conn, pid)
    assert pipeline is not None
    assert pipeline["status"] == db.PipelineStatus.PLANNED
    assert pipeline["target_repo_path"].endswith("qov2")

    db.update_pipeline_status(conn, pid, db.PipelineStatus.RUNNING)
    refreshed = db.get_pipeline(conn, pid)
    assert refreshed["status"] == db.PipelineStatus.RUNNING

    db.set_pipeline_plan(conn, pid, {"assignments": [{"title": "A1"}]})
    with_plan = db.get_pipeline(conn, pid)
    assert with_plan["plan"] == {"assignments": [{"title": "A1"}]}


def test_assignment_lifecycle_and_in_flight_query(conn: sqlite3.Connection) -> None:
    pid = db.create_pipeline(
        conn,
        requirements_doc="...",
        target_repo_path=r"Q:\BI\Users\Dudi\Developments\qov2",
        base_branch="main",
    )

    aid_done = db.create_assignment(
        conn, pid, title="Done assignment", branch="feat/done",
        files=["a.tmdl"], depends_on=[],
    )
    aid_running = db.create_assignment(
        conn, pid, title="Running assignment", branch="feat/running",
        files=["b.tmdl"], depends_on=[],
    )
    aid_qa = db.create_assignment(
        conn, pid, title="QA assignment", branch="feat/qa",
        files=["c.tmdl"], depends_on=[],
    )
    aid_planned = db.create_assignment(
        conn, pid, title="Planned assignment", branch="feat/planned",
        files=["d.tmdl"], depends_on=[],
    )

    db.update_assignment(conn, aid_done, status=db.AssignmentStatus.DONE)
    db.update_assignment(
        conn, aid_running,
        status=db.AssignmentStatus.DEV_RUNNING,
        dev_agent_id="local-agent-abc",
        dev_iter=1,
    )
    db.update_assignment(conn, aid_qa, status=db.AssignmentStatus.STATIC_QA_RUNNING)

    in_flight = db.list_in_flight_assignments(conn)
    in_flight_ids = {a["id"] for a in in_flight}
    assert aid_running in in_flight_ids
    assert aid_qa in in_flight_ids
    assert aid_done not in in_flight_ids
    assert aid_planned not in in_flight_ids

    running = db.get_assignment(conn, aid_running)
    assert running["dev_agent_id"] == "local-agent-abc"
    assert running["dev_iter"] == 1
    assert running["files"] == ["b.tmdl"]


def test_events_and_notifications(conn: sqlite3.Connection) -> None:
    pid = db.create_pipeline(
        conn, requirements_doc="...", target_repo_path="X", base_branch="main"
    )
    aid = db.create_assignment(conn, pid, title="T", branch="b")
    db.update_assignment(conn, aid, status=db.AssignmentStatus.DEV_RUNNING)

    # create_pipeline + create_assignment + the status update each log an event.
    events = db.list_events(conn, assignment_id=aid)
    kinds = {e["kind"] for e in events}
    assert "assignment_created" in kinds
    assert "assignment_status_change" in kinds

    nid = db.add_notification(
        conn,
        kind="validation_needed",
        message="PR #42 is ready for your review",
        assignment_id=aid,
        pipeline_id=pid,
    )
    assert isinstance(nid, int)

    unacked = db.list_unacked_notifications(conn)
    assert any(n["id"] == nid for n in unacked)

    db.acknowledge_notification(conn, nid)
    unacked_after = db.list_unacked_notifications(conn)
    assert not any(n["id"] == nid for n in unacked_after)


def test_concurrent_readers_work_in_wal_mode(tmp_path: Path) -> None:
    state_db = tmp_path / "state.db"
    writer = db.connect(state_db)
    reader = db.connect(state_db)
    try:
        pid = db.create_pipeline(
            writer, requirements_doc="r", target_repo_path="X", base_branch="main"
        )
        # Reader should immediately see the committed write (autocommit mode).
        rows = reader.execute("SELECT id FROM pipeline").fetchall()
        assert [r["id"] for r in rows] == [pid]
    finally:
        writer.close()
        reader.close()
