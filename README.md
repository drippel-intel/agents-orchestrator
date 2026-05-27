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
5. Merges to main once you approve, then drops the per-branch deployed model and removes the worktree.

The orchestrator state lives in SQLite at `~/.bi-orchestrator/state.db`; the orchestrator
daemon and the MCP server are independent processes communicating only through that store.
That makes reboot recovery trivial: the daemon queries running assignments at startup
and re-attaches to live agents via `Agent.resume(...)`.

## Layout

```text
bi_orchestrator/
  config.py            # config loader (TOML), defaults from packaged config.toml
  db.py                # SQLite schema + DAO + reboot-recovery queries (Phase 0b)
  state_machine.py     # assignment lifecycle, transitions, cap enforcement (later)
  worktree.py          # git worktree add/remove + pbi-project.json patcher (Phase 0c)
  orchestrator.py      # main loop, agent launcher, reboot recovery (Phase 0d)
  mcp_server.py        # FastMCP server exposing tools to Cursor chat (Phase 0e)
  agents/
    planner.py         # planner agent invocation (Phase 1)
    developer.py       # developer agent invocation (Phase 0d initially)
    qa.py              # QA agent invocation (Phase 3)
  cli.py               # direct CLI for debugging without the MCP
  __main__.py          # python -m bi_orchestrator
  migrations/          # SQL migrations applied at daemon startup
prompts/               # system prompts for planner / developer / qa
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

## Setup (Windows)

```powershell
# Create the venv outside OneDrive (OneDrive sync can lock venv files).
python -m venv C:\Users\drippel\.bi-orchestrator-venv
C:\Users\drippel\.bi-orchestrator-venv\Scripts\Activate.ps1

# Install in editable mode from the project directory.
pip install -e ".[dev]"

# Verify install.
bi-orchestrator --version
```

The orchestrator daemon and the MCP server are separate entry points:

```powershell
# In one terminal: run the daemon (long-running, drives the state machine).
bi-orchestrator daemon

# The MCP server is started by Cursor on demand once you add it to
# ~/.cursor/mcp.json — you do not normally launch it yourself.
```

## Status

**Phase 0 complete** (scaffold + SQLite DAO + worktree manager + smoke orchestrator +
MCP server + chat skill). 11 tests pass under `tests/`. Next phase: planner agent
+ plan approval (Phase 1).

See [`c:\Users\drippel\.cursor\plans\bi_orchestrator_service_048c41cc.plan.md`](c:\Users\drippel\.cursor\plans\bi_orchestrator_service_048c41cc.plan.md)
for the full plan, the phase breakdown, and the current todo statuses.

## Continuing this work in a new chat

The plan file, the source code, and the tests are all on disk — none of that
depends on a particular chat session being open. To pick up after closing this
chat:

1. Open a new Cursor chat with this folder as the workspace root
   (`C:\Users\drippel\OneDrive - Intel Corporation\Documents\Technical\agents-orchestrator`).
2. Point the new agent at the plan file:

   > "Continue the BI orchestrator plan at
   > `c:\Users\drippel\.cursor\plans\bi_orchestrator_service_048c41cc.plan.md`.
   > Phase 0 is complete; start Phase 1 (planner agent + plan approval)."

3. The new agent reads the plan (which has accurate completed/pending todo
   statuses), reads this README, glances at `bi_orchestrator/` and `tests/`,
   and resumes.

What is **not** preserved automatically: the chat transcript-level reasoning
(why we chose SQLite over Postgres, what we considered for cloud agents and
ruled out, etc.). Those decisions are baked into the plan's body sections, so a
future agent that reads the plan inherits the conclusions but not every step of
the deliberation.
