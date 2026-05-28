---
name: bi-orchestrator
description: >-
  Drive the bi-orchestrator pipeline to run parallel autonomous Cursor SDK agents
  across git worktrees of Power BI repos that use the aim-pbi-dev toolchain
  (intel-pbi-dev, intel-databricks-dev, intel-support-triage MCPs and the
  powerbi-dev skill). Use this when the user asks to kick off a pipeline, run a
  batch of BI requirements in parallel, orchestrate agents across multiple
  branches, fan out work, split a set of changes across worktrees, or check the
  status of running pipelines. Use it any time the user mentions the
  bi-orchestrator MCP, the orchestrator skill, the `start_pipeline` /
  `get_status` tools, or refers to "the orchestrator" in a Power BI / aim-pbi-dev
  context. Do not invoke this skill for everyday single-agent BI work (adding a
  measure, fixing one DAX expression, deploying once, running QA on a single
  branch) — the regular powerbi-dev workflow handles that. The orchestrator only
  earns its keep when the user wants to run multiple assignments concurrently or
  needs durable state across chat sessions.
---

# bi-orchestrator

The **bi-orchestrator** is a local Python service plus a custom MCP server that
runs **parallel** Cursor SDK agents on isolated git worktrees of a Power BI repo
(qov2 etc.), each on its own branch, with planner approval, automated QA via the
aim-pbi-dev MCP, per-branch deploy targets, and human validation gating. State
lives in SQLite at `~/.bi-orchestrator/state.db` so the system survives reboots
and the chat does not need to stay open while agents work.

This skill teaches you (the chat agent) how to drive the orchestrator. It does
**not** replace the `powerbi-dev` skill for ordinary one-task BI work.

---

## Prerequisites the user must have in place

Before the orchestrator can do any real work:

1. The bi-orchestrator MCP server is installed in `~/.cursor/mcp.json`. If the
   tools `bi-orchestrator.start_pipeline` / `bi-orchestrator.get_status` are not
   available to you, ask the user to run:

   ```powershell
   C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe install-mcp --skill
   ```

   and restart any open Cursor chats.

2. `CURSOR_API_KEY` is set in the user's environment. The orchestrator runs all
   agents through the Cursor SDK, which authenticates with that key. If it is
   missing, the orchestrator's CLI / smoke command will fail with an explicit
   message and exit code 1.

3. The target BI repo is reachable from the local machine, is a git repository,
   has the standard `.cursor/mcp.json` referencing `intel-pbi-dev` /
   `intel-databricks-dev` / `intel-support-triage`, and (for live QA) a
   `pbi-project.json` with at least a `dev` model target.

---

## What the orchestrator currently does (phase status)

The orchestrator is being built in phases. As of Phase 5:

| Phase | Status | What works |
|------|--------|------------|
| 0 — end-to-end smoke | shipped | `start_pipeline`, `get_status`, `list_recent_events`, `pending_notifications`, `acknowledge_notification` tools; CLI `bi-orchestrator smoke` runs one developer agent on a sibling worktree end-to-end |
| 1 — planner agent + approval | shipped | CLI `bi-orchestrator plan <pipeline_id>` runs the planner; MCP `show_plan`, `edit_plan`, `approve_plan` materialize assignments after approval |
| 2 — fan-out | shipped | CLI `bi-orchestrator daemon` schedules approved assignments across worktrees up to `max_parallel_dev_agents`; `--once` runs one scheduler tick |
| 3 — static QA loop | shipped | QA agent runs static checks after dev; failures resume the developer agent until `max_qa_iterations` |
| 4 — live QA per branch | shipped | live QA deploys, refreshes, runs measure/regression tests against each assignment deploy target, and feeds failures back through the QA loop |
| 5 — validation + auto-merge | shipped | `pending_validations`, `submit_validation`, `merge_assignment`, `gh pr merge`, worktree cleanup |

When the user asks for capabilities that are not yet shipped, tell them what
phase that lands in and offer to run the Phase 0 smoke instead.

---

## Available tools (from the `bi-orchestrator` MCP server)

- **`start_pipeline(requirements, target_repo_path, base_branch?, notes?)`**
  Creates a pipeline row in the SQLite state store with status `planned`.
  Run `bi-orchestrator plan <pipeline_id>` to create the planner draft.
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
   requirements. If the user spoke a path like "qov2" without a full path, infer
   `Q:\BI\Users\Dudi\Developments\qov2`-style based on their machine.
2. Confirm the **base branch** (default `main`).
3. Call `start_pipeline` to record the pipeline. Show the returned `pipeline_id`
   to the user.
4. Run the planner from the installed CLI:

   ```powershell
   C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe plan <pipeline_id>
   ```

5. Call `show_plan(pipeline_id=…)`, review the assignments with the user, then
   call `edit_plan` if needed or `approve_plan` when the draft is acceptable.
6. Run `bi-orchestrator daemon` to fan out approved assignments. Use
   `bi-orchestrator daemon --once` for one deterministic scheduler tick.

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
- **Never** edit the target BI repo directly from this chat. The orchestrator is
  the only path that touches a repo, via spawned dev / QA agents in their own
  worktrees.
- **Never** instruct the user to disable caps to "get something through". Caps
  exist to prevent runaway cost and infinite loops. If a cap was hit, surface
  the audit log and ask the user how to proceed.
- **Never** restart / kill the daemon (when it exists in Phase 2+) without
  user confirmation. Orchestration state is durable in SQLite, but in-flight
  agents are not.
