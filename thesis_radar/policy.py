"""Thresholds from policy.yaml, applied to stored probabilities at view time.

`classify` here and `RadarLogic.classify` in the dashboard implement the same rules (spec v1.1,
section 4); tests/fixtures/classify_cases.json holds the shared cases both must pass.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from .rubric import ASSUMPTION_PREFIX, NONE, OFF_THESIS, QUESTION_PREFIX, UPDATES_FACT


class PolicyError(ValueError):
    """policy.yaml is unreadable or invalid."""


@dataclass(frozen=True)
class Policy:
    metadata_min_probability: float = 0.6
    whats_new_window_days: int = 30
    pillar_probability_min: float = 0.5
    boilerplate_max: float = 0.3
    new_info_min: float = 0.6
    materiality_min: float = 1.5
    contradictions_window_days: int = 120
    contradicts_min: float = 0.7
    contradiction_materiality_min: float = 1.0
    contradiction_boilerplate_max: float = 0.5
    maybe_new_info_low: float = 0.4
    maybe_new_info_high: float = 0.6
    open_questions_min: float = 0.6
    divergence_window_days: int = 120
    divergence_min_gap: float = 1.0
    divergence_min_passages: int = 3
    ledger_min_probability: float = 0.6

    def flat(self) -> dict[str, float | int]:
        return asdict(self)


_SETTINGS: dict[tuple[str, ...], str] = {
    ("metadata", "min_probability"): "metadata_min_probability",
    ("whats_new", "window_days"): "whats_new_window_days",
    ("whats_new", "pillar_probability", "min"): "pillar_probability_min",
    ("whats_new", "boilerplate", "max"): "boilerplate_max",
    ("whats_new", "new_info", "min"): "new_info_min",
    ("whats_new", "materiality", "min"): "materiality_min",
    ("contradictions", "window_days"): "contradictions_window_days",
    ("contradictions", "contradicts", "min"): "contradicts_min",
    ("contradictions", "materiality", "min"): "contradiction_materiality_min",
    ("contradictions", "boilerplate", "max"): "contradiction_boilerplate_max",
    ("open_questions", "min"): "open_questions_min",
    ("divergence", "window_days"): "divergence_window_days",
    ("divergence", "min_gap"): "divergence_min_gap",
    ("divergence", "min_passages"): "divergence_min_passages",
    ("ledger", "min_probability"): "ledger_min_probability",
}
_MAYBE_BAND = ("maybe", "new_info", "between")
_INTEGER_FIELDS = frozenset(
    {"whats_new_window_days", "contradictions_window_days", "divergence_window_days", "divergence_min_passages"}
)
_PROBABILITY_FIELDS = frozenset(
    {"metadata_min_probability", "pillar_probability_min", "boilerplate_max", "new_info_min", "contradicts_min",
     "contradiction_boilerplate_max",
     "maybe_new_info_low", "maybe_new_info_high", "open_questions_min", "ledger_min_probability"}
)


def load_policy(path: Path) -> Policy:
    if not path.exists():
        return Policy()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: must be a mapping")
    values: dict[str, Any] = {}
    for key, value in _leaves(data):
        name = ".".join(key)
        if key == _MAYBE_BAND:
            if not (isinstance(value, list) and len(value) == 2 and all(_is_number(v) for v in value) and value[0] < value[1]):
                raise PolicyError(f"{path}: {name} must be [low, high] with low < high")
            values["maybe_new_info_low"], values["maybe_new_info_high"] = float(value[0]), float(value[1])
        elif key in _SETTINGS:
            if not _is_number(value):
                raise PolicyError(f"{path}: {name} must be a number")
            values[_SETTINGS[key]] = value
        else:
            raise PolicyError(f"{path}: unknown setting {name}")
    try:
        return policy_from_flat(values)
    except PolicyError as exc:
        raise PolicyError(f"{path}: {exc}") from exc


def policy_from_flat(values: Mapping[str, Any], base: Policy | None = None) -> Policy:
    """Build a Policy from flat field values (a partial mapping updates `base`), validating each."""
    known = {f.name for f in fields(Policy)}
    merged = (base or Policy()).flat()
    for name, value in values.items():
        if name not in known:
            raise PolicyError(f"unknown setting {name}")
        if not _is_number(value):
            raise PolicyError(f"{name} must be a number")
        if name in _INTEGER_FIELDS:
            if float(value) != int(value) or value < 0:
                raise PolicyError(f"{name} must be a whole number >= 0")
            merged[name] = int(value)
        else:
            merged[name] = float(value)
            if name in _PROBABILITY_FIELDS and not 0 <= merged[name] <= 1:
                raise PolicyError(f"{name} must be between 0 and 1")
    if merged["maybe_new_info_low"] >= merged["maybe_new_info_high"]:
        raise PolicyError("maybe band must have low < high")
    return Policy(**merged)


def policy_yaml(policy: Policy) -> str:
    """policy.yaml text in the documented nested layout."""
    p = policy
    return (
        "metadata:\n"
        f"  min_probability: {p.metadata_min_probability:g}\n"
        "whats_new:\n"
        f"  window_days: {p.whats_new_window_days}\n"
        f"  pillar_probability: {{min: {p.pillar_probability_min:g}}}\n"
        f"  boilerplate: {{max: {p.boilerplate_max:g}}}\n"
        f"  new_info: {{min: {p.new_info_min:g}}}\n"
        f"  materiality: {{min: {p.materiality_min:g}}}\n"
        "contradictions:\n"
        f"  window_days: {p.contradictions_window_days}\n"
        f"  contradicts: {{min: {p.contradicts_min:g}}}\n"
        f"  materiality: {{min: {p.contradiction_materiality_min:g}}}\n"
        f"  boilerplate: {{max: {p.contradiction_boilerplate_max:g}}}\n"
        "maybe:\n"
        f"  new_info: {{between: [{p.maybe_new_info_low:g}, {p.maybe_new_info_high:g}]}}\n"
        "open_questions:\n"
        f"  min: {p.open_questions_min:g}\n"
        "divergence:\n"
        f"  window_days: {p.divergence_window_days}\n"
        f"  min_gap: {p.divergence_min_gap:g}\n"
        f"  min_passages: {p.divergence_min_passages}\n"
        "ledger:\n"
        f"  min_probability: {p.ledger_min_probability:g}\n"
    )


def _leaves(data: Mapping[Any, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, value in data.items():
        path = prefix + (str(key),)
        if isinstance(value, dict):
            yield from _leaves(value, path)
        else:
            yield path, value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# Signals: stored answers reduced to the numbers the policy uses (the payload's `p` object).


def signals(answers: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """The payload `p` object for merged answers; None when the core answers are missing."""
    if not answers or "pillar" not in answers:
        return None
    pillar_answer = answers["pillar"]
    pillar = str(pillar_answer["choice"])
    pillar_p = float(pillar_answer.get("probabilities", {}).get(pillar, pillar_answer.get("confidence", 0.0)))
    fact_answer = answers.get(UPDATES_FACT)
    updates_fact, updates_fact_p = None, None
    if fact_answer is not None and fact_answer.get("choice") not in (None, NONE):
        updates_fact = str(fact_answer["choice"])
        updates_fact_p = float(fact_answer.get("probabilities", {}).get(updates_fact, 0.0))
    assumptions = {}
    questions = {}
    for name, answer in answers.items():
        if name.startswith(ASSUMPTION_PREFIX):
            probabilities = answer.get("probabilities", {})
            assumptions[name[len(ASSUMPTION_PREFIX):]] = {
                "supports": float(probabilities.get("supports", 0.0)),
                "contradicts": float(probabilities.get("contradicts", 0.0)),
            }
        elif name.startswith(QUESTION_PREFIX):
            questions[name[len(QUESTION_PREFIX):]] = float(answer.get("noul", 0.0))

    def noul(name: str, default: float = 0.0) -> float:
        answer = answers.get(name)
        return default if answer is None else float(answer["noul"])

    def score(name: str) -> float:
        answer = answers.get(name)
        return 0.0 if answer is None else float(answer["score"])

    evidence = answers.get("evidence")
    return {
        "pillar": pillar,
        "pillar_p": pillar_p,
        "boilerplate": noul("boilerplate"),
        "new_info": noul("new_info"),
        "materiality": score("materiality"),
        "stance": score("stance") if "stance" in answers else 2.0,
        "evidence": None if evidence is None else str(evidence["choice"]),
        "forward_looking": noul("forward_looking"),
        "updates_fact": updates_fact,
        "updates_fact_p": updates_fact_p,
        "assumptions": assumptions,
        "questions": questions,
    }


@dataclass(frozen=True)
class Classified:
    in_contradictions: bool
    in_whats_new: bool
    in_maybe: bool
    flagged: bool
    contradicts: tuple[str, ...]
    supports: tuple[str, ...]
    questions: tuple[str, ...]
    contradiction_raw: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "in_contradictions": self.in_contradictions,
            "in_whats_new": self.in_whats_new,
            "in_maybe": self.in_maybe,
            "flagged": self.flagged,
            "contradicts": list(self.contradicts),
            "supports": list(self.supports),
            "questions": list(self.questions),
        }


NOTHING = Classified(False, False, False, False, (), (), ())


def passage_day(passage_date: str | None, ingested_at: str | None) -> str | None:
    if passage_date:
        return passage_date[:10]
    if ingested_at:
        return ingested_at[:10]
    return None


def recent(day: str | None, window_days: int, today: date) -> bool:
    if day is None:
        return False
    return day >= (today - timedelta(days=int(window_days))).isoformat()


def classify(
    p: Mapping[str, Any] | None,
    policy: Policy,
    *,
    today: date,
    passage_date: str | None = None,
    ingested_at: str | None = None,
    triage_status: str | None = None,
) -> Classified:
    if not p:
        return NOTHING
    day = passage_day(passage_date, ingested_at)
    # Missing (None) answers come from stale judgments made before a question existed: no evidence.
    assumptions = {a: probs for a, probs in (p.get("assumptions") or {}).items() if probs}
    contradicts = sorted(a for a, probs in assumptions.items() if (probs.get("contradicts") or 0.0) >= policy.contradicts_min)
    supports = sorted(
        a for a, probs in assumptions.items()
        if (probs.get("supports") or 0.0) >= policy.contradicts_min and a not in contradicts
    )
    questions = sorted(
        q for q, prob in (p.get("questions") or {}).items() if prob is not None and prob >= policy.open_questions_min
    )
    materiality = float(p.get("materiality") or 0.0)
    new_info = float(p.get("new_info") or 0.0)
    boilerplate = p.get("boilerplate")
    boilerplate_p = float(1.0 if boilerplate is None else boilerplate)
    # Boilerplate, including risk disclosures about what could happen, is never a contradiction. The limit is
    # looser than What's new's on purpose: a missed contradiction costs more than an extra read.
    contra_raw = (
        bool(contradicts)
        and materiality >= policy.contradiction_materiality_min
        and boilerplate_p <= policy.contradiction_boilerplate_max
    )
    in_contradictions = (
        contra_raw and recent(day, policy.contradictions_window_days, today) and triage_status != "acknowledged"
    )
    on_pillar = (
        p.get("pillar") not in (None, OFF_THESIS) and float(p.get("pillar_p") or 0.0) >= policy.pillar_probability_min
    )
    substantive = (
        on_pillar and boilerplate_p <= policy.boilerplate_max
        and materiality >= policy.materiality_min
    )
    eligible = (
        substantive
        and not contra_raw
        and recent(day, policy.whats_new_window_days, today)
        and triage_status not in ("dismissed", "absorbed")
    )
    in_whats_new = eligible and new_info >= policy.new_info_min
    in_maybe = eligible and not in_whats_new and policy.maybe_new_info_low <= new_info < policy.maybe_new_info_high
    flagged = contra_raw or (substantive and new_info >= policy.maybe_new_info_low)
    return Classified(
        in_contradictions=in_contradictions,
        in_whats_new=in_whats_new,
        in_maybe=in_maybe,
        flagged=flagged,
        contradicts=tuple(contradicts),
        supports=tuple(supports),
        questions=tuple(questions),
        contradiction_raw=contra_raw,
    )


def classify_passage(passage: Mapping[str, Any], policy: Policy, today: date) -> Classified:
    """Classify a payload-shaped passage ({date, ingested_at, p, triage})."""
    triage = passage.get("triage") or {}
    return classify(
        passage.get("p"), policy, today=today, passage_date=passage.get("date"),
        ingested_at=passage.get("ingested_at"), triage_status=triage.get("status"),
    )
