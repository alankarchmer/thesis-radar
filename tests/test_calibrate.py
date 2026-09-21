import pytest

from helpers import choice, noul, score, write_thesis
from thesis_radar.calibrate import (
    brier, calibration_report, confident_mistakes, expected_calibration_error, half, metrics_at, run_labeling,
    sample_for_labeling, select_threshold, select_threshold_for_recall, signal, wilson,
)
from thesis_radar.models import JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.policy import Policy
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses

ANSWERS = {
    "materiality": score([0, 0, 1, 0]),
    "assumption__inv_normalizes": choice("neither", {"supports": 0.1, "contradicts": 0.1, "neither": 0.8}),
}


def make_store(tmp_path, count, *, one_document=False):
    """`count` judged passages; each in its own document unless `one_document`."""
    write_thesis(tmp_path / "thesis")
    theses, _ = load_theses(tmp_path / "thesis")
    store = Store(tmp_path / "radar.db")
    with store.transaction():
        documents = 1 if one_document else count
        for d in range(documents):
            doc = store.insert_document(
                NewDocument(text_sha256=f"{d:064d}", path=f"archive/ACME/{d}.txt", title=f"Note {d}", origin="inbox",
                            status="sorted", ticker="ACME", source_type="own_note", doc_date="2026-09-15")
            )
            per_document = range(count) if one_document else [d]
            store.insert_passages(doc, [PassageDraft(i if one_document else 0, 1, 0, 1, f"Passage {i}.") for i in per_document])
    ids = [r["passage_id"] for r in store.passages_for_ticker("ACME")]
    for i, pid in enumerate(ids):
        store.save_judgment(JudgmentRecord(pid, "k", "jev-1.13.0", "r", "t", "judged", {"new_info": noul(i / count), **ANSWERS}))
    return store, theses, ids


def test_metrics_and_precision_threshold():
    pairs = [(0.9, True), (0.8, True), (0.7, False), (0.6, True), (0.2, False)]
    m = metrics_at(pairs, 0.65)
    assert (m.precision, m.recall, m.flagged, m.true_positives, m.positives) == (2 / 3, 2 / 3, 3, 2, 3)
    assert select_threshold(pairs, 0.99, [0.1, 0.5, 0.75, 0.85]) == 0.75
    assert select_threshold([(0.9, False)], 0.5, [0.5]) is None


def test_recall_threshold_is_the_highest_that_keeps_enough_positives():
    pairs = [(0.9, True), (0.8, True), (0.7, False), (0.6, True), (0.2, False)]
    assert select_threshold_for_recall(pairs, 1.0, [0.1, 0.5, 0.65, 0.85]) == 0.5
    assert select_threshold_for_recall(pairs, 0.6, [0.1, 0.5, 0.65, 0.85]) == 0.65
    assert select_threshold_for_recall([(0.5, False)], 0.9, [0.5]) is None


def test_probability_error_measures():
    assert brier([(1.0, True), (0.0, False)]) == 0
    assert brier([(1.0, False)]) == 1
    assert brier([]) is None
    assert confident_mistakes([(0.95, False), (0.05, True), (0.5, True), (0.95, True)]) == (1, 1)
    assert expected_calibration_error([(0.9, True), (0.9, False)]) == pytest.approx(0.4)
    low, high = wilson(8, 10)
    assert 0.4 < low < 0.8 < high < 1.0
    assert wilson(0, 0) is None


def test_halves_are_deterministic_and_balanced():
    halves = [half(key) for key in range(1000)]
    assert halves == [half(key) for key in range(1000)]
    assert 400 < sum(halves) < 600


def test_signal_reads_each_label_question():
    answers = {
        "new_info": noul(0.7),
        "materiality": score([0, 0, 1, 0]),
        "assumption__inv": choice("contradicts", {"contradicts": 0.8, "supports": 0.1, "neither": 0.1}),
    }
    assert signal(answers, "new_info") == 0.7
    assert signal(answers, "material") == 2.0
    assert signal(answers, "contradicts__inv") == 0.8
    assert signal(answers, "contradicts__other") is None


def test_sampling_takes_half_from_flagged():
    picked = sample_for_labeling(list(range(100)), set(range(10)), n=20, seed=1)
    assert len(picked) == 20 and len(set(picked)) == 20
    assert sum(1 for pid in picked if pid < 10) == 10


def test_labeling_saves_answers_and_stops_on_quit(tmp_path):
    store, theses, ids = make_store(tmp_path, 2)
    replies = iter(["y", "n", "maybe", "n", "q"])
    shown = []
    done = run_labeling(store, theses["ACME"], store.get_passages(ids), ask=lambda prompt: next(replies), show=shown.append)
    assert done == 1
    assert {(r["question"], r["value"]) for r in store.labels()} == {
        ("new_info", 1), ("material", 0), ("contradicts__inv_normalizes", 0),
    }
    assert "Passage 0." in shown[0]
    store.close()


def test_report_selects_on_one_half_and_reports_on_the_other(tmp_path):
    store, _, ids = make_store(tmp_path, 80)
    for i, pid in enumerate(ids):
        store.save_label(pid, "new_info", i >= 40)
    text = calibration_report(store, Policy(), target_precision=0.8, target_recall=0.9)
    assert "new_info: 80 labels (40 yes) from 80 documents" in text
    assert "current threshold 0.60 on held-out half: precision" in text
    assert "for 80% precision: threshold" in text
    assert "to catch 90%: threshold" in text
    assert "Brier" in text and "calibration error" in text
    assert "confidently wrong on held-out half:" in text
    assert "reliability (held-out half):" in text
    assert "warning" not in text
    store.close()


def test_report_warns_about_few_positives_and_prefers_recall_for_contradictions(tmp_path):
    store, _, ids = make_store(tmp_path, 40)
    for i, pid in enumerate(ids):
        store.save_label(pid, "contradicts__inv_normalizes", i < 5)
    text = calibration_report(store, Policy())
    assert "warning: only 5 yes labels" in text
    assert "prefer the recall threshold" in text
    store.close()


def test_passages_of_one_document_stay_in_one_half(tmp_path):
    store, _, ids = make_store(tmp_path, 6, one_document=True)
    for i, pid in enumerate(ids):
        store.save_label(pid, "new_info", i >= 3)
    assert "all labels come from one half of the documents" in calibration_report(store, Policy())
    store.close()


def test_report_without_labels(tmp_path):
    store, _, _ = make_store(tmp_path, 1)
    assert calibration_report(store, Policy()) == "No labels yet. Run `radar label` first."
    store.close()
