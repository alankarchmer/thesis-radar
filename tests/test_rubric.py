import pytest

from helpers import ACME_THESIS, write_thesis
from thesis_radar.rubric import (
    ASSUMPTION_PREFIX, EVIDENCE_TYPES, MATERIALITY_LEVELS, NONE, OFF_THESIS, PASSAGE_RULES, RUBRIC_VERSION,
    SOURCE_TYPES, STANCE_LEVELS, document_request, find_date_candidates, normalize_date, passage_questions,
    passage_state,
)
from thesis_radar.thesis import load_thesis


@pytest.fixture
def thesis(tmp_path):
    return load_thesis(write_thesis(tmp_path))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-09-15", "2026-09-15"),
        ("September 15, 2026", "2026-09-15"),
        ("Sep. 15, 2026", "2026-09-15"),
        ("Sept 15, 2026", "2026-09-15"),
        ("15 September 2026", "2026-09-15"),
        ("9/15/2026", "2026-09-15"),
        ("2026-02-30", None),
    ],
)
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


def test_date_candidates_are_unique_valid_and_in_text_order():
    text = "Call held September 15, 2026. Posted 2026-09-16. Again September 15, 2026. Bad 2026-13-01."
    assert find_date_candidates(text) == ["September 15, 2026", "2026-09-16"]


def test_document_request_lists_tickers_and_dates(thesis):
    request, candidates = document_request(
        {"ACME": thesis}, file_name="call.txt", title="ACME call", text="ACME call on September 15, 2026", model="jev-1.13.0"
    )
    assert candidates == ["September 15, 2026"]
    assert set(request.questions["ticker"]["criteria"]) == {"ACME", NONE}
    assert set(request.questions["source_type"]["criteria"]) == set(SOURCE_TYPES)
    assert set(request.questions["doc_date"]["criteria"]) == {"September 15, 2026", NONE}
    assert request.state == {"file_name": "call.txt", "title": "ACME call", "text": "ACME call on September 15, 2026"}
    assert request.model == "jev-1.13.0"


def test_document_request_skips_dates_when_none_are_found(thesis):
    request, candidates = document_request({"ACME": thesis}, file_name="n.txt", title="n", text="no dates here", model="m")
    assert candidates == [] and "doc_date" not in request.questions


def test_passage_questions_cover_the_rubric(thesis):
    questions = passage_questions(thesis)
    assert set(questions) == {
        "pillar", "boilerplate", "new_info", "materiality", "stance", "evidence", "forward_looking",
        f"{ASSUMPTION_PREFIX}inv_normalizes",
    }
    assert set(questions["pillar"]["criteria"]) == {"inventory", "pricing", OFF_THESIS}
    assert questions["materiality"]["criteria"] == list(MATERIALITY_LEVELS) and len(MATERIALITY_LEVELS) == 4
    assert questions["stance"]["criteria"] == list(STANCE_LEVELS) and len(STANCE_LEVELS) == 5
    assert set(questions["evidence"]["criteria"]) == set(EVIDENCE_TYPES)
    assert set(questions[f"{ASSUMPTION_PREFIX}inv_normalizes"]["criteria"]) == {"supports", "contradicts", "neither"}
    assert {q["type"] for q in questions.values()} == {"choice", "noul", "score"}


def test_ten_assumptions_make_seventeen_questions(tmp_path):
    lines = "\n".join(f'  a{n}: {{pillar: inventory, statement: "Assumption {n}."}}' for n in range(10))
    text = ACME_THESIS.replace(
        '  inv_normalizes: {pillar: inventory, statement: "Dealer inventory returns to normal within two quarters."}',
        lines,
    )
    assert len(passage_questions(load_thesis(write_thesis(tmp_path, text=text)))) == 17


def test_passage_state_carries_thesis_and_document(thesis):
    state = passage_state(
        thesis, passage_text="Inventory rose.", speaker="CFO", source_type="earnings_transcript",
        doc_date="2026-09-15", title="Q3 call",
    )
    assert state["known_facts"] == {"inventory": ["Dealer inventory was elevated at the end of Q2."], "pricing": []}
    assert state["document"] == {"source_type": "earnings_transcript", "date": "2026-09-15", "title": "Q3 call", "speaker": "CFO"}
    assert state["passage"] == "Inventory rose."
    assert state["company"] == {"name": "ACME Snowmobiles Inc.", "ticker": "ACME"}
    assert state["rules"] == list(PASSAGE_RULES)


def test_every_passage_question_points_at_the_evidence_rules(thesis):
    questions = passage_questions(thesis)
    assert all(q["instructions"]["rules"] == "Follow every rule in `rules`." for q in questions.values())
    assert any("outside knowledge" in rule for rule in PASSAGE_RULES)
    assumption = questions[f"{ASSUMPTION_PREFIX}inv_normalizes"]
    assert "opposite" in assumption["instructions"]["direction"]
    assert assumption["criteria"]["contradicts"]["what"].startswith("The passage states evidence")


def test_rubric_version_is_set():
    assert RUBRIC_VERSION
