import json
import urllib.error
from datetime import date

import pytest

from thesis_radar.config import Workspace
from thesis_radar.edgar import EdgarError, fetch_all, find_cik, load_cik_map, make_http_get, new_filings
from thesis_radar.store import Store

TODAY = date(2026, 9, 21)
ROWS = [
    ("0000001-26-000004", "8-K", "2026-09-10", "acme-8k.htm"),
    ("0000001-26-000003", "4", "2026-09-01", "form4.xml"),
    ("0000001-26-000002", "10-Q", "2026-08-01", "acme-10q.htm"),
    ("0000001-25-000001", "10-K", "2025-02-01", "acme-10k.htm"),
]


def submissions(rows):
    return {
        "filings": {
            "recent": {
                "accessionNumber": [r[0] for r in rows],
                "form": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "primaryDocument": [r[3] for r in rows],
            }
        }
    }


class FakeSec:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if url not in self.pages:
            raise EdgarError(f"GET {url} failed: The read operation timed out")
        return self.pages[url]


class OkResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return b"ok"


def index_headers(documents):
    """An EDGAR -index-headers.html page: each document's SGML header, HTML-escaped inside <pre>."""
    blocks = "".join(
        f"&lt;DOCUMENT&gt;\n&lt;TYPE&gt;{kind}\n&lt;SEQUENCE&gt;{n}\n&lt;FILENAME&gt;{name}\n"
        f"&lt;DESCRIPTION&gt;{kind}\n&lt;TEXT&gt;\n<a href=\"{name}\">Document {n} - file: {name}</a><br>\n&lt;/DOCUMENT&gt;\n"
        for n, (kind, name) in enumerate(documents, start=1)
    )
    return f"<html><body><pre>{blocks}</pre></body></html>".encode()


def sec_pages():
    base = "https://www.sec.gov/Archives/edgar/data/1"
    headers = index_headers(
        [("8-K", "acme-8k.htm"), ("EX-99.1", "acme-q3-results.htm"), ("EX-10.1", "acme-credit-agreement.htm"),
         ("EX-101.SCH", "acme-20260910.xsd")]
    )
    return {
        "https://www.sec.gov/files/company_tickers.json": json.dumps({"0": {"cik_str": 1, "ticker": "ACME", "title": "ACME"}}).encode(),
        "https://data.sec.gov/submissions/CIK0000000001.json": json.dumps(submissions(ROWS)).encode(),
        f"{base}/000000126000002/acme-10q.htm": b"<p>ACME 10-Q. Inventory rose.</p>",
        f"{base}/000000126000004/acme-8k.htm": b"<p>ACME 8-K cover page.</p>",
        f"{base}/000000126000004/0000001-26-000004-index-headers.html": headers,
        f"{base}/000000126000004/acme-q3-results.htm": b"<p>ACME third quarter results. Revenue grew.</p>",
        f"{base}/000000126000004/acme-credit-agreement.htm": b"<p>Credit agreement.</p>",
    }


def test_first_fetch_covers_one_year_oldest_first():
    assert [p[1] for p in new_filings(submissions(ROWS), last_accession=None, today=TODAY)] == ["10-Q", "8-K"]


def test_later_fetches_stop_at_the_last_accession():
    picked = new_filings(submissions(ROWS), last_accession="0000001-26-000002", today=TODAY)
    assert [p[0] for p in picked] == ["0000001-26-000004"]


def test_fetch_all_stores_filings_and_exhibits_once(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    store = Store(ws.db_path)
    report = fetch_all(ws, store, ["ACME"], FakeSec(sec_pages()), today=TODAY)
    assert (report.stored, report.duplicates, report.skipped) == (3, 0, 0)
    assert sorted(d["title"] for d in store.documents()) == [
        "ACME 10-Q 2026-08-01 acme-10q.htm",
        "ACME 8-K 2026-09-10 acme-8k.htm",
        "ACME 8-K 2026-09-10 acme-q3-results.htm",
    ]
    assert store.fetch_state("ACME")["last_accession"] == "0000001-26-000004"
    assert fetch_all(ws, store, ["ACME"], FakeSec(sec_pages()), today=TODAY).stored == 0
    store.close()


def test_unknown_tickers_are_reported(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    store = Store(ws.db_path)
    report = fetch_all(ws, store, ["ZZZZ"], FakeSec(sec_pages()), today=TODAY)
    assert report.messages == ["ZZZZ: not found in the SEC ticker list"]
    store.close()


def test_class_share_tickers_are_found_with_dashes():
    http = FakeSec({"https://www.sec.gov/files/company_tickers.json": json.dumps({"0": {"cik_str": 7, "ticker": "BRK-B", "title": "B"}}).encode()})
    assert find_cik(load_cik_map(http), "BRK.B") == 7


def test_http_get_sends_the_contact_email_and_spaces_requests(monkeypatch):
    seen, sleeps = [], []

    def fake_urlopen(request, timeout):
        seen.append(request.get_header("User-agent"))
        return OkResponse()

    monkeypatch.setattr("thesis_radar.edgar.urllib.request.urlopen", fake_urlopen)
    get = make_http_get("me@example.com", clock=lambda: 5.0, sleep=sleeps.append)
    assert get("https://example.test/a") == b"ok" and get("https://example.test/b") == b"ok"
    assert seen == ["thesis-radar me@example.com"] * 2
    assert sleeps == [pytest.approx(0.11)]


def test_http_get_retries_a_timed_out_request(monkeypatch):
    replies = [TimeoutError("The read operation timed out"), OkResponse()]
    calls, sleeps = [], []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("thesis_radar.edgar.urllib.request.urlopen", fake_urlopen)
    get = make_http_get("me@example.com", clock=lambda: 5.0, sleep=sleeps.append)
    assert get("https://example.test/slow") == b"ok"
    assert len(calls) == 2
    assert any(seconds >= 1 for seconds in sleeps)


def test_http_get_gives_up_after_three_attempts(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("thesis_radar.edgar.urllib.request.urlopen", fake_urlopen)
    get = make_http_get("me@example.com", clock=lambda: 5.0, sleep=lambda seconds: None)
    with pytest.raises(EdgarError, match="timed out"):
        get("https://example.test/dead")
    assert len(calls) == 3


def test_http_get_does_not_retry_a_missing_file(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr("thesis_radar.edgar.urllib.request.urlopen", fake_urlopen)
    get = make_http_get("me@example.com", clock=lambda: 5.0, sleep=lambda seconds: None)
    with pytest.raises(EdgarError, match="404"):
        get("https://example.test/missing")
    assert len(calls) == 1


def test_a_failed_download_skips_that_ticker_until_the_next_fetch(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    store = Store(ws.db_path)
    pages = sec_pages()
    broken = {url: body for url, body in pages.items() if not url.endswith("acme-q3-results.htm")}
    report = fetch_all(ws, store, ["ACME", "ZZZZ"], FakeSec(broken), today=TODAY)
    assert report.stored == 2
    assert report.messages[0].startswith("ACME: ") and "next fetch" in report.messages[0]
    assert report.messages[1] == "ZZZZ: not found in the SEC ticker list"
    assert store.fetch_state("ACME") is None
    again = fetch_all(ws, store, ["ACME"], FakeSec(pages), today=TODAY)
    assert (again.stored, again.duplicates) == (1, 2)
    assert store.fetch_state("ACME")["last_accession"] == "0000001-26-000004"
    store.close()
