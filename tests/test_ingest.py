import asyncio
import os
from datetime import UTC, datetime

import pytest
from helpers import choice_from, make_pdf, write_thesis

from thesis_radar.config import Workspace
from thesis_radar.ingest import ingest_inbox, store_filing, tag_document
from thesis_radar.judge import FakeJudge, JudgeError, JudgeFatal
from thesis_radar.policy import Policy
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses


def doc_responder(ticker="ACME", ticker_p=0.95, source="own_note", source_p=0.9, date_p=0.9):
    def respond(request):
        questions = request.questions
        answers = {
            "ticker": choice_from(questions["ticker"], ticker, ticker_p),
            "source_type": choice_from(questions["source_type"], source, source_p),
        }
        if "doc_date" in questions:
            first = next(key for key in questions["doc_date"]["criteria"] if key != "none")
            answers["doc_date"] = choice_from(questions["doc_date"], first, date_p)
        return answers

    return respond


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    write_thesis(ws.thesis_dir)
    theses, _ = load_theses(ws.thesis_dir)
    store = Store(ws.db_path)
    yield ws, store, theses
    store.close()


def ingest(env, respond):
    ws, store, theses = env
    judge = FakeJudge(respond)
    report = asyncio.run(ingest_inbox(ws, store, theses, judge, model="jev-1.13.0", policy=Policy()))
    return report, judge


def test_sorted_document_is_archived_with_passages(env):
    ws, store, _ = env
    (ws.inbox / "note.txt").write_text(
        "ACME dealer notes\nSeptember 15, 2026\n\nInventory still heavy at dealers.", encoding="utf-8"
    )
    report, _ = ingest(env, doc_responder())
    assert (report.sorted, report.unsorted, report.failed) == (1, 0, 0)
    document = store.documents()[0]
    assert (document["status"], document["ticker"], document["source_type"], document["doc_date"]) == (
        "sorted", "ACME", "own_note", "2026-09-15",
    )
    assert document["path"] == "archive/ACME/2026-09-15_own_note_acme-dealer-notes.txt"
    assert (ws.root / document["path"]).exists() and not (ws.inbox / "note.txt").exists()
    assert len(store.passages_for_ticker("ACME")) == 1


def test_many_files_are_resolved_concurrently(env):
    ws, store, _ = env
    for n in range(6):
        (ws.inbox / f"note{n}.txt").write_text(f"ACME note number {n} about dealers.", encoding="utf-8")
    report, judge = ingest(env, doc_responder())
    assert report.sorted == 6 and len(judge.requests) == 6


def test_transcripts_keep_speakers(env):
    ws, store, _ = env
    (ws.inbox / "call.txt").write_text("ACME Q3 call\n\nOperator: Welcome.\nJane Doe - CFO: Inventory rose.\n", encoding="utf-8")
    ingest(env, doc_responder(source="earnings_transcript"))
    assert [row["speaker"] for row in store.passages_for_ticker("ACME")] == [None, "Operator", "Jane Doe - CFO"]


def test_unknown_company_is_unsorted(env):
    ws, store, _ = env
    (ws.inbox / "weather.txt").write_text("Sunny on Tuesday.", encoding="utf-8")
    report, _ = ingest(env, doc_responder(ticker="none"))
    assert report.unsorted == 1
    document = store.documents()[0]
    assert document["status"] == "unsorted" and "no matching company" in document["status_reason"]
    assert document["path"] == "archive/_unsorted/weather.txt"


def test_low_probability_is_unsorted(env):
    ws, store, _ = env
    (ws.inbox / "maybe.txt").write_text("ACME maybe.", encoding="utf-8")
    ingest(env, doc_responder(ticker_p=0.55))
    assert "unsure of company (0.55)" in store.documents()[0]["status_reason"]


def test_duplicates_are_set_aside_without_calling_jev(env):
    ws, store, _ = env
    (ws.inbox / "a.txt").write_text("ACME inventory rose.", encoding="utf-8")
    ingest(env, doc_responder())
    (ws.inbox / "b.txt").write_text("ACME  inventory\nrose.", encoding="utf-8")
    report, judge = ingest(env, doc_responder())
    assert report.duplicates == 1 and judge.requests == []
    assert len(store.documents()) == 1 and (ws.archive / "_duplicates" / "b.txt").exists()


def test_duplicates_within_one_batch(env):
    ws, store, _ = env
    (ws.inbox / "a.txt").write_text("ACME inventory rose.", encoding="utf-8")
    (ws.inbox / "b.txt").write_text("ACME inventory rose.", encoding="utf-8")
    report, judge = ingest(env, doc_responder())
    assert (report.sorted, report.duplicates, len(judge.requests)) == (1, 1, 1)


def test_repeated_passages_are_linked_on_ingest(env):
    ws, store, _ = env
    long = "Dealers are discounting heavily across every region we track. " * 18
    (ws.inbox / "a.txt").write_text(f"ACME notes\nAugust 1, 2026\n\n{long}", encoding="utf-8")
    ingest(env, doc_responder())
    (ws.inbox / "b.txt").write_text(f"ACME notes two\nSeptember 1, 2026\n\n{long}", encoding="utf-8")
    report, _ = ingest(env, doc_responder())
    assert report.repeats == 1


def test_unreadable_files_go_to_failed_with_a_reason(env):
    ws, _, _ = env
    (ws.inbox / "data.xyz").write_text("?", encoding="utf-8")
    report, _ = ingest(env, doc_responder())
    assert report.failed == 1
    assert (ws.failed / "data.xyz").exists()
    assert "unsupported" in (ws.failed / "data.xyz.reason.txt").read_text(encoding="utf-8")


def test_scanned_pdfs_need_ocr_and_skip_jev(env):
    ws, store, _ = env
    (ws.inbox / "scan.pdf").write_bytes(make_pdf([""]))
    report, judge = ingest(env, doc_responder())
    assert report.unsorted == 1 and judge.requests == []
    assert store.documents()[0]["status_reason"] == "needs OCR"


def test_judge_errors_leave_the_file_for_next_time(env):
    ws, store, _ = env
    (ws.inbox / "note.txt").write_text("ACME note.", encoding="utf-8")

    def boom(request):
        raise JudgeError("TypeSafeRateLimitError: 429")

    report, _ = ingest(env, boom)
    assert report.deferred == 1 and (ws.inbox / "note.txt").exists() and store.documents() == []


def test_fatal_errors_stop_ingest_and_touch_nothing(env):
    ws, store, _ = env
    (ws.inbox / "note.txt").write_text("ACME note.", encoding="utf-8")

    def reject(request):
        raise JudgeFatal("401")

    with pytest.raises(JudgeFatal):
        ingest(env, reject)
    assert (ws.inbox / "note.txt").exists() and store.documents() == []


def test_undated_documents_fall_back_to_file_time(env):
    ws, store, _ = env
    path = ws.inbox / "note.txt"
    path.write_text("ACME note without a date.", encoding="utf-8")
    stamp = datetime(2026, 9, 21, 12, tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp))
    report, judge = ingest(env, doc_responder())
    assert "doc_date" not in judge.requests[0].questions
    assert report.sorted == 1 and store.documents()[0]["doc_date"] == "2026-09-21"


def test_filings_are_stored_sorted_and_deduplicated(env):
    ws, store, _ = env
    content = b"<html><body><p>ACME 10-Q.</p><p>Dealer inventory rose.</p></body></html>"
    kwargs = {"ticker": "ACME", "form": "10-Q", "filing_date": "2026-08-01"}
    assert store_filing(ws, store, name="acme-10q.htm", content=content, accession="0001-26-000002", **kwargs) == "stored"
    assert store_filing(ws, store, name="copy.htm", content=content, **kwargs) == "duplicate"
    assert store_filing(ws, store, name="data.xml", content=b"<x/>", **kwargs) == "skipped"
    document = store.documents()[0]
    assert (document["origin"], document["status"], document["source_type"], document["doc_date"], document["title"]) == (
        "edgar", "sorted", "filing", "2026-08-01", "ACME 10-Q 2026-08-01 acme-10q.htm",
    )
    assert (document["form"], document["accession"]) == ("10-Q", "0001-26-000002")
    assert (ws.root / document["path"]).read_bytes() == content
    assert len(store.passages_for_ticker("ACME")) == 1


def test_tagging_sorts_resplits_and_moves_a_document(env):
    ws, store, _ = env
    (ws.inbox / "call.txt").write_text("Weather call\n\nOperator: Welcome.\nJane Doe - CFO: Sunny.\n", encoding="utf-8")
    ingest(env, doc_responder(ticker="none"))
    document_id = store.documents()[0]["id"]
    assert all(p["speaker"] is None for p in store.passages_for_document(document_id))
    dest = tag_document(ws, store, document_id, ticker="ACME", source_type="earnings_transcript", doc_date="2026-09-01")
    assert dest == ws.archive / "ACME" / "2026-09-01_earnings_transcript_weather-call.txt"
    assert dest.exists() and store.get_document(document_id)["status"] == "sorted"
    assert [p["speaker"] for p in store.passages_for_document(document_id)] == [None, "Operator", "Jane Doe - CFO"]
    with pytest.raises(ValueError, match="already sorted"):
        tag_document(ws, store, document_id, ticker="ACME", source_type="own_note", doc_date="2026-09-01")


def test_tag_validation(env):
    ws, store, _ = env
    (ws.inbox / "weather.txt").write_text("Sunny.", encoding="utf-8")
    ingest(env, doc_responder(ticker="none"))
    document_id = store.documents()[0]["id"]
    with pytest.raises(ValueError, match="source type"):
        tag_document(ws, store, document_id, ticker="ACME", source_type="blog", doc_date="2026-09-01")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tag_document(ws, store, document_id, ticker="ACME", source_type="own_note", doc_date="Sept 1")
    with pytest.raises(ValueError, match="no document"):
        tag_document(ws, store, 999, ticker="ACME", source_type="own_note", doc_date="2026-09-01")
