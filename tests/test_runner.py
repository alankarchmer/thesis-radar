import asyncio
import sqlite3

import pytest

from helpers import ACME_THESIS, noul, write_thesis
from thesis_radar.judge import FakeJudge, JudgeError, JudgeRequest, JudgeResult
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.runner import RateLimiter, cache_key, plan_judging, run_judging
from thesis_radar.store import Store
from thesis_radar.thesis import load_thesis

MODEL = "jev-1.13.0"


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "radar.db")
    thesis = load_thesis(write_thesis(tmp_path / "thesis"))
    with store.transaction():
        sorted_id = store.insert_document(
            NewDocument(text_sha256="a" * 64, path="archive/ACME/a.txt", title="Q3 call", origin="inbox",
                        status="sorted", ticker="ACME", source_type="earnings_transcript", doc_date="2026-09-15")
        )
        store.insert_passages(sorted_id, [PassageDraft(0, 1, 0, 10, "Inventory rose."), PassageDraft(1, 1, 11, 20, "Pricing held.")])
        unsorted_id = store.insert_document(
            NewDocument(text_sha256="b" * 64, path="archive/_unsorted/b.txt", title="?", origin="inbox", status="unsorted")
        )
        store.insert_passages(unsorted_id, [PassageDraft(0, 1, 0, 5, "Hello.")])
    yield store, {"ACME": thesis}, tmp_path
    store.close()


def respond_all(request):
    return {name: noul(0.5) for name in request.questions}


def run(store, judge, items, concurrency=2):
    return asyncio.run(run_judging(store, judge, items, concurrency=concurrency, limiter=RateLimiter(60_000)))


def test_cache_key_tracks_every_input():
    base = JudgeRequest(state={"passage": "a"}, questions={"q": {"type": "noul", "instructions": "?"}}, model=MODEL)
    same = JudgeRequest(state={"passage": "a"}, questions={"q": {"type": "noul", "instructions": "?"}}, model=MODEL)
    assert cache_key(base) == cache_key(same)
    assert cache_key(base) != cache_key(JudgeRequest(state={"passage": "b"}, questions=base.questions, model=MODEL))
    assert cache_key(base) != cache_key(JudgeRequest(state=base.state, questions=base.questions, model="jev-1.14.0"))


def test_plan_covers_only_sorted_documents(setup):
    store, theses, _ = setup
    plan = plan_judging(store, theses, MODEL)
    assert [item.request.state["passage"] for item in plan.pending] == ["Inventory rose.", "Pricing held."]
    assert plan.judged == {} and plan.failed == {}
    assert plan.estimated_tokens > 0
    assert plan.estimated_cost == pytest.approx(plan.estimated_tokens * 0.042 / 1_000_000)


def test_run_commits_each_result_and_the_next_plan_skips_them(setup):
    store, theses, tmp_path = setup
    report = run(store, FakeJudge(respond_all, model=MODEL), plan_judging(store, theses, MODEL).pending, concurrency=4)
    assert (report.judged, report.failed) == (2, 0)
    assert report.input_tokens > 0
    other = sqlite3.connect(tmp_path / "radar.db")
    assert other.execute("SELECT COUNT(*) FROM judgments WHERE status = 'judged'").fetchone() == (2,)
    other.close()
    again = plan_judging(store, theses, MODEL)
    assert again.pending == [] and len(again.judged) == 2


def test_thesis_edits_invalidate_judgments(setup):
    store, theses, tmp_path = setup
    run(store, FakeJudge(respond_all), plan_judging(store, theses, MODEL).pending)
    edited = ACME_THESIS + '  pricing:\n    - "Promotions were cut."\n'
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis", text=edited))}
    assert len(plan_judging(store, theses, MODEL).pending) == 2


def test_failures_are_recorded_and_retried(setup):
    store, theses, _ = setup

    def flaky(request):
        if request.state["passage"] == "Pricing held.":
            raise JudgeError("TypeSafeInternalServerError: 529")
        return respond_all(request)

    report = run(store, FakeJudge(flaky), plan_judging(store, theses, MODEL).pending)
    assert (report.judged, report.failed) == (1, 1)
    assert "529" in report.errors[0]
    retry = plan_judging(store, theses, MODEL)
    assert [item.request.state["passage"] for item in retry.pending] == ["Pricing held."]
    assert list(retry.failed.values()) == ["TypeSafeInternalServerError: 529"]


class SlowJudge:
    def __init__(self):
        self.active = 0
        self.peak = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def judge(self, request):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return JudgeResult(answers={name: {"type": "noul", "noul": 0.5} for name in request.questions}, model=MODEL, input_tokens=10)


def test_concurrency_is_capped(setup):
    store, theses, _ = setup
    judge = SlowJudge()
    run(store, judge, plan_judging(store, theses, MODEL).pending * 5, concurrency=3)
    assert judge.peak == 3


def test_rate_limiter_spaces_request_starts():
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    limiter = RateLimiter(1200, clock=lambda: 100.0, sleep=fake_sleep)

    async def go():
        for _ in range(3):
            await limiter.wait()

    asyncio.run(go())
    assert sleeps == [pytest.approx(0.05), pytest.approx(0.10)]
