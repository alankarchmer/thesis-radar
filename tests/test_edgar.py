import json
from datetime import date

import pytest

from thesis_radar.config import Workspace
from thesis_radar.edgar import EdgarError, fetch_all, find_cik, is_exhibit_99, load_cik_map, make_http_get, new_filings
from thesis_radar.store import Store

TODAY = date(2026, 9, 21)
ROWS = [
    ("0000001-26-000005", "10-Q/A", "2026-09-12", "acme-10qa.htm"),
    ("0000001-26-000004", "8-K", "2026-09-10", "acme-8k.htm"),
    ("0000001-26-000003", "4", "2026-09-01", "form4.xml"),
    ("0000001-26-000002", "10-Q", "2026-08-01", "acme-10q.htm"),
    ("0000001-25-000001", "10-K", "2025-02-01", "acme-10k.htm"),
]
BASE = "https://www.sec.gov/Archives/edgar/data/1"


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
            raise EdgarError(f"GET {url} failed: 404")
        return self.pages[url]


def sec_pages():
    index = {"directory": {"item": [{"name": "acme-8k.htm"}, {"name": "ex99-1.htm"}, {"name": "R1.htm"}]}}
    peer_index = {"directory": {"item": [{"name": "brrr-8k.htm"}, {"name": "brrr_exhibit99.htm"}]}}
    return {
        "https://www.sec.gov/files/company_tickers.json": json.dumps({
            "0": {"cik_str": 1, "ticker": "ACME", "title": "ACME"},
            "1": {"cik_str": 2, "ticker": "BRRR", "title": "Brrr"},
            "2": {"cik_str": 3, "ticker": "BROKE", "title": "Broke"},
        }).encode(),
        "https://data.sec.gov/submissions/CIK0000000001.json": json.dumps(submissions(ROWS)).encode(),
        "https://data.sec.gov/submissions/CIK0000000002.json": json.dumps(
            submissions([("0000002-26-000001", "8-K", "2026-09-05", "brrr-8k.htm")])
        ).encode(),
        "https://data.sec.gov/submissions/CIK0000000003.json": json.dumps(
            submissions([("0000003-26-000001", "10-Q", "2026-09-05", "missing.htm")])
        ).encode(),
        f"{BASE}/000000126000002/acme-10q.htm": b"<p>ACME 10-Q. Inventory rose.</p>",
        f"{BASE}/000000126000005/acme-10qa.htm": b"<p>ACME 10-Q/A. Restated inventory.</p>",
        f"{BASE}/000000126000004/acme-8k.htm": b"<p>ACME 8-K cover page.</p>",
        f"{BASE}/000000126000004/index.json": json.dumps(index).encode(),
        f"{BASE}/000000126000004/ex99-1.htm": b"<p>ACME third quarter results. Revenue grew.</p>",
        "https://www.sec.gov/Archives/edgar/data/2/000000226000001/index.json": json.dumps(peer_index).encode(),
        "https://www.sec.gov/Archives/edgar/data/2/000000226000001/brrr_exhibit99.htm": b"<p>Brrr results. Dealers cut orders.</p>",
    }


def test_first_fetch_covers_one_year_oldest_first():
    assert [p[1] for p in new_filings(submissions(ROWS), last_accession=None, today=TODAY)] == ["10-Q", "8-K", "10-Q/A"]
    only = new_filings(submissions(ROWS), last_accession=None, today=TODAY, forms=["10-Q"])
    assert [p[1] for p in only] == ["10-Q"]


def test_later_fetches_stop_at_the_last_accession():
    picked = new_filings(submissions(ROWS), last_accession="0000001-26-000002", today=TODAY)
    assert [p[0] for p in picked] == ["0000001-26-000004", "0000001-26-000005"]


def test_exhibit_names():
    assert is_exhibit_99("ex99-1.htm") and is_exhibit_99("d123dex991.htm") and is_exhibit_99("Exhibit_99.1.html")
    assert not is_exhibit_99("ex10-1.htm") and not is_exhibit_99("ex99.pdf")


def test_fetch_all_stores_filings_exhibits_and_peers_once(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    store = Store(ws.db_path)
    report = fetch_all(ws, store, ["ACME"], FakeSec(sec_pages()), today=TODAY, peers=["BRRR"])
    assert (report.stored, report.duplicates, report.skipped) == (5, 0, 0), report.messages
    assert sorted(d["title"] for d in store.documents()) == [
        "ACME 10-Q 2026-08-01 acme-10q.htm",
        "ACME 10-Q/A 2026-09-12 acme-10qa.htm",
        "ACME 8-K 2026-09-10 acme-8k.htm",
        "ACME 8-K 2026-09-10 ex99-1.htm",
        "BRRR 8-K 2026-09-05 brrr_exhibit99.htm",
    ]
    assert {d["accession"] for d in store.documents() if d["ticker"] == "BRRR"} == {"0000002-26-000001"}
    assert store.fetch_state("ACME")["last_accession"] == "0000001-26-000005"
    assert fetch_all(ws, store, ["ACME"], FakeSec(sec_pages()), today=TODAY, peers=["BRRR"]).stored == 0
    store.close()


def test_one_failing_ticker_does_not_stop_the_others(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    store = Store(ws.db_path)
    report = fetch_all(ws, store, ["BROKE", "ACME", "ZZZZ"], FakeSec(sec_pages()), today=TODAY)
    assert report.stored == 4
    assert any(m.startswith("BROKE: GET") for m in report.messages)
    assert "ZZZZ: not found in the SEC ticker list" in report.messages
    assert store.fetch_state("BROKE") is None
    store.close()


def test_class_share_tickers_are_found_with_dashes():
    http = FakeSec({"https://www.sec.gov/files/company_tickers.json": json.dumps({"0": {"cik_str": 7, "ticker": "BRK-B", "title": "B"}}).encode()})
    assert find_cik(load_cik_map(http), "BRK.B") == 7


def test_http_get_sends_the_contact_email_and_spaces_requests(monkeypatch):
    seen, sleeps = [], []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        seen.append(request.get_header("User-agent"))
        return Response()

    monkeypatch.setattr("thesis_radar.edgar.urllib.request.urlopen", fake_urlopen)
    get = make_http_get("me@example.com", clock=lambda: 5.0, sleep=sleeps.append)
    assert get("https://example.test/a") == b"ok" and get("https://example.test/b") == b"ok"
    assert seen == ["thesis-radar me@example.com"] * 2
    assert sleeps == [pytest.approx(0.11)]
