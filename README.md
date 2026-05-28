# bi-orchestrator

Local Python orchestration service that drives parallel Cursor SDK agents across
git worktrees of Power BI repositories using the **aim-pbi-dev** toolchain
(`intel-pbi-dev` / `intel-databricks-dev` / `intel-support-triage` MCPs and the
`.cursor/skills/powerbi-dev` skill).

Each pipeline:

1. Decomposes a batch of requirements into independent assignments via a **planner agent** that you approve.
2. Fans out **developer agents** in parallel, one per assignment, each running locally on its own git worktree with a unique per-branch deploy target.
3. Runs a **QA agent** on each branch when development finishes; failures resume the developer with a structured report (capped iterations).
4. Notifies you via the **orchestrator MCP** when a branch is ready to validate.
5. Merges to main once you approve, then removes the assignment worktree.

The orchestrator state lives in SQLite at `~/.bi-orchestrator/state.db`; the orchestrator
daemon and the MCP server are independent processes communicating only through that store.
That keeps Cursor's chat-scoped MCP process thin while the daemon owns long-running work.

## Layout

```text
bi_orchestrator/
  config.py            # config loader (TOML), defaults from packaged config.toml
  db.py                # SQLite schema + DAO + events / notifications
  state_machine.py     # assignment scheduling helpers, caps, file-conflict checks
  worktree.py          # git worktree add/remove + pbi-project.json patcher
  orchestrator.py      # smoke, planner, daemon scheduler, dev/QA loops
  mcp_server.py        # FastMCP server exposing tools to Cursor chat
  agents/
    planner.py         # planner agent invocation + plan normalization
    developer.py       # developer agent create/resume wrappers
    qa.py              # static/live QA agent invocation + report parsing
  prompts/             # planner / developer / static QA / live QA prompts
  __main__.py          # python -m bi_orchestrator
  migrations/          # SQL migrations applied at daemon startup
config.toml            # packaged default config
```

User-level installation footprint:

```text
~/.cursor/mcp.json                       # adds the bi-orchestrator MCP entry
~/.cursor/skills/bi-orchestrator/        # ergonomic chat trigger
~/.bi-orchestrator/state.db              # SQLite store
~/.bi-orchestrator/logs/                 # per-pipeline log files
~/.bi-orchestrator/config.toml           # optional per-user override of defaults
```

## Setup (Windows — recommended)

```powershell
# From the project root. Installs everything by default.
.\scripts\install.ps1
```

This single command:

- Verifies Python 3.10+ is on PATH.
- Creates `~\.bi-orchestrator-venv` (override with `-VenvPath`). The venv lives
  outside OneDrive on purpose — OneDrive sync occasionally locks files and
  collides with `pip` and venv launches.
- Installs the project in editable mode.
- Registers the MCP server in `~\.cursor\mcp.json`.
- Installs the chat skill in `~\.cursor\skills\bi-orchestrator\`.
- Checks `CURSOR_API_KEY`. If it is not set on this machine, prompts you to
  paste a key (input is masked) and persists it via `setx`. The current shell
  is also updated so subsequent commands see it.

Opt out of any default with the corresponding switch:

```powershell
.\scripts\install.ps1 -SkipMcp -SkipSkill -SkipApiKeyPrompt
.\scripts\install.ps1 -VenvPath D:\envs\bi-orch-venv
.\scripts\install.ps1 -ApiKey 'crsr_...'        # non-interactive key
```

If you do not have a key yet, mint one at
[Cursor Dashboard → Cloud Agents → User API Keys](https://cursor.com/dashboard/cloud-agents)
("New API Key") and paste it when the installer asks.

## Setup (Linux / macOS)

```bash
./scripts/install.sh
```

Same defaults (install everything, prompt for the key if absent). Opt out with
`--skip-mcp`, `--skip-skill`, `--skip-api-key-prompt`. The Linux installer
writes a profile snippet at `~/.bi-orchestrator/env.sh` instead of using
`setx`; source it from your shell rc to make the key persistent.

Override the venv location with `BI_ORCHESTRATOR_VENV=/opt/bi-orch`. BI
execution itself (Tabular Editor, Power BI Desktop, Intel on-prem SSAS) is
Windows-only — the Linux installer is useful for CI smoke tests of the
orchestrator code or for running the daemon on a host that drives only cloud
BI assets.

## Manual setup (what the installer does, for reference)

```powershell
# 1. Create a venv on local disk (NOT inside OneDrive).
python -m venv C:\Users\drippel\.bi-orchestrator-venv

# 2. Install the project from this folder, in editable mode.
C:\Users\drippel\.bi-orchestrator-venv\Scripts\python.exe -m pip install --upgrade pip
C:\Users\drippel\.bi-orchestrator-venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3. Register the MCP server + chat skill with Cursor.
C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe install-mcp --skill
```

Editable mode (`-e`) means the venv points at this source folder — edits take
effect on the next command, no reinstall needed. For "ship to a host without
source on disk" build a wheel with `python -m build` and `pip install` the
resulting `.whl` instead.

## Day-to-day entry points

```powershell
# CLI (smoke, plan, daemon, status, install-mcp)
C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe

# MCP server — Cursor spawns this; you do not normally run it yourself.
C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator-mcp.exe

# Run the daemon (long-running, drives approved assignments).
bi-orchestrator daemon

# Run exactly one scheduler tick for debugging/tests.
bi-orchestrator daemon --once
```

## Pipeline workflow

1. Start a pipeline from Cursor chat with `start_pipeline(requirements, target_repo_path, base_branch?, notes?)`.
2. Generate the planner draft from the CLI:

   ```powershell
   C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe plan <pipeline_id>
   ```

3. Review the draft with `show_plan(pipeline_id)`. Use `edit_plan(pipeline_id, patch_json, replace?)` if needed.
4. Approve it with `approve_plan(pipeline_id)`. Approval materializes assignment rows and checks for overlapping file scopes.
5. Run `bi-orchestrator daemon` to fan out developer agents. Each ready assignment gets a sibling worktree and a per-branch deploy target.
6. The daemon runs static QA, then live QA. Failures resume the developer agent with a structured report until the QA iteration cap is reached.
7. When QA passes, use `pending_validations()` and `submit_validation(assignment_id, approved, feedback?)`.
8. After validation approval and once a PR number is recorded on the assignment, use `merge_assignment(assignment_id)` to squash-merge and clean up the worktree.

Useful inspection tools:

- `get_status(pipeline_id?)`
- `list_recent_events(assignment_id?, pipeline_id?, limit?)`
- `pending_notifications()`
- `acknowledge_notification(notification_id)`

## Moving to a new machine

Three pieces are per-machine and need to be set up each time:

1. **Source code** on disk. Easiest options:
   - OneDrive sync (you already have this — the `agents-orchestrator` folder
     appears automatically on any Intel machine you sign into).
   - Git clone, once we push to innersource.
   - Built wheel: `python -m build`, then `pip install bi_orchestrator-*.whl`
     on the target (no source folder required).
2. **Venv + install**: run `scripts\install.ps1` (or `scripts/install.sh`).
3. **`CURSOR_API_KEY`** env var: `setx CURSOR_API_KEY "..."` on Windows or
   `export CURSOR_API_KEY=...` in your shell profile elsewhere. The same key
   is valid on any machine; you mint it once.

Things that *do* carry across machines automatically:

- The packaged `config.toml`, the SQL migrations, the agent prompts, and the
  chat skill — all are part of the source tree.
- The SQLite state store at `~/.bi-orchestrator/state.db` is local to each
  machine (so each machine has its own pipeline history). If you want a single
  history across machines, point `paths.state_db` in
  `~/.bi-orchestrator/config.toml` at a synced location.

## Status

**Phases 0-5 complete**:

- Phase 0: scaffold, SQLite DAO, worktree manager, smoke orchestrator, MCP server, chat skill.
- Phase 1: planner agent, plan review/edit/approval.
- Phase 2: daemon fan-out, disjoint-file enforcement, parallel cap.
- Phase 3: static QA agent and capped dev-resume loop.
- Phase 4: live QA against per-branch deploy targets and scenarios.
- Phase 5: human validation, merge, notifications, and worktree cleanup.

Current test gate: `28 passed` under `tests/`, plus `ruff check .`.

See [`.cursor/plans/bi_orchestrator_service.plan.md`](.cursor/plans/bi_orchestrator_service.plan.md)
for the full plan, the phase breakdown, and the current todo statuses.

## Continuing this work in a new chat

The plan file, the source code, and the tests are all on disk — none of that
depends on a particular chat session being open. To pick up after closing this
chat:

1. Open a new Cursor chat with this folder as the workspace root
   (`C:\Users\drippel\OneDrive - Intel Corporation\Documents\Technical\agents-orchestrator`).
2. Point the new agent at the plan file:

   > "Continue the BI orchestrator plan at
   > `.cursor/plans/bi_orchestrator_service.plan.md`.
   > Phases 0-5 are complete; review the current code and continue from the next requested change."

3. The new agent reads the plan (which has accurate completed/pending todo
   statuses), reads this README, glances at `bi_orchestrator/` and `tests/`,
   and resumes.

What is **not** preserved automatically: the chat transcript-level reasoning
(why we chose SQLite over Postgres, what we considered for cloud agents and
ruled out, etc.). Those decisions are baked into the plan's body sections, so a
future agent that reads the plan inherits the conclusions but not every step of
the deliberation.
