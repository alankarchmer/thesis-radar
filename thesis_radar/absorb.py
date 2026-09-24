"""Format chosen passages beside the thesis's current known_facts, ready to fold in."""

from __future__ import annotations

import shlex
from collections import defaultdict
from collections.abc import Mapping, Sequence

from .policy import signals
from .rubric import OFF_THESIS
from .store import Store
from .thesis import Thesis


def thesis_for(theses: Mapping[str, Thesis], document_ticker: str | None) -> Thesis | None:
    """The thesis a passage is judged under: its own company's, else the first thesis listing it as a peer."""
    if document_ticker is None:
        return None
    if document_ticker in theses:
        return theses[document_ticker]
    return next((t for _, t in sorted(theses.items()) if document_ticker in t.peers), None)


def absorb_text(store: Store, theses: Mapping[str, Thesis], passage_ids: Sequence[int]) -> str:
    rows = store.get_passages(passage_ids)
    missing = sorted(set(passage_ids) - {row["passage_id"] for row in rows})
    groups: dict[tuple[str, str], list[tuple[object, dict | None]]] = defaultdict(list)
    for row in rows:
        thesis = thesis_for(theses, row["ticker"])
        ticker = thesis.ticker if thesis else (row["ticker"] or "UNSORTED")
        p = signals(store.latest_judged(row["passage_id"], ticker)) if thesis else None
        pillar = p["pillar"] if p else OFF_THESIS
        groups[(ticker, pillar)].append((row, p))

    lines: list[str] = []
    for (ticker, pillar), items in sorted(groups.items()):
        lines.append(f"# {ticker} / {pillar}")
        thesis = theses.get(ticker)
        if thesis is not None and pillar in thesis.pillars:
            lines.append(f"Current known_facts.{pillar} in thesis/{ticker}.yaml:")
            facts = thesis.known_facts.get(pillar, ())
            if facts:
                lines.extend(f"  [{f.id}] {f.for_jev()}" for f in facts)
            else:
                lines.append("  (none)")
        lines.append("Passages to absorb:")
        for row, p in items:
            speaker = f"{row['speaker']}: " if row["speaker"] else ""
            read_through = f" · read-through: {row['ticker']}" if thesis and row["ticker"] != thesis.ticker else ""
            lines.append(
                f"  [{row['passage_id']}] {row['source_type'] or 'unknown'} · {row['doc_date'] or 'undated'}"
                f" · {row['title']} · p.{row['page']}{read_through}"
            )
            lines.append(f"      {speaker}{row['text']}")
            replaces = None
            if p and p.get("updates_fact") and (p.get("updates_fact_p") or 0) >= 0.5 and thesis and thesis.fact(p["updates_fact"]):
                replaces = p["updates_fact"]
                lines.append(f"      updates [{replaces}] {thesis.fact(replaces).text}")  # type: ignore[union-attr]
            if thesis is not None and pillar in thesis.pillars:
                command = ["radar", "fact", ticker, pillar, "<one short line>", "--source", str(row["passage_id"])]
                if replaces:
                    command += ["--replace", replaces]
                lines.append("      " + shlex.join(command))
        lines.append("")
    if missing:
        lines.append(f"Unknown passage ids: {', '.join(str(pid) for pid in missing)}")
    lines.append("Add one short line per new fact (radar fact ... or edit the thesis file), then run `radar run`.")
    return "\n".join(lines)
