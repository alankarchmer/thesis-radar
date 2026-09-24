import json
from datetime import date
from pathlib import Path

import pytest
from helpers import choice, noul, score

from thesis_radar.policy import (
    Policy,
    PolicyError,
    classify,
    classify_passage,
    load_policy,
    policy_from_flat,
    policy_yaml,
    signals,
)

TODAY = date(2026, 9, 24)
CASES = Path(__file__).parent / "fixtures" / "classify_cases.json"


def answers(*, pillar="inventory", pillar_p=0.9, boilerplate=0.05, new_info=0.9,
            materiality=(0, 0, 0.2, 0.8), contradicts=0.05, supports=0.05, question=0.1):
    return {
        "pillar": choice(pillar, {pillar: pillar_p, "other": 1 - pillar_p}),
        "boilerplate": noul(boilerplate),
        "new_info": noul(new_info),
        "materiality": score(list(materiality)),
        "stance": score([0, 0, 1, 0, 0]),
        "evidence": choice("guidance", {"guidance": 1.0}),
        "forward_looking": noul(0.5),
        "updates_fact": choice("inventory.0", {"inventory.0": 0.8, "none": 0.2}),
        "assumption__inv_normalizes": choice(
            "neither", {"supports": supports, "contradicts": contradicts, "neither": 1 - contradicts - supports}
        ),
        "question__q4": noul(question),
    }


def run(date_="2026-09-20", triage=None, **kwargs):
    return classify(signals(answers(**kwargs)), Policy(), today=TODAY, passage_date=date_, triage_status=triage)


def test_defaults_match_the_spec(tmp_path):
    policy = load_policy(tmp_path / "policy.yaml")
    assert policy == Policy()
    assert (policy.new_info_min, policy.materiality_min, policy.contradicts_min, policy.whats_new_window_days) == (
        0.6, 1.5, 0.7, 30,
    )


def test_policy_file_overrides_and_round_trips(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "whats_new:\n  window_days: 14\n  new_info: {min: 0.8}\nmaybe:\n  new_info: {between: [0.5, 0.8]}\n",
        encoding="utf-8",
    )
    policy = load_policy(path)
    assert (policy.new_info_min, policy.maybe_new_info_low, policy.maybe_new_info_high) == (0.8, 0.5, 0.8)
    assert policy.whats_new_window_days == 14 and policy.materiality_min == 1.5
    path.write_text(policy_yaml(policy), encoding="utf-8")
    assert load_policy(path) == policy


@pytest.mark.parametrize(
    "text",
    [
        "whats_new:\n  novelty: {min: 0.5}\n",
        "maybe:\n  new_info: {between: [0.6, 0.4]}\n",
        "metadata:\n  min_probability: high\n",
        "whats_new:\n  new_info: {min: 1.5}\n",
        "whats_new:\n  window_days: 2.5\n",
    ],
)
def test_bad_policy_is_rejected(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_policy_from_flat_merges_and_validates():
    assert policy_from_flat({"new_info_min": 0.7}).new_info_min == 0.7
    with pytest.raises(PolicyError):
        policy_from_flat({"nope": 1})
    with pytest.raises(PolicyError):
        policy_from_flat({"maybe_new_info_low": 0.9})


def test_signals_use_the_chosen_probability():
    p = signals(answers(pillar_p=0.55))
    assert p["pillar"] == "inventory" and p["pillar_p"] == pytest.approx(0.55)
    assert p["updates_fact"] == "inventory.0" and p["updates_fact_p"] == 0.8
    assert p["assumptions"] == {"inv_normalizes": {"supports": 0.05, "contradicts": 0.05}}
    assert p["questions"] == {"q4": 0.1}
    assert signals(None) is None and signals({"new_info": noul(0.5)}) is None


def test_new_material_on_thesis_passage_is_whats_new():
    c = run()
    assert (c.in_whats_new, c.in_maybe, c.in_contradictions, c.flagged) == (True, False, False, True)


def test_borderline_novelty_goes_to_maybe():
    c = run(new_info=0.5)
    assert (c.in_whats_new, c.in_maybe) == (False, True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"pillar": "off_thesis"},
        {"pillar_p": 0.45},
        {"boilerplate": 0.8},
        {"materiality": (0.9, 0.1, 0, 0)},
        {"new_info": 0.2},
        {"date_": "2026-07-01"},
        {"triage": "dismissed"},
        {"triage": "absorbed"},
    ],
)
def test_filters_keep_passages_out_of_the_feed(overrides):
    c = run(**overrides)
    assert not c.in_whats_new and not c.in_maybe


def test_contradictions_are_pinned_not_repeated_and_can_be_acknowledged():
    c = run(contradicts=0.9)
    assert c.contradicts == ("inv_normalizes",)
    assert (c.in_contradictions, c.in_whats_new, c.in_maybe) == (True, False, False)
    acknowledged = run(contradicts=0.9, triage="acknowledged")
    assert not acknowledged.in_contradictions and not acknowledged.in_whats_new
    assert not run(contradicts=0.9, date_="2026-01-01").in_contradictions


def test_contradictions_ignore_the_novelty_filter():
    assert run(contradicts=0.9, new_info=0.1, pillar="off_thesis").in_contradictions


def test_supports_and_open_questions_are_reported():
    assert run(supports=0.9).supports == ("inv_normalizes",)
    assert run(question=0.8).questions == ("q4",)


def test_missing_date_falls_back_to_ingestion_and_none_is_nothing():
    p = signals(answers())
    assert classify(p, Policy(), today=TODAY, ingested_at="2026-09-23T10:00:00Z").in_whats_new
    assert not classify(None, Policy(), today=TODAY).flagged


@pytest.mark.skipif(not CASES.exists(), reason="shared classify cases not written yet")
def test_shared_cases_match_the_dashboard_logic():
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 20
    for case in cases:
        policy = policy_from_flat(case["policy"])
        got = classify_passage(case["passage"], policy, date.fromisoformat(case["today"])).as_payload()
        assert got == case["expected"], case["name"]


def test_boilerplate_is_not_pinned_as_a_contradiction():
    assert not run(contradicts=0.9, boilerplate=0.8).in_contradictions
    assert not run(contradicts=0.9, boilerplate=0.8).flagged
    assert run(contradicts=0.9, boilerplate=0.5).in_contradictions
    assert run(contradicts=0.9, boilerplate=0.4).in_contradictions


def test_contradiction_boilerplate_limit_is_configurable(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("contradictions:\n  boilerplate: {max: 0.2}\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.contradiction_boilerplate_max == 0.2
    assert "  boilerplate: {max: 0.2}" in policy_yaml(policy).split("contradictions:")[1]
