"""Build the dashboard payload (spec v1.1 section 6) and write the self-contained dashboard.html."""

from __future__ import annotations

import json
import os
import random
from collections.abc import Mapping, Sequence
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from . import analysis, ledger, metrics, search
from .app import App
from .policy import classify_passage, signals
from .rubric import CONTEXT_CHARS, OFF_THESIS
from .runner import JudgePlan
from .similar import word_diff
from .thesis import Thesis

DATA_MARKER = "__RADAR_DATA__"
PAYLOAD_VERSION = 2
RECENT_DOCUMENT_DAYS = 60
SPOT_CHECKS = 20
SKIM_BOILERPLATE = 0.9


def build_payload(
    app: App,
    *,
    plan: JudgePlan | None = None,
    mode: str = "static",
    generated_at: str,
    previous_view: str | None,
    serve_token: str | None = None,
) -> dict[str, Any]:
    plan = plan or app.plan()
    companies = [
        company_payload(app, thesis, plan, previous_view=previous_view) for _, thesis in sorted(app.theses.items())
    ]
    return {
        "version": PAYLOAD_VERSION,
        "mode": mode,
        "generated_at": generated_at,
        "today": app.today.isoformat(),
        "previous_view": previous_view,
        "policy": app.policy.flat(),
        "policy_defaults": type(app.policy)().flat(),
        "companies": companies,
        "unsorted": unsorted_payload(app),
        "pending": {"unjudged": plan.count("unjudged"), "failed": len(plan.failed), "stale": plan.count("stale")},
        "serve": {"token": serve_token} if mode == "serve" and serve_token else None,
    }


def unsorted_payload(app: App) -> list[dict[str, Any]]:
    return [
        {
            "id": d["id"],
            "title": d["title"],
            "path": d["path"],
            "link": link_for(app, d["path"], None),
            "reason": d["status_reason"],
            "ticker": d["ticker"],
            "source_type": d["source_type"],
            "date": d["doc_date"],
            "command": (
                f"radar tag {d['id']} --ticker {d['ticker'] or 'TICKER'} "
                f"--source {d['source_type'] or 'SOURCE'} --date {d['doc_date'] or 'YYYY-MM-DD'}"
            ),
        }
        for d in app.store.documents(status="unsorted")
    ]


def link_for(app: App, path: str, page: int | None) -> str:
    uri = (app.ws.root / path).resolve().as_uri()
    return f"{uri}#page={page}" if page and path.lower().endswith(".pdf") else uri


def _clip_context(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= CONTEXT_CHARS else "… " + text[-CONTEXT_CHARS:].split(" ", 1)[-1]


class _PassageBuilder:
    """Builds payload passages for one thesis, sharing lookups across calls."""

    def __init__(self, app: App, thesis: Thesis, plan: JudgePlan | None, previous_view: str | None) -> None:
        self.app = app
        self.thesis = thesis
        self.plan = plan
        self.previous_view = previous_view
        self.triage = app.store.triage_map(thesis.ticker)
        # Assumptions the user marked as false alarms for a passage (`contradicts__<id>` labeled no).
        self.false_alarms: dict[int, list[str]] = {}
        for label in app.store.labels(thesis.ticker):
            if label["question"].startswith("contradicts__") and not label["value"]:
                self.false_alarms.setdefault(label["passage_id"], []).append(label["question"][len("contradicts__"):])
        self._similar_text: dict[int, str] = {}

    def preload_similar(self, rows: Sequence[Any]) -> None:
        wanted = {row["similar_to"] for row in rows if row["similar_to"] is not None} - set(self._similar_text)
        for old in self.app.store.get_passages(sorted(wanted)):
            self._similar_text[old["passage_id"]] = old["text"]

    def answers(self, row: Any) -> tuple[dict[str, Any] | None, str]:
        pid = row["passage_id"]
        if self.plan is not None:
            return self.plan.answers(self.thesis.ticker, pid), self.plan.status(self.thesis.ticker, pid)
        answers = self.app.store.latest_judged(pid, self.thesis.ticker)
        return answers, ("unjudged" if answers is None else "current")

    def build(self, row: Any, context: str | None) -> dict[str, Any]:
        answers, status = self.answers(row)
        p = signals(answers)
        if p is None:
            status = "unjudged"
        similar = None
        if row["similar_to"] is not None and row["similar_to"] in self._similar_text:
            old_text = self._similar_text[row["similar_to"]]
            similar = {"id": row["similar_to"], "similarity": row["similarity"], "diff": word_diff(old_text, row["text"])}
        triage = self.triage.get(row["passage_id"], {"status": None, "starred": False})
        passage = {
            "id": row["passage_id"],
            "document_id": row["document_id"],
            "seq": row["seq"],
            "text": row["text"],
            "page": row["page"],
            "speaker": row["speaker"],
            "title": row["title"],
            "date": row["doc_date"],
            "source_type": row["source_type"],
            "form": row["form"],
            "link": link_for(self.app, row["path"], row["page"]),
            "ingested_at": row["ingested_at"],
            "arrived_new": self.previous_view is not None and row["ingested_at"] > self.previous_view,
            "read_through": row["ticker"] if row["ticker"] != self.thesis.ticker else None,
            "status": status,
            "p": p,
            "similar": similar,
            "context": _clip_context(context),
            "triage": {
                "status": triage["status"],
                "starred": bool(triage["starred"]),
                "false_alarms": sorted(self.false_alarms.get(row["passage_id"], [])),
            },
        }
        passage["classified"] = classify_passage(passage, self.app.policy, self.app.today).as_payload()
        return passage


def _contexts(rows: Sequence[Any]) -> dict[int, str | None]:
    contexts: dict[int, str | None] = {}
    previous: dict[int, str] = {}
    for row in rows:
        contexts[row["passage_id"]] = previous.get(row["document_id"])
        previous[row["document_id"]] = row["text"]
    return contexts


def _worth_embedding(passage: Mapping[str, Any]) -> bool:
    p = passage["p"]
    if passage["triage"]["status"] or passage["triage"]["starred"]:
        return True
    if p is None:
        return False
    classified = passage["classified"]
    if classified["flagged"] or classified["in_contradictions"] or classified["contradicts"] or classified["supports"]:
        return True
    return p["pillar"] != OFF_THESIS and p["boilerplate"] < SKIM_BOILERPLATE


def company_payload(app: App, thesis: Thesis, plan: JudgePlan, *, previous_view: str | None) -> dict[str, Any]:
    ticker = thesis.ticker
    store, policy, today = app.store, app.policy, app.today
    all_rows = store.passages_for_tickers([ticker, *thesis.peers])
    contexts = _contexts(all_rows)
    rows = [row for row in all_rows if row["repeat_of"] is None]
    builder = _PassageBuilder(app, thesis, plan, previous_view)
    builder.preload_similar(rows)
    recent_cutoff = (today - timedelta(days=RECENT_DOCUMENT_DAYS)).isoformat()
    documents = {d["id"]: d for d in store.documents(status="sorted", tickers=[ticker, *thesis.peers])}
    recent_docs = {doc_id for doc_id, d in documents.items() if d["ingested_at"][:10] >= recent_cutoff}

    built = [builder.build(row, contexts.get(row["passage_id"])) for row in rows]
    answered = {row["passage_id"] for row in store.labels(ticker) if row["origin"] == "spotcheck"}
    spot_checks = _spot_checks([p for p in built if p["id"] not in answered], policy.whats_new_window_days, ticker, app)
    spot_set = set(spot_checks)
    passages = [p for p in built if p["document_id"] in recent_docs or p["id"] in spot_set or _worth_embedding(p)]
    embedded_ids = {p["id"] for p in passages}
    per_document: dict[int, list[int]] = {}
    for p in built:
        per_document.setdefault(p["document_id"], []).append(p["id"])

    judged = [p for p in passages if p["p"] is not None]
    passages_by_id = {p["id"]: p for p in judged}
    predictions, forecast = analysis.prediction_view(
        thesis, store.resolutions(ticker), today=today,
        related=lambda text: search.related_passages(store, [ticker, *thesis.peers], text, 5),
    )
    pillars = list(thesis.pillars)
    return {
        "ticker": ticker,
        "company": thesis.company,
        "peers": list(thesis.peers),
        "pillars": dict(thesis.pillars),
        "assumptions": [{"id": a.id, "pillar": a.pillar, "statement": a.statement} for a in thesis.assumptions],
        "open_questions": [{"id": q.id, "text": q.text} for q in thesis.open_questions],
        "known_facts": {
            pillar: [
                {"id": f.id, "text": f.text, "as_of": f.as_of, "source": f.source}
                for f in thesis.known_facts.get(pillar, ())
            ]
            for pillar in pillars
        },
        "predictions": predictions,
        "forecast": forecast,
        "documents": [
            {
                "id": doc_id,
                "title": d["title"],
                "date": d["doc_date"],
                "source_type": d["source_type"],
                "form": d["form"],
                "link": link_for(app, d["path"], None),
                "ingested_at": d["ingested_at"],
                "ticker": d["ticker"],
                "read_through": d["ticker"] != ticker,
                "complete": all(pid in embedded_ids for pid in per_document.get(doc_id, [])),
                "passage_count": len(per_document.get(doc_id, [])),
            }
            for doc_id, d in sorted(documents.items(), key=lambda item: (item[1]["doc_date"] or "", item[0]), reverse=True)
        ],
        "passages": passages,
        "spot_checks": spot_checks,
        "heatmap": analysis.heatmap(judged, pillars, today=today),
        "assumption_series": analysis.assumption_series(judged, [a.id for a in thesis.assumptions], policy),
        "divergence": analysis.divergence(judged, pillars, policy, today=today),
        "ledger": ledger.ledger_summary(store, thesis, passages_by_id, policy),
        "redlines": analysis.redlines(store, ticker),
        "metrics": metrics.metric_series(store, thesis, policy, link=lambda path, page: link_for(app, path, page)),
    }


def _spot_checks(passages: Sequence[Mapping[str, Any]], window_days: int, ticker: str, app: App) -> list[int]:
    """A reproducible random sample of judged, unflagged, untriaged, recent passages."""
    cutoff = (app.today - timedelta(days=window_days)).isoformat()
    pool = sorted(
        p["id"] for p in passages
        if p["p"] is not None
        and not p["classified"]["flagged"]
        and not p["triage"]["status"]
        and (p["date"] or p["ingested_at"][:10]) >= cutoff
    )
    rng = random.Random(f"{ticker}:{app.today.isoformat()}")
    return rng.sample(pool, min(SPOT_CHECKS, len(pool)))


def document_view(app: App, document_id: int, ticker: str, *, plan: JudgePlan | None = None) -> dict[str, Any] | None:
    """Every passage of one document as payload passages (skim mode in serve), or None if unknown."""
    thesis = app.theses.get(ticker)
    doc = app.store.get_document(document_id)
    if thesis is None or doc is None or doc["ticker"] not in (ticker, *thesis.peers):
        return None
    rows = app.store.passages_for_document(document_id)
    contexts = _contexts(rows)
    builder = _PassageBuilder(app, thesis, plan, app.store.last_view())
    live = [row for row in rows if row["repeat_of"] is None]
    builder.preload_similar(live)
    return {
        "document": {
            "id": doc["id"], "title": doc["title"], "date": doc["doc_date"], "source_type": doc["source_type"],
            "form": doc["form"], "link": link_for(app, doc["path"], None), "ingested_at": doc["ingested_at"],
            "ticker": doc["ticker"], "read_through": doc["ticker"] != ticker, "complete": True,
            "passage_count": len(live),
        },
        "passages": [builder.build(row, contexts.get(row["passage_id"])) for row in live],
    }


def passages_view(app: App, ticker: str, passage_ids: Sequence[int], *, plan: JudgePlan | None = None) -> list[dict[str, Any]]:
    """Payload passages for specific ids under one thesis (search results in serve)."""
    thesis = app.theses.get(ticker)
    if thesis is None:
        return []
    rows = [row for row in app.store.get_passages(passage_ids) if row["ticker"] in (ticker, *thesis.peers)]
    builder = _PassageBuilder(app, thesis, plan, app.store.last_view())
    builder.preload_similar(rows)
    context_rows: dict[int, str | None] = {}
    for row in rows:
        siblings = app.store.passages_for_document(row["document_id"])
        context_rows.update({k: v for k, v in _contexts(siblings).items() if k == row["passage_id"]})
    order = {pid: index for index, pid in enumerate(passage_ids)}
    return sorted((builder.build(row, context_rows.get(row["passage_id"])) for row in rows), key=lambda p: order.get(p["id"], 0))


def script_safe_json(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def template_text() -> str:
    return resources.files("thesis_radar").joinpath("dashboard_template.html").read_text(encoding="utf-8")


def render_html(payload: Any) -> str:
    template = template_text()
    if template.count(DATA_MARKER) != 1:
        raise RuntimeError("dashboard template must contain the data marker exactly once")
    return template.replace(DATA_MARKER, script_safe_json(payload))


def write_dashboard(path: Path, html: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, path)
