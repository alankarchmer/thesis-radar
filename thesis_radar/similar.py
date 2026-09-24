"""Deterministic text similarity: exact repeats, near duplicates, previously seen passages, word diffs.

No model is involved. Candidates come from SQLite FTS5 (BM25) when available, then word-shingle
Jaccard similarity decides:

- an identical passage (after whitespace normalization) in an earlier document of the same ticker
  makes this one a repeat (`repeat_of`): it is never judged or shown in feeds;
- a word-sequence match ratio >= NEAR_DUPLICATE links the best match as `similar_to` (shown with a
  diff), so a paragraph repeated with updated numbers is recognized even when it is short;
- up to SEEN_LIMIT earlier passages with word-set Jaccard >= SEEN_MIN become `previously_seen`
  context for Jev, so novelty is judged against the corpus and not only the hand-kept facts.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from typing import Any

from .store import Store, sha256_text

NEAR_DUPLICATE = 0.7
NEAR_PREFILTER = 0.3
SEEN_MIN = 0.2
SEEN_LIMIT = 3
CANDIDATES = 25
QUERY_TERMS = 14

_TOKEN = re.compile(r"\d[\d,.]*\d%?|\d%?|[a-z][a-z0-9'\-]*[a-z0-9]|[a-z]")
STOPWORDS = frozenset(
    ["a", "about", "above", "after", "again", "against", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me", "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours", "out", "over", "own", "same", "she", "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours", "company", "quarter", "year", "we're", "it's"]
)


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def shingles(words: Sequence[str], k: int = 3) -> set[tuple[str, ...]]:
    if len(words) < k:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[Any], b: set[Any]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def content_words(text: str) -> set[str]:
    return {w for w in tokens(text) if w not in STOPWORDS and (len(w) > 2 or any(c.isdigit() for c in w))}


def shingle_similarity(a: str, b: str) -> float:
    return jaccard(shingles(tokens(a)), shingles(tokens(b)))


def topic_similarity(a: str, b: str) -> float:
    return jaccard(content_words(a), content_words(b))


def sequence_similarity(a: str, b: str) -> float:
    """difflib's match ratio over word tokens: 1.0 identical, high when only a few words changed."""
    return difflib.SequenceMatcher(a=tokens(a), b=tokens(b), autojunk=False).ratio()


def fts_query(text: str, max_terms: int = QUERY_TERMS) -> str | None:
    """An OR query of the passage's most distinctive words (longest first, numbers included)."""
    seen: dict[str, int] = {}
    for position, word in enumerate(tokens(text)):
        if word in STOPWORDS or word in seen:
            continue
        if len(word) > 2 or any(c.isdigit() for c in word):
            seen[word] = position
    if not seen:
        return None
    ranked = sorted(seen, key=lambda w: (-len(w), seen[w]))[:max_terms]
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in ranked)


def link_document(store: Store, document_id: int) -> int:
    """Link each passage of a sorted document to earlier passages of its ticker. Returns repeats found."""
    doc = store.get_document(document_id)
    if doc is None or doc["status"] != "sorted" or not doc["ticker"]:
        return 0
    doc_date = doc["doc_date"] or doc["ingested_at"][:10]
    repeats = 0
    with store.transaction():
        for row in store.passages_for_document(document_id):
            text = row["text"]
            repeat_of = store.exact_earlier_passage(
                ticker=doc["ticker"], document_id=document_id, doc_date=doc_date, text_sha256=sha256_text(text)
            )
            if repeat_of is not None:
                store.set_passage_links(row["passage_id"], repeat_of=repeat_of, similar_to=None, similarity=None,
                                        seen_ids=[])
                repeats += 1
                continue
            candidates = store.earlier_passages(
                ticker=doc["ticker"], document_id=document_id, doc_date=doc_date, match=fts_query(text),
                limit=CANDIDATES,
            )
            similar_to, similarity = best_near_duplicate(text, candidates)
            seen = previously_seen_ids(text, candidates)
            store.set_passage_links(
                row["passage_id"], repeat_of=None, similar_to=similar_to, similarity=similarity, seen_ids=seen
            )
    return repeats


def best_near_duplicate(text: str, candidates: Iterable[Any]) -> tuple[int | None, float | None]:
    mine = content_words(text)
    best_id, best = None, 0.0
    for candidate in candidates:
        if candidate["repeat_of"] is not None:
            continue
        if jaccard(mine, content_words(candidate["text"])) < NEAR_PREFILTER:
            continue
        score = sequence_similarity(text, candidate["text"])
        if score > best:
            best_id, best = int(candidate["passage_id"]), score
    if best_id is None or best < NEAR_DUPLICATE:
        return None, None
    return best_id, round(best, 4)


def previously_seen_ids(text: str, candidates: Iterable[Any], limit: int = SEEN_LIMIT) -> list[int]:
    mine = content_words(text)
    scored = []
    for candidate in candidates:
        if candidate["repeat_of"] is not None:
            continue
        score = jaccard(mine, content_words(candidate["text"]))
        if score >= SEEN_MIN:
            scored.append((-score, int(candidate["passage_id"])))
    return [pid for _, pid in sorted(scored)[:limit]]


def word_diff(old: str, new: str) -> list[list[str]]:
    """A word-level diff as [op, text] pairs: "=" unchanged, "-" only in old, "+" only in new."""
    a = re.findall(r"\S+\s*", old)
    b = re.findall(r"\S+\s*", new)
    ops: list[list[str]] = []

    def push(op: str, words: Sequence[str]) -> None:
        if not words:
            return
        text = "".join(words)
        if ops and ops[-1][0] == op:
            ops[-1][1] += text
        else:
            ops.append([op, text])

    matcher = difflib.SequenceMatcher(a=[w.strip() for w in a], b=[w.strip() for w in b], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            push("=", b[j1:j2])
        else:
            push("-", a[i1:i2])
            push("+", b[j1:j2])
    return ops


def rank_facts(text: str, facts: Sequence[Any], limit: int) -> list[Any]:
    """The `limit` facts most similar in wording to `text` (all of them when there are few)."""
    if len(facts) <= limit:
        return list(facts)
    mine = content_words(text)
    scored = sorted(
        ((-jaccard(mine, content_words(f.text)), index, f) for index, f in enumerate(facts)), key=lambda t: t[:2]
    )
    return [fact for _, _, fact in scored[:limit]]
