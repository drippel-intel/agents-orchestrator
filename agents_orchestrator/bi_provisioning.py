"""BI-specific worktree provisioning helpers."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("agents_orchestrator.bi_provisioning")


@dataclass
class PbiTargetPatch:
    """Result of patching ``pbi-project.json`` for a per-branch deploy target."""

    new_target_name: str
    model_database: str | None
    report_name: str | None


def patch_pbi_project_targets(
    worktree_path: Path,
    new_target_name: str,
    *,
    base_target_name: str = "dev",
    set_default: bool = True,
    project_file: str = "pbi-project.json",
) -> PbiTargetPatch:
    """Clone model/report dev targets into a uniquely named branch target."""
    path = worktree_path / project_file
    if not path.is_file():
        raise FileNotFoundError(f"No {project_file} in {worktree_path}")

    project: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    model_db: str | None = None
    report_name: str | None = None

    model_targets = (project.setdefault("model", {})).setdefault("targets", {})
    if new_target_name in model_targets:
        log.info("Model target %s already present; not modifying", new_target_name)
        model_db = model_targets[new_target_name].get("database")
    else:
        if base_target_name not in model_targets:
            raise KeyError(
                f"model.targets has no base target '{base_target_name}'; "
                f"cannot derive a per-branch target. Available: {list(model_targets)}"
            )
        base = dict(model_targets[base_target_name])
        if "database" in base:
            base["database"] = f"{base['database']}_{new_target_name}"
            model_db = base["database"]
        model_targets[new_target_name] = base
        if set_default:
            project["model"]["defaultTarget"] = new_target_name
        log.info(
            "Patched model.targets: added '%s' with database='%s'",
            new_target_name,
            model_db,
        )

    report_targets = (project.setdefault("report", {})).setdefault("targets", {})
    if new_target_name in report_targets:
        log.info("Report target %s already present; not modifying", new_target_name)
        report_name = report_targets[new_target_name].get("reportName")
    elif base_target_name not in report_targets:
        log.info(
            "report.targets has no base target '%s'; skipping report patch.",
            base_target_name,
        )
    else:
        base = dict(report_targets[base_target_name])
        if "reportName" in base:
            base["reportName"] = f"{base['reportName']} [{new_target_name}]"
            report_name = base["reportName"]
        report_targets[new_target_name] = base
        if set_default:
            project["report"]["defaultTarget"] = new_target_name
        log.info(
            "Patched report.targets: added '%s' with reportName='%s'",
            new_target_name,
            report_name,
        )

    path.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    return PbiTargetPatch(
        new_target_name=new_target_name,
        model_database=model_db,
        report_name=report_name,
    )
