# thesis-radar

`radar` reads the research you collect on a few companies (filings, earnings-call
transcripts, sell-side reports, expert calls, AI research, your own notes) and uses
TypeSafe's Jev model to judge every passage against your investment thesis. It writes
an offline dashboard showing what is new to you, what matters, and what contradicts
your assumptions. It never writes text of its own: every passage it shows is quoted
verbatim with a link to its source.

## Install

```bash
uv tool install --editable .
```

Set `TYPESAFE_API_KEY` in your environment.

## Set up a workspace

Keep your research outside this repository. Create a folder such as `~/research` and
point `radar` at it with `--workspace ~/research` or `export RADAR_HOME=~/research`.
The first command creates `inbox/`, `archive/`, and `thesis/` inside it.

Write one thesis file per company, named after its ticker, for example
`thesis/PII.yaml`:

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
```

Optional `config.yaml`:

```yaml
edgar_email: you@example.com   # required for SEC EDGAR fetching
model: jev-1.13.0
concurrency: 16
max_cost_per_run: 2.0
```

Optional `policy.yaml` overrides the dashboard thresholds (see the design spec).

## Daily use

```bash
radar run                 # fetch filings, ingest inbox/, judge, write dashboard.html
open ~/research/dashboard.html
```

Drop downloaded files (PDF, HTML, DOCX, TXT, MD) into `inbox/`. Files Jev cannot place
confidently appear under **Unsorted** with a `radar tag` command to copy.

When you have taken in a passage, tick **absorb** on its card, copy the command, and run
it. `radar absorb` prints the passages beside your current `known_facts`; add short fact
lines to the thesis file and the next `radar run` stops showing them as new.

| Command | Does |
|---|---|
| `radar fetch` | Fetch new 10-K, 10-Q, and 8-K filings from SEC EDGAR |
| `radar ingest` | Process `inbox/` |
| `radar judge [--dry-run] [--yes]` | Judge pending passages; `--dry-run` prints count and cost |
| `radar view` | Write `dashboard.html` |
| `radar run [--yes]` | All of the above, in order |
| `radar tag DOC --ticker T --source S --date YYYY-MM-DD` | Resolve an unsorted document |
| `radar absorb ID...` | Print passages to fold into `known_facts` |
| `radar label [--ticker T] [--n 20]` | Label a sample of passages |
| `radar calibrate [--ticker T] [--precision 0.8] [--recall 0.9]` | Report Jev's accuracy against your labels |

## Trust the feed only after calibrating it

Jev's probabilities are only useful once checked against your own judgment. In your first
week, run `radar label` until you have labeled about 200 passages for one company, then
`radar calibrate`. It splits your labels by document, picks thresholds on one half, and
reports on the other half: precision and recall with 95% intervals, a threshold for your
precision target and one for your recall target, Brier score, calibration error, and how
often Jev was confidently wrong. For contradictions, prefer the recall threshold: missing
one costs more than reading an extra passage. If no threshold reaches about 70% precision
for `new_info`, change the question wording in `thesis_radar/rubric.py` (and bump
`RUBRIC_VERSION`) before relying on the dashboard.

## Development

```bash
uv sync
uv run pytest            # offline; never needs an API key
uv run pytest -m live -s # one real Jev call; needs TYPESAFE_API_KEY
```
