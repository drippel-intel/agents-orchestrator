-- Initial schema for agents-orchestrator state store.
-- Applied automatically at daemon / MCP startup if not already present.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    -- planned | awaiting_plan_approval | approved | running | done | failed | cancelled
    requirements_doc TEXT NOT NULL,
    target_repo_path TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    plan_json TEXT,                            -- planner output once produced
    cost_usd REAL NOT NULL DEFAULT 0.0,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS assignment (
    id TEXT PRIMARY KEY,
    pipeline_id TEXT NOT NULL REFERENCES pipeline(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    title TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT,
    deploy_target_name TEXT,
    status TEXT NOT NULL,
    -- planned | dev_running | static_qa_running | live_qa_running |
    -- awaiting_validation | validation_iteration | merging |
    -- done | paused | cancelled | cap_exceeded | failed
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT,
    dev_agent_id TEXT,
    dev_iter INTEGER NOT NULL DEFAULT 0,
    qa_agent_id TEXT,
    qa_iter INTEGER NOT NULL DEFAULT 0,
    validation_iter INTEGER NOT NULL DEFAULT 0,
    pr_number INTEGER,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    started_at TEXT,
    last_event_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_assignment_pipeline ON assignment(pipeline_id);
CREATE INDEX IF NOT EXISTS idx_assignment_status   ON assignment(status);

CREATE TABLE IF NOT EXISTS scenario (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL REFERENCES assignment(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    -- dax_assertion | schema_diff | aggregate_reconcile | visual_smoke | acceptance_criteria
    expected_json TEXT NOT NULL,
    last_actual_json TEXT,
    last_status TEXT,                          -- pass | fail | not_run
    last_run_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_scenario_assignment ON scenario(assignment_id);

CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT REFERENCES assignment(id) ON DELETE CASCADE,
    pipeline_id TEXT REFERENCES pipeline(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_event_assignment ON event(assignment_id, id);
CREATE INDEX IF NOT EXISTS idx_event_pipeline   ON event(pipeline_id, id);

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    assignment_id TEXT REFERENCES assignment(id) ON DELETE CASCADE,
    pipeline_id TEXT REFERENCES pipeline(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    -- validation_needed | cap_exceeded | error | plan_ready | merged | etc.
    message TEXT NOT NULL,
    acknowledged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notification_unacked ON notification(acknowledged_at, id);
