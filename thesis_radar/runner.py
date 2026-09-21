"""Plan and run passage judging: cache keys, cost estimates, rate limiting, incremental commits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import PRICE_PER_MILLION_INPUT_TOKENS
from .judge import Judge, JudgeError, JudgeRequest, estimate_tokens_for
from .models import JudgmentRecord
from .rubric import RUBRIC_VERSION, passage_questions, passage_state
from .store import Store
from .thesis import Thesis


def cache_key(request: JudgeRequest) -> str:
    blob = json.dumps(request.payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingItem:
    passage_id: int
    ticker: str
    thesis_version: str
    cache_key: str
    request: JudgeRequest


@dataclass(frozen=True)
class JudgePlan:
    pending: list[PendingItem]
    judged: dict[int, dict[str, Any]]
    failed: dict[int, str]
    estimated_tokens: int

    @property
    def estimated_cost(self) -> float:
        return self.estimated_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000


def plan_judging(store: Store, theses: Mapping[str, Thesis], model: str) -> JudgePlan:
    pending: list[PendingItem] = []
    judged: dict[int, dict[str, Any]] = {}
    failed: dict[int, str] = {}
    for ticker, thesis in sorted(theses.items()):
        questions = passage_questions(thesis)
        version = thesis.version
        for row in store.passages_for_ticker(ticker):
            state = passage_state(
                thesis, passage_text=row["text"], speaker=row["speaker"], source_type=row["source_type"],
                doc_date=row["doc_date"], title=row["title"],
            )
            request = JudgeRequest(state=state, questions=questions, model=model)
            key = cache_key(request)
            existing = store.judgment(row["passage_id"], key)
            if existing is not None and existing["status"] == "judged":
                judged[row["passage_id"]] = json.loads(existing["answers_json"])
                continue
            if existing is not None:
                failed[row["passage_id"]] = existing["error"] or "failed"
            pending.append(PendingItem(row["passage_id"], ticker, version, key, request))
    estimated = sum(estimate_tokens_for(item.request) for item in pending)
    return JudgePlan(pending=pending, judged=judged, failed=failed, estimated_tokens=estimated)


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


async def run_judging(
    store: Store, judge: Judge, items: Sequence[PendingItem], *, concurrency: int, limiter: RateLimiter
) -> RunReport:
    report = RunReport()
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item: PendingItem) -> None:
        async with semaphore:
            await limiter.wait()
            try:
                result = await judge.judge(item.request)
            except JudgeError as exc:
                store.save_judgment(
                    JudgmentRecord(
                        passage_id=item.passage_id, cache_key=item.cache_key, model=item.request.model,
                        rubric_version=RUBRIC_VERSION, thesis_version=item.thesis_version,
                        status="failed", error=str(exc),
                    )
                )
                report.failed += 1
                report.errors.append(f"passage {item.passage_id}: {exc}")
                return
            store.save_judgment(
                JudgmentRecord(
                    passage_id=item.passage_id, cache_key=item.cache_key, model=result.model,
                    rubric_version=RUBRIC_VERSION, thesis_version=item.thesis_version,
                    status="judged", answers=result.answers, input_tokens=result.input_tokens,
                    request_id=result.request_id,
                )
            )
            report.judged += 1
            report.input_tokens += result.input_tokens or 0

    await asyncio.gather(*(one(item) for item in items))
    return report
