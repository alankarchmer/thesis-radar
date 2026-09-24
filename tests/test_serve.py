import http.client
import json
import threading
import urllib.error
import urllib.request
from importlib import resources
from types import SimpleNamespace

import pytest
from helpers import payload_from_html
from test_apply import build_app

import thesis_radar.search
from thesis_radar import dashboard
from thesis_radar.lock import exclusive_lock
from thesis_radar.serve import CONTENT_SECURITY_POLICY, RadarServer, make_server, serve

MINIMAL_TEMPLATE = '<script id="radar-data" type="application/json">__RADAR_DATA__</script>'
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@pytest.fixture
def served(tmp_path, monkeypatch):
    if not resources.files("thesis_radar").joinpath("dashboard_template.html").is_file():
        monkeypatch.setattr(dashboard, "template_text", lambda: MINIMAL_TEMPLATE)
    app, corpus = build_app(tmp_path)
    server, token = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield SimpleNamespace(app=app, corpus=corpus, token=token, port=server.port)
    server.shutdown()
    server.server_close()
    thread.join(5)
    app.close()


def request(served, path, *, method="GET", body=None, token=True, headers=None):
    """(status, headers, body bytes) for one request to the test server."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{served.port}{path}", data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Radar-Token", served.token if token is True else token)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with NO_PROXY.open(req, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers, error.read()


def api(served, path, **kwargs):
    status, _, body = request(served, path, **kwargs)
    return status, json.loads(body)


def post_actions(served, actions, **kwargs):
    return api(served, "/api/actions", method="POST", body={"actions": actions}, **kwargs)


def test_index_renders_the_dashboard_in_serve_mode(served):
    assert served.app.store.last_view() is None
    status, headers, body = request(served, "/", token=False)
    assert status == 200
    html = body.decode()
    assert '"mode":"serve"' in html and served.token in html
    payload = payload_from_html(html)
    assert payload["serve"] == {"token": served.token}
    assert [c["ticker"] for c in payload["companies"]] == ["ACME"]
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert served.app.store.last_view() == payload["generated_at"]
    again = payload_from_html(request(served, "/", token=False)[2].decode())
    assert again["previous_view"] == payload["generated_at"]


def test_index_picks_up_thesis_edits(served):
    path = served.app.ws.thesis_path("ACME")
    path.write_text(path.read_text().replace("ACME Snowmobiles Inc.", "ACME Sleds Inc."))
    payload = payload_from_html(request(served, "/", token=False)[2].decode())
    assert payload["companies"][0]["company"] == "ACME Sleds Inc."


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1", "localhost"])
def test_other_hosts_are_rejected(served, host):
    status, headers, body = request(served, "/", headers={"Host": host.format(port=served.port)})
    assert status == 403
    assert json.loads(body)["ok"] is False
    assert headers["Cache-Control"] == "no-store"
    status, _ = post_actions(served, [], headers={"Host": host.format(port=served.port)})
    assert status == 403


def test_localhost_is_accepted(served):
    status, _, _ = request(served, "/", token=False, headers={"Host": f"localhost:{served.port}"})
    assert status == 200


@pytest.mark.parametrize("token", [False, "wrong", "é"])
def test_api_requires_the_token(served, token):
    pid = served.corpus.acme[0]
    action = {"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": True}
    status, body = post_actions(served, [action], token=token)
    assert (status, body["ok"]) == (403, False)
    assert served.app.store.triage_map("ACME") == {}
    for path in ["/api/search?q=inventory&ticker=ACME", f"/api/document/{served.corpus.acme_doc}?ticker=ACME", "/api/x"]:
        assert request(served, path, token=token)[0] == 403


def test_actions_apply_and_persist(served):
    pid = served.corpus.acme[0]
    status, body = post_actions(
        served,
        [
            {"op": "triage", "ticker": "ACME", "passage_id": pid, "status": "absorbed"},
            {"op": "label", "ticker": "ACME", "passage_id": pid, "question": "whats_new", "value": 1},
        ],
    )
    assert status == 200 and body["ok"] is True
    assert [r["op"] for r in body["results"]] == ["triage", "label"]
    assert served.app.store.triage_map("ACME") == {pid: {"status": "absorbed", "starred": False}}
    payload = payload_from_html(request(served, "/", token=False)[2].decode())
    [passage] = [p for p in payload["companies"][0]["passages"] if p["id"] == pid]
    assert passage["triage"] == {"status": "absorbed", "starred": False, "false_alarms": []}


def test_a_bare_array_body_is_accepted(served):
    pid = served.corpus.acme[0]
    status, _, raw = request(
        served, "/api/actions", method="POST", body=[{"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": True}]
    )
    assert status == 200 and json.loads(raw)["ok"] is True


def test_invalid_actions_are_a_400_and_change_nothing(served):
    c = served.corpus
    status, body = post_actions(
        served,
        [
            {"op": "triage", "ticker": "ACME", "passage_id": c.acme[0], "status": "dismissed"},
            {"op": "triage", "ticker": "ACME", "passage_id": c.other, "status": "dismissed"},
        ],
    )
    assert status == 400 and body["ok"] is False
    assert "action 1" in body["error"] and "not from ACME" in body["error"]
    assert served.app.store.triage_map("ACME") == {}
    status, _, raw = request(served, "/api/actions", method="POST", body={"nope": 1})
    assert status == 400 and json.loads(raw)["ok"] is False


def test_actions_need_a_json_body(served):
    req = urllib.request.Request(
        f"http://127.0.0.1:{served.port}/api/actions", data=b"[]", method="POST",
        headers={"Content-Type": "text/plain", "X-Radar-Token": served.token},
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        NO_PROXY.open(req, timeout=10)
    assert caught.value.code == 415
    caught.value.close()
    status, _, _ = request(served, "/api/actions", method="POST", headers={"Content-Type": "application/json"})
    assert status in (400, 411)


def test_oversized_bodies_are_refused_unread(served):
    connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=10)
    connection.putrequest("POST", "/api/actions")
    connection.putheader("Content-Type", "application/json")
    connection.putheader("X-Radar-Token", served.token)
    connection.putheader("Content-Length", str(1024 * 1024 + 1))
    connection.endheaders()
    response = connection.getresponse()
    assert response.status == 413
    assert json.loads(response.read())["ok"] is False
    connection.close()


def test_actions_wait_for_no_one_when_the_lock_is_held(served):
    pid = served.corpus.acme[0]
    with exclusive_lock(served.app.ws.lock_path):
        status, body = post_actions(served, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": True}])
    assert status == 409 and body["ok"] is False and "another radar command" in body["error"]
    assert served.app.store.triage_map("ACME") == {}
    status, _ = post_actions(served, [{"op": "triage", "ticker": "ACME", "passage_id": pid, "starred": True}])
    assert status == 200


def test_fact_and_policy_actions_write_through(served):
    status, body = post_actions(
        served,
        [
            {"op": "fact", "ticker": "ACME", "pillar": "pricing", "text": "List prices rose 3%."},
            {"op": "policy", "values": {"new_info_min": 0.75}},
        ],
    )
    assert status == 200
    assert body["results"][0]["fact_id"] == "pricing.0"
    payload = payload_from_html(request(served, "/", token=False)[2].decode())
    assert payload["policy"]["new_info_min"] == 0.75
    assert payload["companies"][0]["known_facts"]["pricing"][0]["text"] == "List prices rose 3%."


def test_document_route(served):
    c = served.corpus
    status, body = api(served, f"/api/document/{c.acme_doc}?ticker=ACME")
    assert status == 200
    assert body["document"]["id"] == c.acme_doc
    assert [p["id"] for p in body["passages"]] == c.acme
    status, body = api(served, f"/api/document/{c.peer_doc}?ticker=ACME")
    assert status == 200 and body["document"]["read_through"] is True
    assert api(served, f"/api/document/{c.other_doc}?ticker=ACME")[0] == 404
    assert api(served, "/api/document/99999?ticker=ACME")[0] == 404
    assert api(served, f"/api/document/{c.acme_doc}?ticker=ZZZ")[0] == 404
    assert api(served, f"/api/document/{c.acme_doc}")[0] == 400
    assert api(served, "/api/document/abc?ticker=ACME")[0] == 404


def test_search_route(served, monkeypatch):
    c = served.corpus
    calls = []

    def fake_search(store, query, *, tickers=None, limit=50):
        calls.append((query, list(tickers), limit))
        return [
            {"passage_id": c.peer[0], "snippet": "[dealers] cut"},
            {"passage_id": c.other, "snippet": "not ACME's"},
            {"passage_id": c.acme[0], "snippet": "[inventory] fell"},
        ]

    monkeypatch.setattr(thesis_radar.search, "search", fake_search)
    status, body = api(served, "/api/search?q=dealer+inventory&ticker=ACME&limit=500")
    assert status == 200
    assert calls == [("dealer inventory", ["ACME", "BRP"], 200)]
    assert [(r["passage"]["id"], r["snippet"]) for r in body["results"]] == [
        (c.peer[0], "[dealers] cut"),
        (c.acme[0], "[inventory] fell"),
    ]
    assert body["results"][1]["passage"]["text"] == "Dealer inventory fell 10% in the quarter."
    api(served, "/api/search?q=x&ticker=ACME")
    assert calls[-1][2] == 50
    assert api(served, "/api/search?ticker=ACME")[0] == 400
    assert api(served, "/api/search?q=x&ticker=ZZZ")[0] == 404
    assert api(served, "/api/search?q=x&ticker=ACME&limit=many")[0] == 400


def test_unknown_routes_and_methods(served):
    assert api(served, "/nope")[0] == 404
    assert api(served, "/api/nope")[0] == 404
    status, headers, _ = request(served, "/api/actions")
    assert status == 405 and headers["Allow"] == "POST"
    status, headers, _ = request(served, "/", method="POST", body={})
    assert status == 405 and headers["Allow"] == "GET"
    for method in ("PUT", "DELETE", "PATCH"):
        assert request(served, "/api/actions", method=method, body={})[0] == 405
    assert request(served, "/", method="OPTIONS")[0] == 405


def test_errors_do_not_leak_tracebacks(served, monkeypatch, capsys):
    def explode(*args, **kwargs):
        raise RuntimeError("secret internals")

    monkeypatch.setattr(dashboard, "document_view", explode)
    status, _, raw = request(served, f"/api/document/{served.corpus.acme_doc}?ticker=ACME")
    assert status == 500
    assert b"secret" not in raw and b"Traceback" not in raw
    assert json.loads(raw)["ok"] is False
    assert "RuntimeError: secret internals" in capsys.readouterr().err


def test_serve_prints_the_url_opens_a_browser_and_stops_on_ctrl_c(tmp_path, monkeypatch, capsys):
    app, _ = build_app(tmp_path)
    opened, closed = [], []

    def interrupted(self, poll_interval=0.5):
        raise KeyboardInterrupt

    monkeypatch.setattr(RadarServer, "serve_forever", interrupted)
    close = RadarServer.server_close
    monkeypatch.setattr(RadarServer, "server_close", lambda self: (closed.append(self.port), close(self)))
    monkeypatch.setattr("webbrowser.open", opened.append)
    try:
        serve(app, port=0)
        [url] = opened
        assert url.startswith("http://127.0.0.1:") and url.endswith("/")
        assert capsys.readouterr().out == f"radar: serving {url} (Ctrl-C to stop)\n"
        assert closed and url == f"http://127.0.0.1:{closed[0]}/"
        serve(app, port=0, open_browser=False)
        assert len(opened) == 1
    finally:
        app.close()
