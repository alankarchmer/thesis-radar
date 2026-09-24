import asyncio
import re
from datetime import date

import pytest
from helpers import ACME_THESIS, answer_all, choice_from, noul, write_thesis

from thesis_radar.judge import FakeJudge, JudgeError
from thesis_radar.metrics import (
    build_series,
    metric_series,
    metric_work,
    metrics_text,
    passage_requests,
    plan_metrics,
)
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.policy import Policy
from thesis_radar.rubric import NONE, NUMBERS_PER_REQUEST, UNSTATED
from thesis_radar.runner import RateLimiter, plan_judging, run_judging, run_work
from thesis_radar.similar import link_document
from thesis_radar.store import Store
from thesis_radar.thesis import Metric, load_thesis

TODAY = date(2026, 10, 25)
MODEL = "jev-1.13.0"
METRICS = """metrics:
  gross_margin: {label: "Gross margin", unit: "%", pillar: pricing, higher_is: good}
  revenue: {label: "Revenue", unit: "$M", higher_is: good}
  dealer_inventory: {label: "Dealer inventory", unit: "units", pillar: inventory, higher_is: bad}
"""
Q2 = ("Revenue was $1.85 billion in the second quarter. Gross margin was 21.8% in the second quarter. "
      "We expect full-year gross margin of 21% to 22%.")
NOTE = "We estimate third quarter gross margin of 21.0%, below the Street."
Q3 = ("Revenue was $1.92 billion in the third quarter. Gross margin was 20.6% in the third quarter. "
      "We now expect full-year gross margin of 20% to 21%. Dealer inventory ended at 41,000 units.")
RISK = "If commodity prices rose 10%, gross margin could fall by 150 basis points or more."


def add(store, digest, text, *, date_, source="filing", ticker="ACME"):
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest * 64, path=f"archive/{ticker}/{digest}.htm", title=f"Doc {digest}",
                        origin="edgar" if source == "filing" else "inbox", status="sorted", ticker=ticker,
                        source_type=source, doc_date=date_)
        )
        store.insert_passages(doc, [PassageDraft(0, 1, 0, len(text), text)])
    link_document(store, doc)
    return store.passages_for_document(doc)[0]["passage_id"]


def passage_answers(request):
    text = request.state["passage"]
    return answer_all(request, boilerplate=noul(0.9 if "could fall" in text else 0.05))


def number_answers(request):
    """Answer like a careful reader: the metric by the sentence's words, the kind by its verb, the period as written."""
    answers = {}
    numbers = {n["id"]: n for n in request.state["numbers"]}
    for name, question in request.questions.items():
        mention_id, what = name.split("__")
        sentence = numbers[mention_id]["in_sentence"].lower()
        options = list(question["criteria"])
        if what == "metric":
            pick = next((m for m, word in (("gross_margin", "margin"), ("revenue", "revenue"),
                                           ("dealer_inventory", "inventory")) if word in sentence and m in options), NONE)
        elif what == "kind":
            pick = "estimate" if "estimate" in sentence else "guidance" if "expect" in sentence else "reported"
        else:
            pick = next((key for key, text in question["criteria"].items()
                         if key != UNSTATED and re.search(r"'(.+)'", text).group(1).lower() in sentence), UNSTATED)
        answers[name] = choice_from(question, pick, 0.9)
    return answers


@pytest.fixture
def corpus(tmp_path):
    store = Store(tmp_path / "radar.db")
    thesis = load_thesis(write_thesis(tmp_path / "thesis", text=ACME_THESIS + METRICS))
    ids = {
        "q2": add(store, "a", Q2, date_="2026-07-21"),
        "note": add(store, "b", NOTE, date_="2026-09-18", source="sell_side"),
        "q3": add(store, "c", Q3, date_="2026-10-20"),
        "risk": add(store, "d", RISK, date_="2026-10-20"),
        "peer": add(store, "e", "Brrr gross margin was 30.0% in the third quarter.", date_="2026-10-19", ticker="BRRR"),
    }
    theses = {"ACME": thesis}
    plan = plan_judging(store, theses, MODEL, today=TODAY)
    asyncio.run(run_judging(store, FakeJudge(passage_answers), plan.pending, concurrency=4, limiter=RateLimiter(60000)))
    yield store, theses, ids
    store.close()


def judge_numbers(store, theses, respond=number_answers):
    plan = plan_judging(store, theses, MODEL, today=TODAY)
    numbers = plan_metrics(store, theses, MODEL, plan=plan, policy=Policy())
    judge = FakeJudge(respond)
    report = asyncio.run(run_work(judge, [metric_work(store, i) for i in numbers.pending], concurrency=4,
                                  limiter=RateLimiter(60000)))
    return numbers, report, judge


def series(store, theses):
    return {m["id"]: m for m in metric_series(store, theses["ACME"], Policy(), model=MODEL, link=lambda path, page: f"file:///{path}")}


def test_requests_only_offer_metrics_whose_unit_fits(corpus):
    store, theses, ids = corpus
    [row] = store.get_passages([ids["q3"]])
    [(part, request)] = passage_requests(theses["ACME"], row, model=MODEL)
    assert part == "k0"
    numbers = {n["number"]: n["id"] for n in request.state["numbers"]}
    assert set(numbers) == {"$1.92 billion", "20.6%", "20% to 21%", "41,000 units"}
    assert set(request.questions[f"{numbers['20.6%']}__metric"]["criteria"]) == {"gross_margin", NONE}
    assert set(request.questions[f"{numbers['$1.92 billion']}__metric"]["criteria"]) == {"revenue", NONE}
    assert set(request.questions[f"{numbers['41,000 units']}__metric"]["criteria"]) == {"dealer_inventory", NONE}
    periods = set(request.questions[f"{numbers['20.6%']}__period"]["criteria"])
    assert periods == {"2026-Q3", "FY2026", UNSTATED}
    assert request.state["metrics"]["gross_margin"] == {"label": "Gross margin", "unit": "%"}


def test_many_numbers_are_split_into_parts(corpus):
    store, theses, _ = corpus
    text = " ".join(f"Gross margin was {20 + n}.0% in period {n}." for n in range(NUMBERS_PER_REQUEST + 2))
    pid = add(store, "f", text, date_="2026-10-21")
    [row] = store.get_passages([pid])
    parts = passage_requests(theses["ACME"], row, model=MODEL)
    assert [name for name, _ in parts] == ["k0", "k1"]
    assert all(len(r.questions) <= 17 for _, r in parts)


def test_plan_skips_boilerplate_unjudged_repeats_and_peers_and_caches(corpus):
    store, theses, ids = corpus
    numbers, report, judge = judge_numbers(store, theses)
    asked = {r.state["passage"] for r in judge.requests}
    assert asked == {Q2, NOTE, Q3}
    assert report.judged == 3 and report.failed == 0
    again = plan_metrics(store, theses, MODEL, plan=plan_judging(store, theses, MODEL, today=TODAY), policy=Policy())
    assert again.pending == [] and numbers.estimated_cost > 0


def test_non_retryable_failures_are_not_resent(corpus):
    store, theses, _ = corpus

    def broken(request):
        raise JudgeError("422", retryable=False)

    judge_numbers(store, theses, broken)
    again = plan_metrics(store, theses, MODEL, plan=plan_judging(store, theses, MODEL, today=TODAY), policy=Policy())
    assert again.pending == []


def reported(metric):
    return [p["reported"]["value"] for p in metric["periods"] if p["reported"]]


def test_series_read_only_the_current_parts_after_a_metrics_edit(corpus, tmp_path):
    store, theses, _ = corpus
    text = ("Revenue was $1.1 billion, $1.2 billion, $1.3 billion, $1.4 billion, and $1.5 billion over five years. "
            "Gross margin was 19.0% in the first quarter.")
    pid = add(store, "f", text, date_="2026-10-22")
    plan = plan_judging(store, theses, MODEL, today=TODAY)
    asyncio.run(run_judging(store, FakeJudge(passage_answers), plan.pending, concurrency=4, limiter=RateLimiter(60000)))
    judge_numbers(store, theses)
    [row] = store.get_passages([pid])
    assert [part for part, _ in passage_requests(theses["ACME"], row, model=MODEL)] == ["k0", "k1"]  # 19.0% in k1
    assert 19.0 in reported(series(store, theses)["gross_margin"])

    # Dropping revenue leaves one number to ask about, so the passage now has a single part, k0, holding 19.0%.
    without_revenue = "".join(line for line in METRICS.splitlines(keepends=True) if "revenue" not in line)
    edited = {"ACME": load_thesis(write_thesis(tmp_path / "edited", text=ACME_THESIS + without_revenue))}
    assert [part for part, _ in passage_requests(edited["ACME"], row, model=MODEL)] == ["k0"]
    # Until re-judged, a part keeps its earlier answers, but only for the questions it asks now; the obsolete k1 is gone.
    stale = series(store, edited)["gross_margin"]
    assert 20.6 in reported(stale) and 19.0 not in reported(stale)

    def not_margin(request):
        answers = number_answers(request)
        if "19.0%" not in request.state["passage"]:
            return answers
        return {name: choice_from(request.questions[name], NONE, 0.9) if name.endswith("__metric") else answer
                for name, answer in answers.items()}

    judge_numbers(store, edited, not_margin)
    # Re-judged: the current k0 says 19.0% is no metric, and the old k1 that said gross margin no longer counts.
    current = series(store, edited)["gross_margin"]
    assert 20.6 in reported(current) and 19.0 not in reported(current)


def test_negative_numbers_reach_the_series(corpus):
    store, theses, _ = corpus
    pid = add(store, "g", "Gross margin was -2.5% in the first quarter of 2026.", date_="2026-10-22")
    plan = plan_judging(store, theses, MODEL, today=TODAY)
    asyncio.run(run_judging(store, FakeJudge(passage_answers), plan.pending, concurrency=4, limiter=RateLimiter(60000)))
    judge_numbers(store, theses)
    margin = series(store, theses)["gross_margin"]
    [q1] = [p for p in margin["periods"] if p["period"] == "2026-Q1"]
    assert q1["reported"]["value"] == -2.5 and q1["reported"]["passage_id"] == pid
    assert "Q1 2026   reported -2.5%" in metrics_text([margin], ticker="ACME")


def test_series_track_results_guidance_estimates_and_changes(corpus):
    store, theses, ids = corpus
    judge_numbers(store, theses)
    margin = series(store, theses)["gross_margin"]
    by_period = {p["period"]: p for p in margin["periods"]}
    assert list(by_period) == ["2026-Q2", "2026-Q3", "FY2026"]
    q3 = by_period["2026-Q3"]
    assert q3["reported"]["value"] == 20.6 and q3["reported"]["text"] == "20.6%"
    assert q3["reported"]["quote"] == "Gross margin was 20.6% in the third quarter."
    assert q3["reported"]["link"] == "file:///archive/ACME/c.htm" and q3["reported"]["kind"] == "reported"
    assert [e["value"] for e in q3["estimates"]] == [21.0] and q3["vs_estimates"] == "below"
    year = by_period["FY2026"]
    assert [(g["value"], g["high"]) for g in year["guidance"]] == [(21.0, 22.0), (20.0, 21.0)]
    assert year["guidance_change"] == "lowered" and year["reported"] is None
    assert margin["latest"] == {"period": "2026-Q3", "label": "Q3 2026", "value": 20.6, "change": pytest.approx(-1.2),
                                "direction": "bad"}
    revenue = series(store, theses)["revenue"]
    assert [p["reported"]["value"] for p in revenue["periods"]] == [1850.0, 1920.0]
    assert revenue["latest"]["change"] == pytest.approx(70.0) and revenue["latest"]["direction"] == "good"
    inventory = series(store, theses)["dealer_inventory"]
    # "ended at 41,000 units" names no period: a reported number falls back to the quarter just ended.
    assert inventory["periods"][0]["period"] == "2026-Q3" and inventory["latest"]["change"] is None


def test_low_probability_other_kinds_and_unplaced_estimates_are_dropped(corpus):
    store, theses, _ = corpus

    def unsure(request):
        answers = number_answers(request)
        for name in answers:
            if name.endswith("__metric") and "Revenue" in request.state["passage"]:
                q = request.questions[name]
                answers[name] = choice_from(q, next(o for o in q["criteria"] if o != NONE), 0.4)
            if name.endswith("__period") and "estimate" in request.state["passage"]:
                answers[name] = choice_from(request.questions[name], UNSTATED, 0.9)
        return answers

    judge_numbers(store, theses, unsure)
    result = series(store, theses)
    assert result["revenue"]["periods"] == [] and result["revenue"]["latest"] is None
    assert all(not p["estimates"] for p in result["gross_margin"]["periods"])


def test_build_series_comparisons():
    metric = Metric("gm", "Gross margin", "%", higher_is="good")

    def point(period, kind, value, high=None, date_="2026-10-20", source="filing", p=0.9, pid=1):
        return {"period": period, "period_label": period, "value": value, "high": high, "unit": "%", "text": "",
                "quote": "", "kind": kind, "passage_id": pid, "document_id": 1, "date": date_, "source_type": source,
                "title": "", "link": "", "speaker": None, "p": p}

    points = [
        point("2026-Q3", "guidance", 20.0, 21.0, "2026-07-20"),
        point("2026-Q3", "reported", 21.4, source="sell_side", pid=2),
        point("2026-Q3", "reported", 21.5, pid=3),
        point("2026-Q3", "estimate", 21.45, date_="2026-09-01"),
        point("2026-Q4", "guidance", 21.0, 22.0, "2026-07-20"),
        point("2026-Q4", "guidance", 21.0, 21.5, "2026-10-20"),
    ]
    periods = {p["period"]: p for p in build_series(metric, points)["periods"]}
    q3 = periods["2026-Q3"]
    assert q3["reported"]["value"] == 21.5  # the filing beats the sell-side note
    assert q3["vs_guidance"] == "above" and q3["vs_estimates"] == "in_line"
    assert periods["2026-Q4"]["guidance_change"] == "lowered"
    same_mid = [point("2026-Q4", "guidance", 20.0, 22.0, "2026-07-20"), point("2026-Q4", "guidance", 20.5, 21.5)]
    assert build_series(metric, same_mid)["periods"][0]["guidance_change"] == "narrowed"


def test_metrics_text(corpus):
    store, theses, _ = corpus
    judge_numbers(store, theses)
    text = metrics_text(list(series(store, theses).values()), ticker="ACME")
    assert "ACME · Gross margin (%)" in text
    assert "Q3 2026   reported 20.6% · estimates 21%  [missed estimates]" in text
    assert "FY 2026   guidance 21%–22% → 20%–21% (lowered)" in text
    assert "latest: Q3 2026 down 1.2 pts (bad)" in text
    assert "Revenue ($M)" in text and "reported $1,920M" in text
    assert metrics_text([], ticker="ACME").startswith("ACME: no metrics")
