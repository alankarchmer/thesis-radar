import asyncio
import itertools
from datetime import date

import pytest
from helpers import ACME_THESIS, answer_all, choice, choice_from, noul, score, write_thesis

from thesis_radar.judge import FakeJudge, JudgeError, estimate_tokens_for
from thesis_radar.ledger import (
    RESULTS_PER_PROMISE,
    FollowupItem,
    followup_work,
    ledger_summary,
    plan_followups,
    promise_ids,
    result_candidates,
)
from thesis_radar.models import FollowupRecord, NewDocument, PassageDraft
from thesis_radar.policy import Policy
from thesis_radar.rubric import FOLLOWUP
from thesis_radar.runner import RateLimiter, cache_key, plan_judging, run_judging, run_work
from thesis_radar.store import Store
from thesis_radar.thesis import load_thesis

MODEL = "jev-1.13.0"
TODAY = date(2026, 9, 24)
POLICY = Policy()  # pillar_p >= 0.5, boilerplate <= 0.3, materiality >= 1.5, ledger min 0.6

PROMISE = "We expect dealer inventory to normalize by the end of the second quarter."
RESULT = "Dealer inventory normalized by the end of the second quarter, down 20 percent."


def passage(pid, day, *, text=PROMISE, pillar="inventory", pillar_p=0.9, boilerplate=0.1, materiality=2.0,
            evidence="guidance", forward_looking=0.9, read_through=None, judged=True):
    p = {
        "pillar": pillar, "pillar_p": pillar_p, "boilerplate": boilerplate, "new_info": 0.5,
        "materiality": materiality, "stance": 2.0, "evidence": evidence, "forward_looking": forward_looking,
        "updates_fact": None, "updates_fact_p": None, "assumptions": {}, "questions": {},
    }
    return {
        "id": pid, "document_id": 1, "date": day, "ingested_at": "2026-09-24T08:00:00Z", "text": text,
        "read_through": read_through, "status": "current", "p": p if judged else None,
    }


def result(pid, day, text=RESULT, **kwargs):
    kwargs.setdefault("evidence", "reported_result")
    kwargs.setdefault("forward_looking", 0.1)
    return passage(pid, day, text=text, **kwargs)


# Promises and result candidates


def test_promise_ids_select_forward_looking_guidance():
    passages = [
        passage(1, "2026-01-15"),
        passage(2, "2026-01-15", evidence="management_commentary", forward_looking=0.7),
        passage(3, "2026-01-15", evidence="management_commentary", forward_looking=0.69),
        passage(4, "2026-01-15", forward_looking=0.49),
        passage(5, "2026-01-15", forward_looking=0.5),
        passage(6, "2026-01-15", materiality=1.49),
        passage(7, "2026-01-15", materiality=1.5),
        passage(8, "2026-01-15", pillar="off_thesis"),
        passage(9, "2026-01-15", pillar_p=0.49),
        passage(10, "2026-01-15", boilerplate=0.31),
        passage(11, "2026-01-15", boilerplate=0.3),
        passage(12, "2026-01-15", judged=False),
        passage(13, "2026-01-15", evidence="reported_result"),
        passage(14, None),  # dated by ingestion
    ]
    assert promise_ids(passages, POLICY) == [1, 2, 5, 7, 11, 14]
    assert promise_ids(passages[::-1], POLICY) == [14, 11, 7, 5, 2, 1]
    assert promise_ids([], POLICY) == []
    assert promise_ids(passages, Policy(materiality_min=1.0)) == [1, 2, 5, 6, 7, 11, 14]


def test_result_candidates_are_later_reported_results_on_the_same_pillar():
    promise = passage(1, "2026-01-15")
    candidates = [
        result(10, "2026-04-20"),  # yes
        result(11, "2026-01-15"),  # same day as the promise
        result(12, "2027-04-10"),  # promise day + 450: yes
        result(13, "2027-04-11"),  # + 451
        result(14, "2026-04-20", forward_looking=0.5),
        result(15, "2026-04-20", pillar="pricing"),
        result(16, "2026-04-20", evidence="guidance"),
        result(17, "2026-04-20", boilerplate=0.31),
        result(18, "2026-04-20", pillar_p=0.49),
        result(19, "2026-04-20", text="Snowmobile prices rose in Canada."),  # different topic
        result(20, "2026-04-20", read_through="BRRR"),  # a peer's result
        result(21, "2026-04-20", judged=False),
        result(22, "2025-12-01"),  # before the promise
    ]
    assert [c["id"] for c in result_candidates(promise, candidates, POLICY)] == [10, 12]
    peer_promise = passage(2, "2026-01-15", read_through="BRRR")
    assert [c["id"] for c in result_candidates(peer_promise, candidates, POLICY)] == [20]


def test_result_candidates_keep_the_most_similar_three():
    promise = passage(1, "2026-01-15")
    candidates = [
        result(10, "2026-03-01", text="Dealer inventory rose."),
        result(11, "2026-03-01", text=RESULT),
        result(12, "2026-03-02", text="Dealer inventory normalized by the end of the second quarter."),
        result(13, "2026-03-03", text="Dealer inventory normalized."),
        result(14, "2026-03-04", text="Dealer inventory normalized by the end of the quarter."),
    ]
    ranked = [c["id"] for c in result_candidates(promise, candidates, POLICY)]
    assert len(ranked) == RESULTS_PER_PROMISE == 3
    assert ranked == [12, 11, 14]  # Jaccard 4/7, 4/9, 3/7


# Planning and running follow-ups against a real store


def add_doc(store, digest, day, texts, *, ticker="ACME"):
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest * 64, path=f"archive/{ticker}/{digest}.txt", title=f"Doc {digest}",
                        origin="inbox", status="sorted", ticker=ticker, source_type="earnings_transcript",
                        doc_date=day)
        )
        ids = store.insert_passages(doc, [PassageDraft(i, 1, 0, len(t), t, speaker="CFO") for i, t in enumerate(texts)])
    return ids


BOILERPLATE = "This call contains forward-looking statements that involve risks."
PRICING = "Pricing held steady across the lineup."
LATE = "Dealer inventory normalized at the end of the second quarter again."
PEER = "Brrr dealer inventory normalized by the end of the second quarter."

# What the fake passage judge says about each passage: (evidence, forward_looking, pillar, boilerplate).
PROFILES = {
    PROMISE: ("guidance", 0.9, "inventory", 0.05),
    RESULT: ("reported_result", 0.1, "inventory", 0.05),
    BOILERPLATE: ("guidance", 0.9, "inventory", 0.95),
    PRICING: ("reported_result", 0.1, "pricing", 0.05),
    LATE: ("reported_result", 0.1, "inventory", 0.05),
    PEER: ("reported_result", 0.1, "inventory", 0.05),
}


def respond_passage(request):
    evidence, forward, pillar, boilerplate = PROFILES[request.state["passage"]]
    q = request.questions
    return answer_all(
        request,
        pillar=choice_from(q["pillar"], pillar, 0.9),
        evidence=choice_from(q["evidence"], evidence, 0.9),
        forward_looking=noul(forward),
        boilerplate=noul(boilerplate),
        materiality=score([0.0, 0.0, 1.0, 0.0]),
    )


def followup_answer(verdict, probability):
    rest = (1.0 - probability) / 2
    probabilities = {option: rest for option in ("confirms", "misses", "not_addressed")}
    probabilities[verdict] = probability
    return {FOLLOWUP: choice(verdict, probabilities)}


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "radar.db")
    text = ACME_THESIS.replace("known_facts:", "peers: [BRRR]\nknown_facts:")
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis", text=text))}
    ids = {}
    [ids[PROMISE], ids[BOILERPLATE]] = add_doc(store, "a", "2026-01-15", [PROMISE, BOILERPLATE])
    [ids[RESULT], ids[PRICING]] = add_doc(store, "b", "2026-05-01", [RESULT, PRICING])
    [ids[LATE]] = add_doc(store, "c", "2027-05-01", [LATE])  # beyond the 450-day window
    [ids[PEER]] = add_doc(store, "d", "2026-05-02", [PEER], ticker="BRRR")
    judge_plan = plan_judging(store, theses, MODEL, today=TODAY)
    asyncio.run(run_judging(store, FakeJudge(respond_passage, model=MODEL), judge_plan.pending, concurrency=2,
                            limiter=RateLimiter(60_000)))
    yield store, theses, ids, tmp_path
    store.close()


def plan(store, theses):
    judge_plan = plan_judging(store, theses, MODEL, today=TODAY)
    return plan_followups(store, theses, MODEL, plan=judge_plan, policy=POLICY)


def run(store, respond, items):
    judge = FakeJudge(respond, model=MODEL)
    work = [followup_work(store, item) for item in items]
    return asyncio.run(run_work(judge, work, concurrency=2, limiter=RateLimiter(60_000))), judge


def test_plan_pairs_each_promise_with_later_results(world):
    store, theses, ids, _ = world
    followups = plan(store, theses)
    assert [(i.ticker, i.promise_id, i.result_id) for i in followups.pending] == [("ACME", ids[PROMISE], ids[RESULT])]
    [item] = followups.pending
    state = item.request.state
    assert state["company"] == {"name": "ACME Snowmobiles Inc.", "ticker": "ACME"}
    assert state["promise"] == {"text": PROMISE, "date": "2026-01-15", "speaker": "CFO",
                                "source_type": "earnings_transcript"}
    assert state["result"] == {"text": RESULT, "date": "2026-05-01", "source_type": "earnings_transcript"}
    assert list(item.request.questions) == [FOLLOWUP] and item.request.model == MODEL
    assert item.cache_key == cache_key(item.request)
    assert followups.estimated_tokens == estimate_tokens_for(item.request) > 0
    assert followups.estimated_cost == pytest.approx(followups.estimated_tokens * 0.042 / 1_000_000)


def test_unjudged_passages_give_no_followups(tmp_path):
    store = Store(tmp_path / "radar.db")
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis"))}
    add_doc(store, "a", "2026-01-15", [PROMISE])
    add_doc(store, "b", "2026-05-01", [RESULT])
    followups = plan(store, theses)
    assert followups.pending == [] and followups.estimated_tokens == 0
    store.close()


def test_judged_followups_are_saved_and_not_planned_again(world):
    store, theses, ids, tmp_path = world
    [item] = plan(store, theses).pending
    assert isinstance(item, FollowupItem)
    report, judge = run(store, lambda request: followup_answer("confirms", 0.8), [item])
    assert (report.judged, report.failed) == (1, 0) and judge.requests == [item.request]
    row = store.followup(ids[PROMISE], ids[RESULT], item.cache_key)
    assert (row["status"], row["model"], row["ticker"], row["error"]) == ("judged", MODEL, "ACME", None)
    assert row["input_tokens"] == estimate_tokens_for(item.request)
    assert '"confirms"' in row["answers_json"]
    assert plan(store, theses).pending == []

    # A different request (the company is renamed) has a new cache key and is planned again.
    renamed = ACME_THESIS.replace("company: ACME Snowmobiles Inc.", "company: ACME Sleds Inc.")
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis", text=renamed))}
    [again] = plan(store, theses).pending
    assert again.cache_key != item.cache_key and again.request.state["company"]["name"] == "ACME Sleds Inc."


def test_retryable_failures_are_planned_again_and_permanent_ones_are_not(world):
    store, theses, ids, _ = world

    def overloaded(request):
        raise JudgeError("TypeSafeInternalServerError: 529")

    report, _ = run(store, overloaded, plan(store, theses).pending)
    assert (report.judged, report.failed) == (0, 1)
    [row] = store.followups_for_ticker("ACME")
    assert (row["status"], bool(row["retryable"]), row["error"]) == ("failed", True, "TypeSafeInternalServerError: 529")
    assert row["model"] == MODEL
    [retry] = plan(store, theses).pending

    def rejected(request):
        raise JudgeError("TypeSafeUnprocessableEntityError: 422", retryable=False)

    run(store, rejected, [retry])
    [row] = store.followups_for_ticker("ACME")
    assert (row["status"], row["retryable"]) == ("failed", 0)
    assert plan(store, theses).pending == []


def test_followup_work_label_names_the_pair(world):
    store, theses, ids, _ = world
    [item] = plan(store, theses).pending
    work = followup_work(store, item)
    assert work.label == f"ACME follow-up {ids[PROMISE]}→{ids[RESULT]}"
    assert work.request is item.request


# The ledger summary


@pytest.fixture
def ledger_store(tmp_path):
    ticks = (f"2026-09-24T10:{minute:02d}:00Z" for minute in itertools.count())
    store = Store(tmp_path / "radar.db", clock=lambda: next(ticks))
    ids = add_doc(store, "a", "2026-01-01", [f"Passage number {i}." for i in range(20)])
    add_doc(store, "b", "2026-01-01", ["Peer passage."], ticker="BRRR")
    thesis = load_thesis(write_thesis(tmp_path / "thesis"))
    yield store, thesis, ids
    store.close()


def save(store, promise_id, result_id, verdict, probability, *, key="k1", ticker="ACME", status="judged"):
    store.save_followup(
        FollowupRecord(
            promise_id=promise_id, result_id=result_id, ticker=ticker, cache_key=key, model=MODEL, status=status,
            answers=followup_answer(verdict, probability) if status == "judged" else None,
            error=None if status == "judged" else "boom",
        )
    )


def test_ledger_summary_verdicts(ledger_store):
    store, thesis, ids = ledger_store
    p1, p2, p3, p4, p5, p6, p7 = ids[:7]
    r1, r2, r3, r4, r5, r_missing = ids[10:16]
    passages = [
        passage(p1, "2026-01-15"), passage(p2, "2026-02-01"), passage(p3, "2026-03-01"),
        passage(p4, "2026-03-05"), passage(p5, "2026-03-10"), passage(p6, "2026-03-15"),
        passage(p7, "2026-03-20"),
        result(r1, "2026-04-01"), result(r2, "2026-07-01"), result(r3, "2026-07-01"),
        result(r4, "2026-08-01"), result(r5, "2026-08-01"),
    ]
    by_id = {p["id"]: p for p in passages}
    save(store, p1, r1, "confirms", 0.8)
    save(store, p1, r2, "misses", 0.7)  # the later result decides: missed
    save(store, p2, r1, "confirms", 0.65)  # kept
    save(store, p3, r2, "not_addressed", 0.9)  # open
    save(store, p3, r_missing, "confirms", 0.95)  # result not on the page: ignored
    save(store, p4, r3, "confirms", 0.9, key="old")
    save(store, p4, r3, "misses", 0.8, key="new")  # newest row for the pair wins: missed
    save(store, p4, r3, "confirms", 0.9, key="newer", status="failed")  # a failure does not erase it
    save(store, p5, r4, "confirms", 0.7)
    save(store, p5, r5, "misses", 0.9)  # same day: higher probability decides
    save(store, p6, r1, "confirms", 0.59)  # below ledger.min_probability: open
    save(store, p7, r1, "confirms", 0.9, ticker="BRRR")  # another thesis's follow-up
    summary = ledger_summary(store, thesis, by_id, POLICY)
    assert summary["items"] == [
        {"promise_id": p7, "status": "open", "result_id": None, "p": None},
        {"promise_id": p6, "status": "open", "result_id": None, "p": None},
        {"promise_id": p5, "status": "missed", "result_id": r5, "p": 0.9},
        {"promise_id": p4, "status": "missed", "result_id": r3, "p": 0.8},
        {"promise_id": p3, "status": "open", "result_id": None, "p": None},
        {"promise_id": p2, "status": "kept", "result_id": r1, "p": 0.65},
        {"promise_id": p1, "status": "missed", "result_id": r2, "p": 0.7},
    ]
    assert (summary["kept"], summary["missed"], summary["open"]) == (1, 3, 3)
    assert summary["credibility"] == 0.25

    lenient = ledger_summary(store, thesis, by_id, Policy(ledger_min_probability=0.55))
    assert {i["promise_id"]: i["status"] for i in lenient["items"]}[p6] == "kept"


def test_ledger_summary_without_verdicts(ledger_store):
    store, thesis, ids = ledger_store
    empty = {"items": [], "kept": 0, "missed": 0, "open": 0, "credibility": None}
    assert ledger_summary(store, thesis, {}, POLICY) == empty
    not_promises = {ids[0]: result(ids[0], "2026-04-01"), ids[1]: passage(ids[1], "2026-01-01", judged=False)}
    assert ledger_summary(store, thesis, not_promises, POLICY) == empty
    only_open = {ids[0]: passage(ids[0], "2026-01-15")}
    summary = ledger_summary(store, thesis, only_open, POLICY)
    assert summary["open"] == 1 and summary["credibility"] is None


def test_ledger_summary_reads_what_plan_and_run_saved(world):
    store, theses, ids, _ = world
    run(store, lambda request: followup_answer("misses", 0.85), plan(store, theses).pending)
    by_id = {
        ids[PROMISE]: passage(ids[PROMISE], "2026-01-15"),
        ids[RESULT]: result(ids[RESULT], "2026-05-01"),
    }
    summary = ledger_summary(store, theses["ACME"], by_id, POLICY)
    assert summary["items"] == [{"promise_id": ids[PROMISE], "status": "missed", "result_id": ids[RESULT], "p": 0.85}]
    assert summary["credibility"] == 0.0
