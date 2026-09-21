# thesis-radar design

Date: 2026-09-21
Status: approved in brainstorming, awaiting spec review

## Problem

Research on a handful of companies arrives faster than it can be read: filings,
earnings-call transcripts, sell-side reports, expert-call transcripts, AI
deep-research reports, and personal notes. The hard part is not finding documents
but knowing which passages say something **new relative to what I already know**
and **relevant to my thesis**, and noticing when new evidence **contradicts** an
assumption the thesis rests on.

## Goal

A personal research tool for 1-5 deeply followed companies. Every new passage from
every source is judged against a per-company thesis by TypeSafe's Jev model, and an
offline dashboard shows what is new, material, and on-thesis, with contradictions
pinned first. The tool never generates text: every passage shown is verbatim with a
link to its source.

## Non-goals (v1)

- No LLM calls inside the tool. Claude helps only outside it (drafting and
  updating `thesis.yaml` in a Claude Code session).
- No OCR, no numeric table parsing, no numeric time series extraction.
- No trading, recommendations, price targets, or position sizing.
- No push alerts, daily digests, or email ingestion.
- No SM (state-machine) integration. A later `decide`-style workflow for recording
  thesis changes is listed under future work.
- No multi-user support or server. The dashboard is a static file.

## Constraints and decisions

| Decision | Choice |
|---|---|
| Users | One person, 1-5 companies |
| Sources | Filings, transcripts, sell-side, expert calls, AI reports, own notes; all may be sent to Jev |
| Ingestion | A watched `inbox/` folder plus automatic SEC EDGAR fetch |
| Delivery | A self-contained `dashboard.html`, regenerated per run |
| Language | Python >= 3.11, managed with `uv` |
| Model | `jev-1.13.0`, pinned; never an alias |
| Storage | One SQLite file |
| Novelty | Judged against the thesis's `known_facts`, not by date alone |

## Repository and workspace

The tool (this repository) and the data (a private workspace) are separate. Licensed
research must never be committed to the tool's repository.

```text
thesis-radar/                 # this repo: code, tests, docs
~/research/                   # workspace; path from --workspace or RADAR_HOME, default cwd
  config.yaml                 # EDGAR contact email, model, concurrency, cost cap
  policy.yaml                 # thresholds applied at view time
  thesis/<TICKER>.yaml        # one per company
  inbox/                      # files dropped here; inbox/_failed/ for failures
  archive/<TICKER>/           # processed files, renamed <date>_<source>_<title>.<ext>
  radar.db                    # SQLite
  radar.lock                  # exclusive run lock
  dashboard.html
```

Dependencies: `typesafe-sdk>=0.5.7` (from `https://pypi.typesafe.ai/` as an extra
uv index), `pypdf`, `python-docx`, `pyyaml`. Development: `pytest`.

## Architecture

```text
inbox/ ──────────┐
EDGAR fetcher ───┴─▶ ingest ─▶ split ─▶ judge (Jev) ─▶ SQLite ─▶ dashboard.html
                                          ▲                         ▲
                     thesis/<TICKER>.yaml ┘          policy.yaml ───┘
```

| Unit | Purpose | Depends on |
|---|---|---|
| `thesis` | Load and validate thesis files; produce a canonical form and version hash | `pyyaml` |
| `ingest` | Extract text, dedupe, resolve metadata, archive or fail each inbox file | extractors, `judge` |
| `extract` | One function per format returning text with page boundaries | `pypdf`, `python-docx`, stdlib |
| `edgar` | Fetch new 10-K, 10-Q, 8-K primary documents for workspace tickers | SEC HTTP API |
| `split` | Cut a document into passages with page and character offsets | none |
| `judge` | Build Jev requests, cache results, rate-limit, enforce cost cap | `Judge` implementations |
| `store` | SQLite schema and queries | stdlib `sqlite3` |
| `policy` | Apply `policy.yaml` thresholds to stored probabilities | `store` |
| `dashboard` | Render one self-contained HTML file | `store`, `policy` |
| `cli` | The `radar` command | all of the above |

### Judge interface

```python
class Judge(Protocol):
    def judge_document(self, probe: DocumentProbe) -> DocumentJudgment: ...
    def judge_passage(self, request: PassageRequest) -> PassageJudgment: ...
```

Both return the full probability distribution of every answer plus the versioned
model ID reported by the API. v1 ships `JevJudge` (live API) and `FakeJudge`
(replays recorded answers from JSON fixtures). A Claude-based or local-model judge
can be added later without touching other units.

## Thesis file

```yaml
ticker: PII
company: Polaris Inc.
aliases: [Polaris, "Polaris Industries"]
pillars:
  dealer_inventory: Units on dealer lots and how fast they sell through.
  pricing: List pricing, promotions, and discounting.
assumptions:
  inv_normalizes: {pillar: dealer_inventory, statement: "Dealer inventory returns to normal within two quarters."}
known_facts:
  dealer_inventory:
    - "Shipments down year over year; dealer inventory still elevated (10-Q, 2026-08)."
  pricing: []
```

Validation, at load time, with file, field, and reason on failure:

- `ticker` is required, uppercase, and matches the file name.
- 1-12 pillars; keys are identifiers; descriptions are non-empty strings.
- 0-10 assumptions; each names an existing pillar.
- `known_facts` keys must be existing pillars; at most 20 facts per pillar.
- Every identifier must load as a string (the same YAML 1.1 boolean trap SM guards against).

A ticker whose thesis fails validation is skipped for judging; other tickers still run.

## Data model

| Table | Columns |
|---|---|
| `documents` | `id`, `text_sha256` (unique), `path`, `ticker`, `ticker_p`, `source_type`, `source_type_p`, `doc_date`, `doc_date_p`, `title`, `origin` (`inbox` or `edgar`), `status` (`sorted`, `unsorted`, `failed`), `status_reason`, `ingested_at` |
| `passages` | `id`, `document_id`, `seq`, `page`, `char_start`, `char_end`, `speaker`, `text`, `text_sha256` |
| `judgments` | `passage_id`, `cache_key`, `model`, `rubric_version`, `thesis_version`, `answers_json`, `status` (`judged`, `failed`), `error`, `created_at` |
| `views` | `id`, `generated_at` |
| `labels` | `passage_id`, `question`, `value`, `labeled_at` |
| `fetch_state` | `ticker`, `cik`, `last_accession`, `fetched_at` |

`answers_json` stores every answer's full probabilities, never thresholded values.
The primary key of `judgments` is `(passage_id, cache_key)`. Rows from earlier
cache keys are kept; a passage's current judgment is the row whose cache key
matches the current inputs. Only passages of `sorted` documents are judged, since
an unsorted document has no thesis to judge against.

`title` comes from PDF metadata when present, otherwise the first non-empty line
of text; it is never produced by Jev.

**Cache key:** `sha256(passage text, canonical thesis JSON for the ticker,
RUBRIC_VERSION, model)`. Any thesis edit re-judges every passage for that ticker.
This is intended: "new" always means new relative to the current `known_facts`.

## Jev requests

All question wording lives in `rubric.py` alongside `RUBRIC_VERSION`. Changing any
wording bumps the version. Criteria use TypeSafe's structured form (what counts,
what does not, examples).

### Document metadata (once per inbox document; EDGAR documents skip it)

State: file name, extracted title, and the first ~2,000 tokens of text.

| Id | Type | Options |
|---|---|---|
| `ticker` | Choice | Each workspace ticker, described with company name and aliases; plus `none` |
| `source_type` | Choice | `filing`, `earnings_transcript`, `sell_side`, `expert_call`, `ai_research`, `own_note`, `news`, `other` |
| `doc_date` | Choice | Date strings found in the text by regex, plus `none` |

Jev selects a date; it never writes one. If any answer's confidence is below the
`metadata.min_confidence` policy value, or the answer is `none`, the document is
`unsorted`.

### Passage judgment (once per passage)

State: `company` (name, ticker), `pillars`, `assumptions`, `known_facts`,
`document` (source type, date, title, speaker), `passage` (text).

| Id | Type | Question |
|---|---|---|
| `pillar` | Choice | Which pillar is `passage` about? Options: each pillar, plus `off_thesis` |
| `boilerplate` | Noul | Is `passage` a legal disclaimer, safe-harbor statement, generic risk language, or table of contents? |
| `new_info` | Noul | Does `passage` state a fact or development about `company` not already captured in `known_facts`? |
| `assumption:<id>` | Choice | Does `passage` `supports`, `contradicts`, or `neither` the assumption `<statement>`? One per assumption |
| `materiality` | Score | 0 no bearing on the investment case; 1 minor color; 2 would shift an estimate or confidence in a pillar; 3 could change the thesis on its own |
| `stance` | Score | For the aspect of the business `passage` discusses: 0 clearly negative for the company; 1 somewhat negative; 2 neutral or mixed; 3 somewhat positive; 4 clearly positive |
| `evidence` | Choice | `reported_result`, `guidance`, `management_commentary`, `channel_or_customer_data`, `expert_opinion`, `analyst_opinion`, `speculation` |
| `forward_looking` | Noul | Is `passage` about expected future developments rather than past results? |

At most 17 questions per request. Every assumption question is asked for every
passage, whatever its pillar.

## Policy

`policy.yaml` defaults, applied at view time only:

```yaml
metadata:
  min_confidence: 0.6
whats_new:
  pillar_confidence: {min: 0.5}   # and pillar != off_thesis
  boilerplate: {max: 0.3}
  new_info: {min: 0.6}
  materiality: {min: 1.5}
contradictions:
  contradicts: {min: 0.7}         # P(contradicts) for any assumption
  materiality: {min: 1.0}
maybe:
  new_info: {between: [0.4, 0.6]} # plus the whats_new rules other than new_info
```

Changing policy never calls Jev.

## Ingestion

Per inbox file:

1. Extract text by extension: PDF (`pypdf`, with pages), HTML (stdlib parser),
   DOCX (`python-docx`), TXT and MD (read directly). Unsupported extensions fail.
2. A PDF with no extractable text is `unsorted` with reason `needs OCR`.
3. Normalize whitespace and hash. A matching `text_sha256` is a duplicate: the file
   moves to the archive under the existing document and nothing else happens.
4. Resolve metadata with the document request above.
5. Move the file to `archive/<TICKER>/<date>_<source>_<title>.<ext>`, or to
   `archive/_unsorted/` while unsorted.
6. On any failure, move the file to `inbox/_failed/` with a `<name>.reason.txt`.

Files are never deleted.

### EDGAR

`radar fetch` maps tickers to CIKs from `https://www.sec.gov/files/company_tickers.json`,
reads `https://data.sec.gov/submissions/CIK##########.json`, and downloads the
primary document of each 10-K, 10-Q, and 8-K newer than `fetch_state.last_accession`.
For an 8-K it also downloads every HTML file in the filing's `index.json` whose name
contains `ex99` (case-insensitive), since earnings releases usually live in exhibit
99. This is a file-name heuristic; missed exhibits can still be dropped in `inbox/`.
Requests carry a `User-Agent` with the contact email from `config.yaml` and stay
under 10 requests per second. Metadata comes from EDGAR, not Jev.

## Splitting

- Split on paragraph boundaries. Merge consecutive short paragraphs up to about 250
  tokens. Split paragraphs over about 400 tokens at sentence boundaries.
- Tokens are estimated as characters / 4.
- Transcripts: detect speaker labels (`Operator:`, `Name - Title:` and similar);
  never merge across speakers; record `speaker` on each passage.
- Every passage records `page`, `char_start`, and `char_end`.

## Dashboard

One self-contained `dashboard.html`: inline CSS and JS, no network, no external
assets. Data is embedded as script-safe escaped JSON, and all passage text is
inserted with `textContent`, never `innerHTML`.

Views, per company tab:

1. **Contradictions**, pinned at top.
2. **What's new**, grouped by pillar, newest first (by `doc_date`, then
   `ingested_at`). A dot marks documents ingested
   after the previous `views.generated_at`.
3. **Maybe**, collapsed.
4. **Assumptions**: for and against counts and passages over time.
5. **Pillar timelines**: one strip per pillar; color is stance, size is materiality,
   shape is source type.
6. **Unsorted** (global): each document shows a copyable `radar tag` command.

Each card shows the verbatim passage, source type, document title, date, page link
(`file://...#page=N` for PDFs), materiality, stance, evidence type, and assumption
badges. Filters for source type, date range, and evidence type run client-side.

A banner shows the count of unjudged or failed passages when nonzero.

### Absorbing facts

Cards have an `absorb` checkbox and a **Copy absorb list** button that copies
`radar absorb <ids>`. That command prints the chosen passages grouped by pillar
next to that pillar's current `known_facts`, formatted for editing or pasting into
a Claude session. The person edits `thesis.yaml`; the next `radar run` re-judges,
and absorbed passages leave What's new.

## CLI

| Command | Does |
|---|---|
| `radar fetch` | Fetch new EDGAR filings into the pipeline |
| `radar ingest` | Process `inbox/` |
| `radar judge [--dry-run] [--yes]` | Judge unjudged or stale passages; `--dry-run` prints count and estimated cost |
| `radar view` | Render `dashboard.html` and record a view |
| `radar run` | `fetch`, `ingest`, `judge`, `view` in order |
| `radar tag <doc> --ticker --source --date` | Resolve an unsorted document |
| `radar absorb <passage ids>` | Print passages beside current facts for editing |
| `radar label [--n N] [--ticker T]` | Interactive labeling of a random passage sample |
| `radar calibrate [--ticker T]` | Reliability and threshold report from labels |

All mutating commands take an exclusive lock on `radar.lock` and refuse if it is held.

## Error handling

| Failure | Behavior |
|---|---|
| Jev 429 or 529 | SDK retries with backoff; if still failing, the passage stays unjudged and is retried next run |
| Jev 422 | Judgment `failed` with the error; shown in the banner |
| Missing `TYPESAFE_API_KEY` | `judge` and `ingest` exit with a clear message before touching any file (`ingest` needs Jev for metadata); `fetch` and `view` still work |
| Invalid thesis | That ticker skipped with file, field, reason |
| Extraction failure | File to `inbox/_failed/` with a reason file |
| Estimated cost above `max_cost_per_run` (default $2) | `judge` stops before sending unless `--yes` |
| Lock held | Command refuses and names the lock file |

Concurrency defaults to 8 requests in flight behind a 1,200 requests/minute limiter.
Cost is estimated from token counts at $0.042 per million input tokens.

## Testing

- Unit tests (offline): thesis validation, each extractor, splitting (including
  speaker turns and size limits), cache-key stability, policy application, and
  dashboard escaping (a passage containing `<script>` renders as text).
- End-to-end test with `FakeJudge` over a small fictional corpus ("ACME
  Snowmobiles"): ingest, judge, view; assert which passages land in Contradictions,
  What's new, Maybe, and Unsorted.
- One opt-in live test, `@pytest.mark.live`, against the real API.
- Tests never require an API key unless marked live.

## Calibration

- `radar label` presents sampled passages and records `new_info`, `material`, and
  per-assumption `contradicts` labels.
- `radar calibrate` reports, per question: accuracy by probability bucket,
  precision and recall at current thresholds, and the threshold that reaches a
  requested precision.
- First-week protocol: label about 200 passages for one company. If no `new_info`
  threshold reaches about 70% precision, revise the rubric wording before relying
  on the feed.

## Success criteria (v1 targets to measure)

1. On a normal week, What's new shows at most 20 items per company, and at least 80%
   are ones the user agrees matter.
2. In a labeled sample, at least 80% of new and material passages appear in What's
   new or Maybe.
3. Jev cost under $1 per week outside earnings season.

## Future work

- A `decide`-style SM workflow for recording thesis changes with a run log.
- Numeric extraction into metric time series (code finds candidates, Jev selects).
- OCR for scanned PDFs.
- Alerts when a contradiction appears.
- Alternative `Judge` implementations if Jev underperforms on this domain.
