"""SQLite storage: documents, passages, judgments, views, labels, and fetch state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import JudgmentRecord, NewDocument, PassageDraft

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    text_sha256 TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    ticker TEXT,
    ticker_p REAL,
    source_type TEXT,
    source_type_p REAL,
    doc_date TEXT,
    doc_date_p REAL,
    title TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('inbox', 'edgar')),
    status TEXT NOT NULL CHECK (status IN ('sorted', 'unsorted', 'failed')),
    status_reason TEXT,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    seq INTEGER NOT NULL,
    page INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    speaker TEXT,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    UNIQUE (document_id, seq)
);
CREATE TABLE IF NOT EXISTS judgments (
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    cache_key TEXT NOT NULL,
    model TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    thesis_version TEXT NOT NULL,
    answers_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('judged', 'failed')),
    error TEXT,
    input_tokens INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (passage_id, cache_key)
);
CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    question TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value IN (0, 1)),
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (passage_id, question)
);
CREATE TABLE IF NOT EXISTS fetch_state (
    ticker TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    last_accession TEXT,
    fetched_at TEXT NOT NULL
);
"""

_PASSAGE_COLUMNS = """
    p.id AS passage_id, p.document_id, p.seq, p.page, p.char_start, p.char_end, p.speaker, p.text,
    d.ticker, d.title, d.source_type, d.doc_date, d.path, d.ingested_at, d.origin
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], str] = utc_now) -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._clock = clock

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn:
            yield

    # Documents

    def find_document_by_hash(self, text_sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE text_sha256 = ?", (text_sha256,)).fetchone()

    def get_document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def documents(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return self.conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM documents WHERE status = ? ORDER BY id", (status,)).fetchall()

    def insert_document(self, doc: NewDocument) -> int:
        cursor = self.conn.execute(
            """INSERT INTO documents (text_sha256, path, ticker, ticker_p, source_type, source_type_p,
                   doc_date, doc_date_p, title, origin, status, status_reason, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.text_sha256, doc.path, doc.ticker, doc.ticker_p, doc.source_type, doc.source_type_p,
                doc.doc_date, doc.doc_date_p, doc.title, doc.origin, doc.status, doc.status_reason, self._clock(),
            ),
        )
        return int(cursor.lastrowid)

    def tag_document(self, document_id: int, *, ticker: str, source_type: str, doc_date: str, path: str) -> None:
        self.conn.execute(
            """UPDATE documents SET ticker = ?, ticker_p = 1.0, source_type = ?, source_type_p = 1.0,
                   doc_date = ?, doc_date_p = 1.0, status = 'sorted', status_reason = NULL, path = ?
               WHERE id = ?""",
            (ticker, source_type, doc_date, path, document_id),
        )

    # Passages

    def insert_passages(self, document_id: int, drafts: Iterable[PassageDraft]) -> None:
        self.conn.executemany(
            """INSERT INTO passages (document_id, seq, page, char_start, char_end, speaker, text, text_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (document_id, d.seq, d.page, d.char_start, d.char_end, d.speaker, d.text, sha256_text(d.text))
                for d in drafts
            ],
        )

    def passages_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"""SELECT {_PASSAGE_COLUMNS} FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE d.status = 'sorted' AND d.ticker = ? ORDER BY d.id, p.seq""",
            (ticker,),
        ).fetchall()

    def get_passages(self, passage_ids: Sequence[int]) -> list[sqlite3.Row]:
        if not passage_ids:
            return []
        marks = ", ".join("?" for _ in passage_ids)
        return self.conn.execute(
            f"""SELECT {_PASSAGE_COLUMNS} FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE p.id IN ({marks}) ORDER BY p.id""",
            tuple(passage_ids),
        ).fetchall()

    # Judgments

    def judgment(self, passage_id: int, cache_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM judgments WHERE passage_id = ? AND cache_key = ?", (passage_id, cache_key)
        ).fetchone()

    def has_judged(self, passage_id: int, cache_key: str) -> bool:
        row = self.judgment(passage_id, cache_key)
        return row is not None and row["status"] == "judged"

    def latest_judged(self, passage_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT answers_json FROM judgments WHERE passage_id = ? AND status = 'judged'
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (passage_id,),
        ).fetchone()
        return None if row is None else json.loads(row["answers_json"])

    def save_judgment(self, record: JudgmentRecord) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO judgments (passage_id, cache_key, model, rubric_version, thesis_version,
                       answers_json, status, error, input_tokens, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.passage_id, record.cache_key, record.model, record.rubric_version, record.thesis_version,
                    None if record.answers is None else json.dumps(record.answers), record.status, record.error,
                    record.input_tokens, record.request_id, self._clock(),
                ),
            )

    # Views

    def record_view(self, generated_at: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO views (generated_at) VALUES (?)", (generated_at,))

    def last_view(self) -> str | None:
        row = self.conn.execute("SELECT generated_at FROM views ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else row["generated_at"]

    # Labels

    def save_label(self, passage_id: int, question: str, value: bool) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO labels (passage_id, question, value, labeled_at) VALUES (?, ?, ?, ?)",
                (passage_id, question, int(value), self._clock()),
            )

    def labels(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT l.passage_id, p.document_id, l.question, l.value, d.ticker FROM labels l
               JOIN passages p ON p.id = l.passage_id JOIN documents d ON d.id = p.document_id
               ORDER BY l.passage_id, l.question"""
        ).fetchall()

    def labeled_passage_ids(self) -> set[int]:
        return {row[0] for row in self.conn.execute("SELECT DISTINCT passage_id FROM labels")}

    # EDGAR fetch state

    def fetch_state(self, ticker: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM fetch_state WHERE ticker = ?", (ticker,)).fetchone()

    def set_fetch_state(self, ticker: str, cik: str, last_accession: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO fetch_state (ticker, cik, last_accession, fetched_at) VALUES (?, ?, ?, ?)",
                (ticker, cik, last_accession, self._clock()),
            )
