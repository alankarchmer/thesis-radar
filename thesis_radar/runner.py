"""Plan and run judging: cache keys, parts, stale fallback, cost estimates, rate limiting, incremental commits.

A passage's questions are split into parts of at most 17 (rubric.question_parts); each part is one
request with its own cache key. A passage's answers are the merge of its parts. When a part has no
judgment at the current key (a thesis edit, a new question), the newest judged answers for that part
are used instead and the passage is marked stale. Stale passages from documents older than
`rejudge_window_days` are not re-judged unless asked (`include_all`), so a thesis edit re-judges
recent material, not the whole history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import PRICE_PER_MILLION_INPUT_TOKENS
from .judge import Judge, JudgeError, JudgeFatal, JudgeRequest, JudgeResult, classify_error, estimate_tokens_for
from .models import JudgmentRecord
from .rubric import FACT_CANDIDATES, RUBRIC_VERSION, passage_questions, passage_state, question_parts
from .similar import rank_facts
from .store import Store
from .thesis import Thesis


def cache_key(request: JudgeRequest) -> str:
    blob = json.dumps(request.payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingItem:
    passage_id: int
    ticker: str
    part: str
    thesis_version: str
    cache_key: str
    request: JudgeRequest
    doc_day: str | None = None


@dataclass
class PassageJudgment:
    answers: dict[str, Any] | None
    status: str  # "current", "stale", or "unjudged"
    keys: list[str]  # current cache keys, one per part


@dataclass
class JudgePlan:
    pending: list[PendingItem]
    passages: dict[tuple[str, int], PassageJudgment]
    failed: dict[tuple[str, int], str]
    blocked: int
    stale_kept: int
    estimated_tokens: int

    @property
    def estimated_cost(self) -> float:
        return self.estimated_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000

    def answers(self, ticker: str, passage_id: int) -> dict[str, Any] | None:
        judgment = self.passages.get((ticker, passage_id))
        return None if judgment is None else judgment.answers

    def status(self, ticker: str, passage_id: int) -> str:
        judgment = self.passages.get((ticker, passage_id))
        return "unjudged" if judgment is None else judgment.status

    def keys(self, ticker: str, passage_id: int) -> list[str]:
        judgment = self.passages.get((ticker, passage_id))
        return [] if judgment is None else list(judgment.keys)

    def count(self, status: str) -> int:
        return sum(1 for judgment in self.passages.values() if judgment.status == status)

    @property
    def judged(self) -> dict[tuple[str, int], dict[str, Any]]:
        return {key: j.answers for key, j in self.passages.items() if j.status == "current" and j.answers}


def judged_rows(store: Store, thesis: Thesis) -> list[Any]:
    """The passages judged against `thesis`: its own sorted documents plus its peers', minus exact repeats."""
    rows = store.passages_for_tickers([thesis.ticker, *thesis.peers])
    return [row for row in rows if row["repeat_of"] is None]


def build_requests(
    thesis: Thesis, row: Any, *, model: str, context: str | None, seen: Sequence[Mapping[str, Any]]
) -> list[tuple[str, JudgeRequest]]:
    candidates = rank_facts(row["text"], thesis.facts(), FACT_CANDIDATES)
    questions = passage_questions(thesis, candidates)
    about = row["ticker"] if row["ticker"] != thesis.ticker else None
    state = passage_state(
        thesis, passage_text=row["text"], speaker=row["speaker"], source_type=row["source_type"],
        doc_date=row["doc_date"], title=row["title"], context=context, previously_seen=seen, about=about,
    )
    return [(part, JudgeRequest(state=state, questions=chunk, model=model)) for part, chunk in question_parts(questions)]


def _contexts(rows: Sequence[Any]) -> dict[int, str | None]:
    """The previous passage's text within the same document, for every passage (including repeats)."""
    contexts: dict[int, str | None] = {}
    previous: dict[int, str] = {}
    for row in rows:
        contexts[row["passage_id"]] = previous.get(row["document_id"])
        previous[row["document_id"]] = row["text"]
    return contexts


def plan_judging(
    store: Store,
    theses: Mapping[str, Thesis],
    model: str,
    *,
    today: date | None = None,
    rejudge_window_days: int = 120,
    include_all: bool = False,
    retry_failed: bool = False,
) -> JudgePlan:
    today = today or date.today()
    window_start = (today - timedelta(days=rejudge_window_days)).isoformat()
    pending: list[PendingItem] = []
    passages: dict[tuple[str, int], PassageJudgment] = {}
    failed: dict[tuple[str, int], str] = {}
    blocked = stale_kept = 0

    for ticker, thesis in sorted(theses.items()):
        version = thesis.version
        all_rows = store.passages_for_tickers([ticker, *thesis.peers])
        contexts = _contexts(all_rows)
        rows = [row for row in all_rows if row["repeat_of"] is None]
        seen_rows = _seen_rows(store, rows)
        index: dict[tuple[int, str], Any] = {}
        latest: dict[tuple[int, str], dict[str, Any]] = {}
        for judgment in store.judgments_for_ticker(ticker):
            index[(judgment["passage_id"], judgment["cache_key"])] = judgment
            if judgment["status"] == "judged":
                latest[(judgment["passage_id"], judgment["part"])] = json.loads(judgment["answers_json"])

        for row in rows:
            pid = row["passage_id"]
            doc_day = row["doc_date"] or row["ingested_at"][:10]
            seen = [
                {"date": s["doc_date"], "source_type": s["source_type"], "text": s["text"]}
                for s in (seen_rows.get(i) for i in json.loads(row["seen_ids"] or "[]"))
                if s is not None
            ]
            requests = build_requests(thesis, row, model=model, context=contexts.get(pid), seen=seen)
            merged: dict[str, Any] = {}
            keys: list[str] = []
            all_current = True
            for part, request in requests:
                key = cache_key(request)
                keys.append(key)
                existing = index.get((pid, key))
                if existing is not None and existing["status"] == "judged":
                    merged.update(json.loads(existing["answers_json"]))
                    continue
                all_current = False
                stale = latest.get((pid, part))
                if stale is not None:
                    merged.update(stale)
                if existing is not None:
                    failed[(ticker, pid)] = existing["error"] or "failed"
                    if not existing["retryable"] and not retry_failed:
                        blocked += 1
                        continue
                if stale is not None and not include_all and doc_day < window_start:
                    stale_kept += 1
                    continue
                pending.append(PendingItem(pid, ticker, part, version, key, request, doc_day))
            if all_current:
                status = "current"
            elif "pillar" in merged:
                status = "stale"
            else:
                status = "unjudged"
            passages[(ticker, pid)] = PassageJudgment(merged or None, status, keys)

    pending.sort(key=lambda item: (item.doc_day or "", -item.passage_id), reverse=True)
    estimated = sum(estimate_tokens_for(item.request) for item in pending)
    return JudgePlan(pending, passages, failed, blocked, stale_kept, estimated)


def _seen_rows(store: Store, rows: Sequence[Any]) -> dict[int, Any]:
    ids = sorted({i for row in rows for i in json.loads(row["seen_ids"] or "[]")})
    return {row["passage_id"]: row for row in store.get_passages(ids)}


class RateLimiter:
    """Spaces request starts at least 60 / requests_per_minute seconds apart."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 60.0 / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._next is not None and now < self._next:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


@dataclass
class RunReport:
    judged: int = 0
    failed: int = 0
    input_tokens: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkItem:
    """One request plus what to do with its outcome. Used for passages and ledger follow-ups alike."""

    label: str
    request: JudgeRequest
    on_result: Callable[[JudgeResult], None]
    on_error: Callable[[JudgeError], None]


async def run_work(
    judge: Judge,
    items: Sequence[WorkItem],
    *,
    concurrency: int,
    limiter: RateLimiter,
    progress: Callable[[RunReport], None] | None = None,
) -> RunReport:
    """Run requests concurrently; one request's failure never stops the others.

    A JudgeFatal (such as a rejected API key) stops starting new requests and is re-raised after
    the ones in flight finish. The caller has already entered `judge`.
    """
    report = RunReport()
    semaphore = asyncio.Semaphore(concurrency)
    fatal: list[JudgeFatal] = []

    async def one(item: WorkItem) -> None:
        async with semaphore:
            if fatal:
                return
            await limiter.wait()
            try:
                result = await judge.judge(item.request)
            except Exception as exc:
                error = classify_error(exc)
                if isinstance(error, JudgeFatal):
                    fatal.append(error)
                    return
                item.on_error(error)
                report.failed += 1
                report.errors.append(f"{item.label}: {error}")
                return
            item.on_result(result)
            report.judged += 1
            report.input_tokens += result.input_tokens or 0
            if progress is not None:
                progress(report)

    await asyncio.gather(*(one(item) for item in items))
    if fatal:
        raise fatal[0]
    return report


def passage_work(store: Store, item: PendingItem) -> WorkItem:
    def on_result(result: JudgeResult) -> None:
        store.save_judgment(
            JudgmentRecord(
                passage_id=item.passage_id, cache_key=item.cache_key, model=result.model,
                rubric_version=RUBRIC_VERSION, thesis_version=item.thesis_version, status="judged",
                answers=result.answers, input_tokens=result.input_tokens, request_id=result.request_id,
                ticker=item.ticker, part=item.part,
            )
        )

    def on_error(error: JudgeError) -> None:
        store.save_judgment(
            JudgmentRecord(
                passage_id=item.passage_id, cache_key=item.cache_key, model=item.request.model,
                rubric_version=RUBRIC_VERSION, thesis_version=item.thesis_version, status="failed",
                error=str(error), ticker=item.ticker, part=item.part, retryable=error.retryable,
            )
        )

    return WorkItem(f"{item.ticker} passage {item.passage_id} {item.part}", item.request, on_result, on_error)


async def run_judging(
    store: Store,
    judge: Judge,
    items: Sequence[PendingItem],
    *,
    concurrency: int,
    limiter: RateLimiter,
    progress: Callable[[RunReport], None] | None = None,
) -> RunReport:
    """Judge pending passage parts; each result is committed as it arrives."""
    return await run_work(
        judge, [passage_work(store, item) for item in items], concurrency=concurrency, limiter=limiter,
        progress=progress,
    )
