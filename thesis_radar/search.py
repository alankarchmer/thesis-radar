"""Full-text search and cited-quote export.

`fts_match` turns what a person types into a safe SQLite FTS5 expression, and `search` runs it
through `Store.search` (BM25 ranking with a bracketed snippet). When SQLite was built without FTS5,
the same parsed query is answered by case-insensitive substring matching instead.

Nothing here generates text: quotes are the stored passage text verbatim, followed by a citation
built from the document's metadata.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .config import Workspace
from .similar import fts_query
from .store import Store

MAX_TERMS = 64  # terms beyond this are ignored
SNIPPET_CONTEXT = 80  # characters kept on each side of the match in substring-mode snippets

_PIECE = re.compile(r'"([^"]*)"|(\S+)')
_WORD = re.compile(r"[\W_]*(.*?)([\W_]*)", re.S)
_UNSAFE = frozenset({"Cc", "Cs"})  # control characters (FTS5 stops at NUL) and lone surrogates


@dataclass(frozen=True)
class Term:
    """A word or phrase to match; `prefix` makes its last word match as a prefix."""

    text: str
    prefix: bool = False

    def fts(self) -> str:
        return '"' + self.text.replace('"', '""') + '"' + ("*" if self.prefix else "")


def parse_query(query: str) -> list[list[Term]]:
    """The query as groups that must all match, each a list of alternatives (joined by `OR`)."""
    cleaned = "".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in query)
    groups: list[list[Term]] = []
    join_next = False
    count = 0
    for piece in _PIECE.finditer(cleaned):
        phrase, word = piece.group(1), piece.group(2)
        if phrase is not None:
            term = Term(" ".join(phrase.split()))
        elif word == "OR":
            join_next = bool(groups)
            continue
        elif word == "AND":
            continue
        else:
            core, tail = _WORD.fullmatch(word).groups()  # always matches
            term = Term(core, prefix=tail.startswith("*"))
        if not any(ch.isalnum() for ch in term.text):
            continue
        if join_next:
            groups[-1].append(term)
        else:
            groups.append([term])
        join_next = False
        count += 1
        if count == MAX_TERMS:
            break
    return groups


def _render(groups: Sequence[Sequence[Term]]) -> str:
    rendered = []
    for group in groups:
        alternatives = " OR ".join(term.fts() for term in group)
        rendered.append(f"({alternatives})" if len(group) > 1 and len(groups) > 1 else alternatives)
    return " AND ".join(rendered)


def fts_match(query: str) -> str | None:
    """Turn a user query into a safe FTS5 expression (None when it has no searchable words).

    `"quoted phrases"` stay phrases, `word*` is a prefix, `OR` between terms makes alternatives, and
    everything else must all match. Other FTS5 syntax is treated as plain text.
    """
    groups = parse_query(query)
    return _render(groups) if groups else None


def search(store: Store, query: str, *, tickers: Sequence[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Ranked hits: {passage_id, document_id, ticker, title, doc_date, source_type, page, speaker, text, snippet, path}."""
    match = fts_match(query)
    if match is None:
        return []
    if store.has_fts:
        rows = store.search(match, tickers=tickers, limit=limit)
        return [_hit(row, row["snippet"]) for row in rows]
    groups = parse_query(query)
    needles = [term.text.lower() for group in groups for term in group]
    return [_hit(row, _snippet(row["text"], needles) or row["snippet"]) for row in _substring_rows(store, groups, tickers)[:limit]]


def _hit(row: Any, snippet: str) -> dict[str, Any]:
    return {
        "passage_id": row["passage_id"],
        "document_id": row["document_id"],
        "ticker": row["ticker"],
        "title": row["title"],
        "doc_date": row["doc_date"],
        "source_type": row["source_type"],
        "form": row["form"],
        "page": row["page"],
        "speaker": row["speaker"],
        "text": row["text"],
        "snippet": snippet,
        "path": row["path"],
    }


def _anchor(term: Term) -> str:
    """The longest word of a term: every passage containing the term contains it."""
    return max(re.findall(r"\w+", term.text), key=len, default=term.text)


def _substring_rows(store: Store, groups: Sequence[Sequence[Term]], tickers: Sequence[str] | None) -> list[Any]:
    """Rows containing a term of every group, newest first (for SQLite without FTS5)."""
    narrowest = min(groups, key=lambda group: (len(group), -min(len(_anchor(t)) for t in group)))
    candidates: dict[int, Any] = {}
    for term in narrowest:
        for row in store.search(_anchor(term), tickers=tickers, limit=-1):
            candidates.setdefault(row["passage_id"], row)
    hits = []
    for row in candidates.values():
        text = " ".join(row["text"].lower().split())
        if all(any(term.text.lower() in text for term in group) for group in groups):
            hits.append(row)
    return sorted(hits, key=lambda row: (row["doc_date"] or "", row["passage_id"]), reverse=True)


def _snippet(text: str, needles: Sequence[str]) -> str | None:
    """The text around the first needle found, with the match in brackets (like the FTS5 snippet)."""
    lowered = text.lower()
    if len(lowered) != len(text):
        return None
    found = [(index, len(needle)) for needle in needles if needle and (index := lowered.find(needle)) >= 0]
    if not found:
        return None
    start, length = min(found)
    end = start + length
    low, high = max(0, start - SNIPPET_CONTEXT), min(len(text), end + SNIPPET_CONTEXT)
    return (
        ("… " if low else "") + text[low:start] + "[" + text[start:end] + "]" + text[end:high]
        + (" …" if high < len(text) else "")
    )


def related_passages(store: Store, tickers: Sequence[str], text: str, limit: int = 5) -> list[int]:
    """Passage ids most related to `text` (for predictions)."""
    query = fts_query(text)
    if query is None or not tickers:
        return []
    if store.has_fts:
        return [int(row["passage_id"]) for row in store.search(query, tickers=list(tickers), limit=limit)]
    groups = parse_query(query)  # one group: the words as alternatives
    words = [term.text.lower() for group in groups for term in group]
    rows = _substring_rows(store, groups, list(tickers))
    rows.sort(key=lambda row: (-sum(word in row["text"].lower() for word in words), -row["passage_id"]))
    return [int(row["passage_id"]) for row in rows[:limit]]


def file_link(ws: Workspace, path: str, page: int | None) -> str:
    """A file:// link to a stored document, pointing at the page for PDFs."""
    uri = (ws.root / path).resolve().as_uri()
    return f"{uri}#page={page}" if page and path.lower().endswith(".pdf") else uri


def quote_markdown(ws: Workspace, store: Store, passage_ids: Sequence[int]) -> str:
    """Verbatim passages as Markdown block quotes with citations and file links."""
    rows = {row["passage_id"]: row for row in store.get_passages(passage_ids)}
    blocks = []
    unknown: list[int] = []
    for pid in passage_ids:
        row = rows.get(pid)
        if row is None:
            if pid not in unknown:
                unknown.append(pid)
            continue
        text = f"{row['speaker']}: {row['text']}" if row["speaker"] else row["text"]
        lines = [f"> {line}" if line.strip() else ">" for line in text.splitlines() or [""]]
        citation = [row["title"], row["source_type"], row["doc_date"] or "undated", f"p. {row['page']}"]
        link = file_link(ws, row["path"], row["page"])
        lines += [">", "> — " + " · ".join(part for part in citation if part) + f" · [source]({link})"]
        blocks.append("\n".join(lines))
    if unknown:
        blocks.append("Unknown passage ids: " + ", ".join(str(pid) for pid in unknown))
    return "\n\n".join(blocks)
