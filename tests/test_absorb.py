from helpers import choice, write_thesis
from thesis_radar.absorb import absorb_text
from thesis_radar.models import JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses


def test_absorb_groups_passages_beside_current_facts(tmp_path):
    write_thesis(tmp_path / "thesis")
    theses, _ = load_theses(tmp_path / "thesis")
    store = Store(tmp_path / "radar.db")
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256="a" * 64, path="archive/ACME/x.txt", title="Q3 call", origin="inbox", status="sorted",
                        ticker="ACME", source_type="earnings_transcript", doc_date="2026-09-15")
        )
        store.insert_passages(doc, [PassageDraft(0, 3, 0, 5, "Inventory rose 12%.", speaker="CFO"), PassageDraft(1, 3, 6, 9, "Thanks, everyone.")])
    first, second = [r["passage_id"] for r in store.passages_for_ticker("ACME")]
    store.save_judgment(JudgmentRecord(first, "k", "jev-1.13.0", "r", "t", "judged", {"pillar": choice("inventory", {"inventory": 0.9, "pricing": 0.1})}))
    text = absorb_text(store, theses, [first, second, 999])
    assert "# ACME / inventory" in text
    assert "Current known_facts.inventory in thesis/ACME.yaml:" in text
    assert "  - Dealer inventory was elevated at the end of Q2." in text
    assert f"[{first}] earnings_transcript · 2026-09-15 · Q3 call · p.3" in text
    assert "CFO: Inventory rose 12%." in text
    assert "# ACME / off_thesis" in text
    assert "Unknown passage ids: 999" in text
    store.close()
