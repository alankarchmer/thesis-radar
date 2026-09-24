"""The loaded workspace every command works with: paths, config, policy, theses, and the store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import Config, Workspace, load_config
from .policy import Policy, load_policy
from .runner import JudgePlan, plan_judging
from .store import Store
from .thesis import Thesis, ThesisError, load_theses


@dataclass
class App:
    ws: Workspace
    config: Config
    policy: Policy
    theses: dict[str, Thesis]
    store: Store
    today: date
    thesis_errors: list[ThesisError] = field(default_factory=list)

    @classmethod
    def open(cls, ws: Workspace, *, today: date | None = None, readonly: bool = False) -> App:
        """Load config, policy, and theses (raising ConfigError/PolicyError) and open the store."""
        if not readonly:
            ws.ensure_layout()
        config = load_config(ws.config_path)
        policy = load_policy(ws.policy_path)
        theses, errors = load_theses(ws.thesis_dir)
        store = Store(ws.db_path, readonly=readonly)
        return cls(ws, config, policy, theses, store, today or date.today(), errors)

    def reload(self) -> None:
        """Re-read policy.yaml and the thesis files after they change on disk."""
        self.policy = load_policy(self.ws.policy_path)
        self.theses, self.thesis_errors = load_theses(self.ws.thesis_dir)

    def plan(self, **kwargs: Any) -> JudgePlan:
        return plan_judging(
            self.store, self.theses, self.config.model, today=self.today,
            rejudge_window_days=self.config.rejudge_window_days, **kwargs,
        )

    def close(self) -> None:
        self.store.close()
