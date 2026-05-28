"""Install the bi-orchestrator MCP server entry into the user's Cursor config.

Idempotently adds an ``mcpServers["bi-orchestrator"]`` block to
``~/.cursor/mcp.json``. The command path is derived from the current Python
interpreter so the entry points at the venv we are actually running in.

Run via:

    bi-orchestrator install-mcp                    # adds ~/.cursor/mcp.json entry
    bi-orchestrator install-mcp --skill            # also installs the chat skill
    bi-orchestrator install-mcp --dry-run          # prints what would change
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("bi_orchestrator.install")


USER_MCP_PATH = Path.home() / ".cursor" / "mcp.json"
SKILLS_DIR = Path.home() / ".cursor" / "skills" / "bi-orchestrator"
PACKAGED_SKILL = Path(__file__).resolve().parent.parent / "skills" / "bi-orchestrator"


def _venv_console_script(name: str) -> Path | None:
    """Find ``Scripts/<name>.exe`` (Windows) or ``bin/<name>`` (POSIX) in the same
    venv as the running interpreter."""
    candidates: list[Path] = []
    scripts_dir = Path(sys.executable).resolve().parent
    for stem in (name, f"{name}.exe"):
        candidates.append(scripts_dir / stem)
    via_which = shutil.which(name)
    if via_which:
        candidates.append(Path(via_which))
    for c in candidates:
        if c.is_file():
            return c.resolve()
    return None


def _build_entry(server_name: str) -> dict[str, Any]:
    exe = _venv_console_script("bi-orchestrator-mcp")
    if exe is not None:
        return {"command": str(exe)}
    # Fallback: spawn the current Python with the module entry point. Works in any
    # environment but pays a small import overhead per chat.
    return {
        "command": str(Path(sys.executable).resolve()),
        "args": ["-m", "bi_orchestrator.mcp_server"],
    }


def install_mcp(
    *,
    server_name: str = "bi-orchestrator",
    dry_run: bool = False,
) -> tuple[Path, dict[str, Any]]:
    """Add (or update) the bi-orchestrator entry in ~/.cursor/mcp.json.

    Returns the (path, full_config_dict) tuple. Raises if the existing file is
    not valid JSON — never overwrites unrelated user data silently.
    """
    USER_MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if USER_MCP_PATH.exists():
        # Tolerate BOM-prefixed files written by Windows editors.
        text = USER_MCP_PATH.read_text(encoding="utf-8-sig").strip()
        config: dict[str, Any] = json.loads(text) if text else {}
        if not isinstance(config, dict):
            raise RuntimeError(
                f"{USER_MCP_PATH} is not a JSON object; refusing to overwrite."
            )
    else:
        config = {}

    servers = config.setdefault("mcpServers", {})
    entry = _build_entry(server_name)
    previous = servers.get(server_name)
    servers[server_name] = entry

    log.info("Target mcp.json: %s", USER_MCP_PATH)
    log.info("Entry:           %s", json.dumps(entry))
    if previous is not None and previous != entry:
        log.info("Replacing previous entry: %s", json.dumps(previous))

    if not dry_run:
        USER_MCP_PATH.write_text(
            json.dumps(config, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        log.info("Wrote %s", USER_MCP_PATH)
    else:
        log.info("--dry-run: not writing")
    return USER_MCP_PATH, config


def install_skill(*, dry_run: bool = False) -> Path | None:
    """Copy the packaged ``bi-orchestrator`` skill to ``~/.cursor/skills/``."""
    if not PACKAGED_SKILL.is_dir():
        log.warning("Packaged skill not found at %s; nothing to install.", PACKAGED_SKILL)
        return None
    log.info("Source skill: %s", PACKAGED_SKILL)
    log.info("Target dir:   %s", SKILLS_DIR)
    if dry_run:
        log.info("--dry-run: not copying")
        return SKILLS_DIR
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    for src in PACKAGED_SKILL.rglob("*"):
        rel = src.relative_to(PACKAGED_SKILL)
        dst = SKILLS_DIR / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    log.info("Skill installed to %s", SKILLS_DIR)
    return SKILLS_DIR
