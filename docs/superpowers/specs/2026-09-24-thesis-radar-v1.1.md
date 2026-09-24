# thesis-radar v1.1: design amendments and interface contract

Date: 2026-09-24
Status: implemented alongside the v1 plan
Base: `2026-09-21-thesis-radar-design.md` and `../plans/2026-09-21-thesis-radar.md`

v1.1 builds the v1 plan with the review fixes folded in and adds a set of reading,
analysis, and workflow features. Everything in the v1 spec still holds unless a
section below replaces it. The tool still never generates text: every passage shown
is verbatim, and Jev only chooses, scores, or answers yes/no.

## 1. What changed and why

### Fixes to the v1 plan

| Problem in v1 | v1.1 behavior |
|---|---|
| What's new had no time window and no way to clear items | What's new and Maybe cover documents dated within `whats_new.window_days` (default 30); Contradictions within `contradictions.window_days` (default 120). Items can be dismissed, absorbed, starred, or (contradictions) acknowledged; that triage state lives in SQLite |
| Contradictions ignored novelty and could never leave | Contradictions are grouped by assumption and can be acknowledged; acknowledged ones collapse into an "Acknowledged" list |
| `passages.text_sha256` was stored but never used | Exact repeats of an earlier passage for the same ticker are linked (`repeat_of`) and never judged or shown in feeds. Near duplicates are linked (`similar_to`, `similarity`) and shown with a word diff. Up to three similar earlier passages go into the request as `previously_seen` |
| Any thesis edit re-judged the whole history, and the dashboard emptied meanwhile | Only passages from documents within `rejudge_window_days` (config, default 120) are re-judged after a thesis edit; older ones keep their last judgment, marked `stale`. `radar judge --all` re-judges everything. The dashboard falls back to the latest judgment for any passage without a current one, marked `stale` |
| Labels were not tied to the thesis they were made under | Labels store the judgment cache keys current when they were made; calibration pairs each label with exactly those judgments. Sampled labels store an inverse-probability weight, so Brier, calibration error, precision, and recall are weighted |
| One malformed answer or unwrapped transport error aborted a run; 422s retried forever | Each request's failure is caught and recorded with `retryable`; non-retryable failures are skipped until `radar judge --retry-failed` |
| Thresholds used Jev's `confidence`, whose meaning shifts with the number of options | Pillar and metadata thresholds use the chosen option's probability |
| Ingest sent metadata requests one at a time | Metadata requests run concurrently under the same limiter |
| `radar tag` never re-split | Tagging re-splits (speaker turns for transcripts) and re-links the document |
| One EDGAR failure stopped all tickers; forms were fixed | Failures are isolated per ticker; `edgar_forms` is configurable (default adds amendments and foreign-filer forms) |
| Inline-XBRL headers and table cells produced junk text | `ix:header` and `display:none` elements are skipped; table cells are separated |
| Passages lost their antecedents | Each request carries `context`: the previous passage of the same document, for reference only |
| No schema versioning, CI, or lint | `PRAGMA user_version` migrations; ruff; GitHub Actions |

`typesafe-sdk` is on public PyPI; no extra index is needed.

### New features

1. **Triage** (keyboard): `j`/`k` move, `d` knew it, `x` doesn't matter, `a` absorb,
   `s` star, `c` acknowledge contradiction, `f` false alarm, `o` open source, `v` skim
   document, `/` search, `?` help. Every triage action also records labels. One in
   ten feed cards is a marked spot check drawn from unflagged passages, giving an
   unbiased miss-rate estimate.
2. **`radar serve`**: a localhost-only server for the dashboard, so triage, facts,
   thresholds, and prediction outcomes write straight back. The static
   `dashboard.html` still works: actions queue in the page and export as one
   `radar apply '<json>'` command.
3. **Live thresholds**: sliders re-classify in the browser from the embedded
   probabilities, with live counts, a "why am I seeing this" line on each card, and a
   copyable `policy.yaml` (or Save in serve mode).
4. **Skim mode**: a document rendered as all its passages, with boilerplate
   collapsed, new and material passages highlighted, contradictions in red, and
   `n`/`p` to jump between highlights.
5. **Overview**: per company, a pillar-by-week heat map (net stance weighted by
   materiality), per-assumption evidence balance over time, management-versus-outside
   divergence flags, guidance credibility, and predictions due.
6. **Divergence**: for each pillar, the materiality-weighted stance of company voices
   (guidance, management commentary) against outside voices (channel or customer
   data, expert opinion). A gap of `divergence.min_gap` or more is flagged.
7. **Guidance ledger**: forward-looking guidance passages become promises; later
   reported results on the same pillar are paired with them and Jev chooses
   `confirms`, `misses`, or `not_addressed`. The ledger reports kept and missed
   promises and a credibility ratio.
8. **Changed language**: for each ticker, the latest filing of each form family is
   compared with the previous one, listing changed passages (with word diffs), new
   passages, and removed passages. Deterministic; no Jev.
9. **Known-fact matching**: an `updates_fact` Choice picks which of up to five
   candidate known facts the passage updates (or `none`). Cards show "updates
   <fact>", and absorbing can replace that fact instead of appending.
10. **Open questions and predictions** in `thesis.yaml`. Passages are asked whether
    they bear on each open question. Predictions carry a probability and a date; the
    user resolves them, and the dashboard reports the user's own Brier score.
11. **Peer read-through**: `peers:` tickers are fetched from EDGAR (8-K and 6-K
    exhibit 99 by default) and judged against the host thesis, marked as read-through.
12. **Search and quotes**: SQLite FTS5 search (`radar search`, the dashboard, and
    serve), and "copy as Markdown quotes with citations" (`radar quote`).
13. **`radar mcp`**: a read-only MCP server over stdio so a Claude session can query
    the corpus with citations. The tool itself still makes no LLM calls.

## 2. Thesis file v1.1

```yaml
ticker: PII
company: Polaris Inc.
aliases: [Polaris, "Polaris Industries"]
peers: [BC, HOG]                     # optional, 0-5 tickers, read-through only
pillars:
  dealer_inventory: Units on dealer lots and how fast they sell through.
  pricing: List pricing, promotions, and discounting.
assumptions:
  inv_normalizes: {pillar: dealer_inventory, statement: "Dealer inventory returns to normal within two quarters."}
open_questions:                      # optional, 0-5
  q4_orders: Will dealers cut fourth-quarter orders?
predictions:                         # optional, 0-10
  inv_normal_by_q1: {statement: "Dealer inventory is back to normal by Q1 2027.", by: 2027-03-31, p: 0.6, pillar: dealer_inventory}
known_facts:
  dealer_inventory:
    - "Shipments down year over year; dealer inventory still elevated."
    - {text: "Promotions up 200 bps year over year.", as_of: 2026-08-05, source: 1234}
```

- A fact is a string or a mapping with `text` (required), `as_of` (date), and
  `source` (passage id). Fact ids are `<pillar>.<index>` (0-based).
- `predictions.*.p` is the user's probability (0-1); `by` is a date; `pillar`
  optional.
- Jev sees: company, pillars, assumptions, open questions, and facts as strings,
  with ` (as of YYYY-MM-DD)` appended when dated. The thesis version hashes exactly
  that; peers, predictions, and fact sources do not change it.

## 3. Requests

Passage questions, in this order: `pillar`, `boilerplate`, `new_info`,
`materiality`, `stance`, `evidence`, `forward_looking`, `updates_fact` (only when the
thesis has facts), `assumption__<id>` per assumption, `question__<id>` (Noul) per
open question. Questions are chunked in order into parts of at most 17 (`p0`, `p1`);
each part is a separate request with the same state and its own cache key.

Passage state: `company`, `pillars`, `assumptions`, `open_questions`, `known_facts`,
`document` (`source_type`, `date`, `title`, `speaker`, and `about` naming the peer
company for read-through documents), `context` (previous passage, at most 600
characters, or null), `previously_seen` (list of `{date, source_type, text}`, at most
3), `passage`, `rules`.

Follow-up request (ledger): state `company`, `promise` (`text`, `date`, `speaker`,
`source_type`), `result` (`text`, `date`, `source_type`), `rules`; one Choice
`followup` with `confirms`, `misses`, `not_addressed`.

## 4. Policy v1.1

```yaml
metadata:
  min_probability: 0.6
whats_new:
  window_days: 30
  pillar_probability: {min: 0.5}
  boilerplate: {max: 0.3}
  new_info: {min: 0.6}
  materiality: {min: 1.5}
contradictions:
  window_days: 120
  contradicts: {min: 0.7}
  materiality: {min: 1.0}
maybe:
  new_info: {between: [0.4, 0.6]}
open_questions:
  min: 0.6
divergence:
  window_days: 120
  min_gap: 1.0
  min_passages: 3
ledger:
  min_probability: 0.6
```

Classification (Python `policy.classify` and the dashboard's JavaScript must agree):

```text
on_pillar     = pillar != off_thesis and pillar_p >= pillar_probability.min
contradicts   = assumptions with P(contradicts) >= contradicts.min
supports      = assumptions with P(supports) >= contradicts.min (and not contradicted)
answers_q     = open questions with P >= open_questions.min
recent(d)     = passage date (doc date, else ingested date) >= today - d days
contra_raw    = contradicts nonempty and materiality >= contradictions.materiality.min
in_contradictions = contra_raw and recent(contradictions.window_days) and triage != acknowledged
eligible      = on_pillar and boilerplate <= max and materiality >= min and not contra_raw
                and recent(whats_new.window_days) and triage not in {dismissed, absorbed}
in_whats_new  = eligible and new_info >= new_info.min
in_maybe      = eligible and not in_whats_new and low <= new_info < high
flagged       = contra_raw or (on_pillar and boilerplate <= max and materiality >= min and new_info >= low)
```

Missing answers (stale judgments made before a question existed) count as no
evidence: absent assumptions and questions are skipped, a missing `updates_fact` is
`none`.

## 5. Data model (schema version 1)

`documents` adds `form`, `accession`. `passages` adds `repeat_of`, `similar_to`,
`similarity`, `seen_ids` (JSON list). `judgments` adds `ticker` (the thesis judged
under), `part`, `retryable`. New tables:

| Table | Columns |
|---|---|
| `triage` | `ticker`, `passage_id`, `status` (`dismissed`, `absorbed`, `acknowledged`, or null), `starred`, `updated_at`; key (`ticker`, `passage_id`) |
| `labels` | `passage_id`, `ticker`, `question`, `value`, `origin` (`sample`, `triage`, `spotcheck`), `weight`, `judgment_keys` (JSON), `labeled_at`; key (`passage_id`, `ticker`, `question`) |
| `followups` | `promise_id`, `result_id`, `ticker`, `cache_key`, `model`, `answers_json`, `status`, `error`, `retryable`, `input_tokens`, `request_id`, `created_at`; key (`promise_id`, `result_id`, `cache_key`) |
| `resolutions` | `ticker`, `prediction_id`, `outcome` (0/1), `resolved_at`; key (`ticker`, `prediction_id`) |
| `passages_fts` | FTS5 over `passages.text` (external content) |

Label questions: `new_info`, `material`, `whats_new` (composite: belongs in the feed),
`contradicts__<assumption>`.

## 6. Dashboard payload contract (`version: 2`)

The page embeds this JSON in `<script id="radar-data" type="application/json">`.

```text
Payload {
  version: 2
  mode: "static" | "serve"
  generated_at: "YYYY-MM-DDTHH:MM:SSZ"
  today: "YYYY-MM-DD"
  previous_view: timestamp | null
  policy: Policy            // current values, flat (see below)
  policy_defaults: Policy
  companies: Company[]
  unsorted: {id, title, path, reason, ticker, source_type, date, command}[]
  pending: {unjudged, failed, stale}
  serve: {token} | null     // present only in serve mode
}

Policy (flat) {
  metadata_min_probability, whats_new_window_days, pillar_probability_min, boilerplate_max,
  new_info_min, materiality_min, contradictions_window_days, contradicts_min,
  contradiction_materiality_min, maybe_new_info_low, maybe_new_info_high,
  open_questions_min, divergence_window_days, divergence_min_gap, divergence_min_passages,
  ledger_min_probability
}

Company {
  ticker, company, peers: string[]
  pillars: {id: description}
  assumptions: {id, pillar, statement}[]
  open_questions: {id, text}[]
  known_facts: {pillar: {id, text, as_of, source}[]}
  predictions: {id, statement, by, p, pillar, outcome: true|false|null, resolved_at,
                status: "open"|"due"|"overdue"|"resolved", related: passage_id[]}[]
  forecast: {resolved, brier}                 // brier null when nothing resolved
  documents: {id, title, date, source_type, form, link, ingested_at, ticker,
              read_through: bool, complete: bool, passage_count}[]
  passages: Passage[]
  spot_checks: passage_id[]                   // random unflagged, judged, recent
  heatmap: {weeks: string[], rows: {pillar: ({net, count} | null)[]}}
  assumption_series: {assumption_id: {month, for, against, balance}[]}
  divergence: {pillar, inside: {stance, n, ids}, outside: {stance, n, ids}, gap, flagged}[]
  ledger: {items: {promise_id, status: "kept"|"missed"|"open", result_id, p}[],
           kept, missed, open, credibility}
  redlines: {form, new_document, old_document,
             changed: {id, old_id, similarity, diff: [op, text][]}[],
             added: passage_id[],
             removed: {id, text, page}[]}[]
}

Passage {
  id, document_id, seq, text, page, speaker, title, date, source_type, form, link,
  ingested_at, arrived_new: bool, read_through: string | null,
  status: "current" | "stale" | "unjudged"
  p: null | {
    pillar, pillar_p, boilerplate, new_info, materiality, stance, evidence,
    forward_looking, updates_fact: fact_id | null, updates_fact_p,
    assumptions: {id: {supports, contradicts}}, questions: {id: probability}
  }
  similar: null | {id, similarity, diff: [op, text][]}   // op is "=", "+", "-"
  context: string | null
  triage: {status: null | "dismissed" | "absorbed" | "acknowledged", starred: bool}
  classified: {in_contradictions, in_whats_new, in_maybe, flagged,
               contradicts: string[], supports: string[], questions: string[]}
}
```

`passages` holds every judged passage that is not an exact repeat and is on a
pillar, above the boilerplate limit's complement (`boilerplate < 0.9`), flagged, or
from a document ingested within 60 days; plus unjudged passages from those recent
documents (`status: "unjudged"`, `p: null`). `documents[].complete` says whether all
of a document's passages are embedded (skim mode needs them; in serve mode it can
fetch `/api/document/<id>`).

## 7. Actions (`radar apply` and serve API)

Every action is a JSON object with `op`:

```text
{op: "triage",  ticker, passage_id, status?: "dismissed"|"absorbed"|"acknowledged"|null, starred?: bool}
{op: "label",   ticker, passage_id, question, value: 0|1, origin: "triage"|"spotcheck"}
{op: "fact",    ticker, pillar, text, source?: passage_id, replace?: fact_id}
{op: "resolve", ticker, prediction_id, outcome: true|false|null}
{op: "policy",  values: Policy (flat, partial allowed)}
```

`radar apply '<json array>'` (or `@file`, or `-` for stdin) applies a list. Serve
mode exposes:

| Route | Does |
|---|---|
| `GET /` | the dashboard, rendered fresh with `mode: "serve"` |
| `POST /api/actions` | body `{actions: [...]}`; applies them; returns `{ok, results}` |
| `GET /api/document/<id>?ticker=T` | `{document, passages: Passage[]}` |
| `GET /api/search?q=...&ticker=T&limit=N` | `{results: {passage: Passage, snippet}[]}` |

Serve binds 127.0.0.1 only, requires the header `X-Radar-Token` (the payload's
`serve.token`) on every `/api` request, and rejects any `Host` other than
`127.0.0.1:<port>` or `localhost:<port>`.

Implicit labels sent by the dashboard: `d` gives `new_info=0`, `whats_new=0`; `x`
gives `material=0`, `whats_new=0`; `a` gives `new_info=1`, `material=1`,
`whats_new=1`; `s` (star on) gives `material=1`, `whats_new=1`; `c` gives
`contradicts__<id>=1` for each flagged assumption; `f` gives `contradicts__<id>=0`;
a spot-check `y`/`n` gives `whats_new=1/0` with origin `spotcheck`.

## 8. CLI v1.1

| Command | Does |
|---|---|
| `radar fetch` | EDGAR for thesis tickers and peers |
| `radar ingest` | Process `inbox/` |
| `radar judge [--dry-run] [--yes] [--all] [--retry-failed]` | Judge passages, then ledger follow-ups |
| `radar view` | Write `dashboard.html` |
| `radar run [--yes]` | fetch, ingest, judge, view |
| `radar serve [--port 8765] [--no-browser]` | Serve the dashboard on localhost with write-back |
| `radar apply ACTIONS` | Apply queued dashboard actions |
| `radar tag DOC --ticker --source --date` | Resolve an unsorted document |
| `radar absorb IDS...` | Print passages beside current facts |
| `radar fact TICKER PILLAR TEXT [--source ID] [--replace FACT_ID]` | Add or replace a known fact |
| `radar resolve TICKER PREDICTION yes/no/clear` | Record a prediction outcome |
| `radar search QUERY [--ticker] [--limit]` | Full-text search |
| `radar quote IDS...` | Markdown quotes with citations |
| `radar label` / `radar calibrate` | As in v1, with weights, keys, triage, and spot checks |
| `radar mcp` | Read-only MCP server over stdio |
