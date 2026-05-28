---
name: agents-orchestrator
description: >-
  Drive the agents-orchestrator pipeline to run parallel autonomous Cursor SDK agents
  across git worktrees for standard software-development repos and BI repos. Use
  this when the user asks to kick off a pipeline, run a batch of requirements in
  parallel, orchestrate agents across multiple branches, fan out work, split a
  set of changes across worktrees, or check running pipeline status.
---

# agents-orchestrator

The **agents-orchestrator** is a local Python service plus a custom MCP server
that runs **parallel** Cursor SDK agents on isolated git worktrees. It supports
standard software-dev repos with single-pass QA and BI repos with aim-pbi-dev
static/live QA. State lives in SQLite at `~/.agents-orchestrator/state.db` so the
system survives reboots and the chat does not need to stay open while agents work.

---

## Prerequisites the user must have in place

Before the orchestrator can do any real work:

1. The agents-orchestrator MCP server is installed in `~/.cursor/mcp.json`. If the
   tools `agents-orchestrator.start_pipeline` / `agents-orchestrator.get_status` are not
   available to you, ask the user to run:

   ```powershell
   C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator.exe install-mcp --skill
   ```

   and restart any open Cursor chats.

2. `CURSOR_API_KEY` is set in the user's environment. The orchestrator runs all
   agents through the Cursor SDK, which authenticates with that key. If it is
   missing, the orchestrator's CLI / smoke command will fail with an explicit
   message and exit code 1.

3. The target repo is reachable from the local machine and is a git repository.
   BI repos should have their normal aim-pbi-dev files (`pbi-project.json`,
   `.aim-pbi-dev`, or the `powerbi-dev` skill) so auto-detection selects BI QA.

---

## What the orchestrator currently does (phase status)

The orchestrator is being built in phases. As of Phase 5:

| Phase | Status | What works |
|------|--------|------------|
| 0 — end-to-end smoke | shipped | `start_pipeline`, `get_status`, `list_recent_events`, `pending_notifications`, `acknowledge_notification` tools; CLI `agents-orchestrator smoke` runs one developer agent on a sibling worktree end-to-end |
| 1 — planner agent + approval | shipped | CLI `agents-orchestrator plan <pipeline_id>` runs the planner; MCP `show_plan`, `edit_plan`, `approve_plan` materialize assignments after approval |
| 2 — fan-out | shipped | CLI `agents-orchestrator daemon` schedules approved assignments across worktrees up to `max_parallel_dev_agents`; `--once` runs one scheduler tick |
| 3 — QA loop | shipped | Generic repos run one combined lint/typecheck/test/build QA pass; BI repos run static QA first. Failures resume the developer agent until `max_qa_iterations` |
| 4 — BI live QA per branch | shipped | BI live QA deploys, refreshes, runs measure/regression tests against each assignment deploy target, and feeds failures back through the QA loop |
| 5 — validation + auto-merge | shipped | `pending_validations`, `submit_validation`, `merge_assignment`, `gh pr merge`, worktree cleanup |

When the user asks for capabilities that are not yet shipped, tell them what
phase that lands in and offer to run the Phase 0 smoke instead.

---

## Available tools (from the `agents-orchestrator` MCP server)

- **`start_pipeline(requirements, target_repo_path, base_branch?, notes?, kind?)`**
  Creates a pipeline row in the SQLite state store with status `planned`.
  `kind` defaults to `auto`; pass `bi` or `generic` to override detection.
  Run `agents-orchestrator plan <pipeline_id>` to create the planner draft.
- **`show_plan(pipeline_id)`** — return the planner draft for review.
- **`edit_plan(pipeline_id, patch_json, replace?)`** — update the draft before approval.
- **`approve_plan(pipeline_id)`** — approve the draft and create assignment rows.
- **`get_status(pipeline_id?)`** — list pipelines (no args) or drill into one
  with its assignments.
- **`list_recent_events(assignment_id?, pipeline_id?, limit?)`** — audit log for
  debugging "what did this agent do" / "why did this assignment fail".
- **`pending_notifications()`** / **`acknowledge_notification(id)`** — the
  notification queue (validation-needed, cap breaches). MCP-only channel for
  now — there is no Slack / email integration.
- **`pending_validations()`** — list assignments awaiting human validation.
- **`submit_validation(assignment_id, approved, feedback?)`** — approve or send
  validation feedback for another capped dev/QA iteration.
- **`merge_assignment(assignment_id)`** — squash-merge a validation-approved PR
  and clean up its worktree.

---

## When the user asks to kick off a pipeline

Order of operations:

1. Confirm the **target repo path** (absolute) and that you understand the
   requirements.
2. Confirm the **base branch** (default `main`).
3. Call `start_pipeline` to record the pipeline. Prefer `kind="auto"` unless the
   user explicitly wants `bi` or `generic`. Show the returned `pipeline_id` and
   resolved `kind` to the user.
4. Run the planner from the installed CLI:

   ```powershell
   C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator.exe plan <pipeline_id>
   ```

5. Call `show_plan(pipeline_id=…)`, review the assignments with the user, then
   call `edit_plan` if needed or `approve_plan` when the draft is acceptable.
6. Run `agents-orchestrator daemon` to fan out approved assignments. Use
   `agents-orchestrator daemon --once` for one deterministic scheduler tick.

---

## When the user asks for status

Default to `get_status()` (no args) which returns a one-line-per-pipeline
summary. If a pipeline looks interesting, call `get_status(pipeline_id=…)` for
its full assignments listing. Format the response as a small table with columns
"pipeline id, status, repo, created, notes" — do not dump raw JSON unless the
user asks.

For active assignments use `list_recent_events(assignment_id=…, limit=20)` to
explain *what the agents are currently doing*. Do not hammer the tool repeatedly —
the user is reading, not polling.

---

## When the user asks about validation

Call `pending_validations()` to list assignments awaiting review. Use
`submit_validation` to approve or send feedback. After approval, call
`merge_assignment` when the user asks to merge.

---

## Hard rules

- **Never** invent or guess MCP tool names. Only call `start_pipeline`,
  `show_plan`, `edit_plan`, `approve_plan`, `get_status`, `list_recent_events`,
  `pending_notifications`, `acknowledge_notification`, `pending_validations`,
  `submit_validation`, and `merge_assignment`.
- **Never** edit the target repo directly from this chat. The orchestrator is
  the only path that touches a repo, via spawned dev / QA agents in their own
  worktrees.
- **Never** instruct the user to disable caps to "get something through". Caps
  exist to prevent runaway cost and infinite loops. If a cap was hit, surface
  the audit log and ask the user how to proceed.
- **Never** restart / kill the daemon (when it exists in Phase 2+) without
  user confirmation. Orchestration state is durable in SQLite, but in-flight
  agents are not.
