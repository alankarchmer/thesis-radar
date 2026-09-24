"""Load and validate per-company thesis files (thesis/<TICKER>.yaml)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

MAX_PILLARS = 12
MAX_ASSUMPTIONS = 10
MAX_FACTS_PER_PILLAR = 20
MAX_OPEN_QUESTIONS = 5
MAX_PREDICTIONS = 10
MAX_PEERS = 5
MAX_METRICS = 12
HIGHER_IS = frozenset({"good", "bad", "neutral"})
RESERVED_PILLARS = frozenset({"off_thesis"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KNOWN_KEYS = frozenset(
    {"ticker", "company", "aliases", "peers", "pillars", "assumptions", "open_questions", "predictions", "known_facts",
     "metrics"}
)


class ThesisError(ValueError):
    def __init__(self, path: Path, field: str, reason: str) -> None:
        super().__init__(f"{path}: {field}: {reason}")
        self.path = path
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class Assumption:
    id: str
    pillar: str
    statement: str


@dataclass(frozen=True)
class OpenQuestion:
    id: str
    text: str


@dataclass(frozen=True)
class Prediction:
    id: str
    statement: str
    by: str
    p: float
    pillar: str | None = None


@dataclass(frozen=True)
class Metric:
    """A number the thesis depends on, tracked over time from the passages that report it."""

    id: str
    label: str
    unit: str
    pillar: str | None = None
    higher_is: str = "neutral"
    definition: str | None = None


@dataclass(frozen=True)
class Fact:
    id: str
    pillar: str
    text: str
    as_of: str | None = None
    source: int | None = None

    def for_jev(self) -> str:
        return f"{self.text} (as of {self.as_of})" if self.as_of else self.text


@dataclass(frozen=True)
class Thesis:
    ticker: str
    company: str
    aliases: tuple[str, ...]
    pillars: dict[str, str]
    assumptions: tuple[Assumption, ...]
    known_facts: dict[str, tuple[Fact, ...]]
    open_questions: tuple[OpenQuestion, ...] = ()
    predictions: tuple[Prediction, ...] = ()
    peers: tuple[str, ...] = ()
    path: Path | None = field(default=None, compare=False)
    # Metrics are asked about in their own requests, so they are not part of `canonical()` or `version`:
    # adding or editing a metric never re-judges passages.
    metrics: tuple[Metric, ...] = ()

    def canonical(self) -> dict[str, Any]:
        """Everything Jev sees about the thesis. The version hashes exactly this."""
        return {
            "ticker": self.ticker,
            "company": self.company,
            "aliases": list(self.aliases),
            "pillars": dict(sorted(self.pillars.items())),
            "assumptions": {
                a.id: {"pillar": a.pillar, "statement": a.statement} for a in sorted(self.assumptions, key=lambda a: a.id)
            },
            "open_questions": {q.id: q.text for q in sorted(self.open_questions, key=lambda q: q.id)},
            "known_facts": {p: [f.for_jev() for f in self.known_facts.get(p, ())] for p in sorted(self.pillars)},
        }

    @property
    def version(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def facts(self) -> list[Fact]:
        return [fact for pillar in self.pillars for fact in self.known_facts.get(pillar, ())]

    def fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts() if f.id == fact_id), None)


def load_thesis(path: Path) -> Thesis:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ThesisError(path, "<file>", f"invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ThesisError(path, "<file>", f"cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise ThesisError(path, "<file>", "must be a mapping")
    _check_string_keys(path, "<root>", data)
    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise ThesisError(path, unknown[0], f"unknown key; allowed: {', '.join(sorted(_KNOWN_KEYS))}")
    ticker = _required_text(path, data, "ticker")
    if not _TICKER.match(ticker):
        raise ThesisError(path, "ticker", "must be 1-10 uppercase letters, digits, '.' or '-'")
    if ticker != path.stem:
        raise ThesisError(path, "ticker", f"must match the file name ({path.stem})")
    company = _required_text(path, data, "company")
    aliases = _text_list(path, "aliases", data.get("aliases"))
    peers = _peers(path, data.get("peers"), ticker)
    pillars = _pillars(path, data.get("pillars"))
    assumptions = _assumptions(path, _mapping(path, "assumptions", data.get("assumptions")), pillars)
    open_questions = _open_questions(path, _mapping(path, "open_questions", data.get("open_questions")))
    predictions = _predictions(path, _mapping(path, "predictions", data.get("predictions")), pillars)
    known_facts = _known_facts(path, _mapping(path, "known_facts", data.get("known_facts")), pillars)
    metrics = _metrics(path, _mapping(path, "metrics", data.get("metrics")), pillars)
    return Thesis(
        ticker, company, aliases, pillars, assumptions, known_facts, open_questions, predictions, peers, path=path,
        metrics=metrics,
    )


def load_theses(thesis_dir: Path) -> tuple[dict[str, Thesis], list[ThesisError]]:
    theses: dict[str, Thesis] = {}
    errors: list[ThesisError] = []
    if not thesis_dir.is_dir():
        return theses, errors
    for path in sorted(thesis_dir.glob("*.yaml")):
        try:
            thesis = load_thesis(path)
        except ThesisError as exc:
            errors.append(exc)
            continue
        theses[thesis.ticker] = thesis
    return theses, errors


def _check_string_keys(path: Path, where: str, mapping: dict[Any, Any]) -> None:
    for key in mapping:
        if not isinstance(key, str):
            raise ThesisError(
                path,
                f"{where}.{key!r}",
                "key did not load as a string; quote it (YAML reads yes/no/on/off as booleans)",
            )


def _mapping(path: Path, where: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ThesisError(path, where, "must be a mapping")
    _check_string_keys(path, where, raw)
    return raw


def _required_text(path: Path, data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ThesisError(path, key, "is required and must be non-empty text")
    return value.strip()


def _text_list(path: Path, where: str, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(a, str) and a.strip() for a in raw):
        raise ThesisError(path, where, "must be a list of non-empty text")
    return tuple(a.strip() for a in raw)


def _peers(path: Path, raw: Any, ticker: str) -> tuple[str, ...]:
    peers = _text_list(path, "peers", raw)
    if len(peers) > MAX_PEERS:
        raise ThesisError(path, "peers", f"at most {MAX_PEERS} peers")
    for peer in peers:
        if not _TICKER.match(peer):
            raise ThesisError(path, f"peers.{peer}", "must be an uppercase ticker")
        if peer == ticker:
            raise ThesisError(path, f"peers.{peer}", "a company cannot be its own peer")
    if len(set(peers)) != len(peers):
        raise ThesisError(path, "peers", "lists a ticker twice")
    return peers


def _identifier(path: Path, where: str, name: str, what: str = "name") -> None:
    if not _IDENTIFIER.match(name):
        raise ThesisError(path, where, f"{what} must be lowercase letters, digits and underscores, starting with a letter")


def _pillars(path: Path, raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ThesisError(path, "pillars", f"must map 1-{MAX_PILLARS} pillar names to descriptions")
    _check_string_keys(path, "pillars", raw)
    if len(raw) > MAX_PILLARS:
        raise ThesisError(path, "pillars", f"at most {MAX_PILLARS} pillars")
    pillars: dict[str, str] = {}
    for name, description in raw.items():
        where = f"pillars.{name}"
        _identifier(path, where, name)
        if name in RESERVED_PILLARS:
            raise ThesisError(path, where, "name is reserved")
        if not isinstance(description, str) or not description.strip():
            raise ThesisError(path, where, "description must be non-empty text")
        pillars[name] = description.strip()
    return pillars


def _assumptions(path: Path, raw: dict[str, Any], pillars: dict[str, str]) -> tuple[Assumption, ...]:
    if len(raw) > MAX_ASSUMPTIONS:
        raise ThesisError(path, "assumptions", f"at most {MAX_ASSUMPTIONS} assumptions")
    assumptions = []
    for assumption_id, body in raw.items():
        where = f"assumptions.{assumption_id}"
        _identifier(path, where, assumption_id, "id")
        if not isinstance(body, dict):
            raise ThesisError(path, where, "must have pillar and statement")
        _check_string_keys(path, where, body)
        pillar = body.get("pillar")
        statement = body.get("statement")
        if not isinstance(pillar, str) or pillar not in pillars:
            raise ThesisError(path, f"{where}.pillar", f"must name an existing pillar, got {pillar!r}")
        if not isinstance(statement, str) or not statement.strip():
            raise ThesisError(path, f"{where}.statement", "must be non-empty text")
        assumptions.append(Assumption(assumption_id, pillar, statement.strip()))
    return tuple(assumptions)


def _open_questions(path: Path, raw: dict[str, Any]) -> tuple[OpenQuestion, ...]:
    if len(raw) > MAX_OPEN_QUESTIONS:
        raise ThesisError(path, "open_questions", f"at most {MAX_OPEN_QUESTIONS} open questions")
    questions = []
    for question_id, text in raw.items():
        where = f"open_questions.{question_id}"
        _identifier(path, where, question_id, "id")
        if not isinstance(text, str) or not text.strip():
            raise ThesisError(path, where, "must be non-empty text")
        questions.append(OpenQuestion(question_id, text.strip()))
    return tuple(questions)


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and _DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            return None
    return None


def _predictions(path: Path, raw: dict[str, Any], pillars: dict[str, str]) -> tuple[Prediction, ...]:
    if len(raw) > MAX_PREDICTIONS:
        raise ThesisError(path, "predictions", f"at most {MAX_PREDICTIONS} predictions")
    predictions = []
    for prediction_id, body in raw.items():
        where = f"predictions.{prediction_id}"
        _identifier(path, where, prediction_id, "id")
        if not isinstance(body, dict):
            raise ThesisError(path, where, "must have statement, by, and p")
        _check_string_keys(path, where, body)
        unknown = sorted(set(body) - {"statement", "by", "p", "pillar"})
        if unknown:
            raise ThesisError(path, f"{where}.{unknown[0]}", "unknown key; allowed: statement, by, p, pillar")
        statement = body.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ThesisError(path, f"{where}.statement", "must be non-empty text")
        by = _date_text(body.get("by"))
        if by is None:
            raise ThesisError(path, f"{where}.by", "must be a date written YYYY-MM-DD")
        p = body.get("p")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0 <= p <= 1:
            raise ThesisError(path, f"{where}.p", "must be your probability between 0 and 1")
        pillar = body.get("pillar")
        if pillar is not None and pillar not in pillars:
            raise ThesisError(path, f"{where}.pillar", f"must name an existing pillar, got {pillar!r}")
        predictions.append(Prediction(prediction_id, statement.strip(), by, float(p), pillar))
    return tuple(predictions)


def _known_facts(path: Path, raw: dict[str, Any], pillars: dict[str, str]) -> dict[str, tuple[Fact, ...]]:
    facts: dict[str, tuple[Fact, ...]] = {}
    for pillar, items in raw.items():
        where = f"known_facts.{pillar}"
        if pillar not in pillars:
            raise ThesisError(path, where, "is not a pillar")
        items = [] if items is None else items
        if not isinstance(items, list):
            raise ThesisError(path, where, "must be a list of facts")
        if len(items) > MAX_FACTS_PER_PILLAR:
            raise ThesisError(path, where, f"at most {MAX_FACTS_PER_PILLAR} facts per pillar")
        parsed = []
        for index, item in enumerate(items):
            parsed.append(_fact(path, f"{where}[{index}]", pillar, index, item))
        facts[pillar] = tuple(parsed)
    return facts


def _fact(path: Path, where: str, pillar: str, index: int, item: Any) -> Fact:
    fact_id = f"{pillar}.{index}"
    if isinstance(item, str):
        if not item.strip():
            raise ThesisError(path, where, "must be non-empty text")
        return Fact(fact_id, pillar, item.strip())
    if not isinstance(item, dict):
        raise ThesisError(path, where, "must be text or a mapping with text, as_of, and source")
    _check_string_keys(path, where, item)
    unknown = sorted(set(item) - {"text", "as_of", "source"})
    if unknown:
        raise ThesisError(path, f"{where}.{unknown[0]}", "unknown key; allowed: text, as_of, source")
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ThesisError(path, f"{where}.text", "must be non-empty text")
    as_of = None
    if item.get("as_of") is not None:
        as_of = _date_text(item["as_of"])
        if as_of is None:
            raise ThesisError(path, f"{where}.as_of", "must be a date written YYYY-MM-DD")
    source = item.get("source")
    if source is not None and (isinstance(source, bool) or not isinstance(source, int)):
        raise ThesisError(path, f"{where}.source", "must be a passage id")
    return Fact(fact_id, pillar, text.strip(), as_of, source)


def _metrics(path: Path, raw: dict[str, Any], pillars: dict[str, str]) -> tuple[Metric, ...]:
    if len(raw) > MAX_METRICS:
        raise ThesisError(path, "metrics", f"at most {MAX_METRICS} metrics")
    metrics = []
    for metric_id, body in raw.items():
        where = f"metrics.{metric_id}"
        _identifier(path, where, metric_id, "id")
        if not isinstance(body, dict):
            raise ThesisError(path, where, "must have label and unit")
        _check_string_keys(path, where, body)
        unknown = sorted(set(body) - {"label", "unit", "pillar", "higher_is", "definition"})
        if unknown:
            raise ThesisError(path, f"{where}.{unknown[0]}", "unknown key; allowed: label, unit, pillar, higher_is, definition")
        label, unit = body.get("label"), body.get("unit")
        if not isinstance(label, str) or not label.strip():
            raise ThesisError(path, f"{where}.label", "must be non-empty text")
        if not isinstance(unit, str) or not unit.strip():
            raise ThesisError(path, f"{where}.unit", "must be non-empty text such as %, $M, days, or units")
        pillar = body.get("pillar")
        if pillar is not None and pillar not in pillars:
            raise ThesisError(path, f"{where}.pillar", f"must name an existing pillar, got {pillar!r}")
        higher_is = body.get("higher_is", "neutral")
        if higher_is not in HIGHER_IS:
            raise ThesisError(path, f"{where}.higher_is", "must be good, bad, or neutral")
        definition = body.get("definition")
        if definition is not None and (not isinstance(definition, str) or not definition.strip()):
            raise ThesisError(path, f"{where}.definition", "must be non-empty text")
        metrics.append(Metric(metric_id, label.strip(), unit.strip(), pillar, higher_is,
                              definition.strip() if definition else None))
    return tuple(metrics)
