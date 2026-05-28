"""Pure state-machine helpers for daemon scheduling.

These helpers keep policy decisions testable without launching agents or touching
git worktrees. The orchestrator module owns side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import db


@dataclass(frozen=True)
class FileConflict:
    path: str
    assignment_refs: tuple[str, ...]


def find_file_conflicts(assignments: list[dict[str, Any]]) -> list[FileConflict]:
    """Return file paths claimed by more than one assignment."""
    owners: dict[str, list[str]] = {}
    for index, assignment in enumerate(assignments, start=1):
        ref = str(
            assignment.get("id")
            or assignment.get("slug")
            or assignment.get("title")
            or f"assignment_{index}"
        )
        for path in assignment.get("files") or []:
            normalized = str(path).replace("\\", "/").lower()
            if normalized:
                owners.setdefault(normalized, []).append(ref)
    return [
        FileConflict(path=path, assignment_refs=tuple(refs))
        for path, refs in owners.items()
        if len(refs) > 1
    ]


def ready_planned_assignments(
    assignments: list[dict[str, Any]],
    *,
    max_parallel: int,
) -> list[dict[str, Any]]:
    """Pick planned assignments whose dependencies are done, respecting capacity."""
    running = sum(1 for a in assignments if a["status"] == db.AssignmentStatus.DEV_RUNNING)
    slots = max(0, max_parallel - running)
    if slots == 0:
        return []

    done_ids = {
        a["id"]
        for a in assignments
        if a["status"] == db.AssignmentStatus.DONE
    }
    ready: list[dict[str, Any]] = []
    runnable_statuses = {
        db.AssignmentStatus.PLANNED,
        db.AssignmentStatus.VALIDATION_ITERATION,
    }
    for assignment in assignments:
        if assignment["status"] not in runnable_statuses:
            continue
        depends_on = assignment.get("depends_on") or []
        if all(dep in done_ids for dep in depends_on):
            ready.append(assignment)
        if len(ready) >= slots:
            break
    return ready


def all_assignments_done(assignments: list[dict[str, Any]]) -> bool:
    return bool(assignments) and all(a["status"] == db.AssignmentStatus.DONE for a in assignments)


def any_assignment_failed(assignments: list[dict[str, Any]]) -> bool:
    failed_statuses = {
        db.AssignmentStatus.FAILED,
        db.AssignmentStatus.CAP_EXCEEDED,
        db.AssignmentStatus.CANCELLED,
    }
    return any(a["status"] in failed_statuses for a in assignments)
