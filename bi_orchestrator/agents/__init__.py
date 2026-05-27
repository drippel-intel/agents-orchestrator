"""Agent invocation wrappers (planner, developer, qa).

Each agent role is a thin wrapper over the Cursor SDK that knows:
- Which model to use (from config or per-pipeline override).
- Which prompt template to load.
- How to serialize its output into the SQLite state store.

Phase 0d ships the developer agent only. Planner / QA arrive in later phases.
"""

from __future__ import annotations
