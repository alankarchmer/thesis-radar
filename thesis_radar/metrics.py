"""The KPI tracker: numbers from passages, placed on the metrics a thesis depends on.

Code finds every number and period in a passage (numbers.py); Jev chooses which metric each number
measures, whether it is a reported result, company guidance, or an outside estimate, and which period
it covers (rubric.metric_parts). This module plans those requests and builds, per metric, a series of
periods with the reported figure, every guidance revision, and every estimate — each point carrying
the verbatim sentence and a link to its source. Comparisons (beat or miss against guidance and
estimates, guidance raised or cut, change from the prior period) are arithmetic on those points.

Only the company's own documents are read here, not peers': a peer's number measures the peer.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import PRICE_PER_MILLION_INPUT_TOKENS
from .judge import JudgeError, JudgeRequest, JudgeResult, estimate_tokens_for
from .models import MetricRecord
from .numbers import (
    Mention,
    PeriodCandidate,
    find_mentions,
    find_periods,
    last_completed_quarter,
    period_granularity,
    period_sort_key,
    to_unit,
    unit_kinds,
)
from .policy import Policy, signals
from .rubric import NONE, metric_parts
from .runner import JudgePlan, WorkItem, cache_key
from .store import Store
from .thesis import Metric, Thesis

# Which source wins when several report the same period: filings, then the call, then everything else.
SOURCE_RANK = {"filing": 0, "earnings_transcript": 1}
# "In line with estimates" tolerance: 0.1 points for percentages, else 0.5% of the estimate.
PERCENT_UNITS = frozenset({"%", "percent", "pct", "pp", "pts", "points"})
IN_LINE_POINTS = 0.1
IN_LINE_RELATIVE = 0.005


@dataclass(frozen=True)
class MetricItem:
    ticker: str
    passage_id: int
    part: str
    cache_key: str
    request: JudgeRequest


@dataclass
class MetricPlan:
    pending: list[MetricItem] = field(default_factory=list)
    estimated_tokens: int = 0
    passages: int = 0

    @property
    def estimated_cost(self) -> float:
        return self.estimated_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000


def _doc_day(row: Any) -> date:
    return date.fromisoformat((row["doc_date"] or row["ingested_at"][:10])[:10])


def compatible_metrics(thesis: Thesis, mentions: Sequence[Mention]) -> dict[str, list[str]]:
    """For each mention, the metrics whose unit it could measure."""
    kinds = {metric.id: unit_kinds(metric.unit) for metric in thesis.metrics}
    return {m.id: [metric_id for metric_id, allowed in kinds.items() if m.kind in allowed] for m in mentions}


def passage_requests(thesis: Thesis, row: Any, *, model: str) -> list[tuple[str, JudgeRequest]]:
    mentions = find_mentions(row["text"])
    if not mentions:
        return []
    periods = find_periods(row["text"], _doc_day(row))
    return metric_parts(
        thesis, passage_text=row["text"], speaker=row["speaker"], source_type=row["source_type"],
        doc_date=row["doc_date"], title=row["title"], mentions=mentions, periods=periods,
        compatible=compatible_metrics(thesis, mentions), model=model,
    )


def _worth_reading(answers: Mapping[str, Any] | None, policy: Policy) -> bool:
    """Judged, and not boilerplate (risk factors and accounting-policy text are full of numbers that measure nothing)."""
    p = signals(answers)
    return p is not None and p["boilerplate"] <= policy.contradiction_boilerplate_max


def plan_metrics(
    store: Store, theses: Mapping[str, Thesis], model: str, *, plan: JudgePlan, policy: Policy
) -> MetricPlan:
    """Metric requests not yet judged at their current cache key, for judged, non-boilerplate passages."""
    result = MetricPlan()
    for ticker, thesis in sorted(theses.items()):
        if not thesis.metrics:
            continue
        existing = {(row["passage_id"], row["cache_key"]): row for row in store.metric_judgments_for_ticker(ticker)}
        for row in store.passages_for_ticker(ticker):
            if row["repeat_of"] is not None or not _worth_reading(plan.answers(ticker, row["passage_id"]), policy):
                continue
            parts = passage_requests(thesis, row, model=model)
            if parts:
                result.passages += 1
            for part, request in parts:
                key = cache_key(request)
                done = existing.get((row["passage_id"], key))
                if done is not None and (done["status"] == "judged" or not done["retryable"]):
                    continue
                result.pending.append(MetricItem(ticker, row["passage_id"], part, key, request))
                result.estimated_tokens += estimate_tokens_for(request)
    return result


def metric_work(store: Store, item: MetricItem) -> WorkItem:
    def on_result(result: JudgeResult) -> None:
        store.save_metric_judgment(
            MetricRecord(
                passage_id=item.passage_id, cache_key=item.cache_key, ticker=item.ticker, part=item.part,
                model=result.model, status="judged", answers=result.answers, input_tokens=result.input_tokens,
                request_id=result.request_id,
            )
        )

    def on_error(error: JudgeError) -> None:
        store.save_metric_judgment(
            MetricRecord(
                passage_id=item.passage_id, cache_key=item.cache_key, ticker=item.ticker, part=item.part,
                model=item.request.model, status="failed", error=str(error), retryable=error.retryable,
            )
        )

    return WorkItem(f"{item.ticker} numbers in passage {item.passage_id} {item.part}", item.request, on_result, on_error)


# Series


def _mid(point: Mapping[str, Any]) -> float:
    return point["value"] if point["high"] is None else (point["value"] + point["high"]) / 2


def _probability(answer: Mapping[str, Any]) -> float:
    return float(answer.get("probabilities", {}).get(answer["choice"], 0.0))


def extract_points(
    store: Store, thesis: Thesis, policy: Policy, *, link: Callable[[str, int | None], str]
) -> dict[str, list[dict[str, Any]]]:
    """Points per metric id from the newest judged answers of each passage."""
    metrics = {metric.id: metric for metric in thesis.metrics}
    answers_by_passage: dict[int, dict[str, Any]] = {}
    by_part: dict[tuple[int, str], dict[str, Any]] = {}
    for judgment in store.metric_judgments_for_ticker(thesis.ticker):
        if judgment["status"] == "judged":
            by_part[(judgment["passage_id"], judgment["part"])] = json.loads(judgment["answers_json"])
    for (pid, _), answers in sorted(by_part.items()):
        answers_by_passage.setdefault(pid, {}).update(answers)
    points: dict[str, list[dict[str, Any]]] = {metric_id: [] for metric_id in metrics}
    rows = {row["passage_id"]: row for row in store.get_passages(sorted(answers_by_passage))}
    for pid, answers in sorted(answers_by_passage.items()):
        row = rows.get(pid)
        if row is None or row["ticker"] != thesis.ticker:
            continue
        doc_day = _doc_day(row)
        periods = {p.key: p for p in find_periods(row["text"], doc_day)}
        for mention in find_mentions(row["text"]):
            metric_answer = answers.get(f"{mention.id}__metric")
            kind_answer = answers.get(f"{mention.id}__kind")
            if metric_answer is None or kind_answer is None:
                continue
            metric = metrics.get(metric_answer["choice"])
            if metric is None or metric_answer["choice"] == NONE or mention.kind not in unit_kinds(metric.unit):
                continue
            p = _probability(metric_answer)
            kind = kind_answer["choice"]
            if p < policy.metric_min_probability or kind not in ("reported", "guidance", "estimate"):
                continue
            period = _period(answers.get(f"{mention.id}__period"), periods, kind, doc_day)
            if period is None:
                continue
            value, high = to_unit(mention, metric.unit)
            points[metric.id].append({
                "period": period.key,
                "period_label": period.label,
                "value": value,
                "high": high,
                "unit": metric.unit,
                "text": mention.text,
                "quote": mention.sentence,
                "kind": kind,
                "passage_id": pid,
                "document_id": row["document_id"],
                "date": row["doc_date"],
                "source_type": row["source_type"],
                "title": row["title"],
                "link": link(row["path"], row["page"]),
                "speaker": row["speaker"],
                "p": round(p, 3),
            })
    return points


def _period(
    answer: Mapping[str, Any] | None, periods: Mapping[str, PeriodCandidate], kind: str, doc_day: date
) -> PeriodCandidate | None:
    if answer is not None and answer["choice"] in periods:
        return periods[answer["choice"]]
    if kind == "reported":
        # A reported figure with no period named covers the quarter that just ended.
        return last_completed_quarter(doc_day)
    return None


def _compare_range(value: float, low: float, high: float) -> str:
    if value > high:
        return "above"
    if value < low:
        return "below"
    return "within"


def _in_line_tolerance(unit: str, reference: float) -> float:
    return IN_LINE_POINTS if unit.strip().lower() in PERCENT_UNITS else abs(reference) * IN_LINE_RELATIVE


def _public(point: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in point.items() if k not in ("period", "period_label")}


def _best_reported(points: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Filings beat calls beat everything else; then the likelier match; then the newest."""
    ranked = sorted(points, key=lambda p: (p["date"] or "", p["passage_id"]), reverse=True)
    ranked.sort(key=lambda p: (SOURCE_RANK.get(p["source_type"] or "", 2), -p["p"]))
    return ranked[0] if ranked else None


def build_series(metric: Metric, points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The payload Metric (spec: Company.metrics) for one metric's points."""
    by_period: dict[str, list[Mapping[str, Any]]] = {}
    labels: dict[str, str] = {}
    for point in points:
        by_period.setdefault(point["period"], []).append(point)
        labels[point["period"]] = point["period_label"]
    periods = []
    for key in sorted(by_period, key=period_sort_key):
        items = by_period[key]
        reported = _best_reported([p for p in items if p["kind"] == "reported"])
        by_date = lambda p: (p["date"] or "", p["passage_id"])  # noqa: E731
        guidance = sorted((p for p in items if p["kind"] == "guidance"), key=by_date)
        estimates = sorted((p for p in items if p["kind"] == "estimate"), key=by_date)
        vs_guidance = vs_estimates = None
        if reported is not None:
            before = [g for g in guidance if (g["date"] or "") <= (reported["date"] or "")]
            if before:
                latest = before[-1]
                vs_guidance = _compare_range(_mid(reported), latest["value"], latest["high"] or latest["value"])
            earlier = [e for e in estimates if (e["date"] or "") <= (reported["date"] or "")]
            if earlier:
                mean = sum(_mid(e) for e in earlier) / len(earlier)
                gap = _mid(reported) - mean
                tolerance = _in_line_tolerance(metric.unit, mean)
                vs_estimates = "in_line" if abs(gap) <= tolerance else ("above" if gap > 0 else "below")
        guidance_change = None
        if len(guidance) >= 2:
            last, previous = guidance[-1], guidance[-2]
            delta = _mid(last) - _mid(previous)
            width_last = (last["high"] or last["value"]) - last["value"]
            width_previous = (previous["high"] or previous["value"]) - previous["value"]
            if abs(delta) > 1e-9:
                guidance_change = "raised" if delta > 0 else "lowered"
            elif width_last < width_previous - 1e-9:
                guidance_change = "narrowed"
            elif width_last > width_previous + 1e-9:
                guidance_change = "widened"
            else:
                guidance_change = "maintained"
        periods.append({
            "period": key,
            "label": labels[key],
            "reported": None if reported is None else _public(reported),
            "guidance": [_public(g) for g in guidance],
            "estimates": [_public(e) for e in estimates],
            "vs_guidance": vs_guidance,
            "vs_estimates": vs_estimates,
            "guidance_change": guidance_change,
        })
    return {
        "id": metric.id,
        "label": metric.label,
        "unit": metric.unit,
        "pillar": metric.pillar,
        "higher_is": metric.higher_is,
        "periods": periods,
        "latest": _latest(metric, periods),
    }


def _latest(metric: Metric, periods: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    reported = [p for p in periods if p["reported"] is not None]
    if not reported:
        return None
    last = reported[-1]
    grain = period_granularity(last["period"])
    previous = [p for p in reported[:-1] if period_granularity(p["period"]) == grain]
    value = _mid(last["reported"])
    change = direction = None
    if previous:
        change = round(value - _mid(previous[-1]["reported"]), 6)
        if abs(change) < 1e-9:
            direction = "flat"
        elif metric.higher_is == "neutral":
            direction = None
        else:
            direction = "good" if (change > 0) == (metric.higher_is == "good") else "bad"
    return {"period": last["period"], "label": last["label"], "value": value, "change": change, "direction": direction}


def metric_series(
    store: Store, thesis: Thesis, policy: Policy, *, link: Callable[[str, int | None], str]
) -> list[dict[str, Any]]:
    """Company.metrics for the payload: one series per metric in thesis order."""
    if not thesis.metrics:
        return []
    points = extract_points(store, thesis, policy, link=link)
    return [build_series(metric, points[metric.id]) for metric in thesis.metrics]


# Text output for `radar metrics`.


def format_value(value: float, high: float | None, unit: str) -> str:
    def one(x: float) -> str:
        text = f"{x:,.2f}".rstrip("0").rstrip(".")
        u = unit.strip()
        if u.startswith("$"):
            suffix = u[1:]
            return f"${text}{suffix}" if suffix else f"${text}"
        if u in {"%", "x"}:
            return f"{text}{u}"
        return f"{text} {u}"

    return one(value) if high is None else f"{one(value)}–{one(high)}"


def metrics_text(series: Sequence[Mapping[str, Any]], *, ticker: str) -> str:
    if not series:
        return f"{ticker}: no metrics in thesis/{ticker}.yaml (add a `metrics:` section; see README)"
    lines = []
    for metric in series:
        lines.append(f"{ticker} · {metric['label']} ({metric['unit']})")
        if not metric["periods"]:
            lines.append("  no numbers found yet")
        for period in metric["periods"]:
            parts = []
            if period["reported"]:
                r = period["reported"]
                parts.append(f"reported {format_value(r['value'], r['high'], metric['unit'])}")
            if period["guidance"]:
                history = " → ".join(format_value(g["value"], g["high"], metric["unit"]) for g in period["guidance"])
                parts.append(f"guidance {history}" + (f" ({period['guidance_change']})" if period["guidance_change"] else ""))
            if period["estimates"]:
                estimates = ", ".join(format_value(e["value"], e["high"], metric["unit"]) for e in period["estimates"])
                parts.append(f"estimates {estimates}")
            verdicts = [v for v in (
                f"{period['vs_guidance']} guidance" if period["vs_guidance"] else None,
                {"above": "beat estimates", "below": "missed estimates", "in_line": "in line with estimates"}.get(
                    period["vs_estimates"] or ""),
            ) if v]
            lines.append(f"  {period['label']:<9} " + " · ".join(parts) + (f"  [{'; '.join(verdicts)}]" if verdicts else ""))
        latest = metric["latest"]
        if latest and latest["change"] is not None:
            sign = "up" if latest["change"] > 0 else "down" if latest["change"] < 0 else "flat"
            amount = format_value(abs(latest["change"]), None, "pts" if metric["unit"].strip() == "%" else metric["unit"])
            lines.append(f"  latest: {latest['label']} {sign} {amount}" + (f" ({latest['direction']})" if latest["direction"] else ""))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
