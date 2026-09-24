import asyncio
import sqlite3
from datetime import date

import pytest
from helpers import ACME_THESIS, answer_all, noul, write_thesis

from thesis_radar.judge import FakeJudge, JudgeError, JudgeFatal, JudgeRequest, JudgeResult
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.runner import RateLimiter, cache_key, plan_judging, run_judging
from thesis_radar.similar import link_document
from thesis_radar.store import Store
from thesis_radar.thesis import load_thesis

MODEL = "jev-1.13.0"
TODAY = date(2026, 9, 24)


def add_doc(store, digest, date_, texts, *, ticker="ACME", status="sorted"):
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest, path=f"archive/{ticker}/{digest[:3]}.txt", title=f"Doc {digest[:3]}",
                        origin="inbox", status=status, ticker=ticker if status == "sorted" else None,
                        source_type="earnings_transcript", doc_date=date_)
        )
        store.insert_passages(doc, [PassageDraft(i, 1, 0, 10, t) for i, t in enumerate(texts)])
    link_document(store, doc)
    return doc


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "radar.db")
    thesis = load_thesis(write_thesis(tmp_path / "thesis"))
    add_doc(store, "a" * 64, "2026-09-15", ["Inventory rose.", "Pricing held."])
    add_doc(store, "b" * 64, "2026-09-16", ["Hello."], status="unsorted")
    yield store, {"ACME": thesis}, tmp_path
    store.close()


def respond_all(request):
    return answer_all(request)


def plan(store, theses, **kwargs):
    return plan_judging(store, theses, MODEL, today=TODAY, **kwargs)


def run(store, judge, items, concurrency=2):
    return asyncio.run(run_judging(store, judge, items, concurrency=concurrency, limiter=RateLimiter(60_000)))


def test_cache_key_tracks_every_input():
    base = JudgeRequest(state={"passage": "a"}, questions={"q": {"type": "noul", "instructions": "?"}}, model=MODEL)
    same = JudgeRequest(state={"passage": "a"}, questions={"q": {"type": "noul", "instructions": "?"}}, model=MODEL)
    assert cache_key(base) == cache_key(same)
    assert cache_key(base) != cache_key(JudgeRequest(state={"passage": "b"}, questions=base.questions, model=MODEL))
    assert cache_key(base) != cache_key(JudgeRequest(state=base.state, questions=base.questions, model="jev-1.14.0"))


def test_plan_covers_only_sorted_documents_and_carries_context(setup):
    store, theses, _ = setup
    p = plan(store, theses)
    assert [item.request.state["passage"] for item in p.pending] == ["Inventory rose.", "Pricing held."]
    assert p.pending[1].request.state["context"] == "Inventory rose."
    assert p.pending[0].request.state["context"] is None
    assert p.count("unjudged") == 2 and p.failed == {}
    assert p.estimated_cost == pytest.approx(p.estimated_tokens * 0.042 / 1_000_000)


def test_run_commits_each_result_and_the_next_plan_skips_them(setup):
    store, theses, tmp_path = setup
    report = run(store, FakeJudge(respond_all, model=MODEL), plan(store, theses).pending, concurrency=4)
    assert (report.judged, report.failed) == (2, 0)
    assert report.input_tokens > 0
    other = sqlite3.connect(tmp_path / "radar.db")
    assert other.execute("SELECT COUNT(*) FROM judgments WHERE status = 'judged' AND ticker = 'ACME'").fetchone() == (2,)
    other.close()
    again = plan(store, theses)
    assert again.pending == [] and again.count("current") == 2
    pid = store.passages_for_ticker("ACME")[0]["passage_id"]
    assert again.answers("ACME", pid)["pillar"]["choice"] == "inventory"
    assert len(again.keys("ACME", pid)) == 1


def test_thesis_edits_fall_back_to_stale_answers_and_respect_the_window(setup):
    store, theses, tmp_path = setup
    old_doc = add_doc(store, "c" * 64, "2026-01-10", ["Old inventory note."])
    run(store, FakeJudge(respond_all), plan(store, theses).pending)
    edited = ACME_THESIS + '  pricing:\n    - "Promotions were cut."\n'
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis", text=edited))}
    after = plan(store, theses, rejudge_window_days=120)
    assert sorted(item.request.state["passage"] for item in after.pending) == ["Inventory rose.", "Pricing held."]
    assert after.stale_kept == 1 and after.count("stale") == 3
    old_pid = store.passages_for_document(old_doc)[0]["passage_id"]
    assert after.status("ACME", old_pid) == "stale" and after.answers("ACME", old_pid) is not None
    assert len(plan(store, theses, include_all=True).pending) == 3


def test_repeats_are_never_judged(setup):
    store, theses, _ = setup
    add_doc(store, "d" * 64, "2026-09-20", ["Inventory rose.", "A new sentence about pricing power."])
    texts = [item.request.state["passage"] for item in plan(store, theses).pending]
    assert texts.count("Inventory rose.") == 1 and "A new sentence about pricing power." in texts


def test_failures_are_recorded_retried_or_blocked(setup):
    store, theses, _ = setup

    def flaky(request):
        if request.state["passage"] == "Pricing held.":
            raise JudgeError("TypeSafeInternalServerError: 529")
        if request.state["passage"] == "Inventory rose.":
            raise JudgeError("TypeSafeUnprocessableEntityError: 422", retryable=False)
        return respond_all(request)

    report = run(store, FakeJudge(flaky), plan(store, theses).pending)
    assert (report.judged, report.failed) == (0, 2)
    retry = plan(store, theses)
    assert [item.request.state["passage"] for item in retry.pending] == ["Pricing held."]
    assert retry.blocked == 1 and len(retry.failed) == 2
    assert len(plan(store, theses, retry_failed=True).pending) == 2


def test_unexpected_errors_do_not_stop_the_run(setup):
    store, theses, _ = setup

    def broken(request):
        if request.state["passage"] == "Pricing held.":
            return {"new_info": {"type": "noul"}}  # malformed
        return respond_all(request)

    report = run(store, FakeJudge(broken), plan(store, theses).pending)
    assert (report.judged, report.failed) == (1, 1)
    assert "malformed" in report.errors[0] or "missing" in report.errors[0]


def test_fatal_errors_stop_the_run(setup):
    store, theses, _ = setup

    def reject(request):
        raise JudgeFatal("TypeSafeAuthenticationError: 401")

    with pytest.raises(JudgeFatal):
        run(store, FakeJudge(reject), plan(store, theses).pending)
    assert plan(store, theses).failed == {}


def test_peers_are_judged_against_the_host_thesis(setup):
    store, _, tmp_path = setup
    text = ACME_THESIS.replace("known_facts:", "peers: [BRRR]\nknown_facts:")
    theses = {"ACME": load_thesis(write_thesis(tmp_path / "thesis", text=text))}
    add_doc(store, "e" * 64, "2026-09-18", ["Brrr dealers cut orders."], ticker="BRRR")
    items = plan(store, theses).pending
    peer = [item for item in items if item.request.state["passage"] == "Brrr dealers cut orders."]
    assert peer and peer[0].ticker == "ACME" and peer[0].request.state["document"]["about"] == "BRRR"


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
        return JudgeResult(answers={name: noul(0.5) for name in request.questions}, model=MODEL, input_tokens=10)


def test_concurrency_is_capped(setup):
    store, theses, _ = setup
    judge = SlowJudge()
    run(store, judge, plan(store, theses).pending * 5, concurrency=3)
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
