"""Apply dashboard actions (triage, labels, facts, prediction outcomes, policy) to the workspace.

Action shapes are spec v1.1 section 7. `apply_actions` validates every action before it applies
any, so a malformed batch changes nothing. Actions apply in order, and each one is validated against
the state the actions before it leave behind (fact ids and counts, policy values). A fact edit that
the thesis file itself rejects at apply time stops the batch there; the actions before it stay applied.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import thesis_edit
from .app import App
from .policy import Policy, PolicyError, policy_from_flat, policy_yaml
from .runner import passage_keys
from .store import UNSET
from .thesis import MAX_FACTS_PER_PILLAR, Thesis

OPS = ("triage", "label", "fact", "resolve", "policy")
TRIAGE_STATUSES = ("dismissed", "absorbed", "acknowledged")
LABEL_QUESTIONS = ("new_info", "material", "whats_new")
CONTRADICTS_PREFIX = "contradicts__"
LABEL_ORIGINS = ("triage", "spotcheck")
MAX_FACT_CHARS = 300

Step = Callable[[], dict[str, Any]]


class ActionError(ValueError):
    """An action is malformed or refers to something that does not exist."""


def parse_actions(text: str) -> list[dict[str, Any]]:
    """Parse a JSON array (or {"actions": [...]}) of actions."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"actions are not valid JSON: {exc}") from exc
    if isinstance(data, dict) and "actions" in data:
        data = data["actions"]
    if not isinstance(data, list):
        raise ActionError('actions must be a JSON array of actions or {"actions": [...]}')
    for index, action in enumerate(data):
        if not isinstance(action, dict):
            raise ActionError(f"action {index}: must be an object with an op")
    return data


def apply_actions(app: App, actions: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate then apply every action; returns one result dict per action."""
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Sequence):
        raise ActionError("actions must be a list")
    checker = _Checker(app)
    steps = [checker.check(index, action) for index, action in enumerate(actions)]
    results = []
    for index, (op, step) in enumerate(steps):
        try:
            results.append(step())
        except ActionError as exc:
            applied = f"the {index} action(s) before it were applied" if index else "nothing was applied"
            raise ActionError(f"action {index} ({op}): {exc}; {applied}") from exc
    return results


class _Checker:
    """Validates actions in order, tracking what earlier actions in the batch will change."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.policy: Policy = app.policy
        self.fact_counts: dict[tuple[str, str], int] = {}
        self.keys: dict[tuple[str, int], list[str]] = {}

    def check(self, index: int, action: Any) -> tuple[str, Step]:
        if not isinstance(action, Mapping):
            raise ActionError(f"action {index}: must be an object with an op")
        op = action.get("op")
        if op not in OPS:
            raise ActionError(f"action {index}: unknown op {op!r}; expected one of {', '.join(OPS)}")
        try:
            return op, getattr(self, f"_{op}")(action)
        except ActionError as exc:
            raise ActionError(f"action {index} ({op}): {exc}") from None

    # Shared checks

    def _thesis(self, action: Mapping[str, Any]) -> Thesis:
        ticker = action.get("ticker")
        if not isinstance(ticker, str) or ticker not in self.app.theses:
            raise ActionError(f"unknown ticker {ticker!r}")
        return self.app.theses[ticker]

    def _passage(self, thesis: Thesis, value: Any, name: str = "passage_id") -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ActionError(f"{name} must be a passage id, got {value!r}")
        rows = self.app.store.get_passages([value])
        if not rows:
            raise ActionError(f"no passage {value}")
        row = rows[0]
        if row["doc_status"] != "sorted" or row["ticker"] not in (thesis.ticker, *thesis.peers):
            raise ActionError(f"passage {value} is not from {thesis.ticker} or its peers")
        return row

    def _fact_count(self, thesis: Thesis, pillar: str) -> int:
        key = (thesis.ticker, pillar)
        if key not in self.fact_counts:
            self.fact_counts[key] = len(thesis.known_facts.get(pillar, ()))
        return self.fact_counts[key]

    # Ops

    def _triage(self, action: Mapping[str, Any]) -> Step:
        thesis = self._thesis(action)
        passage_id = self._passage(thesis, action.get("passage_id"))["passage_id"]
        if "status" not in action and "starred" not in action:
            raise ActionError("give status, starred, or both")
        status = action.get("status", UNSET)
        if status is not UNSET and status is not None and status not in TRIAGE_STATUSES:
            raise ActionError(f"status must be one of {', '.join(TRIAGE_STATUSES)}, or null; got {status!r}")
        starred = action.get("starred", UNSET)
        if starred is not UNSET and not isinstance(starred, bool):
            raise ActionError(f"starred must be true or false, got {starred!r}")
        ticker, store = thesis.ticker, self.app.store

        def run() -> dict[str, Any]:
            store.set_triage(ticker, passage_id, status=status, starred=starred)
            state = store.triage_map(ticker).get(passage_id, {"status": None, "starred": False})
            return {"op": "triage", "ok": True, "ticker": ticker, "passage_id": passage_id, **state}

        return run

    def _label(self, action: Mapping[str, Any]) -> Step:
        thesis = self._thesis(action)
        passage_id = self._passage(thesis, action.get("passage_id"))["passage_id"]
        question = action.get("question")
        assumptions = {a.id for a in thesis.assumptions}
        if not isinstance(question, str) or not (
            question in LABEL_QUESTIONS
            or (question.startswith(CONTRADICTS_PREFIX) and question[len(CONTRADICTS_PREFIX):] in assumptions)
        ):
            allowed = [*LABEL_QUESTIONS, *(CONTRADICTS_PREFIX + a for a in sorted(assumptions))]
            raise ActionError(f"question must be one of {', '.join(allowed)}; got {question!r}")
        value = action.get("value")
        if not (isinstance(value, bool) or (isinstance(value, int) and value in (0, 1))):
            raise ActionError(f"value must be 0 or 1, got {value!r}")
        origin = action.get("origin") or "triage"
        if origin not in LABEL_ORIGINS:
            raise ActionError(f"origin must be one of {', '.join(LABEL_ORIGINS)}; got {origin!r}")
        # The judgments the user saw when labeling: keys under the thesis as it was before this batch.
        cache = (thesis.ticker, passage_id)
        if cache not in self.keys:
            self.keys[cache] = passage_keys(self.app.store, thesis, passage_id, self.app.config.model)
        keys = self.keys[cache]
        ticker, store = thesis.ticker, self.app.store

        def run() -> dict[str, Any]:
            store.save_label(
                passage_id, question, bool(value), ticker=ticker, origin=origin, weight=1.0, judgment_keys=keys
            )
            return {
                "op": "label", "ok": True, "ticker": ticker, "passage_id": passage_id, "question": question,
                "value": int(value), "origin": origin,
            }

        return run

    def _fact(self, action: Mapping[str, Any]) -> Step:
        thesis = self._thesis(action)
        pillar = action.get("pillar")
        if not isinstance(pillar, str) or pillar not in thesis.pillars:
            raise ActionError(f"unknown pillar {pillar!r}; {thesis.ticker} has: {', '.join(thesis.pillars)}")
        text = action.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ActionError("text must be non-empty")
        text = text.strip()
        if len(text) > MAX_FACT_CHARS:
            raise ActionError(f"text is {len(text)} characters; a fact holds at most {MAX_FACT_CHARS}")
        source = action.get("source")
        source_row = None if source is None else self._passage(thesis, source, "source")
        as_of = action.get("as_of")
        if as_of is not None and not thesis_edit.is_date(as_of):
            raise ActionError(f"as_of must be a date written YYYY-MM-DD, got {as_of!r}")
        if as_of is None and source_row is not None and source_row["doc_date"]:
            as_of = source_row["doc_date"][:10]
        replace = action.get("replace")
        moved_from = None
        if replace is not None:
            match = thesis_edit.FACT_ID.match(replace) if isinstance(replace, str) else None
            if match is None or match[1] not in thesis.pillars or int(match[2]) >= self._fact_count(thesis, match[1]):
                raise ActionError(f"no known fact {replace!r} to replace")
            moved_from = match[1]
        if moved_from != pillar:
            if self._fact_count(thesis, pillar) >= MAX_FACTS_PER_PILLAR:
                raise ActionError(
                    f"{pillar} already has {MAX_FACTS_PER_PILLAR} known facts; replace one instead of adding"
                )
            self.fact_counts[(thesis.ticker, pillar)] += 1
            if moved_from is not None:
                self.fact_counts[(thesis.ticker, moved_from)] -= 1
        ticker, app = thesis.ticker, self.app

        def run() -> dict[str, Any]:
            try:
                fact_id = thesis_edit.add_fact(
                    app.ws.thesis_path(ticker), pillar, text, source=source, as_of=as_of, replace=replace
                )
            except (OSError, ValueError) as exc:
                raise ActionError(str(exc)) from exc
            app.reload()
            return {"op": "fact", "ok": True, "ticker": ticker, "fact_id": fact_id}

        return run

    def _resolve(self, action: Mapping[str, Any]) -> Step:
        thesis = self._thesis(action)
        prediction_id = action.get("prediction_id")
        predictions = [p.id for p in thesis.predictions]
        if not isinstance(prediction_id, str) or prediction_id not in predictions:
            known = ", ".join(predictions) or "none"
            raise ActionError(f"unknown prediction {prediction_id!r}; {thesis.ticker} has: {known}")
        if "outcome" not in action:
            raise ActionError("outcome is required: true, false, or null to clear")
        outcome = action["outcome"]
        if outcome is not None and not isinstance(outcome, bool):
            raise ActionError(f"outcome must be true, false, or null; got {outcome!r}")
        ticker, store = thesis.ticker, self.app.store

        def run() -> dict[str, Any]:
            store.set_resolution(ticker, prediction_id, outcome)
            return {"op": "resolve", "ok": True, "ticker": ticker, "prediction_id": prediction_id, "outcome": outcome}

        return run

    def _policy(self, action: Mapping[str, Any]) -> Step:
        values = action.get("values")
        if not isinstance(values, Mapping):
            raise ActionError("values must be an object of policy settings")
        try:
            policy = policy_from_flat(values, base=self.policy)
        except PolicyError as exc:
            raise ActionError(str(exc)) from exc
        self.policy = policy
        app = self.app

        def run() -> dict[str, Any]:
            write_atomic(app.ws.policy_path, policy_yaml(policy))
            app.reload()
            return {"op": "policy", "ok": True, "policy": policy.flat()}

        return run


def write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` through a temporary file in the same folder, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
