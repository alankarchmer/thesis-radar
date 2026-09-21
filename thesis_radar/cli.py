"""The `radar` command."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TextIO

from . import __version__
from .absorb import absorb_text
from .calibrate import calibration_report, run_labeling, sample_for_labeling
from .config import Config, ConfigError, Workspace, load_config, resolve_workspace
from .dashboard import build_payload, render_html, write_dashboard
from .edgar import EdgarError, fetch_all, make_http_get
from .ingest import ingest_inbox, tag_document
from .judge import JevJudge, Judge
from .lock import LockHeld, exclusive_lock
from .policy import Policy, PolicyError, classify, load_policy
from .rubric import SOURCE_TYPES
from .runner import RateLimiter, plan_judging, run_judging
from .store import Store, utc_now
from .thesis import Thesis, load_theses

EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2
MUTATING = frozenset({"fetch", "ingest", "judge", "view", "run", "tag", "label"})


class Refused(Exception):
    """The command cannot proceed; the message says why. Exit code 2."""


@dataclass
class Context:
    ws: Workspace
    config: Config
    policy: Policy
    theses: dict[str, Thesis]
    store: Store
    judge_factory: Callable[[], Judge] | None
    out: TextIO
    err: TextIO
    today: date

    def make_judge(self) -> Judge:
        if self.judge_factory is not None:
            return self.judge_factory()
        if not os.environ.get("TYPESAFE_API_KEY", "").strip():
            raise Refused("TYPESAFE_API_KEY is not set; ingest and judge need it")
        return JevJudge()

    def say(self, message: str) -> None:
        print(message, file=self.out)

    def warn(self, message: str) -> None:
        print(message, file=self.err)


def cmd_fetch(ctx: Context, args: argparse.Namespace) -> None:
    if not ctx.config.edgar_email:
        raise Refused("set edgar_email in config.yaml to fetch from SEC EDGAR (SEC requires a contact email)")
    report = fetch_all(ctx.ws, ctx.store, sorted(ctx.theses), make_http_get(ctx.config.edgar_email), today=ctx.today)
    ctx.say(f"fetch: {report.stored} stored, {report.duplicates} duplicates, {report.skipped} skipped")
    for message in report.messages:
        ctx.warn(f"  {message}")


def cmd_ingest(ctx: Context, args: argparse.Namespace) -> None:
    if not ctx.theses:
        raise Refused("no valid thesis files in thesis/; add one before ingesting")
    judge = ctx.make_judge()

    async def go():
        async with judge:
            return await ingest_inbox(ctx.ws, ctx.store, ctx.theses, judge, model=ctx.config.model, policy=ctx.policy)

    report = asyncio.run(go())
    ctx.say(
        f"ingest: {report.sorted} sorted, {report.unsorted} unsorted, {report.duplicates} duplicates, "
        f"{report.failed} failed, {report.deferred} deferred"
    )
    for message in report.messages:
        ctx.warn(f"  {message}")


def cmd_judge(ctx: Context, args: argparse.Namespace) -> None:
    plan = plan_judging(ctx.store, ctx.theses, ctx.config.model)
    ctx.say(
        f"judge: {len(plan.pending)} passages to judge, about {plan.estimated_tokens:,} tokens "
        f"(${plan.estimated_cost:.2f}); {plan.repeats} repeated passages skipped"
    )
    if args.dry_run or not plan.pending:
        return
    if plan.estimated_cost > ctx.config.max_cost_per_run and not args.yes:
        raise Refused(
            f"estimated cost ${plan.estimated_cost:.2f} exceeds max_cost_per_run "
            f"${ctx.config.max_cost_per_run:.2f}; rerun with --yes to proceed"
        )
    judge = ctx.make_judge()
    limiter = RateLimiter(ctx.config.requests_per_minute)

    async def go():
        async with judge:
            return await run_judging(ctx.store, judge, plan.pending, concurrency=ctx.config.concurrency, limiter=limiter)

    report = asyncio.run(go())
    ctx.say(f"judge: {report.judged} judged, {report.failed} failed, {report.input_tokens:,} input tokens")
    for error in report.errors[:10]:
        ctx.warn(f"  {error}")


def cmd_view(ctx: Context, args: argparse.Namespace) -> None:
    plan = plan_judging(ctx.store, ctx.theses, ctx.config.model)
    previous = ctx.store.last_view()
    generated = utc_now()
    payload = build_payload(ctx.ws, ctx.store, ctx.theses, plan, ctx.policy, generated_at=generated, previous_view=previous)
    write_dashboard(ctx.ws.dashboard_path, render_html(payload))
    ctx.store.record_view(generated)
    ctx.say(f"view: wrote {ctx.ws.dashboard_path}")


def cmd_run(ctx: Context, args: argparse.Namespace) -> None:
    if ctx.config.edgar_email:
        try:
            cmd_fetch(ctx, args)
        except EdgarError as exc:
            ctx.warn(f"fetch: {exc}")
    else:
        ctx.say("fetch: skipped (no edgar_email in config.yaml)")
    cmd_ingest(ctx, args)
    try:
        cmd_judge(ctx, args)
    except Refused as exc:
        ctx.warn(f"judge: {exc}")
    cmd_view(ctx, args)


def cmd_tag(ctx: Context, args: argparse.Namespace) -> None:
    if args.ticker not in ctx.theses:
        raise Refused(f"unknown ticker {args.ticker}; known: {', '.join(sorted(ctx.theses)) or 'none'}")
    try:
        dest = tag_document(ctx.ws, ctx.store, args.document_id, ticker=args.ticker, source_type=args.source, doc_date=args.date)
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    ctx.say(f"tag: document {args.document_id} -> {dest.relative_to(ctx.ws.root).as_posix()}")


def cmd_absorb(ctx: Context, args: argparse.Namespace) -> None:
    ctx.say(absorb_text(ctx.store, ctx.theses, args.passage_ids))


def cmd_label(ctx: Context, args: argparse.Namespace) -> None:
    ticker = args.ticker or (next(iter(ctx.theses)) if len(ctx.theses) == 1 else None)
    if ticker is None or ticker not in ctx.theses:
        raise Refused("choose a company with --ticker")
    plan = plan_judging(ctx.store, ctx.theses, ctx.config.model)
    labeled = ctx.store.labeled_passage_ids()
    candidates = [
        row["passage_id"] for row in ctx.store.passages_for_ticker(ticker)
        if row["passage_id"] in plan.judged and row["passage_id"] not in labeled
    ]
    flagged = {pid for pid in candidates if _flagged(plan.judged[pid], ctx.policy)}
    ids = sample_for_labeling(candidates, flagged, n=args.n, seed=args.seed)
    order = {pid: index for index, pid in enumerate(ids)}
    rows = sorted(ctx.store.get_passages(ids), key=lambda row: order[row["passage_id"]])
    done = run_labeling(ctx.store, ctx.theses[ticker], rows, ask=input, show=ctx.say)
    ctx.say(f"label: {done} passages labeled")


def _flagged(answers: dict[str, Any], policy: Policy) -> bool:
    c = classify(answers, policy)
    return c.in_whats_new or c.in_maybe or c.in_contradictions


def cmd_calibrate(ctx: Context, args: argparse.Namespace) -> None:
    ctx.say(
        calibration_report(
            ctx.store, ctx.policy, target_precision=args.precision, target_recall=args.recall, ticker=args.ticker
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description="Judge new research against your thesis with Jev.")
    parser.add_argument("--workspace", help="workspace directory (default: $RADAR_HOME or the current directory)")
    parser.add_argument("--version", action="version", version=f"radar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("fetch", help="fetch new SEC filings").set_defaults(handler=cmd_fetch)
    sub.add_parser("ingest", help="process files in inbox/").set_defaults(handler=cmd_ingest)

    judge = sub.add_parser("judge", help="judge pending passages with Jev")
    judge.add_argument("--dry-run", action="store_true", help="only print the count and estimated cost")
    judge.add_argument("--yes", action="store_true", help="proceed even above max_cost_per_run")
    judge.set_defaults(handler=cmd_judge)

    sub.add_parser("view", help="write dashboard.html").set_defaults(handler=cmd_view)

    run = sub.add_parser("run", help="fetch, ingest, judge, and view")
    run.add_argument("--yes", action="store_true", help="proceed even above max_cost_per_run")
    run.set_defaults(handler=cmd_run, dry_run=False)

    tag = sub.add_parser("tag", help="resolve an unsorted document")
    tag.add_argument("document_id", type=int)
    tag.add_argument("--ticker", required=True)
    tag.add_argument("--source", required=True, choices=sorted(SOURCE_TYPES))
    tag.add_argument("--date", required=True, help="YYYY-MM-DD")
    tag.set_defaults(handler=cmd_tag)

    absorb = sub.add_parser("absorb", help="print passages to fold into known_facts")
    absorb.add_argument("passage_ids", type=int, nargs="+")
    absorb.set_defaults(handler=cmd_absorb)

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
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    judge_factory: Callable[[], Judge] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    today: date | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE

    ws = resolve_workspace(args.workspace)
    ws.ensure_layout()
    try:
        config = load_config(ws.config_path)
        policy = load_policy(ws.policy_path)
    except (ConfigError, PolicyError) as exc:
        print(f"radar: {exc}", file=err)
        return EXIT_REFUSED
    theses, errors = load_theses(ws.thesis_dir)
    for error in errors:
        print(f"radar: skipping thesis: {error}", file=err)

    store = Store(ws.db_path)
    ctx = Context(ws, config, policy, theses, store, judge_factory, out, err, today or date.today())
    try:
        if args.command in MUTATING:
            with exclusive_lock(ws.lock_path):
                args.handler(ctx, args)
        else:
            args.handler(ctx, args)
    except (Refused, LockHeld, EdgarError) as exc:
        print(f"radar: {exc}", file=err)
        return EXIT_REFUSED
    finally:
        store.close()
    return EXIT_OK
