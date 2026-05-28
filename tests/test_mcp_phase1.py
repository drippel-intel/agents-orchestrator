from __future__ import annotations

from pathlib import Path

import pytest

from bi_orchestrator import db, mcp_server
from bi_orchestrator.config import load_config


@pytest.fixture()
def mcp_state(tmp_path: Path):
    cfg = load_config()
    cfg.paths.state_db = tmp_path / "state.db"
    cfg.paths.logs_dir = tmp_path / "logs"
    conn = db.connect(cfg.paths.state_db)
    old_config = mcp_server._CONFIG
    old_conn = mcp_server._CONN
    mcp_server._CONFIG = cfg
    mcp_server._CONN = conn
    try:
        yield conn
    finally:
        mcp_server._CONFIG = old_config
        mcp_server._CONN = old_conn
        conn.close()


def test_edit_show_and_approve_plan(mcp_state) -> None:
    pipeline_id = db.create_pipeline(
        mcp_state,
        requirements_doc="Add Sales YoY.",
        target_repo_path=r"Q:\BI\Users\Dudi\Developments\qov2",
        base_branch="main",
    )
    draft = {
        "summary": "Add Sales YoY.",
        "assignments": [
            {
                "title": "Add Sales YoY measures",
                "slug": "sales-yoy",
                "files": ["model/measures/Sales.tmdl"],
                "acceptance_criteria": ["Sales YoY returns expected values"],
                "scenarios": [
                    {
                        "name": "Sales YoY",
                        "kind": "acceptance_criteria",
                        "expected": {"measure": "Sales YoY"},
                    }
                ],
            }
        ],
    }

    edited = mcp_server.edit_plan(pipeline_id, draft, replace=True)
    assert edited["status"] == db.PipelineStatus.AWAITING_PLAN_APPROVAL
    assert edited["plan"]["assignments"][0]["branch"] == f"agents/{pipeline_id}/sales_yoy"

    shown = mcp_server.show_plan(pipeline_id)
    assert shown["plan"]["summary"] == "Add Sales YoY."

    approved = mcp_server.approve_plan(pipeline_id)
    assert approved["status"] == db.PipelineStatus.APPROVED
    assert len(approved["assignment_ids"]) == 1

    pipeline = db.get_pipeline(mcp_state, pipeline_id)
    assignments = db.list_assignments_for_pipeline(mcp_state, pipeline_id)
    scenarios = db.list_scenarios_for_assignment(mcp_state, assignments[0]["id"])

    assert pipeline["status"] == db.PipelineStatus.APPROVED
    assert assignments[0]["title"] == "Add Sales YoY measures"
    assert assignments[0]["files"] == ["model/measures/Sales.tmdl"]
    assert assignments[0]["acceptance_criteria"] == "- Sales YoY returns expected values"
    assert scenarios[0]["expected"] == {"measure": "Sales YoY"}


def test_approve_plan_requires_existing_plan(mcp_state) -> None:
    pipeline_id = db.create_pipeline(
        mcp_state,
        requirements_doc="No plan yet.",
        target_repo_path="X",
        base_branch="main",
    )

    result = mcp_server.approve_plan(pipeline_id)

    assert "error" in result
    assert "no plan" in result["error"].lower()


def test_approve_plan_rejects_overlapping_files(mcp_state) -> None:
    pipeline_id = db.create_pipeline(
        mcp_state,
        requirements_doc="Conflicting plan.",
        target_repo_path="X",
        base_branch="main",
    )
    db.set_pipeline_plan(
        mcp_state,
        pipeline_id,
        {
            "summary": "Conflict.",
            "assignments": [
                {"title": "A", "slug": "a", "files": ["model/sales.tmdl"]},
                {"title": "B", "slug": "b", "files": [r"Model\Sales.tmdl"]},
            ],
        },
        status=db.PipelineStatus.AWAITING_PLAN_APPROVAL,
    )

    result = mcp_server.approve_plan(pipeline_id)

    assert result["error"] == "Plan has overlapping assignment files."
    assert result["conflicts"][0]["path"] == "model/sales.tmdl"
