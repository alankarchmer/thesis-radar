"""`radar serve`: the dashboard on localhost with write-back (spec v1.1 section 7).

One thread serves every request, because the app shares a single SQLite connection; HTTP/1.0 closes
each connection after its response, so an idle browser connection never blocks the next request.
The server binds 127.0.0.1 only and rejects any Host header other than 127.0.0.1:<port> or
localhost:<port> (DNS rebinding). Every /api request must carry the page's X-Radar-Token, and a
cross-site page cannot send that header or a JSON body without a CORS preflight, which is never
granted.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import sys
import traceback
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import dashboard, search
from .app import App
from .apply import ActionError, apply_actions, parse_actions
from .lock import LockHeld, exclusive_lock
from .policy import PolicyError
from .store import utc_now

HOST = "127.0.0.1"
TOKEN_HEADER = "X-Radar-Token"
MAX_BODY_BYTES = 1024 * 1024
SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_COMMON_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
_DOCUMENT_ROUTE = re.compile(r"^/api/document/(\d+)$")
_INTERNAL_ERROR = "internal error; the details are in the terminal running radar serve"


class HttpError(Exception):
    def __init__(self, status: int, message: str, *, allow: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.allow = allow


class RadarServer(HTTPServer):
    """A single-threaded HTTP server for one App, with the page token it hands out."""

    def __init__(self, app: App, port: int) -> None:
        super().__init__((HOST, port), RadarHandler)
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.port = int(self.server_address[1])
        self.allowed_hosts = frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # the browser went away mid-response
        super().handle_error(request, client_address)


def make_server(app: App, *, port: int) -> tuple[RadarServer, str]:
    """A bound (not yet serving) server on 127.0.0.1:`port` (0 picks a free port) and its token."""
    server = RadarServer(app, port)
    return server, server.token


def serve(app: App, *, port: int, open_browser: bool = True) -> None:
    server, _token = make_server(app, port=port)
    url = f"http://{HOST}:{server.port}/"
    print(f"radar: serving {url} (Ctrl-C to stop)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


class RadarHandler(BaseHTTPRequestHandler):
    server: RadarServer
    timeout = 60  # a stalled client cannot hold the single thread forever

    def version_string(self) -> str:
        return "radar"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: no line per request; errors go to stderr from _handle

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def __getattr__(self, name: str) -> Any:
        # http.server dispatches to do_<METHOD> and answers 501 when there is none; every other
        # method gets the same checks as GET and POST and ends in 405 (or 404, 403).
        if name.startswith("do_"):
            return lambda: self._handle(name[3:])
        raise AttributeError(name)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """http.server's own errors (malformed request line, oversized headers) in the same JSON shape."""
        self.close_connection = True
        try:
            phrase = HTTPStatus(code).phrase
        except ValueError:
            phrase = "error"
        self._json(code, {"ok": False, "error": message or phrase})

    # Dispatch

    def _handle(self, method: str) -> None:
        try:
            if not self._host_allowed():
                raise HttpError(403, "forbidden: requests must be addressed to 127.0.0.1 or localhost")
            parts = urlsplit(self.path)
            if parts.path.startswith("/api/") and not self._token_valid():
                raise HttpError(403, f"forbidden: missing or wrong {TOKEN_HEADER}")
            handler, allowed = self._route(parts.path)
            if method != allowed:
                raise HttpError(405, "method not allowed", allow=allowed)
            handler(parse_qs(parts.query, keep_blank_values=True))
        except HttpError as exc:
            extra = (("Allow", exc.allow),) if exc.allow else ()
            self._json(exc.status, {"ok": False, "error": exc.message}, extra)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            self._json(500, {"ok": False, "error": _INTERNAL_ERROR})

    def _route(self, path: str) -> tuple[Callable[[dict[str, list[str]]], None], str]:
        if path == "/":
            return self._index, "GET"
        if path == "/api/actions":
            return self._actions, "POST"
        if path == "/api/search":
            return self._search, "GET"
        match = _DOCUMENT_ROUTE.match(path)
        if match:
            document_id = int(match[1])
            return (lambda query: self._document(document_id, query)), "GET"
        raise HttpError(404, "not found")

    def _host_allowed(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        return len(hosts) == 1 and hosts[0].strip().lower() in self.server.allowed_hosts

    def _token_valid(self) -> bool:
        given = self.headers.get(TOKEN_HEADER) or ""
        return hmac.compare_digest(given.encode("utf-8", "replace"), self.server.token.encode("utf-8"))

    # Routes

    def _index(self, query: dict[str, list[str]]) -> None:
        app = self.server.app
        try:
            app.reload()  # pick up thesis and policy edits made since the last load
        except PolicyError as exc:
            raise HttpError(500, str(exc)) from exc
        generated_at = utc_now()
        payload = dashboard.build_payload(
            app, mode="serve", generated_at=generated_at, previous_view=app.store.last_view(),
            serve_token=self.server.token,
        )
        html = dashboard.render_html(payload)
        app.store.record_view(generated_at)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", html=True)

    def _actions(self, query: dict[str, list[str]]) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HttpError(415, "Content-Type must be application/json")
        text = self._read_body()
        app = self.server.app
        try:
            actions = parse_actions(text)
            with exclusive_lock(app.ws.lock_path):
                app.reload()
                results = apply_actions(app, actions)
        except LockHeld as exc:
            raise HttpError(409, str(exc)) from exc
        except ActionError as exc:
            raise HttpError(400, str(exc)) from exc
        except PolicyError as exc:
            raise HttpError(500, str(exc)) from exc
        self._json(200, {"ok": True, "results": results})

    def _document(self, document_id: int, query: dict[str, list[str]]) -> None:
        ticker = _param(query, "ticker")
        if not ticker:
            raise HttpError(400, "ticker is required")
        view = dashboard.document_view(self.server.app, document_id, ticker)
        if view is None:
            raise HttpError(404, f"no document {document_id} for {ticker}")
        self._json(200, view)

    def _search(self, query: dict[str, list[str]]) -> None:
        app = self.server.app
        text = (_param(query, "q") or "").strip()
        if not text:
            raise HttpError(400, "q is required")
        ticker = _param(query, "ticker") or ""
        thesis = app.theses.get(ticker)
        if thesis is None:
            raise HttpError(404, f"unknown ticker {ticker!r}")
        raw_limit = _param(query, "limit")
        try:
            limit = SEARCH_LIMIT if not raw_limit else int(raw_limit)
        except ValueError as exc:
            raise HttpError(400, "limit must be a whole number") from exc
        limit = max(1, min(limit, MAX_SEARCH_LIMIT))
        try:
            hits = search.search(app.store, text, tickers=[ticker, *thesis.peers], limit=limit)
        except sqlite3.OperationalError as exc:
            raise HttpError(400, "the search query could not be run; try plain words") from exc
        passages = {
            p["id"]: p for p in dashboard.passages_view(app, ticker, [hit["passage_id"] for hit in hits])
        }
        results = [
            {"passage": passages[hit["passage_id"]], "snippet": hit.get("snippet")}
            for hit in hits
            if hit["passage_id"] in passages
        ]
        self._json(200, {"results": results})

    # Responses

    def _read_body(self) -> str:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise HttpError(411, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HttpError(400, "Content-Length must be a number") from exc
        if length < 0:
            raise HttpError(400, "Content-Length must not be negative")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise HttpError(413, "request body is larger than 1 MB")
        try:
            return self.rfile.read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HttpError(400, "request body must be UTF-8") from exc

    def _json(self, status: int, body: Any, extra_headers: tuple[tuple[str, str], ...] = ()) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8", extra_headers=extra_headers)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        html: bool = False,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in _COMMON_HEADERS:
            self.send_header(name, value)
        if html:
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _param(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    return values[0] if values else None
