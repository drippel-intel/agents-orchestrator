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
# CLI (smoke, status, install-mcp, daemon)
C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator.exe

# MCP server — Cursor spawns this; you do not normally run it yourself.
C:\Users\drippel\.bi-orchestrator-venv\Scripts\bi-orchestrator-mcp.exe

# Run the daemon (long-running, drives the state machine). Phase 2+.
bi-orchestrator daemon
```

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
