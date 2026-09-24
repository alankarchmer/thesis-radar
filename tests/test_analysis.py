from datetime import date

import pytest
from helpers import ACME_THESIS, write_thesis

from thesis_radar.analysis import (
    EVIDENCE_WEIGHTS,
    assumption_series,
    divergence,
    heatmap,
    prediction_view,
    redlines,
    week_labels,
)
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.policy import Policy
from thesis_radar.similar import link_document, word_diff
from thesis_radar.store import Store
from thesis_radar.thesis import load_thesis

TODAY = date(2026, 9, 24)  # a Thursday in ISO week 2026-W39


def passage(pid, day, *, pillar="inventory", pillar_p=0.9, boilerplate=0.1, materiality=2.0, stance=2.0,
            evidence="reported_result", forward_looking=0.1, assumptions=None, judged=True,
            ingested_at="2026-09-24T08:00:00Z"):
    p = {
        "pillar": pillar, "pillar_p": pillar_p, "boilerplate": boilerplate, "new_info": 0.5,
        "materiality": materiality, "stance": stance, "evidence": evidence, "forward_looking": forward_looking,
        "updates_fact": None, "updates_fact_p": None, "assumptions": assumptions or {}, "questions": {},
    }
    return {
        "id": pid, "document_id": 1, "date": day, "ingested_at": ingested_at, "text": f"Passage {pid}.",
        "status": "current" if judged else "unjudged", "p": p if judged else None,
        "triage": {"status": None, "starred": False}, "classified": {},
    }


# Heat map


def test_week_labels_are_iso_weeks_ending_this_week():
    labels = week_labels(TODAY, 26)
    assert len(labels) == 26 and labels[-1] == "2026-W39" and labels[0] == "2026-W14"
    assert heatmap([], ["inventory"], today=TODAY)["weeks"] == labels


def test_week_labels_cross_the_iso_year_boundary():
    # 2026 has 53 ISO weeks; 2027-01-02 (Saturday) still belongs to 2026-W53.
    assert week_labels(date(2027, 1, 2), 2) == ["2026-W52", "2026-W53"]
    assert week_labels(date(2027, 1, 4), 3) == ["2026-W52", "2026-W53", "2027-W01"]
    # 2025-12-30 already belongs to 2026-W01.
    assert week_labels(date(2025, 12, 30), 2) == ["2025-W52", "2026-W01"]


def test_heatmap_places_passages_in_iso_weeks_across_the_year_end():
    today = date(2027, 1, 4)
    passages = [passage(1, "2027-01-01", stance=4.0), passage(2, "2026-12-28", stance=0.0), passage(3, "2027-01-04")]
    result = heatmap(passages, ["inventory"], today=today, weeks=3)
    assert result["weeks"] == ["2026-W52", "2026-W53", "2027-W01"]
    row = result["rows"]["inventory"]
    assert row[0] is None
    # 2026-12-28 and 2027-01-01 are both in 2026-W53.
    assert row[1] == {"net": 0.0, "count": 2}
    assert row[2] == {"net": 0.0, "count": 1}


def test_heatmap_net_is_materiality_weighted_stance():
    passages = [
        passage(1, "2026-09-21", stance=4.0, materiality=3.0),  # weight 3.1, +1
        passage(2, "2026-09-24", stance=0.0, materiality=0.0),  # weight 0.1, -1
        passage(3, "2026-09-14", stance=3.0, materiality=1.0),  # previous week, +0.5
        passage(4, "2026-09-15", stance=1.0, pillar="pricing"),
    ]
    rows = heatmap(passages, ["inventory", "pricing"], today=TODAY)["rows"]
    assert rows["inventory"][-1] == {"net": pytest.approx(0.9375, abs=5e-4), "count": 2}
    assert rows["inventory"][-2] == {"net": 0.5, "count": 1}
    assert rows["pricing"][-2] == {"net": -0.5, "count": 1}
    assert rows["inventory"][:-2] == [None] * 24
    assert all(-1 <= cell["net"] <= 1 for row in rows.values() for cell in row if cell)


def test_heatmap_filters_pillar_probability_boilerplate_and_unjudged():
    passages = [
        passage(1, "2026-09-22", pillar_p=0.5, stance=4.0),  # counts (pillar_p >= 0.5)
        passage(2, "2026-09-22", pillar_p=0.49, stance=0.0),
        passage(3, "2026-09-22", boilerplate=0.5, stance=0.0),  # boilerplate must be < 0.5
        passage(4, "2026-09-22", boilerplate=0.49, stance=4.0),  # counts
        passage(5, "2026-09-22", judged=False),
        passage(6, "2026-09-22", pillar="off_thesis", stance=0.0),
        passage(7, None, stance=4.0, ingested_at="2026-09-23T10:00:00Z"),  # counts by ingested day
        passage(8, "2026-03-01", stance=0.0),  # before the 26-week window
        passage(9, "2026-10-05", stance=0.0),  # after this week
    ]
    rows = heatmap(passages, ["inventory"], today=TODAY)["rows"]
    assert rows["inventory"][-1] == {"net": 1.0, "count": 3}
    assert rows["inventory"][:-1] == [None] * 25


def test_heatmap_with_no_passages_or_no_pillars():
    assert heatmap([], ["inventory"], today=TODAY, weeks=4)["rows"] == {"inventory": [None] * 4}
    assert heatmap([passage(1, "2026-09-22")], [], today=TODAY)["rows"] == {}


# Assumption balance


def test_assumption_series_counts_and_running_balance():
    policy = Policy()  # contradicts_min 0.7
    a = "inv_normalizes"
    passages = [
        passage(1, "2026-03-10", evidence="reported_result", materiality=3.0,
                assumptions={a: {"supports": 0.8, "contradicts": 0.1}}),  # for; +1.0*0.7*1 = 0.7
        passage(2, "2026-03-20", evidence="guidance", materiality=1.5,
                assumptions={a: {"supports": 0.05, "contradicts": 0.9}}),  # against; 0.8*-0.85*0.5 = -0.34
        passage(3, "2026-05-02", evidence=None, materiality=3.0,
                assumptions={a: {"supports": 0.6, "contradicts": 0.2}}),  # neither count; 0.5*0.4*1 = 0.2
        passage(4, "2026-06-15", assumptions={a: {"supports": 0.4, "contradicts": 0.3}}),  # no evidence
        passage(5, "2026-07-01", judged=False),
        passage(6, "2026-08-01", assumptions={}),  # stale judgment without the assumption
        passage(7, "2026-08-02", assumptions={a: None}),
    ]
    series = assumption_series(passages, [a, "other"], policy)
    assert series["other"] == []
    assert series[a] == [
        {"month": "2026-03", "for": 1, "against": 1, "balance": 0.36},
        {"month": "2026-04", "for": 0, "against": 0, "balance": 0.36},
        {"month": "2026-05", "for": 0, "against": 0, "balance": 0.56},
    ]


def test_assumption_series_against_wins_and_months_cross_years():
    policy = Policy(contradicts_min=0.4)
    a = "inv_normalizes"
    passages = [
        passage(1, "2025-12-05", evidence="speculation", materiality=3.0,
                assumptions={a: {"supports": 0.5, "contradicts": 0.45}}),  # against only; 0.2*0.05*1 = 0.01
        passage(2, "2026-02-01", evidence="analyst_opinion", materiality=3.0,
                assumptions={a: {"supports": 0.45, "contradicts": 0.1}}),  # for; below 0.5, no balance
        passage(3, "2026-02-03", evidence="made_up", materiality=1.5,
                assumptions={a: {"supports": 0.0, "contradicts": 0.0}}),  # nothing
    ]
    assert assumption_series(passages, [a], policy)[a] == [
        {"month": "2025-12", "for": 0, "against": 1, "balance": 0.01},
        {"month": "2026-01", "for": 0, "against": 0, "balance": 0.01},
        {"month": "2026-02", "for": 1, "against": 0, "balance": 0.01},
    ]


def test_evidence_weights_and_unknown_evidence():
    a = "x"
    for evidence, weight in [*EVIDENCE_WEIGHTS.items(), ("unknown_kind", 0.5), (None, 0.5)]:
        passages = [passage(1, "2026-09-01", evidence=evidence, materiality=3.0,
                            assumptions={a: {"supports": 1.0, "contradicts": 0.0}})]
        assert assumption_series(passages, [a], Policy())[a][0]["balance"] == weight
    assert assumption_series([], [a], Policy()) == {a: []}


# Divergence


def side(pillar, evidence, stance, n, *, start=1, day="2026-09-01", materiality=2.0):
    return [passage(start + i, day, pillar=pillar, evidence=evidence, stance=stance, materiality=materiality)
            for i in range(n)]


def test_divergence_compares_company_and_outside_voices():
    passages = [
        *side("inventory", "guidance", 3.0, 2, start=1),
        *side("inventory", "management_commentary", 3.0, 1, start=3),
        *side("inventory", "channel_or_customer_data", 2.0, 2, start=10),
        *side("inventory", "expert_opinion", 2.0, 1, start=12),
        *side("inventory", "analyst_opinion", 0.0, 3, start=20),  # neither side
        *side("inventory", "reported_result", 0.0, 3, start=30),  # neither side
    ]
    [entry] = divergence(passages, ["inventory", "pricing"], Policy(), today=TODAY)
    assert entry["pillar"] == "inventory"
    assert entry["inside"]["stance"] == 3.0 and entry["inside"]["n"] == 3
    assert entry["outside"]["stance"] == 2.0 and entry["outside"]["n"] == 3
    assert entry["gap"] == 1.0 and entry["flagged"] is True  # gap exactly min_gap, 3 passages each


def test_divergence_weighted_stance_and_example_ids():
    passages = [
        passage(1, "2026-09-01", evidence="guidance", stance=4.0, materiality=3.0),
        passage(2, "2026-09-10", evidence="guidance", stance=2.0, materiality=0.0),
        passage(3, "2026-09-05", evidence="guidance", stance=2.0, materiality=0.0),
        passage(4, "2026-08-01", evidence="guidance", stance=2.0, materiality=0.0),
        passage(5, "2026-09-01", evidence="expert_opinion", stance=1.0, materiality=1.0),
    ]
    [entry] = divergence(passages, ["inventory"], Policy(), today=TODAY)
    # (3.1*4 + 3*0.1*2) / 3.4
    assert entry["inside"]["stance"] == pytest.approx(round(13.0 / 3.4, 3))
    assert entry["inside"]["ids"] == [1, 2, 3]  # highest materiality, then newest
    assert entry["outside"] == {"stance": 1.0, "n": 1, "ids": [5]}
    assert entry["gap"] == pytest.approx(round(13.0 / 3.4 - 1.0, 3))
    assert entry["flagged"] is False  # too few passages


def test_divergence_window_and_filters():
    policy = Policy()  # window 120 days: 2026-05-27 .. today; pillar_p >= 0.5; boilerplate <= 0.3
    passages = [
        passage(1, "2026-05-27", evidence="guidance", stance=4.0),  # first day in the window
        passage(2, "2026-05-26", evidence="guidance", stance=0.0),  # one day too old
        passage(3, "2026-09-01", evidence="guidance", stance=0.0, pillar_p=0.49),
        passage(4, "2026-09-01", evidence="guidance", stance=0.0, boilerplate=0.31),
        passage(5, "2026-09-01", evidence="expert_opinion", stance=2.0, boilerplate=0.3),
        passage(6, "2026-09-01", evidence="expert_opinion", judged=False),
        passage(7, None, evidence="expert_opinion", stance=2.0, ingested_at="2026-09-20T00:00:00Z"),
    ]
    [entry] = divergence(passages, ["inventory"], policy, today=TODAY)
    assert entry["inside"] == {"stance": 4.0, "n": 1, "ids": [1]}
    assert entry["outside"]["n"] == 2 and sorted(entry["outside"]["ids"]) == [5, 7]
    assert divergence([], ["inventory"], policy, today=TODAY) == []
    assert divergence(side("inventory", "guidance", 3.0, 3), ["inventory"], policy, today=TODAY) == []


def test_divergence_puts_flagged_pillars_first_then_larger_gaps():
    policy = Policy(divergence_min_passages=2, divergence_min_gap=1.0)
    passages = [
        # a: flagged, gap 1.0
        *side("a", "guidance", 3.0, 2, start=1), *side("a", "expert_opinion", 2.0, 2, start=10),
        # b: gap -3 but only one outside passage: not flagged
        *side("b", "guidance", 1.0, 2, start=20), *side("b", "expert_opinion", 4.0, 1, start=30),
        # c: gap 0.5, not flagged
        *side("c", "guidance", 2.5, 2, start=40), *side("c", "channel_or_customer_data", 2.0, 2, start=50),
        # d: flagged, gap -2
        *side("d", "management_commentary", 1.0, 2, start=60), *side("d", "expert_opinion", 3.0, 2, start=70),
    ]
    entries = divergence(passages, ["a", "b", "c", "d"], policy, today=TODAY)
    assert [(e["pillar"], e["gap"], e["flagged"]) for e in entries] == [
        ("d", -2.0, True), ("a", 1.0, True), ("b", -3.0, False), ("c", 0.5, False),
    ]


# Redlines


def add_filing(store, digest, form, day, texts, *, ticker="ACME", origin="edgar", status="sorted", pages=None):
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest * 64, path=f"archive/{ticker}/{digest}.htm", title=f"{form} {day}",
                        origin=origin, status=status, ticker=ticker, source_type="filing", doc_date=day, form=form)
        )
        ids = store.insert_passages(
            doc, [PassageDraft(i, (pages or {}).get(i, 1), 0, len(t), t) for i, t in enumerate(texts)]
        )
    return doc, ids


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "radar.db")
    yield s
    s.close()


FINANCING = "Risk factors: dealers may be unable to obtain floorplan financing on acceptable terms."
FINANCING_NEW = FINANCING[:-1] + ", which could reduce their orders."
DEALERS = "We sell snowmobiles through independent dealers in North America."
LITHIUM = "Lithium cell supply is not expected to limit production of electric models."
HEADQUARTERS = "Our headquarters are in Duluth, Minnesota."
EUROPE = "We began selling electric snowmobiles in Europe this year."


def test_redlines_list_changed_added_and_removed_passages(store):
    old, old_ids = add_filing(store, "a", "10-K", "2025-02-20", [FINANCING, DEALERS, LITHIUM, HEADQUARTERS],
                              pages={2: 34})
    new, new_ids = add_filing(store, "b", "10-K", "2026-02-19",
                              ["we  sell snowmobiles through independent dealers in north america.", FINANCING_NEW,
                               EUROPE, HEADQUARTERS])
    link_document(store, old)
    link_document(store, new)
    assert store.passages_for_document(new)[3]["repeat_of"] == old_ids[3]  # exact repeats still count
    [redline] = redlines(store, "ACME")
    assert redline["form"] == "10-K" and (redline["new_document"], redline["old_document"]) == (new, old)
    [changed] = redline["changed"]
    assert (changed["id"], changed["old_id"]) == (new_ids[1], old_ids[0])
    assert 0.7 <= changed["similarity"] < 1 and changed["similarity"] == round(changed["similarity"], 3)
    assert changed["diff"] == word_diff(FINANCING, FINANCING_NEW)
    assert redline["added"] == [new_ids[2]]
    assert redline["removed"] == [{"id": old_ids[2], "text": LITHIUM, "page": 34}]


def test_redlines_group_form_families_and_pick_the_latest_two(store):
    add_filing(store, "a", "10-Q", "2026-05-01", [DEALERS])
    q_amended, _ = add_filing(store, "b", "10-Q/A", "2026-08-01", [DEALERS])
    q_latest, _ = add_filing(store, "c", "10-Q", "2026-08-01", [DEALERS, EUROPE])  # same date, higher id
    add_filing(store, "d", "10-Q", "2026-09-01", [LITHIUM], origin="inbox")  # not EDGAR
    add_filing(store, "e", "10-Q", "2026-09-02", [LITHIUM], status="unsorted")
    add_filing(store, "f", "10-Q", "2026-09-03", [LITHIUM], ticker="BRRR")
    add_filing(store, "g", "8-K", "2026-09-04", [LITHIUM])
    add_filing(store, "h", "8-K", "2026-09-05", [HEADQUARTERS])
    only_annual, _ = add_filing(store, "i", "20-F", "2026-03-01", [DEALERS])
    result = redlines(store, "ACME")
    assert [(r["form"], r["new_document"], r["old_document"]) for r in result] == [("10-Q", q_latest, q_amended)]
    assert result[0]["changed"] == [] and result[0]["removed"] == [] and len(result[0]["added"]) == 1

    annual, _ = add_filing(store, "j", "40-F", "2027-03-01", [DEALERS])
    assert [(r["form"], r["new_document"], r["old_document"]) for r in redlines(store, "ACME")] == [
        ("10-K", annual, only_annual), ("10-Q", q_latest, q_amended),
    ]
    assert redlines(store, "NONE") == []


SEGMENTS = ["snowmobile", "off-road", "marine"]


def test_redlines_truncate_changed_then_added_then_removed(store):
    old_texts = [f"Net sales in the {s} segment rose 5 percent to 120 million dollars." for s in SEGMENTS] + [
        "The board approved a new share repurchase authorization.",
        "Our pension plan was frozen for salaried employees.",
        "A legal proceeding in Ohio was settled without payment.",
    ]
    new_texts = [f"Net sales in the {s} segment rose 7 percent to 124 million dollars." for s in SEGMENTS] + [
        "We opened a distribution center in Ontario.",
        "Tariffs on imported aluminum raised costs.",
        "The warranty reserve methodology was updated.",
    ]
    _, old_ids = add_filing(store, "a", "10-Q", "2026-05-01", old_texts)
    _, new_ids = add_filing(store, "b", "10-Q", "2026-08-01", new_texts)

    def counts(max_items):
        [r] = redlines(store, "ACME", max_items=max_items)
        return len(r["changed"]), len(r["added"]), len(r["removed"])

    [full] = redlines(store, "ACME")
    assert [(c["id"], c["old_id"]) for c in full["changed"]] == list(zip(new_ids[:3], old_ids[:3], strict=True))
    assert full["added"] == new_ids[3:]
    assert [r["id"] for r in full["removed"]] == old_ids[3:]
    assert counts(80) == (3, 3, 3)
    assert counts(7) == (3, 3, 1)
    assert counts(4) == (3, 1, 0)
    assert counts(2) == (2, 0, 0)
    assert counts(0) == (0, 0, 0)
    [short] = redlines(store, "ACME", max_items=7)
    assert short["removed"] == [{"id": old_ids[3], "text": old_texts[3], "page": 1}]  # document order kept


def test_redlines_scale_to_long_filings(store):
    words = [f"term{i}" for i in range(400)]
    old_texts = [" ".join(words[(i * 7 + k) % 400] for k in range(30)) + f" item {i}." for i in range(500)]
    new_texts = [text.replace(" item", " revised item") if i % 5 == 0 else text for i, text in enumerate(old_texts)]
    add_filing(store, "a", "10-K", "2025-02-20", old_texts)
    add_filing(store, "b", "10-K", "2026-02-20", new_texts)
    [r] = redlines(store, "ACME", max_items=1000)
    assert (len(r["changed"]), len(r["added"]), len(r["removed"])) == (100, 0, 0)


# Predictions


PREDICTIONS = """\
predictions:
  due_edge: {statement: "Dealer inventory normalizes.", by: 2026-10-08, p: 0.6, pillar: inventory}
  open_edge: {statement: "Promotions fall.", by: 2026-10-09, p: 0.5}
  due_today: {statement: "Orders rise.", by: 2026-09-24, p: 0.5}
  overdue: {statement: "Prices rise.", by: 2026-09-23, p: 0.5, pillar: pricing}
  won: {statement: "Dealer count grows.", by: 2026-01-01, p: 0.7}
  lost: {statement: "Margins recover.", by: 2027-06-30, p: 0.4}
"""


def thesis_with(tmp_path, extra):
    return load_thesis(write_thesis(tmp_path / "thesis", text=ACME_THESIS + extra))


def test_prediction_statuses_outcomes_and_brier(tmp_path):
    thesis = thesis_with(tmp_path, PREDICTIONS)
    resolutions = {
        "won": {"outcome": True, "resolved_at": "2026-02-01T09:00:00Z"},
        "lost": {"outcome": False, "resolved_at": "2026-09-01T09:00:00Z"},
        "not_in_thesis": {"outcome": True, "resolved_at": "2026-09-01T09:00:00Z"},
    }
    asked = []

    def related(statement):
        asked.append(statement)
        return [len(asked)]

    predictions, forecast = prediction_view(thesis, resolutions, today=TODAY, related=related)
    assert [(p["id"], p["status"]) for p in predictions] == [
        ("due_edge", "due"), ("open_edge", "open"), ("due_today", "due"), ("overdue", "overdue"),
        ("won", "resolved"), ("lost", "resolved"),
    ]
    assert predictions[0] == {
        "id": "due_edge", "statement": "Dealer inventory normalizes.", "by": "2026-10-08", "p": 0.6,
        "pillar": "inventory", "outcome": None, "resolved_at": None, "status": "due", "related": [1],
    }
    assert (predictions[4]["outcome"], predictions[4]["resolved_at"]) == (True, "2026-02-01T09:00:00Z")
    assert (predictions[5]["outcome"], predictions[5]["pillar"]) == (False, None)
    assert asked == [p["statement"] for p in predictions]
    assert forecast == {"resolved": 2, "brier": 0.125}  # ((0.7 - 1)^2 + (0.4 - 0)^2) / 2


def test_brier_rounds_to_four_places_and_is_null_when_nothing_resolved(tmp_path):
    thesis = thesis_with(tmp_path, 'predictions:\n  x: {statement: "X.", by: 2026-12-31, p: 0.123}\n')
    resolved = {"x": {"outcome": True, "resolved_at": "2026-09-01T00:00:00Z"}}
    assert prediction_view(thesis, resolved, today=TODAY, related=lambda s: [])[1] == {"resolved": 1, "brier": 0.7691}
    assert prediction_view(thesis, {}, today=TODAY, related=lambda s: [])[1] == {"resolved": 0, "brier": None}
    plain = load_thesis(write_thesis(tmp_path / "plain"))
    assert prediction_view(plain, {}, today=TODAY, related=lambda s: [1]) == ([], {"resolved": 0, "brier": None})
