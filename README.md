# thesis-radar

`radar` reads the research you collect on a few companies (filings, earnings-call
transcripts, sell-side reports, expert calls, AI research, your own notes) and uses
TypeSafe's Jev model to judge every passage against your investment thesis. It shows
what is new to you, what matters, and what contradicts your assumptions, in a dashboard
you triage from the keyboard. It never writes text of its own: every passage it shows
is quoted verbatim with a link to its source, and Jev only chooses, scores, or answers
yes/no.

## Install

```bash
uv tool install --editable .
export TYPESAFE_API_KEY=...        # ingest and judge need it; everything else works offline
```

## Set up a workspace

Keep your research outside this repository (it is licensed material and never belongs in
git). Point `radar` at a folder with `--workspace ~/research` or
`export RADAR_HOME=~/research`, then:

```bash
radar init          # creates inbox/, archive/, thesis/, config.yaml, and policy.yaml
```

Write one thesis file per company, named after its ticker, for example `thesis/PII.yaml`:

```yaml
ticker: PII
company: Polaris Inc.
aliases: [Polaris, "Polaris Industries"]
peers: [BC, HOG]                  # optional: their earnings releases are read against your thesis
pillars:
  dealer_inventory: Units on dealer lots and how fast they sell through.
  pricing: List pricing, promotions, and discounting.
assumptions:
  inv_normalizes: {pillar: dealer_inventory, statement: "Dealer inventory returns to normal within two quarters."}
open_questions:                   # optional: passages that bear on them are collected
  q4_orders: Will dealers cut fourth-quarter orders?
predictions:                      # optional: your own forecasts, scored when you resolve them
  inv_normal_by_q1: {statement: "Dealer inventory is back to normal by Q1 2027.", by: 2027-03-31, p: 0.6}
known_facts:                      # what you already know; novelty is judged against it
  dealer_inventory:
    - "Shipments down year over year; dealer inventory still elevated."
    - {text: "Promotions up 200 bps year over year.", as_of: 2026-08-05, source: 1234}
```

Limits: 1-12 pillars, 10 assumptions, 5 open questions, 10 predictions, 5 peers, 20 facts
per pillar. Quote any key YAML might read as a boolean (`yes`, `no`, `on`, `off`).

`config.yaml` (every key optional):

```yaml
edgar_email: you@example.com   # enables SEC EDGAR fetching (SEC asks for a contact)
model: jev-1.13.0              # pinned; aliases such as jev-latest are refused
concurrency: 16                # Jev requests in flight
max_cost_per_run: 2.0          # dollars; judge stops before sending more unless --yes
rejudge_window_days: 120       # after a thesis edit, re-judge only documents this recent
edgar_forms: [10-K, 10-K/A, 10-Q, 10-Q/A, 8-K, 20-F, 40-F, 6-K]
peer_forms: [8-K, 6-K]         # peers: exhibit 99 (earnings releases) only
serve_port: 8765
```

`policy.yaml` holds the dashboard thresholds (`radar init` writes the defaults; the
dashboard's threshold panel can write it for you). Changing policy never calls Jev.

## Daily use

```bash
radar run      # fetch filings, ingest inbox/, judge, write dashboard.html
radar serve    # open the dashboard on localhost with write-back (recommended)
```

Drop downloaded files (PDF, HTML, DOCX, TXT, MD) into `inbox/`. Files Jev cannot place
confidently appear under **Unsorted** with a `radar tag` command to copy.

The dashboard has an **Overview** (heat map of stance by pillar and week, evidence balance
per assumption, management-versus-outside divergence, guidance credibility, predictions
due) and a tab per company: **Contradictions** (grouped by assumption), **What's new**,
**Maybe**, **Open questions**, **Guidance ledger**, **Changed language** (filing-to-filing
redlines), **Predictions**, and **Known facts**.

Triage from the keyboard; every action also teaches the calibration report:

| Key | Does |
|---|---|
| `j` / `k` | next / previous card |
| `d` | knew it (not new) |
| `x` | doesn't matter |
| `a` | absorb: mark read and optionally write a known fact (can replace the fact it updates) |
| `s` | star |
| `c` / `f` | acknowledge a contradiction / mark it a false alarm |
| `v` | skim the whole document, `n` / `p` to jump between highlights |
| `o` | open the source at its page |
| `u` | undo |
| `/` | search |
| `?` | all shortcuts |

One card in ten is a marked **spot check**: a random passage the filters left out. Answer
`y` or `n`; that is how the tool estimates what it misses.

With `radar serve`, actions save immediately. With the static `dashboard.html`, actions
queue at the bottom of the page as one `radar apply '...'` command to paste into a
terminal.

| Command | Does |
|---|---|
| `radar init` | Create the workspace layout, `config.yaml`, and `policy.yaml` |
| `radar status` | Documents, judgments (current, stale, failed), pending cost, spending this month |
| `radar fetch` | Fetch new filings from SEC EDGAR for thesis tickers and peers |
| `radar ingest` | Process `inbox/` |
| `radar judge [--dry-run] [--yes] [--all] [--retry-failed]` | Judge pending passages, then guidance follow-ups |
| `radar view` | Write `dashboard.html` |
| `radar run [--yes]` | fetch, ingest, judge, view |
| `radar serve [--port N] [--no-browser]` | Serve the dashboard on 127.0.0.1 with write-back |
| `radar apply ACTIONS` | Apply actions queued in the static dashboard (JSON, `@file`, or `-`) |
| `radar tag DOC --ticker T --source S --date YYYY-MM-DD` | Resolve an unsorted document |
| `radar absorb ID...` | Print passages beside current facts, with ready `radar fact` commands |
| `radar fact T PILLAR TEXT [--source ID] [--replace FACT]` | Add or replace a known fact (keeps your comments) |
| `radar resolve T PREDICTION yes/no/clear` | Record a prediction's outcome |
| `radar search QUERY [--ticker T]` | Full-text search (`"phrases"`, `prefix*`, `OR`) |
| `radar quote ID...` | Markdown quotes with citations for memos |
| `radar label [--ticker T] [--n 20]` | Label a weighted random sample of passages |
| `radar calibrate [--ticker T] [--precision P] [--recall R]` | Report Jev's accuracy against your labels |
| `radar mcp` | Read-only MCP server over stdio for a Claude session |

### What gets judged, and when

- An exact repeat of an earlier passage for the same company (risk factors copied from the
  last 10-Q, a broker's disclaimer) is linked to the original and never judged or shown.
- A near duplicate is shown with a word diff against the earlier version, and the earlier
  version is given to Jev as `previously_seen`, so a changed number counts as new and a
  paraphrase does not.
- Editing a thesis re-judges passages from the last `rejudge_window_days`; older ones keep
  their last judgment (marked stale) until `radar judge --all`. Until a passage is
  re-judged, the dashboard shows its previous judgment, marked stale.
- A request Jev rejects as invalid is not retried every run; `--retry-failed` sends it again.

## Trust the feed only after calibrating it

Jev's probabilities are only useful once checked against your own judgment. In your first
week, run `radar label` until you have labeled about 200 passages for one company, then
`radar calibrate`. It splits your labels by document, picks thresholds on one half, and
reports on the other half: precision and recall with 95% intervals, a threshold for your
precision target and one for your recall target, Brier score, calibration error, and how
often Jev was confidently wrong. Sampled labels are weighted, because half of each sample
is drawn from flagged passages. Each label is compared with the judgment that was current
when you made it, so absorbing facts later never skews old labels. The report also shows
feed precision from your triage and the miss rate from spot checks. For contradictions,
prefer the recall threshold. If no threshold reaches about 70% precision for `new_info`,
change the wording in `thesis_radar/rubric.py` (and bump `RUBRIC_VERSION`) before relying
on the feed.

## Use it from Claude

`radar mcp` exposes the corpus read-only (companies, theses, What's new, contradictions,
search, passages, documents), with a citation on every passage:

```bash
claude mcp add thesis-radar -- radar --workspace ~/research mcp
```

## Development

```bash
uv sync
uv run pytest            # offline; never needs an API key
uv run pytest -m ui      # dashboard in headless Chromium (needs playwright browsers)
uv run pytest -m live -s # one real Jev call; needs TYPESAFE_API_KEY
uv run ruff check .
```

Design: `docs/superpowers/specs/2026-09-21-thesis-radar-design.md` (v1) and
`docs/superpowers/specs/2026-09-24-thesis-radar-v1.1.md` (v1.1 amendments and the
dashboard, action, and serve contracts).
