"""Labeling, and a calibration report that selects thresholds on one half of the labels and reports on the other.

Labels are split into halves by document, not by passage: passages of one document are correlated, and letting
them straddle the halves would make the held-out numbers look better than they are.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Callable, Sequence
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


def signal(answers: dict[str, Any], question: str) -> float | None:
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
    flagged: int
    true_positives: int
    positives: int
    n: int


def metrics_at(pairs: Sequence[tuple[float, bool]], threshold: float) -> Metrics:
    true_positives = sum(1 for s, y in pairs if s >= threshold and y)
    flagged = sum(1 for s, _ in pairs if s >= threshold)
    positives = sum(1 for _, y in pairs if y)
    return Metrics(
        precision=true_positives / flagged if flagged else None,
        recall=true_positives / positives if positives else None,
        flagged=flagged,
        true_positives=true_positives,
        positives=positives,
        n=len(pairs),
    )


def select_threshold(pairs: Sequence[tuple[float, bool]], target_precision: float, grid: Sequence[float]) -> float | None:
    """The lowest threshold reaching the precision target: among those that do, it keeps the most recall."""
    for threshold in sorted(grid):
        m = metrics_at(pairs, threshold)
        if m.precision is not None and m.precision >= target_precision:
            return threshold
    return None


def select_threshold_for_recall(pairs: Sequence[tuple[float, bool]], target_recall: float, grid: Sequence[float]) -> float | None:
    """The highest threshold that still catches the recall target: the least reading that misses little."""
    best = None
    for threshold in sorted(grid):
        m = metrics_at(pairs, threshold)
        if m.recall is not None and m.recall >= target_recall:
            best = threshold
    return best


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def brier(pairs: Sequence[tuple[float, bool]]) -> float | None:
    if not pairs:
        return None
    return sum((s - (1.0 if y else 0.0)) ** 2 for s, y in pairs) / len(pairs)


def reliability(pairs: Sequence[tuple[float, bool]], buckets: int = 5) -> list[tuple[float, float, float, float, int]]:
    rows = []
    for index in range(buckets):
        low, high = index / buckets, (index + 1) / buckets
        inside = [(s, y) for s, y in pairs if low <= s < high or (index == buckets - 1 and s == 1.0)]
        if inside:
            predicted = sum(s for s, _ in inside) / len(inside)
            observed = sum(1 for _, y in inside if y) / len(inside)
            rows.append((low, high, predicted, observed, len(inside)))
    return rows


def expected_calibration_error(pairs: Sequence[tuple[float, bool]], buckets: int = 10) -> float | None:
    if not pairs:
        return None
    return sum(count / len(pairs) * abs(predicted - observed) for _, _, predicted, observed, count in reliability(pairs, buckets))


def confident_mistakes(pairs: Sequence[tuple[float, bool]], level: float = CONFIDENT) -> tuple[int, int]:
    floor = round(1 - level, 10)
    false_yes = sum(1 for s, y in pairs if s >= level and not y)
    false_no = sum(1 for s, y in pairs if s <= floor and y)
    return false_yes, false_no


def sample_for_labeling(candidates: Sequence[int], flagged: set[int], *, n: int, seed: int) -> list[int]:
    rng = random.Random(seed)
    hot = [pid for pid in candidates if pid in flagged]
    cold = [pid for pid in candidates if pid not in flagged]
    rng.shuffle(hot)
    rng.shuffle(cold)
    take_hot = min(len(hot), n // 2)
    picked = hot[:take_hot] + cold[: n - take_hot]
    if len(picked) < n:
        picked += hot[take_hot : take_hot + (n - len(picked))]
    rng.shuffle(picked)
    return picked


def run_labeling(
    store: Store, thesis: Thesis, rows: Sequence[Any], *, ask: Callable[[str], str], show: Callable[[str], None]
) -> int:
    questions = label_questions([a.id for a in thesis.assumptions])
    done = 0
    for row in rows:
        speaker = f"{row['speaker']}: " if row["speaker"] else ""
        show(f"\n[{row['passage_id']}] {row['source_type']} · {row['doc_date']} · {row['title']}\n{speaker}{row['text']}\n")
        for question in questions:
            answer = ""
            while answer not in {"y", "n", "s", "q"}:
                answer = ask(f"{question.prompt} [y/n, s=skip passage, q=quit] ").strip().lower()
            if answer == "q":
                return done
            if answer == "s":
                break
            store.save_label(row["passage_id"], question.name, answer == "y")
        else:
            done += 1
    return done


def _rate(numerator: int, denominator: int) -> str:
    interval = wilson(numerator, denominator)
    if interval is None:
        return "n/a"
    return f"{numerator / denominator:.2f} [{interval[0]:.2f}-{interval[1]:.2f}]"


def _describe(m: Metrics) -> str:
    return (
        f"precision {_rate(m.true_positives, m.flagged)}, recall {_rate(m.true_positives, m.positives)} "
        f"({m.flagged} flagged of {m.n}, {m.positives} yes)"
    )


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def calibration_report(
    store: Store,
    policy: Policy,
    *,
    target_precision: float = 0.8,
    target_recall: float = 0.9,
    ticker: str | None = None,
) -> str:
    pairs: dict[str, list[tuple[int, float, bool]]] = {}
    for row in store.labels():
        if ticker is not None and row["ticker"] != ticker:
            continue
        answers = store.latest_judged(row["passage_id"])
        if answers is None:
            continue
        value = signal(answers, row["question"])
        if value is None:
            continue
        pairs.setdefault(row["question"], []).append((row["document_id"], value, bool(row["value"])))
    if not pairs:
        return "No labels yet. Run `radar label` first."

    lines: list[str] = []
    for question in sorted(pairs):
        items = pairs[question]
        selection = [(s, y) for document, s, y in items if half(document) == 0]
        held_out = [(s, y) for document, s, y in items if half(document) == 1]
        positives = sum(1 for _, _, y in items if y)
        documents = len({document for document, _, _ in items})
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
            lines.append("  reliability (held-out half):")
            for low, high, predicted, observed, count in reliability(held_out):
                lines.append(f"    {low:.1f}-{high:.1f}  predicted {predicted:.2f}  observed {observed:.2f}  n={count}")
        lines.append("")
    lines.append("Labels are sampled half from flagged passages, so recall figures are rough.")
    return "\n".join(lines)
