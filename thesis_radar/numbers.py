"""Find numbers and reporting periods in passage text, deterministically.

Code finds the candidates; Jev only chooses among them (which metric a number measures, what kind
of number it is, which period it covers). Nothing here guesses what a number means.

A `Mention` is one number (or range) as written, with its kind of unit and its value in base units
(dollars, percent, basis points, days, a count, or a multiple). A value is negative when its notation
or wording says so: an attached minus sign (-5%, −5%, -$10 million, $-10), accounting parentheses
around the number alone ((5)%, $(10) million, (120) bps), parentheses around a number and its unit
where a table puts them ("| (5%) |", "20.6% 21.8% (1.2%)", but not an aside such as "Europe (25%)"),
or a word of decline right next to it ("declined 6%", "down $10 million", "an 8% decline").

A `PeriodCandidate` is a reporting period named in the text, resolved to a sortable key such as
"2026-Q3", "FY2027", "2026-H2", or "2026-09" using the document's date for any missing year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Kinds of unit a number can carry.
PERCENT, BPS, CURRENCY, DAYS, MULTIPLE, COUNT = "percent", "bps", "currency", "days", "multiple", "count"

_SCALES = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "mm": 1e6, "m": 1e6, "mn": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillion": 1e12, "tn": 1e12,
}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SCALE = r"(?:thousand|million|billion|trillion|bn|mm|mn|tn|[kmb])\b"
_DASH = r"\s*(?:to|-|–|—|and)\s*"
_MINUS = "-−–"  # hyphen-minus, the minus sign, and the en dash PDFs often set for a minus


def _signed(name: str, *, parens: bool = True) -> str:
    """A number with an optional attached minus sign (-5, −5) and, if `parens`, accounting parentheses ((5))."""
    if not parens:
        return rf"(?P<{name}_minus>[{_MINUS}])?(?P<{name}>{_NUM})"
    return rf"(?P<{name}_minus>[{_MINUS}])?(?P<{name}_paren>\()?(?P<{name}>{_NUM})(?({name}_paren)\))"


def _money(name: str, *, dollar_optional: bool = False) -> str:
    """-$10, $-10, $(10), or $10 (the dollar sign optional for the high end of "$7.0-7.4 billion")."""
    dollar = r"\$?" if dollar_optional else r"\$"
    return (rf"(?P<{name}_minus>[{_MINUS}])?{dollar}\s?(?P<{name}_minus2>[{_MINUS}])?"
            rf"(?P<{name}_paren>\()?(?P<{name}>{_NUM})(?({name}_paren)\))")


_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (CURRENCY, re.compile(
        rf"(?<![\w.]){_money('a')}\s*(?P<sa>{_SCALE})?(?:{_DASH}{_money('b', dollar_optional=True)}\s*(?P<sb>{_SCALE})?)?", re.I)),
    (PERCENT, re.compile(
        rf"(?<![\w.$]){_signed('a')}\s*(?:%|percent|per cent)?{_DASH}{_signed('b')}\s*(?:%|percent\b|per cent\b)", re.I)),
    (PERCENT, re.compile(rf"(?<![\w.$]){_signed('a')}\s*(?:%|percent\b|per cent\b)", re.I)),
    (BPS, re.compile(rf"(?<![\w.$]){_signed('a')}(?:{_DASH}{_signed('b')})?\s*(?:basis[ -]points?|bps|bp)\b", re.I)),
    (DAYS, re.compile(rf"(?<![\w.$]){_signed('a')}(?:{_DASH}{_signed('b')})?\s*days?\b", re.I)),
    (MULTIPLE, re.compile(rf"(?<![\w.$]){_signed('a')}(?:{_DASH}{_signed('b')})?\s*(?:x\b|times\b)", re.I)),
    # Counts: a number followed by a word ("41,000 units", "1,100 retail reservations"); years are not counts.
    # No parentheses: "(12) Represents ..." is a footnote marker far more often than a negative count.
    (COUNT, re.compile(
        rf"(?<![\w.$]){_signed('a', parens=False)}(?:{_DASH}{_signed('b', parens=False)})?\s+(?P<noun>[a-z][a-z\-]{{2,}})",
        re.I)),
]
# A word of decline right before a number ("declined 6%", "down by about $10 million", "a decrease of 120 bps")
# or right after it ("an 8% decline", "5% lower"). "fell to 20.6%" and "down from 22%" name a level, not a decline.
_DOWN_BEFORE = re.compile(
    r"(?:^|\W)(?:declin\w*|decreas\w*|fell|falls|falling|fall\s+of|drop(?:s|ped|ping)?|down|reduc\w*|contract(?:ed|ion)"
    r"|shr[au]nk|negative|minus|lower\s+by)\s+(?:(?:by|of)\s+)?"
    r"(?:(?:about|approximately|roughly|nearly|almost|around|some|over|more\s+than|less\s+than)\s+)?$",
    re.I,
)
_DOWN_AFTER = re.compile(r"\s+(?:declines?|decreases?|drops?|reductions?|contraction|lower|less)\b(?!-)", re.I)
_DOWN_NOUNS = frozenset({"fewer"})  # "1,200 fewer units"
_NOT_COUNT_NOUNS = frozenset(
    {"percent", "per", "basis", "bps", "days", "day", "times", "to", "and", "or", "of", "in", "on", "at", "for", "from",
     "the", "a", "an", "is", "was", "were", "are", "million", "billion", "thousand", "trillion", "quarter", "quarters",
     "year", "years", "month", "months", "week", "weeks", "fiscal", "versus", "vs", "compared", "with", "as", "by"}
)


@dataclass(frozen=True)
class Mention:
    index: int
    start: int
    end: int
    text: str
    kind: str
    value: float  # base units: dollars, percent, basis points, days, a count, or a multiple
    high: float | None = None  # set for a range ("20% to 21%")
    sentence: str = ""

    @property
    def id(self) -> str:
        return f"m{self.index}"


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _negative(match: re.Match[str], name: str) -> bool:
    groups = match.groupdict()
    return any(groups.get(f"{name}_{mark}") for mark in ("minus", "minus2", "paren"))


def _in_table_parens(text: str, start: int, end: int) -> bool:
    """Parentheses around a number and its unit ("(1.2%)", "($10 million)") where a table puts them: in a cell
    ("| (1.2%) |") or after another figure ("20.6% 21.8% (1.2%)"). Elsewhere they are an aside ("Europe (25%)")."""
    if start == 0 or text[start - 1] != "(" or text[end : end + 1] != ")":
        return False
    before = text[: start - 1].rstrip(" \t")
    return not before or before[-1] in "\n|%)" or before[-1].isdigit()


def _declining(text: str, start: int, end: int) -> bool:
    return bool(_DOWN_BEFORE.search(text[max(0, start - 40) : start]) or _DOWN_AFTER.match(text, end))


def _is_year(raw: str) -> bool:
    return "," not in raw and "." not in raw and len(raw) == 4 and 1900 <= int(raw) <= 2100


def sentences(text: str) -> list[tuple[int, int]]:
    """(start, end) spans of sentences, split after . ! ? followed by whitespace and a capital or digit."""
    spans, start = [], 0
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9\"“(])", text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return spans


def find_mentions(text: str, *, limit: int = 40) -> list[Mention]:
    """Every number with a recognizable unit, in text order, without overlaps (first pattern wins)."""
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, int, str, float, float | None]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            a, b = match.group("a"), match.groupdict().get("b")
            if kind == COUNT:
                noun = match.group("noun").lower()
                if noun in _NOT_COUNT_NOUNS or _is_year(a) or (b is not None and _is_year(b)):
                    continue
                if _number(a) < 10 and "," not in a:
                    continue  # "3 new models" is a quantity in prose, rarely a tracked metric
            value = -_number(a) if _negative(match, "a") else _number(a)
            high = None if b is None else -_number(b) if _negative(match, "b") else _number(b)
            end = start + len(text[start:end].rstrip())  # an optional tail ("$10 " before no scale) can end in spaces
            if _in_table_parens(text, start, end):
                start, end = start - 1, end + 1  # the mention reads "(1.2%)", as written
                value, high = -abs(value), None if high is None else -abs(high)
            elif _declining(text, start, end) or (kind == COUNT and match.group("noun").lower() in _DOWN_NOUNS):
                value, high = -abs(value), None if high is None else -abs(high)
            if kind == CURRENCY:
                scale_a = _SCALES.get((match.group("sa") or "").lower(), 1.0)
                scale_b = _SCALES.get((match.group("sb") or "").lower(), scale_a)
                if match.group("sa") is None and match.group("sb") is not None:
                    scale_a = scale_b  # "$7.0 to $7.4 billion"
                value *= scale_a
                high = high * scale_b if high is not None else None
            if high is not None and high < value:
                value, high = high, value
            taken.append((start, end))
            found.append((start, end, kind, value, high))
    found.sort()
    spans = sentences(text)
    mentions = []
    for index, (start, end, kind, value, high) in enumerate(found[:limit], start=1):
        s_start, s_end = next(((a, b) for a, b in spans if a <= start < b), (0, len(text)))
        mentions.append(Mention(index, start, end, text[start:end].strip(), kind, value, high, text[s_start:s_end].strip()))
    return mentions


# Metric units and the mention kinds that can measure them.


def unit_kinds(unit: str) -> frozenset[str]:
    u = unit.strip().lower().replace(" ", "")
    if u in {"%", "percent", "pct"}:
        return frozenset({PERCENT})
    if u in {"bps", "bp", "basispoints"}:
        return frozenset({BPS})
    if u in {"pp", "pts", "points", "percentagepoints"}:
        return frozenset({BPS, PERCENT})
    if u.startswith("$") or u in {"usd", "dollars"}:
        return frozenset({CURRENCY})
    if u in {"days", "day"}:
        return frozenset({DAYS})
    if u in {"x", "times", "multiple"}:
        return frozenset({MULTIPLE})
    return frozenset({COUNT})


_UNIT_SCALES = {"$": 1.0, "$k": 1e3, "$m": 1e6, "$mm": 1e6, "$b": 1e9, "$bn": 1e9, "usd": 1.0, "dollars": 1.0}


def to_unit(mention: Mention, unit: str) -> tuple[float, float | None]:
    """The mention's value (and high) expressed in the metric's unit."""
    u = unit.strip().lower().replace(" ", "")
    factor = 1.0
    if mention.kind == CURRENCY:
        factor = 1.0 / _UNIT_SCALES.get(u, 1.0)
    elif mention.kind == BPS and u in {"pp", "pts", "points", "percentagepoints"}:
        factor = 0.01
    value = round(mention.value * factor, 6)
    high = round(mention.high * factor, 6) if mention.high is not None else None
    return value, high


# Periods.

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9

_PERIOD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("quarter", re.compile(r"\b(?P<q>first|second|third|fourth|1st|2nd|3rd|4th)[- ]quarter(?:\s+(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?(?P<y>(?:19|20)\d{2}))?", re.I)),
    ("quarter", re.compile(r"\bQ(?P<q>[1-4])(?:\s*(?:FY|fiscal)?\s*'?(?P<y>(?:19|20)?\d{2}))?\b", re.I)),
    ("quarter", re.compile(r"\b(?P<q>[1-4])Q\s*'?(?P<y>(?:19|20)?\d{2})?\b", re.I)),
    ("half", re.compile(r"\b(?P<h>first|second|back|front)[- ]half(?:\s+(?:of\s+)?(?:fiscal\s+)?(?P<y>(?:19|20)\d{2}))?", re.I)),
    ("half", re.compile(r"\bH(?P<h>[12])\s*'?(?P<y>(?:19|20)?\d{2})?\b")),
    ("year", re.compile(r"\b(?:fiscal(?:\s+year)?|FY)\s*'?(?P<y>(?:19|20)?\d{2})\b", re.I)),
    ("year", re.compile(r"\bfull[- ]year(?:\s+(?:of\s+)?(?:fiscal\s+)?(?P<y>(?:19|20)\d{2}))?", re.I)),
    ("year", re.compile(r"\bcalendar\s+(?P<y>(?:19|20)\d{2})\b", re.I)),
    ("month", re.compile(r"\b(?P<m>january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\.?(?:\s+(?P<y>(?:19|20)\d{2}))?\b", re.I)),
]


_DAY_AFTER = re.compile(r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b(?!\s*(?:%|percent))")


@dataclass(frozen=True)
class PeriodCandidate:
    key: str  # "2026-Q3", "2026-H2", "FY2026", "2026-09"
    label: str  # "Q3 2026", "H2 2026", "FY 2026", "Sep 2026"
    written: str  # as it appears in the text


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip("'")
    value = int(raw)
    return value + 2000 if value < 100 else value


def _end_month(kind: str, sub: int) -> int:
    return {"quarter": sub * 3, "half": sub * 6, "year": 12, "month": sub}[kind]


def _nearest_year(kind: str, sub: int, today: date) -> int:
    """The year whose instance of this period ends closest to `today` (the document's date)."""
    best, best_gap = today.year, None
    for year in (today.year - 1, today.year, today.year + 1):
        end = date(year, _end_month(kind, sub), 28)
        gap = abs((end - today).days)
        if best_gap is None or gap < best_gap:
            best, best_gap = year, gap
    return best


def period_key(kind: str, sub: int, year: int) -> PeriodCandidate:
    if kind == "quarter":
        return PeriodCandidate(f"{year}-Q{sub}", f"Q{sub} {year}", "")
    if kind == "half":
        return PeriodCandidate(f"{year}-H{sub}", f"H{sub} {year}", "")
    if kind == "year":
        return PeriodCandidate(f"FY{year}", f"FY {year}", "")
    month = date(year, sub, 1).strftime("%b")
    return PeriodCandidate(f"{year}-{sub:02d}", f"{month} {year}", "")


def period_sort_key(key: str) -> tuple[int, int, int]:
    """Sort periods by when they end; a fiscal year sorts after its fourth quarter."""
    if key.startswith("FY"):
        return int(key[2:]), 12, 3
    year, part = key.split("-")
    if part.startswith("Q"):
        return int(year), int(part[1]) * 3, 1
    if part.startswith("H"):
        return int(year), int(part[1]) * 6, 2
    return int(year), int(part), 0


def period_granularity(key: str) -> str:
    if key.startswith("FY"):
        return "year"
    part = key.split("-")[1]
    return {"Q": "quarter", "H": "half"}.get(part[0], "month")


def find_periods(text: str, doc_date: date) -> list[PeriodCandidate]:
    """Reporting periods named in `text`, resolved against the document date, unique, in text order."""
    found: list[tuple[int, PeriodCandidate]] = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PERIOD_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < t_end and t_start < end for t_start, t_end in taken):
                continue
            groups = match.groupdict()
            if kind == "quarter":
                raw = groups["q"].lower()
                sub = _ORDINALS.get(raw) or int(raw)
            elif kind == "half":
                raw = groups["h"].lower()
                sub = {"first": 1, "front": 1, "second": 2, "back": 2, "1": 1, "2": 2}[raw]
            elif kind == "month":
                name = groups["m"].lower().rstrip(".")
                if not groups.get("y") and (len(name) <= 3 or name == "may"):
                    continue  # bare "may", "mar", "jun" are ordinary words far more often than months
                if _DAY_AFTER.match(text, match.start("m") + len(groups["m"])):
                    continue  # "September 18, 2026" is a date (a dateline, a call date), not a reporting period
                sub = _MONTHS[name]
            else:
                sub = 12
            year = _year(groups.get("y")) or _nearest_year(kind, sub, doc_date)
            candidate = period_key(kind, sub, year)
            taken.append((start, end))
            found.append((start, PeriodCandidate(candidate.key, candidate.label, match.group(0).strip())))
    seen: set[str] = set()
    unique = []
    for _, candidate in sorted(found, key=lambda item: item[0]):
        if candidate.key not in seen:
            seen.add(candidate.key)
            unique.append(candidate)
    return unique


def last_completed_quarter(doc_date: date) -> PeriodCandidate:
    """The quarter that most recently ended on or before the document date: what a report usually covers."""
    quarter = (doc_date.month - 1) // 3  # quarters fully before this month's quarter
    year = doc_date.year
    if quarter == 0:
        quarter, year = 4, year - 1
    return period_key("quarter", quarter, year)
