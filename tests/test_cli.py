import io
import json
from datetime import date
from pathlib import Path

import pytest
from helpers import ACME_THESIS, choice_from, noul, payload_from_html, score, write_thesis

from thesis_radar import dashboard
from thesis_radar.cli import main
from thesis_radar.judge import FakeJudge, JudgeFatal
from thesis_radar.lock import exclusive_lock

TODAY = date(2026, 9, 24)
CALL = """ACME Snowmobiles Q3 2026 earnings call
September 15, 2026

Operator: Welcome to the ACME Snowmobiles third quarter call.
Jane Doe - CFO: Dealer inventory rose again and should stay elevated into next year. [contradicts]
Q: How is pricing holding up?
John Roe - CEO: We cut promotions sharply in September. [new]
"""
NOTE = "My ACME notes\n\nSeptember 20, 2026\n\nDealer checks suggest pricing is stable. [maybe]\n"
WEATHER = "Local weather for Tuesday: sunny.\n"


def responder(request):
    questions, state = request.questions, request.state
    if "ticker" in questions:
        text = state["text"]
        answers = {
            "ticker": choice_from(questions["ticker"], "ACME" if "ACME" in text else "none", 0.95),
            "source_type": choice_from(questions["source_type"], "earnings_transcript" if "Operator:" in text else "own_note", 0.9),
        }
        if "doc_date" in questions:
            first = next(key for key in questions["doc_date"]["criteria"] if key != "none")
            answers["doc_date"] = choice_from(questions["doc_date"], first, 0.9)
        return answers
    if "followup" in questions:
        return {"followup": choice_from(questions["followup"], "not_addressed", 0.8)}
    text = state["passage"]
    lower = text.lower()
    answers = {}
    for name, question in questions.items():
        if name == "pillar":
            pillar = "inventory" if "inventory" in lower else ("pricing" if "pricing" in lower or "promotion" in lower else "off_thesis")
            answers[name] = choice_from(question, pillar, 0.9)
        elif name == "boilerplate":
            answers[name] = noul(0.05)
        elif name == "new_info":
            answers[name] = noul(0.9 if "[new]" in text or "[contradicts]" in text else 0.5 if "[maybe]" in text else 0.1)
        elif name == "materiality":
            marked = any(tag in text for tag in ("[new]", "[maybe]", "[contradicts]"))
            answers[name] = score([0, 0, 0.2, 0.8] if marked else [0.9, 0.1, 0, 0])
        elif name == "stance":
            answers[name] = score([0, 0, 1, 0, 0])
        elif name == "evidence":
            answers[name] = choice_from(question, "management_commentary", 0.8)
        elif name == "forward_looking":
            answers[name] = noul(0.5)
        elif name == "updates_fact":
            answers[name] = choice_from(question, "none", 0.8)
        elif name.startswith("assumption__"):
            answers[name] = choice_from(question, "contradicts" if "[contradicts]" in text else "neither", 0.9)
        else:
            answers[name] = noul(0.1)
    return answers


@pytest.fixture(autouse=True)
def template(monkeypatch):
    if not Path(dashboard.__file__).with_name("dashboard_template.html").exists():
        monkeypatch.setattr(dashboard, "template_text", lambda: '<script id="radar-data" type="application/json">__RADAR_DATA__</script>')


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "research"
    (workspace / "inbox").mkdir(parents=True)
    write_thesis(workspace / "thesis")
    (workspace / "inbox" / "acme-call.txt").write_text(CALL, encoding="utf-8")
    (workspace / "inbox" / "acme-note.md").write_text(NOTE, encoding="utf-8")
    (workspace / "inbox" / "weather.txt").write_text(WEATHER, encoding="utf-8")
    return workspace


def radar(ws, *argv, fake=True, respond=responder, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    factory = (lambda: FakeJudge(respond, model="jev-1.13.0")) if fake else None
    code = main(["--workspace", str(ws), *argv], judge_factory=factory, out=out, err=err, stdin=io.StringIO(stdin), today=TODAY)
    return code, out.getvalue(), err.getvalue()


def dashboard_payload(ws):
    return payload_from_html((ws / "dashboard.html").read_text(encoding="utf-8"))


def find(payload, tag):
    return next(p for p in payload["companies"][0]["passages"] if tag in p["text"])


def test_run_builds_a_dashboard_end_to_end(ws):
    code, out, err = radar(ws, "run")
    assert code == 0, err
    assert "fetch: skipped" in out
    assert "ingest: 2 sorted, 1 unsorted" in out
    assert "ACME: 1 contradictions, 1 new, 1 maybe" in out
    payload = dashboard_payload(ws)
    assert find(payload, "[contradicts]")["classified"]["in_contradictions"]
    assert find(payload, "[new]")["classified"]["in_whats_new"] and find(payload, "[new]")["speaker"] == "John Roe - CEO"
    assert find(payload, "[maybe]")["classified"]["in_maybe"]
    assert [u["title"] for u in payload["unsorted"]] == ["Local weather for Tuesday: sunny."]
    assert payload["pending"] == {"unjudged": 0, "failed": 0, "stale": 0}
    assert sorted(p.name for p in (ws / "inbox").iterdir()) == ["_failed"]
    assert len(list((ws / "archive" / "ACME").iterdir())) == 2


def test_second_run_reuses_cached_judgments(ws):
    radar(ws, "run")
    code, out, _ = radar(ws, "run")
    assert code == 0 and "judge: 0 passage requests to send" in out


def test_thesis_edits_rejudge_recent_passages_and_show_stale_meanwhile(ws):
    radar(ws, "run")
    write_thesis(ws / "thesis", text=ACME_THESIS + '  pricing:\n    - "Promotions were cut sharply in September."\n')
    code, out, _ = radar(ws, "judge", "--dry-run")
    assert code == 0 and "judge: 6 passage requests to send" in out
    assert radar(ws, "view")[0] == 0
    assert dashboard_payload(ws)["pending"]["stale"] == 6
    assert radar(ws, "judge")[0] == 0
    radar(ws, "view")
    assert dashboard_payload(ws)["pending"]["stale"] == 0


def test_absorb_and_tag(ws):
    radar(ws, "run")
    payload = dashboard_payload(ws)
    new_id = find(payload, "[new]")["id"]
    code, out, _ = radar(ws, "absorb", str(new_id))
    assert code == 0 and "# ACME / pricing" in out and "We cut promotions sharply" in out
    unsorted_id = payload["unsorted"][0]["id"]
    code, _, err = radar(ws, "tag", str(unsorted_id), "--ticker", "NOPE", "--source", "own_note", "--date", "2026-09-21")
    assert code == 2 and "unknown ticker" in err
    code, out, _ = radar(ws, "tag", str(unsorted_id), "--ticker", "ACME", "--source", "own_note", "--date", "2026-09-21")
    assert code == 0 and "2026-09-21_own_note_local-weather-for-tuesday-sunny.txt" in out


def test_ingest_without_a_key_touches_nothing(ws, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, _, err = radar(ws, "ingest", fake=False)
    assert code == 2 and "TYPESAFE_API_KEY" in err
    assert (ws / "inbox" / "acme-call.txt").exists()


def test_run_without_a_key_still_writes_the_dashboard(ws, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    code, _, err = radar(ws, "run", fake=False)
    assert code == 0 and "TYPESAFE_API_KEY" in err and (ws / "dashboard.html").exists()


def test_rejected_key_stops_cleanly(ws):
    def reject(request):
        raise JudgeFatal("TypeSafeAuthenticationError: 401")

    code, _, err = radar(ws, "ingest", respond=reject)
    assert code == 2 and "401" in err
    assert (ws / "inbox" / "acme-call.txt").exists()


def test_cost_cap_needs_yes(ws):
    (ws / "config.yaml").write_text("max_cost_per_run: 0\n", encoding="utf-8")
    assert radar(ws, "ingest")[0] == 0
    code, _, err = radar(ws, "judge")
    assert code == 2 and "--yes" in err
    code, out, _ = radar(ws, "judge", "--yes")
    assert code == 0 and "6 judged" in out


def test_locked_workspace_is_refused(ws):
    with exclusive_lock(ws / "radar.lock"):
        code, _, err = radar(ws, "view")
    assert code == 2 and "another radar command" in err


def test_init_and_status(ws):
    code, out, _ = radar(ws, "init")
    assert code == 0 and "wrote config.yaml, policy.yaml" in out
    assert "min_probability" in (ws / "policy.yaml").read_text(encoding="utf-8")
    radar(ws, "run")
    code, out, _ = radar(ws, "status")
    assert code == 0 and "theses: ACME" in out and "passages: 6 current" in out and "this month:" in out


def test_invalid_policy_is_refused(ws):
    (ws / "policy.yaml").write_text("whats_new:\n  nope: 1\n", encoding="utf-8")
    code, _, err = radar(ws, "view")
    assert code == 2 and "unknown setting" in err


def test_calibrate_without_labels(ws):
    code, out, _ = radar(ws, "calibrate")
    assert code == 0 and "No labels yet" in out


def test_label_records_weighted_keyed_labels(ws):
    radar(ws, "run")
    code, out, _ = radar(ws, "label", "--n", "2", stdin="y\nn\nn\nq\n")
    assert code == 0 and "label: 1 passages labeled" in out
    code, out, _ = radar(ws, "calibrate")
    assert code == 0 and "labels" in out


def test_usage_errors_exit_1(ws):
    assert radar(ws, "frobnicate")[0] == 1


PREDICTIONS = ACME_THESIS.replace(
    "known_facts:",
    'predictions:\n  inv_back: {statement: "Inventory is normal by Q1.", by: 2027-03-31, p: 0.7}\nknown_facts:',
)


def test_fact_resolve_and_apply_write_back(ws):
    write_thesis(ws / "thesis", text=PREDICTIONS)
    radar(ws, "run")
    new_id = find(dashboard_payload(ws), "[new]")["id"]
    code, out, err = radar(ws, "fact", "ACME", "pricing", "Promotions cut sharply in September.", "--source", str(new_id))
    assert code == 0, err
    assert "added pricing.0" in out
    thesis_text = (ws / "thesis" / "ACME.yaml").read_text(encoding="utf-8")
    assert "Promotions cut sharply in September." in thesis_text and "2026-09-15" in thesis_text
    assert radar(ws, "resolve", "ACME", "inv_back", "no")[0] == 0
    code, _, err = radar(ws, "resolve", "ACME", "nope", "yes")
    assert code == 2 and "nope" in err
    actions = f'[{{"op": "triage", "ticker": "ACME", "passage_id": {new_id}, "status": "dismissed"}}]'
    code, out, _ = radar(ws, "apply", actions)
    assert code == 0 and "apply: 1 triage" in out
    label = json.dumps([{"op": "label", "ticker": "ACME", "passage_id": new_id, "question": "whats_new", "value": 0}])
    code, out, _ = radar(ws, "apply", "-", stdin=label)
    assert code == 0 and "1 label" in out
    radar(ws, "run")
    payload = dashboard_payload(ws)
    passage = find(payload, "[new]")
    assert passage["triage"]["status"] == "dismissed" and not passage["classified"]["in_whats_new"]
    prediction = payload["companies"][0]["predictions"][0]
    assert prediction["outcome"] is False and payload["companies"][0]["forecast"]["resolved"] == 1
    assert radar(ws, "apply", '[{"op": "explode"}]')[0] == 2


def test_search_and_quote(ws):
    radar(ws, "run")
    code, out, _ = radar(ws, "search", "promotions")
    assert code == 0 and "We cut [promotions]" in out and "p.1" in out
    new_id = find(dashboard_payload(ws), "[new]")["id"]
    code, out, _ = radar(ws, "quote", str(new_id))
    assert code == 0 and out.startswith("> John Roe - CEO: We cut promotions sharply") and "[source](file://" in out
    assert "search: no matches" in radar(ws, "search", "zeppelin")[1]


def test_mcp_speaks_json_rpc(ws):
    radar(ws, "run")
    requests = "\n".join(json.dumps(m) for m in [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "whats_new", "arguments": {"ticker": "ACME"}}},
    ]) + "\n"
    code, out, _ = radar(ws, "mcp", stdin=requests)
    replies = [json.loads(line) for line in out.splitlines()]
    assert code == 0 and [r["id"] for r in replies] == [1, 2]
    result = replies[1]["result"]["structuredContent"]
    assert any("We cut promotions" in p["text"] for p in result["passages"])
