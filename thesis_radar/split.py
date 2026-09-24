"""Split extracted pages into passages of roughly 100-400 tokens.

Every passage records the character span it came from within its page, including the pieces of an
over-long paragraph, so links and skim views point at the right place.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .models import PassageDraft

TARGET_TOKENS = 250
MAX_TOKENS = 400
CHARS_PER_TOKEN = 4

SPEAKER_LABEL = re.compile(
    r"^(?P<speaker>Operator|Moderator|Interviewer|Analyst|Expert|Client|Q|A"
    r"|[A-Z][a-zA-Z'\-]+(?: [A-Z][a-zA-Z'\-]+){1,3}(?: [-–—] [^:\n]{1,80})?)"
    r":\s*(?P<rest>.*)$"
)
_NOT_SPEAKERS = frozenset(
    {"forward-looking statements", "safe harbor", "table of contents", "risk factors", "note", "source", "disclaimer"}
)
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True)
class _Unit:
    page: int
    text: str
    raw: tuple[int, ...]  # raw page offset of every character of `text`
    speaker: str | None

    @property
    def start(self) -> int:
        return self.raw[0]

    @property
    def end(self) -> int:
        return self.raw[-1] + 1


def _unit(page: int, source: str, spans: list[tuple[int, int]], speaker: str | None) -> _Unit | None:
    if not spans:
        return None
    chars: list[str] = []
    raw: list[int] = []
    for index, (start, end) in enumerate(spans):
        if index:
            chars.append(" ")
            raw.append(spans[index - 1][1] - 1)
        chars.append(source[start:end])
        raw.extend(range(start, end))
    return _Unit(page, "".join(chars), tuple(raw), speaker)


def split_pages(pages: list[str], *, transcript: bool = False) -> list[PassageDraft]:
    units: list[_Unit] = []
    for number, text in enumerate(pages, start=1):
        units.extend(_turns(number, text) if transcript else _paragraphs(number, text))
    drafts: list[PassageDraft] = []
    for unit in _merge(units):
        cursor = 0
        for piece in _split_long(unit.text):
            start = unit.text.find(piece, cursor)
            if start < 0:  # cannot happen for pieces cut from the text, but never lose a passage over it
                start = cursor
            end = min(len(unit.text), start + len(piece))
            cursor = end
            drafts.append(
                PassageDraft(
                    seq=len(drafts), page=unit.page, char_start=unit.raw[start], char_end=unit.raw[end - 1] + 1,
                    text=piece, speaker=unit.speaker,
                )
            )
    return drafts


def _paragraphs(page: int, text: str) -> list[_Unit]:
    bounds = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        bounds.append((cursor, match.start()))
        cursor = match.end()
    bounds.append((cursor, len(text)))
    units = []
    for start, end in bounds:
        spans = [(start + m.start(), start + m.end()) for m in _WORD.finditer(text[start:end])]
        unit = _unit(page, text, spans, None)
        if unit is not None:
            units.append(unit)
    return units


def _speaker(body: str) -> tuple[str, str] | None:
    match = SPEAKER_LABEL.match(body)
    if not match or match.group("speaker").lower() in _NOT_SPEAKERS:
        return None
    return match.group("speaker"), match.group("rest")


def _turns(page: int, text: str) -> list[_Unit]:
    units: list[_Unit] = []
    speaker: str | None = None
    spans: list[tuple[int, int]] = []

    def flush() -> None:
        unit = _unit(page, text, spans, speaker)
        if unit is not None:
            units.append(unit)

    offset = 0
    for line in text.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        body = line.strip()
        if not body:
            continue
        labelled = _speaker(body)
        if labelled is not None:
            flush()
            speaker = labelled[0]
            rest_start = line_start + line.index(body) + len(body) - len(labelled[1])
            spans = [(rest_start + m.start(), rest_start + m.end()) for m in _WORD.finditer(labelled[1])]
        else:
            spans.extend((line_start + m.start(), line_start + m.end()) for m in _WORD.finditer(line))
    flush()
    return units


def _merge(units: list[_Unit]) -> list[_Unit]:
    merged: list[_Unit] = []
    for unit in units:
        if merged:
            last = merged[-1]
            combined = f"{last.text} {unit.text}"
            if last.page == unit.page and last.speaker == unit.speaker and estimate_tokens(combined) <= TARGET_TOKENS:
                merged[-1] = _Unit(last.page, combined, last.raw + (last.raw[-1],) + unit.raw, last.speaker)
                continue
        merged.append(unit)
    return merged


def _split_long(text: str) -> list[str]:
    if estimate_tokens(text) <= MAX_TOKENS:
        return [text]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_BREAK.split(text):
        candidate = f"{current} {sentence}".strip()
        if current and estimate_tokens(candidate) > MAX_TOKENS:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [chunk for piece in pieces for chunk in _hard_cut(piece)]


def _hard_cut(text: str) -> list[str]:
    limit = MAX_TOKENS * CHARS_PER_TOKEN
    chunks = []
    while len(text) > limit:
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks
