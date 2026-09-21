import pytest

from helpers import ACME_THESIS, write_thesis
from thesis_radar.thesis import ThesisError, load_thesis, load_theses

FACT = '    - "Dealer inventory was elevated at the end of Q2."'


def test_loads_a_valid_thesis(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path))
    assert thesis.ticker == "ACME"
    assert thesis.company == "ACME Snowmobiles Inc."
    assert thesis.aliases == ("ACME", "ACME Snowmobiles")
    assert list(thesis.pillars) == ["inventory", "pricing"]
    assert [a.id for a in thesis.assumptions] == ["inv_normalizes"]
    assert thesis.assumptions[0].pillar == "inventory"
    assert thesis.known_facts == {"inventory": ("Dealer inventory was elevated at the end of Q2.",)}


def test_canonical_form_lists_facts_for_every_pillar(tmp_path):
    canonical = load_thesis(write_thesis(tmp_path)).canonical()
    assert canonical["known_facts"] == {
        "inventory": ["Dealer inventory was elevated at the end of Q2."],
        "pricing": [],
    }


def test_version_is_stable_and_changes_with_facts(tmp_path):
    first = load_thesis(write_thesis(tmp_path))
    assert first.version == load_thesis(write_thesis(tmp_path)).version
    assert len(first.version) == 16
    changed = ACME_THESIS.replace(FACT, FACT + '\n    - "Promotions rose in Q3."')
    assert load_thesis(write_thesis(tmp_path, text=changed)).version != first.version


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


def test_load_theses_keeps_valid_files_and_reports_errors(tmp_path):
    write_thesis(tmp_path)
    (tmp_path / "BAD.yaml").write_text("ticker: BAD\n", encoding="utf-8")
    theses, errors = load_theses(tmp_path)
    assert list(theses) == ["ACME"]
    assert len(errors) == 1 and errors[0].path.name == "BAD.yaml"


def test_missing_directory_is_empty(tmp_path):
    assert load_theses(tmp_path / "nope") == ({}, [])
