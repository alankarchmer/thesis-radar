import sqlite3

import pytest

from thesis_radar.models import JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.store import Store, sha256_text

CLOCK = "2026-09-21T12:00:00Z"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "radar.db", clock=lambda: CLOCK)
    yield s
    s.close()


def _doc(store, digest="a" * 64, status="sorted", ticker="ACME"):
    with store.transaction():
        doc_id = store.insert_document(
            NewDocument(
                text_sha256=digest, path="archive/ACME/x.txt", title="Q3 call", origin="inbox", status=status,
                ticker=ticker, ticker_p=0.9, source_type="earnings_transcript", source_type_p=0.9,
                doc_date="2026-09-15", doc_date_p=0.8,
            )
        )
        store.insert_passages(
            doc_id,
            [
                PassageDraft(seq=0, page=1, char_start=0, char_end=10, text="Inventory rose.", speaker="CFO"),
                PassageDraft(seq=1, page=1, char_start=12, char_end=30, text="Pricing held."),
            ],
        )
    return doc_id


def test_document_and_passages_round_trip(store):
    doc_id = _doc(store)
    assert store.find_document_by_hash("a" * 64)["id"] == doc_id
    rows = store.passages_for_ticker("ACME")
    assert [r["text"] for r in rows] == ["Inventory rose.", "Pricing held."]
    assert (rows[0]["speaker"], rows[0]["doc_date"], rows[0]["ingested_at"]) == ("CFO", "2026-09-15", CLOCK)


def test_only_sorted_documents_are_listed_for_judging(store):
    _doc(store, digest="b" * 64, status="unsorted")
    assert store.passages_for_ticker("ACME") == []
    assert [d["status"] for d in store.documents("unsorted")] == ["unsorted"]


def test_duplicate_hash_is_rejected(store):
    _doc(store)
    with pytest.raises(sqlite3.IntegrityError):
        _doc(store)


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.insert_document(NewDocument(text_sha256="c" * 64, path="p", title="t", origin="inbox", status="unsorted"))
            raise RuntimeError("boom")
    assert store.find_document_by_hash("c" * 64) is None


def test_judgments_replace_failures_and_are_committed(store, tmp_path):
    _doc(store)
    pid = store.passages_for_ticker("ACME")[0]["passage_id"]
    store.save_judgment(JudgmentRecord(pid, "k1", "jev-1.13.0", "r", "t", "failed", error="timeout"))
    assert not store.has_judged(pid, "k1")
    store.save_judgment(
        JudgmentRecord(pid, "k1", "jev-1.13.0", "r", "t", "judged", answers={"new_info": {"type": "noul", "noul": 0.9}},
                       input_tokens=300, request_id="req_1")
    )
    assert store.has_judged(pid, "k1")
    other = sqlite3.connect(tmp_path / "radar.db")
    assert other.execute("SELECT status, input_tokens, request_id FROM judgments").fetchall() == [("judged", 300, "req_1")]
    other.close()
    assert store.latest_judged(pid) == {"new_info": {"type": "noul", "noul": 0.9}}


def test_views_labels_and_fetch_state(store):
    assert store.last_view() is None
    store.record_view("2026-09-21T10:00:00Z")
    store.record_view("2026-09-21T11:00:00Z")
    assert store.last_view() == "2026-09-21T11:00:00Z"
    _doc(store)
    pid = store.passages_for_ticker("ACME")[0]["passage_id"]
    store.save_label(pid, "new_info", True)
    store.save_label(pid, "new_info", False)
    assert [(r["passage_id"], r["question"], r["value"], r["ticker"]) for r in store.labels()] == [(pid, "new_info", 0, "ACME")]
    assert store.labels()[0]["document_id"] == store.passages_for_ticker("ACME")[0]["document_id"]
    assert store.labeled_passage_ids() == {pid}
    assert store.fetch_state("ACME") is None
    store.set_fetch_state("ACME", "320193", "0000320193-26-000001")
    assert store.fetch_state("ACME")["last_accession"] == "0000320193-26-000001"


def test_tag_document_marks_it_sorted(store):
    doc_id = _doc(store, status="unsorted", ticker=None)
    with store.transaction():
        store.tag_document(doc_id, ticker="ACME", source_type="own_note", doc_date="2026-09-01", path="archive/ACME/y.txt")
    row = store.get_document(doc_id)
    assert (row["status"], row["ticker"], row["source_type"], row["doc_date"], row["path"]) == (
        "sorted", "ACME", "own_note", "2026-09-01", "archive/ACME/y.txt",
    )


def test_get_passages_returns_requested_ids_with_document_fields(store):
    _doc(store)
    ids = [r["passage_id"] for r in store.passages_for_ticker("ACME")]
    rows = store.get_passages([ids[1]])
    assert [(r["passage_id"], r["ticker"], r["title"]) for r in rows] == [(ids[1], "ACME", "Q3 call")]
    assert store.get_passages([]) == []


def test_sha256_text_is_hex():
    assert len(sha256_text("x")) == 64
