"""Planner agent invocation and plan normalization.

Phase 1 keeps planning separate from execution: the planner emits a structured
JSON plan, the MCP exposes it for approval/editing, and only approved plans
become assignment rows for later daemon phases.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from ..config import Config
from ..worktree import slugify_branch

log = logging.getLogger("bi_orchestrator.agents.planner")

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "planner.md"


class PlannerPlanError(ValueError):
    """Raised when planner output cannot be parsed or validated."""


class PlanScenario(BaseModel):
    name: str
    kind: str
    expected: dict[str, Any] = Field(default_factory=dict)


class PlannerAssignment(BaseModel):
    title: str
    slug: str | None = None
    branch: str | None = None
    deploy_target_name: str | None = None
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: str | list[str] | None = None
    scenarios: list[PlanScenario] = Field(default_factory=list)

    @field_validator("files", "depends_on", mode="before")
    @classmethod
    def _coerce_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        raise TypeError("expected a string or list of strings")


class PlannerPlan(BaseModel):
    summary: str = ""
    assignments: list[PlannerAssignment]
    notes: str | None = None

    @model_validator(mode="after")
    def _require_assignments(self) -> PlannerPlan:
        if not self.assignments:
            raise ValueError("planner plan must contain at least one assignment")
        return self


@dataclass
class PlannerRunOutcome:
    agent_id: str | None
    run_id: str | None
    status: str
    final_text: str | None
    plan: dict[str, Any] | None
    error_message: str | None
    cost_usd: float | None


def extract_plan_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from raw planner text.

    The prompt asks for JSON only, but accepting a fenced JSON block makes the
    parser tolerant of common assistant formatting without weakening validation.
    """
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise PlannerPlanError("planner output did not contain a JSON object")
        candidate = stripped[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as err:
        raise PlannerPlanError(f"planner output was not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise PlannerPlanError("planner output JSON must be an object")
    return parsed


def normalize_plan(raw_plan: dict[str, Any], *, pipeline_id: str, config: Config) -> dict[str, Any]:
    """Validate planner JSON and fill deterministic branch/deploy defaults."""
    try:
        plan = PlannerPlan.model_validate(raw_plan)
    except ValidationError as err:
        raise PlannerPlanError(str(err)) from err

    used_slugs: set[str] = set()
    normalized_assignments: list[dict[str, Any]] = []
    for index, assignment in enumerate(plan.assignments, start=1):
        base_slug = assignment.slug or assignment.branch or assignment.title
        slug = _dedupe_slug(slugify_branch(base_slug), used_slugs)
        branch = assignment.branch or config.git.branch_pattern.format(
            pipeline=pipeline_id,
            slug=slug,
        )
        deploy_target_name = assignment.deploy_target_name or config.deploy_target.pattern.format(
            base="dev",
            slug=slugify_branch(branch),
        )
        normalized_assignments.append(
            {
                "title": assignment.title,
                "slug": slug,
                "branch": branch,
                "deploy_target_name": deploy_target_name,
                "files": assignment.files,
                "depends_on": assignment.depends_on,
                "acceptance_criteria": _format_acceptance_criteria(
                    assignment.acceptance_criteria
                ),
                "scenarios": [scenario.model_dump(mode="json") for scenario in assignment.scenarios],
                "order": index,
            }
        )

    return {
        "summary": plan.summary,
        "assignments": normalized_assignments,
        "notes": plan.notes,
    }


def parse_planner_response(text: str, *, pipeline_id: str, config: Config) -> dict[str, Any]:
    return normalize_plan(extract_plan_json(text), pipeline_id=pipeline_id, config=config)


def load_planner_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def render_planner_prompt(
    *,
    requirements: str,
    target_repo_path: str,
    base_branch: str,
    pipeline_id: str,
    config: Config,
) -> str:
    template = load_planner_prompt()
    return template.format(
        requirements=requirements,
        target_repo_path=target_repo_path,
        base_branch=base_branch,
        pipeline_id=pipeline_id,
        branch_pattern=config.git.branch_pattern,
        deploy_target_pattern=config.deploy_target.pattern,
    )


def run_planner_once(
    *,
    api_key: str,
    model: str,
    target_repo_path: Path,
    prompt: str,
    pipeline_id: str,
    config: Config,
    stream_to_console: bool = True,
) -> PlannerRunOutcome:
    """Run the planner agent once and parse its structured plan."""
    from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions

    final_chunks: list[str] = []
    agent_id: str | None = None
    run_id: str | None = None
    result = None

    try:
        with Agent.create(
            model=model,
            api_key=api_key,
            local=LocalAgentOptions(cwd=str(target_repo_path)),
        ) as agent:
            agent_id = getattr(agent, "agent_id", None) or getattr(agent, "agentId", None)
            run = agent.send(prompt)
            run_id = getattr(run, "id", None) or getattr(run, "run_id", None)
            for message in run.messages():
                if getattr(message, "type", None) != "assistant":
                    continue
                msg = getattr(message, "message", None) or message
                content = getattr(msg, "content", None) or []
                for block in content:
                    if getattr(block, "type", None) == "text":
                        text = getattr(block, "text", "") or ""
                        final_chunks.append(text)
                        if stream_to_console:
                            sys.stdout.write(text)
                            sys.stdout.flush()
            result = run.wait()
            if stream_to_console:
                sys.stdout.write("\n")
                sys.stdout.flush()
    except CursorAgentError as err:
        log.error("Planner agent startup failed: %s", err)
        return PlannerRunOutcome(
            agent_id=agent_id,
            run_id=run_id,
            status="startup_failed",
            final_text=None,
            plan=None,
            error_message=str(err),
            cost_usd=None,
        )

    status = str(getattr(result, "status", None) or "unknown")
    final_text = "".join(final_chunks) if final_chunks else None
    plan: dict[str, Any] | None = None
    error_message: str | None = None
    if status == "finished" and final_text:
        try:
            plan = parse_planner_response(final_text, pipeline_id=pipeline_id, config=config)
        except PlannerPlanError as err:
            status = "error"
            error_message = str(err)
    elif status == "error":
        error = getattr(result, "error", None) or getattr(result, "message", None)
        error_message = error if isinstance(error, str) else repr(error) if error else None

    return PlannerRunOutcome(
        agent_id=agent_id,
        run_id=run_id,
        status=status,
        final_text=final_text,
        plan=plan,
        error_message=error_message,
        cost_usd=_extract_cost(result),
    )


def _dedupe_slug(slug: str, used_slugs: set[str]) -> str:
    if slug not in used_slugs:
        used_slugs.add(slug)
        return slug
    i = 2
    while f"{slug}_{i}" in used_slugs:
        i += 1
    deduped = f"{slug}_{i}"
    used_slugs.add(deduped)
    return deduped


def _format_acceptance_criteria(value: str | list[str] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return "\n".join(f"- {item}" for item in value)


def _extract_cost(result: Any) -> float | None:
    if result is None:
        return None
    for attr in ("cost_usd", "usage", "totals"):
        value = getattr(result, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("cost_usd", "totalCostUsd", "cost"):
                if key in value and isinstance(value[key], (int, float)):
                    return float(value[key])
    return None
