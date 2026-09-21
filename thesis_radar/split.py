"""Split extracted pages into passages of roughly 100-400 tokens."""

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
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True)
class _Unit:
    page: int
    start: int
    end: int
    text: str
    speaker: str | None


def split_pages(pages: list[str], *, transcript: bool = False) -> list[PassageDraft]:
    units: list[_Unit] = []
    for number, text in enumerate(pages, start=1):
        units.extend(_turns(number, text) if transcript else _paragraphs(number, text))
    drafts: list[PassageDraft] = []
    for unit in _merge(units):
        for piece in _split_long(unit.text):
            drafts.append(
                PassageDraft(
                    seq=len(drafts), page=unit.page, char_start=unit.start, char_end=unit.end,
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
        raw = text[start:end]
        cleaned = " ".join(raw.split())
        if not cleaned:
            continue
        lead = len(raw) - len(raw.lstrip())
        trail = len(raw) - len(raw.rstrip())
        units.append(_Unit(page, start + lead, end - trail, cleaned, None))
    return units


def _turns(page: int, text: str) -> list[_Unit]:
    units: list[_Unit] = []
    speaker: str | None = None
    start: int | None = None
    end = 0
    parts: list[str] = []

    def flush() -> None:
        if parts and start is not None:
            units.append(_Unit(page, start, end, " ".join(" ".join(parts).split()), speaker))

    offset = 0
    for line in text.splitlines(keepends=True):
        body = line.strip()
        line_start = offset + (len(line) - len(line.lstrip()))
        line_end = offset + len(line.rstrip())
        offset += len(line)
        if not body:
            continue
        match = SPEAKER_LABEL.match(body)
        if match:
            flush()
            speaker = match.group("speaker")
            start, end = line_start, line_end
            rest = match.group("rest").strip()
            parts = [rest] if rest else []
        else:
            if start is None:
                start = line_start
            parts.append(body)
            end = line_end
    flush()
    return units


def _merge(units: list[_Unit]) -> list[_Unit]:
    merged: list[_Unit] = []
    for unit in units:
        if merged:
            last = merged[-1]
            combined = f"{last.text} {unit.text}"
            if last.page == unit.page and last.speaker == unit.speaker and estimate_tokens(combined) <= TARGET_TOKENS:
                merged[-1] = _Unit(last.page, last.start, unit.end, combined, last.speaker)
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
