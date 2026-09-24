"""`radar mcp`: a read-only MCP server over stdio.

A minimal, dependency-free implementation of the Model Context Protocol's stdio transport:
JSON-RPC 2.0 messages, one per line, on stdin and stdout. It answers `initialize`, `ping`,
`tools/list`, and `tools/call`, and ignores notifications. Every tool call opens the workspace
read-only and closes it afterwards, so the server never blocks `radar run` and always answers from
the current data. Passages are returned verbatim with their citations; the server makes no LLM
calls. Only protocol messages go to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from . import __version__, analysis, dashboard, search
from .app import App
from .config import ConfigError, Workspace
from .policy import PolicyError, passage_day
from .store import SchemaTooNew
from .thesis import Thesis

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "thesis-radar"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
RELATED_LIMIT = 5

INSTRUCTIONS = (
    "Read-only access to the user's own investment research corpus, collected by thesis-radar: one thesis "
    "per company (pillars, assumptions, open questions, known facts, predictions) and the passages of the "
    "filings, transcripts, broker reports, and notes they collected, as judged against each thesis. Every "
    "passage is the verbatim text of its source and comes with a citation (title, source type, date, page, "
    "and a file link); the server never summarizes or generates text. When you quote or rely on a passage, "
    "keep its words exact and cite its title, date, and page. Start with list_companies; use whats_new and "
    "contradictions for what the radar currently flags, search_passages for anything else, and get_passage "
    "or get_document for the surrounding text."
)

_SCORES = (
    "materiality is the judged 0-3 score (0 no bearing, 3 could change the thesis on its own); stance is 0-4 "
    "(0 clearly negative for the company, 2 neutral, 4 clearly positive); contradicts lists the ids of thesis "
    "assumptions the passage contradicts; read_through names the peer ticker when the passage is about a peer."
)


class ToolError(Exception):
    """A tool could not answer; the client sees it as a result with isError: true."""


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def log(message: str) -> None:
    print(f"radar mcp: {message}", file=sys.stderr, flush=True)


# Argument helpers


def _text(arguments: Mapping[str, Any], name: str, *, required: bool = True) -> str | None:
    value = arguments.get(name)
    if value is None:
        if required:
            raise ToolError(f"Missing required argument {name!r}.")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"Argument {name!r} must be non-empty text.")
    return value.strip()


def _integer(arguments: Mapping[str, Any], name: str, *, default: int | None = None) -> int:
    value = arguments.get(name)
    if value is None:
        if default is None:
            raise ToolError(f"Missing required argument {name!r}.")
        return default
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ToolError(f"Argument {name!r} must be an integer.") from None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"Argument {name!r} must be an integer.")
    return value


def _limit(arguments: Mapping[str, Any]) -> int:
    return max(1, min(MAX_LIMIT, _integer(arguments, "limit", default=DEFAULT_LIMIT)))


def _thesis(app: App, arguments: Mapping[str, Any]) -> Thesis:
    raw = _text(arguments, "ticker") or ""
    thesis = app.theses.get(raw.upper())
    if thesis is None:
        known = ", ".join(sorted(app.theses)) or "none"
        raise ToolError(f"Unknown ticker {raw!r}. Tickers with a thesis: {known}.")
    return thesis


# Result shapes


def _round(value: Any) -> float | None:
    return None if value is None else round(float(value), 2)


def _cite(passage: Mapping[str, Any]) -> dict[str, Any]:
    """A payload Passage trimmed to what a reader needs to quote and cite it."""
    p = passage.get("p") or {}
    classified = passage.get("classified") or {}
    return {
        "id": passage["id"],
        "text": passage["text"],
        "speaker": passage["speaker"],
        "title": passage["title"],
        "date": passage["date"],
        "source_type": passage["source_type"],
        "page": passage["page"],
        "link": passage["link"],
        "pillar": p.get("pillar"),
        "materiality": _round(p.get("materiality")),
        "stance": _round(p.get("stance")),
        "contradicts": list(classified.get("contradicts") or []),
        "read_through": passage.get("read_through"),
    }


def _thesis_outline(thesis: Thesis) -> dict[str, Any]:
    return {
        "ticker": thesis.ticker,
        "company": thesis.company,
        "aliases": list(thesis.aliases),
        "peers": list(thesis.peers),
        "pillars": dict(thesis.pillars),
        "assumptions": [{"id": a.id, "pillar": a.pillar, "statement": a.statement} for a in thesis.assumptions],
        "open_questions": [{"id": q.id, "text": q.text} for q in thesis.open_questions],
    }


def _counts(app: App, tickers: Sequence[str]) -> tuple[int, int]:
    """(sorted documents, passages) filed under `tickers`."""
    if not tickers:
        return 0, 0
    marks = ", ".join("?" for _ in tickers)
    row = app.store.conn.execute(
        f"""SELECT COUNT(DISTINCT d.id), COUNT(p.id) FROM documents d LEFT JOIN passages p ON p.document_id = d.id
            WHERE d.status = 'sorted' AND d.ticker IN ({marks})""",
        tuple(tickers),
    ).fetchone()
    return int(row[0]), int(row[1])


# Tools


def list_companies(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    companies = []
    for _, thesis in sorted(app.theses.items()):
        documents, passages = _counts(app, [thesis.ticker])
        peer_documents, peer_passages = _counts(app, thesis.peers)
        companies.append(
            {
                **_thesis_outline(thesis),
                "documents": documents,
                "passages": passages,
                "peer_documents": peer_documents,
                "peer_passages": peer_passages,
            }
        )
    return {
        "companies": companies,
        "unsorted_documents": len(app.store.documents(status="unsorted")),
        "thesis_errors": [str(error) for error in app.thesis_errors],
    }


def get_thesis(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    thesis = _thesis(app, arguments)
    tickers = [thesis.ticker, *thesis.peers]
    predictions, forecast = analysis.prediction_view(
        thesis, app.store.resolutions(thesis.ticker), today=app.today,
        related=lambda text: search.related_passages(app.store, tickers, text, RELATED_LIMIT),
    )
    return {
        **_thesis_outline(thesis),
        "known_facts": {
            pillar: [
                {"id": f.id, "text": f.text, "as_of": f.as_of, "source": f.source}
                for f in thesis.known_facts.get(pillar, ())
            ]
            for pillar in thesis.pillars
        },
        "predictions": predictions,
        "forecast": forecast,
    }


def _feed(app: App, arguments: Mapping[str, Any], flag: str, window_days: int) -> dict[str, Any]:
    thesis = _thesis(app, arguments)
    limit = _limit(arguments)
    payload = dashboard.company_payload(app, thesis, app.plan(), previous_view=None)
    hits = [p for p in payload["passages"] if p["classified"][flag]]
    hits.sort(key=lambda p: (passage_day(p["date"], p["ingested_at"]) or "", p["id"]), reverse=True)
    return {
        "ticker": thesis.ticker,
        "today": app.today.isoformat(),
        "window_days": window_days,
        "total": len(hits),
        "passages": [_cite(p) for p in hits[:limit]],
    }


def whats_new(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return _feed(app, arguments, "in_whats_new", app.policy.whats_new_window_days)


def contradictions(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    result = _feed(app, arguments, "in_contradictions", app.policy.contradictions_window_days)
    thesis = app.theses[result["ticker"]]
    named = {a for p in result["passages"] for a in p["contradicts"]}
    result["assumptions"] = {a.id: a.statement for a in thesis.assumptions if a.id in named}
    return result


def search_passages(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    query = _text(arguments, "query") or ""
    limit = _limit(arguments)
    ticker = _text(arguments, "ticker", required=False)
    tickers: list[str] | None = None
    if ticker is not None:
        ticker = ticker.upper()
        thesis = app.theses.get(ticker)
        if thesis is not None:
            tickers = [ticker, *thesis.peers]
        elif app.store.documents(status="sorted", tickers=[ticker]):
            tickers = [ticker]
        else:
            known = ", ".join(sorted(app.theses)) or "none"
            raise ToolError(f"Unknown ticker {ticker!r}: no thesis or documents. Tickers with a thesis: {known}.")
    if search.fts_match(query) is None:
        raise ToolError(f"Nothing searchable in query {query!r}: use words, \"quoted phrases\", word*, or OR.")
    hits = search.search(app.store, query, tickers=tickers, limit=limit)
    return {
        "query": query,
        "ticker": ticker,
        "count": len(hits),
        "passages": [
            {
                "id": hit["passage_id"],
                "text": hit["text"],
                "speaker": hit["speaker"],
                "title": hit["title"],
                "date": hit["doc_date"],
                "source_type": hit["source_type"],
                "page": hit["page"],
                "link": search.file_link(app.ws, hit["path"], hit["page"]),
                "snippet": hit["snippet"],
                "ticker": hit["ticker"],
                "document_id": hit["document_id"],
            }
            for hit in hits
        ],
    }


def get_passage(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    passage_id = _integer(arguments, "id")
    rows = app.store.get_passages([passage_id])
    if not rows:
        raise ToolError(f"No passage with id {passage_id}.")
    row = rows[0]
    order = [r["passage_id"] for r in app.store.passages_for_document(row["document_id"])]
    index = order.index(passage_id)
    return {
        "id": passage_id,
        "text": row["text"],
        "speaker": row["speaker"],
        "title": row["title"],
        "date": row["doc_date"],
        "source_type": row["source_type"],
        "form": row["form"],
        "page": row["page"],
        "link": search.file_link(app.ws, row["path"], row["page"]),
        "ticker": row["ticker"],
        "document_id": row["document_id"],
        "seq": row["seq"],
        "previous_id": order[index - 1] if index > 0 else None,
        "next_id": order[index + 1] if index + 1 < len(order) else None,
    }


def get_document(app: App, arguments: Mapping[str, Any]) -> dict[str, Any]:
    document_id = _integer(arguments, "id")
    thesis = _thesis(app, arguments)
    view = dashboard.document_view(app, document_id, thesis.ticker, plan=app.plan())
    if view is None:
        doc = app.store.get_document(document_id)
        if doc is None:
            raise ToolError(f"No document with id {document_id}.")
        owner = doc["ticker"] or "no ticker"
        raise ToolError(f"Document {document_id} is filed under {owner}, not {thesis.ticker} or its peers.")
    doc = view["document"]
    return {
        "document": {
            key: doc[key]
            for key in ("id", "title", "date", "source_type", "form", "link", "ticker", "read_through", "passage_count")
        },
        "passages": [_cite(p) for p in view["passages"]],
    }


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[App, Mapping[str, Any]], dict[str, Any]]

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": {"title": self.title, "readOnlyHint": True, "openWorldHint": False},
        }


def _schema(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = list(required)
    return schema


_TICKER = {"type": "string", "description": "Ticker of a company with a thesis, e.g. PII (see list_companies)."}
_LIMIT = {
    "type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT,
    "description": f"Most passages to return (1-{MAX_LIMIT}).",
}

TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in (
        Tool(
            "list_companies", "List companies",
            "Every company the user follows: ticker, company name, peers, thesis pillars, assumptions, and open "
            "questions, with counts of collected documents and passages (own and peers'). Start here.",
            _schema({}), list_companies,
        ),
        Tool(
            "get_thesis", "Get a thesis",
            "The user's full thesis for one company: pillars, assumptions, open questions, known facts (with ids "
            "such as pricing.0, dates, and source passage ids), and predictions with the user's probability, due "
            "date, outcome, and related passage ids.",
            _schema({"ticker": _TICKER}, ["ticker"]), get_thesis,
        ),
        Tool(
            "whats_new", "What's new",
            "Passages currently in the company's What's new feed at the user's saved thresholds: on a thesis "
            "pillar, material, new information, and recent. Newest first, each verbatim with its citation. "
            + _SCORES,
            _schema({"ticker": _TICKER, "limit": _LIMIT}, ["ticker"]), whats_new,
        ),
        Tool(
            "contradictions", "Contradictions",
            "Passages currently flagged as contradicting one of the company's thesis assumptions (recent and not "
            "yet acknowledged). Newest first, each verbatim with its citation; `assumptions` maps the contradicted "
            "assumption ids to their statements. " + _SCORES,
            _schema({"ticker": _TICKER, "limit": _LIMIT}, ["ticker"]), contradictions,
        ),
        Tool(
            "search_passages", "Search passages",
            "Full-text search over every collected passage, best matches first, each verbatim with its citation "
            "and a snippet with the matched words in [brackets]. Words must all appear; use \"quoted phrases\", "
            "a trailing * for prefixes (infl*), and OR between alternatives.",
            _schema(
                {
                    "query": {"type": "string", "description": "Words, \"quoted phrases\", prefix*, and OR."},
                    "ticker": {
                        "type": "string",
                        "description": "Only this company's documents (and its peers' when it has a thesis).",
                    },
                    "limit": _LIMIT,
                },
                ["query"],
            ),
            search_passages,
        ),
        Tool(
            "get_passage", "Get a passage",
            "One passage by id, verbatim with its citation, its document id, and the ids of the passages just "
            "before and after it in the document (previous_id, next_id) for reading the surrounding text.",
            _schema({"id": {"type": "integer", "minimum": 1, "description": "Passage id."}}, ["id"]), get_passage,
        ),
        Tool(
            "get_document", "Get a document",
            "Every passage of one document in order, verbatim with citations and how each was judged against the "
            "company's thesis. " + _SCORES,
            _schema(
                {"id": {"type": "integer", "minimum": 1, "description": "Document id."}, "ticker": _TICKER},
                ["id", "ticker"],
            ),
            get_document,
        ),
    )
}


# Protocol


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _initialize(params: Mapping[str, Any]) -> dict[str, Any]:
    requested = params.get("protocolVersion")
    return {
        "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
        "instructions": INSTRUCTIONS,
    }


def call_tool(ws: Workspace, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run one tool against a freshly opened read-only workspace; returns the tools/call result."""
    tool = TOOLS[name]
    if not ws.db_path.exists():
        return _tool_error(f"No radar.db in {ws.root}. Run `radar run` first to collect and judge documents.")
    try:
        app = App.open(ws, readonly=True)
    except (ConfigError, PolicyError, SchemaTooNew, sqlite3.Error) as exc:
        return _tool_error(f"Cannot open the workspace {ws.root}: {exc}")
    try:
        result = tool.handler(app, arguments)
    except ToolError as exc:
        return _tool_error(str(exc))
    except Exception as exc:
        log(f"{name} failed:\n{traceback.format_exc()}")
        return _tool_error(f"{name} failed: {type(exc).__name__}: {exc}")
    finally:
        app.close()
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=1)}],
        "structuredContent": result,
    }


def _tools_call(ws: Workspace, params: Mapping[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str):
        raise RpcError(INVALID_PARAMS, "Invalid params: tools/call needs a tool name")
    if name not in TOOLS:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "Invalid params: arguments must be an object")
    return call_tool(ws, name, arguments)


def handle_message(ws: Workspace, line: str) -> dict[str, Any] | None:
    """The response to one line of input, or None when nothing should be sent (notifications)."""
    try:
        message = json.loads(line)
    except ValueError as exc:
        return _error(None, PARSE_ERROR, f"Parse error: {exc}")
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Invalid request: expected one JSON object per line")
    if "method" not in message:
        if "id" in message and ("result" in message or "error" in message):
            return None  # a response; this server sends no requests, so there is nothing to match it to
        return _error(None, INVALID_REQUEST, "Invalid request: no method")
    method = message["method"]
    if "id" not in message:
        return None  # notifications (initialized, cancelled, ...) need no reply
    request_id = message["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return _error(None, INVALID_REQUEST, "Invalid request: id must be a string or an integer")
    if not isinstance(method, str):
        return _error(request_id, INVALID_REQUEST, "Invalid request: method must be a string")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(request_id, INVALID_PARAMS, "Invalid params: params must be an object")
    try:
        if method == "initialize":
            result = _initialize(params)
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [tool.describe() for tool in TOOLS.values()]}
        elif method == "tools/call":
            result = _tools_call(ws, params)
        else:
            return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except RpcError as exc:
        return _error(request_id, exc.code, exc.message)
    except Exception as exc:
        log(f"{method} failed:\n{traceback.format_exc()}")
        return _error(request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}: {exc}")
    return _response(request_id, result)


def run_stdio(ws: Workspace, stdin: TextIO, stdout: TextIO) -> None:
    """Serve MCP over newline-delimited JSON until stdin closes."""
    log(f"serving {ws.root} (read-only)")
    while True:
        line = stdin.readline()
        if not line:
            break
        if not line.strip():
            continue
        response = handle_message(ws, line)
        if response is None:
            continue
        try:
            stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            stdout.flush()
        except BrokenPipeError:
            break
