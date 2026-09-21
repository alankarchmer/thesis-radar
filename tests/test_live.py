import asyncio
import os
import time

import pytest

from helpers import write_thesis
from thesis_radar.judge import JevJudge, JudgeRequest
from thesis_radar.rubric import document_request, passage_questions, passage_state
from thesis_radar.thesis import load_thesis

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("TYPESAFE_API_KEY"), reason="needs TYPESAFE_API_KEY"),
]
MODEL = "jev-1.13.0"


def judge_once(request):
    async def go():
        async with JevJudge() as judge:
            started = time.perf_counter()
            result = await judge.judge(request)
            return result, time.perf_counter() - started

    return asyncio.run(go())


def test_passage_rubric_round_trip(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path))
    request = JudgeRequest(
        state=passage_state(
            thesis,
            passage_text="Dealer inventory rose 12% in the quarter and management now expects it to stay elevated through spring.",
            speaker="CFO", source_type="earnings_transcript", doc_date="2026-09-15", title="ACME Q3 call",
        ),
        questions=passage_questions(thesis),
        model=MODEL,
    )
    result, seconds = judge_once(request)
    assert set(result.answers) == set(request.questions)
    assert result.model.startswith("jev-")
    for answer in result.answers.values():
        if answer["type"] != "noul":
            assert abs(sum(answer["probabilities"].values()) - 1) < 0.02
    print(f"\npassage request: {seconds:.2f}s, {result.input_tokens} input tokens, model {result.model}")


def test_document_metadata_round_trip(tmp_path):
    thesis = load_thesis(write_thesis(tmp_path))
    request, candidates = document_request(
        {"ACME": thesis}, file_name="acme-q3-call.txt", title="ACME Snowmobiles Q3 2026 earnings call",
        text="ACME Snowmobiles Q3 2026 earnings call\nSeptember 15, 2026\n\nOperator: Welcome to the call.",
        model=MODEL,
    )
    result, seconds = judge_once(request)
    assert candidates == ["September 15, 2026"]
    assert result.answers["ticker"]["choice"] == "ACME"
    print(f"\ndocument request: {seconds:.2f}s, source {result.answers['source_type']['choice']}")
