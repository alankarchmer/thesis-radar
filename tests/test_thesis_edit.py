import re

import pytest
from helpers import ACME_THESIS, write_thesis

from thesis_radar.thesis import load_thesis
from thesis_radar.thesis_edit import add_fact

COMMENTED = """\
# ACME: the snowmobile thesis
ticker: ACME
company: ACME Snowmobiles Inc.   # parent company
aliases: [ACME, "ACME Snowmobiles"]
pillars:
  inventory: Units on dealer lots and how fast they sell through.
  pricing: 'List pricing, promotions, and discounting.'
assumptions:
  inv_normalizes: {pillar: inventory, statement: "Dealer inventory returns to normal within two quarters."}
predictions:
  inv_back: {statement: "Inventory is normal by Q1.", by: 2027-03-31, p: 0.6, pillar: inventory}
known_facts:
  inventory:
    - "Dealer inventory was elevated at the end of Q2."   # from the Q2 call
    - {text: "Promotions up 200 bps.", as_of: 2026-08-05, source: 12}
  # pricing facts go below
  pricing:
    - List prices rose 3%.
# end of file
"""
LONG = "Dealer inventory fell 14% year over year while retail sell-through rose, " * 3


def edit(tmp_path, text=COMMENTED):
    return write_thesis(tmp_path / "thesis", text=text)


def test_appends_a_plain_fact_changing_nothing_else(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "inventory", f"  {LONG}  ") == "inventory.2"
    expected = COMMENTED.replace(
        "source: 12}\n", f'source: 12}}\n    - "{LONG.strip()}"\n'
    )
    assert path.read_text() == expected
    assert load_thesis(path).fact("inventory.2").text == LONG.strip()


def test_appending_keeps_trailing_comments_after_the_new_fact(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "pricing", 'Net price "held": flat') == "pricing.1"
    expected = COMMENTED.replace(
        "    - List prices rose 3%.\n", '    - List prices rose 3%.\n    - "Net price \\"held\\": flat"\n'
    )
    assert path.read_text() == expected
    assert load_thesis(path).fact("pricing.1").text == 'Net price "held": flat'


def test_dated_sourced_fact_is_a_flow_mapping(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "pricing", "Promotions ended.", source=77, as_of="2026-09-01") == "pricing.1"
    assert '    - {text: "Promotions ended.", as_of: 2026-09-01, source: 77}\n' in path.read_text()
    fact = load_thesis(path).fact("pricing.1")
    assert (fact.text, fact.as_of, fact.source) == ("Promotions ended.", "2026-09-01", 77)
    add_fact(path, "pricing", "Only a source.", source=5)
    add_fact(path, "pricing", "Only a date.", as_of="2026-09-02")
    text = path.read_text()
    assert '{text: "Only a source.", source: 5}' in text
    assert '{text: "Only a date.", as_of: 2026-09-02}' in text


def test_keeps_the_files_own_indentation(tmp_path):
    text = """\
ticker: ACME
company: ACME
pillars:
    inventory: Units.
    pricing: Prices.
known_facts:
    inventory:
    - 'a fact'
"""
    path = edit(tmp_path, text)
    add_fact(path, "inventory", "b fact")
    add_fact(path, "pricing", "c fact")
    assert path.read_text() == text + '    - "b fact"\n    pricing:\n    - "c fact"\n'


def test_keeps_comments_above_an_explicit_document_start(tmp_path):
    text = "# header\n---\n" + ACME_THESIS
    path = edit(tmp_path, text)
    add_fact(path, "inventory", "New.")
    assert path.read_text() == text + '    - "New."\n'


@pytest.mark.parametrize(
    "tail",
    ["", "known_facts:\n", "known_facts:   # none yet\n", "known_facts: {}\n", "known_facts:\n  pricing: []\n"],
)
def test_creates_known_facts_and_the_pillar_list(tmp_path, tail):
    base = ACME_THESIS.split("known_facts:")[0]
    path = edit(tmp_path, base + tail)
    assert add_fact(path, "inventory", "First fact.") == "inventory.0"
    assert add_fact(path, "pricing", "Second fact.") == "pricing.0"
    thesis = load_thesis(path)
    assert [f.text for f in thesis.facts()] == ["First fact.", "Second fact."]
    if "# none yet" in tail:
        assert "known_facts:   # none yet\n" in path.read_text()


def test_replace_in_the_same_pillar_keeps_the_index_and_comment(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "inventory", "Inventory normalized.", replace="inventory.0") == "inventory.0"
    text = path.read_text()
    assert re.search(r'\n    - "Inventory normalized\." +# from the Q2 call\n', text)
    assert "elevated at the end of Q2" not in text
    thesis = load_thesis(path)
    assert [f.text for f in thesis.known_facts["inventory"]] == ["Inventory normalized.", "Promotions up 200 bps."]


def test_replace_from_another_pillar_moves_the_fact(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "pricing", "Promotions ended.", replace="inventory.1", as_of="2026-09-20") == "pricing.1"
    text = path.read_text()
    assert "Promotions up 200 bps" not in text
    assert "  # pricing facts go below\n  pricing:\n" in text
    assert text.endswith('    - {text: "Promotions ended.", as_of: 2026-09-20}\n# end of file\n')
    thesis = load_thesis(path)
    assert [f.text for f in thesis.known_facts["inventory"]] == ["Dealer inventory was elevated at the end of Q2."]
    assert [f.text for f in thesis.known_facts["pricing"]] == ["List prices rose 3%.", "Promotions ended."]


def test_replacing_the_only_fact_of_a_pillar_leaves_an_empty_list(tmp_path):
    path = edit(tmp_path)
    assert add_fact(path, "inventory", "Moved.", replace="pricing.0") == "inventory.2"
    assert "  pricing: []\n" in path.read_text()
    assert load_thesis(path).known_facts["pricing"] == ()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"pillar": "margins", "text": "x"}, "unknown pillar 'margins'"),
        ({"pillar": "inventory", "text": "   "}, "must not be empty"),
        ({"pillar": "inventory", "text": "x", "replace": "inventory.2"}, "no known fact inventory.2"),
        ({"pillar": "inventory", "text": "x", "replace": "margins.0"}, "no known fact margins.0"),
        ({"pillar": "inventory", "text": "x", "replace": "inventory"}, "fact ids look like"),
        ({"pillar": "inventory", "text": "x", "as_of": "2026-13-01"}, "as_of must be a date"),
        ({"pillar": "inventory", "text": "x", "source": "12"}, "source must be a passage id"),
    ],
)
def test_rejects_bad_edits_without_touching_the_file(tmp_path, kwargs, message):
    path = edit(tmp_path)
    with pytest.raises(ValueError, match=message):
        add_fact(path, kwargs.pop("pillar"), kwargs.pop("text"), **kwargs)
    assert path.read_text() == COMMENTED


def test_a_full_pillar_suggests_replace(tmp_path):
    facts = "".join(f'    - "Fact {i}."\n' for i in range(20))
    text = ACME_THESIS.split("known_facts:")[0] + "known_facts:\n  inventory:\n" + facts + '  pricing:\n    - "P."\n'
    path = edit(tmp_path, text)
    with pytest.raises(ValueError, match=r"--replace inventory\.<index>"):
        add_fact(path, "inventory", "One too many.")
    with pytest.raises(ValueError, match="already has 20 known facts"):
        add_fact(path, "inventory", "Moved in.", replace="pricing.0")
    assert path.read_text() == text
    assert add_fact(path, "inventory", "Swapped.", replace="inventory.19") == "inventory.19"


def test_an_edit_that_fails_validation_leaves_the_original(tmp_path):
    broken = COMMENTED.replace("aliases:", "notes: something\naliases:")
    path = edit(tmp_path, broken)
    with pytest.raises(ValueError, match="notes: unknown key"):
        add_fact(path, "inventory", "New.")
    assert path.read_text() == broken
    assert sorted(p.name for p in path.parent.iterdir()) == ["ACME.yaml"]


def test_the_validated_file_replaces_the_original_atomically(tmp_path, monkeypatch):
    import thesis_radar.thesis_edit as thesis_edit

    path = edit(tmp_path)
    seen = []
    monkeypatch.setattr(thesis_edit, "load_thesis", lambda candidate: seen.append(candidate) or load_thesis(candidate))
    add_fact(path, "inventory", "New.")
    [candidate] = seen
    assert candidate.name == "ACME.yaml" and candidate.parent.parent == path.parent
    assert not candidate.parent.exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["ACME.yaml"]
