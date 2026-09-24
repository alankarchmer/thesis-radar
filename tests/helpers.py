"""Shared test helpers. Nothing here imports thesis_radar."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ACME_THESIS = """\
ticker: ACME
company: ACME Snowmobiles Inc.
aliases: [ACME, "ACME Snowmobiles"]
pillars:
  inventory: Units on dealer lots and how fast they sell through.
  pricing: List pricing, promotions, and discounting.
assumptions:
  inv_normalizes: {pillar: inventory, statement: "Dealer inventory returns to normal within two quarters."}
known_facts:
  inventory:
    - "Dealer inventory was elevated at the end of Q2."
"""


def write_thesis(directory: Path, ticker: str = "ACME", text: str = ACME_THESIS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticker}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _confidence(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    count = len(values)
    return max(0.0, min(1.0, (count * max(values) - 1) / (count - 1)))


def noul(probability: float) -> dict[str, Any]:
    return {"type": "noul", "noul": probability}


def choice(selected: str, probabilities: dict[str, float]) -> dict[str, Any]:
    return {
        "type": "choice",
        "choice": selected,
        "probabilities": dict(probabilities),
        "confidence": _confidence(list(probabilities.values())),
    }


def choice_from(question: dict[str, Any], selected: str, probability: float) -> dict[str, Any]:
    """A Choice answer over the question's own options, with `probability` on `selected`."""
    options = list(question["criteria"])
    assert selected in options, f"{selected!r} is not an option of {options}"
    if len(options) == 1:
        return choice(selected, {selected: 1.0})
    rest = (1.0 - probability) / (len(options) - 1)
    probabilities = {option: rest for option in options}
    probabilities[selected] = probability
    return choice(selected, probabilities)


def score(probabilities: list[float]) -> dict[str, Any]:
    expected = sum(level * p for level, p in enumerate(probabilities))
    return {
        "type": "score",
        "score": expected,
        "probabilities": {str(level): p for level, p in enumerate(probabilities)},
        "confidence": _confidence(list(probabilities)),
    }


def answer_all(request, **overrides: Any) -> dict[str, Any]:
    """A plausible answer for every question in a passage request; `overrides` replace by name."""
    answers: dict[str, Any] = {}
    for name, question in request.questions.items():
        if name in overrides:
            answers[name] = overrides[name]
        elif question["type"] == "noul":
            answers[name] = noul(0.1)
        elif question["type"] == "score":
            levels = len(question["criteria"])
            answers[name] = score([1.0 if i == levels // 2 else 0.0 for i in range(levels)])
        else:
            options = list(question["criteria"])
            pick = "none" if "none" in options else ("neither" if "neither" in options else options[0])
            answers[name] = choice_from(question, pick, 0.9)
    return answers


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str], title: str | None = None) -> bytes:
    """A minimal valid PDF with one line of Helvetica text per page."""
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = []
    number = 4
    for text in pages:
        page_number, content_number = number, number + 1
        number += 2
        kids.append(page_number)
        stream = f"BT /F1 12 Tf 72 720 Td ({_pdf_escape(text)}) Tj ET".encode("latin-1")
        bodies[page_number] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
        ).encode()
        bodies[content_number] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    kid_refs = " ".join(f"{kid} 0 R" for kid in kids)
    bodies[2] = f"<< /Type /Pages /Kids [{kid_refs}] /Count {len(kids)} >>".encode()
    info = None
    if title is not None:
        info = number
        bodies[info] = f"<< /Title ({_pdf_escape(title)}) >>".encode("latin-1")
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for obj in sorted(bodies):
        offsets[obj] = len(out)
        out += f"{obj} 0 obj\n".encode() + bodies[obj] + b"\nendobj\n"
    xref = len(out)
    size = max(bodies) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for obj in range(1, size):
        out += f"{offsets[obj]:010d} 00000 n \n".encode()
    info_ref = f" /Info {info} 0 R" if info is not None else ""
    out += f"trailer\n<< /Size {size} /Root 1 0 R{info_ref} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def payload_from_html(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="radar-data" type="application/json">(.*?)</script>', html, re.S)
    assert match, "dashboard has no embedded data"
    return json.loads(match.group(1))
