#!/usr/bin/env bash
# One-shot installer for bi-orchestrator on Linux / macOS.
#
# Idempotent — safe to re-run. Default behaviour:
#   1. Verify python3 3.10+ is on PATH.
#   2. Create / reuse a venv (default: ~/.bi-orchestrator-venv,
#      override with BI_ORCHESTRATOR_VENV).
#   3. Install the project from the surrounding source folder in editable mode.
#   4. Register the MCP server in ~/.cursor/mcp.json.
#   5. Install the chat skill in ~/.cursor/skills/bi-orchestrator/.
#   6. Check CURSOR_API_KEY — if absent, prompt and append an export to a
#      profile snippet at ~/.bi-orchestrator/env.sh (the user is asked to
#      source it from their shell rc; we never auto-edit ~/.bashrc).
#
# Opt out of any of those defaults with the corresponding --skip-... flag.
#
# Usage:
#   ./scripts/install.sh                           # full install
#   ./scripts/install.sh --skip-mcp --skip-skill   # venv + pip only
#   ./scripts/install.sh --skip-api-key-prompt
#   CURSOR_API_KEY='crsr_...' ./scripts/install.sh # non-interactive key
#   BI_ORCHESTRATOR_VENV=/opt/bi-orch ./scripts/install.sh
#
# Note: BI work itself is Windows-only (Tabular Editor, Power BI Desktop, on-prem
# SSAS). This script is mainly useful for CI smoke tests of the orchestrator code
# or for running the orchestrator daemon on a Linux host that drives cloud BI
# assets only.

set -euo pipefail

VENV_PATH="${BI_ORCHESTRATOR_VENV:-$HOME/.bi-orchestrator-venv}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SKIP_MCP=0
SKIP_SKILL=0
SKIP_API_KEY_PROMPT=0
SKIP_PIP_UPGRADE=0

for arg in "$@"; do
    case "$arg" in
        --skip-mcp)              SKIP_MCP=1 ;;
        --skip-skill)            SKIP_SKILL=1 ;;
        --skip-api-key-prompt)   SKIP_API_KEY_PROMPT=1 ;;
        --skip-pip-upgrade)      SKIP_PIP_UPGRADE=1 ;;
        -h|--help)
            grep -E '^# ' "$0" | sed 's/^# \{0,1\}//' | head -n 28
            exit 0
            ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

hr() { echo; echo "=== $1 ==="; }

hr "bi-orchestrator install"
echo "Project root : $PROJECT_ROOT"
echo "Venv path    : $VENV_PATH"
echo "Install MCP  : $([ $SKIP_MCP -eq 0 ] && echo true || echo false)"
echo "Install skill: $([ $SKIP_SKILL -eq 0 ] && echo true || echo false)"

# --- 1. Python check ---------------------------------------------------------
hr "Python"
PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
    echo "python3 not found on PATH" >&2
    exit 1
fi
"$PYTHON_BIN" --version
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), f"need Python 3.10+; have {sys.version}"; print("ok")'

# --- 2. Venv -----------------------------------------------------------------
hr "Virtual environment"
if [ -x "$VENV_PATH/bin/python" ]; then
    echo "Venv exists, reusing: $VENV_PATH"
else
    echo "Creating venv at $VENV_PATH ..."
    "$PYTHON_BIN" -m venv "$VENV_PATH"
fi

if [ "$SKIP_PIP_UPGRADE" -eq 0 ]; then
    echo "Upgrading pip..."
    "$VENV_PATH/bin/python" -m pip install --upgrade pip --quiet
fi

# --- 3. Install bi-orchestrator ---------------------------------------------
hr "Install bi-orchestrator (editable mode)"
(
    cd "$PROJECT_ROOT"
    "$VENV_PATH/bin/python" -m pip install -e ".[dev]" --quiet
)
"$VENV_PATH/bin/bi-orchestrator" --version

# --- 4. Register MCP + skill (default on) ------------------------------------
if [ "$SKIP_MCP" -eq 0 ] || [ "$SKIP_SKILL" -eq 0 ]; then
    hr "Register with Cursor"
    if [ "$SKIP_MCP" -eq 0 ] && [ "$SKIP_SKILL" -eq 0 ]; then
        "$VENV_PATH/bin/bi-orchestrator" install-mcp --skill
    elif [ "$SKIP_MCP" -eq 0 ]; then
        "$VENV_PATH/bin/bi-orchestrator" install-mcp
    else
        # Skill only — still goes through the install-mcp subcommand.
        "$VENV_PATH/bin/bi-orchestrator" install-mcp --skill
    fi
    echo "Restart any open Cursor chats so the new MCP / skill is picked up."
else
    hr "MCP / skill registration"
    echo "Skipped (both --skip-mcp and --skip-skill were set)."
fi

# --- 5. API key --------------------------------------------------------------
hr "CURSOR_API_KEY"
ENV_SNIPPET="$HOME/.bi-orchestrator/env.sh"
if [ -n "${CURSOR_API_KEY:-}" ]; then
    echo "Already set in this shell (length ${#CURSOR_API_KEY})."
elif [ "$SKIP_API_KEY_PROMPT" -eq 1 ]; then
    echo "CURSOR_API_KEY is not set and --skip-api-key-prompt was passed."
    echo "Add to your shell profile: export CURSOR_API_KEY='<key>'"
else
    echo "CURSOR_API_KEY is not set."
    echo "Mint one at  https://cursor.com/dashboard/cloud-agents  (User API Keys > New)"
    echo "Paste it now to persist via $ENV_SNIPPET, or press Enter to skip."
    printf "CURSOR_API_KEY: "
    # -s hides input.
    if read -r -s entered_key; then
        echo ""
        # Trim whitespace and surrounding quotes.
        entered_key="${entered_key#\"}"; entered_key="${entered_key%\"}"
        entered_key="${entered_key#\'}"; entered_key="${entered_key%\'}"
        entered_key="$(printf '%s' "$entered_key" | tr -d '[:space:]')"
        if [ -n "$entered_key" ]; then
            mkdir -p "$(dirname "$ENV_SNIPPET")"
            umask 077
            printf 'export CURSOR_API_KEY=%q\n' "$entered_key" > "$ENV_SNIPPET"
            export CURSOR_API_KEY="$entered_key"
            echo "Wrote $ENV_SNIPPET (length ${#entered_key})."
            echo "Add to your shell rc to make it persistent:"
            echo "  echo 'source $ENV_SNIPPET' >> ~/.bashrc      # or ~/.zshrc, etc."
        else
            echo "No key entered. Add to your shell profile later:"
            echo "  export CURSOR_API_KEY='<key>'"
        fi
    fi
fi

# --- 6. Final summary --------------------------------------------------------
hr "Next steps"
cat <<EOF
Try the smoke flow:
  $VENV_PATH/bin/bi-orchestrator smoke --target-repo <absolute-path-to-bi-repo>

Daily entry points in this venv:
  $VENV_PATH/bin/bi-orchestrator       # CLI (smoke, status, install-mcp, daemon)
  $VENV_PATH/bin/bi-orchestrator-mcp   # MCP server (spawned by Cursor)
EOF
