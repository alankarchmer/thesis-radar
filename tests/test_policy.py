import pytest

from helpers import choice, noul, score
from thesis_radar.policy import Policy, PolicyError, classify, load_policy


def answers(*, pillar="inventory", pillar_p=0.9, boilerplate=0.05, new_info=0.9,
            materiality=(0, 0, 0.2, 0.8), contradicts=0.05, supports=0.05):
    return {
        "pillar": choice(pillar, {pillar: pillar_p, "other": 1 - pillar_p}),
        "boilerplate": noul(boilerplate),
        "new_info": noul(new_info),
        "materiality": score(list(materiality)),
        "stance": score([0, 0, 1, 0, 0]),
        "evidence": choice("guidance", {"guidance": 1.0}),
        "forward_looking": noul(0.5),
        "assumption__inv_normalizes": choice(
            "neither", {"supports": supports, "contradicts": contradicts, "neither": 1 - contradicts - supports}
        ),
    }


def test_defaults_match_the_spec(tmp_path):
    policy = load_policy(tmp_path / "policy.yaml")
    assert policy == Policy()
    assert (policy.new_info_min, policy.materiality_min, policy.contradicts_min) == (0.6, 1.5, 0.7)


def test_policy_file_overrides(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("whats_new:\n  new_info: {min: 0.8}\nmaybe:\n  new_info: {between: [0.5, 0.8]}\n", encoding="utf-8")
    policy = load_policy(path)
    assert (policy.new_info_min, policy.maybe_new_info_low, policy.maybe_new_info_high) == (0.8, 0.5, 0.8)
    assert policy.materiality_min == 1.5


@pytest.mark.parametrize(
    "text",
    [
        "whats_new:\n  novelty: {min: 0.5}\n",
        "maybe:\n  new_info: {between: [0.6, 0.4]}\n",
        "metadata:\n  min_confidence: high\n",
    ],
)
def test_bad_policy_is_rejected(tmp_path, text):
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(PolicyError):
        load_policy(path)


def test_new_material_on_thesis_passage_is_whats_new():
    c = classify(answers(), Policy())
    assert (c.in_whats_new, c.in_maybe, c.in_contradictions, c.pillar) == (True, False, False, "inventory")
    assert c.materiality == pytest.approx(2.8)


def test_borderline_novelty_goes_to_maybe():
    c = classify(answers(new_info=0.5), Policy())
    assert (c.in_whats_new, c.in_maybe) == (False, True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"pillar": "off_thesis"},
        {"pillar_p": 0.6},
        {"boilerplate": 0.8},
        {"materiality": (0.9, 0.1, 0, 0)},
        {"new_info": 0.2},
    ],
)
def test_filters_keep_passages_out_of_the_feed(overrides):
    c = classify(answers(**overrides), Policy())
    assert not c.in_whats_new and not c.in_maybe


def test_contradictions_are_pinned_and_not_repeated():
    c = classify(answers(contradicts=0.9), Policy())
    assert c.contradicts == ("inv_normalizes",)
    assert (c.in_contradictions, c.in_whats_new, c.in_maybe) == (True, False, False)


def test_contradictions_ignore_the_novelty_filter():
    assert classify(answers(contradicts=0.9, new_info=0.1, pillar="off_thesis"), Policy()).in_contradictions


def test_supports_are_reported():
    assert classify(answers(supports=0.9), Policy()).supports == ("inv_normalizes",)
