# agents-orchestrator

Local Python orchestration service that drives parallel Cursor SDK agents across
git worktrees for both standard software-development repos and BI repos that use
the **aim-pbi-dev** toolchain.

Each pipeline:

1. Decomposes a batch of requirements into independent assignments via a **planner agent** that you approve.
2. Fans out **developer agents** in parallel, one per assignment, each running locally on its own git worktree.
3. Runs a kind-aware **QA agent** on each branch when development finishes; failures resume the developer with a structured report (capped iterations).
4. Notifies you via the **orchestrator MCP** when a branch is ready to validate.
5. Merges to main once you approve, then removes the assignment worktree.

The orchestrator state lives in SQLite at `~/.agents-orchestrator/state.db`; the orchestrator
daemon and the MCP server are independent processes communicating only through that store.
That keeps Cursor's chat-scoped MCP process thin while the daemon owns long-running work.

This project targets Windows only.

## Layout

```text
agents_orchestrator/
  config.py            # config loader (TOML), defaults from packaged config.toml
  db.py                # SQLite schema + DAO + events / notifications
  state_machine.py     # assignment scheduling helpers, caps, file-conflict checks
  worktree.py          # git worktree add/remove
  bi_provisioning.py   # BI-only pbi-project.json target patcher
  orchestrator.py      # smoke, planner, daemon scheduler, dev/QA loops
  mcp_server.py        # FastMCP server exposing tools to Cursor chat
  agents/
    planner.py         # planner agent invocation + plan normalization
    developer.py       # developer agent create/resume wrappers
    qa.py              # QA agent invocation + report parsing
  qa_strategies/       # BI and generic QA prompt strategies
  prompts/             # kind-specific planner / developer / QA prompts
  __main__.py          # python -m agents_orchestrator
  migrations/          # SQL migrations applied at daemon startup
config.toml            # packaged default config
```

User-level installation footprint:

```text
~/.cursor/mcp.json                       # adds the agents-orchestrator MCP entry
~/.cursor/skills/agents-orchestrator/        # ergonomic chat trigger
~/.agents-orchestrator/state.db              # SQLite store
~/.agents-orchestrator/logs/                 # per-pipeline log files
~/.agents-orchestrator/config.toml           # optional per-user override of defaults
```

## Setup (Windows)

```powershell
# From the project root. Installs everything by default.
.\scripts\install.ps1
```

This single command:

- Verifies Python 3.10+ is on PATH.
- Creates `~\.agents-orchestrator-venv` (override with `-VenvPath`). The venv lives
  outside OneDrive on purpose — OneDrive sync occasionally locks files and
  collides with `pip` and venv launches.
- Installs the project in editable mode.
- Registers the MCP server in `~\.cursor\mcp.json`.
- Installs the chat skill in `~\.cursor\skills\agents-orchestrator\`.
- Checks `CURSOR_API_KEY`. If it is not set on this machine, prompts you to
  paste a key (input is masked) and persists it via `setx`. The current shell
  is also updated so subsequent commands see it.

Opt out of any default with the corresponding switch:

```powershell
.\scripts\install.ps1 -SkipMcp -SkipSkill -SkipApiKeyPrompt
.\scripts\install.ps1 -VenvPath D:\envs\agents-orch-venv
.\scripts\install.ps1 -ApiKey 'crsr_...'        # non-interactive key
```

If you do not have a key yet, mint one at
[Cursor Dashboard → Cloud Agents → User API Keys](https://cursor.com/dashboard/cloud-agents)
("New API Key") and paste it when the installer asks.

## Manual setup (equivalent to the installer)

```powershell
# 1. Create a venv on local disk (NOT inside OneDrive).
python -m venv C:\Users\drippel\.agents-orchestrator-venv

# 2. Install the project from this folder, in editable mode.
C:\Users\drippel\.agents-orchestrator-venv\Scripts\python.exe -m pip install --upgrade pip
C:\Users\drippel\.agents-orchestrator-venv\Scripts\python.exe -m pip install -e ".[dev]"

# 3. Register the MCP server + chat skill with Cursor.
C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator.exe install-mcp --skill
```

Editable mode (`-e`) means the venv points at this source folder — edits take
effect on the next command, no reinstall needed. For "ship to a host without
source on disk" build a wheel with `python -m build` and `pip install` the
resulting `.whl` instead.

## Day-to-day entry points

```powershell
# CLI (smoke, plan, daemon, status, install-mcp)
C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator.exe

# MCP server — Cursor spawns this; you do not normally run it yourself.
C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator-mcp.exe

# Run the daemon (long-running, drives approved assignments).
agents-orchestrator daemon

# Run exactly one scheduler tick for debugging/tests.
agents-orchestrator daemon --once
```

## Pipeline workflow

1. Start a pipeline from Cursor chat with `start_pipeline(requirements, target_repo_path, base_branch?, notes?, kind?)`. `kind` defaults to `auto`.
2. Generate the planner draft from the CLI:

   ```powershell
   C:\Users\drippel\.agents-orchestrator-venv\Scripts\agents-orchestrator.exe plan <pipeline_id>
   ```

3. Review the draft with `show_plan(pipeline_id)`. Use `edit_plan(pipeline_id, patch_json, replace?)` if needed.
4. Approve it with `approve_plan(pipeline_id)`. Approval materializes assignment rows and checks for overlapping file scopes.
5. Run `agents-orchestrator daemon` to fan out developer agents. Each ready assignment gets a sibling worktree.
6. The daemon runs QA. Generic repos run one combined lint/typecheck/test/build QA pass. BI repos run static QA, then live QA against the per-branch deploy target.
7. When QA passes, use `pending_validations()` and `submit_validation(assignment_id, approved, feedback?)`.
8. After validation approval and once a PR number is recorded on the assignment, use `merge_assignment(assignment_id)` to squash-merge and clean up the worktree.

Useful inspection tools:

- `get_status(pipeline_id?)`
- `list_recent_events(assignment_id?, pipeline_id?, limit?)`
- `pending_notifications()`
- `acknowledge_notification(notification_id)`

## Repo kind detection

`start_pipeline(..., kind="auto")` detects BI repos when any of these markers
exist at the target repo root:

- `pbi-project.json`
- `.aim-pbi-dev/`
- `.cursor/skills/powerbi-dev/`
- `qa/measure-baselines.json`

BI pipelines patch `pbi-project.json` in each worktree to create a unique
per-branch deploy target. Generic pipelines leave `deploy_target_name` empty and
skip the live QA state.

## Migrating From `bi-orchestrator`

After installing this renamed package, run:

```powershell
agents-orchestrator migrate-from-bi
```

The command copies the old SQLite state from `~/.bi-orchestrator/`, moves old
logs/config when possible, and rewrites `~/.cursor/mcp.json` from the
`bi-orchestrator` entry to `agents-orchestrator`.

## Moving to a new machine

Three pieces are per-machine and need to be set up each time:

1. **Source code** on disk. Easiest options:
   - OneDrive sync (you already have this — the `agents-orchestrator` folder
     appears automatically on any Intel machine you sign into).
   - Git clone, once we push to innersource.
   - Built wheel: `python -m build`, then `pip install agents_orchestrator-*.whl`
     on the target (no source folder required).
2. **Venv + install**: run `scripts\install.ps1`, or follow the manual setup
   steps above if you do not want to use the helper script.
3. **`CURSOR_API_KEY`** env var: `setx CURSOR_API_KEY "..."`. The same key is
   valid on any machine; you mint it once.

Things that *do* carry across machines automatically:

- The packaged `config.toml`, the SQL migrations, the agent prompts, and the
  chat skill — all are part of the source tree.
- The SQLite state store at `~/.agents-orchestrator/state.db` is local to each
  machine (so each machine has its own pipeline history). If you want a single
  history across machines, point `paths.state_db` in
  `~/.agents-orchestrator/config.toml` at a synced location.

