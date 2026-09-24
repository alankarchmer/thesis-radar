"""The real pipeline in a real browser: `radar run` with a fake judge, then the dashboard it wrote
(static mode) and the live `radar serve` server, driven from the keyboard.

Unlike test_dashboard_ui.py (which renders the hand-written fixture), these catch any mismatch
between the payload the backend actually builds and what the page expects.
"""

from __future__ import annotations

import io
import json
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from helpers import write_thesis
from test_cli import CALL, NOTE, WEATHER, responder
from test_dashboard_ui import VERDICT_LABELS, _chromium_executable, focus_first, goto_section, goto_tab

from thesis_radar.app import App
from thesis_radar.cli import main
from thesis_radar.config import Workspace
from thesis_radar.judge import FakeJudge
from thesis_radar.serve import make_server

sync_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.ui
TODAY = date.today()
VERDICTS = set(VERDICT_LABELS)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            launched = pw.chromium.launch()
        except Exception:
            executable = _chromium_executable()
            if executable is None:
                pytest.skip("no Chromium build available for Playwright")
            launched = pw.chromium.launch(executable_path=executable)
        yield launched
        launched.close()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "research"
    (root / "inbox").mkdir(parents=True)
    write_thesis(root / "thesis")
    today = TODAY.strftime("%B %d, %Y").replace(" 0", " ")
    (root / "inbox" / "acme-call.txt").write_text(CALL.replace("September 15, 2026", today), encoding="utf-8")
    (root / "inbox" / "acme-note.md").write_text(NOTE.replace("September 20, 2026", today), encoding="utf-8")
    (root / "inbox" / "weather.txt").write_text(WEATHER, encoding="utf-8")
    code, _, err = cli(["--workspace", str(root), "run"], judge_factory=lambda: FakeJudge(responder))
    assert code == 0, err
    return root


def cli(argv, **kwargs):
    """Run `radar` on a worker thread: Playwright's sync API owns this thread's event loop."""
    out, err = io.StringIO(), io.StringIO()
    with ThreadPoolExecutor(1) as pool:
        code = pool.submit(main, argv, out=out, err=err, today=TODAY, **kwargs).result()
    return code, out.getvalue(), err.getvalue()


def open_tab(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    return context, page, errors


def test_static_dashboard_from_the_real_pipeline(browser, workspace):
    context, page, errors = open_tab(browser)
    page.goto((workspace / "dashboard.html").as_uri())
    page.wait_for_selector("#app > *")
    # The portfolio board has a verdict for every assumption the real thesis defines.
    board = page.locator('.board-card[data-ticker="ACME"]')
    assert board.locator(".score-row").count() >= 1
    assert all(
        v in VERDICTS for v in board.locator(".score-row .verdict").evaluate_all("ns => ns.map(n => n.dataset.verdict)")
    )
    for key in ["ACME", "unsorted", "overview", "ACME"]:
        goto_tab(page, key)
    for section in ["thesis", "metrics", "signals", "filings", "read"]:
        goto_section(page, section)
        assert page.url.endswith(f"#ACME/{section}")
    assert page.locator('[data-count="whats_new"]').first.text_content().strip() == "1"
    card = page.locator('.card[data-section="new"]').first
    assert card.locator(".src").is_visible() and card.locator(".more").is_visible()
    assert page.get_by_text("We cut promotions sharply in September.").first.is_visible()
    pid = focus_first(page, "new")
    page.keyboard.press("d")
    argv = shlex.split(page.locator("#apply-command").text_content())
    actions = json.loads(argv[2])
    assert {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"}.items() <= actions[0].items()
    # The queued command applies cleanly to the real workspace.
    code, _, err = cli(["--workspace", str(workspace), "apply", argv[2]])
    assert code == 0, err
    app = App.open(Workspace(workspace), today=TODAY)
    assert app.store.triage_map("ACME")[pid]["status"] == "dismissed"
    assert {row["question"] for row in app.store.labels("ACME")} >= {"new_info", "whats_new"}
    app.close()
    assert errors == []
    context.close()


def test_serve_round_trip_in_the_browser(browser, workspace):
    app = App.open(Workspace(workspace), today=TODAY)
    server, _token = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    context, page, errors = open_tab(browser)
    try:
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        page.wait_for_selector("#app > *")
        goto_tab(page, "ACME")
        pid = focus_first(page, "new")
        with page.expect_response(lambda r: r.url.endswith("/api/actions")) as response:
            page.keyboard.press("s")
        assert response.value.status == 200
        page.keyboard.press("d")
        page.wait_for_function("() => !document.querySelector('.card.pending')", timeout=5000)
        page.wait_for_timeout(300)
        state = app.store.triage_map("ACME")[pid]
        assert state == {"status": "dismissed", "starred": True}
        # Skim the document of a contradiction via the server's document route.
        contradiction = focus_first(page, "contra")
        assert contradiction
        page.keyboard.press("v")
        page.wait_for_selector(".skim")
        assert errors == []
    finally:
        context.close()
        server.shutdown()
        server.server_close()
        app.close()


def test_metrics_view_from_the_real_pipeline(browser, tmp_path):
    from helpers import ACME_THESIS
    from test_metrics import METRICS, Q2, Q3, number_answers
    from test_metrics import NOTE as ESTIMATE

    root = tmp_path / "research"
    (root / "inbox").mkdir(parents=True)
    write_thesis(root / "thesis", text=ACME_THESIS + METRICS)
    (root / "inbox" / "q2.txt").write_text(f"ACME second quarter release\nJuly 21, 2026\n\n{Q2}", encoding="utf-8")
    (root / "inbox" / "note.txt").write_text(f"ACME preview\nSeptember 18, 2026\n\n{ESTIMATE}", encoding="utf-8")
    (root / "inbox" / "q3.txt").write_text(f"ACME third quarter release\nOctober 20, 2026\n\n{Q3}", encoding="utf-8")

    def respond(request):
        return number_answers(request) if "numbers" in request.state else responder(request)

    code, out, err = cli(["--workspace", str(root), "run"], judge_factory=lambda: FakeJudge(respond))
    assert code == 0, err
    assert "metrics:" in out
    context, page, errors = open_tab(browser)
    page.goto((root / "dashboard.html").as_uri())
    page.wait_for_selector("#app > *")
    assert "Gross margin" in page.locator('.board-card[data-ticker="ACME"]').text_content()
    goto_tab(page, "ACME")
    goto_section(page, "metrics")
    margin = page.locator('.metric-card[data-metric="gross_margin"]')
    assert margin.locator(".metric-value").text_content().strip() == "20.6%"
    assert "Down 1.2 pts" in margin.text_content()
    # The analyst's estimate is placed in Q3 despite the note's "September 18, 2026" dateline.
    assert "Missed estimates" in margin.text_content()
    assert margin.locator("svg").count() >= 1
    margin.get_by_text("Show as table").click()
    table = margin.locator("table")
    assert "Q3 2026" in table.text_content() and "20.6%" in table.text_content()
    # Every number opens to the sentence it came from.
    cell = table.locator("details.qnum", has_text="20.6%").first
    cell.locator("summary").click()
    assert cell.locator(".qtext").is_visible()
    assert "Gross margin was 20.6% in the third quarter." in cell.locator(".qtext").text_content()
    revenue = page.locator('.metric-card[data-metric="revenue"]')
    assert "1,920" in revenue.locator(".metric-value").text_content()
    assert revenue.locator(".chart-key").text_content() == "Line: reported"  # no guidance or estimates to explain
    assert "FY 2026 guidance cut" in margin.text_content()
    inventory = page.locator('.metric-card[data-metric="dealer_inventory"]')
    assert [t.strip() for t in inventory.locator(".ytick").all_text_contents()] == ["35K", "40K", "45K", "50K"]
    assert errors == []
    context.close()
