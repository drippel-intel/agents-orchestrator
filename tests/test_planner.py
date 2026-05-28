from __future__ import annotations

from pathlib import Path

import pytest

from agents_orchestrator.agents.planner import PlannerPlanError, parse_planner_response
from agents_orchestrator.config import load_config


@pytest.fixture()
def config(tmp_path: Path):
    cfg = load_config()
    cfg.paths.state_db = tmp_path / "state.db"
    cfg.paths.logs_dir = tmp_path / "logs"
    return cfg


def test_parse_planner_response_normalizes_defaults(config) -> None:
    text = """
    ```json
    {
      "summary": "Add sales measures.",
      "assignments": [
        {
          "title": "Add Sales YoY measures",
          "files": "model/measures/Sales.tmdl",
          "depends_on": null,
          "acceptance_criteria": ["YoY measure exists"],
          "scenarios": [
            {
              "name": "YoY measure",
              "kind": "acceptance_criteria",
              "expected": {"measure": "Sales YoY"}
            }
          ]
        }
      ],
      "notes": null
    }
    ```
    """

    plan = parse_planner_response(text, pipeline_id="p_abc123", config=config)

    assert plan["summary"] == "Add sales measures."
    assignment = plan["assignments"][0]
    assert assignment["slug"] == "add_sales_yoy_measures"
    assert assignment["branch"] == "agents/p_abc123/add_sales_yoy_measures"
    assert assignment["deploy_target_name"] == "dev-add_sales_yoy_measures"
    assert assignment["files"] == ["model/measures/Sales.tmdl"]
    assert assignment["acceptance_criteria"] == "- YoY measure exists"
    assert assignment["scenarios"][0]["expected"] == {"measure": "Sales YoY"}


def test_parse_planner_response_dedupes_slugs(config) -> None:
    text = """
    {
      "summary": "Two similar assignments.",
      "assignments": [
        {"title": "Update model"},
        {"title": "Update model"}
      ]
    }
    """

    plan = parse_planner_response(text, pipeline_id="p_abc123", config=config)

    assert [a["slug"] for a in plan["assignments"]] == ["update_model", "update_model_2"]


def test_parse_planner_response_rejects_missing_assignments(config) -> None:
    with pytest.raises(PlannerPlanError):
        parse_planner_response('{"summary": "empty", "assignments": []}', pipeline_id="p", config=config)


def test_parse_planner_response_omits_deploy_target_for_generic(config) -> None:
    text = """
    {
      "summary": "Fix API behavior.",
      "assignments": [
        {
          "title": "Fix API response",
          "files": ["src/api.py"],
          "scenarios": [
            {
              "name": "Regression test",
              "kind": "unit_test",
              "expected": {"description": "test passes"}
            }
          ]
        }
      ]
    }
    """

    plan = parse_planner_response(text, pipeline_id="p_abc123", config=config, kind="generic")

    assignment = plan["assignments"][0]
    assert assignment["deploy_target_name"] is None
    assert assignment["scenarios"][0]["kind"] == "unit_test"
