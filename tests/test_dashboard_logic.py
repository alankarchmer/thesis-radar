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


@pytest.mark.parametrize(
    "needle", ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "http://", "https://"]
)
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
              L.recent(base, 4.9, "2026-09-24"), L.monthsBack("2026-02-10", 4), L.monthLabel("2026-08"),
              L.formatDate("2026-09-05"), L.shortDate("2026-09-05"), L.formatDate(null)];
    """
    assert run_logic(tmp_path, body, None) == [
        True,
        False,
        "2026-09-20",
        True,
        True,
        ["2025-11", "2025-12", "2026-01", "2026-02"],
        "Aug 2026",
        "Sep 5, 2026",
        "Sep 5",
        "Undated",
    ]


@needs_node
def test_why_shown_names_each_rule(tmp_path):
    [contra_case] = [c for c in CASES if c["name"] == "contradiction is pinned and not repeated in whats_new"]
    [old_case] = [c for c in CASES if c["name"] == "one day past the whats_new window is out but still flagged"]
    body = """
      const names = {assumptions: {inv_normalizes: "Dealer inventory normalizes."}, pillars: {dealer_inventory: "Dealer inventory"}};
      return input.map((c) => L.whyShown(c.passage, c.policy, c.today, names)).concat([L.whyShown(input[0].passage, input[0].policy, input[0].today)]);
    """
    contra, old, bare = run_logic(tmp_path, body, [contra_case, old_case])
    assert contra[0] == "Shown under Contradictions."
    assert "✓ Contradicts “Dealer inventory normalizes.” (85% likely; threshold 70%)" in contra
    assert "✓ Contradicts inv_normalizes (85% likely; threshold 70%)" in bare
    assert old[0] == "Flagged, but not in a feed right now."
    assert "✗ Dated Aug 24, 2026, outside the 30-day What's new window" in old
    assert "✓ Not boilerplate (5%; limit 30%)" in old
    assert "✓ About “Dealer inventory” (90% likely; minimum 50%)" in old


# ---------------------------------------------------------------- helpers


@needs_node
def test_shell_quote_round_trips_single_quotes(tmp_path):
    texts = ["it's", "'", "a 'quoted' $HOME `x` \\ \"double\"", "", "line\nbreak"]
    actions = [{"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "Dealers' incentives 'up' $1,450"}]
    quoted, command = run_logic(
        tmp_path,
        "return [input.texts.map(L.shellQuote), L.applyCommand(input.actions)];",
        {"texts": texts, "actions": actions},
    )
    for text, q in zip(texts, quoted, strict=True):
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
  boilerplate: {max: 0.5}
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
metrics:
  min_probability: 0.6
"""
    body = "return [L.policyYaml(L.DEFAULT_POLICY), L.policyYaml(Object.assign({}, L.DEFAULT_POLICY, input))];"
    defaults, changed = run_logic(
        tmp_path, body, {"new_info_min": 0.55, "whats_new_window_days": 45, "materiality_min": 2}
    )
    assert defaults == expected
    assert (
        "  new_info: {min: 0.55}" in changed
        and "  window_days: 45" in changed
        and "  materiality: {min: 2.0}" in changed
    )


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
        {
            "text": "Retail was softer.\nWe asked dealers to hold orders.",
            "speaker": "Jane Doe - CEO",
            "title": "Q3 call",
            "source_type": "earnings_transcript",
            "date": "2026-09-15",
            "page": 2,
            "link": "file:///r/call.pdf#page=2",
        },
        {
            "text": "Lot had 60+ sleds.",
            "speaker": None,
            "title": "Dealer visit",
            "source_type": "own_note",
            "date": "2026-09-22",
            "page": None,
            "link": "javascript:alert(1)",
        },
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


# ---------------------------------------------------------------- verdicts, tone, words, metrics

TODAY = "2026-09-24"


def evidence_passage(pid, date, *, supports=0.0, contradicts=0.0, materiality=2.0, boilerplate=0.05, false_alarms=()):
    return {
        "id": pid,
        "date": date,
        "ingested_at": date + "T08:00:00Z",
        "triage": {"status": None, "starred": False, "false_alarms": list(false_alarms)},
        "p": {
            "pillar": "inv",
            "pillar_p": 0.9,
            "boilerplate": boilerplate,
            "new_info": 0.5,
            "materiality": materiality,
            "stance": 2.0,
            "assumptions": {"a": {"supports": supports, "contradicts": contradicts}},
            "questions": {},
        },
    }


def verdict_of(tmp_path, passages):
    body = f'return L.assumptionEvidence(input, "a", L.DEFAULT_POLICY, "{TODAY}");'
    return run_logic(tmp_path, body, passages)


def pro(pid, date, **kw):
    return evidence_passage(pid, date, supports=0.8, **kw)


def con(pid, date, **kw):
    return evidence_passage(pid, date, contradicts=0.8, **kw)


RECENT, EARLIER = "2026-08-01", "2026-01-15"


@needs_node
@pytest.mark.parametrize(
    ("passages", "kind", "label"),
    [
        ([], "quiet", "Quiet"),
        ([pro(1, EARLIER)], "quiet", "Quiet"),
        ([pro(1, RECENT), pro(2, RECENT), pro(3, RECENT), con(4, RECENT)], "holding", "Holding"),
        ([con(1, RECENT), con(2, RECENT), pro(3, RECENT)], "contradicted", "Contradicted"),
        ([pro(1, RECENT), con(2, RECENT)], "mixed", "Mixed"),
        (
            [con(1, EARLIER), con(2, EARLIER), pro(3, RECENT), pro(4, RECENT), con(5, RECENT)],
            "turning_for",
            "Turning toward support",
        ),
        (
            [pro(1, EARLIER), pro(2, EARLIER), con(3, RECENT), con(4, RECENT), pro(5, RECENT)],
            "turning_against",
            "Turning against",
        ),
        # An earlier record that already leaned the same way is not a turn.
        ([con(1, EARLIER), pro(2, RECENT), pro(3, RECENT)], "turning_for", "Turning toward support"),
        ([pro(1, EARLIER), pro(2, RECENT), pro(3, RECENT)], "holding", "Holding"),
    ],
    ids=[
        "no-evidence",
        "only-earlier",
        "holding",
        "contradicted",
        "mixed",
        "turning-for",
        "turning-against",
        "turn-from-against",
        "steady",
    ],
)
def test_assumption_verdicts(tmp_path, passages, kind, label):
    ev = verdict_of(tmp_path, passages)
    assert (ev["kind"], ev["label"]) == (kind, label)


@needs_node
def test_verdict_notes_and_window_edges(tmp_path):
    # 182 days before today is recent; 183 is earlier; 364 is earlier; 365 is outside both windows.
    ev = verdict_of(tmp_path, [pro(1, "2026-03-26"), con(2, "2026-03-25"), con(3, "2025-09-25"), con(4, "2025-09-24")])
    assert (ev["recentFor"], ev["recentAgainst"], ev["earlierFor"], ev["earlierAgainst"]) == (1, 0, 0, 2)
    assert ev["kind"] == "turning_for" and ev["note"] == "Based on 1 passage in the last six months"
    assert ev["forIds"] == [1] and ev["againstIds"] == [2, 3, 4]
    assert verdict_of(tmp_path, [])["note"] == "No evidence yet"
    assert verdict_of(tmp_path, [pro(1, EARLIER)])["note"] == "No evidence in the last six months"
    four = verdict_of(tmp_path, [pro(i, RECENT) for i in range(4)])
    assert four["note"] is None and four["share"] == 1


@needs_node
def test_verdict_gates_and_false_alarms(tmp_path):
    ev = verdict_of(
        tmp_path,
        [
            con(1, RECENT, materiality=0.9),  # below contradiction_materiality_min
            con(2, RECENT, boilerplate=0.6),  # above contradiction_boilerplate_max
            con(3, RECENT, false_alarms=["a"]),  # the user said it is not a contradiction
            con(4, RECENT, false_alarms=["other"]),
            evidence_passage(5, RECENT, supports=0.69),  # below contradicts_min
            dict(pro(6, RECENT), p=None),  # unjudged
            dict(
                con(7, RECENT), triage={"status": "acknowledged", "starred": False}
            ),  # triage does not remove evidence
        ],
    )
    assert ev["againstIds"] == [7, 4] and ev["forIds"] == []
    assert ev["kind"] == "contradicted"


@needs_node
def test_verdict_months_cover_the_last_twelve(tmp_path):
    ev = verdict_of(tmp_path, [pro(1, "2026-09-01"), con(2, "2026-09-20"), con(3, "2025-10-02"), pro(4, "2025-09-30")])
    months = ev["months"]
    assert [m["month"] for m in months][0] == "2025-10" and months[-1]["month"] == "2026-09" and len(months) == 12
    assert months[-1] == {"month": "2026-09", "for": 1, "against": 1}
    assert months[0] == {"month": "2025-10", "for": 0, "against": 1}


EXPECTED_VERDICTS = {
    ("ACME", "inv_normalizes"): "contradicted",
    ("ACME", "pricing_holds"): "holding",
    ("ACME", "margin_recovers"): "turning_against",
    ("ACME", "ev_on_track"): "turning_for",
    ("BRRR", "share_gains"): "mixed",
    ("BRRR", "lean_channel"): "holding",
    ("BRRR", "dealer_adds"): "quiet",
}


@needs_node
def test_sample_payload_shows_every_verdict_kind(tmp_path):
    body = """
      const out = {};
      for (const c of input.companies) for (const a of c.assumptions) {
        const ev = L.assumptionEvidence(c.passages, a.id, input.policy, input.today);
        out[c.ticker + "|" + a.id] = ev.kind;
      }
      // Without the user's false alarm, the pricing assumption would only be mixed.
      const acme = input.companies[0];
      const cleared = acme.passages.map((p) => Object.assign({}, p, {triage: Object.assign({}, p.triage, {false_alarms: []})}));
      out.without_false_alarm = L.assumptionEvidence(cleared, "pricing_holds", input.policy, input.today).kind;
      return out;
    """
    got = run_logic(tmp_path, body, SAMPLE)
    assert {k: v for k, v in got.items() if "|" in k} == {f"{t}|{a}": v for (t, a), v in EXPECTED_VERDICTS.items()}
    assert set(EXPECTED_VERDICTS.values()) == {
        "holding",
        "contradicted",
        "mixed",
        "turning_for",
        "turning_against",
        "quiet",
    }
    assert got["without_false_alarm"] == "mixed"


@needs_node
def test_tone_by_pillar_and_read_next(tmp_path):
    body = """
      const acme = input.companies[0];
      const tone = L.toneByPillar(acme.passages, Object.keys(acme.pillars), input.policy, input.today);
      const cell = tone.rows.dealer_inventory[11];
      return {months: tone.months, cell: {net: cell.net, count: cell.count}, empty: tone.rows.demand[0],
              words: [-0.8, -0.3, 0, 0.3, 0.9, null].map(L.toneWord),
              next: L.readNext(acme.passages, input.policy, input.today, 3)};
    """
    got = run_logic(tmp_path, body, SAMPLE)
    assert got["months"][0] == "2025-10" and got["months"][-1] == "2026-09"
    assert got["cell"]["count"] > 0 and got["cell"]["net"] < 0 and got["empty"] is None
    assert got["words"] == [
        "Clearly negative",
        "Negative",
        "Mixed or neutral",
        "Positive",
        "Clearly positive",
        "No passages",
    ]
    assert got["next"] == [
        {"id": 1061, "kind": "contradiction"},
        {"id": 1041, "kind": "contradiction"},
        {"id": 1046, "kind": "contradiction"},
    ]


@needs_node
def test_plain_words_for_scores_and_sources(tmp_path):
    body = """
      return {stance: [0, 0.6, 1.4, 2, 2.6, 3.5, 4, null].map(L.stanceWord),
              materiality: [0, 0.4, 0.5, 1.6, 2.8, 5].map(L.materialityWord),
              evidence: ["channel_or_customer_data", "reported_result", "odd_kind"].map(L.evidenceWord),
              sources: [["filing", "8-K"], ["earnings_transcript", null], ["own_note", null], ["sell_side", null],
                        ["expert_call", null], ["ai_research", null], ["news", null], ["filing", null]].map(([s, f]) => L.sourceName(s, f)),
              pillar: L.pillarName("dealer_inventory")};
    """
    got = run_logic(tmp_path, body, None)
    assert got["stance"] == [
        "Clearly negative",
        "Negative",
        "Negative",
        "Neutral",
        "Positive",
        "Clearly positive",
        "Clearly positive",
        None,
    ]
    assert got["materiality"] == ["No bearing", "No bearing", "Minor", "Meaningful", "Major", "Major"]
    assert got["evidence"] == ["Channel data", "Reported result", "odd kind"]
    assert got["sources"] == [
        "8-K",
        "Earnings call",
        "Your note",
        "Sell-side",
        "Expert call",
        "AI research",
        "News",
        "Filing",
    ]
    assert got["pillar"] == "Dealer inventory"


@needs_node
def test_exhibit_wrappers_and_number_heavy_passages(tmp_path):
    texts = [
        "EX-99.1 2 pii-q22026earningsrelease.htm EX-99.1 Document Polaris reports results.",
        "  ex-99.2 3 exhibit992.html Exhibit text follows.",
        "EX-99.1 is mentioned in the middle, so nothing is removed.",
        "Net sales 1,184.2 1,301.5 (9)% North America 918.4 1,032.1 (11)%",
        "Promotional spending increased to 9.5% of sales from 7.1% a year ago.",
        "",
    ]
    body = "return {stripped: input.map(L.stripWrapper), numeric: input.map(L.isNumberHeavy)};"
    got = run_logic(tmp_path, body, texts)
    assert got["stripped"][:3] == ["Polaris reports results.", "Exhibit text follows.", texts[2]]
    assert got["numeric"] == [False, False, False, True, False, False]


@needs_node
def test_metric_formatting(tmp_path):
    body = """
      const pt = (value, high, unit) => ({value, high, unit});
      return {
        values: [L.formatValue(20.4, "%"), L.formatValue(-6, "%"), L.formatValue(1184.2, "$M"), L.formatValue(-3.5, "$B"),
                 L.formatValue(12400, "units"), L.formatValue(1.8, "x"), L.formatValue(92, "days"), L.formatValue(7, "")],
        ranges: [L.formatRange(pt(20, 21, "%")), L.formatRange(pt(-5, -3, "%")), L.formatRange(pt(1100, 1200, "$M")),
                 L.formatRange(pt(21, null, "%")), L.formatRange(pt(21, 21, "%"))],
        changes: [L.formatChange(-1.2, "%"), L.formatChange(0.5999999999999979, "%"), L.formatChange(1, "%"),
                  L.formatChange(4, "%"), L.formatChange(-12.345, "$M"), L.formatChange(0, "%"), L.formatChange(null, "%")],
        words: [L.vsGuidanceWord("below"), L.vsEstimatesWord("above", "good"), L.vsEstimatesWord("above", "bad"),
                L.vsEstimatesWord("in_line", "good"), L.guidanceChangeWord("lowered"), L.guidanceChangeWord("narrowed")],
        goodness: [L.goodness("up", "good"), L.goodness("up", "bad"), L.goodness("down", "bad"), L.goodness("up", "neutral"),
                   L.goodness(L.movementOf("lowered"), "good"), L.goodness(L.movementOf("within"), "good")],
        ticks: [L.niceTicks(18.5, 23, 4), L.niceTicks(-6.2, -1.8, 4), L.niceTicks(5, 5, 4)],
      };
    """
    got = run_logic(tmp_path, body, None)
    assert got["values"] == ["20.4%", "−6%", "$1,184.2M", "−$3.5B", "12,400 units", "1.8x", "92 days", "7"]
    assert got["ranges"] == ["20–21%", "−5% to −3%", "$1,100M to $1,200M", "21%", "21%"]
    assert got["changes"] == ["down 1.2 pts", "up 0.6 pts", "up 1 pt", "up 4 pts", "down $12.35M", "unchanged", None]
    assert got["words"] == [
        "Below guidance",
        "Beat estimates",
        "Above estimates",
        "In line with estimates",
        "Guidance cut",
        "Guidance narrowed",
    ]
    assert got["goodness"] == ["good", "bad", "good", "neutral", "bad", "neutral"]
    assert got["ticks"] == [[18, 20, 22, 24], [-8, -6, -4, -2, 0], [4.5, 5, 5.5]]


@needs_node
def test_metric_summaries_and_guidance_trails_on_the_sample(tmp_path):
    body = """
      const m = input.companies[0].metrics;
      return {summaries: m.map(L.metricSummary), trails: m[0].periods.map(L.guidanceTrail), retail: m[2].periods.map(L.guidanceTrail)};
    """
    got = run_logic(tmp_path, body, SAMPLE)
    assert got["summaries"] == [
        "Gross margin 20.4% in Q3 2026, up 0.6 pts; within guidance; missed estimates; guidance cut",
        "Dealer inventory, change year over year 22% in Q3 2026, up 4 pts; above estimates",
        "North American retail sales, change year over year −6% in Q3 2026, down 1 pt; below guidance",
    ]
    assert got["trails"] == [
        None,
        "22–23% (Oct 21)",
        "21–22% (Feb 20)",
        "18.5–19.5% (May 6)",
        "21–22% → 20–21% (cut Sep 1)",
    ]
    assert got["retail"][3] == "−5% to −3% (Aug 5)"


@needs_node
def test_axis_ticks_are_compact_and_unitless(tmp_path):
    body = "return input.map(([v, u]) => L.formatTick(v, u));"
    cases = [[41000, "units"], [1950, "$M"], [1.92e9, "$"], [20.5, "%"], [-6, "%"], [14, "x"], [112, "days"], [2500000, "units"]]
    assert run_logic(tmp_path, body, cases) == ["41K", "$1,950M", "$1.9B", "20.5%", "−6%", "14x", "112", "2.5M"]


@needs_node
def test_the_latest_guidance_revision_is_reported_for_any_period(tmp_path):
    def guidance(date_):
        return {"value": 20, "high": 21, "unit": "%", "date": date_}

    metric = {
        "id": "gm", "label": "Gross margin", "unit": "%", "higher_is": "good",
        "periods": [
            {"period": "2026-Q3", "label": "Q3 2026", "reported": {"value": 20.6}, "guidance": [], "estimates": [],
             "vs_guidance": None, "vs_estimates": "below", "guidance_change": None},
            {"period": "FY2026", "label": "FY 2026", "reported": None,
             "guidance": [guidance("2026-07-21"), guidance("2026-10-20")], "estimates": [],
             "vs_guidance": None, "vs_estimates": None, "guidance_change": "lowered"},
        ],
        "latest": {"period": "2026-Q3", "label": "Q3 2026", "value": 20.6, "change": -1.2, "direction": "bad"},
    }
    body = "return [L.guidanceChangeText(input, '2026-Q3'), L.guidanceChangeText(input, 'FY2026'), L.metricSummary(input)];"
    chip, same_period, summary = run_logic(tmp_path, body, metric)
    assert chip == "FY 2026 guidance cut" and same_period == "Guidance cut"
    assert summary == "Gross margin 20.6% in Q3 2026, down 1.2 pts; missed estimates; FY 2026 guidance cut"
