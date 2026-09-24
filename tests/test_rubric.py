import pytest
from helpers import ACME_THESIS, write_thesis

from thesis_radar.rubric import (
    ASSUMPTION_PREFIX,
    EVIDENCE_TYPES,
    FOLLOWUP,
    MATERIALITY_LEVELS,
    MAX_QUESTIONS_PER_REQUEST,
    NONE,
    OFF_THESIS,
    PASSAGE_RULES,
    QUESTION_PREFIX,
    RUBRIC_VERSION,
    SOURCE_TYPES,
    STANCE_LEVELS,
    UPDATES_FACT,
    document_request,
    find_date_candidates,
    followup_request,
    normalize_date,
    passage_questions,
    passage_state,
    question_parts,
)
from thesis_radar.thesis import load_thesis


@pytest.fixture
def thesis(tmp_path):
    return load_thesis(write_thesis(tmp_path))


def big_thesis(tmp_path):
    lines = "\n".join(f'  a{n}: {{pillar: inventory, statement: "Assumption {n}."}}' for n in range(10))
    text = ACME_THESIS.replace(
        '  inv_normalizes: {pillar: inventory, statement: "Dealer inventory returns to normal within two quarters."}',
        lines,
    ).replace("known_facts:", "open_questions:\n" + "\n".join(f"  q{n}: Question {n}?" for n in range(5)) + "\nknown_facts:")
    return load_thesis(write_thesis(tmp_path, text=text))


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


def test_document_request_skips_dates_when_none_are_found(thesis):
    request, candidates = document_request({"ACME": thesis}, file_name="n.txt", title="n", text="no dates here", model="m")
    assert candidates == [] and "doc_date" not in request.questions


def test_passage_questions_cover_the_rubric(thesis):
    questions = passage_questions(thesis, thesis.facts())
    assert list(questions) == [
        "pillar", "boilerplate", "new_info", "materiality", "stance", "evidence", "forward_looking", UPDATES_FACT,
        f"{ASSUMPTION_PREFIX}inv_normalizes",
    ]
    assert set(questions["pillar"]["criteria"]) == {"inventory", "pricing", OFF_THESIS}
    assert questions["materiality"]["criteria"] == list(MATERIALITY_LEVELS) and len(MATERIALITY_LEVELS) == 4
    assert questions["stance"]["criteria"] == list(STANCE_LEVELS) and len(STANCE_LEVELS) == 5
    assert set(questions["evidence"]["criteria"]) == set(EVIDENCE_TYPES)
    assert set(questions[UPDATES_FACT]["criteria"]) == {"inventory.0", NONE}
    assert set(questions[f"{ASSUMPTION_PREFIX}inv_normalizes"]["criteria"]) == {"supports", "contradicts", "neither"}
    assert {q["type"] for q in questions.values()} == {"choice", "noul", "score"}


def test_no_fact_question_without_facts(thesis):
    assert UPDATES_FACT not in passage_questions(thesis, [])


def test_large_theses_split_into_parts_of_at_most_seventeen(tmp_path):
    thesis = big_thesis(tmp_path)
    questions = passage_questions(thesis, thesis.facts())
    assert len(questions) == 8 + 10 + 5
    assert sum(1 for name in questions if name.startswith(QUESTION_PREFIX)) == 5
    parts = question_parts(questions)
    assert [name for name, _ in parts] == ["p0", "p1"]
    assert [len(chunk) for _, chunk in parts] == [MAX_QUESTIONS_PER_REQUEST, 6]
    assert list(parts[0][1])[0] == "pillar"


def test_passage_state_carries_thesis_document_context_and_seen(thesis):
    state = passage_state(
        thesis, passage_text="Inventory rose.", speaker="CFO", source_type="earnings_transcript",
        doc_date="2026-09-15", title="Q3 call", context="x" * 700, about="BRRR",
        previously_seen=[{"date": "2026-06-01", "source_type": "filing", "text": "Inventory rose before.", "extra": 1}],
    )
    assert state["known_facts"] == {"inventory": ["Dealer inventory was elevated at the end of Q2."], "pricing": []}
    assert state["document"] == {
        "source_type": "earnings_transcript", "date": "2026-09-15", "title": "Q3 call", "speaker": "CFO", "about": "BRRR",
    }
    assert state["passage"] == "Inventory rose."
    assert state["company"] == {"name": "ACME Snowmobiles Inc.", "ticker": "ACME"}
    assert state["rules"] == list(PASSAGE_RULES)
    assert len(state["context"]) <= 602
    assert state["previously_seen"] == [{"date": "2026-06-01", "source_type": "filing", "text": "Inventory rose before."}]


def test_every_passage_question_points_at_the_evidence_rules(thesis):
    questions = passage_questions(thesis, thesis.facts())
    assert all(q["instructions"]["rules"] == "Follow every rule in `rules`." for q in questions.values())
    assert any("outside knowledge" in rule for rule in PASSAGE_RULES)
    assert any("previously_seen" in rule for rule in PASSAGE_RULES)
    assumption = questions[f"{ASSUMPTION_PREFIX}inv_normalizes"]
    assert "opposite" in assumption["instructions"]["direction"]
    assert assumption["criteria"]["contradicts"]["what"].startswith("The passage states evidence")


def test_followup_request(thesis):
    request = followup_request(
        thesis, promise={"text": "We expect inventory to normalize by spring.", "doc_date": "2026-03-01"},
        result={"text": "Inventory is back to normal.", "doc_date": "2026-06-01", "source_type": "filing"}, model="m",
    )
    assert set(request.questions[FOLLOWUP]["criteria"]) == {"confirms", "misses", "not_addressed"}
    assert request.state["promise"]["date"] == "2026-03-01"


def test_rubric_version_is_set():
    assert RUBRIC_VERSION
