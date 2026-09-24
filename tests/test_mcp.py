import asyncio
import io
import json
from datetime import date, timedelta

import pytest
from helpers import ACME_THESIS, answer_all, choice_from, noul, write_thesis

from thesis_radar import __version__
from thesis_radar.config import DEFAULT_MODEL, Workspace
from thesis_radar.judge import FakeJudge
from thesis_radar.mcp_server import TOOLS, run_stdio
from thesis_radar.models import NewDocument, PassageDraft
from thesis_radar.runner import RateLimiter, plan_judging, run_judging
from thesis_radar.similar import link_document
from thesis_radar.store import Store
from thesis_radar.thesis import load_theses

TODAY = date.today()  # the server reads the real clock
THESIS = ACME_THESIS.replace(
    "known_facts:",
    "peers: [BRRR]\n"
    "open_questions:\n  q4_orders: Will dealers cut fourth-quarter orders?\n"
    "predictions:\n"
    '  inv_normal: {statement: "Dealer inventory is back to normal by spring.", by: 2027-03-31, p: 0.6, '
    "pillar: inventory}\n"
    "known_facts:",
)
NEW = {
    "Dealer inventory fell to a record low this quarter.",
    "Promotions rose as pricing weakened across the lineup.",
    "Brrr dealers cut orders for snowmobiles.",
    "Dealer inventory was high last winter.",
}
CONTRA = "Retail demand stayed soft and dealers are holding too many units."


def day(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def respond(request):
    text = request.state["passage"]
    overrides = {}
    if text in NEW:
        overrides["new_info"] = noul(0.9)
    if text == CONTRA and "assumption__inv_normalizes" in request.questions:
        overrides["assumption__inv_normalizes"] = choice_from(
            request.questions["assumption__inv_normalizes"], "contradicts", 0.9
        )
    return answer_all(request, **overrides)


def add_doc(store, digest, texts, *, ticker, date_, path, title, source_type, speakers=None, pages=None):
    speakers = speakers or [None] * len(texts)
    pages = pages or [1] * len(texts)
    with store.transaction():
        doc = store.insert_document(
            NewDocument(text_sha256=digest * 64, path=path, title=title, origin="inbox", status="sorted",
                        ticker=ticker, source_type=source_type, doc_date=date_)
        )
        ids = store.insert_passages(
            doc, [PassageDraft(i, pages[i], 0, len(t), t, speakers[i]) for i, t in enumerate(texts)]
        )
    link_document(store, doc)
    return doc, ids


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("workspace")
    ws = Workspace(root)
    ws.ensure_layout()
    write_thesis(ws.thesis_dir, text=THESIS)
    store = Store(ws.db_path)
    call, call_ids = add_doc(
        store, "a",
        ["Dealer inventory fell to a record low this quarter.", CONTRA, "Safe harbor statement applies to this call."],
        ticker="ACME", date_=day(20), path="archive/ACME/call.pdf", title="ACME Q2 call",
        source_type="earnings_transcript", speakers=["CFO", "CEO", None], pages=[1, 2, 2],
    )
    filing, filing_ids = add_doc(
        store, "b", ["Promotions rose as pricing weakened across the lineup."], ticker="ACME", date_=day(5),
        path="archive/ACME/8k.txt", title="ACME 8-K", source_type="filing",
    )
    peer, peer_ids = add_doc(
        store, "c", ["Brrr dealers cut orders for snowmobiles."], ticker="BRRR", date_=day(3),
        path="archive/BRRR/pr.txt", title="BRRR press release", source_type="filing",
    )
    old, old_ids = add_doc(
        store, "d", ["Dealer inventory was high last winter."], ticker="ACME", date_=day(200),
        path="archive/ACME/old.txt", title="Old note", source_type="own_note",
    )
    theses, errors = load_theses(ws.thesis_dir)
    assert not errors
    plan = plan_judging(store, theses, DEFAULT_MODEL, today=TODAY)
    report = asyncio.run(
        run_judging(store, FakeJudge(respond, model=DEFAULT_MODEL), plan.pending, concurrency=2,
                    limiter=RateLimiter(60_000))
    )
    assert report.failed == 0
    store.close()
    return ws, {
        "call": call, "filing": filing, "peer": peer, "old": old,
        "call_ids": call_ids, "filing_ids": filing_ids, "peer_ids": peer_ids, "old_ids": old_ids,
    }


def rpc(ws, *messages):
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    out = io.StringIO()
    run_stdio(ws, io.StringIO("\n".join(lines) + "\n"), out)
    text = out.getvalue()
    assert text == "" or text.endswith("\n")
    return [json.loads(line) for line in text.splitlines()]


def request(method, params=None, id_=1):
    message = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(ws, name, **arguments):
    [response] = rpc(ws, request("tools/call", {"name": name, "arguments": arguments}))
    result = response["result"]
    if not result.get("isError"):
        [content] = result["content"]
        assert content["type"] == "text" and json.loads(content["text"]) == result["structuredContent"]
    return result


def data(ws, name, **arguments):
    result = call(ws, name, **arguments)
    assert not result.get("isError"), result
    return result["structuredContent"]


def error_text(result):
    assert result["isError"] is True
    return result["content"][0]["text"]


def test_initialize_handshake(corpus):
    ws, _ = corpus
    responses = rpc(
        ws,
        request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        request("ping", id_="two"),
    )
    assert [r["id"] for r in responses] == [1, "two"]
    init = responses[0]["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"] == {"name": "thesis-radar", "version": __version__}
    assert "verbatim" in init["instructions"] and "cite" in init["instructions"]
    assert responses[1] == {"jsonrpc": "2.0", "id": "two", "result": {}}
    [unknown] = rpc(ws, request("initialize", {"protocolVersion": "1999-01-01"}))
    assert unknown["result"]["protocolVersion"] == "2025-06-18"
    [bare] = rpc(ws, request("initialize"))
    assert bare["result"]["protocolVersion"] == "2025-06-18"


def test_tools_list_describes_every_tool(corpus):
    ws, _ = corpus
    [response] = rpc(ws, request("tools/list"))
    tools = response["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "list_companies", "get_thesis", "get_metrics", "whats_new", "contradictions", "search_passages",
        "get_passage", "get_document",
    ]
    for tool in tools:
        schema = tool["inputSchema"]
        assert tool["description"] and schema["type"] == "object"
        assert set(schema.get("required", [])) <= set(schema["properties"])
        assert tool["annotations"]["readOnlyHint"] is True
        json.dumps(tool)
    by_name = {t["name"]: t for t in tools}
    assert by_name["search_passages"]["inputSchema"]["required"] == ["query"]
    assert by_name["get_document"]["inputSchema"]["required"] == ["id", "ticker"]
    assert by_name["whats_new"]["inputSchema"]["properties"]["limit"]["maximum"] == 100


def test_list_companies(corpus):
    ws, _ = corpus
    result = data(ws, "list_companies")
    [acme] = result["companies"]
    assert acme["ticker"] == "ACME" and acme["company"] == "ACME Snowmobiles Inc."
    assert acme["peers"] == ["BRRR"]
    assert set(acme["pillars"]) == {"inventory", "pricing"}
    assert acme["assumptions"] == [
        {"id": "inv_normalizes", "pillar": "inventory",
         "statement": "Dealer inventory returns to normal within two quarters."}
    ]
    assert acme["open_questions"] == [{"id": "q4_orders", "text": "Will dealers cut fourth-quarter orders?"}]
    assert (acme["documents"], acme["passages"], acme["peer_documents"], acme["peer_passages"]) == (3, 5, 1, 1)
    assert result["unsorted_documents"] == 0 and result["thesis_errors"] == []


def test_get_thesis(corpus):
    ws, ids = corpus
    thesis = data(ws, "get_thesis", ticker="acme")
    assert thesis["ticker"] == "ACME"
    assert thesis["known_facts"]["inventory"] == [
        {"id": "inventory.0", "text": "Dealer inventory was elevated at the end of Q2.", "as_of": None, "source": None}
    ]
    assert thesis["known_facts"]["pricing"] == []
    [prediction] = thesis["predictions"]
    assert (prediction["id"], prediction["p"], prediction["by"], prediction["outcome"]) == (
        "inv_normal", 0.6, "2027-03-31", None,
    )
    assert ids["call_ids"][0] in prediction["related"]
    assert thesis["forecast"] == {"resolved": 0, "brier": None}


def test_whats_new_lists_flagged_passages_newest_first(corpus, tmp_path):
    ws, ids = corpus
    result = data(ws, "whats_new", ticker="ACME")
    assert result["ticker"] == "ACME" and result["total"] == 3 and result["window_days"] == 30
    assert [p["id"] for p in result["passages"]] == [ids["peer_ids"][0], ids["filing_ids"][0], ids["call_ids"][0]]
    peer, _, first = result["passages"]
    assert peer["read_through"] == "BRRR" and first["read_through"] is None
    assert first == {
        "id": ids["call_ids"][0],
        "text": "Dealer inventory fell to a record low this quarter.",
        "speaker": "CFO",
        "title": "ACME Q2 call",
        "date": day(20),
        "source_type": "earnings_transcript",
        "page": 1,
        "link": (ws.root / "archive/ACME/call.pdf").resolve().as_uri() + "#page=1",
        "pillar": "inventory",
        "materiality": 2.0,
        "stance": 2.0,
        "contradicts": [],
        "read_through": None,
    }
    limited = data(ws, "whats_new", ticker="ACME", limit=1)
    assert limited["total"] == 3 and [p["id"] for p in limited["passages"]] == [ids["peer_ids"][0]]


def test_contradictions_and_fresh_data_per_call(corpus):
    ws, ids = corpus
    result = data(ws, "contradictions", ticker="ACME")
    [passage] = result["passages"]
    assert passage["id"] == ids["call_ids"][1] and passage["text"] == CONTRA and passage["speaker"] == "CEO"
    assert passage["contradicts"] == ["inv_normalizes"] and passage["page"] == 2
    assert result["assumptions"] == {"inv_normalizes": "Dealer inventory returns to normal within two quarters."}
    store = Store(ws.db_path)
    store.set_triage("ACME", ids["call_ids"][1], status="acknowledged")
    try:
        assert data(ws, "contradictions", ticker="ACME")["passages"] == []
    finally:
        store.set_triage("ACME", ids["call_ids"][1], status=None)
        store.close()
    assert len(data(ws, "contradictions", ticker="ACME")["passages"]) == 1


def test_search_passages(corpus):
    ws, ids = corpus
    result = data(ws, "search_passages", query="dealer*", ticker="ACME")
    found = {p["id"] for p in result["passages"]}
    assert found == {ids["call_ids"][0], ids["call_ids"][1], ids["peer_ids"][0], ids["old_ids"][0]}
    hit = next(p for p in result["passages"] if p["id"] == ids["call_ids"][0])
    assert hit["title"] == "ACME Q2 call" and hit["page"] == 1 and hit["link"].endswith("call.pdf#page=1")
    assert hit["snippet"].startswith("[Dealer]") and hit["document_id"] == ids["call"]
    only_peer = data(ws, "search_passages", query="dealer*", ticker="BRRR")
    assert [p["id"] for p in only_peer["passages"]] == [ids["peer_ids"][0]]
    assert data(ws, "search_passages", query='"record low"', limit=500)["count"] == 1
    assert len(data(ws, "search_passages", query="dealer*", limit=2)["passages"]) == 2
    assert "Unknown ticker" in error_text(call(ws, "search_passages", query="dealer", ticker="ZZZ"))
    assert "Nothing searchable" in error_text(call(ws, "search_passages", query="*** ()"))
    assert "query" in error_text(call(ws, "search_passages"))


def test_get_passage_with_neighbors(corpus):
    ws, ids = corpus
    first, middle, last = ids["call_ids"]
    passage = data(ws, "get_passage", id=middle)
    assert passage["text"] == CONTRA and passage["document_id"] == ids["call"]
    assert (passage["previous_id"], passage["next_id"]) == (first, last)
    assert passage["title"] == "ACME Q2 call" and passage["link"].endswith("call.pdf#page=2")
    assert data(ws, "get_passage", id=str(first))["previous_id"] is None
    assert data(ws, "get_passage", id=last)["next_id"] is None
    assert "No passage" in error_text(call(ws, "get_passage", id=9999))
    assert "integer" in error_text(call(ws, "get_passage", id="abc"))


def test_get_document(corpus):
    ws, ids = corpus
    view = data(ws, "get_document", id=ids["call"], ticker="ACME")
    assert view["document"]["title"] == "ACME Q2 call" and view["document"]["passage_count"] == 3
    assert [p["id"] for p in view["passages"]] == ids["call_ids"]
    assert view["passages"][1]["contradicts"] == ["inv_normalizes"]
    assert view["passages"][0]["pillar"] == "inventory"
    peer = data(ws, "get_document", id=ids["peer"], ticker="ACME")
    assert peer["document"]["read_through"] is True and peer["passages"][0]["read_through"] == "BRRR"
    assert "No document" in error_text(call(ws, "get_document", id=9999, ticker="ACME"))
    assert "Unknown ticker" in error_text(call(ws, "get_document", id=ids["call"], ticker="NOPE"))


@pytest.mark.parametrize("tool", ["get_thesis", "get_metrics", "whats_new", "contradictions"])
def test_bad_ticker_is_a_tool_error(corpus, tool):
    ws, _ = corpus
    text = error_text(call(ws, tool, ticker="NOPE"))
    assert "Unknown ticker 'NOPE'" in text and "ACME" in text
    assert "ticker" in error_text(call(ws, tool))


def test_get_metrics_without_metrics_and_unknown_metric(corpus):
    ws, _ = corpus
    assert data(ws, "get_metrics", ticker="ACME") == {"ticker": "ACME", "metrics": []}
    assert "no metric 'margin'" in error_text(call(ws, "get_metrics", ticker="ACME", metric="margin"))
    assert data(ws, "get_thesis", ticker="ACME")["metrics"] == []


def test_protocol_errors(corpus):
    ws, _ = corpus
    responses = rpc(
        ws,
        request("resources/list", id_=1),
        "{not json",
        "",
        "   ",
        "[1, 2]",
        request("tools/call", {"name": "no_such_tool"}, id_=2),
        request("tools/call", {"arguments": {}}, id_=3),
        request("tools/call", {"name": "list_companies", "arguments": [1]}, id_=4),
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": [1]},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "method": "no/such/notification"},
        {"jsonrpc": "2.0", "id": 99, "result": {}},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
    )
    codes = [(r["id"], r["error"]["code"]) for r in responses]
    assert codes == [(1, -32601), (None, -32700), (None, -32600), (2, -32602), (3, -32602), (4, -32602),
                     (5, -32602), (None, -32600)]
    assert all(r["jsonrpc"] == "2.0" for r in responses)


def test_notifications_alone_produce_no_output(corpus):
    ws, _ = corpus
    assert rpc(ws, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_missing_database_is_a_tool_error(tmp_path):
    ws = Workspace(tmp_path / "empty")
    responses = rpc(ws, request("initialize", {"protocolVersion": "2025-06-18"}), request("tools/list", id_=2))
    assert len(responses) == 2 and "result" in responses[1]
    for name in TOOLS:
        assert "radar run" in error_text(call(ws, name, ticker="ACME", query="x", id=1))
    assert not ws.db_path.exists() and not (tmp_path / "empty").exists()


def test_get_metrics_on_a_schema_v1_database(tmp_path):
    """Right after upgrading, `radar mcp` may be the first command: it opens radar.db read-only, cannot migrate it,
    and must still answer (no metric judgments exist yet) without changing the file."""
    import sqlite3

    from thesis_radar.store import MIGRATIONS

    ws = Workspace(tmp_path / "ws")
    ws.ensure_layout()
    write_thesis(ws.thesis_dir, text=THESIS + 'metrics:\n  gross_margin: {label: "Gross margin", unit: "%"}\n')
    conn = sqlite3.connect(ws.db_path)
    conn.executescript("BEGIN;" + MIGRATIONS[0] + "PRAGMA user_version = 1;COMMIT;")
    conn.close()
    [metric] = data(ws, "get_metrics", ticker="ACME")["metrics"]
    assert metric["id"] == "gross_margin" and metric["periods"] == [] and metric["latest"] is None
    conn = sqlite3.connect(ws.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    conn.close()
