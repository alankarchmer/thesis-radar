"""View-time analysis over judged passages: heat map, assumption balance, divergence, redlines, predictions.

Every function takes payload-shaped passages (spec v1.1 section 6, `Passage`) unless it says otherwise,
and returns the payload shapes of section 6. Passages whose `p` is None (unjudged) are skipped. A
passage's day is its document date, else the date it was ingested (`policy.passage_day`).

Nothing here calls Jev: every number is arithmetic over stored probabilities, and redlines are
deterministic text comparison (`similar`).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from .policy import Policy, passage_day, recent
from .similar import NEAR_DUPLICATE, NEAR_PREFILTER, content_words, normalize, sequence_similarity, word_diff
from .store import Store
from .thesis import Thesis

# Heat map: a passage counts toward its pillar's week when the pillar answer is at least this likely
# and it is less likely than not to be boilerplate. Looser than the feed thresholds on purpose: the
# heat map shows the drift of everything said about a pillar, not only what is new.
HEATMAP_PILLAR_MIN = 0.5
HEATMAP_BOILERPLATE_BELOW = 0.5
# Weight of a passage in weighted means: materiality (0-3) plus this floor, so immaterial passages
# still count a little and a week of only immaterial passages still has a value.
WEIGHT_FLOOR = 0.1
NEUTRAL_STANCE = 2.0  # stance is a 0-4 score; 2 is neutral

# Assumption balance: how much a passage of each evidence type moves the running balance.
EVIDENCE_WEIGHTS: dict[str, float] = {
    "reported_result": 1.0,
    "guidance": 0.8,
    "channel_or_customer_data": 0.8,
    "management_commentary": 0.6,
    "expert_opinion": 0.6,
    "analyst_opinion": 0.5,
    "speculation": 0.2,
}
DEFAULT_EVIDENCE_WEIGHT = 0.5  # unknown or missing evidence type
# A passage moves the balance only when Jev leans at least this much toward supports or contradicts.
BALANCE_MIN_PROBABILITY = 0.5
MAX_MATERIALITY = 3.0

# Divergence: company voices against outside voices.
INSIDE_EVIDENCE = frozenset({"guidance", "management_commentary"})
OUTSIDE_EVIDENCE = frozenset({"channel_or_customer_data", "expert_opinion"})
DIVERGENCE_IDS = 3  # example passage ids per side

# Redlines: form families compared latest against previous, in this order.
FORM_FAMILIES: dict[str, frozenset[str]] = {
    "10-K": frozenset({"10-K", "10-K/A", "20-F", "40-F"}),
    "10-Q": frozenset({"10-Q", "10-Q/A"}),
}
REDLINE_MAX_ITEMS = 80

# Predictions within this many days of their date are "due".
DUE_SOON_DAYS = 14


def _day(passage: Mapping[str, Any]) -> str | None:
    return passage_day(passage.get("date"), passage.get("ingested_at"))


def _parse_day(day: str | None) -> date | None:
    if not day:
        return None
    try:
        return date.fromisoformat(day[:10])
    except ValueError:
        return None


def _weight(p: Mapping[str, Any]) -> float:
    return float(p.get("materiality") or 0.0) + WEIGHT_FLOOR


def _stance(p: Mapping[str, Any]) -> float:
    stance = p.get("stance")
    return NEUTRAL_STANCE if stance is None else float(stance)


def _boilerplate(p: Mapping[str, Any]) -> float:
    # A missing boilerplate answer counts as boilerplate, as in policy.classify.
    value = p.get("boilerplate")
    return 1.0 if value is None else float(value)


def iso_week(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_labels(today: date, weeks: int) -> list[str]:
    """The last `weeks` ISO weeks ending with today's, oldest first."""
    monday = today - timedelta(days=today.weekday())
    return [iso_week(monday - timedelta(weeks=back)) for back in range(max(0, weeks) - 1, -1, -1)]


def heatmap(passages: Sequence[Mapping[str, Any]], pillars: Sequence[str], *, today: date, weeks: int = 26) -> dict[str, Any]:
    """Company.heatmap: {weeks: [...], rows: {pillar: [{net, count} | None, ...]}}.

    `net` is the materiality-weighted mean of (stance - 2) / 2, so -1 is clearly negative and +1
    clearly positive.
    """
    labels = week_labels(today, weeks)
    position = {label: index for index, label in enumerate(labels)}
    wanted = set(pillars)
    sums: dict[tuple[str, int], list[float]] = {}
    for passage in passages:
        p = passage.get("p")
        if not p or p.get("pillar") not in wanted:
            continue
        if float(p.get("pillar_p") or 0.0) < HEATMAP_PILLAR_MIN or _boilerplate(p) >= HEATMAP_BOILERPLATE_BELOW:
            continue
        day = _parse_day(_day(passage))
        if day is None:
            continue
        index = position.get(iso_week(day))
        if index is None:
            continue
        weight = _weight(p)
        cell = sums.setdefault((p["pillar"], index), [0.0, 0.0, 0])
        cell[0] += weight * (_stance(p) - NEUTRAL_STANCE) / NEUTRAL_STANCE
        cell[1] += weight
        cell[2] += 1
    rows: dict[str, list[dict[str, Any] | None]] = {}
    for pillar in pillars:
        row: list[dict[str, Any] | None] = []
        for index in range(len(labels)):
            cell = sums.get((pillar, index))
            if cell is None:
                row.append(None)
            else:
                net = max(-1.0, min(1.0, cell[0] / cell[1]))
                row.append({"net": round(net, 3) + 0.0, "count": int(cell[2])})  # + 0.0 turns -0.0 into 0.0
        rows[pillar] = row
    return {"weeks": labels, "rows": rows}


def evidence_weight(evidence: str | None) -> float:
    return EVIDENCE_WEIGHTS.get(evidence or "", DEFAULT_EVIDENCE_WEIGHT)


def _next_month(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    return f"{year + 1}-01" if number == 12 else f"{year}-{number + 1:02d}"


def assumption_series(
    passages: Sequence[Mapping[str, Any]], assumption_ids: Sequence[str], policy: Policy
) -> dict[str, list[dict[str, Any]]]:
    """Company.assumption_series: {assumption_id: [{month, for, against, balance}, ...]}.

    `for` and `against` count passages that `policy.classify` would mark as supporting or
    contradicting the assumption. `balance` is a running total of evidence weight x (P(supports) -
    P(contradicts)) x materiality / 3 over passages that lean one way (max >= 0.5). A month has
    evidence when a passage in it counts for, against, or toward the balance; the series runs from
    the first such month to the last, with empty months in between carrying the balance.
    """
    series: dict[str, list[dict[str, Any]]] = {}
    for assumption_id in assumption_ids:
        months: dict[str, list[float]] = {}  # month -> [for, against, balance delta]
        for passage in passages:
            p = passage.get("p")
            if not p:
                continue
            probabilities = (p.get("assumptions") or {}).get(assumption_id)
            if not probabilities:
                continue  # stale judgment made before the assumption existed: no evidence
            day = _day(passage)
            if not day or len(day) < 7:
                continue
            supports = float(probabilities.get("supports") or 0.0)
            contradicts = float(probabilities.get("contradicts") or 0.0)
            against = contradicts >= policy.contradicts_min
            in_favor = supports >= policy.contradicts_min and not against
            leans = max(supports, contradicts) >= BALANCE_MIN_PROBABILITY
            if not (against or in_favor or leans):
                continue
            cell = months.setdefault(day[:7], [0, 0, 0.0])
            cell[0] += int(in_favor)
            cell[1] += int(against)
            if leans:
                materiality = float(p.get("materiality") or 0.0)
                cell[2] += evidence_weight(p.get("evidence")) * (supports - contradicts) * materiality / MAX_MATERIALITY
        points: list[dict[str, Any]] = []
        if months:
            month, last = min(months), max(months)
            balance = 0.0
            while True:
                count_for, count_against, delta = months.get(month, (0, 0, 0.0))
                balance += delta
                points.append(
                    {"month": month, "for": int(count_for), "against": int(count_against), "balance": round(balance, 3)}
                )
                if month == last:
                    break
                month = _next_month(month)
        series[assumption_id] = points
    return series


def _side(members: Sequence[tuple[Mapping[str, Any], str]]) -> dict[str, Any]:
    if not members:
        return {"stance": None, "n": 0, "ids": []}
    total = sum(_weight(passage["p"]) for passage, _ in members)
    stance = sum(_weight(passage["p"]) * _stance(passage["p"]) for passage, _ in members) / total
    ranked = sorted(
        members,
        key=lambda item: (float(item[0]["p"].get("materiality") or 0.0), item[1], item[0]["id"]),
        reverse=True,
    )
    return {"stance": round(stance, 3), "n": len(members), "ids": [passage["id"] for passage, _ in ranked[:DIVERGENCE_IDS]]}


def divergence(
    passages: Sequence[Mapping[str, Any]], pillars: Sequence[str], policy: Policy, *, today: date
) -> list[dict[str, Any]]:
    """Company.divergence: [{pillar, inside, outside, gap, flagged}, ...].

    Within the last `divergence_window_days` (inclusive, as `policy.recent`), compares the
    materiality-weighted mean stance of company voices (guidance, management commentary) with outside
    voices (channel or customer data, expert opinion) per pillar. Pillars missing either side are
    left out; flagged pillars come first, then larger gaps.
    """
    inside: dict[str, list[tuple[Mapping[str, Any], str]]] = defaultdict(list)
    outside: dict[str, list[tuple[Mapping[str, Any], str]]] = defaultdict(list)
    wanted = set(pillars)
    for passage in passages:
        p = passage.get("p")
        if not p or p.get("pillar") not in wanted:
            continue
        if float(p.get("pillar_p") or 0.0) < policy.pillar_probability_min or _boilerplate(p) > policy.boilerplate_max:
            continue
        day = _day(passage)
        if not recent(day, policy.divergence_window_days, today):
            continue
        evidence = p.get("evidence")
        if evidence in INSIDE_EVIDENCE:
            inside[p["pillar"]].append((passage, day or ""))
        elif evidence in OUTSIDE_EVIDENCE:
            outside[p["pillar"]].append((passage, day or ""))
    entries = []
    for pillar in pillars:
        if not inside[pillar] or not outside[pillar]:
            continue
        inner, outer = _side(inside[pillar]), _side(outside[pillar])
        gap = round(inner["stance"] - outer["stance"], 3) + 0.0
        flagged = (
            inner["n"] >= policy.divergence_min_passages
            and outer["n"] >= policy.divergence_min_passages
            and abs(gap) >= policy.divergence_min_gap
        )
        entries.append({"pillar": pillar, "inside": inner, "outside": outer, "gap": gap, "flagged": flagged})
    entries.sort(key=lambda entry: (not entry["flagged"], -abs(entry["gap"])))
    return entries


def form_family(form: str | None) -> str | None:
    for family, forms in FORM_FAMILIES.items():
        if form in forms:
            return family
    return None


def compare_passages(old_rows: Sequence[Mapping[str, Any]], new_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Match a new filing's passages against the previous one's: {changed, added, removed}, untruncated.

    A new passage identical to an old one after whitespace and case normalization is unchanged.
    Otherwise its best old match by word-sequence ratio, among old passages sharing at least
    NEAR_PREFILTER of their content words (found through an inverted index), makes it changed when
    the ratio is at least NEAR_DUPLICATE; else it is added. Old passages nothing matched are removed.
    """
    old_by_text: dict[str, list[int]] = defaultdict(list)
    old_words: list[set[str]] = []
    index: dict[str, list[int]] = defaultdict(list)
    for position, row in enumerate(old_rows):
        old_by_text[normalize(row["text"])].append(position)
        words = content_words(row["text"])
        old_words.append(words)
        for word in words:
            index[word].append(position)

    matched: set[int] = set()
    changed: list[dict[str, Any]] = []
    added: list[int] = []
    for row in new_rows:
        same = old_by_text.get(normalize(row["text"]))
        if same:
            matched.update(same)
            continue
        words = content_words(row["text"])
        shared = Counter(position for word in words for position in index.get(word, ()))
        best_position, best = None, 0.0
        for position in sorted(shared):
            overlap = shared[position]
            if overlap / (len(words) + len(old_words[position]) - overlap) < NEAR_PREFILTER:
                continue
            ratio = sequence_similarity(old_rows[position]["text"], row["text"])
            if ratio > best:
                best_position, best = position, ratio
        if best_position is not None and best >= NEAR_DUPLICATE:
            matched.add(best_position)
            old = old_rows[best_position]
            changed.append(
                {
                    "id": row["passage_id"],
                    "old_id": old["passage_id"],
                    "similarity": round(best, 3),
                    "diff": word_diff(old["text"], row["text"]),
                }
            )
        else:
            added.append(row["passage_id"])
    removed = [
        {"id": row["passage_id"], "text": row["text"], "page": row["page"]}
        for position, row in enumerate(old_rows)
        if position not in matched
    ]
    return {"changed": changed, "added": added, "removed": removed}


def redlines(store: Store, ticker: str, *, max_items: int = REDLINE_MAX_ITEMS) -> list[dict[str, Any]]:
    """Company.redlines for the ticker's own filings (latest vs previous per form family).

    Every passage of both filings is compared, exact repeats included. Each family's lists are
    truncated to `max_items` items in total, keeping changed passages first, then added, then removed.
    """
    families: dict[str, list[Any]] = defaultdict(list)
    for document in store.documents(status="sorted", tickers=[ticker]):
        if document["origin"] != "edgar":
            continue
        family = form_family(document["form"])
        if family is not None:
            families[family].append(document)
    out = []
    for family in FORM_FAMILIES:
        documents = sorted(families.get(family, []), key=lambda d: (d["doc_date"] or "", d["id"]))
        if len(documents) < 2:
            continue
        old_document, new_document = documents[-2], documents[-1]
        comparison = compare_passages(
            store.passages_for_document(old_document["id"]), store.passages_for_document(new_document["id"])
        )
        budget = max(0, max_items)
        lists = {}
        for name in ("changed", "added", "removed"):
            lists[name] = comparison[name][:budget]
            budget -= len(lists[name])
        out.append(
            {"form": family, "new_document": new_document["id"], "old_document": old_document["id"], **lists}
        )
    return out


def prediction_status(by: str, today: date, *, resolved: bool) -> str:
    if resolved:
        return "resolved"
    deadline = date.fromisoformat(by)
    if today > deadline:
        return "overdue"
    if deadline - timedelta(days=DUE_SOON_DAYS) <= today:
        return "due"
    return "open"


def prediction_view(
    thesis: Thesis,
    resolutions: Mapping[str, Mapping[str, Any]],
    *,
    today: date,
    related: Callable[[str], list[int]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(Company.predictions, Company.forecast).

    The forecast is the user's own Brier score over resolved predictions: the mean of
    (p - outcome)^2 with outcome 1 or 0; 0 is perfect, 0.25 is what always saying 50% earns.
    """
    predictions = []
    squared_errors = []
    for prediction in thesis.predictions:
        resolution = resolutions.get(prediction.id)
        outcome = None if resolution is None else resolution.get("outcome")
        resolved = outcome is not None
        if resolved:
            squared_errors.append((prediction.p - (1.0 if outcome else 0.0)) ** 2)
        predictions.append(
            {
                "id": prediction.id,
                "statement": prediction.statement,
                "by": prediction.by,
                "p": prediction.p,
                "pillar": prediction.pillar,
                "outcome": None if outcome is None else bool(outcome),
                "resolved_at": resolution.get("resolved_at") if resolution is not None and resolved else None,
                "status": prediction_status(prediction.by, today, resolved=resolved),
                "related": list(related(prediction.statement)),
            }
        )
    brier = round(sum(squared_errors) / len(squared_errors), 4) if squared_errors else None
    return predictions, {"resolved": len(squared_errors), "brier": brier}
