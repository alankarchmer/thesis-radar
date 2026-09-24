"""Plain records passed between modules and the store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NewDocument:
    text_sha256: str
    path: str
    title: str
    origin: str  # "inbox" or "edgar"
    status: str  # "sorted", "unsorted", or "failed"
    ticker: str | None = None
    ticker_p: float | None = None
    source_type: str | None = None
    source_type_p: float | None = None
    doc_date: str | None = None
    doc_date_p: float | None = None
    status_reason: str | None = None
    form: str | None = None
    accession: str | None = None


@dataclass(frozen=True)
class PassageDraft:
    seq: int
    page: int
    char_start: int
    char_end: int
    text: str
    speaker: str | None = None


@dataclass(frozen=True)
class JudgmentRecord:
    passage_id: int
    cache_key: str
    model: str
    rubric_version: str
    thesis_version: str
    status: str  # "judged" or "failed"
    answers: dict[str, Any] | None = None
    error: str | None = None
    input_tokens: int | None = None
    request_id: str | None = None
    ticker: str = ""
    part: str = "p0"
    retryable: bool = True


@dataclass(frozen=True)
class FollowupRecord:
    promise_id: int
    result_id: int
    ticker: str
    cache_key: str
    model: str
    status: str  # "judged" or "failed"
    answers: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = True
    input_tokens: int | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class MetricRecord:
    passage_id: int
    cache_key: str
    ticker: str
    part: str
    model: str
    status: str  # "judged" or "failed"
    answers: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = True
    input_tokens: int | None = None
    request_id: str | None = None
