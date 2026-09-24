"""The guidance ledger: forward-looking promises paired with later reported results, judged by Jev.

A promise is a judged passage that is formal guidance (or management commentary that is clearly
forward-looking), on a pillar, material, and not boilerplate. Its result candidates are later
reported results on the same pillar about the same company, within RESULT_WINDOW_DAYS, ranked by
word overlap with the promise. Jev answers one Choice per (promise, result) pair: `confirms`,
`misses`, or `not_addressed` (rubric.followup_request). The ledger then calls a promise kept or
missed when some result's verdict is at least `ledger.min_probability` likely.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import PRICE_PER_MILLION_INPUT_TOKENS
from .judge import JudgeError, JudgeRequest, JudgeResult, estimate_tokens_for
from .models import FollowupRecord
from .policy import Policy, passage_day, signals
from .rubric import FOLLOWUP, OFF_THESIS, followup_request
from .runner import JudgePlan, WorkItem, cache_key
from .similar import topic_similarity
from .store import Store
from .thesis import Thesis

# A promise must be at least this forward-looking; management commentary (less formal than
# guidance) must be at least COMMENTARY_FORWARD_LOOKING_MIN.
FORWARD_LOOKING_MIN = 0.5
COMMENTARY_FORWARD_LOOKING_MIN = 0.7
PROMISE_EVIDENCE = "guidance"
COMMENTARY_EVIDENCE = "management_commentary"
RESULT_EVIDENCE = "reported_result"
# A result must be less forward-looking than this, dated after the promise and within this window.
RESULT_FORWARD_LOOKING_BELOW = 0.5
RESULT_WINDOW_DAYS = 450
# Results are ranked by content-word Jaccard with the promise; weaker overlap is not the same topic.
MIN_TOPIC_SIMILARITY = 0.1
RESULTS_PER_PROMISE = 3


@dataclass(frozen=True)
class FollowupItem:
    ticker: str
    promise_id: int
    result_id: int
    cache_key: str
    request: JudgeRequest


@dataclass
class FollowupPlan:
    pending: list[FollowupItem] = field(default_factory=list)
    estimated_tokens: int = 0

    @property
    def estimated_cost(self) -> float:
        return self.estimated_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000


def _day(passage: Mapping[str, Any]) -> str | None:
    return passage_day(passage.get("date"), passage.get("ingested_at"))


def _on_pillar(p: Mapping[str, Any], policy: Policy) -> bool:
    boilerplate = p.get("boilerplate")
    return (
        p.get("pillar") not in (None, OFF_THESIS)
        and float(p.get("pillar_p") or 0.0) >= policy.pillar_probability_min
        and float(1.0 if boilerplate is None else boilerplate) <= policy.boilerplate_max
    )


def is_promise(passage: Mapping[str, Any], policy: Policy) -> bool:
    p = passage.get("p")
    if not p or not _on_pillar(p, policy):
        return False
    forward = float(p.get("forward_looking") or 0.0)
    evidence = p.get("evidence")
    formal = evidence == PROMISE_EVIDENCE or (evidence == COMMENTARY_EVIDENCE and forward >= COMMENTARY_FORWARD_LOOKING_MIN)
    return (
        formal
        and forward >= FORWARD_LOOKING_MIN
        and float(p.get("materiality") or 0.0) >= policy.materiality_min
        and _day(passage) is not None
    )


def promise_ids(passages: Sequence[Mapping[str, Any]], policy: Policy) -> list[int]:
    """Ids of payload passages that count as forward-looking promises, in input order."""
    return [passage["id"] for passage in passages if is_promise(passage, policy)]


def result_candidates(
    promise: Mapping[str, Any], passages: Sequence[Mapping[str, Any]], policy: Policy
) -> list[Mapping[str, Any]]:
    """Up to RESULTS_PER_PROMISE later reported results that may confirm or miss `promise`, best first.

    A result must be about the same company as the promise (`read_through`), so a peer's results
    never settle the host company's guidance or the other way round.
    """
    p = promise["p"]
    start = _day(promise)
    if start is None:
        return []
    try:
        end = (date.fromisoformat(start[:10]) + timedelta(days=RESULT_WINDOW_DAYS)).isoformat()
    except ValueError:
        return []
    ranked = []
    for passage in passages:
        q = passage.get("p")
        if not q or passage["id"] == promise["id"]:
            continue
        if q.get("evidence") != RESULT_EVIDENCE or float(q.get("forward_looking") or 0.0) >= RESULT_FORWARD_LOOKING_BELOW:
            continue
        if q.get("pillar") != p.get("pillar") or not _on_pillar(q, policy):
            continue
        if passage.get("read_through") != promise.get("read_through"):
            continue
        day = _day(passage)
        if day is None or not start < day <= end:
            continue
        similarity = topic_similarity(promise["text"], passage["text"])
        if similarity < MIN_TOPIC_SIMILARITY:
            continue
        ranked.append((-similarity, day, passage["id"], passage))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:RESULTS_PER_PROMISE]]


def _judged_passages(store: Store, thesis: Thesis, plan: JudgePlan) -> list[dict[str, Any]]:
    """Payload-like dicts (with the row fields a follow-up request needs) for the thesis's judged passages."""
    ticker = thesis.ticker
    out = []
    for row in store.passages_for_tickers([ticker, *thesis.peers]):
        if row["repeat_of"] is not None:
            continue
        p = signals(plan.answers(ticker, row["passage_id"]))
        if p is None:
            continue
        out.append(
            {
                "id": row["passage_id"],
                "document_id": row["document_id"],
                "text": row["text"],
                "speaker": row["speaker"],
                "source_type": row["source_type"],
                "doc_date": row["doc_date"],
                "date": row["doc_date"],
                "ingested_at": row["ingested_at"],
                "read_through": row["ticker"] if row["ticker"] != ticker else None,
                "p": p,
            }
        )
    return out


def plan_followups(
    store: Store, theses: Mapping[str, Thesis], model: str, *, plan: JudgePlan, policy: Policy
) -> FollowupPlan:
    """Pending follow-up requests (promise, later result) not yet judged at their current cache key.

    A pair already judged at its key is skipped, as is one that failed permanently (retryable 0);
    one that failed with a retryable error is planned again.
    """
    pending: list[FollowupItem] = []
    for ticker, thesis in sorted(theses.items()):
        passages = _judged_passages(store, thesis, plan)
        for promise in passages:
            if not is_promise(promise, policy):
                continue
            for result in result_candidates(promise, passages, policy):
                request = followup_request(thesis, promise=promise, result=result, model=model)
                key = cache_key(request)
                existing = store.followup(promise["id"], result["id"], key)
                if existing is not None and (existing["status"] == "judged" or not existing["retryable"]):
                    continue
                pending.append(FollowupItem(ticker, promise["id"], result["id"], key, request))
    return FollowupPlan(pending, sum(estimate_tokens_for(item.request) for item in pending))


def followup_work(store: Store, item: FollowupItem) -> WorkItem:
    """A runner WorkItem that saves the follow-up outcome with store.save_followup."""

    def on_result(result: JudgeResult) -> None:
        store.save_followup(
            FollowupRecord(
                promise_id=item.promise_id, result_id=item.result_id, ticker=item.ticker, cache_key=item.cache_key,
                model=result.model, status="judged", answers=result.answers, input_tokens=result.input_tokens,
                request_id=result.request_id,
            )
        )

    def on_error(error: JudgeError) -> None:
        store.save_followup(
            FollowupRecord(
                promise_id=item.promise_id, result_id=item.result_id, ticker=item.ticker, cache_key=item.cache_key,
                model=item.request.model, status="failed", error=str(error), retryable=error.retryable,
            )
        )

    label = f"{item.ticker} follow-up {item.promise_id}→{item.result_id}"
    return WorkItem(label, item.request, on_result, on_error)


def _verdicts(answers_json: str | None) -> tuple[float, float] | None:
    """(P(confirms), P(misses)) from stored follow-up answers, or None when there is no follow-up answer."""
    if not answers_json:
        return None
    answer = json.loads(answers_json).get(FOLLOWUP)
    if not answer:
        return None
    probabilities = answer.get("probabilities") or {}
    return float(probabilities.get("confirms") or 0.0), float(probabilities.get("misses") or 0.0)


def ledger_summary(
    store: Store, thesis: Thesis, passages_by_id: Mapping[int, Mapping[str, Any]], policy: Policy
) -> dict[str, Any]:
    """Company.ledger: {items: [{promise_id, status, result_id, p}], kept, missed, open, credibility}.

    For each promise, the newest judged follow-up per result is read. Every result whose P(confirms)
    or P(misses) reaches `ledger.min_probability` is a verdict; the verdict from the latest-dated
    result decides (ties: the higher probability, then the newer passage). Only results present in
    `passages_by_id` count, so the page can always show the deciding passage. Items are ordered by
    promise date, newest first.
    """
    promises = promise_ids(list(passages_by_id.values()), policy)
    wanted = set(promises)
    latest: dict[tuple[int, int], Any] = {}
    for row in store.followups_for_ticker(thesis.ticker):
        if row["status"] == "judged" and row["promise_id"] in wanted:
            latest[(row["promise_id"], row["result_id"])] = row  # rows come oldest first

    verdicts: dict[int, list[tuple[str, int, float, str]]] = {pid: [] for pid in promises}
    for (promise_id, result_id), row in latest.items():
        result = passages_by_id.get(result_id)
        probabilities = _verdicts(row["answers_json"])
        if result is None or probabilities is None:
            continue
        day = _day(result) or ""
        confirms, misses = probabilities
        if confirms >= policy.ledger_min_probability:
            verdicts[promise_id].append((day, result_id, confirms, "kept"))
        if misses >= policy.ledger_min_probability:
            verdicts[promise_id].append((day, result_id, misses, "missed"))

    items = []
    for promise_id in promises:
        found = verdicts[promise_id]
        if found:
            day, result_id, probability, status = max(found, key=lambda v: (v[0], v[2], v[1]))
            items.append({"promise_id": promise_id, "status": status, "result_id": result_id, "p": round(probability, 3)})
        else:
            items.append({"promise_id": promise_id, "status": "open", "result_id": None, "p": None})
    items.sort(key=lambda item: (_day(passages_by_id[item["promise_id"]]) or "", item["promise_id"]), reverse=True)

    kept = sum(1 for item in items if item["status"] == "kept")
    missed = sum(1 for item in items if item["status"] == "missed")
    credibility = round(kept / (kept + missed), 3) if kept + missed else None
    return {"items": items, "kept": kept, "missed": missed, "open": len(items) - kept - missed, "credibility": credibility}
