"""QA agent invocation and structured report parsing."""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("agents_orchestrator.agents.qa")


class QAReportError(ValueError):
    """Raised when QA output cannot be parsed or validated."""


class QAReport(BaseModel):
    passed: bool
    summary: str = ""
    failures: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


@dataclass
class QARunOutcome:
    agent_id: str | None
    run_id: str | None
    status: str
    passed: bool
    report: dict[str, Any] | None
    final_text: str | None
    error_message: str | None
    cost_usd: float | None


def parse_qa_report(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise QAReportError("QA output did not contain a JSON object")
        candidate = stripped[start : end + 1]
    try:
        raw = json.loads(candidate)
    except json.JSONDecodeError as err:
        raise QAReportError(f"QA output was not valid JSON: {err}") from err
    try:
        return QAReport.model_validate(raw).model_dump(mode="json")
    except ValidationError as err:
        raise QAReportError(str(err)) from err


def render_static_qa_prompt(
    *,
    title: str,
    files: list[str],
    acceptance_criteria: str | None,
    deploy_target_name: str | None,
    kind: str = "bi",
) -> str:
    from ..qa_strategies import get_strategy

    return get_strategy(kind).render_static_prompt(
        title=title,
        files=files,
        acceptance_criteria=acceptance_criteria,
        deploy_target_name=deploy_target_name,
    )


def render_live_qa_prompt(
    *,
    title: str,
    deploy_target_name: str | None,
    scenarios: list[dict[str, Any]],
    kind: str = "bi",
) -> str:
    from ..qa_strategies import get_strategy

    return get_strategy(kind).render_live_prompt(
        title=title,
        deploy_target_name=deploy_target_name,
        scenarios=scenarios,
    )


def render_developer_qa_feedback_prompt(report: dict[str, Any]) -> str:
    return (
        "QA found issues. Fix them in the current worktree, keeping the "
        "assignment scope unchanged, then summarize what changed.\n\n"
        f"QA report JSON:\n{json.dumps(report, indent=2)}"
    )


def run_static_qa_once(
    *,
    api_key: str,
    model: str,
    worktree_path: Path,
    prompt: str,
    stream_to_console: bool = True,
) -> QARunOutcome:
    from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions

    final_chunks: list[str] = []
    agent_id: str | None = None
    run_id: str | None = None
    result = None
    try:
        with Agent.create(
            model=model,
            api_key=api_key,
            local=LocalAgentOptions(cwd=str(worktree_path)),
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
        return QARunOutcome(
            agent_id=agent_id,
            run_id=run_id,
            status="startup_failed",
            passed=False,
            report=None,
            final_text=None,
            error_message=str(err),
            cost_usd=None,
        )

    status = str(getattr(result, "status", None) or "unknown")
    final_text = "".join(final_chunks) if final_chunks else None
    report: dict[str, Any] | None = None
    error_message: str | None = None
    passed = False
    if status == "finished" and final_text:
        try:
            report = parse_qa_report(final_text)
            passed = bool(report["passed"])
        except QAReportError as err:
            status = "error"
            error_message = str(err)
    elif status == "error":
        error = getattr(result, "error", None) or getattr(result, "message", None)
        error_message = error if isinstance(error, str) else repr(error) if error else None

    return QARunOutcome(
        agent_id=agent_id,
        run_id=run_id,
        status=status,
        passed=passed,
        report=report,
        final_text=final_text,
        error_message=error_message,
        cost_usd=_extract_cost(result),
    )


def run_live_qa_once(
    *,
    api_key: str,
    model: str,
    worktree_path: Path,
    prompt: str,
    stream_to_console: bool = True,
) -> QARunOutcome:
    return run_static_qa_once(
        api_key=api_key,
        model=model,
        worktree_path=worktree_path,
        prompt=prompt,
        stream_to_console=stream_to_console,
    )


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
