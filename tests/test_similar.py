import json

from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.similar import (
    fts_query,
    link_document,
    rank_facts,
    sequence_similarity,
    shingle_similarity,
    topic_similarity,
    word_diff,
)
from thesis_radar.store import Store
from thesis_radar.thesis import Fact


def _doc(store, digest, date, texts, ticker="ACME"):
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest, path=f"archive/{digest[:4]}.htm", title=digest[:4], origin="edgar",
                        status="sorted", ticker=ticker, source_type="filing", doc_date=date, form="10-Q")
        )
        store.insert_passages(doc, [PassageDraft(i, 1, 0, 1, t) for i, t in enumerate(texts)])
    return doc


RISK = "Our business depends on a network of independent dealers, and the loss of dealers could hurt sales and results."
MDNA = "Dealer inventory rose 12% year over year to 41,000 units as retail demand softened across North America."


def test_repeats_near_duplicates_and_previously_seen(tmp_path):
    store = Store(tmp_path / "radar.db")
    _doc(store, "a" * 64, "2026-05-01", [RISK, MDNA, "Promotional spending increased in the snowmobile segment."])
    new = _doc(store, "b" * 64, "2026-08-01", [
        RISK,
        MDNA.replace("12%", "18%").replace("41,000", "44,500"),
        "Promotional spending in the snowmobile segment kept increasing through the summer.",
        "A brand new topic about battery suppliers in Asia.",
    ])
    assert link_document(store, new) == 1
    rows = store.passages_for_document(new)
    old = {r["text"]: r["passage_id"] for r in store.passages_for_ticker("ACME") if r["document_id"] != new}
    assert rows[0]["repeat_of"] == old[RISK]
    assert rows[1]["similar_to"] == old[MDNA] and 0.7 <= rows[1]["similarity"] < 1
    assert json.loads(rows[2]["seen_ids"]) and rows[2]["similar_to"] is None
    assert rows[3]["repeat_of"] is None and rows[3]["similar_to"] is None and json.loads(rows[3]["seen_ids"]) == []
    store.close()


def test_other_tickers_and_later_documents_are_ignored(tmp_path):
    store = Store(tmp_path / "radar.db")
    later = _doc(store, "c" * 64, "2026-09-01", [RISK])
    other = _doc(store, "d" * 64, "2026-01-01", [RISK], ticker="BRRR")
    earliest = _doc(store, "e" * 64, "2026-02-01", [RISK])
    assert link_document(store, earliest) == 0
    assert link_document(store, later) == 1
    assert link_document(store, other) == 0
    store.close()


def test_similarity_helpers():
    assert shingle_similarity(MDNA, MDNA) == 1.0
    assert 0.5 < shingle_similarity(MDNA, MDNA.replace("12%", "18%")) < 1.0
    assert 0.85 < sequence_similarity(MDNA, MDNA.replace("12%", "18%").replace("41,000", "44,500")) < 1.0
    assert topic_similarity(MDNA, "dealer inventory units demand") > 0.2
    assert fts_query("the and of") is None
    assert '"41,000"' in fts_query(MDNA)


def test_word_diff_marks_changes():
    diff = word_diff("Inventory rose 12% to 41,000 units.", "Inventory rose 18% to 44,500 units.")
    assert diff == [["=", "Inventory rose "], ["-", "12% "], ["+", "18% "], ["=", "to "], ["-", "41,000 "],
                    ["+", "44,500 "], ["=", "units."]]
    assert "".join(t for op, t in diff if op != "-") == "Inventory rose 18% to 44,500 units."


def test_rank_facts_keeps_the_closest():
    facts = [Fact(f"inventory.{i}", "inventory", t) for i, t in enumerate(
        ["Gross margin fell.", "Dealer inventory was 41,000 units.", "CEO retired.", "Tariffs rose.", "Snow was light.",
         "Promotions rose."])]
    top = rank_facts(MDNA, facts, 2)
    assert top[0].id == "inventory.1" and len(top) == 2
    assert rank_facts(MDNA, facts[:3], 5) == facts[:3]
