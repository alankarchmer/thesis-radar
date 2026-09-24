# thesis-radar

`radar` reads the research you collect on a few companies (filings, earnings-call
transcripts, sell-side reports, expert calls, AI research, your own notes) and uses
TypeSafe's Jev model to judge every passage against your investment thesis. It shows
what is new to you, what matters, and what contradicts your assumptions, in a dashboard
you triage from the keyboard. It never writes text of its own: every passage it shows
is quoted verbatim with a link to its source, and Jev only chooses, scores, or answers
yes/no.

## Quick start on a Mac: Apple (AAPL)

A complete first run with a real company. It fetches Apple's last year of SEC filings from EDGAR,
has Jev judge them against a sample thesis, and opens the dashboard. Everything is a Terminal
command: copy each block, paste it into Terminal, and press Return. You only type two things when
asked, your email and your TypeSafe API key. The blocks carry no `#` comments on purpose: zsh on
macOS does not treat `#` as a comment at the prompt.

**1. Install uv**, which installs Python and `radar`. Skip this if `uv --version` already works.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

**2. Get the code and install `radar`.** If macOS asks to install the command line developer tools
(they provide `git`), click Install, wait for it to finish, and paste the block again.

```bash
git clone https://github.com/alankarchmer/thesis-radar.git ~/thesis-radar
cd ~/thesis-radar
uv tool install --editable .
uv tool update-shell
export PATH="$HOME/.local/bin:$PATH"
radar --version
```

**3. Create the workspace.** It lives outside the repository, since filings and judgments never
belong in git.

```bash
export RADAR_HOME=~/radar-aapl
radar init
```

Paste the next two lines one at a time, since each one waits for you to type something. The first
asks for your email, which SEC EDGAR requires as a contact on every request. The second asks for
your TypeSafe API key, which stays hidden as you type or paste it.

```bash
printf 'Email address for SEC EDGAR: '; read -r EDGAR_EMAIL; echo "edgar_email: $EDGAR_EMAIL" >> "$RADAR_HOME/config.yaml"
```

```bash
printf 'TypeSafe API key (hidden): '; read -rs TYPESAFE_API_KEY; echo; export TYPESAFE_API_KEY
```

Optional: keep the workspace and key for future Terminal windows. This saves the key in plain text
in `~/.zshrc`.

```bash
echo 'export RADAR_HOME=~/radar-aapl' >> ~/.zshrc
echo "export TYPESAFE_API_KEY='$TYPESAFE_API_KEY'" >> ~/.zshrc
```

**4. Write the thesis.** This is a sample thesis to edit, not investment advice. The assumptions,
open questions, and predictions are opinions to replace with your own. The known facts are
deliberately general: add the specifics you already know, since novelty is judged against them.

```bash
cat > "$RADAR_HOME/thesis/AAPL.yaml" <<'EOF'
ticker: AAPL
company: Apple Inc.
aliases: [Apple, "Apple Inc."]
# peers: [GOOGL, QCOM]   # optional: also read these companies' earnings releases against this thesis (costs more)

pillars:
  iphone: iPhone demand, the upgrade cycle, pricing, and mix.
  services: App Store, advertising, iCloud, subscriptions, and licensing such as Google's search payments.
  china: Demand in Greater China and competition from local brands.
  margins: Gross margin for products and services, tariffs, and component costs.
  ai: Apple Intelligence, Siri, and partnerships with outside AI models.
  regulation: App Store rules, antitrust cases, and the EU's Digital Markets Act.
  capital_return: Share buybacks and dividends.

assumptions:
  iphone_grows: {pillar: iphone, statement: "iPhone revenue grows year over year through fiscal 2027."}
  services_double_digit: {pillar: services, statement: "Services revenue keeps growing at least 10% a year."}
  gross_margin_holds: {pillar: margins, statement: "Total gross margin stays at or above 45% despite tariffs and component costs."}
  china_stabilizes: {pillar: china, statement: "Greater China revenue stops declining year over year."}
  ai_on_schedule: {pillar: ai, statement: "Apple ships its announced AI features, including the new Siri, on the schedule it has given."}
  search_payments_hold: {pillar: regulation, statement: "Court remedies leave Google's default-search payments to Apple largely intact."}
  buybacks_continue: {pillar: capital_return, statement: "Apple keeps repurchasing at least $90 billion of stock a year."}

open_questions:
  tariff_cost: How much are tariffs costing Apple each quarter, and is it raising prices to offset them?
  ai_partners: Which outside AI models will Apple use, and on what terms?
  china_share: Is Apple losing share in China to Huawei and other local brands?
  memory_costs: Are rising memory prices squeezing product gross margin?

predictions:
  dec_services_record: {statement: "Services revenue sets a record in the December 2026 quarter.", by: 2027-02-15, p: 0.8, pillar: services}
  dec_china_growth: {statement: "Greater China revenue grows year over year in the December 2026 quarter.", by: 2027-02-15, p: 0.5, pillar: china}

known_facts:
  iphone:
    - "iPhone is Apple's largest product category, about half of total revenue."
    - "Apple reports revenue for iPhone, Mac, iPad, Wearables/Home and Accessories, and Services."
  services:
    - "Services is Apple's second-largest revenue category, with a much higher gross margin than products."
    - "Google pays Apple to be the default search engine in Safari."
  china:
    - "Greater China is one of Apple's five reportable segments, with the Americas, Europe, Japan, and Rest of Asia Pacific."
  margins:
    - "Apple reports gross margin separately for Products and Services."
  regulation:
    - "In the EU, the Digital Markets Act requires Apple to allow alternative app marketplaces and payment options."

metrics:
  revenue: {label: "Revenue", unit: "$B", higher_is: good}
  iphone_revenue: {label: "iPhone revenue", unit: "$B", pillar: iphone, higher_is: good}
  services_revenue: {label: "Services revenue", unit: "$B", pillar: services, higher_is: good}
  greater_china_revenue: {label: "Greater China revenue", unit: "$B", pillar: china, higher_is: good}
  gross_margin: {label: "Gross margin", unit: "%", pillar: margins, higher_is: good, definition: "Total company gross margin, products and services combined"}
  services_gross_margin: {label: "Services gross margin", unit: "%", pillar: margins, higher_is: good}
  eps: {label: "Diluted EPS", unit: "$", higher_is: good}
EOF
radar status
```

`radar status` should list `theses: AAPL`. If the thesis has a mistake, it names the key at fault.

**5. Fetch Apple's filings from SEC EDGAR.** The first fetch takes the last 365 days of 10-K, 10-Q,
and 8-K filings, including the earnings releases filed as 8-K exhibit 99. Later fetches take only
newer filings. It needs no API key.

```bash
radar fetch
radar status
```

**6. Judge with Jev.** Start with a dry run: it prints how many requests would be sent and the
estimated cost, without calling Jev.

```bash
radar judge --dry-run
radar judge
```

- **Over the cost cap:** if the estimate exceeds `max_cost_per_run` ($2.00 by default), `radar judge`
  stops without sending anything. Run `radar judge --yes` to go ahead once, or raise the cap for
  every run (to $10 here): `echo "max_cost_per_run: 10.0" >> "$RADAR_HOME/config.yaml"`.
- **Metrics show 0 in the dry run:** that's expected. Numbers are asked about only for passages
  already judged, so `radar judge` handles them after the passages. The guidance ledger follow-ups
  work the same way.
- **Skipped as over the cap:** when the metric numbers or ledger follow-ups don't fit in what's left
  of the budget, `radar judge` says they were skipped. Run `radar judge --yes` to send them.

**7. Open the dashboard.** `radar serve` opens `http://127.0.0.1:8765` in your browser and saves
your triage as you go. Press `Ctrl-C` in Terminal to stop it.

```bash
radar metrics --ticker AAPL
radar serve
```

To get a static file instead of the server:

```bash
radar view
open "$RADAR_HOME/dashboard.html"
```

**Every day after that.** To add your own research (sell-side PDFs, transcripts, notes), open the
inbox in Finder and drag the files in:

```bash
open ~/radar-aapl/inbox
```

Then `radar run` fetches new filings, ingests the inbox, judges, and rewrites the dashboard, and
`radar serve` opens it. In a new Terminal window without the optional `~/.zshrc` lines above,
paste the key line from step 3 first.

```bash
export RADAR_HOME=~/radar-aapl
radar run
radar serve
```

## Install

From a clone of this repository:

```bash
uv tool install --editable .
```

`ingest` and `judge` need a TypeSafe API key; everything else works offline. Paste this line on
its own; it asks for the key and keeps it hidden:

```bash
printf 'TypeSafe API key (hidden): '; read -rs TYPESAFE_API_KEY; echo; export TYPESAFE_API_KEY
```

## Set up a workspace

Keep your research outside this repository (it is licensed material and never belongs in
git). These commands create a workspace in `~/research` with `inbox/`, `archive/`, `thesis/`,
`config.yaml`, and `policy.yaml`. Any other folder works: pass `--workspace DIR` or set
`RADAR_HOME`.

```bash
export RADAR_HOME=~/research
radar init
```

To fetch from SEC EDGAR, `radar` needs a contact email, which SEC asks every client for. Paste this
line on its own; it asks for your address:

```bash
printf 'Email address for SEC EDGAR: '; read -r EDGAR_EMAIL; echo "edgar_email: $EDGAR_EMAIL" >> "$RADAR_HOME/config.yaml"
```

Write one thesis file per company, named after its ticker. This writes `thesis/PII.yaml` for
Polaris and checks that it loads:

```bash
cat > "$RADAR_HOME/thesis/PII.yaml" <<'EOF'
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
    - {text: "Promotions up 200 bps year over year.", as_of: 2026-08-05}
EOF
radar status
```

`radar status` should list the ticker under `theses:`. A fact can also carry `source:` with the id of
the passage it came from. `radar fact --source` and the dashboard's Absorb action fill that in for
you.

Limits: 1-12 pillars, 10 assumptions, 5 open questions, 10 predictions, 5 peers, 12 metrics, 20 facts
per pillar. Quote any key YAML might read as a boolean (`yes`, `no`, `on`, `off`).

`config.yaml` takes these keys, all optional. `radar init` writes the defaults:

```yaml
edgar_email: you@example.com   # enables SEC EDGAR fetching (SEC asks for a contact)
model: jev-1.13.0              # pinned; aliases such as jev-latest are refused
concurrency: 16                # Jev requests in flight
max_cost_per_run: 2.0          # dollars; judge stops before sending more unless --yes
rejudge_window_days: 120       # after a thesis edit, re-judge only documents this recent
edgar_forms: [10-K, 10-K/A, 10-Q, 10-Q/A, 8-K, 20-F, 40-F, 6-K]
peer_forms: [8-K, 6-K]         # peers: exhibit 99 (earnings releases) only
# 8-K/6-K exhibits are picked by their declared type (EX-99*), not their file name.
serve_port: 8765
```

To change a setting, append it. When a key appears twice, the last one wins:

```bash
echo "max_cost_per_run: 5.0" >> "$RADAR_HOME/config.yaml"
```

`policy.yaml` holds the dashboard thresholds (`radar init` writes the defaults; the
dashboard's threshold panel can write it for you). Changing policy never calls Jev.

## Daily use

`radar run` fetches filings, ingests `inbox/`, judges, and writes `dashboard.html`. `radar serve`
opens the dashboard on localhost with write-back, which is the recommended way to use it:

```bash
radar run
radar serve
```

Drop downloaded files (PDF, HTML, DOCX, TXT, MD) into `inbox/`; `open "$RADAR_HOME/inbox"` opens it
in Finder. Files Jev cannot place
confidently appear under **Unsorted** with a `radar tag` command to copy.

The dashboard opens on an **Overview**: per company, a verdict on each assumption (holding,
contradicted, mixed, turning, or quiet: the last six months of evidence against the six
before, with a 12-month chart), what to read next, a line per tracked metric, and signals
in plain sentences (where management and outside sources disagree, guidance hit rate,
predictions due). Each company has five sections:

- **Read**: contradictions grouped by assumption, then what's new by pillar, maybe-new,
  and starred.
- **Thesis**: the assumption scorecard with the evidence for and against each, open
  questions and the passages bearing on them, predictions, and known facts.
- **Metrics**: each tracked number by period (see *Track the numbers*).
- **Signals**: tone by pillar and month, management versus outside sources, and the
  guidance ledger.
- **Filings**: filing-to-filing redlines of changed language.

A chip in the header counts what needs attention (stale, unjudged, or failed passages).
Blue means supports your thesis, red means contradicts it.

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
| `g` then `r` `t` `m` `s` `f` | Read, Thesis, Metrics, Signals, Filings (`g o` Overview, `g 1`-`9` company) |
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
| `radar metrics [--ticker T] [--metric M]` | The tracked metrics per period: reported, guidance, estimates |
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
- Risk-factor language about what could happen, and standard accounting-policy text, counts as
  boilerplate: it is never evidence for or against an assumption, and never a contradiction.

## Track the numbers

Add a `metrics:` section to a thesis to follow the figures it depends on:

```yaml
metrics:
  gross_margin: {label: "Gross margin", unit: "%", pillar: margins, higher_is: good}
  revenue: {label: "Revenue", unit: "$M", higher_is: good}
  dealer_inventory: {label: "Dealer inventory", unit: "units", pillar: dealer_inventory, higher_is: bad}
```

On the next `radar run`, code finds every number and reporting period in the company's documents,
and Jev picks which metric each number measures, whether it is a reported result, company guidance,
or an outside estimate, and which period it covers. `radar metrics` (and the dashboard's Metrics
view) then shows, per period, what was reported, every guidance revision, the estimates, whether the
result beat or missed guidance and estimates, and whether guidance was raised or cut — every number
with the sentence it came from and a link to its page. Units: `%`, `bps`, `pp`, `$`, `$K`, `$M`, `$B`,
`days`, `x`, or any word for a count. Metrics never re-judge passages; changing one only re-asks
about the numbers.

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

`radar mcp` exposes the corpus read-only (companies, theses, tracked metrics, What's new,
contradictions, search, passages, documents), with a citation on every passage:

```bash
claude mcp add thesis-radar -- radar --workspace ~/research mcp
```

## Development

The tests run offline and never need an API key:

```bash
uv sync
uv run pytest
uv run ruff check .
```

The dashboard tests drive headless Chromium and need Playwright's browsers. The live test makes one
real Jev call and needs `TYPESAFE_API_KEY`:

```bash
uv run pytest -m ui
uv run pytest -m live -s
```

Design: `docs/superpowers/specs/2026-09-21-thesis-radar-design.md` (v1) and
`docs/superpowers/specs/2026-09-24-thesis-radar-v1.1.md` (v1.1 amendments and the
dashboard, action, and serve contracts).
