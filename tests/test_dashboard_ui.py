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
CLIPBOARD = (
    "window.__copied = []; Object.defineProperty(navigator, 'clipboard', {configurable: true, "
    "value: {writeText: (t) => { window.__copied.push(t); return Promise.resolve(); }}});"
)
EXPECTED_VERDICTS = {
    ("ACME", "inv_normalizes"): "contradicted",
    ("ACME", "pricing_holds"): "holding",
    ("ACME", "margin_recovers"): "turning_against",
    ("ACME", "ev_on_track"): "turning_for",
    ("BRRR", "share_gains"): "mixed",
    ("BRRR", "lean_channel"): "holding",
    ("BRRR", "dealer_adds"): "quiet",
}
VERDICT_LABELS = {
    "holding": "Holding",
    "contradicted": "Contradicted",
    "mixed": "Mixed",
    "turning_for": "Turning toward support",
    "turning_against": "Turning against",
    "quiet": "Quiet",
}


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
    roots = [
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
        "/opt/pw-browsers",
        str(Path.home() / ".cache" / "ms-playwright"),
    ]
    patterns = [
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ]
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

    def _open(payload=None, *, viewport=None, init_script=None, serve_routes=None, hash=""):
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
            page.route(
                SERVE_URL, lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            )
            for pattern, handler in (serve_routes or {}).items():
                page.route(pattern, handler)
            page.goto(SERVE_URL + hash)
        else:
            path = tmp_path / f"dashboard{len(contexts)}.html"
            path.write_text(html, encoding="utf-8")
            page.goto(path.as_uri() + hash)
        page.wait_for_selector("#app > *")
        return tab

    yield _open
    for context in contexts:
        context.close()


# ---------------------------------------------------------------- expectations from the fixture


def company(ticker):
    return next(c for c in SAMPLE["companies"] if c["ticker"] == ticker)


def passage(ticker, pid):
    return next(p for p in company(ticker)["passages"] if p["id"] == pid)


def pdate(p):
    return p["date"] or p["ingested_at"][:10]


def newest(items):
    return sorted(items, key=lambda p: (pdate(p), p["id"]), reverse=True)


def visible_feed(c, expanded=()):
    """What's new as shown: pillars in thesis order, newest first, three per pillar unless expanded."""
    out = []
    for pillar in c["pillars"]:
        items = newest([p for p in c["passages"] if p["classified"]["in_whats_new"] and p["p"]["pillar"] == pillar])
        out += items if pillar in expanded else items[:3]
    return [p["id"] for p in out]


def serve_payload(token="tok-123"):
    payload = copy.deepcopy(SAMPLE)
    payload["mode"] = "serve"
    payload["serve"] = {"token": token}
    return payload


# ---------------------------------------------------------------- page helpers


def goto_tab(page, key):
    page.click(f'nav#tabs [data-tab="{key}"]')
    page.wait_for_selector(f'nav#tabs [data-tab="{key}"].active')


def goto_section(page, section):
    page.click(f'nav#sections [data-section-tab="{section}"]')
    page.wait_for_selector(f'nav#sections [data-section-tab="{section}"].active')


def active_section(page):
    return page.locator("nav#sections .sec.active").get_attribute("data-section-tab")


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


def ids_of(locator):
    return locator.evaluate_all("ns => ns.map(n => Number(n.dataset.id))")


def whats_new_count(page):
    return int(page.locator('[data-count="whats_new"]').text_content())


def feed_card(page, pid, section="new"):
    return page.locator(f'.card[data-section="{section}"][data-id="{pid}"]')


def open_menu(card):
    card.locator(".more").click()
    card.locator(".menu").wait_for()
    return card.locator(".menu")


# ---------------------------------------------------------------- page, header, overview


def test_page_loads_every_view_without_console_errors(open_page):
    tab = open_page()
    page = tab.page
    assert page.title() == "thesis-radar"
    for key in ["ACME", "BRRR", "unsorted", "overview"]:
        goto_tab(page, key)
    goto_tab(page, "ACME")
    for section in ["thesis", "metrics", "signals", "filings", "read"]:
        goto_section(page, section)
    goto_tab(page, "BRRR")
    goto_section(page, "metrics")
    empty = page.locator("#metrics").text_content()
    assert "No metrics tracked yet" in empty and "the next radar run" in empty.lower()
    assert 'gross_margin: {label: "Gross margin", unit: "%", pillar: margins, higher_is: good}' in empty
    page.click("#btn-thresholds")
    page.keyboard.press("?")
    page.wait_for_selector(".sheet.help")
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()
    assert (
        page.locator("#status-line").text_content() == "Updated Sep 24, 8:00 UTC · 3 new passages since your last visit"
    )
    assert page.locator('nav#tabs [data-tab="unsorted"]').text_content() == "Unsorted (2)"
    goto_tab(page, "unsorted")
    assert page.locator('#app a[href^="file:"]', has_text="Open file").count() == 2
    assert tab.errors == [] and tab.dialogs == []


def test_status_chip_opens_the_pipeline_panel(open_page):
    page = open_page().page
    pending = SAMPLE["pending"]
    chip = page.locator("#status-chip")
    assert chip.is_visible() and chip.text_content() == f"⚠{sum(pending.values())} need attention"
    panel = page.locator("#status-panel")
    assert panel.is_hidden()
    chip.click()
    text = panel.text_content()
    assert f"{pending['stale']} stale judgments" in text and "radar judge --all" in text
    assert "2 passages not judged yet" in text and "1 judgment failed" in text and "radar judge --retry-failed" in text
    page.keyboard.press("Escape")
    assert panel.is_hidden()
    quiet = copy.deepcopy(SAMPLE)
    quiet["pending"] = {"unjudged": 0, "failed": 0, "stale": 0}
    assert open_page(quiet).page.locator("#status-chip").is_hidden()


def test_overview_board_scorecard_read_next_metrics_and_signals(open_page):
    page = open_page().page
    for c in SAMPLE["companies"]:
        board = page.locator(f'.board-card[data-ticker="{c["ticker"]}"]')
        assert board.locator(".score-row").count() == len(c["assumptions"])
        for a in c["assumptions"]:
            row = board.locator(f'.score-row[data-assumption="{a["id"]}"]')
            kind = EXPECTED_VERDICTS[(c["ticker"], a["id"])]
            assert a["statement"] in row.text_content() and a["id"] not in row.text_content()
            assert row.locator(".verdict").get_attribute("data-verdict") == kind
            assert row.locator(".verdict").text_content().endswith(VERDICT_LABELS[kind])
            assert row.locator("svg.tide rect.hit").count() == 12
    acme = page.locator('.board-card[data-ticker="ACME"]')
    assert "Based on 1 passage in the last six months" in acme.locator('[data-assumption="ev_on_track"]').text_content()
    assert "Based on" not in acme.locator('[data-assumption="inv_normalizes"]').text_content()
    brrr = page.locator('.board-card[data-ticker="BRRR"]')
    assert "No evidence in the last six months" in brrr.locator('[data-assumption="dealer_adds"]').text_content()
    assert "Nothing new since your last visit" in brrr.locator(".read-next").text_content()
    c = company("ACME")
    counts = [sum(p["classified"][k] for p in c["passages"]) for k in ("in_contradictions", "in_whats_new")]
    assert acme.locator(".read-next").text_content() == (
        f"Read next{counts[0]} contradictions{counts[1]} new passages3 since your last visit"
    )
    assert ids_of(acme.locator(".next-item")) == [1061, 1041, 1046]
    assert acme.locator(".metric-line").all_text_contents() == [
        "▲Gross margin 20.4% in Q3 2026, up 0.6 pts; within guidance; missed estimates; guidance cut",
        "▲Dealer inventory, change year over year 22% in Q3 2026, up 4 pts; above estimates",
        "▼North American retail sales, change year over year −6% in Q3 2026, down 1 pt; below guidance",
    ]
    assert acme.locator(".signals li").all_text_contents() == [
        "⚑Dealer inventory: management sounds neutral; channel checks and experts sound negative (4 passages each).",
        "◆Management kept 1 of 3 promises checked so far; 2 are still open.",
        "◷Due Sep 30: ACME announces Volt-X retail pricing before October 1 — you said 50%.",
        "◷Overdue since Sep 15: ACME announces a cut to promotional spending on the Q3 call — you said 35%.",
        "◎Your forecasts: 2 resolved, average error 0.13 — 0 is perfect, 0.25 is a coin flip.",
    ]
    assert page.locator(".hm-cell").count() == 0 and "#10" not in page.locator("#app").inner_text()
    howto = open_fold(page, "howto").text_content()
    assert "last six months" in howto and "not yet checked against your own judgment" in howto
    acme.locator('.next-item[data-id="1041"]').click()
    assert page.url.endswith("#ACME/read")
    assert page.locator(".card.focused").get_attribute("data-id") == "1041"


def test_tide_tooltips_on_hover_and_keyboard_focus(open_page):
    page = open_page().page
    row = page.locator('.board-card[data-ticker="ACME"] .score-row[data-assumption="inv_normalizes"]')
    hits = row.locator("rect.hit")
    tip = page.locator("#tip")
    hits.nth(11).hover()
    assert tip.is_visible() and tip.text_content() == "Sep 2026: 0 for, 2 against"
    page.mouse.move(2, 600)
    assert tip.is_hidden()
    hits.nth(10).focus()
    assert tip.is_visible() and tip.text_content() == "Aug 2026: 1 for, 0 against"
    assert hits.nth(10).get_attribute("aria-label") == "Aug 2026: 1 for, 0 against"
    box = hits.nth(10).bounding_box()
    assert box["width"] >= 24 and box["height"] >= 24
    summary = row.locator(".sr-only").text_content()
    assert summary.startswith("Last 12 months: 2 passages for, 4 against.") and "Sep 2026: 0 for, 2 against" in summary


def test_section_nav_keys_and_hash(open_page):
    tab = open_page()
    page = tab.page
    goto_tab(page, "ACME")
    assert page.url.endswith("#ACME/read")
    assert page.locator("nav#sections .sec").all_text_contents() == [
        "Read23",
        "Thesis4",
        "Metrics3",
        "Signals1",
        "Filings2",
    ]
    goto_section(page, "thesis")
    assert page.url.endswith("#ACME/thesis")
    page.reload()
    page.wait_for_selector("#scorecard")
    assert active_section(page) == "thesis"
    for key, section in [("m", "metrics"), ("s", "signals"), ("f", "filings"), ("r", "read"), ("t", "thesis")]:
        page.keyboard.press("g")
        page.keyboard.press(key)
        page.wait_for_selector(f'nav#sections [data-section-tab="{section}"].active')
        assert page.url.endswith(f"#ACME/{section}")
    page.go_back()
    page.wait_for_selector('nav#sections [data-section-tab="read"].active')
    page.keyboard.press("g")
    page.keyboard.press("o")
    assert page.url.endswith("#overview") and page.locator("nav#sections").is_hidden()
    page.keyboard.press("g")
    page.keyboard.press("2")
    assert page.url.endswith("#BRRR/read")
    direct = open_page(hash="#BRRR/signals").page
    assert active_section(direct) == "signals" and direct.locator("#tone").is_visible()
    assert tab.errors == []


# ---------------------------------------------------------------- Read


def test_contradictions_grouped_by_assumption_most_material_first(open_page):
    payload = copy.deepcopy(SAMPLE)
    extra = next(p for p in payload["companies"][0]["passages"] if p["id"] == 1071)
    extra["p"]["assumptions"]["inv_normalizes"]["contradicts"] = 0.9
    page = open_page(payload).page
    goto_tab(page, "ACME")
    groups = page.locator(".contra-group")
    assert groups.evaluate_all("gs => gs.map(g => g.dataset.assumption)") == [
        "inv_normalizes",
        "pricing_holds",
        "margin_recovers",
    ]
    inv = page.locator('.contra-group[data-assumption="inv_normalizes"]')
    assert inv.locator("h3").text_content().startswith("Dealer inventory returns to normal within two quarters.")
    assert inv.locator("h3 .verdict").get_attribute("data-verdict") == "contradicted"
    assert ids_of(inv.locator(".card.contra")) == [1061, 1041]
    inv.get_by_text("Show 1 more").click()
    assert ids_of(inv.locator(".card.contra")) == [1061, 1041, 1071]
    assert page.locator('[data-count="contradictions"]').text_content() == "5"
    assert "Acknowledged (2)" in page.locator("#contradictions").text_content()


def test_whats_new_groups_three_per_pillar_with_show_more(open_page):
    page = open_page().page
    c = company("ACME")
    expected = sum(p["classified"]["in_whats_new"] for p in c["passages"])
    assert page.locator('nav#tabs [data-tab="ACME"] .badge').text_content() == str(expected)
    goto_tab(page, "ACME")
    assert whats_new_count(page) == expected
    assert ids_of(page.locator('.feed[data-feed="new"] > .card:not(.spot)')) == visible_feed(c)
    heads = page.locator('.feed[data-feed="new"] > .group-head > span:first-child').all_text_contents()
    assert heads == ["Dealer inventory", "Pricing", "Demand", "Margins", "Electric"]
    more = page.locator('[data-more="dealer_inventory"]')
    assert more.text_content() == "Show 2 more in Dealer inventory"
    more.click()
    assert ids_of(page.locator('.feed[data-feed="new"] > .card:not(.spot)')) == visible_feed(c, {"dealer_inventory"})
    assert page.locator('[data-more="dealer_inventory"]').text_content() == "Show fewer in Dealer inventory"


def test_spot_check_takes_position_ten_and_records_a_label(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    cards = new_cards(page)
    tenth = cards.nth(9)
    assert "spot" in tenth.get_attribute("class")
    assert int(tenth.get_attribute("data-id")) == company("ACME")["spot_checks"][0]
    assert "Spot check: should this have been in your feed?" in tenth.text_content()
    assert all("spot" not in cards.nth(i).get_attribute("class") for i in range(9))
    tenth.click()
    page.keyboard.press("y")
    assert queued(page) == [
        {
            "op": "label",
            "ticker": "ACME",
            "passage_id": 1073,
            "question": "whats_new",
            "value": 1,
            "origin": "spotcheck",
        }
    ]
    assert int(new_cards(page).nth(9).get_attribute("data-id")) == company("ACME")["spot_checks"][1]


def test_j_then_d_dismisses_with_labels_and_u_undoes(open_page):
    tab = open_page()
    page = tab.page
    goto_tab(page, "ACME")
    before = whats_new_count(page)
    pid = focus_first(page, "new")
    assert pid == visible_feed(company("ACME"))[0]
    page.keyboard.press("d")
    assert feed_card(page, pid).count() == 0
    assert whats_new_count(page) == before - 1
    assert page.locator(".card.focused").get_attribute("data-section") == "new"
    assert queued(page) == [
        {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "new_info", "value": 0, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "whats_new", "value": 0, "origin": "triage"},
    ]
    page.keyboard.press("u")
    assert feed_card(page, pid).count() == 1
    assert page.locator(".card.focused").get_attribute("data-id") == str(pid)
    assert whats_new_count(page) == before
    assert page.locator("#actionbar").is_hidden()
    assert tab.errors == []


def test_static_queue_survives_a_reload(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    pid = focus_first(page, "new")
    page.keyboard.press("x")
    assert [a["question"] for a in queued(page) if a["op"] == "label"] == ["material", "whats_new"]
    page.reload()
    page.wait_for_selector("#app > *")
    assert len(queued(page)) == 3
    assert feed_card(page, pid).count() == 0
    page.click('#actionbar >> text="Clear"')
    assert page.locator("#actionbar").is_hidden()


def test_queue_left_by_an_earlier_dashboard_is_offered(open_page):
    old = [{"op": "triage", "ticker": "ACME", "passage_id": 1053, "status": "dismissed"}]
    script = f"localStorage.setItem('thesis-radar:queue:2026-09-20T08:00:00Z', {json.dumps(json.dumps(old))!s});"
    page = open_page(init_script=script).page
    row = page.locator("#actionbar .older")
    assert "1 action from the dashboard generated Sep 20, 8:00 UTC was never cleared" in row.text_content()
    assert json.loads(shlex.split(row.locator("code").text_content())[2]) == old
    goto_tab(page, "ACME")
    assert feed_card(page, 1053).count() == 1
    row.locator('text="Discard"').click()
    assert page.locator("#actionbar").is_hidden()
    assert page.evaluate("localStorage.length") == 0


def test_storage_that_throws_does_not_break_the_queue(open_page):
    tab = open_page(
        init_script="Object.defineProperty(window, 'localStorage', "
        "{configurable: true, get() { throw new Error('storage disabled'); }});"
    )
    page = tab.page
    goto_tab(page, "ACME")
    focus_first(page, "new")
    page.keyboard.press("d")
    assert len(queued(page)) == 3
    assert tab.errors == []


def test_false_alarm_updates_the_verdict_and_undo_restores_it(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    chip = page.locator('.contra-group[data-assumption="inv_normalizes"] h3 .verdict')
    assert chip.get_attribute("data-verdict") == "contradicted"
    pid = focus_first(page, "contra")
    assert pid == 1061
    page.keyboard.press("f")
    assert queued(page) == [
        {"op": "triage", "ticker": "ACME", "passage_id": 1061, "status": "acknowledged"},
        {
            "op": "label",
            "ticker": "ACME",
            "passage_id": 1061,
            "question": "contradicts__inv_normalizes",
            "value": 0,
            "origin": "triage",
        },
    ]
    assert chip.get_attribute("data-verdict") == "mixed"
    assert "Acknowledged (3)" in page.locator("#contradictions").text_content()
    page.keyboard.press("u")
    assert chip.get_attribute("data-verdict") == "contradicted"
    pid = focus_first(page, "contra")
    page.keyboard.press("c")
    assert queued(page)[-1] == {
        "op": "label",
        "ticker": "ACME",
        "passage_id": pid,
        "question": "contradicts__inv_normalizes",
        "value": 1,
        "origin": "triage",
    }
    goto_section(page, "thesis")
    assert (
        page.locator('.score-row[data-assumption="inv_normalizes"] .verdict').get_attribute("data-verdict")
        == "contradicted"
    )


def test_card_layout_flags_and_plain_words(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    contra = feed_card(page, 1061, "contra")
    src = contra.locator(".src").text_content()
    assert "Sell-side" in src and "Sep 18, 2026" in src and "Page 1 ↗" in src and "#1061" not in contra.inner_text()
    assert contra.locator(".src").get_attribute("title") == "Passage 1061"
    assert "Against: Dealer inventory returns to normal within two quarters." in contra.locator(".rels").text_content()
    assert "Answers: Will dealers cut fourth-quarter orders?" in contra.locator(".rels").text_content()
    assert contra.locator(".facts > span").all_text_contents() == [
        "Negative for the company",
        "Major",
        "Channel data",
        "New to you 80%",
    ]
    assert contra.locator(".actions > .ghost").all_text_contents() == ["Acknowledge", "False alarm"]
    contra.get_by_text("Earlier version — show changes").click()
    diff = feed_card(page, 1061, "contra").locator(".panel-x")
    assert diff.locator("ins").count() > 0 and diff.locator("del").count() > 0
    updates = feed_card(page, 1041, "contra").locator(".flags").text_content()
    assert "Updates your fact: Dealer inventory up 18% year over year at the end of Q2." in updates
    note = feed_card(page, 1081)
    assert "Your own note — read it as opinion, not evidence." in note.locator(".flags").text_content()
    assert "New since your last visit" in note.locator(".src").text_content()
    assert "Your note" in note.locator(".src").text_content()
    assert note.locator(".actions > .ghost").all_text_contents() == ["Knew it", "Not important", "Absorb…"]
    assert note.locator('.ghost[title="Knew it (d)"]').count() == 1 and note.locator("kbd").count() == 0
    assert "Judged before your last thesis edit" in feed_card(page, 1066).locator(".flags").text_content()
    page.click('[data-more="margins"]')
    figures = feed_card(page, 1047)
    assert "Mostly figures — open the page to check the numbers." in figures.locator(".flags").text_content()
    assert "clamp-2" in figures.locator(".quote").get_attribute("class")
    figures.get_by_text("Show the whole passage").click()
    assert "clamp" not in feed_card(page, 1047).locator(".quote").get_attribute("class")
    assert feed_card(page, 1047).get_by_text("Show less").is_visible()
    wrapped = feed_card(page, 1131).locator(".quote").text_content()
    assert wrapped.startswith("The Company now expects third-quarter gross margin") and "EX-99.1" not in wrapped
    goto_section(page, "filings")
    added = page.locator('.card[data-section="added"][data-id="1036"]')
    assert "Rated as speculation, not a reported fact." in added.locator(".flags").text_content()
    assert page.locator(".change ins").count() > 0 and page.locator(".change del").count() > 0


def test_card_overflow_menu_actions(open_page):
    page = open_page(init_script=CLIPBOARD).page
    goto_tab(page, "ACME")
    card = feed_card(page, 1081)
    menu = open_menu(card)
    assert menu.locator(".menu-item").all_text_contents() == [
        "Star",
        "Skim document",
        "Open source",
        "Why is this here?",
        "Select for quote",
    ]
    page.keyboard.press("Escape")
    assert card.locator(".menu").count() == 0
    open_menu(card).get_by_text("Why is this here?").click()
    why = feed_card(page, 1081).locator(".panel-x").text_content()
    assert "Shown under What's new." in why and "✓ New to you: 70% (minimum 60%)" in why
    open_menu(feed_card(page, 1081)).get_by_text("Star").click()
    assert queued(page) == [
        {"op": "triage", "ticker": "ACME", "passage_id": 1081, "starred": True},
        {"op": "label", "ticker": "ACME", "passage_id": 1081, "question": "material", "value": 1, "origin": "triage"},
        {"op": "label", "ticker": "ACME", "passage_id": 1081, "question": "whats_new", "value": 1, "origin": "triage"},
    ]
    assert "★ Starred" in feed_card(page, 1081).locator(".src").text_content()
    open_menu(feed_card(page, 1081)).get_by_text("Select for quote").click()
    open_menu(feed_card(page, 1058)).get_by_text("Select for quote").click()
    assert "Selected for quote" in feed_card(page, 1081).locator(".src").text_content()
    assert "2 passages selected" in page.locator("#actionbar").text_content()
    page.click('text="Copy as Markdown quotes"')
    page.wait_for_function("window.__copied.length === 1")
    text = page.evaluate("window.__copied[0]")
    first, second = passage("ACME", 1081), passage("ACME", 1058)
    assert text.startswith("> " + first["text"] + "\n\n— Dealer visit, Duluth, own note, 2026-09-22, p. 1")
    assert "\n\n> " + second["speaker"] + ": " + second["text"] + "\n\n— " in text
    contra = feed_card(page, 1041, "contra")
    assert open_menu(contra).locator(".menu-item").all_text_contents()[:4] == [
        "Knew it",
        "Not important",
        "Absorb…",
        "Star",
    ]
    page.keyboard.press("Escape")
    open_menu(feed_card(page, 1081)).get_by_text("Skim document").click()
    assert page.locator(".sheet.skim").is_visible()


def test_absorb_with_a_fact_that_replaces_one(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    page.click('[data-more="margins"]')
    feed_card(page, 1044).click()
    page.keyboard.press("a")
    field = page.locator('[data-fkey="absorb-text"]')
    assert field.evaluate("n => n === document.activeElement")
    assert page.locator("form.absorb").get_by_text("Replace “Gross margin 19.8% in Q2, down 240 bps.”").is_visible()
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
        {
            "op": "fact",
            "ticker": "ACME",
            "pillar": "margins",
            "text": "Gross margin 20.4% in Q3, up 60 bps sequentially.",
            "source": 1044,
            "replace": "margins.0",
        },
    ]
    assert feed_card(page, 1044).count() == 0
    goto_section(page, "thesis")
    facts = page.locator("#facts").text_content()
    assert "Gross margin 20.4% in Q3, up 60 bps sequentially." in facts and "Gross margin 19.8% in Q2" not in facts


def test_mark_these_as_read_asks_first_and_sends_no_labels(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    page.select_option('select[aria-label="Filter by source"]', "sell_side")
    shown = page.locator('.feed[data-feed="new"] > .card:not(.spot)').count()
    assert 0 < shown < whats_new_count(page)
    page.click(f'text="Mark these {shown} as read"')
    assert page.locator("#actionbar").is_hidden()
    page.click(f'button >> text="Dismiss {shown}"')
    actions = queued(page)
    assert len(actions) == shown and all(a["op"] == "triage" and a["status"] == "dismissed" for a in actions)
    page.keyboard.press("u")
    assert page.locator("#actionbar").is_hidden()


def test_since_my_last_visit_filter(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    page.check('input[aria-label="Since my last visit"]')
    arrived = [p["id"] for p in company("ACME")["passages"] if p["arrived_new"] and p["classified"]["in_whats_new"]]
    assert sorted(ids_of(page.locator('.feed[data-feed="new"] > .card'))) == sorted(arrived)
    assert page.locator(".contra-group").count() == 0


def test_threshold_slider_updates_live_counts(open_page):
    page = open_page().page
    goto_tab(page, "ACME")
    c = company("ACME")
    before = whats_new_count(page)
    after = before + sum(p["classified"]["in_maybe"] for p in c["passages"])
    page.click("#btn-thresholds")
    page.eval_on_selector(
        'input[data-policy="new_info_min"]',
        "n => { n.value = '0.4'; n.dispatchEvent(new Event('input', {bubbles: true})); }",
    )
    page.wait_for_function(f"document.querySelector('[data-count=\"whats_new\"]').textContent === '{after}'")
    assert (
        page.locator('#th-counts [data-ticker="ACME"] [data-live="whats_new"]').text_content()
        == f"What's new {before} → {after}"
    )
    assert page.locator('nav#tabs [data-tab="ACME"] .badge').text_content() == str(after)
    page.click('text="Reset to saved"')
    assert whats_new_count(page) == before
    page.eval_on_selector(
        'input[data-policy="whats_new_window_days"]',
        "n => { n.value = '1'; n.dispatchEvent(new Event('input', {bubbles: true})); }",
    )
    page.wait_for_function("document.querySelector('[data-count=\"whats_new\"]').textContent === '0'")
    page.click('text="Reset to defaults"')
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
    ids = ids_of(sheet.locator(".skim-p.hl-new, .skim-p.hl-maybe, .skim-p.contra"))
    page.keyboard.press("n")
    assert int(sheet.locator(".skim-current").get_attribute("data-id")) == ids[ids.index(pid) + 1]
    page.keyboard.press("p")
    assert int(sheet.locator(".skim-current").get_attribute("data-id")) == pid
    stub = sheet.locator("button.stub")
    assert stub.count() == 1 and "Off thesis" in stub.text_content()
    stub.click()
    assert sheet.locator('.skim-p[data-id="1084"]').is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()


def test_search_finds_passages_and_highlights_matches(open_page):
    page = open_page().page
    page.fill("#search", "volt-x")
    page.press("#search", "Enter")
    expected = sum(
        1
        for c in SAMPLE["companies"]
        for p in c["passages"]
        if "volt-x" in ((p["speaker"] or "") + " " + p["text"]).lower()
    )
    results = page.locator('#app .card[data-section="search"]')
    results.first.wait_for()
    assert results.count() == expected > 0
    assert set(page.locator("#app .card mark").all_text_contents()) == {"Volt-X"}
    assert page.locator("nav#sections").is_hidden()
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


# ---------------------------------------------------------------- Thesis, Metrics, Signals


def test_thesis_scorecard_drawer_questions_predictions_and_facts(open_page):
    page = open_page(hash="#ACME/thesis").page
    rows = page.locator("#scorecard .score-row")
    assert rows.count() == 4 and rows.first.locator("svg.tide rect.hit").count() == 12
    page.locator('.score-row[data-assumption="inv_normalizes"] button.score-statement').click()
    drawer = page.locator(".sheet.drawer")
    drawer.wait_for()
    headings = drawer.locator("h3").all_text_contents()
    assert headings == ["Against (4)", "For (2)"]
    assert ids_of(drawer.locator('.card[data-section="against-inv_normalizes"]')) == [1061, 1041, 1091, 1022]
    assert ids_of(drawer.locator('.card[data-section="for-inv_normalizes"]')) == [1033, 1025]
    page.keyboard.press("Escape")
    assert page.locator("#overlay").is_hidden()
    assert page.locator("#questions details").count() == 2
    page.click('[data-prediction="inv_normal_by_q1"] >> button[aria-label="Resolve yes"]')
    assert queued(page) == [{"op": "resolve", "ticker": "ACME", "prediction_id": "inv_normal_by_q1", "outcome": True}]
    assert (
        "Your average error (Brier score) is 0.14 over 3 resolved predictions"
        in page.locator("#predictions").text_content()
    )
    assert "Happened" in page.locator('[data-prediction="inv_normal_by_q1"]').text_content()
    facts = page.locator("#facts")
    assert "As of Aug 5, 2026" in facts.text_content()
    facts.get_by_text("Source ↗").first.click()
    assert ids_of(page.locator(".sheet.modal .card")) == [1031]


def test_metrics_cards_chart_tooltips_and_table(open_page):
    page = open_page(hash="#ACME/metrics").page
    assert page.locator(".metric-card").count() == 3
    gm = page.locator('.metric-card[data-metric="gross_margin"]')
    assert gm.locator(".metric-value").text_content() == "20.4%"
    assert gm.locator(".metric-change").text_content() == "▲Up 0.6 pts from Q2 2026"
    assert "for" in gm.locator(".metric-change .mark").get_attribute("class")
    assert gm.locator(".mchip").all_text_contents() == ["●Within guidance", "▼Missed estimates", "▼Guidance cut"]
    di = page.locator('.metric-card[data-metric="dealer_inventory_yoy"]')
    assert "against" in di.locator(".metric-change .mark").get_attribute("class")
    assert di.locator(".mchip").all_text_contents() == ["▲Above estimates"]
    assert di.locator('rect.hit[data-kind="guidance"]').count() == 0
    assert gm.locator("text.end-label").text_content() == "20.4%"
    assert "Line: reported · Gray bars: company guidance" in gm.locator(".chart-key").text_content()
    tip = page.locator("#tip")
    reported = gm.locator('rect.hit[data-kind="reported"]')
    assert reported.count() == 5
    reported.nth(4).hover()
    text = tip.text_content()
    assert text.startswith("Q3 2026 · Reported: 20.4%") and "8-K · Sep 15, 2026" in text
    assert "“Gross margin was 20.4%, an improvement of 60 basis points from the second quarter.”" in text
    gm.locator('rect.hit[data-kind="guidance"]').first.hover()
    assert tip.text_content().startswith("Q4 2025 · Company guidance: 22% to 23%")
    gm.locator('rect.hit[data-kind="guidance"]').last.focus()
    text = tip.text_content()
    assert text.startswith("Q3 2026 · Company guidance: 20% to 21%") and "EX-99.1" not in text
    gm.locator('rect.hit[data-kind="estimate"]').last.focus()
    assert tip.text_content().startswith("Q3 2026 · Analyst estimate: 21.0%")
    reported.nth(4).click()
    page.locator(".sheet.skim").wait_for()
    assert page.locator(".sheet.skim .skim-current").get_attribute("data-id") == "1044"
    page.keyboard.press("Escape")
    gm.get_by_text("Show as table").click()
    table = gm.locator("table.data")
    assert table.locator("tbody tr").count() == 5
    q3 = table.locator("tbody tr").nth(4).locator("td")
    assert q3.nth(1).inner_text() == "21–22% → 20–21% (cut Sep 1)"
    assert q3.nth(2).inner_text() == "21%"
    assert q3.nth(3).text_content() == "Within guidance" and q3.nth(4).text_content() == "Missed estimates"
    q3.nth(0).locator("summary").click()
    assert q3.nth(0).locator(".qtext").is_visible()
    assert "Gross margin was 20.4%" in q3.nth(0).locator(".qtext").text_content()
    retail = page.locator('.metric-card[data-metric="na_retail_yoy"]')
    assert retail.locator(".metric-value").text_content() == "−6%" and "Down 1 pt from Q2 2026" in retail.text_content()


def test_tone_heat_map_tooltips_and_table(open_page):
    page = open_page(hash="#ACME/signals").page
    cells = page.locator(".tone-grid .tone-cell")
    c = company("ACME")
    expected = [
        p
        for p in c["passages"]
        if p["p"]
        and p["p"]["pillar"] == "dealer_inventory"
        and p["p"]["pillar_p"] >= 0.5
        and p["p"]["boilerplate"] <= 0.5
        and pdate(p).startswith("2026-09")
    ]
    cell = page.locator('.tone-cell[aria-label^="Dealer inventory · Sep 2026"]')
    tip = page.locator("#tip")
    cell.hover()
    assert tip.text_content().startswith(f"Dealer inventory · Sep 2026: negative ({len(expected)} passages, ")
    page.mouse.move(2, 880)
    cells.first.focus()
    assert tip.is_visible() and tip.text_content() == cells.first.get_attribute("aria-label")
    n = cells.count()
    page.locator("#tone").get_by_text("Show as table").click()
    table = page.locator("#tone table.tone-table")
    assert table.locator("tbody tr").count() == n
    assert table.locator("th").all_text_contents() == ["Pillar", "Month", "Tone", "Passages", "Material"]
    page.locator("#tone").get_by_text("Show as chart").click()
    page.locator('.tone-cell[aria-label^="Dealer inventory · Sep 2026"]').click()
    assert page.locator(".sheet.modal .card").count() == len(expected)


def test_signals_voices_and_ledger(open_page):
    page = open_page(hash="#ACME/signals").page
    flagged = page.locator(".db-row.flagged")
    assert flagged.count() == 1 and flagged.get_attribute("data-pillar") == "dealer_inventory"
    assert "Diverges" in flagged.text_content()
    assert flagged.locator(".db-sentence").text_content() == (
        "Management sounds neutral; channel checks and experts sound negative (4 passages each)."
    )
    assert flagged.locator("text.dot-label").all_text_contents() == ["Outside", "Management"]
    flagged.locator("circle.hit").first.hover()
    assert page.locator("#tip").text_content() == "Management: neutral (4 passages)"
    [d] = [d for d in company("ACME")["divergence"] if d["flagged"]]
    flagged.get_by_text("Read the passages").click()
    assert ids_of(page.locator(".sheet.modal .card")) == d["inside"]["ids"] + d["outside"]["ids"]
    page.keyboard.press("Escape")
    ledger = page.locator("#ledger")
    assert ledger.locator(".promise").count() == 5
    assert "Management kept 1 of 3 promises checked so far; 2 are still open." in ledger.text_content()
    statuses = ledger.locator(".promise").evaluate_all("ns => ns.map(n => n.dataset.status)")
    assert statuses == [i["status"] for i in company("ACME")["ledger"]["items"]]


def test_keyboard_help_and_tab_switching(open_page):
    page = open_page().page
    page.keyboard.press("g")
    page.keyboard.press("2")
    assert page.locator('nav#tabs [data-tab="BRRR"]').get_attribute("aria-selected") == "true"
    page.keyboard.press("g")
    page.keyboard.press("o")
    assert page.locator('nav#tabs [data-tab="overview"]').get_attribute("aria-selected") == "true"
    page.keyboard.press("?")
    help_text = page.locator(".sheet.help").text_content()
    assert "Read · Thesis · Metrics · Signals · Filings" in help_text
    page.keyboard.press("Escape")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "search"
    page.keyboard.press("j")
    assert page.input_value("#search") == "j"


@pytest.mark.parametrize("width", [360, 390, 414])
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
    for more in page.locator("[data-more]").all():
        more.click()
    assert page.evaluate(overflow) <= 0
    focus_first(page, "new")
    page.keyboard.press("d")
    open_menu(page.locator(".card.focused"))
    assert page.evaluate(overflow) <= 0
    page.keyboard.press("Escape")
    for section in ["thesis", "metrics", "signals", "filings"]:
        goto_section(page, section)
        assert page.evaluate(overflow) <= 0, section
    goto_section(page, "metrics")
    page.locator(".metric-card").first.get_by_text("Show as table").click()
    assert page.evaluate(overflow) <= 0
    goto_section(page, "read")
    focus_first(page, "new")
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
    assert request.post_data_json == {
        "actions": [
            {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"},
            {
                "op": "label",
                "ticker": "ACME",
                "passage_id": pid,
                "question": "new_info",
                "value": 0,
                "origin": "triage",
            },
            {
                "op": "label",
                "ticker": "ACME",
                "passage_id": pid,
                "question": "whats_new",
                "value": 0,
                "origin": "triage",
            },
        ]
    }
    with page.expect_request("**/api/actions"):
        page.keyboard.press("u")
    assert requests[-1].post_data_json == {
        "actions": [{"op": "triage", "ticker": "ACME", "passage_id": pid, "status": None, "starred": False}]
    }
    assert feed_card(page, pid).count() == 1
    page.click("#btn-thresholds")
    with page.expect_request("**/api/actions"):
        page.click('#thresholds >> text="Save"')
    [action] = requests[-1].post_data_json["actions"]
    assert action["op"] == "policy" and action["values"]["new_info_min"] == 0.6
    assert action["values"]["metric_min_probability"] == 0.6


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
    assert feed_card(page, pid).count() == 0
    state["fail"] = False
    page.click('#actionbar >> text="Retry"')
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
        extra = dict(
            by_id[1031],
            id=9999,
            seq=99,
            text="An unjudged passage from the full filing.",
            p=None,
            status="unjudged",
            classified=None,
        )
        body = {
            "document": dict(doc, complete=True),
            "passages": [by_id[i] for i in (1031, 1033, 1034, 1035)] + [extra],
        }
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page = open_page(
        serve_payload(),
        serve_routes={
            re.compile(r".*/api/search\?.*"): search,
            re.compile(r".*/api/document/103\?ticker=ACME$"): document,
        },
    ).page
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
