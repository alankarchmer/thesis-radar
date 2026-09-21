"""Build the dashboard payload and write the self-contained dashboard.html."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from .config import Workspace
from .policy import Policy, classify
from .runner import JudgePlan
from .store import Store
from .thesis import Thesis

DATA_MARKER = "__RADAR_DATA__"


def build_payload(
    ws: Workspace,
    store: Store,
    theses: Mapping[str, Thesis],
    plan: JudgePlan,
    policy: Policy,
    *,
    generated_at: str,
    previous_view: str | None,
) -> dict[str, Any]:
    companies = []
    for ticker, thesis in sorted(theses.items()):
        passages = []
        for row in store.passages_for_ticker(ticker):
            answers = plan.judged.get(row["passage_id"])
            if answers is None:
                continue
            c = classify(answers, policy)
            if not c.in_contradictions and (c.pillar is None or c.boilerplate > policy.boilerplate_max):
                continue
            passages.append(
                {
                    "id": row["passage_id"],
                    "document_id": row["document_id"],
                    "text": row["text"],
                    "page": row["page"],
                    "speaker": row["speaker"],
                    "title": row["title"],
                    "origin": row["origin"],
                    "form": _filing_form(row["origin"], row["title"]),
                    "date": row["doc_date"],
                    "source_type": row["source_type"],
                    "link": _link(ws, row["path"], row["page"]),
                    "arrived_new": previous_view is not None and row["ingested_at"] > previous_view,
                    "pillar": c.pillar,
                    "pillar_confidence": c.pillar_confidence,
                    "new_info": c.new_info,
                    "materiality": c.materiality,
                    "stance": c.stance,
                    "evidence": c.evidence,
                    "forward_looking": c.forward_looking,
                    "supports": list(c.supports),
                    "contradicts": list(c.contradicts),
                    "in_contradictions": c.in_contradictions,
                    "in_whats_new": c.in_whats_new,
                    "in_maybe": c.in_maybe,
                }
            )
        companies.append(
            {
                "ticker": ticker,
                "company": thesis.company,
                "pillars": dict(thesis.pillars),
                "assumptions": [{"id": a.id, "pillar": a.pillar, "statement": a.statement} for a in thesis.assumptions],
                "passages": passages,
            }
        )
    unsorted = [
        {
            "id": d["id"],
            "title": d["title"],
            "path": d["path"],
            "reason": d["status_reason"],
            "ticker": d["ticker"],
            "source_type": d["source_type"],
            "date": d["doc_date"],
            "command": (
                f"radar tag {d['id']} --ticker {d['ticker'] or 'TICKER'} "
                f"--source {d['source_type'] or 'SOURCE'} --date {d['doc_date'] or 'YYYY-MM-DD'}"
            ),
        }
        for d in store.documents(status="unsorted")
    ]
    return {
        "generated_at": generated_at,
        "previous_view": previous_view,
        "companies": companies,
        "unsorted": unsorted,
        "pending": {"unjudged": len(plan.pending) - len(plan.failed), "failed": len(plan.failed)},
    }


def _filing_form(origin: str, title: str) -> str | None:
    """The SEC form of an EDGAR document; store_filing titles them "{ticker} {form} {date} {file name}"."""
    parts = title.split(" ")
    return parts[1] if origin == "edgar" and len(parts) >= 4 else None


def _link(ws: Workspace, path: str, page: int) -> str:
    uri = (ws.root / path).resolve().as_uri()
    return f"{uri}#page={page}" if path.lower().endswith(".pdf") else uri


def script_safe_json(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_html(payload: Any) -> str:
    template = resources.files("thesis_radar").joinpath("dashboard_template.html").read_text(encoding="utf-8")
    if template.count(DATA_MARKER) != 1:
        raise RuntimeError("dashboard template must contain the data marker exactly once")
    return template.replace(DATA_MARKER, script_safe_json(payload))


def write_dashboard(path: Path, html: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, path)
