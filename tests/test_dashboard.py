import json
import re
import shutil
import subprocess
from importlib import resources

import pytest

from helpers import choice, noul, payload_from_html, score, write_thesis
from thesis_radar.config import Workspace
from thesis_radar.dashboard import DATA_MARKER, build_payload, render_html, script_safe_json, write_dashboard
from thesis_radar.models import JudgmentRecord, NewDocument, PassageDraft
from thesis_radar.policy import Policy
from thesis_radar.runner import plan_judging
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses

MODEL = "jev-1.13.0"


def passage_answers(*, new_info, contradicts):
    return {
        "pillar": choice("inventory", {"inventory": 0.9, "other": 0.1}),
        "boilerplate": noul(0.05),
        "new_info": noul(new_info),
        "materiality": score([0, 0, 0.2, 0.8]),
        "stance": score([0, 1, 0, 0, 0]),
        "evidence": choice("guidance", {"guidance": 1.0}),
        "forward_looking": noul(0.2),
        "assumption__inv_normalizes": choice(
            "neither", {"supports": 0.05, "contradicts": contradicts, "neither": 0.95 - contradicts}
        ),
    }


@pytest.fixture
def env(tmp_path):
    ws = Workspace(tmp_path)
    ws.ensure_layout()
    write_thesis(ws.thesis_dir)
    theses, _ = load_theses(ws.thesis_dir)
    clock = iter(["2026-09-20T00:00:00Z"] + ["2026-09-22T00:00:00Z"] * 50)
    store = Store(ws.db_path, clock=lambda: next(clock))
    with store.transaction():
        old = store.insert_document(
            NewDocument(text_sha256="a" * 64, path="archive/ACME/2026-09-10_sell_side_note.pdf", title="Old note",
                        origin="inbox", status="sorted", ticker="ACME", source_type="sell_side", doc_date="2026-09-10")
        )
        store.insert_passages(old, [PassageDraft(0, 2, 0, 10, "Inventory stays high. [contradicts]")])
        new = store.insert_document(
            NewDocument(text_sha256="b" * 64, path="archive/ACME/2026-09-21_own_note_n.txt", title="New note",
                        origin="inbox", status="sorted", ticker="ACME", source_type="own_note", doc_date="2026-09-21")
        )
        store.insert_passages(
            new,
            [PassageDraft(0, 1, 0, 10, "Inventory rose. [new]"),
             PassageDraft(1, 1, 11, 20, "</script><script>alert(1)</script> inventory")],
        )
        store.insert_document(
            NewDocument(text_sha256="c" * 64, path="archive/_unsorted/w.txt", title="Weather", origin="inbox",
                        status="unsorted", status_reason="no matching company")
        )
    for item in plan_judging(store, theses, MODEL).pending:
        text = item.request.state["passage"]
        answers = passage_answers(
            new_info=0.9 if "[new]" in text else 0.1, contradicts=0.9 if "[contradicts]" in text else 0.05
        )
        store.save_judgment(JudgmentRecord(item.passage_id, item.cache_key, MODEL, "r", item.thesis_version, "judged", answers))
    yield ws, store, theses
    store.close()


def build(env, previous="2026-09-21T00:00:00Z"):
    ws, store, theses = env
    plan = plan_judging(store, theses, MODEL)
    return build_payload(ws, store, theses, plan, Policy(), generated_at="2026-09-22T08:00:00Z", previous_view=previous)


def test_payload_sorts_passages_into_views(env):
    payload = build(env)
    [company] = payload["companies"]
    by_text = {p["text"]: p for p in company["passages"]}
    assert by_text["Inventory stays high. [contradicts]"]["in_contradictions"]
    assert not by_text["Inventory stays high. [contradicts]"]["in_whats_new"]
    assert by_text["Inventory rose. [new]"]["in_whats_new"]
    assert company["assumptions"] == [
        {"id": "inv_normalizes", "pillar": "inventory", "statement": "Dealer inventory returns to normal within two quarters."}
    ]
    assert payload["pending"] == {"unjudged": 0, "failed": 0}


def test_new_arrivals_and_links(env):
    ws, _, _ = env
    passages = {p["text"]: p for p in build(env)["companies"][0]["passages"]}
    old, new = passages["Inventory stays high. [contradicts]"], passages["Inventory rose. [new]"]
    assert not old["arrived_new"] and new["arrived_new"]
    assert old["link"] == (ws.root / "archive/ACME/2026-09-10_sell_side_note.pdf").resolve().as_uri() + "#page=2"
    assert "#page=" not in new["link"]
    assert all(not p["arrived_new"] for p in build(env, previous=None)["companies"][0]["passages"])


def test_unsorted_documents_come_with_a_tag_command(env):
    [item] = build(env)["unsorted"]
    assert item["reason"] == "no matching company"
    assert item["command"] == f"radar tag {item['id']} --ticker TICKER --source SOURCE --date YYYY-MM-DD"


def test_passage_text_cannot_break_out_of_the_data_block(env):
    payload = build(env)
    html = render_html(payload)
    assert "<script>alert(1)</script>" not in html
    assert payload_from_html(html) == payload


def test_script_safe_json_escapes_dangerous_characters():
    text = script_safe_json({"a": "</script>& "})
    assert "<" not in text and ">" not in text and "&" not in text and " " not in text
    assert json.loads(text) == {"a": "</script>& "}


def _template():
    return resources.files("thesis_radar").joinpath("dashboard_template.html").read_text(encoding="utf-8")


def test_template_is_offline_and_has_one_marker():
    template = _template()
    assert template.count(DATA_MARKER) == 1
    assert "http://" not in template and "https://" not in template
    assert "innerHTML" not in template


def test_write_dashboard_replaces_atomically(tmp_path):
    path = tmp_path / "dashboard.html"
    write_dashboard(path, "one")
    write_dashboard(path, "two")
    assert path.read_text(encoding="utf-8") == "two"
    assert not (tmp_path / "dashboard.html.tmp").exists()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_script_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", _template(), re.S)
    assert len(scripts) == 1
    path = tmp_path / "dashboard.js"
    path.write_text(scripts[0], encoding="utf-8")
    subprocess.run(["node", "--check", str(path)], check=True)
