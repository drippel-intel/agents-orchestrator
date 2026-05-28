from __future__ import annotations

import pytest

from agents_orchestrator.qa_strategies import get_strategy


def test_bi_strategy_renders_static_and_live_prompts() -> None:
    strategy = get_strategy("bi")

    static_prompt = strategy.render_static_prompt(
        title="Add Sales YoY",
        files=["model/measures/Sales.tmdl"],
        acceptance_criteria="Sales YoY is present",
        deploy_target_name="dev-sales",
    )
    live_prompt = strategy.render_live_prompt(
        title="Add Sales YoY",
        deploy_target_name="dev-sales",
        scenarios=[{"name": "Baseline", "kind": "dax_assertion", "expected": {"value": 1}}],
    )

    assert strategy.has_live_qa is True
    assert "DAX linting" in static_prompt
    assert "dev-sales" in live_prompt
    assert "Baseline" in live_prompt


def test_generic_strategy_renders_single_pass_prompt() -> None:
    strategy = get_strategy("generic")

    prompt = strategy.render_static_prompt(
        title="Fix API bug",
        files=["src/api.py"],
        acceptance_criteria="Regression test passes",
        deploy_target_name=None,
    )

    assert strategy.has_live_qa is False
    assert "lint, typecheck, unit tests, and build" in prompt
    assert "Deploy target" not in prompt
    with pytest.raises(RuntimeError, match="Generic QA"):
        strategy.render_live_prompt(title="Fix API bug", deploy_target_name=None, scenarios=[])
