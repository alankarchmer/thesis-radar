"""The `radar` command."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from . import __version__, ledger
from .absorb import absorb_text
from .app import App
from .apply import ActionError, apply_actions, parse_actions
from .calibrate import calibration_report, run_labeling, sample_for_labeling
from .config import ConfigError, Workspace, resolve_workspace
from .dashboard import build_payload, render_html, write_dashboard
from .edgar import EdgarError, fetch_all, make_http_get
from .ingest import ingest_inbox, tag_document
from .judge import JevJudge, Judge, JudgeFatal
from .lock import LockHeld, exclusive_lock
from .policy import PolicyError, classify_passage, policy_yaml, signals
from .rubric import SOURCE_TYPES
from .runner import RateLimiter, RunReport, run_judging, run_work
from .search import quote_markdown, search
from .store import SchemaTooNew, utc_now

EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2
MUTATING = frozenset({"fetch", "ingest", "judge", "view", "run", "tag", "label", "apply", "fact", "resolve", "init"})


class Refused(Exception):
    """The command cannot proceed; the message says why. Exit code 2."""


@dataclass
class Context:
    app: App
    judge_factory: Callable[[], Judge] | None
    out: TextIO
    err: TextIO
    stdin: TextIO

    @property
    def ws(self) -> Workspace:
        return self.app.ws

    def make_judge(self) -> Judge:
        if self.judge_factory is not None:
            return self.judge_factory()
        if not os.environ.get("TYPESAFE_API_KEY", "").strip():
            raise Refused("TYPESAFE_API_KEY is not set; ingest and judge need it")
        return JevJudge()

    def limiter(self) -> RateLimiter:
        return RateLimiter(self.app.config.requests_per_minute)

    def say(self, message: str) -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)


def _progress(ctx: Context, total: int) -> Callable[[RunReport], None] | None:
    if total < 50 or not getattr(ctx.err, "isatty", lambda: False)():
        return None
    step = max(1, total // 20)

    def show(report: RunReport) -> None:
        if report.judged % step == 0:
            print(f"\r  {report.judged}/{total} judged", end="", file=ctx.err, flush=True)

    return show


# Commands


def cmd_init(ctx: Context, args: argparse.Namespace) -> None:
    ws = ctx.ws
    written = []
    if not ws.config_path.exists():
        ws.config_path.write_text(
            "# thesis-radar settings. Every key is optional.\n"
            "# edgar_email: you@example.com   # needed for SEC EDGAR fetching (SEC asks for a contact)\n"
            "model: jev-1.13.0                  # pinned; never an alias\n"
            "concurrency: 16\n"
            "max_cost_per_run: 2.0              # dollars; judge stops before sending more unless --yes\n"
            "rejudge_window_days: 120           # after a thesis edit, only re-judge documents this recent\n",
            encoding="utf-8",
        )
        written.append("config.yaml")
    if not ws.policy_path.exists():
        ws.policy_path.write_text(policy_yaml(ctx.app.policy), encoding="utf-8")
        written.append("policy.yaml")
    ctx.say(f"init: workspace {ws.root}" + (f" (wrote {', '.join(written)})" if written else ""))
    if not ctx.app.theses:
        ctx.say("  next: write thesis/<TICKER>.yaml (see README), drop files into inbox/, then `radar run`")


def cmd_status(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    plan = app.plan()
    documents = app.store.documents()
    by_status: dict[str, int] = {}
    for d in documents:
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
    ctx.say(f"workspace: {app.ws.root}")
    ctx.say(f"theses: {', '.join(sorted(app.theses)) or 'none'}")
    for error in app.thesis_errors:
        ctx.say(f"  invalid: {error}")
    ctx.say("documents: " + (", ".join(f"{n} {s}" for s, n in sorted(by_status.items())) or "none"))
    ctx.say(
        f"passages: {plan.count('current')} current, {plan.count('stale')} stale, {plan.count('unjudged')} unjudged, "
        f"{len(plan.failed)} failed ({plan.blocked} permanently)"
    )
    ctx.say(f"pending: {len(plan.pending)} requests, about ${plan.estimated_cost:.2f}")
    month = app.today.replace(day=1).isoformat() + "T00:00:00Z"
    requests, tokens = app.store.usage_since(month)
    from .config import PRICE_PER_MILLION_INPUT_TOKENS

    ctx.say(f"this month: {requests} requests, {tokens:,} input tokens (${tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1e6:.2f})")


def cmd_fetch(ctx: Context, args: argparse.Namespace) -> None:
    config = ctx.app.config
    if not config.edgar_email:
        raise Refused("set edgar_email in config.yaml to fetch from SEC EDGAR (SEC requires a contact email)")
    theses = ctx.app.theses
    peers = sorted({peer for thesis in theses.values() for peer in thesis.peers} - set(theses))
    report = fetch_all(
        ctx.ws, ctx.app.store, sorted(theses), make_http_get(config.edgar_email), today=ctx.app.today,
        forms=config.edgar_forms, peers=peers, peer_forms=config.peer_forms,
    )
    ctx.say(f"fetch: {report.stored} stored, {report.duplicates} duplicates, {report.skipped} skipped")
    for message in report.messages:
        ctx.warn(f"  {message}")


def cmd_ingest(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    if not app.theses:
        raise Refused("no valid thesis files in thesis/; add one before ingesting")
    judge = ctx.make_judge()

    async def go():
        async with judge:
            return await ingest_inbox(
                app.ws, app.store, app.theses, judge, model=app.config.model, policy=app.policy,
                concurrency=min(app.config.concurrency, 8), limiter=ctx.limiter(),
            )

    try:
        report = asyncio.run(go())
    except JudgeFatal as exc:
        raise Refused(f"Jev refused the request: {exc}") from exc
    ctx.say(
        f"ingest: {report.sorted} sorted, {report.unsorted} unsorted, {report.duplicates} duplicates, "
        f"{report.failed} failed, {report.deferred} deferred, {report.repeats} repeated passages linked"
    )
    for message in report.messages:
        ctx.warn(f"  {message}")


def cmd_judge(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    cap = app.config.max_cost_per_run
    plan = app.plan(include_all=getattr(args, "all", False), retry_failed=getattr(args, "retry_failed", False))
    ctx.say(
        f"judge: {len(plan.pending)} passage requests to send, about {plan.estimated_tokens:,} tokens "
        f"(${plan.estimated_cost:.2f})"
    )
    if plan.stale_kept:
        ctx.say(
            f"  {plan.stale_kept} stale requests are for documents older than {app.config.rejudge_window_days} days "
            "and keep their last judgment; `radar judge --all` re-judges them"
        )
    if plan.blocked:
        ctx.say(f"  {plan.blocked} requests failed permanently; `radar judge --retry-failed` sends them again")
    if args.dry_run:
        followups = ledger.plan_followups(app.store, app.theses, app.config.model, plan=plan, policy=app.policy)
        ctx.say(f"ledger: {len(followups.pending)} follow-up requests (${followups.estimated_cost:.2f}) with current judgments")
        return
    if plan.estimated_cost > cap and not args.yes:
        raise Refused(
            f"estimated cost ${plan.estimated_cost:.2f} exceeds max_cost_per_run ${cap:.2f}; rerun with --yes to proceed"
        )
    spent = 0.0
    if plan.pending:
        judge = ctx.make_judge()

        async def go():
            async with judge:
                return await run_judging(
                    app.store, judge, plan.pending, concurrency=app.config.concurrency, limiter=ctx.limiter(),
                    progress=_progress(ctx, len(plan.pending)),
                )

        try:
            report = asyncio.run(go())
        except JudgeFatal as exc:
            raise Refused(f"Jev refused the request; stopped: {exc}") from exc
        from .config import PRICE_PER_MILLION_INPUT_TOKENS

        spent = report.input_tokens * PRICE_PER_MILLION_INPUT_TOKENS / 1_000_000
        ctx.say(f"judge: {report.judged} judged, {report.failed} failed, {report.input_tokens:,} input tokens")
        for error in report.errors[:10]:
            ctx.warn(f"  {error}")
    _judge_followups(ctx, args, budget=cap - spent)


def _judge_followups(ctx: Context, args: argparse.Namespace, *, budget: float) -> None:
    app = ctx.app
    followups = ledger.plan_followups(app.store, app.theses, app.config.model, plan=app.plan(), policy=app.policy)
    if not followups.pending:
        return
    if followups.estimated_cost > budget and not args.yes:
        ctx.warn(
            f"ledger: {len(followups.pending)} follow-up requests (${followups.estimated_cost:.2f}) skipped: "
            "over max_cost_per_run; rerun with --yes"
        )
        return
    judge = ctx.make_judge()

    async def go():
        async with judge:
            items = [ledger.followup_work(app.store, item) for item in followups.pending]
            return await run_work(judge, items, concurrency=app.config.concurrency, limiter=ctx.limiter())

    try:
        report = asyncio.run(go())
    except JudgeFatal as exc:
        raise Refused(f"Jev refused the request; stopped: {exc}") from exc
    ctx.say(f"ledger: {report.judged} follow-ups judged, {report.failed} failed")


def cmd_view(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    plan = app.plan()
    previous = app.store.last_view()
    generated = utc_now()
    payload = build_payload(app, plan=plan, mode="static", generated_at=generated, previous_view=previous)
    write_dashboard(app.ws.dashboard_path, render_html(payload))
    app.store.record_view(generated)
    ctx.say(f"view: wrote {app.ws.dashboard_path}")
    for company in payload["companies"]:
        counts = {key: sum(1 for p in company["passages"] if p["classified"][key]) for key in
                  ("in_contradictions", "in_whats_new", "in_maybe")}
        ctx.say(
            f"  {company['ticker']}: {counts['in_contradictions']} contradictions, {counts['in_whats_new']} new, "
            f"{counts['in_maybe']} maybe"
        )


def cmd_run(ctx: Context, args: argparse.Namespace) -> None:
    if ctx.app.config.edgar_email:
        try:
            cmd_fetch(ctx, args)
        except EdgarError as exc:
            ctx.warn(f"fetch: {exc}")
    else:
        ctx.say("fetch: skipped (no edgar_email in config.yaml)")
    for step in (cmd_ingest, cmd_judge):
        try:
            step(ctx, args)
        except Refused as exc:
            ctx.warn(f"{step.__name__[4:]}: {exc}")
    cmd_view(ctx, args)


def cmd_serve(ctx: Context, args: argparse.Namespace) -> None:
    from .serve import serve

    serve(ctx.app, port=args.port or ctx.app.config.serve_port, open_browser=not args.no_browser)


def cmd_apply(ctx: Context, args: argparse.Namespace) -> None:
    raw = args.actions
    if raw == "-":
        text = ctx.stdin.read()
    elif raw.startswith("@"):
        text = Path(raw[1:]).expanduser().read_text(encoding="utf-8")
    else:
        text = raw
    results = apply_actions(ctx.app, parse_actions(text))
    counts: dict[str, int] = {}
    for result in results:
        counts[result["op"]] = counts.get(result["op"], 0) + 1
    ctx.say("apply: " + (", ".join(f"{n} {op}" for op, n in sorted(counts.items())) or "nothing to do"))
    if "fact" in counts:
        ctx.say("  facts changed: the next `radar run` re-judges recent passages against them")


def cmd_tag(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    if args.ticker not in app.theses:
        raise Refused(f"unknown ticker {args.ticker}; known: {', '.join(sorted(app.theses)) or 'none'}")
    try:
        dest = tag_document(app.ws, app.store, args.document_id, ticker=args.ticker, source_type=args.source, doc_date=args.date)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    ctx.say(f"tag: document {args.document_id} -> {dest.relative_to(app.ws.root).as_posix()}")


def cmd_absorb(ctx: Context, args: argparse.Namespace) -> None:
    ctx.say(absorb_text(ctx.app.store, ctx.app.theses, args.passage_ids))


def cmd_fact(ctx: Context, args: argparse.Namespace) -> None:
    action: dict[str, Any] = {"op": "fact", "ticker": args.ticker, "pillar": args.pillar, "text": args.text}
    if args.source is not None:
        action["source"] = args.source
    if args.replace:
        action["replace"] = args.replace
    if args.as_of:
        action["as_of"] = args.as_of
    [result] = apply_actions(ctx.app, [action])
    verb = f"replaced {args.replace} with" if args.replace else "added"
    ctx.say(f"fact: {verb} {result.get('fact_id')} in thesis/{args.ticker}.yaml; `radar run` re-judges recent passages")


def cmd_resolve(ctx: Context, args: argparse.Namespace) -> None:
    outcome = {"yes": True, "no": False, "clear": None}[args.outcome]
    apply_actions(ctx.app, [{"op": "resolve", "ticker": args.ticker, "prediction_id": args.prediction, "outcome": outcome}])
    ctx.say(f"resolve: {args.ticker} {args.prediction} -> {args.outcome}")


def cmd_search(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    tickers = None
    if args.ticker:
        thesis = app.theses.get(args.ticker)
        tickers = [args.ticker, *(thesis.peers if thesis else ())]
    hits = search(app.store, args.query, tickers=tickers, limit=args.limit)
    if not hits:
        ctx.say("search: no matches")
        return
    for hit in hits:
        ctx.say(
            f"[{hit['passage_id']}] {hit['ticker']} · {hit['doc_date'] or 'undated'} · {hit['source_type']} · "
            f"{hit['title']} · p.{hit['page']}"
        )
        ctx.say(f"    {hit['snippet']}")


def cmd_quote(ctx: Context, args: argparse.Namespace) -> None:
    ctx.say(quote_markdown(ctx.app.ws, ctx.app.store, args.passage_ids))


def cmd_label(ctx: Context, args: argparse.Namespace) -> None:
    app = ctx.app
    ticker = args.ticker or (next(iter(app.theses)) if len(app.theses) == 1 else None)
    if ticker is None or ticker not in app.theses:
        raise Refused("choose a company with --ticker")
    plan = app.plan()
    labeled = app.store.labeled_passage_ids(ticker)
    thesis = app.theses[ticker]
    candidates = []
    flagged = set()
    for row in app.store.passages_for_tickers([ticker, *thesis.peers]):
        pid = row["passage_id"]
        if row["repeat_of"] is not None or pid in labeled or plan.status(ticker, pid) != "current":
            continue
        candidates.append(pid)
        c = classify_passage(
            {"p": signals(plan.answers(ticker, pid)), "date": row["doc_date"], "ingested_at": row["ingested_at"]},
            app.policy, app.today,
        )
        if c.flagged:
            flagged.add(pid)
    sample = sample_for_labeling(candidates, flagged, n=args.n, seed=args.seed)
    order = {pid: index for index, pid in enumerate(sample.ids)}
    rows = sorted(app.store.get_passages(sample.ids), key=lambda row: order[row["passage_id"]])
    keys = {pid: plan.keys(ticker, pid) for pid in sample.ids}
    done = run_labeling(
        app.store, thesis, rows, ask=lambda prompt: _ask(ctx, prompt), show=ctx.say, weights=sample.weights, keys=keys
    )
    ctx.say(f"label: {done} passages labeled")


def _ask(ctx: Context, prompt: str) -> str:
    print(prompt, end="", file=ctx.out, flush=True)
    line = ctx.stdin.readline()
    return line if line else "q"


def cmd_calibrate(ctx: Context, args: argparse.Namespace) -> None:
    ctx.say(
        calibration_report(
            ctx.app.store, ctx.app.policy, target_precision=args.precision, target_recall=args.recall, ticker=args.ticker
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description="Judge new research against your thesis with Jev.")
    parser.add_argument("--workspace", help="workspace directory (default: $RADAR_HOME or the current directory)")
    parser.add_argument("--version", action="version", version=f"radar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the workspace layout, config.yaml, and policy.yaml").set_defaults(handler=cmd_init)
    sub.add_parser("status", help="summarize documents, judgments, and spending").set_defaults(handler=cmd_status)
    sub.add_parser("fetch", help="fetch new SEC filings for thesis tickers and peers").set_defaults(handler=cmd_fetch)
    sub.add_parser("ingest", help="process files in inbox/").set_defaults(handler=cmd_ingest)

    judge = sub.add_parser("judge", help="judge pending passages with Jev, then guidance follow-ups")
    judge.add_argument("--dry-run", action="store_true", help="only print the count and estimated cost")
    judge.add_argument("--yes", action="store_true", help="proceed even above max_cost_per_run")
    judge.add_argument("--all", action="store_true", help="also re-judge stale passages outside the re-judge window")
    judge.add_argument("--retry-failed", action="store_true", help="also retry requests that failed permanently")
    judge.set_defaults(handler=cmd_judge)

    sub.add_parser("view", help="write dashboard.html").set_defaults(handler=cmd_view)

    run = sub.add_parser("run", help="fetch, ingest, judge, and view")
    run.add_argument("--yes", action="store_true", help="proceed even above max_cost_per_run")
    run.set_defaults(handler=cmd_run, dry_run=False, all=False, retry_failed=False)

    serve = sub.add_parser("serve", help="serve the dashboard on localhost with write-back")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--no-browser", action="store_true")
    serve.set_defaults(handler=cmd_serve)

    apply = sub.add_parser("apply", help="apply actions queued in the static dashboard")
    apply.add_argument("actions", help="a JSON array, @file, or - for stdin")
    apply.set_defaults(handler=cmd_apply)

    tag = sub.add_parser("tag", help="resolve an unsorted document")
    tag.add_argument("document_id", type=int)
    tag.add_argument("--ticker", required=True)
    tag.add_argument("--source", required=True, choices=sorted(SOURCE_TYPES))
    tag.add_argument("--date", required=True, help="YYYY-MM-DD")
    tag.set_defaults(handler=cmd_tag)

    absorb = sub.add_parser("absorb", help="print passages beside current known facts")
    absorb.add_argument("passage_ids", type=int, nargs="+")
    absorb.set_defaults(handler=cmd_absorb)

    fact = sub.add_parser("fact", help="add or replace a known fact in a thesis file")
    fact.add_argument("ticker")
    fact.add_argument("pillar")
    fact.add_argument("text")
    fact.add_argument("--source", type=int, help="passage id the fact comes from")
    fact.add_argument("--replace", help="fact id to replace, such as inventory.0")
    fact.add_argument("--as-of", help="YYYY-MM-DD (default: the source passage's date)")
    fact.set_defaults(handler=cmd_fact)

    resolve = sub.add_parser("resolve", help="record a prediction's outcome")
    resolve.add_argument("ticker")
    resolve.add_argument("prediction")
    resolve.add_argument("outcome", choices=["yes", "no", "clear"])
    resolve.set_defaults(handler=cmd_resolve)

    search_cmd = sub.add_parser("search", help="full-text search over passages")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--ticker")
    search_cmd.add_argument("--limit", type=int, default=20)
    search_cmd.set_defaults(handler=cmd_search)

    quote = sub.add_parser("quote", help="print passages as Markdown quotes with citations")
    quote.add_argument("passage_ids", type=int, nargs="+")
    quote.set_defaults(handler=cmd_quote)

    label = sub.add_parser("label", help="label a sample of judged passages")
    label.add_argument("--ticker")
    label.add_argument("--n", type=int, default=20)
    label.add_argument("--seed", type=int, default=0)
    label.set_defaults(handler=cmd_label)

    calibrate = sub.add_parser("calibrate", help="report Jev's accuracy against your labels")
    calibrate.add_argument("--ticker")
    calibrate.add_argument("--precision", type=float, default=0.8, help="precision target for threshold selection")
    calibrate.add_argument("--recall", type=float, default=0.9, help="recall target for threshold selection")
    calibrate.set_defaults(handler=cmd_calibrate)

    sub.add_parser("mcp", help="run a read-only MCP server over stdio").set_defaults(handler=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    judge_factory: Callable[[], Judge] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    stdin: TextIO | None = None,
    today: date | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    stdin = stdin or sys.stdin
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE

    ws = resolve_workspace(args.workspace)
    if args.command == "mcp":
        from .mcp_server import run_stdio

        run_stdio(ws, stdin, out)
        return EXIT_OK

    try:
        app = App.open(ws, today=today)
    except (ConfigError, PolicyError, SchemaTooNew) as exc:
        print(f"radar: {exc}", file=err)
        return EXIT_REFUSED
    for error in app.thesis_errors:
        print(f"radar: skipping thesis: {error}", file=err)

    ctx = Context(app, judge_factory, out, err, stdin)
    try:
        if args.command in MUTATING:
            with exclusive_lock(ws.lock_path):
                args.handler(ctx, args)
        else:
            args.handler(ctx, args)
    except (Refused, LockHeld, EdgarError, ActionError, PolicyError) as exc:
        print(f"radar: {exc}", file=err)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("radar: interrupted; finished judgments are saved", file=err)
        return EXIT_REFUSED
    finally:
        app.close()
    return EXIT_OK
