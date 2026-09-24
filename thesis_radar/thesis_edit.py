"""Edit thesis files in place (known facts), preserving the user's comments and formatting.

Edits go through ruamel.yaml's round-trip mode, which keeps comments, quoting, key order, and flow
style. The edited text is validated with `load_thesis` before it replaces the original, so a failed
edit never leaves a broken or half-written thesis behind.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .thesis import MAX_FACTS_PER_PILLAR, ThesisError, load_thesis

FACT_ID = re.compile(r"^([a-z][a-z0-9_]*)\.(\d+)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A block-opening key line, such as "known_facts:" or "  inventory:   # comment".
_OPENS_BLOCK = re.compile(r"^[^\s#-][^#]*:\s*(#.*)?$")


def add_fact(
    path: Path,
    pillar: str,
    text: str,
    *,
    source: int | None = None,
    as_of: str | None = None,
    replace: str | None = None,
) -> str:
    """Append (or replace `replace`, a fact id) a known fact; returns the new fact's id.

    Replacing a fact of the same pillar keeps its index; replacing one of another pillar removes it
    there and appends the new fact to `pillar`. Raises ValueError, leaving the file untouched, when
    the pillar or fact id is unknown, the text is empty, the pillar is full, or the result is invalid.
    """
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise ValueError("fact text must not be empty")
    if as_of is not None and not is_date(as_of):
        raise ValueError(f"as_of must be a date written YYYY-MM-DD, got {as_of!r}")
    if source is not None and (isinstance(source, bool) or not isinstance(source, int)):
        raise ValueError(f"source must be a passage id, got {source!r}")

    original = path.read_text(encoding="utf-8")
    yaml = _round_trip(original)
    try:
        data = yaml.load(original)
    except YAMLError as exc:
        raise ValueError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, CommentedMap):
        raise ValueError(f"{path.name} must be a mapping")
    pillars = data.get("pillars")
    if not isinstance(pillar, str) or not isinstance(pillars, dict) or pillar not in pillars:
        known = ", ".join(str(p) for p in pillars) if isinstance(pillars, dict) else "none"
        raise ValueError(f"unknown pillar {pillar!r}; the thesis has: {known}")

    facts = _facts_mapping(data)
    new = _fact_node(text, as_of=as_of, source=source)
    if replace is not None:
        old_pillar, old_index = _locate(facts, replace)
        if old_pillar == pillar:
            facts[pillar][old_index] = new
            index = old_index
        else:
            target = _pillar_list(facts, pillar)
            _check_room(target, pillar)
            del facts[old_pillar][old_index]
            target.append(new)
            index = len(target) - 1
    else:
        target = _pillar_list(facts, pillar)
        _check_room(target, pillar)
        target.append(new)
        index = len(target) - 1

    buffer = io.StringIO()
    yaml.dump(data, buffer)
    _replace_validated(path, buffer.getvalue())
    return f"{pillar}.{index}"


def is_date(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _round_trip(text: str) -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 1_000_000  # never wrap long facts or flow mappings
    mapping, sequence, offset = _guess_indent(text)
    yaml.indent(mapping=mapping, sequence=sequence, offset=offset)
    return yaml


def _guess_indent(text: str) -> tuple[int, int, int]:
    """(mapping, sequence, offset) indentation as the file writes it, so ruamel writes it back the same.

    `offset` is the dash's indent under its parent key; `sequence` is the item text's indent.
    """
    mapping: int | None = None
    sequence: int | None = None
    offset: int | None = None
    parent: int | None = None  # indent of the previous line when it opened a block ("key:")
    for line in text.splitlines():
        body = line.lstrip(" ")
        if not body or body.startswith("#"):
            continue
        indent = len(line) - len(body)
        is_item = body == "-" or body.startswith("- ")
        if parent is not None and indent >= parent:
            if is_item and offset is None:
                item = body[1:]
                offset = indent - parent
                sequence = offset + 1 + len(item) - len(item.lstrip(" "))
            elif not is_item and indent > parent and mapping is None:
                mapping = indent - parent
        if mapping is not None and offset is not None:
            break
        parent = indent if not is_item and _OPENS_BLOCK.match(body) else None
    mapping = mapping or 2
    if offset is None or sequence is None:
        offset, sequence = mapping, mapping + 2
    return mapping, max(sequence, offset + 2), offset


def _facts_mapping(data: CommentedMap) -> CommentedMap:
    facts = data.get("known_facts")
    if facts is None or (isinstance(facts, dict) and not facts):
        facts = CommentedMap()
        data["known_facts"] = facts  # an existing (null or empty) key keeps its place
    if not isinstance(facts, CommentedMap):
        raise ValueError("known_facts must be a mapping of pillar to facts")
    return facts


def _pillar_list(facts: CommentedMap, pillar: str) -> CommentedSeq:
    items = facts.get(pillar)
    if items is None or (isinstance(items, list) and not items):
        items = CommentedSeq()
        facts[pillar] = items
    if not isinstance(items, CommentedSeq):
        raise ValueError(f"known_facts.{pillar} must be a list of facts")
    return items


def _locate(facts: CommentedMap, fact_id: str) -> tuple[str, int]:
    match = FACT_ID.match(fact_id) if isinstance(fact_id, str) else None
    if match is None:
        raise ValueError(f"fact ids look like <pillar>.<index>, such as inventory.0; got {fact_id!r}")
    pillar, index = match[1], int(match[2])
    items = facts.get(pillar)
    if not isinstance(items, list) or index >= len(items):
        raise ValueError(f"no known fact {fact_id}")
    return pillar, index


def _check_room(items: CommentedSeq, pillar: str) -> None:
    if len(items) >= MAX_FACTS_PER_PILLAR:
        raise ValueError(
            f"{pillar} already has {MAX_FACTS_PER_PILLAR} known facts, the most a pillar can hold; "
            f"replace one instead (--replace {pillar}.<index>)"
        )


def _fact_node(text: str, *, as_of: str | None, source: int | None) -> Any:
    if as_of is None and source is None:
        return DoubleQuotedScalarString(text)
    node = CommentedMap()
    node["text"] = DoubleQuotedScalarString(text)
    if as_of is not None:
        node["as_of"] = date.fromisoformat(as_of)  # written unquoted, as YAML dates are
    if source is not None:
        node["source"] = source
    node.fa.set_flow_style()
    return node


def _replace_validated(path: Path, text: str) -> None:
    """Validate `text` as the thesis at `path`, then swap it in atomically; the original stays on failure."""
    # load_thesis checks the file is named after its ticker, so validate under the same name in a
    # private directory next to the thesis (same filesystem, so os.replace is atomic). The directory
    # name does not end in .yaml, so a concurrent load_theses never picks it up.
    scratch = Path(tempfile.mkdtemp(prefix=f".{path.stem}-edit-", suffix=".tmp", dir=path.parent))
    try:
        candidate = scratch / path.name
        candidate.write_text(text, encoding="utf-8")
        try:
            load_thesis(candidate)
        except ThesisError as exc:
            raise ValueError(f"the edit would leave {path.name} invalid: {exc.field}: {exc.reason}") from exc
        shutil.copymode(path, candidate)
        os.replace(candidate, path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
