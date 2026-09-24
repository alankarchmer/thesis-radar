import pytest
from helpers import choice, noul, score, write_thesis

from thesis_radar.calibrate import (
    brier,
    calibration_report,
    confident_mistakes,
    expected_calibration_error,
    half,
    metrics_at,
    run_labeling,
    sample_for_labeling,
    select_threshold,
    select_threshold_for_recall,
    signal,
    wilson,
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
        store.save_judgment(JudgmentRecord(pid, f"k{pid}", "jev-1.13.0", "r", "t", "judged",
                                           {"new_info": noul(i / count), **ANSWERS}, ticker="ACME"))
    return store, theses, ids


def pairs(*items, weight=1.0):
    return [(s, y, weight) for s, y in items]


def test_metrics_and_precision_threshold():
    data = pairs((0.9, True), (0.8, True), (0.7, False), (0.6, True), (0.2, False))
    m = metrics_at(data, 0.65)
    assert (m.precision, m.recall, m.flagged, m.true_positives, m.positives) == (2 / 3, 2 / 3, 3, 2, 3)
    assert select_threshold(data, 0.99, [0.1, 0.5, 0.75, 0.85]) == 0.75
    assert select_threshold(pairs((0.9, False)), 0.5, [0.5]) is None


def test_weights_change_precision_and_recall():
    data = [(0.9, True, 1.0), (0.9, False, 3.0), (0.2, True, 4.0)]
    m = metrics_at(data, 0.5)
    assert m.precision == pytest.approx(0.25) and m.recall == pytest.approx(0.2)
    assert m.n_flagged_effective == pytest.approx(16 / 10)


def test_recall_threshold_is_the_highest_that_keeps_enough_positives():
    data = pairs((0.9, True), (0.8, True), (0.7, False), (0.6, True), (0.2, False))
    assert select_threshold_for_recall(data, 1.0, [0.1, 0.5, 0.65, 0.85]) == 0.5
    assert select_threshold_for_recall(data, 0.6, [0.1, 0.5, 0.65, 0.85]) == 0.65
    assert select_threshold_for_recall(pairs((0.5, False)), 0.9, [0.5]) is None


def test_probability_error_measures():
    assert brier(pairs((1.0, True), (0.0, False))) == 0
    assert brier(pairs((1.0, False))) == 1
    assert brier([]) is None
    assert brier([(1.0, False, 1.0), (1.0, True, 3.0)]) == pytest.approx(0.25)
    assert confident_mistakes(pairs((0.95, False), (0.05, True), (0.5, True), (0.95, True))) == (1, 1)
    assert expected_calibration_error(pairs((0.9, True), (0.9, False))) == pytest.approx(0.4)
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


def test_sampling_takes_half_from_flagged_with_weights():
    sample = sample_for_labeling(list(range(100)), set(range(10)), n=20, seed=1)
    assert len(sample.ids) == 20 and len(set(sample.ids)) == 20
    assert sum(1 for pid in sample.ids if pid < 10) == 10
    assert {sample.weights[pid] for pid in sample.ids if pid < 10} == {1.0}
    assert {sample.weights[pid] for pid in sample.ids if pid >= 10} == {9.0}
    small = sample_for_labeling([1, 2, 3], {1}, n=10, seed=0)
    assert sorted(small.ids) == [1, 2, 3]


def test_labeling_saves_answers_keys_and_weights_and_stops_on_quit(tmp_path):
    store, theses, ids = make_store(tmp_path, 2)
    replies = iter(["y", "n", "maybe", "n", "q"])
    shown = []
    done = run_labeling(store, theses["ACME"], store.get_passages(ids), ask=lambda prompt: next(replies),
                        show=shown.append, weights={ids[0]: 4.0}, keys={ids[0]: [f"k{ids[0]}"]})
    assert done == 1
    labels = store.labels()
    assert {(r["question"], r["value"]) for r in labels} == {("new_info", 1), ("material", 0), ("contradicts__inv_normalizes", 0)}
    assert {(r["weight"], r["judgment_keys"], r["origin"]) for r in labels} == {(4.0, f'["k{ids[0]}"]', "sample")}
    assert "Passage 0." in shown[0]
    store.close()


def test_report_selects_on_one_half_and_reports_on_the_other(tmp_path):
    store, _, ids = make_store(tmp_path, 80)
    for i, pid in enumerate(ids):
        store.save_label(pid, "new_info", i >= 40, ticker="ACME", judgment_keys=[f"k{pid}"])
    text = calibration_report(store, Policy(), target_precision=0.8, target_recall=0.9)
    assert "new_info: 80 labels (40 yes) from 80 documents" in text
    assert "current threshold 0.60 on held-out half: precision" in text
    assert "for 80% precision: threshold" in text
    assert "to catch 90%: threshold" in text
    assert "Brier" in text and "calibration error" in text
    assert "confidently wrong on held-out half:" in text
    assert "reliability (held-out half, weighted):" in text
    assert "warning" not in text
    store.close()


def test_labels_are_compared_with_the_judgment_they_were_made_against(tmp_path):
    store, _, ids = make_store(tmp_path, 4)
    store.save_label(ids[0], "new_info", True, ticker="ACME", judgment_keys=["gone"])
    store.save_label(ids[1], "new_info", True, ticker="ACME", judgment_keys=[f"k{ids[1]}"])
    text = calibration_report(store, Policy())
    assert "1 labels skipped" in text and "new_info: 1 labels" in text
    store.close()


def test_report_warns_about_few_positives_and_prefers_recall_for_contradictions(tmp_path):
    store, _, ids = make_store(tmp_path, 40)
    for i, pid in enumerate(ids):
        store.save_label(pid, "contradicts__inv_normalizes", i < 5, ticker="ACME")
    text = calibration_report(store, Policy())
    assert "warning: only 5 yes labels" in text
    assert "prefer the recall threshold" in text
    store.close()


def test_passages_of_one_document_stay_in_one_half(tmp_path):
    store, _, ids = make_store(tmp_path, 6, one_document=True)
    for i, pid in enumerate(ids):
        store.save_label(pid, "new_info", i >= 3, ticker="ACME")
    assert "all labels come from one half of the documents" in calibration_report(store, Policy())
    store.close()


def test_feed_labels_report_precision_and_miss_rate(tmp_path):
    store, _, ids = make_store(tmp_path, 6)
    for pid, value in zip(ids[:4], [True, True, True, False], strict=True):
        store.save_label(pid, "whats_new", value, ticker="ACME", origin="triage")
    store.save_label(ids[4], "whats_new", True, ticker="ACME", origin="spotcheck")
    store.save_label(ids[5], "whats_new", False, ticker="ACME", origin="spotcheck")
    text = calibration_report(store, Policy())
    assert "feed precision from triage: 0.75" in text and "3 of 4" in text
    assert "miss rate from spot checks: 0.50" in text and "warning: only 2 spot checks" in text
    assert "No sampled labels yet" in text
    store.close()


def test_report_without_labels(tmp_path):
    store, _, _ = make_store(tmp_path, 1)
    assert calibration_report(store, Policy()).startswith("No labels yet.")
    store.close()
