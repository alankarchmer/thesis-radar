import asyncio
import json
from dataclasses import dataclass
from datetime import date

import pytest
from helpers import answer_all, write_thesis

from thesis_radar import thesis_edit
from thesis_radar.app import App
from thesis_radar.apply import ActionError, apply_actions, parse_actions
from thesis_radar.config import Workspace
from thesis_radar.judge import FakeJudge
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.policy import Policy, load_policy
from thesis_radar.runner import RateLimiter, passage_keys, run_judging
from thesis_radar.store import Store
from thesis_radar.thesis import load_thesis

TODAY = date(2026, 9, 24)
THESIS = """\
# ACME thesis
ticker: ACME
company: ACME Snowmobiles Inc.
aliases: [ACME]
peers: [BRP]
pillars:
  inventory: Units on dealer lots and how fast they sell through.
  pricing: List pricing, promotions, and discounting.
assumptions:
  inv_normalizes: {pillar: inventory, statement: "Dealer inventory returns to normal within two quarters."}
predictions:
  inv_back: {statement: "Inventory is normal by Q1.", by: 2027-03-31, p: 0.6, pillar: inventory}
known_facts:
  inventory:
    - "Dealer inventory was elevated at the end of Q2."  # Q2 call
"""


@dataclass
class Corpus:
    acme_doc: int
    acme: list[int]
    peer_doc: int
    peer: list[int]
    other_doc: int
    other: int
    unsorted: int


def _add(store, digest, ticker, doc_date, texts, *, status="sorted"):
    doc = store.insert_document(
        NewDocument(
            text_sha256=digest * 64, path=f"archive/{ticker}/{digest}.txt", title=f"{ticker} {doc_date}",
            origin="inbox", status=status, ticker=ticker if status == "sorted" else None,
            source_type="earnings_transcript", doc_date=doc_date,
        )
    )
    ids = store.insert_passages(doc, [PassageDraft(i, 1, 0, len(t), t) for i, t in enumerate(texts)])
    return doc, ids


def build_app(tmp_path, thesis_text=THESIS):
    """A workspace with ACME (peer BRP) judged by the fake judge, plus unrelated and unsorted documents."""
    ws = Workspace(tmp_path / "ws")
    ws.ensure_layout()
    write_thesis(ws.thesis_dir, text=thesis_text)
    store = Store(ws.db_path)
    with store.transaction():
        acme_doc, acme = _add(store, "a", "ACME", "2026-09-15", ["Dealer inventory fell 10% in the quarter.", "Pricing held."])
        peer_doc, peer = _add(store, "b", "BRP", "2026-09-10", ["BRP dealers cut snowmobile orders."])
        other_doc, [other] = _add(store, "c", "ZZZ", "2026-09-12", ["Unrelated company news."])
        _, [unsorted] = _add(store, "d", "ACME", "2026-09-13", ["Unsorted inventory note."], status="unsorted")
    store.close()
    app = App.open(ws, today=TODAY)
    plan = app.plan()
    asyncio.run(
        run_judging(app.store, FakeJudge(answer_all), plan.pending, concurrency=4, limiter=RateLimiter(60_000))
    )
    return app, Corpus(acme_doc, acme, peer_doc, peer, other_doc, other, unsorted)


@pytest.fixture
def workspace(tmp_path):
    app, corpus = build_app(tmp_path)
    yield app, corpus
    app.close()


def test_parse_actions_accepts_an_array_or_an_object():
    action = {"op": "triage", "ticker": "ACME", "passage_id": 1, "starred": True}
    assert parse_actions(json.dumps([action])) == [action]
    assert parse_actions(json.dumps({"actions": [action]})) == [action]
    assert parse_actions("[]") == []


@pytest.mark.parametrize("text", ["not json", "5", '{"ops": []}', '{"actions": {}}', "[1]", '[{"op": "x"}, "y"]'])
def test_parse_actions_rejects_anything_else(text):
    with pytest.raises(ActionError):
        parse_actions(text)


def test_triage_sets_merges_and_clears(workspace):
    app, c = workspace
    pid = c.acme[0]
    [result] = apply_actions(app, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "dismissed"}])
    assert result == {"op": "triage", "ok": True, "ticker": "ACME", "passage_id": pid, "status": "dismissed", "starred": False}
    apply_actions(app, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": True}])
    assert app.store.triage_map("ACME") == {pid: {"status": "dismissed", "starred": True}}
    apply_actions(app, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "status": None}])
    assert app.store.triage_map("ACME") == {pid: {"status": None, "starred": True}}
    apply_actions(app, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": False}])
    assert app.store.triage_map("ACME") == {}


def test_triage_accepts_peer_passages_under_the_host_thesis(workspace):
    app, c = workspace
    apply_actions(app, [{"op": "triage", "ticker": "ACME", "passage_id": c.peer[0], "status": "acknowledged"}])
    assert app.store.triage_map("ACME") == {c.peer[0]: {"status": "acknowledged", "starred": False}}


def test_labels_store_the_judgment_keys_current_when_made(workspace):
    app, c = workspace
    pid = c.acme[0]
    thesis = app.theses["ACME"]
    keys = passage_keys(app.store, thesis, pid, app.config.model)
    assert keys == app.plan().keys("ACME", pid) and all(app.store.has_judged(pid, key) for key in keys)
    results = apply_actions(
        app,
        [
            {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "new_info", "value": 1},
            {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "whats_new", "value": False,
             "origin": "spotcheck"},
            {"op": "label", "ticker": "ACME", "passage_id": c.peer[0], "question": "contradicts__inv_normalizes",
             "value": True},
        ],
    )
    assert [r["question"] for r in results] == ["new_info", "whats_new", "contradicts__inv_normalizes"]
    labels = {(row["passage_id"], row["question"]): row for row in app.store.labels("ACME")}
    assert len(labels) == 3
    row = labels[(pid, "new_info")]
    assert (row["value"], row["origin"], row["weight"]) == (1, "triage", 1.0)
    assert json.loads(row["judgment_keys"]) == keys
    assert (labels[(pid, "whats_new")]["value"], labels[(pid, "whats_new")]["origin"]) == (0, "spotcheck")
    peer_keys = passage_keys(app.store, thesis, c.peer[0], app.config.model)
    assert json.loads(labels[(c.peer[0], "contradicts__inv_normalizes")]["judgment_keys"]) == peer_keys


def test_labels_keep_the_keys_from_before_a_fact_in_the_same_batch(workspace):
    app, c = workspace
    pid = c.acme[0]
    before = passage_keys(app.store, app.theses["ACME"], pid, app.config.model)
    apply_actions(
        app,
        [
            {"op": "fact", "ticker": "ACME", "pillar": "inventory", "text": "Dealer inventory fell 10%."},
            {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "new_info", "value": 1},
        ],
    )
    after = passage_keys(app.store, app.theses["ACME"], pid, app.config.model)
    assert after != before
    assert json.loads(app.store.labels("ACME")[0]["judgment_keys"]) == before


def test_fact_appends_and_reloads_the_thesis(workspace):
    app, _ = workspace
    [result] = apply_actions(app, [{"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": " List prices rose 3%. "}])
    assert result == {"op": "fact", "ok": True, "ticker": "ACME", "fact_id": "pricing.0"}
    assert app.theses["ACME"].fact("pricing.0").text == "List prices rose 3%."
    text = app.ws.thesis_path("ACME").read_text()
    assert text.startswith("# ACME thesis\n") and "# Q2 call" in text


def test_fact_from_a_source_passage_is_dated_by_its_document(workspace):
    app, c = workspace
    [result] = apply_actions(
        app,
        [{"op": "fact", "ticker": "ACME", "pillar": "inventory", "text": "Inventory fell 10%.", "source": c.acme[0],
          "replace": "inventory.0"}],
    )
    assert result["fact_id"] == "inventory.0"
    fact = app.theses["ACME"].fact("inventory.0")
    assert (fact.text, fact.as_of, fact.source) == ("Inventory fell 10%.", "2026-09-15", c.acme[0])
    [peer] = apply_actions(
        app,
        [{"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "BRP cut orders.", "source": c.peer[0],
          "as_of": "2026-09-11"}],
    )
    fact = app.theses["ACME"].fact(peer["fact_id"])
    assert (fact.as_of, fact.source) == ("2026-09-11", c.peer[0])


def test_fact_replace_across_pillars_moves_it(workspace):
    app, _ = workspace
    [result] = apply_actions(
        app, [{"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "Moved.", "replace": "inventory.0"}]
    )
    assert result["fact_id"] == "pricing.0"
    thesis = app.theses["ACME"]
    assert thesis.known_facts["inventory"] == () and thesis.fact("pricing.0").text == "Moved."


def test_resolve_records_and_clears_an_outcome(workspace):
    app, _ = workspace
    [result] = apply_actions(app, [{"op": "resolve", "ticker": "ACME", "prediction_id": "inv_back", "outcome": True}])
    assert result == {"op": "resolve", "ok": True, "ticker": "ACME", "prediction_id": "inv_back", "outcome": True}
    assert app.store.resolutions("ACME")["inv_back"]["outcome"] is True
    apply_actions(app, [{"op": "resolve", "ticker": "ACME", "prediction_id": "inv_back", "outcome": False}])
    assert app.store.resolutions("ACME")["inv_back"]["outcome"] is False
    apply_actions(app, [{"op": "resolve", "ticker": "ACME", "prediction_id": "inv_back", "outcome": None}])
    assert app.store.resolutions("ACME") == {}


def test_policy_writes_a_loadable_file_and_reloads(workspace):
    app, _ = workspace
    results = apply_actions(
        app,
        [
            {"op": "policy", "values": {"new_info_min": 0.7, "whats_new_window_days": 45}},
            {"op": "policy", "values": {"maybe_new_info_low": 0.5, "maybe_new_info_high": 0.7}},
        ],
    )
    expected = Policy(new_info_min=0.7, whats_new_window_days=45, maybe_new_info_low=0.5, maybe_new_info_high=0.7)
    assert results[-1] == {"op": "policy", "ok": True, "policy": expected.flat()}
    assert load_policy(app.ws.policy_path) == expected
    assert app.policy == expected
    assert [p.name for p in app.ws.root.iterdir() if p.name.startswith(".")] == []


def _invalid_cases(c):
    triage = {"op": "triage", "ticker": "ACME", "passage_id": c.acme[0]}
    label = {"op": "label", "ticker": "ACME", "passage_id": c.acme[0], "question": "new_info", "value": 1}
    fact = {"op": "fact", "ticker": "ACME", "pillar": "inventory", "text": "A fact."}
    resolve = {"op": "resolve", "ticker": "ACME", "prediction_id": "inv_back", "outcome": True}
    return [
        ("not an object", "must be an object"),
        ({"op": "delete"}, "unknown op 'delete'"),
        ({**triage, "ticker": "ZZZ", "status": "dismissed"}, "unknown ticker 'ZZZ'"),
        ({**triage, "passage_id": c.other, "status": "dismissed"}, "not from ACME or its peers"),
        ({**triage, "passage_id": c.unsorted, "status": "dismissed"}, "not from ACME or its peers"),
        ({**triage, "passage_id": 99_999, "status": "dismissed"}, "no passage 99999"),
        ({**triage, "passage_id": str(c.acme[0]), "status": "dismissed"}, "passage_id must be a passage id"),
        ({**triage, "status": "done"}, "status must be one of"),
        (triage, "give status, starred, or both"),
        ({**triage, "starred": "yes"}, "starred must be true or false"),
        ({**label, "passage_id": c.other}, "not from ACME or its peers"),
        ({**label, "question": "bogus"}, "question must be one of"),
        ({**label, "question": "contradicts__nope"}, "contradicts__inv_normalizes; got 'contradicts__nope'"),
        ({**label, "value": 2}, "value must be 0 or 1"),
        ({**label, "value": 0.5}, "value must be 0 or 1"),
        ({**label, "origin": "sample"}, "origin must be one of"),
        ({**fact, "pillar": "margins"}, "unknown pillar 'margins'"),
        ({**fact, "text": "  "}, "text must be non-empty"),
        ({**fact, "text": "x" * 301}, "at most 300"),
        ({**fact, "replace": "inventory.1"}, "no known fact 'inventory.1'"),
        ({**fact, "replace": "inventory"}, "no known fact 'inventory'"),
        ({**fact, "source": c.other}, "passage .* is not from ACME"),
        ({**fact, "as_of": "yesterday"}, "as_of must be a date"),
        ({**resolve, "prediction_id": "nope"}, "unknown prediction 'nope'"),
        ({**resolve, "outcome": "yes"}, "outcome must be true, false, or null"),
        ({k: v for k, v in resolve.items() if k != "outcome"}, "outcome is required"),
        ({"op": "policy", "values": {"bogus": 1}}, "unknown setting bogus"),
        ({"op": "policy", "values": {"new_info_min": 2}}, "between 0 and 1"),
        ({"op": "policy", "values": {"maybe_new_info_low": 0.9}}, "low < high"),
        ({"op": "policy", "values": [1]}, "values must be an object"),
    ]


def test_every_invalid_action_is_rejected_before_anything_applies(workspace):
    app, c = workspace
    thesis_text = app.ws.thesis_path("ACME").read_text()
    first = {"op": "triage", "ticker": "ACME", "passage_id": c.acme[1], "status": "dismissed"}
    for bad, message in _invalid_cases(c):
        with pytest.raises(ActionError, match=message) as caught:
            apply_actions(app, [first, bad])
        assert str(caught.value).startswith("action 1"), caught.value
    assert app.store.triage_map("ACME") == {}
    assert app.ws.thesis_path("ACME").read_text() == thesis_text
    assert not app.ws.policy_path.exists()


def test_a_batch_with_one_bad_action_applies_nothing(workspace):
    app, c = workspace
    thesis_text = app.ws.thesis_path("ACME").read_text()
    good = [
        {"op": "triage", "ticker": "ACME", "passage_id": c.acme[0], "status": "absorbed", "starred": True},
        {"op": "label", "ticker": "ACME", "passage_id": c.acme[0], "question": "material", "value": 1},
        {"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "A fact."},
        {"op": "resolve", "ticker": "ACME", "prediction_id": "inv_back", "outcome": True},
        {"op": "policy", "values": {"new_info_min": 0.7}},
    ]
    bad = {"op": "label", "ticker": "ACME", "passage_id": c.other, "question": "material", "value": 1}
    with pytest.raises(ActionError, match="action 5 \\(label\\)"):
        apply_actions(app, [*good, bad])
    assert app.store.triage_map("ACME") == {}
    assert app.store.labels() == []
    assert app.store.resolutions("ACME") == {}
    assert app.ws.thesis_path("ACME").read_text() == thesis_text
    assert not app.ws.policy_path.exists()
    assert len(apply_actions(app, good)) == 5


def test_fact_limits_and_ids_follow_earlier_actions_in_the_batch(tmp_path):
    facts = "".join(f'    - "Fact {i}."\n' for i in range(19))
    text = THESIS.split("known_facts:")[0] + "known_facts:\n  inventory:\n" + facts
    app, _ = build_app(tmp_path, text)
    add = {"op": "fact", "ticker": "ACME", "pillar": "inventory", "text": "Twentieth."}
    try:
        with pytest.raises(ActionError, match="action 1 \\(fact\\): inventory already has 20 known facts"):
            apply_actions(app, [add, {**add, "text": "Twenty-first."}])
        assert len(app.theses["ACME"].known_facts["inventory"]) == 19
        results = apply_actions(app, [add, {**add, "text": "Replaced.", "replace": "inventory.19"}])
        assert [r["fact_id"] for r in results] == ["inventory.19", "inventory.19"]
        assert app.theses["ACME"].fact("inventory.19").text == "Replaced."
    finally:
        app.close()


def test_a_fact_edit_failing_at_apply_time_is_an_action_error(workspace, monkeypatch):
    app, c = workspace

    def refuse(*args, **kwargs):
        raise ValueError("disk says no")

    monkeypatch.setattr(thesis_edit, "add_fact", refuse)
    with pytest.raises(ActionError, match="action 1 \\(fact\\): disk says no; the 1 action\\(s\\) before it"):
        apply_actions(
            app,
            [
                {"op": "triage", "ticker": "ACME", "passage_id": c.acme[0], "starred": True},
                {"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "A fact."},
            ],
        )
    assert app.store.triage_map("ACME") == {c.acme[0]: {"status": None, "starred": True}}
    assert load_thesis(app.ws.thesis_path("ACME")).known_facts.get("pricing", ()) == ()
