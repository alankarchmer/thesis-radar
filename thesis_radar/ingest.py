"""Turn inbox files and fetched filings into documents and passages; resolve unsorted documents."""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Workspace
from .extract import Extracted, ExtractionError, extract, html_to_text
from .judge import Judge, JudgeError
from .models import NewDocument
from .policy import Policy
from .rubric import NONE, SOURCE_TYPES, document_request, normalize_date
from .split import split_pages
from .store import Store
from .thesis import Thesis

TRANSCRIPT_TYPES = frozenset({"earnings_transcript", "expert_call"})


@dataclass
class IngestReport:
    sorted: int = 0
    unsorted: int = 0
    failed: int = 0
    duplicates: int = 0
    deferred: int = 0
    messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Metadata:
    ticker: str | None
    ticker_p: float | None
    source_type: str | None
    source_type_p: float | None
    doc_date: str | None
    doc_date_p: float | None
    problems: tuple[str, ...]


def slugify(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:limit].rstrip("-") or "untitled"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"no free file name near {path}")


def archive_path(ws: Workspace, *, ticker: str, doc_date: str | None, source_type: str | None, title: str, suffix: str) -> Path:
    name = f"{doc_date or 'undated'}_{source_type or 'unknown'}_{slugify(title)}{suffix}"
    return unique_path(ws.archive / ticker / name)


def resolve_metadata(
    answers: Mapping[str, Mapping[str, Any]], policy: Policy, *, fallback_date: str
) -> Metadata:
    problems: list[str] = []
    ticker = answers["ticker"]["choice"]
    ticker_p = float(answers["ticker"]["confidence"])
    if ticker == NONE:
        problems.append("no matching company")
        ticker = None
    elif ticker_p < policy.metadata_min_confidence:
        problems.append(f"unsure of company ({ticker_p:.2f})")
    source_type = answers["source_type"]["choice"]
    source_p = float(answers["source_type"]["confidence"])
    if source_p < policy.metadata_min_confidence:
        problems.append(f"unsure of source type ({source_p:.2f})")
    if "doc_date" in answers:
        picked = answers["doc_date"]["choice"]
        doc_date_p: float | None = float(answers["doc_date"]["confidence"])
        if picked == NONE:
            problems.append("no document date among the dates found")
            doc_date = None
        else:
            doc_date = normalize_date(picked)
            if doc_date_p < policy.metadata_min_confidence:
                problems.append(f"unsure of date ({doc_date_p:.2f})")
    else:
        doc_date, doc_date_p = fallback_date, None
    return Metadata(ticker, ticker_p, source_type, source_p, doc_date, doc_date_p, tuple(problems))


async def ingest_inbox(
    ws: Workspace, store: Store, theses: Mapping[str, Thesis], judge: Judge, *, model: str, policy: Policy
) -> IngestReport:
    report = IngestReport()
    files = sorted(p for p in ws.inbox.iterdir() if p.is_file() and not p.name.startswith("."))
    for path in files:
        try:
            extracted = extract(path)
        except ExtractionError as exc:
            _fail(ws, path, str(exc))
            report.failed += 1
            report.messages.append(f"failed: {path.name}: {exc}")
            continue

        if extracted.has_text:
            digest = extracted.text_sha256()
        else:
            digest = "bytes:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if store.find_document_by_hash(digest) is not None:
            _move(path, unique_path(ws.archive / "_duplicates" / path.name))
            report.duplicates += 1
            continue

        if not extracted.has_text:
            dest = unique_path(ws.archive / "_unsorted" / path.name)
            with store.transaction():
                store.insert_document(
                    NewDocument(text_sha256=digest, path=_relative(ws, dest), title=extracted.title,
                                origin="inbox", status="unsorted", status_reason="needs OCR")
                )
                _move(path, dest)
            report.unsorted += 1
            continue

        text = "\n\n".join(extracted.pages)
        request, _ = document_request(theses, file_name=path.name, title=extracted.title, text=text, model=model)
        try:
            result = await judge.judge(request)
        except JudgeError as exc:
            report.deferred += 1
            report.messages.append(f"deferred: {path.name}: {exc}")
            continue

        meta = resolve_metadata(result.answers, policy, fallback_date=_mtime_date(path))
        status = "unsorted" if meta.problems else "sorted"
        if status == "sorted":
            assert meta.ticker is not None
            dest = archive_path(
                ws, ticker=meta.ticker, doc_date=meta.doc_date, source_type=meta.source_type,
                title=extracted.title, suffix=path.suffix.lower(),
            )
        else:
            dest = unique_path(ws.archive / "_unsorted" / path.name)
        drafts = split_pages(extracted.pages, transcript=meta.source_type in TRANSCRIPT_TYPES)
        with store.transaction():
            document_id = store.insert_document(
                NewDocument(
                    text_sha256=digest, path=_relative(ws, dest), title=extracted.title, origin="inbox",
                    status=status, ticker=meta.ticker, ticker_p=meta.ticker_p, source_type=meta.source_type,
                    source_type_p=meta.source_type_p, doc_date=meta.doc_date, doc_date_p=meta.doc_date_p,
                    status_reason="; ".join(meta.problems) or None,
                )
            )
            store.insert_passages(document_id, drafts)
            _move(path, dest)
        if status == "sorted":
            report.sorted += 1
        else:
            report.unsorted += 1
    return report


def store_filing(
    ws: Workspace, store: Store, *, ticker: str, form: str, filing_date: str, name: str, content: bytes
) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in {".htm", ".html"}:
        text = html_to_text(content.decode("utf-8", errors="replace"))
    elif suffix == ".txt":
        text = content.decode("utf-8", errors="replace")
    else:
        return "skipped"
    title = f"{ticker} {form} {filing_date} {name}"
    extracted = Extracted(pages=[text], title=title)
    if not extracted.has_text:
        return "empty"
    digest = extracted.text_sha256()
    if store.find_document_by_hash(digest) is not None:
        return "duplicate"
    dest = archive_path(
        ws, ticker=ticker, doc_date=filing_date, source_type="filing", title=f"{form}-{Path(name).stem}", suffix=suffix
    )
    drafts = split_pages(extracted.pages)
    with store.transaction():
        document_id = store.insert_document(
            NewDocument(
                text_sha256=digest, path=_relative(ws, dest), title=title, origin="edgar", status="sorted",
                ticker=ticker, ticker_p=1.0, source_type="filing", source_type_p=1.0,
                doc_date=filing_date, doc_date_p=1.0,
            )
        )
        store.insert_passages(document_id, drafts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return "stored"


def tag_document(ws: Workspace, store: Store, document_id: int, *, ticker: str, source_type: str, doc_date: str) -> Path:
    row = store.get_document(document_id)
    if row is None:
        raise ValueError(f"no document with id {document_id}")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown source type {source_type!r}; choose one of {', '.join(SOURCE_TYPES)}")
    if normalize_date(doc_date) != doc_date:
        raise ValueError("date must be written as YYYY-MM-DD")
    current = ws.root / row["path"]
    dest = archive_path(
        ws, ticker=ticker, doc_date=doc_date, source_type=source_type, title=row["title"], suffix=current.suffix.lower()
    )
    with store.transaction():
        store.tag_document(document_id, ticker=ticker, source_type=source_type, doc_date=doc_date, path=_relative(ws, dest))
        _move(current, dest)
    return dest


def _move(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))


def _fail(ws: Workspace, path: Path, reason: str) -> None:
    dest = unique_path(ws.failed / path.name)
    _move(path, dest)
    dest.with_name(dest.name + ".reason.txt").write_text(reason + "\n", encoding="utf-8")


def _relative(ws: Workspace, path: Path) -> str:
    return path.relative_to(ws.root).as_posix()


def _mtime_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
