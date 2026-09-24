import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from helpers import ACME_THESIS, answer_all, choice_from, noul, payload_from_html, score, write_thesis

from thesis_radar import dashboard
from thesis_radar.app import App
from thesis_radar.config import Workspace
from thesis_radar.dashboard import (
    DATA_MARKER,
    build_payload,
    document_view,
    passages_view,
    render_html,
    script_safe_json,
    write_dashboard,
)
from thesis_radar.judge import FakeJudge
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.runner import RateLimiter, run_judging
from thesis_radar.similar import link_document

TODAY = date(2026, 9, 24)
SAMPLE = Path(__file__).parent / "fixtures" / "sample_payload.json"
TEMPLATE = Path(dashboard.__file__).with_name("dashboard_template.html")
THESIS = ACME_THESIS.replace("known_facts:", "peers: [BRRR]\nopen_questions:\n  q4: Will dealers cut Q4 orders?\nknown_facts:")


def respond(request):
    text = request.state["passage"]
    overrides = {}
    if "[new]" in text or "[contradicts]" in text or "[maybe]" in text:
        overrides["pillar"] = choice_from(request.questions["pillar"], "inventory", 0.9)
        overrides["materiality"] = score([0, 0, 0.2, 0.8])
        overrides["boilerplate"] = noul(0.05)
        overrides["new_info"] = noul(0.5 if "[maybe]" in text else 0.9)
    if "[contradicts]" in text:
        overrides["assumption__inv_normalizes"] = choice_from(request.questions["assumption__inv_normalizes"], "contradicts", 0.9)
    return answer_all(request, **overrides)


def add_doc(app, digest, texts, *, ticker="ACME", date_="2026-09-20", path=None, status="sorted", form=None):
    store = app.store
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest, path=path or f"archive/{ticker}/{digest[:4]}.txt", title=f"Doc {digest[:4]}",
                        origin="edgar" if form else "inbox", status=status, ticker=ticker if status == "sorted" else None,
                        source_type="filing" if form else "own_note", doc_date=date_, form=form,
                        status_reason=None if status == "sorted" else "no matching company")
        )
        store.insert_passages(doc, [PassageDraft(i, i + 1, 0, 5, t) for i, t in enumerate(texts)])
    link_document(store, doc)
    return doc


@pytest.fixture
def app(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    write_thesis(ws.thesis_dir, text=THESIS)
    app = App.open(ws, today=TODAY)
    clock = iter(["2026-09-20T00:00:00Z"] + ["2026-09-22T00:00:00Z"] * 500)
    app.store._clock = lambda: next(clock)
    add_doc(app, "a" * 64, ["Inventory stays high. [contradicts]", "Safe harbor language."], path="archive/ACME/old.pdf",
            date_="2026-09-10")
    add_doc(app, "b" * 64, ["Inventory rose again. [new]", "</script><script>alert(1)</script> inventory",
                            "Dealer checks were mixed. [maybe]"])
    add_doc(app, "c" * 64, ["Brrr dealers cut orders. [new]"], ticker="BRRR")
    add_doc(app, "d" * 64, ["Sunny weather."], status="unsorted")
    add_doc(app, "e" * 64, ["Inventory rose again. [new]"], date_="2026-09-21")  # an exact repeat
    plan = app.plan()
    asyncio.run(run_judging(app.store, FakeJudge(respond), plan.pending, concurrency=4, limiter=RateLimiter(60_000)))
    yield app
    app.close()


def build(app, previous="2026-09-21T00:00:00Z", **kwargs):
    return build_payload(app, generated_at="2026-09-24T08:00:00Z", previous_view=previous, **kwargs)


def by_text(payload, ticker="ACME"):
    company = next(c for c in payload["companies"] if c["ticker"] == ticker)
    return {p["text"]: p for p in company["passages"]}


def test_payload_sorts_passages_into_views(app):
    payload = build(app)
    assert payload["version"] == 2 and payload["mode"] == "static" and payload["serve"] is None
    passages = by_text(payload)
    assert passages["Inventory stays high. [contradicts]"]["classified"]["in_contradictions"]
    assert not passages["Inventory stays high. [contradicts]"]["classified"]["in_whats_new"]
    assert passages["Inventory rose again. [new]"]["classified"]["in_whats_new"]
    assert passages["Dealer checks were mixed. [maybe]"]["classified"]["in_maybe"]
    assert payload["pending"] == {"unjudged": 0, "failed": 0, "stale": 0}
    assert payload["policy"]["new_info_min"] == 0.6 == payload["policy_defaults"]["new_info_min"]


def test_repeats_are_excluded_and_peers_are_read_through(app):
    passages = by_text(build(app))
    assert sum(1 for text in passages if text == "Inventory rose again. [new]") == 1
    assert passages["Brrr dealers cut orders. [new]"]["read_through"] == "BRRR"
    company = build(app)["companies"][0]
    assert {d["ticker"] for d in company["documents"]} == {"ACME", "BRRR"}
    assert company["peers"] == ["BRRR"] and company["open_questions"] == [{"id": "q4", "text": "Will dealers cut Q4 orders?"}]
    assert company["known_facts"]["inventory"][0]["id"] == "inventory.0"


def test_context_links_and_new_arrivals(app):
    passages = by_text(build(app))
    old, new = passages["Inventory stays high. [contradicts]"], passages["Inventory rose again. [new]"]
    assert not old["arrived_new"] and new["arrived_new"]
    assert old["link"].endswith("old.pdf#page=1") and "#page=" not in new["link"]
    assert passages["Dealer checks were mixed. [maybe]"]["context"].startswith("</script>")
    assert all(not p["arrived_new"] for p in by_text(build(app, previous=None)).values())


def test_triage_changes_classification(app):
    pid = by_text(build(app))["Inventory rose again. [new]"]["id"]
    app.store.set_triage("ACME", pid, status="dismissed", starred=True)
    passage = by_text(build(app))["Inventory rose again. [new]"]
    assert passage["triage"] == {"status": "dismissed", "starred": True, "false_alarms": []}
    assert not passage["classified"]["in_whats_new"]


def test_false_alarms_are_carried_for_the_verdicts(app):
    pid = by_text(build(app))["Inventory stays high. [contradicts]"]["id"]
    app.store.save_label(pid, "contradicts__inv_normalizes", False, ticker="ACME", origin="triage")
    app.store.save_label(pid, "new_info", False, ticker="ACME", origin="triage")
    assert by_text(build(app))["Inventory stays high. [contradicts]"]["triage"]["false_alarms"] == ["inv_normalizes"]


def test_stale_answers_are_shown_after_a_thesis_edit(app):
    write_thesis(app.ws.thesis_dir, text=THESIS + '  pricing:\n    - "Promotions were cut."\n')
    app.reload()
    payload = build(app)
    passage = by_text(payload)["Inventory rose again. [new]"]
    assert passage["status"] == "stale" and passage["p"] is not None
    assert payload["pending"]["stale"] > 0


def test_unsorted_documents_come_with_a_tag_command(app):
    [item] = build(app)["unsorted"]
    assert item["reason"] == "no matching company" and item["link"].startswith("file://")
    # Known metadata is filled in; unknown parts are placeholders.
    assert item["command"] == f"radar tag {item['id']} --ticker TICKER --source own_note --date 2026-09-20"


def test_spot_checks_are_unflagged_embedded_and_not_repeated(app):
    company = build(app)["companies"][0]
    embedded = {p["id"]: p for p in company["passages"]}
    assert company["spot_checks"]
    for pid in company["spot_checks"]:
        assert pid in embedded and not embedded[pid]["classified"]["flagged"]
    answered = company["spot_checks"][0]
    app.store.save_label(answered, "whats_new", False, ticker="ACME", origin="spotcheck")
    assert answered not in build(app)["companies"][0]["spot_checks"]


def test_documents_report_completeness(app):
    company = build(app)["companies"][0]
    assert all(d["complete"] for d in company["documents"])  # all recent


def test_serve_mode_carries_the_token(app):
    payload = build(app, mode="serve", serve_token="t0k")
    assert payload["serve"] == {"token": "t0k"} and payload["mode"] == "serve"


def test_document_and_passage_views(app):
    doc_id = app.store.documents(tickers=["ACME"])[0]["id"]
    view = document_view(app, doc_id, "ACME")
    assert [p["seq"] for p in view["passages"]] == [0, 1]
    assert view["document"]["complete"] is True
    assert document_view(app, doc_id, "NOPE") is None
    ids = [p["id"] for p in view["passages"]]
    assert [p["id"] for p in passages_view(app, "ACME", list(reversed(ids)))] == list(reversed(ids))
    assert passages_view(app, "NOPE", ids) == []


def _shape(value, depth=0):
    if isinstance(value, dict) and depth < 3:
        return {k: _shape(v, depth + 1) for k, v in value.items()}
    return type(value).__name__


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample payload not written yet")
def test_payload_keys_match_the_frontend_sample(app):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    payload = build(app)
    assert set(payload) == set(sample)
    ours, theirs = payload["companies"][0], sample["companies"][0]
    assert set(ours) == set(theirs)
    judged = next(p for p in ours["passages"] if p["p"] is not None)
    sample_judged = next(p for p in theirs["passages"] if p["p"] is not None)
    assert set(judged) == set(sample_judged)
    assert set(judged["p"]) == set(sample_judged["p"])
    assert set(judged["classified"]) == set(sample_judged["classified"])
    assert set(ours["documents"][0]) == set(theirs["documents"][0])
    assert set(payload["policy"]) == set(sample["policy"])
    assert set(payload["pending"]) == set(sample["pending"])


def test_script_safe_json_escapes_dangerous_characters():
    text = script_safe_json({"a": "</script>&  "})
    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"a": "</script>&  "}


@pytest.mark.skipif(not TEMPLATE.exists(), reason="dashboard template not written yet")
def test_passage_text_cannot_break_out_of_the_data_block(app):
    payload = build(app)
    html = render_html(payload)
    assert "<script>alert(1)</script>" not in html
    assert payload_from_html(html) == payload
    assert html.count(DATA_MARKER) == 0


def test_write_dashboard_replaces_atomically(tmp_path):
    path = tmp_path / "dashboard.html"
    write_dashboard(path, "one")
    write_dashboard(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert not (tmp_path / "dashboard.html.tmp").exists()
