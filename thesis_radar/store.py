"""SQLite storage: documents, passages, judgments, triage, labels, follow-ups, and fetch state.

The schema is versioned with `PRAGMA user_version`; `MIGRATIONS[n]` upgrades a database from
version n to n + 1. Add a migration for every schema change instead of editing an old one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import FollowupRecord, JudgmentRecord, MetricRecord, NewDocument, PassageDraft

SCHEMA_V1 = """
CREATE TABLE documents (
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
    form TEXT,
    accession TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX documents_ticker ON documents (ticker, status);
CREATE TABLE passages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    seq INTEGER NOT NULL,
    page INTEGER NOT NULL,
    char_start INTEGER NOT NULL,
    char_end INTEGER NOT NULL,
    speaker TEXT,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    repeat_of INTEGER REFERENCES passages(id),
    similar_to INTEGER REFERENCES passages(id),
    similarity REAL,
    seen_ids TEXT,
    UNIQUE (document_id, seq)
);
CREATE INDEX passages_hash ON passages (text_sha256);
CREATE TABLE judgments (
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    cache_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    part TEXT NOT NULL,
    model TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    thesis_version TEXT NOT NULL,
    answers_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('judged', 'failed')),
    error TEXT,
    retryable INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (passage_id, cache_key)
);
CREATE INDEX judgments_ticker ON judgments (ticker, passage_id);
CREATE TABLE views (
    id INTEGER PRIMARY KEY,
    generated_at TEXT NOT NULL
);
CREATE TABLE labels (
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    ticker TEXT NOT NULL,
    question TEXT NOT NULL,
    value INTEGER NOT NULL CHECK (value IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ('sample', 'triage', 'spotcheck')),
    weight REAL NOT NULL DEFAULT 1.0,
    judgment_keys TEXT,
    labeled_at TEXT NOT NULL,
    PRIMARY KEY (passage_id, ticker, question)
);
CREATE TABLE triage (
    ticker TEXT NOT NULL,
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    status TEXT CHECK (status IN ('dismissed', 'absorbed', 'acknowledged')),
    starred INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticker, passage_id)
);
CREATE TABLE followups (
    promise_id INTEGER NOT NULL REFERENCES passages(id),
    result_id INTEGER NOT NULL REFERENCES passages(id),
    ticker TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    model TEXT NOT NULL,
    answers_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('judged', 'failed')),
    error TEXT,
    retryable INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (promise_id, result_id, cache_key)
);
CREATE TABLE resolutions (
    ticker TEXT NOT NULL,
    prediction_id TEXT NOT NULL,
    outcome INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (ticker, prediction_id)
);
CREATE TABLE fetch_state (
    ticker TEXT PRIMARY KEY,
    cik TEXT NOT NULL,
    last_accession TEXT,
    fetched_at TEXT NOT NULL
);
"""

FTS_V1 = """
CREATE VIRTUAL TABLE passages_fts USING fts5(text, content='passages', content_rowid='id');
CREATE TRIGGER passages_fts_insert AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER passages_fts_delete AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts (passages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

SCHEMA_V2 = """
CREATE TABLE metric_judgments (
    passage_id INTEGER NOT NULL REFERENCES passages(id),
    cache_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    part TEXT NOT NULL,
    model TEXT NOT NULL,
    answers_json TEXT,
    status TEXT NOT NULL CHECK (status IN ('judged', 'failed')),
    error TEXT,
    retryable INTEGER NOT NULL DEFAULT 1,
    input_tokens INTEGER,
    request_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (passage_id, cache_key)
);
CREATE INDEX metric_judgments_ticker ON metric_judgments (ticker, passage_id);
"""

MIGRATIONS: tuple[str, ...] = (SCHEMA_V1, SCHEMA_V2)
SCHEMA_VERSION = len(MIGRATIONS)

_PASSAGE_COLUMNS = """
    p.id AS passage_id, p.document_id, p.seq, p.page, p.char_start, p.char_end, p.speaker, p.text,
    p.repeat_of, p.similar_to, p.similarity, p.seen_ids,
    d.ticker, d.title, d.source_type, d.doc_date, d.path, d.ingested_at, d.form, d.origin, d.status AS doc_status
"""

UNSET: Any = object()


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fts_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


class SchemaTooNew(RuntimeError):
    """The database was written by a newer thesis-radar."""


class Store:
    def __init__(self, path: Path | str, *, clock: Callable[[], str] = utc_now, readonly: bool = False) -> None:
        if readonly:
            self.conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._clock = clock
        if readonly:
            self._check_version()  # a read-only connection cannot migrate; tables added since read as empty
        else:
            self._migrate()
        self.has_fts = self._table_exists("passages_fts")
        self.has_metric_judgments = self._table_exists("metric_judgments")

    # Setup

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row is not None

    def _check_version(self) -> int:
        version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise SchemaTooNew(f"radar.db has schema version {version}; this radar understands up to {SCHEMA_VERSION}")
        return version

    def _migrate(self) -> None:
        version = self._check_version()
        for target in range(version, SCHEMA_VERSION):
            self.conn.executescript("BEGIN;" + MIGRATIONS[target] + f"PRAGMA user_version = {target + 1};COMMIT;")
            if target == 0 and fts_available():
                self.conn.executescript("BEGIN;" + FTS_V1 + "COMMIT;")

    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn:
            yield

    def now(self) -> str:
        return self._clock()

    # Documents

    def find_document_by_hash(self, text_sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE text_sha256 = ?", (text_sha256,)).fetchone()

    def get_document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def documents(self, status: str | None = None, *, tickers: Sequence[str] | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if tickers is not None:
            if not tickers:
                return []
            clauses.append(f"ticker IN ({', '.join('?' for _ in tickers)})")
            params.extend(tickers)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.conn.execute(f"SELECT * FROM documents {where} ORDER BY id", params).fetchall()

    def insert_document(self, doc: NewDocument) -> int:
        cursor = self.conn.execute(
            """INSERT INTO documents (text_sha256, path, ticker, ticker_p, source_type, source_type_p,
                   doc_date, doc_date_p, title, origin, status, status_reason, form, accession, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.text_sha256, doc.path, doc.ticker, doc.ticker_p, doc.source_type, doc.source_type_p,
                doc.doc_date, doc.doc_date_p, doc.title, doc.origin, doc.status, doc.status_reason,
                doc.form, doc.accession, self._clock(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def tag_document(self, document_id: int, *, ticker: str, source_type: str, doc_date: str, path: str) -> None:
        self.conn.execute(
            """UPDATE documents SET ticker = ?, ticker_p = 1.0, source_type = ?, source_type_p = 1.0,
                   doc_date = ?, doc_date_p = 1.0, status = 'sorted', status_reason = NULL, path = ?
               WHERE id = ?""",
            (ticker, source_type, doc_date, path, document_id),
        )

    # Passages

    def insert_passages(self, document_id: int, drafts: Iterable[PassageDraft]) -> list[int]:
        ids = []
        for d in drafts:
            cursor = self.conn.execute(
                """INSERT INTO passages (document_id, seq, page, char_start, char_end, speaker, text, text_sha256)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, d.seq, d.page, d.char_start, d.char_end, d.speaker, d.text, sha256_text(d.text)),
            )
            ids.append(int(cursor.lastrowid or 0))
        return ids

    def replace_passages(self, document_id: int, drafts: Iterable[PassageDraft]) -> list[int]:
        """Replace a document's passages. Only safe for documents that were never judged or labeled."""
        old = [row[0] for row in self.conn.execute("SELECT id FROM passages WHERE document_id = ?", (document_id,))]
        if old:
            marks = ", ".join("?" for _ in old)
            self.conn.execute(f"UPDATE passages SET repeat_of = NULL WHERE repeat_of IN ({marks})", old)
            self.conn.execute(
                f"UPDATE passages SET similar_to = NULL, similarity = NULL WHERE similar_to IN ({marks})", old
            )
            for table in ("judgments", "labels", "triage", "metric_judgments"):
                self.conn.execute(f"DELETE FROM {table} WHERE passage_id IN ({marks})", old)
            self.conn.execute(f"DELETE FROM followups WHERE promise_id IN ({marks}) OR result_id IN ({marks})", old + old)
            self.conn.execute("DELETE FROM passages WHERE document_id = ?", (document_id,))
        return self.insert_passages(document_id, drafts)

    def passages_for_document(self, document_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"""SELECT {_PASSAGE_COLUMNS} FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE p.document_id = ? ORDER BY p.seq""",
            (document_id,),
        ).fetchall()

    def passages_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self.passages_for_tickers([ticker])

    def passages_for_tickers(self, tickers: Sequence[str]) -> list[sqlite3.Row]:
        if not tickers:
            return []
        marks = ", ".join("?" for _ in tickers)
        return self.conn.execute(
            f"""SELECT {_PASSAGE_COLUMNS} FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE d.status = 'sorted' AND d.ticker IN ({marks}) ORDER BY d.id, p.seq""",
            tuple(tickers),
        ).fetchall()

    def get_passages(self, passage_ids: Sequence[int]) -> list[sqlite3.Row]:
        if not passage_ids:
            return []
        rows: list[sqlite3.Row] = []
        unique = list(dict.fromkeys(passage_ids))
        for start in range(0, len(unique), 500):
            chunk = unique[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            rows.extend(
                self.conn.execute(
                    f"""SELECT {_PASSAGE_COLUMNS} FROM passages p JOIN documents d ON d.id = p.document_id
                        WHERE p.id IN ({marks})""",
                    tuple(chunk),
                ).fetchall()
            )
        return sorted(rows, key=lambda row: row["passage_id"])

    def set_passage_links(
        self, passage_id: int, *, repeat_of: int | None, similar_to: int | None, similarity: float | None,
        seen_ids: Sequence[int],
    ) -> None:
        self.conn.execute(
            "UPDATE passages SET repeat_of = ?, similar_to = ?, similarity = ?, seen_ids = ? WHERE id = ?",
            (repeat_of, similar_to, similarity, json.dumps(list(seen_ids)), passage_id),
        )

    def earlier_passages(
        self, *, ticker: str, document_id: int, doc_date: str, match: str | None, limit: int
    ) -> list[sqlite3.Row]:
        """Passages of the ticker's sorted documents that come before (doc_date, document_id).

        With FTS and a `match` query, the best `limit` matches by BM25; otherwise the latest `limit`.
        """
        before = "(d.doc_date < ? OR (d.doc_date = ? AND d.id < ?))"
        params: list[Any] = [ticker, doc_date, doc_date, document_id]
        if self.has_fts and match:
            return self.conn.execute(
                f"""SELECT p.id AS passage_id, p.text, p.text_sha256, p.repeat_of, d.doc_date, d.source_type,
                           d.id AS document_id
                    FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid
                    JOIN documents d ON d.id = p.document_id
                    WHERE passages_fts MATCH ? AND d.status = 'sorted' AND d.ticker = ? AND {before}
                    ORDER BY bm25(passages_fts) LIMIT ?""",
                [match, *params, limit],
            ).fetchall()
        return self.conn.execute(
            f"""SELECT p.id AS passage_id, p.text, p.text_sha256, p.repeat_of, d.doc_date, d.source_type,
                       d.id AS document_id
                FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE d.status = 'sorted' AND d.ticker = ? AND {before}
                ORDER BY d.doc_date DESC, p.id DESC LIMIT ?""",
            [*params, limit],
        ).fetchall()

    def exact_earlier_passage(self, *, ticker: str, document_id: int, doc_date: str, text_sha256: str) -> int | None:
        row = self.conn.execute(
            """SELECT p.id, p.repeat_of FROM passages p JOIN documents d ON d.id = p.document_id
               WHERE p.text_sha256 = ? AND d.status = 'sorted' AND d.ticker = ?
                 AND (d.doc_date < ? OR (d.doc_date = ? AND d.id < ?))
               ORDER BY d.doc_date, d.id, p.seq LIMIT 1""",
            (text_sha256, ticker, doc_date, doc_date, document_id),
        ).fetchone()
        if row is None:
            return None
        return int(row["repeat_of"] or row["id"])

    def search(self, match: str, *, tickers: Sequence[str] | None = None, limit: int = 50) -> list[sqlite3.Row]:
        """Full-text search over passages of sorted documents; `match` is an FTS5 query."""
        clauses = ["d.status = 'sorted'"]
        params: list[Any] = []
        if tickers is not None:
            if not tickers:
                return []
            clauses.append(f"d.ticker IN ({', '.join('?' for _ in tickers)})")
            params.extend(tickers)
        where = " AND ".join(clauses)
        if self.has_fts:
            return self.conn.execute(
                f"""SELECT {_PASSAGE_COLUMNS}, bm25(passages_fts) AS rank,
                           snippet(passages_fts, 0, '[', ']', ' … ', 24) AS snippet
                    FROM passages_fts JOIN passages p ON p.id = passages_fts.rowid
                    JOIN documents d ON d.id = p.document_id
                    WHERE passages_fts MATCH ? AND {where} ORDER BY rank LIMIT ?""",
                [match, *params, limit],
            ).fetchall()
        like = "%" + match.replace('"', "").strip() + "%"
        return self.conn.execute(
            f"""SELECT {_PASSAGE_COLUMNS}, 0.0 AS rank, substr(p.text, 1, 200) AS snippet
                FROM passages p JOIN documents d ON d.id = p.document_id
                WHERE p.text LIKE ? AND {where} ORDER BY d.doc_date DESC LIMIT ?""",
            [like, *params, limit],
        ).fetchall()

    # Judgments

    def judgment(self, passage_id: int, cache_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM judgments WHERE passage_id = ? AND cache_key = ?", (passage_id, cache_key)
        ).fetchone()

    def has_judged(self, passage_id: int, cache_key: str) -> bool:
        row = self.judgment(passage_id, cache_key)
        return row is not None and row["status"] == "judged"

    def judgments_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT passage_id, cache_key, part, status, answers_json, error, retryable, created_at
               FROM judgments WHERE ticker = ? ORDER BY created_at, rowid""",
            (ticker,),
        ).fetchall()

    def latest_judged(self, passage_id: int, ticker: str | None = None) -> dict[str, Any] | None:
        """The newest judged answers for each part, merged; None when the passage was never judged."""
        params: list[Any] = [passage_id]
        clause = ""
        if ticker is not None:
            clause = "AND ticker = ?"
            params.append(ticker)
        rows = self.conn.execute(
            f"""SELECT part, answers_json FROM judgments WHERE passage_id = ? {clause} AND status = 'judged'
                ORDER BY created_at, rowid""",
            params,
        ).fetchall()
        if not rows:
            return None
        by_part: dict[str, dict[str, Any]] = {}
        for row in rows:
            by_part[row["part"]] = json.loads(row["answers_json"])
        merged: dict[str, Any] = {}
        for part in sorted(by_part):
            merged.update(by_part[part])
        return merged

    def answers_for_keys(self, passage_id: int, cache_keys: Sequence[str]) -> dict[str, Any] | None:
        merged: dict[str, Any] = {}
        for key in cache_keys:
            row = self.judgment(passage_id, key)
            if row is None or row["status"] != "judged":
                return None
            merged.update(json.loads(row["answers_json"]))
        return merged or None

    def save_judgment(self, record: JudgmentRecord) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO judgments (passage_id, cache_key, ticker, part, model, rubric_version,
                       thesis_version, answers_json, status, error, retryable, input_tokens, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.passage_id, record.cache_key, record.ticker, record.part, record.model,
                    record.rubric_version, record.thesis_version,
                    None if record.answers is None else json.dumps(record.answers), record.status, record.error,
                    int(record.retryable), record.input_tokens, record.request_id, self._clock(),
                ),
            )

    def usage_since(self, since: str) -> tuple[int, int]:
        """(requests, input tokens) for judgments, follow-ups, and metric judgments created at or after `since`."""
        total_requests = total_tokens = 0
        for table in ("judgments", "followups", "metric_judgments"):
            if table == "metric_judgments" and not self.has_metric_judgments:
                continue
            row = self.conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(input_tokens), 0) FROM {table} WHERE created_at >= ? AND status = 'judged'",
                (since,),
            ).fetchone()
            total_requests += int(row[0])
            total_tokens += int(row[1])
        return total_requests, total_tokens

    # Follow-ups (guidance ledger)

    def followup(self, promise_id: int, result_id: int, cache_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM followups WHERE promise_id = ? AND result_id = ? AND cache_key = ?",
            (promise_id, result_id, cache_key),
        ).fetchone()

    def followups_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM followups WHERE ticker = ? ORDER BY created_at, rowid", (ticker,)
        ).fetchall()

    def save_followup(self, record: FollowupRecord) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO followups (promise_id, result_id, ticker, cache_key, model, answers_json,
                       status, error, retryable, input_tokens, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.promise_id, record.result_id, record.ticker, record.cache_key, record.model,
                    None if record.answers is None else json.dumps(record.answers), record.status, record.error,
                    int(record.retryable), record.input_tokens, record.request_id, self._clock(),
                ),
            )

    # Metric judgments (KPI tracker)

    def metric_judgment(self, passage_id: int, cache_key: str) -> sqlite3.Row | None:
        if not self.has_metric_judgments:
            return None
        return self.conn.execute(
            "SELECT * FROM metric_judgments WHERE passage_id = ? AND cache_key = ?", (passage_id, cache_key)
        ).fetchone()

    def metric_judgments_for_ticker(self, ticker: str) -> list[sqlite3.Row]:
        if not self.has_metric_judgments:
            return []  # a schema-v1 database opened read-only (radar mcp before any other command since upgrading)
        return self.conn.execute(
            "SELECT * FROM metric_judgments WHERE ticker = ? ORDER BY created_at, rowid", (ticker,)
        ).fetchall()

    def save_metric_judgment(self, record: MetricRecord) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO metric_judgments (passage_id, cache_key, ticker, part, model, answers_json,
                       status, error, retryable, input_tokens, request_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.passage_id, record.cache_key, record.ticker, record.part, record.model,
                    None if record.answers is None else json.dumps(record.answers), record.status, record.error,
                    int(record.retryable), record.input_tokens, record.request_id, self._clock(),
                ),
            )

    # Views

    def record_view(self, generated_at: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO views (generated_at) VALUES (?)", (generated_at,))

    def last_view(self) -> str | None:
        row = self.conn.execute("SELECT generated_at FROM views ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else row["generated_at"]

    # Triage

    def set_triage(self, ticker: str, passage_id: int, *, status: Any = UNSET, starred: Any = UNSET) -> None:
        current = self.conn.execute(
            "SELECT status, starred FROM triage WHERE ticker = ? AND passage_id = ?", (ticker, passage_id)
        ).fetchone()
        new_status = (current["status"] if current else None) if status is UNSET else status
        new_starred = (bool(current["starred"]) if current else False) if starred is UNSET else bool(starred)
        with self.conn:
            if new_status is None and not new_starred:
                self.conn.execute("DELETE FROM triage WHERE ticker = ? AND passage_id = ?", (ticker, passage_id))
            else:
                self.conn.execute(
                    """INSERT OR REPLACE INTO triage (ticker, passage_id, status, starred, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (ticker, passage_id, new_status, int(new_starred), self._clock()),
                )

    def triage_map(self, ticker: str) -> dict[int, dict[str, Any]]:
        rows = self.conn.execute("SELECT passage_id, status, starred FROM triage WHERE ticker = ?", (ticker,))
        return {row["passage_id"]: {"status": row["status"], "starred": bool(row["starred"])} for row in rows}

    # Labels

    def save_label(
        self,
        passage_id: int,
        question: str,
        value: bool,
        *,
        ticker: str,
        origin: str = "sample",
        weight: float = 1.0,
        judgment_keys: Sequence[str] | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO labels (passage_id, ticker, question, value, origin, weight, judgment_keys,
                       labeled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    passage_id, ticker, question, int(value), origin, float(weight),
                    None if judgment_keys is None else json.dumps(list(judgment_keys)), self._clock(),
                ),
            )

    def labels(self, ticker: str | None = None) -> list[sqlite3.Row]:
        clause, params = ("WHERE l.ticker = ?", (ticker,)) if ticker is not None else ("", ())
        return self.conn.execute(
            f"""SELECT l.passage_id, p.document_id, l.ticker, l.question, l.value, l.origin, l.weight,
                       l.judgment_keys, l.labeled_at
                FROM labels l JOIN passages p ON p.id = l.passage_id {clause}
                ORDER BY l.passage_id, l.question""",
            params,
        ).fetchall()

    def labeled_passage_ids(self, ticker: str | None = None) -> set[int]:
        if ticker is None:
            return {row[0] for row in self.conn.execute("SELECT DISTINCT passage_id FROM labels")}
        return {row[0] for row in self.conn.execute("SELECT DISTINCT passage_id FROM labels WHERE ticker = ?", (ticker,))}

    # Prediction resolutions

    def set_resolution(self, ticker: str, prediction_id: str, outcome: bool | None) -> None:
        with self.conn:
            if outcome is None:
                self.conn.execute(
                    "DELETE FROM resolutions WHERE ticker = ? AND prediction_id = ?", (ticker, prediction_id)
                )
            else:
                self.conn.execute(
                    "INSERT OR REPLACE INTO resolutions (ticker, prediction_id, outcome, resolved_at) VALUES (?, ?, ?, ?)",
                    (ticker, prediction_id, int(outcome), self._clock()),
                )

    def resolutions(self, ticker: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT prediction_id, outcome, resolved_at FROM resolutions WHERE ticker = ?", (ticker,))
        return {row["prediction_id"]: {"outcome": bool(row["outcome"]), "resolved_at": row["resolved_at"]} for row in rows}

    # EDGAR fetch state

    def fetch_state(self, ticker: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM fetch_state WHERE ticker = ?", (ticker,)).fetchone()

    def set_fetch_state(self, ticker: str, cik: str, last_accession: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO fetch_state (ticker, cik, last_accession, fetched_at) VALUES (?, ?, ?, ?)",
                (ticker, cik, last_accession, self._clock()),
            )
