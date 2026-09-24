"""Browser tests of the dashboard template rendered with tests/fixtures/sample_payload.json.

Run with Playwright's sync API against Chromium; skipped when playwright is not installed.
The template is rendered here without importing the package: the data marker is replaced
with script-safe JSON exactly as `dashboard.script_safe_json` does.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.ui

TESTS = Path(__file__).resolve().parent
TEMPLATE = TESTS.parent / "thesis_radar" / "dashboard_template.html"
SAMPLE = json.loads((TESTS / "fixtures" / "sample_payload.json").read_text(encoding="utf-8"))
MARKER = "__RADAR_DATA__"
SERVE_URL = "http://127.0.0.1:8765/"
SCRIPT_TEXT = "</script><script>alert(1)</script>"


def render(payload) -> str:
    data = (
        json.dumps(payload, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    template = TEMPLATE.read_text(encoding="utf-8")
    assert template.count(MARKER) == 1
    return template.replace(MARKER, data)


def _chromium_executable() -> str | None:
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH"), "/opt/pw-browsers", str(Path.home() / ".cache" / "ms-playwright")]
    patterns = ["chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome",
                "chromium_headless_shell-*/chrome-linux/headless_shell"]
    for root in filter(None, roots):
        for pattern in patterns:
            hits = sorted(glob.glob(os.path.join(root, pattern)), reverse=True)
            if hits:
                return hits[0]
    return None


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
def open_page(browser, tmp_path):
    contexts = []

    def _open(payload=None, *, viewport=None, init_script=None, serve_routes=None):
        payload = SAMPLE if payload is None else payload
        context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900})
        contexts.append(context)
        page = context.new_page()
        tab = SimpleNamespace(page=page, errors=[], dialogs=[])
        page.on("console", lambda m: tab.errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: tab.errors.append(str(e)))
        page.on("dialog", lambda d: (tab.dialogs.append(d.message), d.dismiss()))
        if init_script:
            page.add_init_script(init_script)
        html = render(payload)
        if payload.get("mode") == "serve":
            page.route(SERVE_URL, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
            for pattern, handler in (serve_routes or {}).items():
                page.route(pattern, handler)
            page.goto(SERVE_URL)
        else:
            path = tmp_path / f"dashboard{len(contexts)}.html"
            path.write_text(html, encoding="utf-8")
            page.goto(path.as_uri())
        page.wait_for_selector("#app > *")
        return tab

    yield _open
    for context in contexts:
        context.close()


# ---------------------------------------------------------------- expectations from the fixture


def company(ticker):
    return next(c for c in SAMPLE["companies"] if c["ticker"] == ticker)


def pdate(p):
    return p["date"] or p["ingested_at"][:10]


def newest(items):
    return sorted(items, key=lambda p: (pdate(p), p["id"]), reverse=True)


def feed_order(c, key="in_whats_new"):
    pillars = list(c["pillars"])
    items = newest([p for p in c["passages"] if p["classified"][key]])
    return sorted(items, key=lambda p: pillars.index(p["p"]["pillar"]))


def serve_payload(token="tok-123"):
    payload = copy.deepcopy(SAMPLE)
    payload["mode"] = "serve"
    payload["serve"] = {"token": token}
    return payload


# ---------------------------------------------------------------- page helpers


def goto_tab(page, key):
    page.click(f'nav#tabs [data-tab="{key}"]')
    page.wait_for_selector(f'nav#tabs [data-tab="{key}"].active')


def open_fold(page, key):
    page.click(f'[data-fold="{key}"] > summary')
    page.locator(f'[data-fold="{key}"][open] > :not(summary)').first.wait_for()
    return page.locator(f'[data-fold="{key}"]')


def focus_first(page, section):
    for _ in range(80):
        page.keyboard.press("j")
        focused = page.locator(".card.focused")
        if focused.get_attribute("data-section") == section:
            return int(focused.get_attribute("data-id"))
    raise AssertionError(f"no {section} card reachable with j")


def queued(page):
    argv = shlex.split(page.locator("#apply-command").text_content())
    assert argv[:2] == ["radar", "apply"] and len(argv) == 3
    return json.loads(argv[2])


def new_cards(page):
    return page.locator('.feed[data-feed="new"] > .card')


def whats_new_count(page):
    return int(page.locator('[data-count="whats_new"]').text_content())


# ---------------------------------------------------------------- tests


def test_page_loads_every_view_without_console_errors(open_page):
    tab = open_page()
    page = tab.page
    assert page.title() == "thesis-radar"
    for key in ["ACME", "BRRR", "unsorted", "overview"]:
        goto_tab(page, key)
    page.click("#btn-thresholds")
    page.keyboard.press("?")
    page.wait_for_selector(".sheet.help")
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()
    banner = page.locator(".banner").text_content()
    assert "10 stale judgments shown from before your last thesis edit" in banner and "radar judge --all" in banner
    assert "2 passages not judged yet" in banner and "1 judgment failed" in banner
    assert page.locator('nav#tabs [data-tab="unsorted"]').text_content() == "Unsorted (2)"
    assert tab.errors == [] and tab.dialogs == []


def test_overview_heatmap_sparklines_and_flagged_divergence(open_page):
    page = open_page().page
    acme = page.locator('.ov[data-ticker="ACME"]')
    cells = [cell for row in company("ACME")["heatmap"]["rows"].values() for cell in row if cell and cell["count"]]
    assert acme.locator(".hm-cell").count() == len(cells) > 0
    title = acme.locator(".hm-cell").first.get_attribute("title")
    assert re.search(r"· 2026-W\d\d \(week of 2026-\d\d-\d\d\) · net [+−]?\d\.\d\d · \d+ passages?$", title), title
    labels = [t for t in acme.locator(".hm .wk").all_text_contents() if t]
    weeks = company("ACME")["heatmap"]["weeks"]
    assert labels == ["W" + w[-2:] for w in weeks[(len(weeks) - 1) % 4::4]] and labels[-1] == "W39"
    assert acme.locator("svg.spark").count() == len(company("ACME")["assumptions"])
    assert acme.locator("svg.spark polyline").count() == len(company("ACME")["assumptions"])
    [d] = [d for d in company("ACME")["divergence"] if d["flagged"]]
    flagged = acme.locator(".divergence .div-row.flagged")
    assert flagged.count() == 1 and flagged.get_attribute("data-pillar") == d["pillar"]
    assert "diverges" in flagged.text_content() and f"gap +{d['gap']:.1f}" in flagged.text_content()
    flagged.click()
    shown = page.locator(".sheet .card").evaluate_all("nodes => nodes.map(n => Number(n.dataset.id))")
    assert shown == d["inside"]["ids"] + d["outside"]["ids"]
    page.keyboard.press("Escape")
    acme.locator(".hm-cell").first.click()
    assert page.locator(".sheet.modal").is_visible()
    assert "1 kept · 2 missed · 2 open · credibility 33%" in acme.text_content()
    assert "Brier 0.125" in acme.text_content() or "Brier score 0.125" in acme.text_content()


def test_contradictions_are_grouped_by_assumption(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    c = company("ACME")
    order = [a["id"] for a in c["assumptions"]]
    expected = {}
    for p in newest([p for p in c["passages"] if p["classified"]["in_contradictions"]]):
        first = next(a for a in order if a in p["classified"]["contradicts"])
        expected.setdefault(first, []).append(p["id"])
    groups = page.locator(".contra-group")
    assert groups.evaluate_all("gs => gs.map(g => g.dataset.assumption)") == sorted(expected, key=order.index)
    for i in range(groups.count()):
        group = groups.nth(i)
        aid = group.get_attribute("data-assumption")
        statement = next(a["statement"] for a in c["assumptions"] if a["id"] == aid)
        assert statement in group.locator("h3").text_content()
        assert group.locator(".card.contra").evaluate_all("ns => ns.map(n => Number(n.dataset.id))") == expected[aid]
    assert "Acknowledged (1)" in page.locator("#contradictions").text_content()


def test_whats_new_count_matches_the_classification(open_page):
    page = open_page().page
    c = company("ACME")
    expected = sum(p["classified"]["in_whats_new"] for p in c["passages"])
    assert expected >= 10
    assert page.locator('nav#tabs [data-tab="ACME"] .badge').text_content() == str(expected)
    goto_tab(page, "ACME")
    assert whats_new_count(page) == expected
    ids = page.locator('.feed[data-feed="new"] > .card:not(.spot)').evaluate_all("ns => ns.map(n => Number(n.dataset.id))")
    assert ids == [p["id"] for p in feed_order(c)]


def test_spot_check_takes_position_ten_and_records_a_label(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    cards = new_cards(page)
    tenth = cards.nth(9)
    assert "spot" in tenth.get_attribute("class")
    assert int(tenth.get_attribute("data-id")) == company("ACME")["spot_checks"][0]
    assert "Spot check: should this have been in your feed?" in tenth.text_content()
    assert all("spot" not in (cards.nth(i).get_attribute("class")) for i in range(9))
    tenth.click()
    page.keyboard.press("y")
    assert queued(page) == [{"op": "label", "ticker": "ACME", "passage_id": 1073, "question": "whats_new", "value": 1,
                             "origin": "spotcheck"}]
    assert int(new_cards(page).nth(9).get_attribute("data-id")) == company("ACME")["spot_checks"][1]


def test_j_then_d_dismisses_with_labels_and_u_undoes(open_page):
    tab = open_page()
    page = tab.page
    goto_tab(page, "ACME")
    before = whats_new_count(page)
    pid = focus_first(page, "new")
    assert pid == feed_order(company("ACME"))[0]["id"]
    page.keyboard.press("d")
    assert page.locator(f'.card[data-section="new"][data-id="{pid}"]').count() == 0
    assert whats_new_count(page) == before - 1
    assert page.locator(".card.focused").get_attribute("data-section") == "new"
    assert queued(page) == [
        {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "new_info", "value": 0, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "whats_new", "value": 0, "origin": "triage"},
    ]
    page.keyboard.press("u")
    assert page.locator(f'.card[data-section="new"][data-id="{pid}"]').count() == 1
    assert whats_new_count(page) == before
    assert page.locator("#actionbar").is_hidden()
    assert tab.errors == []


def test_static_queue_survives_a_reload(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    pid = focus_first(page, "new")
    page.keyboard.press("x")
    labels = [a["question"] for a in queued(page) if a["op"] == "label"]
    assert labels == ["material", "whats_new"]
    page.reload()
    goto_tab(page, "ACME")
    assert len(queued(page)) == 3
    assert page.locator(f'.card[data-section="new"][data-id="{pid}"]').count() == 0
    page.click("#actionbar >> text=Clear")
    assert page.locator("#actionbar").is_hidden()


def test_storage_that_throws_does_not_break_the_queue(open_page):
    tab = open_page(init_script="Object.defineProperty(window, 'localStorage', "
                                "{configurable: true, get() { throw new Error('storage disabled'); }});")
    page = tab.page
    goto_tab(page, "ACME")
    focus_first(page, "new")
    page.keyboard.press("d")
    assert len(queued(page)) == 3
    assert tab.errors == []


def test_star_contradiction_and_absorb_actions(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    pid = focus_first(page, "contra")
    page.keyboard.press("c")
    assert queued(page)[-2:] == [
        {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "acknowledged"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "contradicts__inv_normalizes", "value": 1,
         "origin": "triage"},
    ]
    assert "Acknowledged (2)" in page.locator("#contradictions").text_content()
    page.locator('.card[data-section="new"][data-id="1044"]').click()
    page.keyboard.press("s")
    assert queued(page)[-3:] == [
        {"op": "triage", "ticker": "ACME", "passage_id": 1044, "starred": True},
        {"op": "label", "ticker": "ACME", "passage_id": 1044, "question": "material", "value": 1, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": 1044, "question": "whats_new", "value": 1, "origin": "triage"},
    ]
    page.keyboard.press("a")
    field = page.locator('[data-fkey="absorb-text"]')
    assert field.evaluate("n => n === document.activeElement")
    assert page.locator("form.absorb").locator("text=replace margins.0").is_visible()
    field.type("Gross margin 20.4% in Q3, up 60 bps sequentially.")
    page.keyboard.press("j")
    assert field.input_value().endswith("j")
    field.press("Backspace")
    field.press("Enter")
    assert queued(page)[-5:] == [
        {"op": "triage", "ticker": "ACME", "passage_id": 1044, "status": "absorbed"},
        {"op": "label", "ticker": "ACME", "passage_id": 1044, "question": "new_info", "value": 1, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": 1044, "question": "material", "value": 1, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": 1044, "question": "whats_new", "value": 1, "origin": "triage"},
        {"op": "fact", "ticker": "ACME", "pillar": "margins", "text": "Gross margin 20.4% in Q3, up 60 bps sequentially.",
         "source": 1044, "replace": "margins.0"},
    ]
    assert page.locator('.card[data-section="new"][data-id="1044"]').count() == 0
    facts = open_fold(page, "facts|ACME")
    assert "Gross margin 20.4% in Q3, up 60 bps sequentially." in facts.text_content()
    assert "Gross margin 19.8% in Q2, down 240 bps." not in facts.text_content()


def test_mark_all_shown_as_read_asks_first_and_sends_no_labels(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    page.select_option('select[aria-label="Filter by source"]', "sell_side")
    shown = page.locator('.feed[data-feed="new"] > .card:not(.spot)').count()
    assert 0 < shown < whats_new_count(page)
    page.click("text=Mark all shown as read")
    assert page.locator("#actionbar").is_hidden()
    page.click(f"text=Dismiss {shown}")
    actions = queued(page)
    assert len(actions) == shown and all(a["op"] == "triage" and a["status"] == "dismissed" for a in actions)
    page.keyboard.press("u")
    assert page.locator("#actionbar").is_hidden()


def test_threshold_slider_updates_live_counts(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    c = company("ACME")
    before = whats_new_count(page)
    after = before + sum(p["classified"]["in_maybe"] for p in c["passages"])
    page.click("#btn-thresholds")
    page.eval_on_selector('input[data-policy="new_info_min"]',
                          "n => { n.value = '0.4'; n.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.wait_for_function(f"document.querySelector('[data-count=\"whats_new\"]').textContent === '{after}'")
    assert page.locator('#th-counts [data-ticker="ACME"] [data-live="whats_new"]').text_content() == f"What's new {before} → {after}"
    assert page.locator('nav#tabs [data-tab="ACME"] .badge').text_content() == str(after)
    page.click("text=Reset to saved")
    assert whats_new_count(page) == before
    page.eval_on_selector('input[data-policy="whats_new_window_days"]',
                          "n => { n.value = '1'; n.dispatchEvent(new Event('input', {bubbles: true})); }")
    page.wait_for_function("document.querySelector('[data-count=\"whats_new\"]').textContent === '0'")
    page.click("text=Reset to defaults")
    assert whats_new_count(page) == before


def test_skim_mode_opens_with_v_and_n_jumps(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    pid = focus_first(page, "new")
    page.keyboard.press("v")
    sheet = page.locator(".sheet.skim")
    sheet.wait_for()
    assert "Dealer visit, Duluth" in sheet.locator(".sheet-head").text_content()
    assert int(sheet.locator(".skim-current").get_attribute("data-id")) == pid
    highlighted = sheet.locator(".skim-p.hl-new, .skim-p.hl-maybe, .skim-p.contra")
    ids = highlighted.evaluate_all("ns => ns.map(n => Number(n.dataset.id))")
    page.keyboard.press("n")
    assert int(sheet.locator(".skim-current").get_attribute("data-id")) == ids[ids.index(pid) + 1]
    page.keyboard.press("p")
    assert int(sheet.locator(".skim-current").get_attribute("data-id")) == pid
    stub = sheet.locator("button.stub")
    assert stub.count() == 1 and "off thesis" in stub.text_content()
    stub.click()
    assert sheet.locator('.skim-p[data-id="1084"]').is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()


def test_search_finds_passages_and_highlights_matches(open_page):
    page = open_page().page
    page.fill("#search", "volt-x")
    page.press("#search", "Enter")
    expected = sum(1 for c in SAMPLE["companies"] for p in c["passages"]
                   if "volt-x" in ((p["speaker"] or "") + " " + p["text"]).lower())
    results = page.locator('#app .card[data-section="search"]')
    results.first.wait_for()
    assert results.count() == expected > 0
    assert set(page.locator("#app .card mark").all_text_contents()) == {"Volt-X"}
    page.press("#search", "Escape")
    assert page.locator('#app .card[data-section="search"]').count() == 0


def test_passage_text_is_never_interpreted_as_markup(open_page):
    tab = open_page()
    page = tab.page
    goto_tab(page, "ACME")
    assert page.get_by_text(SCRIPT_TEXT, exact=True).is_visible()
    page.fill("#search", "throttle")
    page.press("#search", "Enter")
    page.locator('#app .card[data-section="search"]').first.wait_for()
    assert page.locator('a[href^="javascript"]').count() == 0
    hrefs = page.locator("a[href]").evaluate_all("ns => ns.map(n => n.getAttribute('href'))")
    goto_tab(page, "ACME")
    hrefs += page.locator("a[href]").evaluate_all("ns => ns.map(n => n.getAttribute('href'))")
    assert hrefs and all(h.startswith("file:") for h in hrefs)
    assert tab.dialogs == [] and tab.errors == []


def test_predictions_resolve_and_quotes_copy(open_page):
    page = open_page(init_script="window.__copied = []; Object.defineProperty(navigator, 'clipboard', {configurable: true, "
                                 "value: {writeText: (t) => { window.__copied.push(t); return Promise.resolve(); }}});").page
    goto_tab(page, "ACME")
    open_fold(page, "predictions|ACME")
    page.click('[data-prediction="inv_normal_by_q1"] >> button[aria-label="Resolve yes"]')
    assert queued(page) == [{"op": "resolve", "ticker": "ACME", "prediction_id": "inv_normal_by_q1", "outcome": True}]
    assert "Brier 0.117 over 3 resolved" in page.locator('[data-fold="predictions|ACME"]').text_content()
    first, second = feed_order(company("ACME"))[:2]
    for p in (first, second):
        page.locator(f'.card[data-section="new"][data-id="{p["id"]}"] input[aria-label="Select for quote export"]').check()
    page.click("text=Copy as Markdown quotes")
    page.wait_for_function("window.__copied.length === 1")
    text = page.evaluate("window.__copied[0]")
    assert text.startswith("> " + first["text"]) and ("> " + second["text"]) in text
    assert f"— {first['title']}, own note, {first['date']}, p. {first['page']}" in text


def test_keyboard_help_and_tab_switching(open_page):
    page = open_page().page
    page.keyboard.press("g")
    page.keyboard.press("2")
    assert page.locator('nav#tabs [data-tab="BRRR"]').get_attribute("aria-selected") == "true"
    page.keyboard.press("g")
    page.keyboard.press("o")
    assert page.locator('nav#tabs [data-tab="overview"]').get_attribute("aria-selected") == "true"
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "search"
    page.keyboard.press("j")
    assert page.input_value("#search") == "j"


@pytest.mark.parametrize("width", [360, 414])
def test_phone_width_has_no_horizontal_page_scroll(open_page, width):
    page = open_page(viewport={"width": width, "height": 800}).page
    overflow = "document.documentElement.scrollWidth - window.innerWidth"
    assert page.evaluate(overflow) <= 0
    goto_tab(page, "ACME")
    for _ in range(40):
        closed = page.locator("details.fold:not([open]) > summary")
        if not closed.count():
            break
        closed.first.click()
    focus_first(page, "new")
    page.keyboard.press("d")
    assert page.evaluate(overflow) <= 0
    page.keyboard.press("j")
    page.keyboard.press("v")
    page.locator(".sheet.skim").wait_for()
    assert page.evaluate("document.querySelector('#overlay').scrollWidth - window.innerWidth") <= 0


# ---------------------------------------------------------------- serve mode


def test_serve_mode_posts_actions_with_the_token(open_page):
    requests = []

    def api(route):
        requests.append(route.request)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "results": []}))

    page = open_page(serve_payload(), serve_routes={"**/api/actions": api}).page
    goto_tab(page, "ACME")
    pid = focus_first(page, "new")
    with page.expect_request("**/api/actions"):
        page.keyboard.press("d")
    page.wait_for_selector("#actionbar", state="hidden")
    [request] = requests
    assert request.method == "POST"
    assert request.headers["x-radar-token"] == "tok-123"
    assert request.headers["content-type"].startswith("application/json")
    assert request.post_data_json == {"actions": [
        {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "new_info", "value": 0, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "whats_new", "value": 0, "origin": "triage"},
    ]}
    with page.expect_request("**/api/actions"):
        page.keyboard.press("u")
    assert requests[-1].post_data_json == {"actions": [
        {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": None, "starred": False}]}
    assert page.locator(f'.card[data-section="new"][data-id="{pid}"]').count() == 1
    page.click("#btn-thresholds")
    with page.expect_request("**/api/actions"):
        page.click("#thresholds >> text=Save")
    [action] = requests[-1].post_data_json["actions"]
    assert action["op"] == "policy" and action["values"]["new_info_min"] == 0.6


def test_serve_mode_failure_keeps_actions_queued(open_page):
    state = {"fail": True}

    def api(route):
        if state["fail"]:
            route.fulfill(status=500, body="boom")
        else:
            route.fulfill(status=200, content_type="application/json", body='{"ok": true, "results": []}')

    page = open_page(serve_payload(), serve_routes={"**/api/actions": api}).page
    goto_tab(page, "ACME")
    pid = focus_first(page, "new")
    page.keyboard.press("x")
    page.locator(".toast.error").wait_for()
    assert "3 unsaved actions" in page.locator("#actionbar").text_content()
    assert page.locator(f'.card[data-section="new"][data-id="{pid}"]').count() == 0
    state["fail"] = False
    page.click("#actionbar >> text=Retry")
    page.wait_for_selector("#actionbar", state="hidden")


def test_serve_mode_search_and_full_document(open_page):
    acme = company("ACME")
    by_id = {p["id"]: p for p in acme["passages"]}
    seen = {}

    def search(route):
        seen["search"] = route.request
        body = {"results": [{"passage": by_id[1033], "snippet": "normalize"}]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def document(route):
        seen["document"] = route.request
        doc = next(d for d in acme["documents"] if d["id"] == 103)
        extra = dict(by_id[1031], id=9999, seq=99, text="An unjudged passage from the full filing.", p=None,
                     status="unjudged", classified=None)
        body = {"document": dict(doc, complete=True), "passages": [by_id[i] for i in (1031, 1033, 1034, 1035)] + [extra]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page = open_page(serve_payload(), serve_routes={re.compile(r".*/api/search\?.*"): search,
                                                    re.compile(r".*/api/document/103\?ticker=ACME$"): document}).page
    goto_tab(page, "ACME")
    page.fill("#search", "normalize")
    page.press("#search", "Enter")
    page.locator('#app .card[data-section="search"]').first.wait_for()
    request = seen["search"]
    assert request.headers["x-radar-token"] == "tok-123"
    assert re.search(r"/api/search\?q=normalize&ticker=ACME&limit=50$", request.url)
    focus_first(page, "search")
    page.keyboard.press("v")
    sheet = page.locator(".sheet.skim")
    sheet.locator('[data-id="9999"]').wait_for()
    assert seen["document"].headers["x-radar-token"] == "tok-123"
    assert sheet.locator("[data-id]").count() == 5
    assert "Only on-thesis passages are embedded" not in sheet.text_content()
