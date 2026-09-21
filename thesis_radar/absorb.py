"""Format chosen passages beside the thesis's current known_facts, ready to edit."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from .rubric import OFF_THESIS
from .store import Store
from .thesis import Thesis


def absorb_text(store: Store, theses: Mapping[str, Thesis], passage_ids: Sequence[int]) -> str:
    rows = store.get_passages(passage_ids)
    missing = sorted(set(passage_ids) - {row["passage_id"] for row in rows})
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        answers = store.latest_judged(row["passage_id"])
        pillar = answers["pillar"]["choice"] if answers and "pillar" in answers else OFF_THESIS
        groups[(row["ticker"] or "UNSORTED", pillar)].append(row)

    lines: list[str] = []
    for (ticker, pillar), items in sorted(groups.items()):
        lines.append(f"# {ticker} / {pillar}")
        thesis = theses.get(ticker)
        if thesis is not None and pillar in thesis.pillars:
            lines.append(f"Current known_facts.{pillar} in thesis/{ticker}.yaml:")
            facts = thesis.known_facts.get(pillar, ())
            if facts:
                lines.extend(f"  - {fact}" for fact in facts)
            else:
                lines.append("  (none)")
        lines.append("Passages to absorb:")
        for row in items:
            speaker = f"{row['speaker']}: " if row["speaker"] else ""
            lines.append(
                f"  [{row['passage_id']}] {row['source_type'] or 'unknown'} · {row['doc_date'] or 'undated'}"
                f" · {row['title']} · p.{row['page']}"
            )
            lines.append(f"      {speaker}{row['text']}")
        lines.append("")
    if missing:
        lines.append(f"Unknown passage ids: {', '.join(str(pid) for pid in missing)}")
    lines.append("Add one short line per new fact under known_facts in the thesis file, then run `radar run`.")
    return "\n".join(lines)
