"""BI QA strategy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "qa"


class BIQAStrategy:
    kind = "bi"
    has_live_qa = True

    def render_static_prompt(
        self,
        *,
        title: str,
        files: list[str],
        acceptance_criteria: str | None,
        deploy_target_name: str | None,
    ) -> str:
        template = (PROMPT_DIR / "bi_static.md").read_text(encoding="utf-8")
        files_text = "\n".join(f"- {path}" for path in files) if files else "- Inspect changed files."
        return template.format(
            title=title,
            files=files_text,
            acceptance_criteria=acceptance_criteria or "Use the assignment title and diff.",
            deploy_target_name=deploy_target_name or "dev",
        )

    def render_live_prompt(
        self,
        *,
        title: str,
        deploy_target_name: str | None,
        scenarios: list[dict[str, Any]],
    ) -> str:
        template = (PROMPT_DIR / "bi_live.md").read_text(encoding="utf-8")
        scenarios_text = json.dumps(scenarios, indent=2) if scenarios else "[]"
        return template.format(
            title=title,
            deploy_target_name=deploy_target_name or "dev",
            scenarios=scenarios_text,
        )
