"""Configuration loader.

Loads the packaged ``config.toml`` defaults, then layers
``~/.agents-orchestrator/config.toml`` on top when present. Returns a typed view
via pydantic so misspellings fail loudly at startup rather than at the
agent-launch boundary.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import tomllib
from pydantic import BaseModel, Field

PACKAGED_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
USER_CONFIG = Path.home() / ".agents-orchestrator" / "config.toml"


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


class BiConfig(BaseModel):
    deploy_target: DeployTargetConfig = Field(default_factory=DeployTargetConfig)


class GitConfig(BaseModel):
    branch_pattern: str = "agents/{pipeline}/{slug}"
    default_base_branch: str = "main"


class McpConfig(BaseModel):
    server_name: str = "agents-orchestrator"


class Config(BaseModel):
    paths: PathsConfig
    models: ModelsConfig
    caps: CapsConfig = Field(default_factory=CapsConfig)
    bi: BiConfig = Field(default_factory=BiConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @property
    def deploy_target(self) -> DeployTargetConfig:
        """Compatibility accessor for older call sites during migration."""
        return self.bi.deploy_target

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


def _normalize_legacy_config(config: dict) -> dict:
    """Accept old top-level [deploy_target] overrides by mapping them under [bi]."""
    if "deploy_target" in config:
        config = dict(config)
        bi = dict(config.get("bi") or {})
        bi["deploy_target"] = config.pop("deploy_target")
        config["bi"] = bi
    return config


def load_config(extra_path: Path | None = None) -> Config:
    """Load merged config.

    Order (last wins):
    1. Packaged ``config.toml``.
    2. ``~/.agents-orchestrator/config.toml`` if it exists.
    3. ``extra_path`` if supplied (useful for tests / CLI ``--config``).
    """
    if not PACKAGED_CONFIG.is_file():
        raise FileNotFoundError(
            f"Packaged config not found at {PACKAGED_CONFIG}. "
            "The agents-orchestrator package is mis-installed."
        )
    merged = _read_toml(PACKAGED_CONFIG)
    if USER_CONFIG.is_file():
        merged = _deep_merge(merged, _read_toml(USER_CONFIG))
    if extra_path is not None and extra_path.is_file():
        merged = _deep_merge(merged, _read_toml(extra_path))

    config = Config.model_validate(_normalize_legacy_config(merged))
    config.expand_paths()
    return config
