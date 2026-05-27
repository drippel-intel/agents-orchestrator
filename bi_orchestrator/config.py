"""Configuration loader.

Loads the packaged ``config.toml`` defaults, then layers ``~/.bi-orchestrator/config.toml``
on top when present. Returns a typed view via pydantic so misspellings fail loudly at
startup rather than at the agent-launch boundary.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


PACKAGED_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
USER_CONFIG = Path.home() / ".bi-orchestrator" / "config.toml"


class PathsConfig(BaseModel):
    state_db: Path
    logs_dir: Path
    worktree_root_kind: Literal["sibling", "dedicated_dir"] = "sibling"
    worktree_dedicated_dir: Path | None = None


class ModelsConfig(BaseModel):
    planner: str
    developer: str
    qa: str


class AssignmentCaps(BaseModel):
    max_qa_iterations: int = 3
    max_validation_iterations: int = 3
    max_cost_usd: float = 5.0
    max_wallclock_minutes: int = 120


class PipelineCaps(BaseModel):
    max_parallel_dev_agents: int = 4
    max_cost_usd: float = 30.0


class CapsConfig(BaseModel):
    assignment: AssignmentCaps = Field(default_factory=AssignmentCaps)
    pipeline: PipelineCaps = Field(default_factory=PipelineCaps)


class DeployTargetConfig(BaseModel):
    pattern: str = "{base}-{slug}"


class GitConfig(BaseModel):
    branch_pattern: str = "agents/{pipeline}/{slug}"
    default_base_branch: str = "main"


class McpConfig(BaseModel):
    server_name: str = "bi-orchestrator"


class Config(BaseModel):
    paths: PathsConfig
    models: ModelsConfig
    caps: CapsConfig = Field(default_factory=CapsConfig)
    deploy_target: DeployTargetConfig = Field(default_factory=DeployTargetConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    def expand_paths(self) -> None:
        self.paths.state_db = Path(os.path.expandvars(self.paths.state_db.expanduser()))
        self.paths.logs_dir = Path(os.path.expandvars(self.paths.logs_dir.expanduser()))
        if self.paths.worktree_dedicated_dir is not None:
            self.paths.worktree_dedicated_dir = Path(
                os.path.expandvars(self.paths.worktree_dedicated_dir.expanduser())
            )


def _read_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(extra_path: Path | None = None) -> Config:
    """Load merged config.

    Order (last wins):
    1. Packaged ``config.toml``.
    2. ``~/.bi-orchestrator/config.toml`` if it exists.
    3. ``extra_path`` if supplied (useful for tests / CLI ``--config``).
    """
    if not PACKAGED_CONFIG.is_file():
        raise FileNotFoundError(
            f"Packaged config not found at {PACKAGED_CONFIG}. "
            "The bi-orchestrator package is mis-installed."
        )
    merged = _read_toml(PACKAGED_CONFIG)
    if USER_CONFIG.is_file():
        merged = _deep_merge(merged, _read_toml(USER_CONFIG))
    if extra_path is not None and extra_path.is_file():
        merged = _deep_merge(merged, _read_toml(extra_path))

    config = Config.model_validate(merged)
    config.expand_paths()
    return config
