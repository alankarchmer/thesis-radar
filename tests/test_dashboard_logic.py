"""Dashboard template: static safety checks, and its RadarLogic script run under node."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
TEMPLATE = TESTS.parent / "thesis_radar" / "dashboard_template.html"
FIXTURES = TESTS / "fixtures"
MARKER = "__RADAR_DATA__"


def _find_node() -> str | None:
    for candidate in (shutil.which("node"), "/opt/node22/bin/node"):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


NODE = _find_node()
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _scripts() -> list[tuple[str, str]]:
    return re.findall(r"<script\b([^>]*)>(.*?)</script>", _template(), re.S)


def _logic_script() -> str:
    [body] = [body for attrs, body in _scripts() if 'id="radar-logic"' in attrs]
    return body


CASES = json.loads((FIXTURES / "classify_cases.json").read_text(encoding="utf-8"))
SAMPLE = json.loads((FIXTURES / "sample_payload.json").read_text(encoding="utf-8"))


def run_logic(directory: Path, body: str, data) -> object:
    """Run `body` in node with `L` bound to RadarLogic and `input` to `data`; return what it prints as JSON."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "logic.js").write_text(_logic_script(), encoding="utf-8")
    (directory / "input.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "main.js").write_text(
        'const L = require("./logic.js");\nconst input = require("./input.json");\n'
        f"const result = (() => {{ {body} }})();\nprocess.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    done = subprocess.run([NODE, "main.js"], cwd=directory, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# ---------------------------------------------------------------- static checks


def test_template_has_exactly_one_marker_inside_the_json_data_block():
    template = _template()
    assert template.count(MARKER) == 1
    assert f'<script id="radar-data" type="application/json">{MARKER}</script>' in template


@pytest.mark.parametrize("needle", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "http://", "https://"])
def test_template_avoids_unsafe_apis_and_network(needle):
    assert needle not in _template()


def test_template_never_evaluates_strings():
    template = _template()
    assert not re.search(r"\beval\b", template)
    assert "new Function" not in template
    assert not re.search(r"set(Timeout|Interval)\(\s*[\"'`]", template)


def test_template_loads_no_external_assets():
    template = _template()
    assert not re.search(r"<(script|img|link|iframe)\b[^>]*\b(src|href)=", template)
    assert "@import" not in template and "url(" not in template


def test_logic_script_comes_before_the_ui_script():
    scripts = _scripts()
    ids = [re.search(r'id="([^"]+)"', attrs).group(1) if "id=" in attrs else "ui" for attrs, _ in scripts]
    assert ids == ["radar-data", "radar-logic", "ui"]
    assert 'if (typeof module !== "undefined") module.exports = RadarLogic;' in _logic_script()
    assert not re.search(r"\b(document|window|localStorage)\.", _logic_script())


@needs_node
def test_inline_scripts_parse(tmp_path):
    for index, (attrs, body) in enumerate(_scripts()):
        if "application/json" in attrs:
            continue
        path = tmp_path / f"script{index}.js"
        path.write_text(body, encoding="utf-8")
        done = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------- classification


def test_there_are_enough_classify_cases():
    assert len(CASES) >= 25
    assert len({case["name"] for case in CASES}) == len(CASES)


@pytest.fixture(scope="module")
def classified(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    return run_logic(
        tmp_path_factory.mktemp("classify"),
        "return input.map((c) => L.classify(c.passage, c.policy, c.today));",
        CASES,
    )


@needs_node
@pytest.mark.parametrize("index", range(len(CASES)), ids=[case["name"] for case in CASES])
def test_classify_case(classified, index):
    assert classified[index] == CASES[index]["expected"]


@needs_node
def test_sample_payload_classified_values_match_the_logic(tmp_path):
    body = """
      const out = [];
      for (const c of input.companies) for (const p of c.passages)
        out.push({ticker: c.ticker, id: p.id, got: L.classify(p, input.policy, input.today), want: p.classified});
      return out;
    """
    for row in run_logic(tmp_path, body, SAMPLE):
        assert row["got"] == row["want"], (row["ticker"], row["id"])


@needs_node
def test_recent_and_acknowledged_helpers(tmp_path):
    body = """
      const base = {date: null, ingested_at: "2026-09-20T23:00:00Z"};
      const contra = {date: "2026-09-20", triage: {status: "acknowledged", starred: false},
        p: {pillar: "x", pillar_p: 0.9, boilerplate: 0, new_info: 0.9, materiality: 2,
            assumptions: {a: {supports: 0, contradicts: 0.9}}, questions: {}}};
      return [L.recent(base, 4, "2026-09-24"), L.recent(base, 3, "2026-09-24"), L.passageDate(base),
              L.isAcknowledgedContradiction(contra, L.DEFAULT_POLICY, "2026-09-24"),
              L.weekStart("2026-W38"), L.weekStart("2026-09-14")];
    """
    assert run_logic(tmp_path, body, None) == [True, False, "2026-09-20", True, "2026-09-14", "2026-09-14"]


@needs_node
def test_why_shown_names_each_rule(tmp_path):
    [contra_case] = [c for c in CASES if c["name"] == "contradiction is pinned and not repeated in whats_new"]
    [old_case] = [c for c in CASES if c["name"] == "one day past the whats_new window is out but still flagged"]
    body = "return input.map((c) => L.whyShown(c.passage, c.policy, c.today));"
    contra, old = run_logic(tmp_path, body, [contra_case, old_case])
    assert contra[0] == "In Contradictions."
    assert any(r.startswith("✓ contradicts inv_normalizes: P 0.85 ≥ 0.70") for r in contra)
    assert old[0] == "Flagged, but not in a feed right now."
    assert "✗ dated 2026-08-24, outside the 30-day What's new window" in old
    assert "✓ boilerplate P 0.05 ≤ 0.30" in old


# ---------------------------------------------------------------- helpers


@needs_node
def test_shell_quote_round_trips_single_quotes(tmp_path):
    texts = ["it's", "'", "a 'quoted' $HOME `x` \\ \"double\"", "", "line\nbreak"]
    actions = [{"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "Dealers' incentives 'up' $1,450"}]
    quoted, command = run_logic(tmp_path, "return [input.texts.map(L.shellQuote), L.applyCommand(input.actions)];",
                                {"texts": texts, "actions": actions})
    for text, q in zip(texts, quoted):
        assert shlex.split(q) == [text]
    argv = shlex.split(command)
    assert argv[:2] == ["radar", "apply"] and json.loads(argv[2]) == actions
    sh = shutil.which("sh")
    if sh:
        done = subprocess.run([sh, "-c", f"printf %s {quoted[2]}"], capture_output=True, text=True, check=True)
        assert done.stdout == texts[2]


@needs_node
def test_policy_yaml_has_the_spec_layout(tmp_path):
    expected = """metadata:
  min_probability: 0.6
whats_new:
  window_days: 30
  pillar_probability: {min: 0.5}
  boilerplate: {max: 0.3}
  new_info: {min: 0.6}
  materiality: {min: 1.5}
contradictions:
  window_days: 120
  contradicts: {min: 0.7}
  materiality: {min: 1.0}
maybe:
  new_info: {between: [0.4, 0.6]}
open_questions:
  min: 0.6
divergence:
  window_days: 120
  min_gap: 1.0
  min_passages: 3
ledger:
  min_probability: 0.6
"""
    body = "return [L.policyYaml(L.DEFAULT_POLICY), L.policyYaml(Object.assign({}, L.DEFAULT_POLICY, input))];"
    defaults, changed = run_logic(tmp_path, body, {"new_info_min": 0.55, "whats_new_window_days": 45, "materiality_min": 2})
    assert defaults == expected
    assert "  new_info: {min: 0.55}" in changed and "  window_days: 45" in changed and "  materiality: {min: 2.0}" in changed


@needs_node
def test_interleave_puts_spot_checks_at_every_tenth_position(tmp_path):
    body = """
      const shape = (n, s) => L.interleave([...Array(n).keys()], s, 10).map((e) => ("spot" in e ? "S" + e.spot : e.item));
      return [shape(25, ["a", "b", "c"]), shape(9, ["a"]), shape(12, [])];
    """
    many, few, none = run_logic(tmp_path, body, None)
    assert [i + 1 for i, e in enumerate(many) if isinstance(e, str)] == [10, 20]
    assert many[9] == "Sa" and many[19] == "Sb" and len(many) == 27
    assert few == list(range(9))
    assert none == list(range(12))


@needs_node
def test_markdown_quotes_are_verbatim_with_citations(tmp_path):
    passages = [
        {"text": "Retail was softer.\nWe asked dealers to hold orders.", "speaker": "Jane Doe - CEO",
         "title": "Q3 call", "source_type": "earnings_transcript", "date": "2026-09-15", "page": 2,
         "link": "file:///r/call.pdf#page=2"},
        {"text": "Lot had 60+ sleds.", "speaker": None, "title": "Dealer visit", "source_type": "own_note",
         "date": "2026-09-22", "page": None, "link": "javascript:alert(1)"},
    ]
    text = run_logic(tmp_path, "return L.markdownQuotes(input);", passages)
    assert text == (
        "> Jane Doe - CEO: Retail was softer.\n> We asked dealers to hold orders.\n\n"
        "— Q3 call, earnings transcript, 2026-09-15, p. 2\nfile:///r/call.pdf#page=2\n\n"
        "> Lot had 60+ sleds.\n\n— Dealer visit, own note, 2026-09-22\n"
    )


@needs_node
def test_brier_score_of_resolved_predictions(tmp_path):
    predictions = [{"p": 0.7, "outcome": True}, {"p": 0.4, "outcome": False}, {"p": 0.9, "outcome": None}]
    score, empty = run_logic(tmp_path, "return [L.brier(input), L.brier([])];", predictions)
    assert score["resolved"] == 2 and score["brier"] == pytest.approx(0.125)
    assert empty == {"resolved": 0, "brier": None}
