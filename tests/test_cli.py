import io

import pytest

from helpers import choice_from, noul, payload_from_html, score, write_thesis
from thesis_radar.cli import main
from thesis_radar.judge import FakeJudge
from thesis_radar.lock import exclusive_lock

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
    text = state["passage"]
    lower = text.lower()
    if "inventory" in lower:
        pillar = "inventory"
    elif "pricing" in lower or "promotion" in lower:
        pillar = "pricing"
    else:
        pillar = "off_thesis"
    marked = any(tag in text for tag in ("[new]", "[maybe]", "[contradicts]"))
    if "[new]" in text or "[contradicts]" in text:
        new_info = 0.9
    elif "[maybe]" in text:
        new_info = 0.5
    else:
        new_info = 0.1
    answers = {
        "pillar": choice_from(questions["pillar"], pillar, 0.9),
        "boilerplate": noul(0.05),
        "new_info": noul(new_info),
        "materiality": score([0, 0, 0.2, 0.8] if marked else [0.9, 0.1, 0, 0]),
        "stance": score([0, 0, 1, 0, 0]),
        "evidence": choice_from(questions["evidence"], "management_commentary", 0.8),
        "forward_looking": noul(0.5),
    }
    for name in questions:
        if name.startswith("assumption__"):
            answers[name] = choice_from(questions[name], "contradicts" if "[contradicts]" in text else "neither", 0.9)
    return answers


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "research"
    (workspace / "inbox").mkdir(parents=True)
    write_thesis(workspace / "thesis")
    (workspace / "inbox" / "acme-call.txt").write_text(CALL, encoding="utf-8")
    (workspace / "inbox" / "acme-note.md").write_text(NOTE, encoding="utf-8")
    (workspace / "inbox" / "weather.txt").write_text(WEATHER, encoding="utf-8")
    return workspace


def radar(ws, *argv, fake=True):
    out, err = io.StringIO(), io.StringIO()
    factory = (lambda: FakeJudge(responder, model="jev-1.13.0")) if fake else None
    code = main(["--workspace", str(ws), *argv], judge_factory=factory, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def dashboard(ws):
    return payload_from_html((ws / "dashboard.html").read_text(encoding="utf-8"))


def test_run_builds_a_dashboard_end_to_end(ws):
    code, out, err = radar(ws, "run")
    assert code == 0, err
    assert "fetch: skipped" in out
    assert "ingest: 2 sorted, 1 unsorted" in out
    payload = dashboard(ws)
    passages = payload["companies"][0]["passages"]

    def find(tag):
        return next(p for p in passages if tag in p["text"])

    assert find("[contradicts]")["in_contradictions"]
    assert find("[new]")["in_whats_new"] and find("[new]")["speaker"] == "John Roe - CEO"
    assert find("[maybe]")["in_maybe"]
    assert [u["title"] for u in payload["unsorted"]] == ["Local weather for Tuesday: sunny."]
    assert payload["pending"] == {"unjudged": 0, "failed": 0}
    assert sorted(p.name for p in (ws / "inbox").iterdir()) == ["_failed"]
    assert len(list((ws / "archive" / "ACME").iterdir())) == 2


def test_second_run_reuses_cached_judgments(ws):
    radar(ws, "run")
    code, out, _ = radar(ws, "run")
    assert code == 0 and "judge: 0 passages to judge" in out


def test_absorb_and_tag(ws):
    radar(ws, "run")
    payload = dashboard(ws)
    new_id = next(p["id"] for p in payload["companies"][0]["passages"] if "[new]" in p["text"])
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


def test_calibrate_without_labels(ws):
    code, out, _ = radar(ws, "calibrate")
    assert code == 0 and "No labels yet" in out


def test_usage_errors_exit_1(ws):
    assert radar(ws, "frobnicate")[0] == 1


def test_judge_reports_skipped_repeats(ws):
    radar(ws, "ingest")
    code, out, _ = radar(ws, "judge", "--dry-run")
    assert code == 0 and "judge: 6 passages to judge" in out and "0 repeated passages skipped" in out
