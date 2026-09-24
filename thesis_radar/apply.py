"""Apply dashboard actions (triage, labels, facts, prediction outcomes, policy) to the workspace.

STUB: implemented by the serve work package. Action shapes: spec v1.1 section 7.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .app import App


class ActionError(ValueError):
    """An action is malformed or refers to something that does not exist."""


def parse_actions(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array (or {"actions": [...]}) of actions."""
    raise NotImplementedError


def apply_actions(app: App, actions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate then apply every action; returns one result dict per action."""
    raise NotImplementedError
