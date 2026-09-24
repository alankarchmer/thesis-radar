import pytest
from helpers import ACME_THESIS, write_thesis

from thesis_radar.thesis import ThesisError, load_theses, load_thesis

FACT = '    - "Dealer inventory was elevated at the end of Q2."'

FULL = ACME_THESIS.replace("known_facts:", """peers: [BRRR]
open_questions:
  q4_orders: Will dealers cut fourth-quarter orders?
predictions:
  inv_back: {statement: "Inventory is normal by Q1.", by: 2027-03-31, p: 0.6, pillar: inventory}
known_facts:""") + '    - {text: "Promotions up 200 bps.", as_of: 2026-08-05, source: 12}\n'


def test_loads_a_valid_thesis(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path))
    assert thesis.ticker == "ACME"
    assert thesis.company == "ACME Snowmobiles Inc."
    assert thesis.aliases == ("ACME", "ACME Snowmobiles")
    assert list(thesis.pillars) == ["inventory", "pricing"]
    assert [a.id for a in thesis.assumptions] == ["inv_normalizes"]
    assert [f.text for f in thesis.known_facts["inventory"]] == ["Dealer inventory was elevated at the end of Q2."]
    assert thesis.known_facts["inventory"][0].id == "inventory.0"
    assert thesis.peers == () and thesis.open_questions == () and thesis.predictions == ()


def test_loads_v11_sections(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path, text=FULL))
    assert thesis.peers == ("BRRR",)
    assert thesis.open_questions[0].text == "Will dealers cut fourth-quarter orders?"
    prediction = thesis.predictions[0]
    assert (prediction.by, prediction.p, prediction.pillar) == ("2027-03-31", 0.6, "inventory")
    dated = thesis.fact("inventory.1")
    assert (dated.text, dated.as_of, dated.source) == ("Promotions up 200 bps.", "2026-08-05", 12)
    assert dated.for_jev() == "Promotions up 200 bps. (as of 2026-08-05)"


def test_canonical_form_lists_facts_for_every_pillar(tmp_path):
    canonical = load_thesis(write_thesis(tmp_path)).canonical()
    assert canonical["known_facts"] == {
        "inventory": ["Dealer inventory was elevated at the end of Q2."],
        "pricing": [],
    }
    assert canonical["open_questions"] == {}


def test_version_is_stable_and_changes_with_facts(tmp_path):
    first = load_thesis(write_thesis(tmp_path))
    assert first.version == load_thesis(write_thesis(tmp_path)).version
    assert len(first.version) == 16
    changed = ACME_THESIS.replace(FACT, FACT + '\n    - "Promotions rose in Q3."')
    assert load_thesis(write_thesis(tmp_path, text=changed)).version != first.version


def test_version_ignores_peers_predictions_and_fact_sources(tmp_path):
    base = load_thesis(write_thesis(tmp_path, text=FULL)).version
    variant = FULL.replace("peers: [BRRR]", "peers: [ZZZ]").replace("p: 0.6", "p: 0.9").replace("source: 12", "source: 13")
    assert load_thesis(write_thesis(tmp_path, text=variant)).version == base


def _expect_error(tmp_path, text, field_fragment, ticker="ACME"):
    with pytest.raises(ThesisError) as caught:
        load_thesis(write_thesis(tmp_path, ticker=ticker, text=text))
    assert field_fragment in caught.value.field
    return caught.value


def test_ticker_must_match_file_name(tmp_path):
    error = _expect_error(tmp_path, ACME_THESIS, "ticker", ticker="OTHER")
    assert "file name" in error.reason


def test_boolean_keys_are_rejected(tmp_path):
    text = ACME_THESIS.replace("  pricing: List pricing", "  no: List pricing")
    error = _expect_error(tmp_path, text, "pillars")
    assert "quote it" in error.reason


def test_reserved_pillar_name_is_rejected(tmp_path):
    text = ACME_THESIS.replace("  pricing: List pricing", "  off_thesis: List pricing")
    _expect_error(tmp_path, text, "pillars.off_thesis")


def test_assumption_must_name_an_existing_pillar(tmp_path):
    text = ACME_THESIS.replace("{pillar: inventory,", "{pillar: margins,")
    _expect_error(tmp_path, text, "assumptions.inv_normalizes.pillar")


def test_too_many_facts_are_rejected(tmp_path):
    facts = "\n".join(f'    - "Fact {n}."' for n in range(21))
    _expect_error(tmp_path, ACME_THESIS.replace(FACT, facts), "known_facts.inventory")


def test_facts_must_belong_to_a_pillar(tmp_path):
    text = ACME_THESIS + '  margins:\n    - "Gross margin fell."\n'
    _expect_error(tmp_path, text, "known_facts.margins")


@pytest.mark.parametrize(
    "old, new, field",
    [
        ("peers: [BRRR]", "peers: [ACME]", "peers.ACME"),
        ("peers: [BRRR]", "peers: [brrr]", "peers.brrr"),
        ("by: 2027-03-31", "by: soon", "predictions.inv_back.by"),
        ("p: 0.6", "p: 1.5", "predictions.inv_back.p"),
        ("as_of: 2026-08-05", "as_of: August", "as_of"),
        ("source: 12", "source: twelve", "source"),
    ],
)
def test_v11_sections_are_validated(tmp_path, old, new, field):
    _expect_error(tmp_path, FULL.replace(old, new), field)


def test_unknown_top_level_keys_are_rejected(tmp_path):
    _expect_error(tmp_path, ACME_THESIS + "notes: hi\n", "notes")


def test_load_theses_keeps_valid_files_and_reports_errors(tmp_path):
    write_thesis(tmp_path)
    (tmp_path / "BAD.yaml").write_text("ticker: BAD\n", encoding="utf-8")
    theses, errors = load_theses(tmp_path)
    assert list(theses) == ["ACME"]
    assert len(errors) == 1 and errors[0].path.name == "BAD.yaml"


def test_missing_directory_is_empty(tmp_path):
    assert load_theses(tmp_path / "nope") == ({}, [])


METRICS = ACME_THESIS + """metrics:
  gross_margin: {label: "Gross margin", unit: "%", pillar: pricing, higher_is: good}
  revenue: {label: Revenue, unit: $M}
"""


def test_metrics_load_and_do_not_change_the_version(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path, text=METRICS))
    assert [(m.id, m.unit, m.pillar, m.higher_is) for m in thesis.metrics] == [
        ("gross_margin", "%", "pricing", "good"), ("revenue", "$M", None, "neutral"),
    ]
    assert thesis.version == load_thesis(write_thesis(tmp_path)).version


@pytest.mark.parametrize(
    "old, new, field",
    [
        ('unit: "%", pillar', 'unit: "", pillar', "metrics.gross_margin.unit"),
        ("pillar: pricing, higher_is", "pillar: margins, higher_is", "metrics.gross_margin.pillar"),
        ("higher_is: good", "higher_is: up", "metrics.gross_margin.higher_is"),
        ("{label: Revenue, unit: $M}", "{label: Revenue, unit: $M, color: red}", "metrics.revenue.color"),
        ("  revenue:", "  Revenue:", "metrics.Revenue"),
    ],
)
def test_metrics_are_validated(tmp_path, old, new, field):
    _expect_error(tmp_path, METRICS.replace(old, new), field)
