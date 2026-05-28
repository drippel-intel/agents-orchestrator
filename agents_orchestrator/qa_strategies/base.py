"""Shared QA strategy protocol."""

from __future__ import annotations

from typing import Any, Protocol


class QAStrategy(Protocol):
    kind: str
    has_live_qa: bool

    def render_static_prompt(
        self,
        *,
        title: str,
        files: list[str],
        acceptance_criteria: str | None,
        deploy_target_name: str | None,
    ) -> str: ...

    def render_live_prompt(
        self,
        *,
        title: str,
        deploy_target_name: str | None,
        scenarios: list[dict[str, Any]],
    ) -> str: ...
