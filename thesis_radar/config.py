"""Workspace layout and config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODEL = "jev-1.13.0"
PRICE_PER_MILLION_INPUT_TOKENS = 0.042
DEFAULT_EDGAR_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "20-F", "40-F", "6-K")
DEFAULT_PEER_FORMS = ("8-K", "6-K")


class ConfigError(ValueError):
    """config.yaml is unreadable or invalid."""


@dataclass(frozen=True)
class Config:
    edgar_email: str | None = None
    model: str = DEFAULT_MODEL
    concurrency: int = 16
    requests_per_minute: int = 1200
    max_cost_per_run: float = 2.0
    rejudge_window_days: int = 120
    edgar_forms: tuple[str, ...] = DEFAULT_EDGAR_FORMS
    peer_forms: tuple[str, ...] = DEFAULT_PEER_FORMS
    serve_port: int = 8765


_SCALAR_TYPES: dict[str, type | tuple[type, ...]] = {
    "edgar_email": str,
    "model": str,
    "concurrency": int,
    "requests_per_minute": int,
    "max_cost_per_run": (int, float),
    "rejudge_window_days": int,
    "serve_port": int,
}
_LIST_FIELDS = frozenset({"edgar_forms", "peer_forms"})


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def failed(self) -> Path:
        return self.inbox / "_failed"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def thesis_dir(self) -> Path:
        return self.root / "thesis"

    @property
    def db_path(self) -> Path:
        return self.root / "radar.db"

    @property
    def lock_path(self) -> Path:
        return self.root / "radar.lock"

    @property
    def dashboard_path(self) -> Path:
        return self.root / "dashboard.html"

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def policy_path(self) -> Path:
        return self.root / "policy.yaml"

    def thesis_path(self, ticker: str) -> Path:
        return self.thesis_dir / f"{ticker}.yaml"

    def ensure_layout(self) -> None:
        for folder in (self.inbox, self.failed, self.archive, self.thesis_dir):
            folder.mkdir(parents=True, exist_ok=True)


def resolve_workspace(explicit: str | None = None) -> Workspace:
    raw = explicit or os.environ.get("RADAR_HOME") or os.getcwd()
    return Workspace(Path(raw).expanduser().resolve())


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: must be a mapping")
    known = {f.name for f in fields(Config)}
    unknown = sorted(str(key) for key in data if key not in known)
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key in _LIST_FIELDS:
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ConfigError(f"{path}: {key} must be a list of form names such as 10-K")
            values[key] = tuple(v.strip().upper() for v in value)
            continue
        kind = _SCALAR_TYPES[key]
        if isinstance(value, bool) or not isinstance(value, kind):
            raise ConfigError(f"{path}: {key} has the wrong type")
        values[key] = value
    config = Config(**values)
    if config.concurrency < 1 or config.requests_per_minute < 1:
        raise ConfigError(f"{path}: concurrency and requests_per_minute must be >= 1")
    if config.max_cost_per_run < 0:
        raise ConfigError(f"{path}: max_cost_per_run must be >= 0")
    if config.rejudge_window_days < 0:
        raise ConfigError(f"{path}: rejudge_window_days must be >= 0")
    if not 1024 <= config.serve_port <= 65535:
        raise ConfigError(f"{path}: serve_port must be between 1024 and 65535")
    if config.model.endswith(("-latest", "-preview")):
        raise ConfigError(f"{path}: pin a versioned model such as {DEFAULT_MODEL}, not an alias")
    return config
