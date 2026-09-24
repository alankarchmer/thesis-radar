import sqlite3

import pytest

from thesis_radar.models import FollowupRecord, JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.store import SCHEMA_VERSION, SchemaTooNew, Store, sha256_text

CLOCK = "2026-09-21T12:00:00Z"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "radar.db", clock=lambda: CLOCK)
    yield s
    s.close()


def _doc(store, digest="a" * 64, status="sorted", ticker="ACME", texts=("Inventory rose.", "Pricing held."), date="2026-09-15"):
    with store.transaction():
        doc_id = store.insert_document(
            NewDocument(
                text_sha256=digest, path="archive/ACME/x.txt", title="Q3 call", origin="inbox", status=status,
                ticker=ticker, ticker_p=0.9, source_type="earnings_transcript", source_type_p=0.9,
                doc_date=date, doc_date_p=0.8, form="10-Q", accession="0001-26-000001",
            )
        )
        store.insert_passages(
            doc_id, [PassageDraft(seq=i, page=1, char_start=0, char_end=10, text=t, speaker="CFO" if i == 0 else None)
                     for i, t in enumerate(texts)]
        )
    return doc_id


def test_schema_is_versioned_and_reopens(tmp_path):
    path = tmp_path / "radar.db"
    Store(path).close()
    again = Store(path)
    assert again.schema_version == SCHEMA_VERSION == 1
    assert again.has_fts
    again.close()


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "radar.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaTooNew):
        Store(path)


def test_document_and_passages_round_trip(store):
    doc_id = _doc(store)
    assert store.find_document_by_hash("a" * 64)["id"] == doc_id
    assert store.get_document(doc_id)["form"] == "10-Q"
    rows = store.passages_for_ticker("ACME")
    assert [r["text"] for r in rows] == ["Inventory rose.", "Pricing held."]
    assert (rows[0]["speaker"], rows[0]["doc_date"], rows[0]["ingested_at"]) == ("CFO", "2026-09-15", CLOCK)
    assert [r["seq"] for r in store.passages_for_document(doc_id)] == [0, 1]


def test_only_sorted_documents_are_listed_for_judging(store):
    _doc(store, digest="b" * 64, status="unsorted")
    assert store.passages_for_ticker("ACME") == []
    assert [d["status"] for d in store.documents("unsorted")] == ["unsorted"]
    assert store.documents(tickers=["ACME"])[0]["ticker"] == "ACME"
    assert store.documents(tickers=[]) == []


def test_duplicate_hash_is_rejected(store):
    _doc(store)
    with pytest.raises(sqlite3.IntegrityError):
        _doc(store)


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError), store.transaction():
        store.insert_document(NewDocument(text_sha256="c" * 64, path="p", title="t", origin="inbox", status="unsorted"))
        raise RuntimeError("boom")
    assert store.find_document_by_hash("c" * 64) is None


def test_judgments_parts_and_latest(store, tmp_path):
    _doc(store)
    pid = store.passages_for_ticker("ACME")[0]["passage_id"]
    store.save_judgment(JudgmentRecord(pid, "k1", "jev-1.13.0", "r", "t", "failed", error="timeout", ticker="ACME", retryable=False))
    assert not store.has_judged(pid, "k1")
    assert store.judgment(pid, "k1")["retryable"] == 0
    store.save_judgment(JudgmentRecord(pid, "k1", "jev-1.13.0", "r", "t", "judged", answers={"a": 1}, input_tokens=300,
                                       request_id="req_1", ticker="ACME", part="p0"))
    store.save_judgment(JudgmentRecord(pid, "k2", "jev-1.13.0", "r", "t", "judged", answers={"b": 2}, ticker="ACME", part="p1"))
    assert store.has_judged(pid, "k1")
    other = sqlite3.connect(tmp_path / "radar.db")
    assert other.execute("SELECT status, input_tokens, request_id FROM judgments WHERE cache_key='k1'").fetchall() == [
        ("judged", 300, "req_1")
    ]
    other.close()
    assert store.latest_judged(pid) == {"a": 1, "b": 2}
    assert store.latest_judged(pid, "OTHER") is None
    assert store.answers_for_keys(pid, ["k1", "k2"]) == {"a": 1, "b": 2}
    assert store.answers_for_keys(pid, ["k1", "missing"]) is None
    assert [r["part"] for r in store.judgments_for_ticker("ACME")] == ["p0", "p1"]
    assert store.usage_since("2026-01-01T00:00:00Z") == (2, 300)


def test_views_labels_triage_resolutions_and_fetch_state(store):
    assert store.last_view() is None
    store.record_view("2026-09-21T10:00:00Z")
    store.record_view("2026-09-21T11:00:00Z")
    assert store.last_view() == "2026-09-21T11:00:00Z"
    _doc(store)
    pid = store.passages_for_ticker("ACME")[0]["passage_id"]
    store.save_label(pid, "new_info", True, ticker="ACME")
    store.save_label(pid, "new_info", False, ticker="ACME", origin="triage", weight=2.5, judgment_keys=["k"])
    [label] = store.labels()
    assert (label["passage_id"], label["question"], label["value"], label["ticker"], label["origin"], label["weight"]) == (
        pid, "new_info", 0, "ACME", "triage", 2.5,
    )
    assert label["judgment_keys"] == '["k"]'
    assert store.labeled_passage_ids() == {pid} == store.labeled_passage_ids("ACME")
    store.set_triage("ACME", pid, status="dismissed")
    store.set_triage("ACME", pid, starred=True)
    assert store.triage_map("ACME") == {pid: {"status": "dismissed", "starred": True}}
    store.set_triage("ACME", pid, status=None, starred=False)
    assert store.triage_map("ACME") == {}
    store.set_resolution("ACME", "inv_back", True)
    assert store.resolutions("ACME") == {"inv_back": {"outcome": True, "resolved_at": CLOCK}}
    store.set_resolution("ACME", "inv_back", None)
    assert store.resolutions("ACME") == {}
    assert store.fetch_state("ACME") is None
    store.set_fetch_state("ACME", "320193", "0000320193-26-000001")
    assert store.fetch_state("ACME")["last_accession"] == "0000320193-26-000001"


def test_followups_round_trip(store):
    _doc(store)
    a, b = [r["passage_id"] for r in store.passages_for_ticker("ACME")]
    store.save_followup(FollowupRecord(a, b, "ACME", "k", "jev", "judged", answers={"followup": {"choice": "confirms"}}))
    assert store.followup(a, b, "k")["status"] == "judged"
    assert len(store.followups_for_ticker("ACME")) == 1


def test_tag_and_replace_passages(store):
    doc_id = _doc(store, status="unsorted", ticker=None)
    with store.transaction():
        store.tag_document(doc_id, ticker="ACME", source_type="own_note", doc_date="2026-09-01", path="archive/ACME/y.txt")
        store.replace_passages(doc_id, [PassageDraft(0, 1, 0, 3, "One passage now.")])
    row = store.get_document(doc_id)
    assert (row["status"], row["ticker"], row["source_type"], row["doc_date"], row["path"]) == (
        "sorted", "ACME", "own_note", "2026-09-01", "archive/ACME/y.txt",
    )
    assert [r["text"] for r in store.passages_for_document(doc_id)] == ["One passage now."]
    assert [r["text"] for r in store.search('"passage"')] == ["One passage now."]


def test_search_and_earlier_passages(store):
    first = _doc(store, digest="1" * 64, texts=("Dealer inventory rose sharply in the quarter.",), date="2026-06-01")
    second = _doc(store, digest="2" * 64, texts=("Dealer inventory rose sharply again.",), date="2026-09-01")
    hits = store.search('"inventory"', tickers=["ACME"])
    assert {r["document_id"] for r in hits} == {first, second}
    assert "[" in hits[0]["snippet"]
    earlier = store.earlier_passages(ticker="ACME", document_id=second, doc_date="2026-09-01", match='"inventory"', limit=5)
    assert [r["document_id"] for r in earlier] == [first]
    later = store.earlier_passages(ticker="ACME", document_id=first, doc_date="2026-06-01", match='"inventory"', limit=5)
    assert later == []
    text = "Dealer inventory rose sharply in the quarter."
    assert store.exact_earlier_passage(ticker="ACME", document_id=second, doc_date="2026-09-01",
                                       text_sha256=sha256_text(text)) is not None


def test_get_passages_returns_requested_ids_with_document_fields(store):
    _doc(store)
    ids = [r["passage_id"] for r in store.passages_for_ticker("ACME")]
    rows = store.get_passages([ids[1]])
    assert [(r["passage_id"], r["ticker"], r["title"]) for r in rows] == [(ids[1], "ACME", "Q3 call")]
    assert store.get_passages([]) == []


def test_readonly_store(tmp_path):
    writer = Store(tmp_path / "radar.db")
    writer.close()
    reader = Store(tmp_path / "radar.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        reader.record_view("x")
    reader.close()
