from helpers import ACME_THESIS, choice, write_thesis

from thesis_radar.absorb import absorb_text, thesis_for
from thesis_radar.models import JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses


def test_absorb_groups_passages_beside_current_facts_with_commands(tmp_path):
    write_thesis(tmp_path / "thesis", text=ACME_THESIS.replace("known_facts:", "peers: [BRRR]\nknown_facts:"))
    theses, _ = load_theses(tmp_path / "thesis")
    store = Store(tmp_path / "radar.db")
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256="a" * 64, path="archive/ACME/x.txt", title="Q3 call", origin="inbox", status="sorted",
                        ticker="ACME", source_type="earnings_transcript", doc_date="2026-09-15")
        )
        store.insert_passages(doc, [PassageDraft(0, 3, 0, 5, "Inventory rose 12%.", speaker="CFO"),
                                    PassageDraft(1, 3, 6, 9, "Thanks, everyone.")])
        peer = store.insert_document(
            NewDocument(text_sha256="b" * 64, path="archive/BRRR/y.htm", title="Brrr 8-K", origin="edgar", status="sorted",
                        ticker="BRRR", source_type="filing", doc_date="2026-09-10")
        )
        store.insert_passages(peer, [PassageDraft(0, 1, 0, 5, "Brrr dealers cut orders.")])
    first, second, third = [r["passage_id"] for r in store.passages_for_tickers(["ACME", "BRRR"])]
    answers = {
        "pillar": choice("inventory", {"inventory": 0.9, "pricing": 0.1}),
        "updates_fact": choice("inventory.0", {"inventory.0": 0.8, "none": 0.2}),
    }
    store.save_judgment(JudgmentRecord(first, "k", "jev-1.13.0", "r", "t", "judged", answers, ticker="ACME"))
    store.save_judgment(JudgmentRecord(third, "k2", "jev-1.13.0", "r", "t", "judged", answers, ticker="ACME"))
    text = absorb_text(store, theses, [first, second, third, 999])
    assert "# ACME / inventory" in text
    assert "Current known_facts.inventory in thesis/ACME.yaml:" in text
    assert "  [inventory.0] Dealer inventory was elevated at the end of Q2." in text
    assert f"[{first}] earnings_transcript · 2026-09-15 · Q3 call · p.3" in text
    assert "CFO: Inventory rose 12%." in text
    assert "updates [inventory.0]" in text
    assert f"radar fact ACME inventory '<one short line>' --source {first} --replace inventory.0" in text
    assert "read-through: BRRR" in text
    assert "# ACME / off_thesis" in text
    assert "Unknown passage ids: 999" in text
    assert thesis_for(theses, "BRRR").ticker == "ACME" and thesis_for(theses, "ZZZ") is None
    store.close()
