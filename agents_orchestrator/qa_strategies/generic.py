"""Generic software-development QA strategy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "qa"


class GenericQAStrategy:
    kind = "generic"
    has_live_qa = False

    def render_static_prompt(
        self,
        *,
        title: str,
        files: list[str],
        acceptance_criteria: str | None,
        deploy_target_name: str | None,
    ) -> str:
        template = (PROMPT_DIR / "generic.md").read_text(encoding="utf-8")
        files_text = "\n".join(f"- {path}" for path in files) if files else "- Inspect changed files."
        return template.format(
            title=title,
            files=files_text,
            acceptance_criteria=acceptance_criteria or "Use the assignment title and diff.",
        )

    def render_live_prompt(
        self,
        *,
        title: str,
        deploy_target_name: str | None,
        scenarios: list[dict[str, Any]],
    ) -> str:
        raise RuntimeError("Generic QA does not have a live QA phase")
