"""Labeling, and a calibration report that selects thresholds on one half of the labels and reports on the other.

Labels are split into halves by document, not by passage: passages of one document are correlated, and letting
them straddle the halves would make the held-out numbers look better than they are.

Three kinds of labels feed the report:

- `sample` labels come from `radar label`, which draws half its sample from flagged passages. Each label stores
  an inverse-probability weight (pool size / sample size of its stratum), so precision, recall, Brier score, and
  calibration error estimate what they would be over all passages, not over the oversampled flagged ones.
- `triage` labels come from reading the feed (knew it / doesn't matter / absorb / star). They only cover flagged
  passages, so they measure the feed's precision, never recall.
- `spotcheck` labels are answers to random unflagged passages mixed into the feed. They estimate the miss rate.

Every label stores the judgment cache keys current when it was made, and is compared with exactly those answers,
so absorbing facts into the thesis later never makes old labels disagree with new judgments.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .policy import Policy
from .rubric import ASSUMPTION_PREFIX
from .store import Store
from .thesis import Thesis

CONTRADICTS_PREFIX = "contradicts__"
MIN_POSITIVES = 30
CONFIDENT = 0.9
_PROBABILITY_GRID = [round(i / 20, 2) for i in range(1, 20)]
_SCORE_GRID = [round(i / 10, 1) for i in range(0, 31)]

Pair = tuple[float, bool, float]  # (signal, label, weight)


@dataclass(frozen=True)
class LabelQuestion:
    name: str
    prompt: str


def label_questions(assumption_ids: Sequence[str]) -> list[LabelQuestion]:
    questions = [
        LabelQuestion("new_info", "Is this new to you (not already in your known facts)?"),
        LabelQuestion("material", "Does it matter for the investment case (would it shift an estimate or your confidence)?"),
    ]
    questions += [LabelQuestion(f"{CONTRADICTS_PREFIX}{aid}", f"Does it contradict assumption '{aid}'?") for aid in assumption_ids]
    return questions


def signal(answers: Mapping[str, Any], question: str) -> float | None:
    if question == "new_info":
        return float(answers["new_info"]["noul"]) if "new_info" in answers else None
    if question == "material":
        return float(answers["materiality"]["score"]) if "materiality" in answers else None
    if question.startswith(CONTRADICTS_PREFIX):
        answer = answers.get(ASSUMPTION_PREFIX + question[len(CONTRADICTS_PREFIX):])
        return None if answer is None else float(answer["probabilities"].get("contradicts", 0.0))
    return None


def current_threshold(question: str, policy: Policy) -> float:
    if question == "new_info":
        return policy.new_info_min
    if question == "material":
        return policy.materiality_min
    return policy.contradicts_min


def half(key: int) -> int:
    return int(hashlib.sha256(str(key).encode("utf-8")).hexdigest(), 16) % 2


@dataclass(frozen=True)
class Metrics:
    precision: float | None
    recall: float | None
    flagged: float
    true_positives: float
    positives: float
    n: int
    n_flagged_effective: float
    n_positive_effective: float


def _weighted(pairs: Sequence[Pair], keep: Callable[[float, bool], bool]) -> tuple[float, float]:
    """(Σw, Σw²) over pairs satisfying `keep`."""
    kept = [w for s, y, w in pairs if keep(s, y)]
    return sum(kept), sum(w * w for w in kept)


def _effective(total: float, squares: float) -> float:
    return total * total / squares if squares else 0.0


def metrics_at(pairs: Sequence[Pair], threshold: float) -> Metrics:
    tp, _ = _weighted(pairs, lambda s, y: s >= threshold and y)
    flagged, flagged_sq = _weighted(pairs, lambda s, y: s >= threshold)
    positives, positives_sq = _weighted(pairs, lambda s, y: y)
    return Metrics(
        precision=tp / flagged if flagged else None,
        recall=tp / positives if positives else None,
        flagged=flagged,
        true_positives=tp,
        positives=positives,
        n=len(pairs),
        n_flagged_effective=_effective(flagged, flagged_sq),
        n_positive_effective=_effective(positives, positives_sq),
    )


def select_threshold(pairs: Sequence[Pair], target_precision: float, grid: Sequence[float]) -> float | None:
    """The lowest threshold reaching the precision target: among those that do, it keeps the most recall."""
    for threshold in sorted(grid):
        m = metrics_at(pairs, threshold)
        if m.precision is not None and m.precision >= target_precision:
            return threshold
    return None


def select_threshold_for_recall(pairs: Sequence[Pair], target_recall: float, grid: Sequence[float]) -> float | None:
    """The highest threshold that still catches the recall target: the least reading that misses little."""
    best = None
    for threshold in sorted(grid):
        m = metrics_at(pairs, threshold)
        if m.recall is not None and m.recall >= target_recall:
            best = threshold
    return best


def wilson(successes: float, total: float, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson interval; `total` may be an effective (weighted) sample size."""
    if total <= 0:
        return None
    p = min(1.0, max(0.0, successes / total))
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def brier(pairs: Sequence[Pair]) -> float | None:
    total = sum(w for _, _, w in pairs)
    if not pairs or total <= 0:
        return None
    return sum(w * (s - (1.0 if y else 0.0)) ** 2 for s, y, w in pairs) / total


def reliability(pairs: Sequence[Pair], buckets: int = 5) -> list[tuple[float, float, float, float, int]]:
    rows = []
    for index in range(buckets):
        low, high = index / buckets, (index + 1) / buckets
        inside = [(s, y, w) for s, y, w in pairs if low <= s < high or (index == buckets - 1 and s == 1.0)]
        weight = sum(w for _, _, w in inside)
        if inside and weight > 0:
            predicted = sum(w * s for s, _, w in inside) / weight
            observed = sum(w for _, y, w in inside if y) / weight
            rows.append((low, high, predicted, observed, len(inside)))
    return rows


def expected_calibration_error(pairs: Sequence[Pair], buckets: int = 10) -> float | None:
    total = sum(w for _, _, w in pairs)
    if not pairs or total <= 0:
        return None
    error = 0.0
    for index in range(buckets):
        low, high = index / buckets, (index + 1) / buckets
        inside = [(s, y, w) for s, y, w in pairs if low <= s < high or (index == buckets - 1 and s == 1.0)]
        weight = sum(w for _, _, w in inside)
        if weight > 0:
            predicted = sum(w * s for s, _, w in inside) / weight
            observed = sum(w for _, y, w in inside if y) / weight
            error += weight / total * abs(predicted - observed)
    return error


def confident_mistakes(pairs: Sequence[Pair], level: float = CONFIDENT) -> tuple[int, int]:
    floor = round(1 - level, 10)
    false_yes = sum(1 for s, y, _ in pairs if s >= level and not y)
    false_no = sum(1 for s, y, _ in pairs if s <= floor and y)
    return false_yes, false_no


@dataclass(frozen=True)
class Sample:
    ids: list[int]
    weights: dict[int, float]


def sample_for_labeling(candidates: Sequence[int], flagged: set[int], *, n: int, seed: int) -> Sample:
    """Half from `flagged` where possible, the rest from the others, with inverse-probability weights."""
    rng = random.Random(seed)
    hot = [pid for pid in candidates if pid in flagged]
    cold = [pid for pid in candidates if pid not in flagged]
    rng.shuffle(hot)
    rng.shuffle(cold)
    take_hot = min(len(hot), n // 2)
    take_cold = min(len(cold), n - take_hot)
    take_hot = min(len(hot), n - take_cold)
    picked_hot, picked_cold = hot[:take_hot], cold[:take_cold]
    weights = {pid: len(hot) / len(picked_hot) for pid in picked_hot}
    weights.update({pid: len(cold) / len(picked_cold) for pid in picked_cold})
    picked = picked_hot + picked_cold
    rng.shuffle(picked)
    return Sample(picked, weights)


def run_labeling(
    store: Store,
    thesis: Thesis,
    rows: Sequence[Any],
    *,
    ask: Callable[[str], str],
    show: Callable[[str], None],
    weights: Mapping[int, float] | None = None,
    keys: Mapping[int, Sequence[str]] | None = None,
) -> int:
    questions = label_questions([a.id for a in thesis.assumptions])
    done = 0
    for row in rows:
        pid = row["passage_id"]
        speaker = f"{row['speaker']}: " if row["speaker"] else ""
        show(f"\n[{pid}] {row['source_type']} · {row['doc_date']} · {row['title']}\n{speaker}{row['text']}\n")
        for question in questions:
            answer = ""
            while answer not in {"y", "n", "s", "q"}:
                answer = ask(f"{question.prompt} [y/n, s=skip passage, q=quit] ").strip().lower()
            if answer == "q":
                return done
            if answer == "s":
                break
            store.save_label(
                pid, question.name, answer == "y", ticker=thesis.ticker, origin="sample",
                weight=(weights or {}).get(pid, 1.0), judgment_keys=(keys or {}).get(pid),
            )
        else:
            done += 1
    return done


def _rate(numerator: float, denominator: float, effective: float) -> str:
    if denominator <= 0:
        return "n/a"
    value = numerator / denominator
    interval = wilson(value * effective, effective)
    if interval is None:
        return f"{value:.2f}"
    return f"{value:.2f} [{interval[0]:.2f}-{interval[1]:.2f}]"


def _describe(m: Metrics) -> str:
    return (
        f"precision {_rate(m.true_positives, m.flagged, m.n_flagged_effective)}, "
        f"recall {_rate(m.true_positives, m.positives, m.n_positive_effective)} "
        f"({m.n} labels, weighted {m.flagged:.1f} flagged, {m.positives:.1f} yes)"
    )


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _label_answers(store: Store, row: Any) -> dict[str, Any] | None:
    keys = json.loads(row["judgment_keys"]) if row["judgment_keys"] else None
    if keys:
        answers = store.answers_for_keys(row["passage_id"], keys)
        if answers is not None:
            return answers
        return None  # the judgment the label was made against is gone; don't compare with a different one
    return store.latest_judged(row["passage_id"], row["ticker"])


def calibration_report(
    store: Store,
    policy: Policy,
    *,
    target_precision: float = 0.8,
    target_recall: float = 0.9,
    ticker: str | None = None,
) -> str:
    rows = [row for row in store.labels(ticker)]
    if not rows:
        return "No labels yet. Run `radar label`, or triage the dashboard feed, first."
    lines: list[str] = []
    lines += _sample_section(store, policy, [r for r in rows if r["origin"] == "sample"], target_precision, target_recall)
    lines += _feed_section([r for r in rows if r["origin"] != "sample"])
    return "\n".join(lines).rstrip() + "\n"


def _sample_section(
    store: Store, policy: Policy, rows: Sequence[Any], target_precision: float, target_recall: float
) -> list[str]:
    if not rows:
        return ["No sampled labels yet: run `radar label` for thresholds and probability error.", ""]
    pairs: dict[str, list[tuple[int, float, bool, float]]] = {}
    missing = 0
    for row in rows:
        answers = _label_answers(store, row)
        if answers is None:
            missing += 1
            continue
        value = signal(answers, row["question"])
        if value is None:
            continue
        pairs.setdefault(row["question"], []).append((row["document_id"], value, bool(row["value"]), float(row["weight"])))
    lines = ["Sampled labels (weighted to stand for all passages):", ""]
    if missing:
        lines += [f"{missing} labels skipped: the judgments they were made against no longer exist", ""]
    for question in sorted(pairs):
        items = pairs[question]
        selection = [(s, y, w) for document, s, y, w in items if half(document) == 0]
        held_out = [(s, y, w) for document, s, y, w in items if half(document) == 1]
        positives = sum(1 for _, _, y, _ in items if y)
        documents = len({document for document, _, _, _ in items})
        lines.append(f"{question}: {len(items)} labels ({positives} yes) from {documents} documents")
        if positives < MIN_POSITIVES:
            lines.append(f"  warning: only {positives} yes labels; one miss moves recall sharply, so treat these numbers as rough")
        if not selection or not held_out:
            lines.append("  warning: all labels come from one half of the documents; label passages from more documents")
        current = current_threshold(question, policy)
        lines.append(f"  current threshold {current:.2f} on held-out half: {_describe(metrics_at(held_out, current))}")
        grid = _SCORE_GRID if question == "material" else _PROBABILITY_GRID
        by_precision = select_threshold(selection, target_precision, grid)
        if by_precision is None:
            lines.append(f"  no threshold reaches {target_precision:.0%} precision on the selection half")
        else:
            lines.append(f"  for {target_precision:.0%} precision: threshold {by_precision:.2f} (lowest reaching it on the selection half)")
            lines.append(f"    on held-out half: {_describe(metrics_at(held_out, by_precision))}")
        by_recall = select_threshold_for_recall(selection, target_recall, grid)
        if by_recall is None:
            lines.append(f"  no threshold catches {target_recall:.0%} of yes labels on the selection half")
        else:
            lines.append(f"  to catch {target_recall:.0%}: threshold {by_recall:.2f} (highest reaching it on the selection half)")
            lines.append(f"    on held-out half: {_describe(metrics_at(held_out, by_recall))}")
        if question.startswith(CONTRADICTS_PREFIX):
            lines.append("  missing a contradiction costs more than reading an extra passage: prefer the recall threshold")
        if question == "material":
            lines.append("  materiality is a 0-3 score, so there is no probability error or reliability table")
        else:
            false_yes, false_no = confident_mistakes(held_out)
            lines.append(
                f"  probability error on held-out half: Brier {_number(brier(held_out))}, "
                f"calibration error {_number(expected_calibration_error(held_out))}"
            )
            lines.append(
                f"  confidently wrong on held-out half: {false_yes} said >= {CONFIDENT:.2f} but the label was no, "
                f"{false_no} said <= {1 - CONFIDENT:.2f} but the label was yes"
            )
            lines.append("  reliability (held-out half, weighted):")
            for low, high, predicted, observed, count in reliability(held_out):
                lines.append(f"    {low:.1f}-{high:.1f}  predicted {predicted:.2f}  observed {observed:.2f}  n={count}")
        lines.append("")
    return lines


def _feed_section(rows: Sequence[Any]) -> list[str]:
    triage = [r for r in rows if r["origin"] == "triage" and r["question"] == "whats_new"]
    spot = [r for r in rows if r["origin"] == "spotcheck" and r["question"] == "whats_new"]
    if not triage and not spot:
        return []
    lines = ["From reading the feed:", ""]
    if triage:
        kept = sum(1 for r in triage if r["value"])
        lines.append(
            f"  feed precision from triage: {_rate(kept, len(triage), len(triage))} "
            f"({kept} of {len(triage)} feed items were worth it)"
        )
    if spot:
        missed = sum(1 for r in spot if r["value"])
        lines.append(
            f"  miss rate from spot checks: {_rate(missed, len(spot), len(spot))} "
            f"({missed} of {len(spot)} random unflagged passages should have been in the feed)"
        )
        if len(spot) < MIN_POSITIVES:
            lines.append(f"  warning: only {len(spot)} spot checks; answer more before trusting the miss rate")
    lines.append("")
    return lines
