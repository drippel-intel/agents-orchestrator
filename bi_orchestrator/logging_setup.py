"""Centralized logging configuration.

Two sinks: a rotating per-process log file under ``paths.logs_dir`` and a console
handler at INFO. Phase 0 keeps it simple; we add per-pipeline log files later.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path


_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def configure_logging(logs_dir: Path, process_name: str, level: int = logging.INFO) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"{process_name}.log"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)
