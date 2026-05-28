"""SQLite DAO for agents-orchestrator state.

Single source of truth shared between the orchestrator daemon and the MCP server.
WAL mode is enabled so the two processes can read/write concurrently without
blocking each other.

Phase 0b: core schema + the small set of mutations Phase 0d needs (pipelines,
assignments, status transitions, events, reboot-recovery query).
Later phases add helpers for scenarios, notifications, validation state, etc.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("agents_orchestrator.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_LATEST = 2


# ---------- Status enums (kept as strings to play nice with SQLite) ------------

class PipelineStatus:
    PLANNED = "planned"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    APPROVED = "approved"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AssignmentStatus:
    PLANNED = "planned"
    DEV_RUNNING = "dev_running"
    STATIC_QA_RUNNING = "static_qa_running"
    LIVE_QA_RUNNING = "live_qa_running"
    AWAITING_VALIDATION = "awaiting_validation"
    VALIDATION_ITERATION = "validation_iteration"
    MERGING = "merging"
    DONE = "done"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    CAP_EXCEEDED = "cap_exceeded"
    FAILED = "failed"


IN_FLIGHT_STATUSES: tuple[str, ...] = (
    AssignmentStatus.DEV_RUNNING,
    AssignmentStatus.STATIC_QA_RUNNING,
    AssignmentStatus.LIVE_QA_RUNNING,
)


# ---------- ID + timestamp helpers --------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_pipeline_id() -> str:
    """Human-friendly short id, e.g. ``p_a3f2c1``."""
    return "p_" + secrets.token_hex(3)


def new_assignment_id(pipeline_id: str) -> str:
    """Nested under the pipeline for readability, e.g. ``p_a3f2c1_a_2b91``."""
    return f"{pipeline_id}_a_{secrets.token_hex(2)}"


def new_scenario_id(assignment_id: str) -> str:
    return f"{assignment_id}_s_{secrets.token_hex(2)}"


# ---------- Connection + migrations -------------------------------------------

def connect(state_db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + foreign keys, applying migrations on first use."""
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    # Bootstrap the version table without a transaction; CREATE IF NOT EXISTS is idempotent.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TEXT NOT NULL"
        ")"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    current = row["v"]
    if current >= SCHEMA_LATEST:
        return

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    for migration in migration_files:
        # Filenames are "0001_init.sql"; extract the version prefix.
        try:
            version = int(migration.name.split("_", 1)[0])
        except ValueError:
            log.warning("Skipping migration with unparseable name: %s", migration.name)
            continue
        if version <= current:
            continue
        log.info("Applying migration %s", migration.name)
        with conn:
            conn.executescript(migration.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )


# ---------- Pipelines ----------------------------------------------------------

def create_pipeline(
    conn: sqlite3.Connection,
    requirements_doc: str,
    target_repo_path: str,
    base_branch: str,
    status: str = PipelineStatus.PLANNED,
    notes: str | None = None,
    kind: str = "bi",
) -> str:
    pipeline_id = new_pipeline_id()
    ts = _now()
    conn.execute(
        """
        INSERT INTO pipeline (
            id, created_at, updated_at, status,
            requirements_doc, target_repo_path, base_branch, notes, kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pipeline_id, ts, ts, status, requirements_doc, target_repo_path, base_branch, notes, kind),
    )
    log_event(conn, pipeline_id=pipeline_id, kind="pipeline_created",
              payload={"target_repo_path": target_repo_path, "base_branch": base_branch, "kind": kind})
    return pipeline_id


def update_pipeline_status(conn: sqlite3.Connection, pipeline_id: str, status: str) -> None:
    conn.execute(
        "UPDATE pipeline SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), pipeline_id),
    )
    log_event(conn, pipeline_id=pipeline_id, kind="pipeline_status_change",
              payload={"status": status})


def set_pipeline_plan(
    conn: sqlite3.Connection,
    pipeline_id: str,
    plan: dict[str, Any],
    *,
    status: str | None = None,
) -> None:
    if status is None:
        conn.execute(
            "UPDATE pipeline SET plan_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(plan), _now(), pipeline_id),
        )
    else:
        conn.execute(
            "UPDATE pipeline SET plan_json = ?, status = ?, updated_at = ? WHERE id = ?",
            (json.dumps(plan), status, _now(), pipeline_id),
        )
        log_event(conn, pipeline_id=pipeline_id, kind="pipeline_status_change",
                  payload={"status": status})
    log_event(
        conn,
        pipeline_id=pipeline_id,
        kind="pipeline_plan_updated",
        payload={"assignments": len(plan.get("assignments", []))},
    )


def get_pipeline(conn: sqlite3.Connection, pipeline_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM pipeline WHERE id = ?", (pipeline_id,)).fetchone()
    return _row_to_dict(row)


def list_pipelines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM pipeline ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- Assignments --------------------------------------------------------

def create_assignment(
    conn: sqlite3.Connection,
    pipeline_id: str,
    title: str,
    branch: str,
    files: Iterable[str] = (),
    depends_on: Iterable[str] = (),
    acceptance_criteria: str | None = None,
    deploy_target_name: str | None = None,
    status: str = AssignmentStatus.PLANNED,
    assignment_id: str | None = None,
) -> str:
    assignment_id = assignment_id or new_assignment_id(pipeline_id)
    ts = _now()
    conn.execute(
        """
        INSERT INTO assignment (
            id, pipeline_id, created_at, updated_at, title, branch,
            deploy_target_name, status, depends_on_json, files_json,
            acceptance_criteria
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id, pipeline_id, ts, ts, title, branch,
            deploy_target_name, status,
            json.dumps(list(depends_on)),
            json.dumps(list(files)),
            acceptance_criteria,
        ),
    )
    log_event(conn, assignment_id=assignment_id, pipeline_id=pipeline_id,
              kind="assignment_created",
              payload={"title": title, "branch": branch})
    return assignment_id


def update_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    **fields: Any,
) -> None:
    """Update arbitrary assignment columns. Caller is responsible for column names.

    Always bumps ``updated_at`` and, when status changes, logs an event.
    """
    if not fields:
        return
    status_change = "status" in fields
    fields = {**fields, "updated_at": _now()}
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = (*fields.values(), assignment_id)
    conn.execute(f"UPDATE assignment SET {set_clause} WHERE id = ?", params)
    if status_change:
        row = conn.execute(
            "SELECT pipeline_id FROM assignment WHERE id = ?", (assignment_id,)
        ).fetchone()
        log_event(
            conn,
            assignment_id=assignment_id,
            pipeline_id=row["pipeline_id"] if row else None,
            kind="assignment_status_change",
            payload={"status": fields["status"]},
        )


def get_assignment(conn: sqlite3.Connection, assignment_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM assignment WHERE id = ?", (assignment_id,)).fetchone()
    return _row_to_dict(row)


def list_assignments_for_pipeline(
    conn: sqlite3.Connection, pipeline_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM assignment WHERE pipeline_id = ? ORDER BY created_at",
        (pipeline_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_in_flight_assignments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Used at daemon start to recover state — these are the rows whose agents we need
    to ``Agent.resume(...)`` and re-attach watchers to.
    """
    placeholders = ", ".join("?" * len(IN_FLIGHT_STATUSES))
    rows = conn.execute(
        f"SELECT * FROM assignment WHERE status IN ({placeholders})",
        IN_FLIGHT_STATUSES,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- Scenarios ----------------------------------------------------------

def create_scenario(
    conn: sqlite3.Connection,
    assignment_id: str,
    name: str,
    kind: str,
    expected: dict[str, Any],
    *,
    status: str = "not_run",
    scenario_id: str | None = None,
) -> str:
    scenario_id = scenario_id or new_scenario_id(assignment_id)
    conn.execute(
        """
        INSERT INTO scenario (
            id, assignment_id, name, kind, expected_json, last_status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (scenario_id, assignment_id, name, kind, json.dumps(expected), status),
    )
    row = conn.execute(
        "SELECT pipeline_id FROM assignment WHERE id = ?", (assignment_id,)
    ).fetchone()
    log_event(
        conn,
        assignment_id=assignment_id,
        pipeline_id=row["pipeline_id"] if row else None,
        kind="scenario_created",
        payload={"name": name, "kind": kind},
    )
    return scenario_id


def list_scenarios_for_assignment(
    conn: sqlite3.Connection, assignment_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM scenario WHERE assignment_id = ? ORDER BY name",
        (assignment_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ---------- Events + notifications --------------------------------------------

def log_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    assignment_id: str | None = None,
    pipeline_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO event (assignment_id, pipeline_id, ts, kind, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (assignment_id, pipeline_id, _now(), kind, json.dumps(payload or {})),
    )
    if assignment_id is not None:
        conn.execute(
            "UPDATE assignment SET last_event_at = ? WHERE id = ?",
            (_now(), assignment_id),
        )


def list_events(
    conn: sqlite3.Connection,
    *,
    assignment_id: str | None = None,
    pipeline_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if assignment_id is not None:
        rows = conn.execute(
            "SELECT * FROM event WHERE assignment_id = ? ORDER BY id DESC LIMIT ?",
            (assignment_id, limit),
        ).fetchall()
    elif pipeline_id is not None:
        rows = conn.execute(
            "SELECT * FROM event WHERE pipeline_id = ? ORDER BY id DESC LIMIT ?",
            (pipeline_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM event ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def add_notification(
    conn: sqlite3.Connection,
    kind: str,
    message: str,
    *,
    assignment_id: str | None = None,
    pipeline_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO notification (created_at, assignment_id, pipeline_id, kind, message)
        VALUES (?, ?, ?, ?, ?)
        """,
        (_now(), assignment_id, pipeline_id, kind, message),
    )
    return int(cur.lastrowid)


def list_unacked_notifications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM notification WHERE acknowledged_at IS NULL ORDER BY id"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def acknowledge_notification(conn: sqlite3.Connection, notification_id: int) -> None:
    conn.execute(
        "UPDATE notification SET acknowledged_at = ? WHERE id = ?",
        (_now(), notification_id),
    )


# ---------- Internal helpers ---------------------------------------------------

def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    # Decode common JSON columns for convenience.
    for col in ("depends_on_json", "files_json", "expected_json",
                "last_actual_json", "plan_json", "payload_json"):
        if col in out and out[col] is not None:
            with suppress(TypeError, json.JSONDecodeError):
                out[col[:-5] if col.endswith("_json") else col] = json.loads(out[col])
    return out
