"""Entry point for ``python -m bi_orchestrator`` and the ``bi-orchestrator`` script.

Phase 0a: argument parsing skeleton only. Subcommands fill in as later phases land.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .logging_setup import configure_logging


log = logging.getLogger("bi_orchestrator")


def _cmd_daemon(_args: argparse.Namespace) -> int:
    log.info("daemon subcommand: not implemented yet (Phase 2+)")
    log.info("config loaded successfully; scaffold and DB are wired up")
    return 0


def _cmd_smoke(args: argparse.Namespace, config: Config) -> int:
    from .orchestrator import cli_smoke
    return cli_smoke(config, Path(args.target_repo), cleanup=args.cleanup)


def _cmd_status(_args: argparse.Namespace, config: Config) -> int:
    from . import db

    conn = db.connect(config.paths.state_db)
    try:
        pipelines = db.list_pipelines(conn)
        if not pipelines:
            log.info("No pipelines recorded yet.")
            return 0
        for p in pipelines:
            log.info(
                "%s  %s  repo=%s  status=%s",
                p["id"], p["created_at"], p["target_repo_path"], p["status"],
            )
            assignments = db.list_assignments_for_pipeline(conn, p["id"])
            for a in assignments:
                log.info(
                    "  %s  %s  branch=%s  status=%s  dev_agent=%s",
                    a["id"], a["title"], a["branch"], a["status"], a["dev_agent_id"],
                )
        return 0
    finally:
        conn.close()


def _cmd_install_mcp(args: argparse.Namespace, _config: Config) -> int:
    from .install import install_mcp, install_skill

    install_mcp(dry_run=args.dry_run)
    if args.skill:
        install_skill(dry_run=args.dry_run)
    if not args.dry_run:
        log.info(
            "Restart Cursor (or any active chat) for the new MCP server to be picked up."
        )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bi-orchestrator")
    parser.add_argument("--version", action="version", version=f"bi-orchestrator {__version__}")
    parser.add_argument("--config", type=str, default=None, help="path to an extra config TOML")
    parser.add_argument("--debug", action="store_true", help="enable debug logging")

    sub = parser.add_subparsers(dest="cmd", required=False)

    p_daemon = sub.add_parser("daemon", help="run the orchestrator daemon (long-running)")
    p_daemon.set_defaults(func=_cmd_daemon)

    p_smoke = sub.add_parser("smoke", help="Phase 0 smoke test: launch one dev agent on a worktree")
    p_smoke.add_argument(
        "--target-repo",
        required=True,
        help="absolute path to the target BI repo (e.g. Q:\\BI\\Users\\Dudi\\Developments\\qov2)",
    )
    p_smoke.add_argument(
        "--cleanup",
        action="store_true",
        help="on a clean finish, tear down the worktree and delete the branch",
    )
    p_smoke.set_defaults(func=_cmd_smoke)

    p_status = sub.add_parser("status", help="show pipeline / assignment status")
    p_status.set_defaults(func=_cmd_status)

    p_install = sub.add_parser(
        "install-mcp",
        help="add the bi-orchestrator MCP server entry to ~/.cursor/mcp.json",
    )
    p_install.add_argument(
        "--skill", action="store_true", help="also install the chat skill"
    )
    p_install.add_argument(
        "--dry-run", action="store_true", help="show what would change without writing"
    )
    p_install.set_defaults(func=_cmd_install_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd is None:
        parser.print_help()
        return 0

    extra = Path(args.config) if args.config else None
    config = load_config(extra_path=extra)
    configure_logging(
        config.paths.logs_dir,
        process_name="bi-orchestrator",
        level=logging.DEBUG if args.debug else logging.INFO,
    )
    log.debug("loaded config: %s", config.model_dump())

    # Subcommands declare whether they need the parsed config object.
    if args.func is _cmd_daemon:
        return _cmd_daemon(args)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
