"""Cursor SDK helpers: API-key resolution, model verification.

Kept separate from agent invocation code so the MCP server and CLI can call them
without pulling in any agent-launch logic.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


log = logging.getLogger("bi_orchestrator.sdk_utils")


class ApiKeyMissing(RuntimeError):
    """Raised when CURSOR_API_KEY is not set in the environment."""


def get_api_key() -> str:
    key = os.environ.get("CURSOR_API_KEY", "").strip()
    if not key:
        raise ApiKeyMissing(
            "CURSOR_API_KEY is not set. Mint a key at https://cursor.com/dashboard/integrations "
            "and either set it for the current session (`$env:CURSOR_API_KEY = '...'` in "
            "PowerShell) or persist it with `setx CURSOR_API_KEY '...'` and restart the shell."
        )
    return key


@dataclass
class ModelResolution:
    requested: str
    resolved: str
    fallback_used: bool
    available_ids: list[str]


def resolve_model(requested: str, api_key: str | None = None) -> ModelResolution:
    """Verify ``requested`` exists in the account's model list; fall back to a
    canonical Opus variant when the slug differs, else to ``auto``.

    Returns a ``ModelResolution`` so callers can log and persist the decision.
    """
    from cursor_sdk import Cursor

    models = Cursor.models.list(api_key=api_key) if api_key else Cursor.models.list()
    available_ids: list[str] = []
    for m in models:
        mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
        if isinstance(mid, str):
            available_ids.append(mid)

    if requested in available_ids:
        return ModelResolution(requested, requested, False, available_ids)

    requested_lower = requested.lower()
    for candidate in available_ids:
        if candidate.lower() == requested_lower:
            log.info("Model %r resolved to %r (case-insensitive match)", requested, candidate)
            return ModelResolution(requested, candidate, True, available_ids)

    if "opus" in requested_lower:
        opus_candidates = [m for m in available_ids if "opus" in m.lower()]
        if opus_candidates:
            opus_candidates.sort(reverse=True)
            chosen = opus_candidates[0]
            log.warning(
                "Model %r not found; falling back to closest Opus variant %r. "
                "Available: %s",
                requested, chosen, opus_candidates,
            )
            return ModelResolution(requested, chosen, True, available_ids)

    log.warning(
        "Model %r not found and no Opus variant available; falling back to 'auto'. "
        "Available: %s",
        requested, available_ids,
    )
    return ModelResolution(requested, "auto", True, available_ids)
