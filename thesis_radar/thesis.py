"""Load and validate per-company thesis files (thesis/<TICKER>.yaml)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MAX_PILLARS = 12
MAX_ASSUMPTIONS = 10
MAX_FACTS_PER_PILLAR = 20
RESERVED_PILLARS = frozenset({"off_thesis"})
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


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
class Thesis:
    ticker: str
    company: str
    aliases: tuple[str, ...]
    pillars: dict[str, str]
    assumptions: tuple[Assumption, ...]
    known_facts: dict[str, tuple[str, ...]]

    def canonical(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "aliases": list(self.aliases),
            "pillars": dict(sorted(self.pillars.items())),
            "assumptions": {
                a.id: {"pillar": a.pillar, "statement": a.statement}
                for a in sorted(self.assumptions, key=lambda a: a.id)
            },
            "known_facts": {p: list(self.known_facts.get(p, ())) for p in sorted(self.pillars)},
        }

    @property
    def version(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_thesis(path: Path) -> Thesis:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ThesisError(path, "<file>", f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ThesisError(path, "<file>", "must be a mapping")
    _check_string_keys(path, "<root>", data)
    ticker = _required_text(path, data, "ticker")
    if not _TICKER.match(ticker):
        raise ThesisError(path, "ticker", "must be 1-10 uppercase letters, digits, '.' or '-'")
    if ticker != path.stem:
        raise ThesisError(path, "ticker", f"must match the file name ({path.stem})")
    company = _required_text(path, data, "company")
    aliases = _aliases(path, data.get("aliases", []))
    pillars = _pillars(path, data.get("pillars"))
    assumptions = _assumptions(path, data.get("assumptions") or {}, pillars)
    known_facts = _known_facts(path, data.get("known_facts") or {}, pillars)
    return Thesis(ticker, company, aliases, pillars, assumptions, known_facts)


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


def _required_text(path: Path, data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ThesisError(path, key, "is required and must be non-empty text")
    return value.strip()


def _aliases(path: Path, raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(a, str) and a.strip() for a in raw):
        raise ThesisError(path, "aliases", "must be a list of non-empty text")
    return tuple(a.strip() for a in raw)


def _pillars(path: Path, raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise ThesisError(path, "pillars", f"must map 1-{MAX_PILLARS} pillar names to descriptions")
    _check_string_keys(path, "pillars", raw)
    if len(raw) > MAX_PILLARS:
        raise ThesisError(path, "pillars", f"at most {MAX_PILLARS} pillars")
    pillars: dict[str, str] = {}
    for name, description in raw.items():
        where = f"pillars.{name}"
        if not _IDENTIFIER.match(name):
            raise ThesisError(path, where, "name must be lowercase letters, digits and underscores, starting with a letter")
        if name in RESERVED_PILLARS:
            raise ThesisError(path, where, "name is reserved")
        if not isinstance(description, str) or not description.strip():
            raise ThesisError(path, where, "description must be non-empty text")
        pillars[name] = description.strip()
    return pillars


def _assumptions(path: Path, raw: Any, pillars: dict[str, str]) -> tuple[Assumption, ...]:
    if not isinstance(raw, dict):
        raise ThesisError(path, "assumptions", "must be a mapping")
    _check_string_keys(path, "assumptions", raw)
    if len(raw) > MAX_ASSUMPTIONS:
        raise ThesisError(path, "assumptions", f"at most {MAX_ASSUMPTIONS} assumptions")
    assumptions = []
    for assumption_id, body in raw.items():
        where = f"assumptions.{assumption_id}"
        if not _IDENTIFIER.match(assumption_id):
            raise ThesisError(path, where, "id must be lowercase letters, digits and underscores, starting with a letter")
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


def _known_facts(path: Path, raw: Any, pillars: dict[str, str]) -> dict[str, tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise ThesisError(path, "known_facts", "must be a mapping")
    _check_string_keys(path, "known_facts", raw)
    facts: dict[str, tuple[str, ...]] = {}
    for pillar, items in raw.items():
        where = f"known_facts.{pillar}"
        if pillar not in pillars:
            raise ThesisError(path, where, "is not a pillar")
        items = [] if items is None else items
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            raise ThesisError(path, where, "must be a list of non-empty text")
        if len(items) > MAX_FACTS_PER_PILLAR:
            raise ThesisError(path, where, f"at most {MAX_FACTS_PER_PILLAR} facts per pillar")
        facts[pillar] = tuple(i.strip() for i in items)
    return facts
