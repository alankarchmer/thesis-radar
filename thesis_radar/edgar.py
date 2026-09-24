"""Fetch new filings from SEC EDGAR for thesis tickers and their peers."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import DEFAULT_EDGAR_FORMS, DEFAULT_PEER_FORMS, Workspace
from .ingest import store_filing
from .store import Store

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{name}"
INDEX_HEADERS_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/{accession}-index-headers.html"
FIRST_FETCH_DAYS = 365
MIN_INTERVAL_SECONDS = 0.11
EXHIBIT_FORMS = frozenset({"8-K", "8-K/A", "6-K"})
# Transient failures (a read timeout, a dropped connection, 429, 5xx) are retried with backoff.
ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

HttpGet = Callable[[str], bytes]


class EdgarError(RuntimeError):
    """A request to SEC EDGAR failed."""


def make_http_get(
    email: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> HttpGet:
    last = [float("-inf")]

    def get(url: str) -> bytes:
        for attempt in range(1, ATTEMPTS + 1):
            wait = last[0] + MIN_INTERVAL_SECONDS - clock()
            if wait > 0:
                sleep(wait)
            last[0] = clock()
            request = urllib.request.Request(
                url, headers={"User-Agent": f"thesis-radar {email}", "Accept-Encoding": "identity"}
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRY_STATUSES or attempt == ATTEMPTS:
                    raise EdgarError(f"GET {url} failed: {exc}") from exc
            except OSError as exc:  # timeouts and connection failures are usually momentary
                if attempt == ATTEMPTS:
                    raise EdgarError(f"GET {url} failed after {ATTEMPTS} attempts: {exc}") from exc
            sleep(RETRY_BACKOFF_SECONDS * attempt)
        raise AssertionError("unreachable")

    return get


def load_cik_map(http_get: HttpGet) -> dict[str, int]:
    data = json.loads(http_get(TICKERS_URL))
    return {str(entry["ticker"]).upper(): int(entry["cik_str"]) for entry in data.values()}


def find_cik(cik_map: dict[str, int], ticker: str) -> int | None:
    return cik_map.get(ticker) or cik_map.get(ticker.replace(".", "-"))


def new_filings(
    submissions: dict[str, Any], *, last_accession: str | None, today: date, forms: Sequence[str] = DEFAULT_EDGAR_FORMS
) -> list[tuple[str, str, str, str]]:
    """(accession, form, filing date, primary document) for new filings of `forms`, oldest first."""
    recent = submissions["filings"]["recent"]
    rows = zip(recent["accessionNumber"], recent["form"], recent["filingDate"], recent["primaryDocument"], strict=False)
    cutoff = (today - timedelta(days=FIRST_FETCH_DAYS)).isoformat()
    wanted = {form.upper() for form in forms}
    picked = []
    for accession, form, filed, primary in rows:
        if accession == last_accession or filed < cutoff:
            break
        if form.upper() in wanted:
            picked.append((accession, form, filed, primary))
    picked.reverse()
    return picked


_DOCUMENT = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S | re.I)
_TYPE = re.compile(r"<TYPE>([^\s<]+)", re.I)
_FILENAME = re.compile(r"<FILENAME>([^\s<]+)", re.I)


def exhibit_99_names(index_headers: str) -> list[str]:
    """HTML files filed as exhibit 99 (where earnings releases live), by their declared type, not their name.

    File names are unreliable: Polaris files its release as `pii-q22026earningsrelease.htm`.
    The filing's `-index-headers.html` declares every document's `<TYPE>`.
    """
    names = []
    for block in _DOCUMENT.findall(html.unescape(index_headers)):
        kind, name = _TYPE.search(block), _FILENAME.search(block)
        if kind and name and kind.group(1).upper().startswith("EX-99") and name.group(1).lower().endswith((".htm", ".html")):
            names.append(name.group(1))
    return names


@dataclass
class FetchReport:
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0
    messages: list[str] = field(default_factory=list)


def fetch_all(
    ws: Workspace,
    store: Store,
    tickers: Sequence[str],
    http_get: HttpGet,
    *,
    today: date,
    forms: Sequence[str] = DEFAULT_EDGAR_FORMS,
    peers: Sequence[str] = (),
    peer_forms: Sequence[str] = DEFAULT_PEER_FORMS,
) -> FetchReport:
    """Fetch every ticker; a failure for one ticker is reported and the others still run.

    Peers are fetched for read-through: only exhibit 99 documents (earnings releases) of `peer_forms`.
    """
    report = FetchReport()
    cik_map = load_cik_map(http_get)
    plan = [(ticker, forms, False) for ticker in tickers]
    plan += [(peer, peer_forms, True) for peer in peers if peer not in tickers]
    for ticker, ticker_forms, exhibits_only in plan:
        cik = find_cik(cik_map, ticker)
        if cik is None:
            report.messages.append(f"{ticker}: not found in the SEC ticker list")
            continue
        try:
            _fetch_ticker(ws, store, ticker, cik, http_get, today=today, forms=ticker_forms,
                          exhibits_only=exhibits_only, report=report)
        except (EdgarError, ValueError, KeyError) as exc:
            # The ticker's fetch state is not advanced, so the next fetch retries it; stored files deduplicate.
            report.messages.append(f"{ticker}: {exc}; will retry at the next fetch")
    return report


def _fetch_ticker(
    ws: Workspace,
    store: Store,
    ticker: str,
    cik: int,
    http_get: HttpGet,
    *,
    today: date,
    forms: Sequence[str],
    exhibits_only: bool,
    report: FetchReport,
) -> None:
    state = store.fetch_state(ticker)
    submissions = json.loads(http_get(SUBMISSIONS_URL.format(cik=cik)))
    accessions = submissions["filings"]["recent"]["accessionNumber"]
    last = state["last_accession"] if state is not None else None
    for accession, form, filed, primary in new_filings(submissions, last_accession=last, today=today, forms=forms):
        folder = accession.replace("-", "")
        names = [] if exhibits_only else [primary]
        if form.upper() in EXHIBIT_FORMS:
            headers = http_get(INDEX_HEADERS_URL.format(cik=cik, folder=folder, accession=accession))
            names += [name for name in exhibit_99_names(headers.decode("utf-8", errors="replace")) if name != primary]
        for name in names:
            content = http_get(FILING_URL.format(cik=cik, folder=folder, name=name))
            outcome = store_filing(
                ws, store, ticker=ticker, form=form, filing_date=filed, name=name, content=content, accession=accession
            )
            if outcome == "stored":
                report.stored += 1
            elif outcome == "duplicate":
                report.duplicates += 1
            else:
                report.skipped += 1
    # Only after every filing of this ticker succeeded, so a failure is retried next run.
    if accessions:
        store.set_fetch_state(ticker, str(cik), accessions[0])
