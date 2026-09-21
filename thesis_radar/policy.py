"""Thresholds from policy.yaml, applied to stored probabilities at view time."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .rubric import ASSUMPTION_PREFIX, OFF_THESIS


class PolicyError(ValueError):
    """policy.yaml is unreadable or invalid."""


@dataclass(frozen=True)
class Policy:
    metadata_min_confidence: float = 0.6
    pillar_confidence_min: float = 0.5
    boilerplate_max: float = 0.3
    new_info_min: float = 0.6
    materiality_min: float = 1.5
    contradicts_min: float = 0.7
    contradiction_materiality_min: float = 1.0
    maybe_new_info_low: float = 0.4
    maybe_new_info_high: float = 0.6


_SETTINGS: dict[tuple[str, ...], str] = {
    ("metadata", "min_confidence"): "metadata_min_confidence",
    ("whats_new", "pillar_confidence", "min"): "pillar_confidence_min",
    ("whats_new", "boilerplate", "max"): "boilerplate_max",
    ("whats_new", "new_info", "min"): "new_info_min",
    ("whats_new", "materiality", "min"): "materiality_min",
    ("contradictions", "contradicts", "min"): "contradicts_min",
    ("contradictions", "materiality", "min"): "contradiction_materiality_min",
}
_MAYBE_BAND = ("maybe", "new_info", "between")


def load_policy(path: Path) -> Policy:
    if not path.exists():
        return Policy()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"{path}: must be a mapping")
    values: dict[str, float] = {}
    for key, value in _leaves(data):
        name = ".".join(key)
        if key == _MAYBE_BAND:
            if not (isinstance(value, list) and len(value) == 2 and all(_is_number(v) for v in value) and value[0] < value[1]):
                raise PolicyError(f"{path}: {name} must be [low, high] with low < high")
            values["maybe_new_info_low"], values["maybe_new_info_high"] = float(value[0]), float(value[1])
        elif key in _SETTINGS:
            if not _is_number(value):
                raise PolicyError(f"{path}: {name} must be a number")
            values[_SETTINGS[key]] = float(value)
        else:
            raise PolicyError(f"{path}: unknown setting {name}")
    return Policy(**values)


def _leaves(data: Mapping[Any, Any], prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, value in data.items():
        path = prefix + (str(key),)
        if isinstance(value, dict):
            yield from _leaves(value, path)
        else:
            yield path, value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Classified:
    pillar: str | None
    pillar_confidence: float
    boilerplate: float
    new_info: float
    materiality: float
    stance: float
    evidence: str
    forward_looking: float
    supports: tuple[str, ...]
    contradicts: tuple[str, ...]
    in_contradictions: bool
    in_whats_new: bool
    in_maybe: bool


def classify(answers: Mapping[str, Mapping[str, Any]], policy: Policy) -> Classified:
    pillar_answer = answers["pillar"]
    pillar = pillar_answer["choice"]
    pillar_confidence = float(pillar_answer["confidence"])
    boilerplate = float(answers["boilerplate"]["noul"])
    new_info = float(answers["new_info"]["noul"])
    materiality = float(answers["materiality"]["score"])

    supports: list[str] = []
    contradicts: list[str] = []
    for name, answer in answers.items():
        if not name.startswith(ASSUMPTION_PREFIX):
            continue
        assumption_id = name[len(ASSUMPTION_PREFIX):]
        probabilities = answer["probabilities"]
        if probabilities.get("contradicts", 0.0) >= policy.contradicts_min:
            contradicts.append(assumption_id)
        elif probabilities.get("supports", 0.0) >= policy.contradicts_min:
            supports.append(assumption_id)

    in_contradictions = bool(contradicts) and materiality >= policy.contradiction_materiality_min
    on_pillar = pillar != OFF_THESIS and pillar_confidence >= policy.pillar_confidence_min
    eligible = (
        on_pillar
        and boilerplate <= policy.boilerplate_max
        and materiality >= policy.materiality_min
        and not in_contradictions
    )
    in_whats_new = eligible and new_info >= policy.new_info_min
    in_maybe = (
        eligible and not in_whats_new and policy.maybe_new_info_low <= new_info < policy.maybe_new_info_high
    )
    return Classified(
        pillar=None if pillar == OFF_THESIS else pillar,
        pillar_confidence=pillar_confidence,
        boilerplate=boilerplate,
        new_info=new_info,
        materiality=materiality,
        stance=float(answers["stance"]["score"]),
        evidence=answers["evidence"]["choice"],
        forward_looking=float(answers["forward_looking"]["noul"]),
        supports=tuple(supports),
        contradicts=tuple(contradicts),
        in_contradictions=in_contradictions,
        in_whats_new=in_whats_new,
        in_maybe=in_maybe,
    )
