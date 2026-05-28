from __future__ import annotations

from agents_orchestrator import db
from agents_orchestrator.state_machine import find_file_conflicts, ready_planned_assignments


def test_find_file_conflicts_normalizes_paths() -> None:
    conflicts = find_file_conflicts(
        [
            {"slug": "a", "files": [r"Model\Sales.tmdl"]},
            {"slug": "b", "files": ["model/sales.tmdl"]},
            {"slug": "c", "files": ["model/customers.tmdl"]},
        ]
    )

    assert len(conflicts) == 1
    assert conflicts[0].path == "model/sales.tmdl"
    assert conflicts[0].assignment_refs == ("a", "b")


def test_ready_planned_assignments_respects_dependencies_and_capacity() -> None:
    assignments = [
        {"id": "a1", "status": db.AssignmentStatus.DONE, "depends_on": []},
        {"id": "a2", "status": db.AssignmentStatus.PLANNED, "depends_on": ["a1"]},
        {"id": "a3", "status": db.AssignmentStatus.PLANNED, "depends_on": []},
    ]

    ready = ready_planned_assignments(assignments, max_parallel=1)

    assert [a["id"] for a in ready] == ["a2"]
