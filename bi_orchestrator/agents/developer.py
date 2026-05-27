"""Developer agent invocation.

Phase 0d ships the simplest possible shape: synchronous ``Agent.create`` +
``agent.send`` + ``run.wait``, streaming assistant text to the console as it
arrives.

Later phases:
- Pass MCP servers / setting sources so the agent inherits the worktree's
  aim-pbi-dev tooling.
- Add ``Agent.resume`` support for the QA -> dev feedback loop.
- Persist agent_id immediately after ``send()`` so a crashed daemon can re-attach.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from cursor_sdk import Agent, CursorAgentError, LocalAgentOptions, RunResult


log = logging.getLogger("bi_orchestrator.agents.developer")


@dataclass
class DevRunOutcome:
    """Result of one developer agent run for the orchestrator state machine."""
    agent_id: str | None
    run_id: str | None
    status: str               # "finished" | "error" | "cancelled" | "startup_failed"
    final_text: str | None
    error_message: str | None
    cost_usd: float | None


def _result_status(result: RunResult | None) -> str:
    if result is None:
        return "startup_failed"
    status = getattr(result, "status", None)
    return str(status) if status is not None else "unknown"


def _extract_cost(result: RunResult | None) -> float | None:
    if result is None:
        return None
    for attr in ("cost_usd", "usage", "totals"):
        value = getattr(result, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for k in ("cost_usd", "totalCostUsd", "cost"):
                if k in value and isinstance(value[k], (int, float)):
                    return float(value[k])
    return None


def run_developer_once(
    *,
    api_key: str,
    model: str,
    worktree_path: Path,
    prompt: str,
    stream_to_console: bool = True,
) -> DevRunOutcome:
    """Run one developer agent end-to-end against ``worktree_path``.

    Returns a ``DevRunOutcome`` capturing the IDs, status, and any cost so the
    caller can persist them in the SQLite store. Distinguishes startup failures
    (no run happened) from run failures (run executed and finished with status
    "error") as the SDK guidance prescribes.
    """
    log.info("Launching developer agent: model=%s cwd=%s", model, worktree_path)

    final_chunks: list[str] = []
    agent_id: str | None = None
    run_id: str | None = None
    result: RunResult | None = None

    try:
        with Agent.create(
            model=model,
            api_key=api_key,
            local=LocalAgentOptions(cwd=str(worktree_path)),
        ) as agent:
            agent_id = getattr(agent, "agent_id", None) or getattr(agent, "agentId", None)
            log.info("Agent created: agent_id=%s", agent_id)

            run = agent.send(prompt)
            run_id = getattr(run, "id", None) or getattr(run, "run_id", None)
            log.info("Run started: run_id=%s", run_id)

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
        log.error(
            "Developer agent startup failed: %s (retryable=%s)",
            err, getattr(err, "is_retryable", None),
        )
        return DevRunOutcome(
            agent_id=agent_id,
            run_id=run_id,
            status="startup_failed",
            final_text=None,
            error_message=str(err),
            cost_usd=None,
        )

    status = _result_status(result)
    error_message: str | None = None
    if status == "error":
        error_message = getattr(result, "error", None) or getattr(result, "message", None)
        if not isinstance(error_message, str):
            error_message = repr(error_message) if error_message else None

    return DevRunOutcome(
        agent_id=agent_id,
        run_id=run_id,
        status=status,
        final_text="".join(final_chunks) if final_chunks else None,
        error_message=error_message,
        cost_usd=_extract_cost(result),
    )
