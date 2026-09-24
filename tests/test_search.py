import random
import sqlite3

import pytest

from thesis_radar.config import Workspace
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.search import fts_match, quote_markdown, related_passages, search
from thesis_radar.store import Store


def add_doc(store, digest, texts, *, ticker="ACME", date_="2026-09-01", path=None, title=None, status="sorted",
            speakers=None, pages=None, source_type="earnings_transcript"):
    speakers = speakers or [None] * len(texts)
    pages = pages or [1] * len(texts)
    with store.transaction():
        doc = store.insert_document(
            NewDocument(
                text_sha256=digest * 64, path=path or f"archive/{ticker}/{digest}.txt", title=title or f"Doc {digest}",
                origin="inbox", status=status, ticker=ticker if status == "sorted" else None,
                source_type=source_type, doc_date=date_,
            )
        )
        ids = store.insert_passages(
            doc, [PassageDraft(i, pages[i], 0, len(t), t, speakers[i]) for i, t in enumerate(texts)]
        )
    return doc, ids


@pytest.fixture
def corpus(tmp_path):
    store = Store(tmp_path / "radar.db")
    assert store.has_fts
    _, call = add_doc(
        store, "a",
        ["Dealer inventory rose sharply in the quarter.", "Inflation pressured margins; pricing held firm.",
         "We see deflation in freight costs."],
        path="archive/ACME/q2-call.pdf", title="Q2 call", speakers=["CFO", None, None], pages=[1, 2, 3],
    )
    _, filing = add_doc(
        store, "b", ["Inventory at dealers normalized.", "Promotions increased year-over-year."],
        date_="2026-09-10", title="10-Q", source_type="filing",
    )
    _, peer = add_doc(store, "c", ["Dealer inventory at Brrr fell."], ticker="BRRR", date_="2026-09-05")
    add_doc(store, "d", ["Dealer inventory in an unsorted document."], status="unsorted")
    ids = {"call": call, "filing": filing, "peer": peer}
    yield store, ids, tmp_path
    store.close()


def hit_ids(hits):
    return [hit["passage_id"] for hit in hits]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("inflation", '"inflation"'),
        ("dealer inventory", '"dealer" AND "inventory"'),
        ('"dealer inventory" pricing', '"dealer inventory" AND "pricing"'),
        ("infl*", '"infl"*'),
        ("(infl*)", '"infl"*'),
        ("inflation OR deflation", '"inflation" OR "deflation"'),
        ("pricing inflation OR deflation margins", '"pricing" AND ("inflation" OR "deflation") AND "margins"'),
        ("OR inflation OR", '"inflation"'),
        ("inflation or deflation", '"inflation" AND "or" AND "deflation"'),
        ("a AND b", '"a" AND "b"'),
        ('foo"bar', '"foo""bar"'),
        ('"OR"', '"OR"'),
        ("NEAR(a b)", '"NEAR(a" AND "b"'),
        ("-deflation ^pricing col:value", '"deflation" AND "pricing" AND "col:value"'),
        ("x\x00y", '"x" AND "y"'),
        ("", None),
        ("   ", None),
        ("*** () : ^ OR", None),
        ('"" "++"', None),
    ],
)
def test_fts_match(query, expected):
    assert fts_match(query) == expected


def test_phrase_and_and(corpus):
    store, ids, _ = corpus
    assert sorted(hit_ids(search(store, '"dealer inventory"'))) == sorted([ids["call"][0], ids["peer"][0]])
    assert hit_ids(search(store, "inventory dealers")) == [ids["filing"][0]]
    assert search(store, '"inventory dealers"') == []


def test_prefix_and_or(corpus):
    store, ids, _ = corpus
    assert hit_ids(search(store, "infl*")) == [ids["call"][1]]
    assert search(store, "infl") == []
    assert sorted(hit_ids(search(store, "inflation OR deflation"))) == [ids["call"][1], ids["call"][2]]
    assert hit_ids(search(store, "pricing inflation OR deflation")) == [ids["call"][1]]
    assert hit_ids(search(store, "year-over-year")) == [ids["filing"][1]]


def test_hits_carry_citation_fields_and_a_snippet(corpus):
    store, ids, _ = corpus
    [hit] = search(store, "freight")
    assert hit == {
        "passage_id": ids["call"][2], "document_id": hit["document_id"], "ticker": "ACME", "title": "Q2 call",
        "doc_date": "2026-09-01", "source_type": "earnings_transcript", "form": None, "page": 3, "speaker": None,
        "text": "We see deflation in freight costs.", "snippet": hit["snippet"], "path": "archive/ACME/q2-call.pdf",
    }
    assert "[freight]" in hit["snippet"]


def test_ticker_filter_limit_and_unsorted_documents(corpus):
    store, ids, _ = corpus
    assert hit_ids(search(store, "dealer", tickers=["BRRR"])) == [ids["peer"][0]]
    assert hit_ids(search(store, "dealer", tickers=["ACME"])) == [ids["call"][0]]
    assert search(store, "dealer", tickers=[]) == []
    assert len(search(store, "dealer", limit=1)) == 1
    assert search(store, "unsorted") == []


def test_nothing_searchable_returns_nothing(corpus):
    store, _, _ = corpus
    assert search(store, "*** ()") == []
    assert search(store, "") == []


FUZZ_PIECES = [
    '"', '""', "'", "*", "(", ")", "^", ":", "+", "-", "{", "}", "[", "]", ",", ".", "\\", "%", "_", "OR", "AND",
    "NOT", "NEAR", "NEAR/2", "or", " ", "  ", "\t", "\n", "\x00", "\ud800", "é", "中文", "🙂", "dealer", "infl",
    "inventory", "col:", "text:", "0", "42%", "$5", "a", "b",
]


def test_fuzzed_queries_never_break_fts(corpus):
    store, _, _ = corpus
    rng = random.Random(1234)
    for _ in range(400):
        query = "".join(rng.choice(FUZZ_PIECES) for _ in range(rng.randint(0, 12)))
        match = fts_match(query)
        if match is None:
            assert search(store, query) == []
            continue
        try:
            store.search(match)
        except sqlite3.OperationalError as exc:  # pragma: no cover - the failure message is the point
            pytest.fail(f"{query!r} -> {match!r} raised {exc}")
        search(store, query)


def test_substring_fallback_without_fts(corpus):
    store, ids, _ = corpus
    store.has_fts = False
    # Substring matching, newest first: "dealers" contains "dealer".
    assert hit_ids(search(store, "dealer inventory")) == [ids["filing"][0], ids["peer"][0], ids["call"][0]]
    assert hit_ids(search(store, '"inventory at dealers"')) == [ids["filing"][0]]
    assert hit_ids(search(store, "infl*")) == [ids["call"][1]]
    assert sorted(hit_ids(search(store, "inflation OR deflation"))) == [ids["call"][1], ids["call"][2]]
    assert hit_ids(search(store, "pricing inflation OR deflation")) == [ids["call"][1]]
    assert hit_ids(search(store, "dealer", tickers=["ACME"])) == [ids["filing"][0], ids["call"][0]]
    assert search(store, "unsorted") == []
    assert len(search(store, "dealer", limit=2)) == 2
    [hit] = search(store, "freight")
    assert "[freight]" in hit["snippet"]


def test_related_passages(corpus):
    store, ids, _ = corpus
    related = related_passages(store, ["ACME", "BRRR"], "Dealer inventory is back to normal.", 5)
    assert set(related) >= {ids["call"][0], ids["peer"][0]}
    assert ids["peer"][0] not in related_passages(store, ["ACME"], "Dealer inventory is back to normal.")
    assert len(related_passages(store, ["ACME", "BRRR"], "Dealer inventory is back to normal.", 1)) == 1
    assert related_passages(store, ["ACME"], "It is what it is.") == []
    assert related_passages(store, [], "Dealer inventory") == []
    store.has_fts = False
    assert related_passages(store, ["ACME"], "Dealer inventory rose in the quarter.", 2)[0] == ids["call"][0]


def test_quote_markdown_formats_verbatim_quotes_with_citations(corpus, tmp_path):
    store, ids, root = corpus
    _, [multi] = add_doc(store, "e", ["First line.\nSecond line.\n\nFourth line."], date_=None, title="My notes",
                         source_type="own_note")
    ws = Workspace(root)
    pdf = (root / "archive/ACME/q2-call.pdf").resolve().as_uri()
    txt = (root / "archive/ACME/e.txt").resolve().as_uri()
    text = quote_markdown(ws, store, [ids["call"][2], ids["call"][0], 999, multi, 998])
    assert text == (
        "> We see deflation in freight costs.\n"
        ">\n"
        f"> — Q2 call · earnings_transcript · 2026-09-01 · p. 3 · [source]({pdf}#page=3)\n"
        "\n"
        "> CFO: Dealer inventory rose sharply in the quarter.\n"
        ">\n"
        f"> — Q2 call · earnings_transcript · 2026-09-01 · p. 1 · [source]({pdf}#page=1)\n"
        "\n"
        "> First line.\n"
        "> Second line.\n"
        ">\n"
        "> Fourth line.\n"
        ">\n"
        f"> — My notes · own_note · undated · p. 1 · [source]({txt})\n"
        "\n"
        "Unknown passage ids: 999, 998"
    )
    assert quote_markdown(ws, store, []) == ""
    assert quote_markdown(ws, store, [5000]) == "Unknown passage ids: 5000"
