"""The command line: ``once``, ``run``, ``sweep``, ``digest``, ``archive``, ``backfill``,
``selftest``, ``status``.

    python3 -m transcriber run          the service
    python3 -m transcriber once         one poll and one drain, then exit
    python3 -m transcriber status       what a person actually wants to know
    python3 -m transcriber selftest     prove the deploy is sane, offline

``selftest`` is the important one. It proves parsing, the ledger's state machine, quote
verification, the markdown output contract, the truncation detector, the split guard and
mechanical secret redaction **with no credential and no network**, the same way
``graph_pull.py --selftest`` does downstream. It exits non-zero and names what failed. Run it
on the box, after deploying, before believing anything.

``backfill`` walks history newest first in its own lane: it only touches recordings older
than a cutoff, so today's calls are always the live loop's and never queue behind April's.
It is resumable because the ledger is the progress: re-running it picks up exactly what is
not finished.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile
import time
from typing import Any, Callable, Sequence

from . import archive as archive_module
from . import audio as audio_module
from . import naming, outputs, plausibility, sweep as sweep_module
from .config import Config, ConfigError
from .ledger import DELTA_CURSOR, Ledger, SWEEP_CURSOR
from .logging_setup import configure as configure_logging
from .models import (
    AudioInfo,
    DriveItem,
    Hints,
    Row,
    Segment,
    State,
    Transcript,
    strip_owner_paths,
    utc_now_iso,
)
from .pipeline import Pipeline, PipelineFatal, build_graph
from .worker import (
    DigestUnavailable,
    LAST_CYCLE_ERROR,
    LAST_CYCLE_OK,
    LAST_POLL_OK,
    Worker,
    claimable_now,
    run_digest,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

#: The backfill lane never touches anything more recent than this. Two days is comfortably
#: past the point where the live loop has either finished a recording or quarantined it.
BACKFILL_MIN_AGE_DAYS = 2
BACKFILL_CURSOR = "delta:backfill"
BACKFILL_POSITION = "backfill:position"
BACKFILL_FINISHED = "backfill:finished_at"


# --------------------------------------------------------------------------- entry point


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    except PipelineFatal as exc:
        print(f"the service stopped: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcriber", description=(__doc__ or "").strip().split("\n")[0]
    )
    parser.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")
    parser.add_argument("--json-logs", action="store_true", help="one JSON object per log line")
    parser.add_argument("--ledger", default=None, help="path to the SQLite ledger (overrides LEDGER_PATH)")
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="one poll and one drain, then exit")
    once.add_argument("--limit", type=int, default=None, help="at most this many recordings")
    once.set_defaults(handler=cmd_once)

    run = sub.add_parser("run", help="the service loop")
    run.set_defaults(handler=cmd_run)

    sweep = sub.add_parser("sweep", help="re-enumerate the folder and re-queue anything unfinished")
    sweep.add_argument("--dry-run", action="store_true")
    sweep.set_defaults(handler=cmd_sweep)

    digest = sub.add_parser("digest", help="send the morning digest now")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--day", default=None, help="the day to report on (YYYY-MM-DD)")
    digest.set_defaults(handler=cmd_digest)

    archive = sub.add_parser("archive", help="move aged, finished, output-confirmed recordings")
    archive.add_argument("--dry-run", action="store_true")
    archive.add_argument("--limit", type=int, default=None)
    archive.add_argument("--age-days", type=int, default=None)
    archive.set_defaults(handler=cmd_archive)

    backfill = sub.add_parser("backfill", help="work through history, newest first, in its own lane")
    backfill.add_argument("--limit", type=int, default=None, help="at most this many recordings")
    backfill.add_argument("--concurrency", type=int, default=1,
                          help="recordings at once (kept low on purpose)")
    backfill.add_argument("--min-age-days", type=int, default=BACKFILL_MIN_AGE_DAYS,
                          help="never touch anything more recent than this")
    backfill.add_argument("--yield-seconds", type=float, default=30.0,
                          help="pause this long whenever the live lane has work waiting")
    backfill.add_argument("--enumerate-only", action="store_true",
                          help="record what is there and stop, processing nothing")
    backfill.set_defaults(handler=cmd_backfill)

    requeue = sub.add_parser(
        "requeue", help="put one recording back in the queue after fixing what was wrong"
    )
    requeue.add_argument("item_id", help="the recording's id, from the morning email or `status`")
    requeue.add_argument("--reason", default="a person re-queued it by hand",
                         help="why, recorded in the recording's history")
    requeue.set_defaults(handler=cmd_requeue)

    status = sub.add_parser("status", help="what is known, done, failed, and when it last worked")
    status.add_argument("--json", dest="as_json", action="store_true")
    status.add_argument("--day", default=None, help="the day to count (YYYY-MM-DD, default today)")
    status.set_defaults(handler=cmd_status)

    selftest = sub.add_parser("selftest", help="prove the deploy offline, with no credential")
    selftest.add_argument("--verbose", action="store_true", help="print every check, not only failures")
    selftest.set_defaults(handler=cmd_selftest)

    setup = sub.add_parser(
        "setup", help="interactive wizard — asks for everything, checks it, writes .env"
    )
    setup.add_argument("--env", default=".env", help="path to write (default: .env)")
    setup.add_argument(
        "--no-verify", action="store_true",
        help="do not check the answers against the real services (offline, or before consent is granted)",
    )
    setup.add_argument("--yes", action="store_true", help="take the default for every yes/no question")
    setup.set_defaults(handler=cmd_setup)

    return parser


# --------------------------------------------------------------------------- shared setup


def _config(args: argparse.Namespace) -> Config:
    config = Config.from_env()
    if getattr(args, "ledger", None):
        config.ledger_path = args.ledger
    configure_logging(config, level=args.log_level, json_lines=args.json_logs or None)
    return config


def _service(args: argparse.Namespace) -> tuple[Config, Ledger, Any]:
    config = _config(args)
    # ``config.scrub`` goes in so a secret in an unanticipated exception message is filtered
    # at the ledger boundary too. It is the one sink that is never pruned.
    ledger = Ledger(config.ledger_path, scrub=config.scrub)
    return config, ledger, build_graph(config)


# --------------------------------------------------------------------------- commands


def cmd_setup(args: argparse.Namespace) -> int:
    """The wizard writes .env, so it must NOT go through _config() first — that would
    refuse to start for exactly the settings the wizard exists to collect."""
    from .setup_wizard import run_setup

    return run_setup(env_path=args.env, verify=not args.no_verify, assume_yes=args.yes)


def cmd_once(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        worker = Worker(config, ledger, graph)
        report = worker.run_once(limit=args.limit)
        print(report.line())
        for outcome in report.outcomes:
            print("  " + outcome.line())
        for error in report.errors:
            print("  ERROR " + error, file=sys.stderr)
        return EXIT_OK if report.ok else EXIT_FAILED


def cmd_run(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        return Worker(config, ledger, graph).run()


def cmd_sweep(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        report = sweep_module.sweep(config, ledger, graph, dry_run=args.dry_run)
        print(report.render())
        return EXIT_OK if report.ok else EXIT_FAILED


def cmd_digest(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        try:
            print(run_digest(config, ledger, graph=graph, dry_run=args.dry_run, day=args.day))
        except DigestUnavailable as exc:
            print(f"the digest was not sent: {exc}", file=sys.stderr)
            return EXIT_FAILED
        return EXIT_OK


def cmd_archive(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        report = archive_module.archive(
            config, ledger, graph, dry_run=args.dry_run, limit=args.limit, age_days=args.age_days
        )
        print(report.render())
        return EXIT_OK if report.ok else EXIT_FAILED


def cmd_backfill(args: argparse.Namespace) -> int:
    """History, newest first, in a lane of its own.

    Enumeration is delta from a zero cursor — never ``/children``, which comes back short
    while the phone is writing. Ordering is newest first because the most recent history is
    the most likely to be asked about. Resumption needs no bookmark: the ledger already
    says which recordings are finished, so a re-run continues rather than restarts.
    """
    config, ledger, graph = _service(args)
    with ledger:
        seen = _enumerate_all(ledger, graph, config)
        print(f"enumerated {seen} item(s) from the source folder")
        if args.enumerate_only:
            return EXIT_OK

        cutoff = time.time() - max(0, args.min_age_days) * 86400.0
        cutoff_iso = utc_now_iso(cutoff)
        worker = Worker(config, ledger, graph)
        worker.install_signal_handlers()

        processed = 0
        quarantined = 0
        while not worker.stopping:
            pending = _backfill_queue(ledger, cutoff_iso)
            if not pending:
                break
            if _live_work_waiting(ledger, cutoff_iso):
                print(f"today's recordings are being processed first; waiting {args.yield_seconds:.0f}s")
                time.sleep(max(0.0, args.yield_seconds))
                continue

            batch = pending[: max(1, args.concurrency)]
            if args.limit is not None and processed + len(batch) > args.limit:
                batch = batch[: max(0, args.limit - processed)]
            if not batch:
                break

            for outcome in worker.process_rows(batch, args.concurrency):
                processed += 1
                quarantined += 1 if outcome.needs_a_person else 0
                print(f"  {outcome.line()}")
                ledger.cursor_set(BACKFILL_POSITION, outcome.name or outcome.item_id)
            if args.limit is not None and processed >= args.limit:
                break

        remaining = len(_backfill_outstanding(ledger, cutoff_iso))
        if not remaining:
            ledger.cursor_set(BACKFILL_FINISHED, utc_now_iso())
        print(
            f"backfill: {processed} processed, {quarantined} quarantined, {remaining} still "
            f"to do (older than {args.min_age_days} day(s))"
        )
        worker.release_claims()
        return EXIT_OK if quarantined == 0 else EXIT_FAILED


def _enumerate_all(ledger: Ledger, graph: Any, config: Config) -> int:
    """Delta from a zero cursor, rows and cursor committed page by page."""
    seen = 0
    for page in graph.delta(config.source_folder_id or None, None):
        rows = [
            DriveItem.from_graph_item(item)
            for item in page.items
            if str(getattr(item, "id", "") or "") and not getattr(item, "is_folder", False)
        ]
        seen += len(rows)
        if page.cursor:
            ledger.record_page(rows, page.cursor, cursor_name=BACKFILL_CURSOR)
        else:
            for row in rows:
                ledger.upsert_discovered(row)
    return seen


def _backfill_queue(ledger: Ledger, cutoff_iso: str) -> list[Row]:
    """Unfinished recordings older than the cutoff, newest first."""
    rows = [
        row
        for row in claimable_now(ledger, 10_000, time.time())
        if (row.created_at or row.discovered_at or "") < cutoff_iso
    ]
    rows.sort(key=lambda r: (r.created_at or r.discovered_at or ""), reverse=True)
    return rows


def _backfill_outstanding(ledger: Ledger, cutoff_iso: str) -> list[Row]:
    """Everything in this lane that has not finished — including what is between attempts.

    Counted separately from the queue on purpose: a recording waiting out its backoff is
    still unfinished, and reporting it as nothing left to do is exactly the kind of quiet
    wrong answer this service exists to remove.
    """
    return [
        row
        for row in ledger.unfinished()
        if (row.created_at or row.discovered_at or "") < cutoff_iso
    ]


def _live_work_waiting(ledger: Ledger, cutoff_iso: str) -> bool:
    return any(
        (row.created_at or row.discovered_at or "") >= cutoff_iso
        for row in claimable_now(ledger, 200, time.time())
    )


# --------------------------------------------------------------------------- status


def cmd_requeue(args: argparse.Namespace) -> int:
    """Try one recording again, now.

    Exposed because the morning email tells a person a recording failed and then has to be
    able to tell them what to do about it. The re-queue takes effect immediately: any
    backoff left over from the previous attempt is cleared, since a manual re-queue is an
    explicit decision that this should be tried now and nothing from the failed attempt
    should be able to veto it.
    """
    config, ledger, _graph = _service(args)
    with ledger:
        row = ledger.get(args.item_id)
        if row is None:
            print(f"there is no recording with the id {args.item_id!r} in the ledger",
                  file=sys.stderr)
            return EXIT_FAILED
        was = row.state
        ledger.requeue(args.item_id, args.reason)
        print(f"{row.name or args.item_id}: {was} -> {State.DISCOVERED}; it will be picked up "
              f"on the next poll")
        return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Counts, failures with their reasons, and when this last worked.

    Runs even when the configuration is broken — that is often exactly when somebody runs
    it — but says so loudly and exits non-zero rather than pretending the service is fine.
    """
    config: Config | None = None
    problems = ""
    try:
        config = Config.from_env()
        if args.ledger:
            config.ledger_path = args.ledger
        ledger_path = config.ledger_path
    except ConfigError as exc:
        problems = str(exc)
        ledger_path = args.ledger or os.environ.get("LEDGER_PATH") or ""
        print(problems, file=sys.stderr)
        if not ledger_path:
            return EXIT_CONFIG

    # Installed before the ledger is touched. Without it a logging call made during status
    # escapes through logging.lastResort with no scrubber attached, and the reasons printed
    # below go to stdout through the module's own filter instead of raw.
    configure_logging(config) if config is not None else configure_logging()

    day = args.day or datetime.date.today().isoformat()
    with Ledger(ledger_path, scrub=getattr(config, "scrub", None)) as ledger:
        stats = ledger.stats()
        counts = ledger.counts_for_day(day)
        attention = ledger.attention_for_day(day)
        marks = {
            name: ledger.cursor_get(name)
            for name in (LAST_CYCLE_OK, LAST_CYCLE_ERROR, LAST_POLL_OK,
                         "worker:last_cycle_error_detail", "digest:last_sent_day",
                         "digest:last_attempt_at", "digest:last_error",
                         BACKFILL_POSITION, BACKFILL_FINISHED,
                         "sweep:last_error", "sweep:last_report_at",
                         "archive:last_report_at")
        }
        cursors = stats.get("cursors", {})

        if args.as_json:
            print(json.dumps(
                {"ledger": stats, "day": counts, "marks": marks, "attention": attention,
                 "config_problems": problems.splitlines()},
                indent=1, default=str,
            ))
        else:
            _print_status(ledger_path, stats, counts, marks, cursors, day, attention)

    if problems:
        return EXIT_CONFIG
    return EXIT_FAILED if (counts.get("failures") or []) else EXIT_OK


def _print_status(
    ledger_path: str,
    stats: dict[str, Any],
    counts: dict[str, Any],
    marks: dict[str, Any],
    cursors: dict[str, Any],
    day: str,
    attention: dict[str, Any] | None = None,
) -> None:
    by_state = stats.get("by_state", {})
    print(f"transcriber — {ledger_path}")
    print(f"  known            {stats.get('total', 0):>6}  recordings, all time")
    print(f"  done             {by_state.get(State.DONE, 0):>6}")
    print(f"  verified silence {by_state.get(State.SKIPPED_EMPTY, 0):>6}")
    print(f"  quarantined      {by_state.get(State.QUARANTINED, 0):>6}"
          f"{'   <- needs a person' if by_state.get(State.QUARANTINED) else ''}")
    unfinished = sum(v for k, v in by_state.items() if k not in State.TERMINAL)
    print(f"  still working    {unfinished:>6}")
    for state in State.PIPELINE[:-1]:
        if by_state.get(state):
            print(f"      {state.lower():<14} {by_state[state]:>6}")

    print(f"\n  {day}: {counts.get('discovered', 0)} arrived, {counts.get('done', 0)} done, "
          f"{counts.get('quarantined', 0)} quarantined, {counts.get('in_flight', 0)} unfinished")

    failures = counts.get("failures") or []
    if failures:
        print(f"\n  {len(failures)} failure(s) waiting for a person:")
        for failure in failures[:25]:
            print(f"    {failure.get('name') or failure.get('item_id')}  [{failure.get('state')}]")
            print(f"      {failure.get('reason')}")
            if failure.get("web_url"):
                print(f"      {strip_owner_paths(str(failure['web_url']))}")
        if len(failures) > 25:
            print(f"    ... and {len(failures) - 25} more")
    else:
        print("\n  nothing is waiting for a person")

    facts = dict(attention or {})
    if facts.get("review"):
        # The withheld items themselves, not just their count. The summary and actions files
        # tell a person they "were kept on the review list against this recording"; this is
        # where that list is read.
        print(f"\n  {facts['review']} proposed item(s) were withheld because their quote is "
              f"not in the transcript:")
        for row in list(facts.get("review_rows") or ())[:25]:
            print(f"    {row.get('name') or row.get('item_id')}: {row.get('count')} item(s)")
    if facts.get("unverified_duration_guard"):
        print(f"  {facts['unverified_duration_guard']} split recording(s) could not have the "
              f"assembled transcript measured against the clock")
    if facts.get("degraded_transcripts"):
        print(f"  {facts['degraded_transcripts']} transcript(s) were produced with engine "
              f"settings stripped")

    print("")
    _print_mark("last successful cycle", marks.get(LAST_CYCLE_OK))
    _print_mark("last successful poll", marks.get(LAST_POLL_OK))
    if marks.get(LAST_CYCLE_ERROR):
        _print_mark("last failed cycle", marks.get(LAST_CYCLE_ERROR))
        if marks.get("worker:last_cycle_error_detail"):
            print(f"      {marks['worker:last_cycle_error_detail']}")
    _print_mark("last digest attempt", marks.get("digest:last_attempt_at"))
    if marks.get("digest:last_sent_day"):
        print(f"  digest last sent for {marks['digest:last_sent_day']}")
    if marks.get("digest:last_error"):
        print(f"  digest problem: {marks['digest:last_error']}")
    _print_mark("last sweep report", marks.get("sweep:last_report_at"))
    if marks.get("sweep:last_error"):
        print(f"  sweep problem: {marks['sweep:last_error']}")
    _print_mark("last archive report", marks.get("archive:last_report_at"))
    if marks.get(BACKFILL_POSITION):
        finished = marks.get(BACKFILL_FINISHED)
        print(f"  backfill: {'finished ' + finished if finished else 'last at ' + str(marks[BACKFILL_POSITION])}")

    live = cursors.get(DELTA_CURSOR) or {}
    print(f"  change feed cursor: "
          f"{'stored, updated ' + str(live.get('updated_at')) if live.get('value_present') else 'NOT SET — the next poll enumerates from zero'}")
    swept = cursors.get(SWEEP_CURSOR) or {}
    if swept:
        print(f"  sweep cursor updated {swept.get('updated_at')}")
    oldest = stats.get("oldest_unfinished")
    if oldest:
        print(f"  oldest unfinished: {oldest.get('name')} (discovered {oldest.get('discovered_at')})")


def _print_mark(label: str, value: Any) -> None:
    if not value:
        print(f"  {label}: never")
        return
    print(f"  {label}: {value}{_ago(str(value))}")


def _ago(stamp: str) -> str:
    try:
        when = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return ""
    seconds = (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds()
    if seconds < 90:
        return f"  ({int(seconds)} seconds ago)"
    if seconds < 5400:
        return f"  ({int(seconds // 60)} minutes ago)"
    if seconds < 172800:
        return f"  ({seconds / 3600:.1f} hours ago)"
    return f"  ({seconds / 86400:.1f} days ago)"


# --------------------------------------------------------------------------- selftest


class _Checks:
    """A tiny harness: every check names itself, and a failure prints why, not just where."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed = 0
        self.failures: list[str] = []
        self._section = ""

    def section(self, name: str) -> None:
        self._section = name
        print(f"\n{name}")

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            if self.verbose:
                print(f"  ok    {what}")
            return True
        self.failures.append(f"{self._section}: {what}" + (f" — {detail}" if detail else ""))
        print(f"  FAIL  {what}")
        if detail:
            print(f"        {detail}")
        return False

    def raises(self, what: str, exception: type[BaseException], fn: Callable[[], Any]) -> bool:
        try:
            fn()
        except exception:
            return self.check(what, True)
        except Exception as exc:  # noqa: BLE001
            return self.check(what, False, f"raised {type(exc).__name__}: {exc}")
        return self.check(what, False, "it did not raise at all")


def cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the deploy offline: no credential, no network, no configuration read.

    Everything here runs against real modules with fake collaborators, so a check that
    passes is a statement about the code that will run in production, not about a mock.
    """
    checks = _Checks(verbose=args.verbose)
    print("transcriber selftest — offline, no credential, no network")

    with tempfile.TemporaryDirectory(prefix="transcriber-selftest-") as tmp:
        config = Config.offline(ledger_path=os.path.join(tmp, "ledger.sqlite3"))
        config.work_dir = os.path.join(tmp, "work")
        config.settle_interval_s = 1        # the real two-read gate, one second apart
        config.lease_seconds = 60
        config.max_attempts = 2
        config.output_folder_id = "output-folder"
        os.makedirs(config.work_dir, exist_ok=True)
        # Quiet by default: the checks report what they found, and a deliberately provoked
        # quarantine printing ERROR next to a passing check reads as a failure when it is not.
        level = "INFO" if args.verbose else "CRITICAL"
        configure_logging(config, level=level, stream=sys.stderr)

        _selftest_naming(checks)
        _selftest_ledger(checks, os.path.join(tmp, "state.sqlite3"))
        _selftest_audio(checks)
        _selftest_plausibility(checks)
        _selftest_quotes(checks)
        _selftest_outputs(checks)
        _selftest_redaction(checks, config, level)
        _selftest_pipeline(checks, config)

    print("")
    if checks.failures:
        print(f"SELFTEST FAILED — {len(checks.failures)} of {checks.passed + len(checks.failures)} "
              f"checks did not pass:")
        for failure in checks.failures:
            print(f"  - {failure}")
        return EXIT_FAILED
    print(f"selftest passed — {checks.passed} checks, offline, with no credential")
    return EXIT_OK


# -- 1. parsing --------------------------------------------------------------------


def _selftest_naming(checks: _Checks) -> None:
    checks.section("filenames and timestamps")

    call = naming.parse_source_name("Call Nicholas Burgers_260827_141500.m4a")
    checks.check("the call form is recognised", call.matched_call_form, call.form)
    checks.check("the counterparty is read from the name", call.party == "Nicholas Burgers",
                 repr(call.party))
    checks.check("the phone's own clock is used",
                 call.timestamp is not None and call.timestamp.strftime("%Y%m%d-%H%M%S") == "20260827-141500",
                 repr(call.timestamp))

    older = naming.parse_source_name("Call recording Ulrich_260401_073012.m4a")
    checks.check("the older handset's form is recognised", older.matched_call_form, older.form)

    typed = naming.parse_source_name("Beach Court roof inspection.m4a")
    checks.check("a hand-typed name is kept, not rejected", typed.is_free_text and not typed.has_timestamp)
    checks.raises(
        "a hand-typed name with no Graph created time refuses to invent one",
        naming.TimestampUnavailable,
        lambda: naming.resolve_timestamp(typed, None),
    )
    when, note = naming.resolve_timestamp(typed, "2026-04-01T05:30:12Z")
    checks.check("it falls back to the item's created time, and says so",
                 when.year == 2026 and "OneDrive" in note, note)

    stamped, _ = naming.resolve_timestamp(call, None)
    names = naming.output_names(stamped, call.stem, item_id="item-A")
    checks.check("the three output names share one stamp and one stem",
                 names.transcript.startswith("20260827-141500-Call Nicholas Burgers_260827_141500-")
                 and names.summary.endswith("-summary.md")
                 and names.actions.endswith("-actions.md"),
                 names.transcript)
    checks.check("a stamped output name is recognised as ours",
                 naming.is_output_name(names.transcript))
    checks.check("only the transcript is named so the record ingests it as evidence",
                 not names.transcript.startswith("_")
                 and names.summary.startswith("_") and names.actions.startswith("_"),
                 f"{names.summary} / {names.actions}")

    copied = naming.parse_source_name("Call Nicholas Burgers_260827_141500 (1).m4a")
    checks.check("OneDrive's duplicate marker does not change the recording's identity",
                 copied.copy_marker == 1 and copied.timestamp == call.timestamp)
    duplicate = naming.output_names(stamped, copied.stem, copy_marker=copied.copy_marker,
                                    item_id="item-B")
    checks.check(
        "a re-uploaded duplicate writes different files, so it cannot destroy the first",
        set(duplicate.as_tuple()).isdisjoint(names.as_tuple()),
        f"{names.transcript} vs {duplicate.transcript}",
    )

    addressed = naming.parse_source_name("Call carel@example.co.za_260827_120055.m4a")
    with_address = naming.output_names(stamped, addressed.stem, item_id="item-C")
    checks.check(
        "an address in the source filename never reaches an output filename",
        not any("@" in name for name in with_address.as_tuple()),
        with_address.transcript,
    )


# -- 2. the state machine ----------------------------------------------------------


def _selftest_ledger(checks: _Checks, path: str) -> None:
    checks.section("the ledger and the state machine")

    with Ledger(path) as ledger:
        item = _drive_item("item-1", "Call Ulrich_260827_090000.m4a", b"x" * 1024)
        checks.check("a new recording is inserted", ledger.upsert_discovered(item))
        checks.check("the same recording is not inserted twice", not ledger.upsert_discovered(item))

        # The load-bearing invariant: the cursor cannot advance on its own.
        checks.raises(
            "a delta cursor cannot be set without its rows",
            Exception,
            lambda: ledger.cursor_set(DELTA_CURSOR, "https://example.invalid/delta?token=1"),
        )
        second = _drive_item("item-2", "Call Zanele_260826_170000.m4a", b"y" * 2048)
        ledger.record_page([second], "https://example.invalid/delta?token=2")
        checks.check("rows and cursor commit together",
                     ledger.get("item-2") is not None
                     and ledger.cursor_get(DELTA_CURSOR) == "https://example.invalid/delta?token=2")

        checks.check("a recording can be claimed", ledger.claim("item-1", 60, owner="a"))
        checks.check("a claimed recording cannot be claimed twice",
                     not ledger.claim("item-1", 60, owner="b"))
        checks.check("claiming moves DISCOVERED to CLAIMED",
                     (ledger.get("item-1") or Row("")).state == State.CLAIMED)
        checks.check("an expired lease is re-claimable by somebody else",
                     ledger.claim("item-1", 60, owner="b", now=time.time() + 3600))

        ledger.advance("item-1", State.FETCHED, content_hash="abc", size=1024)
        ledger.advance("item-1", State.TRANSCRIBED, word_count=412)
        ledger.advance("item-1", State.ANALYSED)
        ledger.advance("item-1", State.DONE, transcript_name="t.md", summary_name="s.md",
                       actions_name="a.md", output_item_ids={"transcript": "o1"})
        row = ledger.get("item-1")
        checks.check("the happy path ends DONE with its three outputs recorded",
                     row is not None and row.state == State.DONE and row.outputs_present)
        checks.raises("a finished recording cannot be quietly un-finished", Exception,
                      lambda: ledger.advance("item-1", State.CLAIMED))
        checks.raises("a quarantine without a reason is refused", Exception,
                      lambda: ledger.quarantine("item-2", "   "))

        ledger.quarantine("item-2", "the audio stops 40 seconds in")
        checks.check("a quarantined recording is still a row, never a deletion",
                     (ledger.get("item-2") or Row("")).state == State.QUARANTINED)
        checks.check("a failed attempt releases the claim so another worker can try",
                     ledger.record_attempt("item-1", "a made-up failure") == 1
                     and (ledger.get("item-1") or Row("")).claimed_by is None)
        checks.check("unfinished() reports what never finished", ledger.unfinished() == [])


# -- 3. the audio itself -----------------------------------------------------------


def _selftest_audio(checks: _Checks) -> None:
    checks.section("the audio integrity check")

    with tempfile.TemporaryDirectory(prefix="transcriber-audio-") as tmp:
        whole = os.path.join(tmp, "whole.m4a")
        with open(whole, "wb") as handle:
            handle.write(audio_module.build_mp4_bytes(duration_s=754.0))
        info = audio_module.probe(whole, use_ffprobe=False)
        checks.check("a whole recording probes as intact",
                     not info.truncated and info.duration_s > 700, f"{info.container} {info.reason}")

        cut = os.path.join(tmp, "battery-died.m4a")
        with open(cut, "wb") as handle:
            handle.write(audio_module.truncated_mp4_bytes(duration_s=754.0))
        cut_info = audio_module.probe(cut, use_ffprobe=False)
        checks.check("a recording cut off mid-upload is caught by the container walk",
                     cut_info.truncated, cut_info.reason or "it was reported as intact")

        overrun = os.path.join(tmp, "overrun.m4a")
        with open(overrun, "wb") as handle:
            handle.write(audio_module.mdat_overrun_mp4_bytes())
        checks.check("an mdat that overruns the file is caught",
                     audio_module.probe(overrun, use_ffprobe=False).truncated)

        checks.check("the walk needs no ffprobe", audio_module.probe(cut, use_ffprobe=False).truncated)


# -- 4. plausibility ---------------------------------------------------------------


_ORDINARY_TRANSCRIPT = (
    "James: We walked the whole of block C this morning with the foreman and the "
    "waterproofing subcontractor. The screed falls are wrong across the eastern half, water "
    "is ponding against the parapet, and the flashing has been cut short of the coping on "
    "two elevations. I have asked for a method statement before anybody lifts another "
    "sheet, because if they torch over standing water we will be back here in winter.\n"
    "James: The trustees want a date. I said I would not give one until the engineer has "
    "seen the eastern slab, and that if the falls have to be re-screeded it is three weeks "
    "minimum, plus curing, plus the membrane itself.\n"
    "James: On money, the variation for the extra flashing has not been priced and nobody "
    "has issued a site instruction for it. Somebody has to decide whether that sits under "
    "the provisional sum or comes out of the contingency, and it is not mine to decide.\n"
    "James: Snag list from last Thursday is still open on units four, seven and eleven. "
    "The plasterer never came back. I want that in writing to the main contractor today, "
    "with photographs attached, so the record shows when it was raised.\n"
)


def _selftest_plausibility(checks: _Checks) -> None:
    checks.section("plausibility")

    forty_minutes = AudioInfo(duration_s=2400.0, container="mp4", truncated=False,
                              detail={"duration_known": True})
    eleven_words = Transcript(text="so we went to the site and then the call dropped", engine="x")
    verdict = plausibility.assess(eleven_words, forty_minutes)
    checks.check("a forty-minute recording yielding eleven words is refused",
                 verdict.is_implausible, verdict.reason)
    checks.check("it goes to a person, not to done",
                 verdict.ledger_state == State.QUARANTINED, str(verdict.ledger_state))

    four_minutes = AudioInfo(duration_s=240.0, container="mp4", truncated=False,
                             detail={"duration_known": True})
    real = Transcript(text=_ORDINARY_TRANSCRIPT, engine="x",
                      segments=[Segment(0.0, 240.0, "James", _ORDINARY_TRANSCRIPT)])
    ordinary = plausibility.assess(real, four_minutes)
    checks.check("an ordinary transcript passes", ordinary.is_plausible, ordinary.reason)

    short_silence = AudioInfo(duration_s=6.0, container="mp4", truncated=False,
                              detail={"duration_known": True})
    silent = plausibility.assess(Transcript(text="", engine="x"), short_silence)
    checks.check("verified silence is its own visible state, not a failure",
                 silent.ledger_state == State.SKIPPED_EMPTY, str(silent.ledger_state))

    cut = AudioInfo(duration_s=41.0, container="mp4", truncated=True, reason="the moov atom is missing",
                    detail={"duration_known": True})
    checks.check("a truncated file is refused even when the words look fine",
                 plausibility.assess(real, cut).is_implausible)


# -- 5. quote verification ---------------------------------------------------------

_TRANSCRIPT_TEXT = (
    "James: Right, I'm standing on the roof at Beach Court and the torch-on has lifted "
    "along the parapet on the north side.\n"
    "James: I'll send the trustees a written instruction on Friday, and I'll get Ulrich to "
    "price the flashing before then.\n"
    "Nicholas: Ja, approved, go ahead on Beach Court.\n"
    "James: The retention is still sitting at sixty-nine thousand and something, so that "
    "needs to come off the next certificate.\n"
)


def _selftest_quotes(checks: _Checks) -> None:
    from . import extract as extract_module

    checks.section("quote verification and the safety override")

    good = extract_module.verify_quote("I'll send the trustees a written instruction on Friday",
                                       _TRANSCRIPT_TEXT)
    checks.check("a real quote is found in the transcript", good.ok, good.reason)

    fabricated = extract_module.verify_quote(
        "I'll send the trustees a written instruction on Monday and close the item",
        _TRANSCRIPT_TEXT,
    )
    checks.check("a fabricated quote is refused", not fabricated.ok,
                 "a quote that is not in the transcript was accepted")

    checks.raises(
        "an item cannot claim anything but the agent observed it",
        ValueError,
        lambda: extract_module.ExtractedItem(kind="commitment", text="x", quote="y",
                                             observed_by="James Janeke"),
    )
    checks.raises(
        "an item with no quote is refused",
        ValueError,
        lambda: extract_module.ExtractedItem(kind="commitment", text="x", quote="   "),
    )
    item = extract_module.ExtractedItem(kind="commitment", text="x", quote="y")
    checks.check("no item can carry decided_by", "decided_by" not in item.to_dict())

    triggers = extract_module.route_precheck("Ja, approved, go ahead on Beach Court.", ())
    checks.check("a twelve-second approval is forced to a full read, however short",
                 bool(triggers), "nothing in it triggered the safety check")

    # The real two-tier pass, offline: a canned provider response through the caller hook.
    settings = extract_module.AnalysisSettings(provider="anthropic", api_key="offline-not-a-key")
    extraction = extract_module.Extractor(
        settings, caller=_canned_analysis(settings)
    ).extract(Transcript(text=_TRANSCRIPT_TEXT, engine="selftest"), Hints(source_name="selftest.m4a"))

    checks.check("the verified proposal survives", len(extraction.proposals) == 1,
                 f"{len(extraction.proposals)} proposal(s)")
    checks.check("the fabricated one never becomes a proposal",
                 all("Monday" not in p.item.quote for p in extraction.proposals))
    checks.check("the fabricated one is on the review list rather than discarded",
                 any("Monday" in r.offered_quote for r in extraction.review),
                 f"{len(extraction.review)} review item(s)")
    checks.check("every proposal is observed_by agent",
                 all(p.item.observed_by == "agent" for p in extraction.proposals))
    checks.check("nothing produced carries decided_by",
                 "decided_by" not in json.dumps(extraction.to_dict()))


def _canned_analysis(settings: Any) -> Callable[..., dict[str, Any]]:
    """A stand-in for the model, so the real extractor runs with no network.

    It offers two commitments: one quoting the transcript exactly, one quoting words nobody
    said. Verification is what has to tell them apart, and it is the real verification here.
    """

    def caller(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if body.get("model") == settings.model_cheap:
            data: dict[str, Any] = {
                "label": "substantive",
                "one_line": "A site walk at Beach Court, with an instruction and a retention figure.",
                "languages": ["English"],
                "mentions": {"person": True, "site": True, "number": True, "date": True,
                             "amount": True, "approval": True, "promise": True},
                "reason": "it names a site, a person, an amount and a promise",
            }
        else:
            data = {
                "summary_en": "James inspected the roof at Beach Court and undertook to write to "
                              "the trustees; a retention figure was mentioned.",
                "languages": ["English"],
                "participants": [
                    {"name_or_role": "Nicholas",
                     "quote": "Ja, approved, go ahead on Beach Court."}
                ],
                "site": {"name": "Beach Court",
                         "quote": "I'm standing on the roof at Beach Court"},
                "decisions": [], "money": [], "materials": [], "defects": [], "safety": [],
                "programme": [], "open_questions": [], "follow_ups": [],
                "commitments": [
                    {"owner": "James", "what": "send the trustees a written instruction",
                     "by_when": "Friday",
                     "quote": "I'll send the trustees a written instruction on Friday",
                     "speaker": "James", "site": "Beach Court", "confidence": 0.9},
                    {"owner": "James", "what": "close the item",
                     "by_when": "Monday",
                     "quote": "I'll send the trustees a written instruction on Monday and "
                              "close the item",
                     "speaker": "James", "site": "Beach Court", "confidence": 0.4},
                ],
                "unclear_passages": [],
            }
        return {"content": [{"type": "text", "text": json.dumps(data)}], "usage": {},
                "stop_reason": "end_turn"}

    return caller


# -- 6. the markdown contract ------------------------------------------------------


def _selftest_outputs(checks: _Checks) -> None:
    from . import extract as extract_module
    from .engines import SplitDurationError, verify_result_duration

    checks.section("the markdown the record has to be able to read")

    ctx = _selftest_context(extract_module)
    rendered = outputs.render_all(ctx)
    checks.check("all three files render", len(rendered) == 3,
                 ", ".join(f.kind for f in rendered))

    for one in rendered:
        problems = outputs.check_contract(one.text)
        checks.check(f"{one.kind}: the record can read it", not problems, "; ".join(problems))

        head, body = outputs.parse_like_downstream(one.text)
        kind = "transcript" if not head.get("from") else "email"
        checks.check(f"{one.kind}: the record classifies it as a transcript", kind == "transcript",
                     f"it would be read as an {kind}")
        checks.check(f"{one.kind}: only Subject and Date reach the header",
                     set(head) <= {"subject", "date"}, ", ".join(sorted(head)))
        checks.check(f"{one.kind}: nothing was swallowed between header and body",
                     bool(body.strip()), "the body came back empty")
        checks.check(f"{one.kind}: there is no email address anywhere in it",
                     "@" not in one.text or not _has_address(one.text))
        checks.check(f"{one.kind}: it cannot claim a person decided anything",
                     "decided_by" not in one.text)
        checks.check(f"{one.kind}: every item says the agent observed it",
                     one.kind != "actions" or "observed_by: agent" in one.text)

    transcript_file = next(f for f in rendered if f.kind == "transcript")
    checks.check("the transcript carries the words that were said",
                 "torch-on has lifted" in transcript_file.text)
    actions_file = next(f for f in rendered if f.kind == "actions")
    checks.check("the actions file carries the verbatim quote behind each proposal",
                 "written instruction on Friday" in actions_file.text)
    checks.check("a quote that was not in the transcript never reaches the actions file",
                 "close the item" not in actions_file.text)

    # The contract check, against files built to break it in each of the ways that matter.
    header_date = transcript_file.text.split("\n")[1]
    swallowed = f"Subject: A site walk\n{header_date}\nRecording: call.m4a\n\nthe body\n"
    checks.check("a third header line, which the record silently swallows, is caught",
                 bool(outputs.check_contract(swallowed)),
                 "a line that reaches neither header nor body was accepted")
    as_email = f"From: somebody\nSubject: A site walk\n{header_date}\n\nthe body\n"
    checks.check("a From: header, which reclassifies the file as an email, is caught",
                 bool(outputs.check_contract(as_email)))
    with_address = f"Subject: A site walk\n{header_date}\n\nwrite to nic@example.invalid\n"
    checks.check("an email address anywhere in the file is caught",
                 bool(outputs.check_contract(with_address)))
    decided = f"Subject: A site walk\n{header_date}\n\ndecided_by: James Janeke\n"
    checks.check("a file claiming a person decided something is caught",
                 bool(outputs.check_contract(decided)))
    spoken = (f"Subject: A site walk\n{header_date}\n\nput decided_by James on that one, "
              f"he said\n")
    checks.check("a person merely SAYING those words in the recording is not caught",
                 not outputs.check_contract(spoken),
                 "; ".join(outputs.check_contract(spoken)))
    owner_path = (f"Subject: A site walk\n{header_date}\n\n- Recording: "
                  f"https://x-my.sharepoint.com/personal/james_kbc_co_za/Documents/a.m4a\n")
    checks.check("an owner's address written as a OneDrive path is caught",
                 bool(outputs.check_contract(owner_path)))
    dictated = (f"Subject: A site walk\n{header_date}\n\nsend it to carel at kbc dot co "
                f"dot za please\n")
    checks.check("an address spoken out loud rather than spelled is caught",
                 bool(outputs.check_contract(dictated)))
    checks.check("an output filename carrying an address is refused",
                 bool(outputs.check_name("20260827-143005-Call carel@kbc.co.za.md")))

    # The split guard, proved without splitting anything: pieces that do not account for the
    # recording's duration raise rather than returning a short transcript.
    checks.raises(
        "reassembled pieces that lose half the recording fail loudly",
        SplitDurationError,
        lambda: verify_result_duration(
            Transcript(text="a few words", segments=[Segment(0.0, 300.0, None, "a few words")]),
            2400.0,
            source_name="selftest.m4a",
        ),
    )


def _selftest_context(extract_module: Any) -> outputs.OutputContext:
    parsed = naming.parse_source_name("Call Nicholas Burgers_260827_141500.m4a")
    when, note = naming.resolve_timestamp(parsed, None)
    transcript = Transcript(
        text=_TRANSCRIPT_TEXT,
        segments=[
            Segment(0.0, 12.0, "James", "Right, I'm standing on the roof at Beach Court and the "
                                        "torch-on has lifted along the parapet on the north side."),
            Segment(12.0, 24.0, "James", "I'll send the trustees a written instruction on Friday, "
                                         "and I'll get Ulrich to price the flashing before then."),
            Segment(24.0, 27.0, "Nicholas", "Ja, approved, go ahead on Beach Court."),
        ],
        language="en-ZA",
        engine="selftest",
        duration_s=754.0,
    )
    settings = extract_module.AnalysisSettings(provider="anthropic", api_key="offline-not-a-key")
    extraction = extract_module.Extractor(settings, caller=_canned_analysis(settings)).extract(
        transcript, Hints(source_name=parsed.original_name)
    )
    return outputs.OutputContext(
        item_id="selftest-item",
        source_name=parsed.original_name,
        parsed=parsed,
        recorded_at=when,
        timestamp_source=note,
        transcript=transcript,
        extraction=extraction,
        audio=AudioInfo(duration_s=754.0, container="mp4", truncated=False,
                        probed_by="walk", detail={"duration_known": True}),
        content_hash="0" * 64,
        graph_hash="0" * 64,
        web_url="https://example.invalid/item",
        engine="selftest",
    )


def _has_address(text: str) -> bool:
    from .models import EMAIL_RE

    return bool(EMAIL_RE.search(text))


# -- 7. redaction ------------------------------------------------------------------


def _selftest_redaction(checks: _Checks, config: Config, restore_level: str = "CRITICAL") -> None:
    import io
    import logging

    from . import logging_setup

    checks.section("secrets never reach a log line")

    secret = "sk-selftest-0123456789-not-a-real-key"
    stream = io.StringIO()
    logging_setup.configure(level="DEBUG", stream=stream, secrets=(secret,))
    logger = logging.getLogger("transcriber.selftest")

    logger.info("using key %s", secret)
    try:
        raise RuntimeError(f"the provider rejected {secret}")
    except RuntimeError:
        logger.exception("a call failed")
    logger.warning("writing to %s", "james@example.invalid")

    written = stream.getvalue()
    checks.check("a secret passed as an argument is scrubbed", secret not in written)
    checks.check("a secret inside a traceback is scrubbed", "not-a-real-key" not in written)
    checks.check("the traceback is still there, on one line",
                 "traceback=" in written and "RuntimeError" in written)
    checks.check("an email address never reaches a log line",
                 "james@example.invalid" not in written)
    checks.check("every line carries the item field",
                 all("item=" in line for line in written.splitlines() if line.strip()))
    checks.check("one event is one line", len([l for l in written.splitlines() if l.strip()]) == 3,
                 f"{len(written.splitlines())} lines for 3 events")

    with logging_setup.item_context("item-42"):
        logger.info("inside the context")
    checks.check("the item id follows the recording through the log",
                 "item=item-42" in stream.getvalue())

    scrubbed = logging_setup.scrub(f"key={config.graph_client_secret} to=nobody@example.invalid")
    checks.check("the config's own secrets are registered for scrubbing",
                 config.graph_client_secret not in scrubbed and "nobody@example.invalid" not in scrubbed,
                 scrubbed)

    # Put the handler back where the rest of the selftest expects it.
    logging_setup.configure(config, level=restore_level, stream=sys.stderr)


# -- 8. the state machine end to end -----------------------------------------------


def _selftest_pipeline(checks: _Checks, config: Config) -> None:
    from . import extract as extract_module

    checks.section("one recording, end to end, with no network")

    settings = extract_module.AnalysisSettings(provider="anthropic", api_key="offline-not-a-key")
    extractor = extract_module.Extractor(settings, caller=_canned_analysis(settings))

    with Ledger(config.ledger_path) as ledger:
        graph = _FakeGraph()
        seconds = 120.0
        whole = audio_module.build_mp4_bytes(duration_s=seconds)
        graph.add("rec-1", "Call Nicholas Burgers_260827_141500.m4a", whole)
        ledger.upsert_discovered(_drive_item("rec-1", "Call Nicholas Burgers_260827_141500.m4a", whole))

        pipeline = Pipeline(
            config, ledger, graph,
            engine=_FakeEngine(_TRANSCRIPT_TEXT, seconds),
            extractor=extractor,
            keep_work_files=True,
            sleep=_generous_sleep,
        )
        outcome = pipeline.process_one("rec-1")
        checks.check("a good recording reaches DONE", outcome.result == "done", outcome.reason)
        row = ledger.get("rec-1")
        checks.check("its three outputs are recorded against the row",
                     row is not None and row.outputs_present,
                     str(row.state if row else "no row"))
        checks.check("three files were written, and no more", len(graph.uploads) == 3,
                     f"{len(graph.uploads)} uploaded")
        checks.check("every file was read back before the row said DONE",
                     graph.read_backs >= 3, f"{graph.read_backs} read back")
        checks.check("processing it again does no work and changes nothing",
                     pipeline.process_one("rec-1").result == "already-finished")

        # A recording cut off by a dying battery: uploads perfectly, hashes perfectly.
        cut = audio_module.truncated_mp4_bytes(duration_s=seconds)
        graph.add("rec-2", "Call Ulrich_260827_161500.m4a", cut)
        ledger.upsert_discovered(_drive_item("rec-2", "Call Ulrich_260827_161500.m4a", cut))
        cut_outcome = Pipeline(
            config, ledger, graph, engine=_FakeEngine("this should never run", seconds),
            extractor=extractor, keep_work_files=True, sleep=_generous_sleep,
        ).process_one("rec-2")
        checks.check("a truncated recording is quarantined, not transcribed",
                     cut_outcome.result == "quarantined", cut_outcome.reason)
        checks.check("nothing was written for it", len(graph.uploads) == 3,
                     f"{len(graph.uploads)} files after the truncated one")
        cut_row = ledger.get("rec-2")
        checks.check("the quarantine says why, in words a person can act on",
                     "not a whole recording" in ((cut_row.quarantine_reason if cut_row else "") or ""),
                     (cut_row.quarantine_reason if cut_row else "") or "no reason was recorded")

        # A crash mid-way: the row keeps its progress and the next pass resumes from it.
        graph.add("rec-3", "Call Zanele_260826_101500.m4a", whole)
        ledger.upsert_discovered(_drive_item("rec-3", "Call Zanele_260826_101500.m4a", whole))
        failing = Pipeline(config, ledger, graph, engine=_FailingEngine(), extractor=extractor,
                           keep_work_files=True, sleep=_generous_sleep)
        first = failing.process_one("rec-3")
        checks.check("a failure retries rather than vanishing", first.result == "retry", first.reason)
        mid = ledger.get("rec-3")
        checks.check("the row remembers it already downloaded and verified the audio",
                     mid is not None and mid.state == State.FETCHED and bool(mid.content_hash),
                     str(mid.state if mid else "no row"))

        resumed = Pipeline(config, ledger, graph, engine=_FakeEngine(_TRANSCRIPT_TEXT, seconds),
                           extractor=extractor, keep_work_files=True, sleep=_generous_sleep)
        # Clear the backoff the failure set: the point being proved is resumption, not waiting.
        ledger.set_fields("rec-3", meta={})
        second = resumed.process_one("rec-3")
        checks.check("the next pass finishes it from where it stopped", second.result == "done",
                     second.reason)
        checks.check("it did not download the audio a second time", graph.downloads["rec-3"] == 1,
                     f"{graph.downloads['rec-3']} downloads")

        # Every failure ends visible: attempts past the limit stop, loudly.
        graph.add("rec-4", "Call Sipho_260825_081500.m4a", whole)
        ledger.upsert_discovered(_drive_item("rec-4", "Call Sipho_260825_081500.m4a", whole))
        always = Pipeline(config, ledger, graph, engine=_FailingEngine(always=True),
                          extractor=extractor, keep_work_files=True, sleep=_generous_sleep)
        results = []
        for _ in range(config.max_attempts + 1):
            ledger.set_fields("rec-4", meta={})
            results.append(always.process_one("rec-4").result)
        checks.check("a recording that keeps failing ends quarantined, never done",
                     "quarantined" in results and "done" not in results, ", ".join(results))

        counts = ledger.stats()["by_state"]
        checks.check("nothing was deleted along the way",
                     ledger.stats()["total"] == 4, str(counts))


def _generous_sleep(seconds: float) -> None:
    """Sleep a shade longer than asked.

    The completeness gate compares the elapsed time between its two reads against the settle
    interval. A sleep that returns a millisecond early makes that comparison fail and the
    selftest hang on a second pass, which says nothing about the code under test.
    """
    time.sleep(seconds + 0.25)


# -- fakes for the offline end-to-end run ------------------------------------------


class _FakeEngine:
    """An engine that returns a fixed transcript. ``max_bytes=None``: no splitting."""

    name = "selftest"
    max_bytes: int | None = None

    def __init__(self, text: str, duration_s: float = 120.0) -> None:
        self.text = text
        self.duration_s = duration_s

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        return Transcript(
            text=self.text,
            segments=[Segment(0.0, self.duration_s, "James", self.text.split("\n")[0])],
            language="en-ZA",
            engine=self.name,
            duration_s=self.duration_s,
        )


class _FailingEngine:
    """Fails the way a provider does: after the audio is downloaded and verified."""

    name = "selftest-failing"
    max_bytes: int | None = None

    def __init__(self, always: bool = False) -> None:
        self.always = always
        self.calls = 0

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        self.calls += 1
        raise RuntimeError("the transcription provider timed out")


class _FakeGraph:
    """Enough of Graph to run the pipeline offline: get, download, upload, read back."""

    def __init__(self) -> None:
        self.items: dict[str, Any] = {}
        self.content: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.downloads: dict[str, int] = {}
        self.read_backs = 0
        self._next = 0

    def add(self, item_id: str, name: str, data: bytes) -> None:
        from .graph import DriveItem as GraphItem

        self.items[item_id] = GraphItem.from_api(_graph_payload(item_id, name, data))
        self.content[item_id] = data
        self.downloads.setdefault(item_id, 0)

    def get_item(self, item_id: str) -> Any:
        if item_id not in self.items:
            raise KeyError(item_id)
        if item_id in self.uploads_by_id:
            self.read_backs += 1
        return self.items[item_id]

    @property
    def uploads_by_id(self) -> set[str]:
        return {i for i in self.items if i.startswith("out-")}

    def download(self, item_id: str, dest_path: str, *, download_url: str = "",
                 expected_size: int | None = None) -> Any:
        from .graph import DownloadResult

        data = self.content[item_id]
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with open(dest_path, "wb") as handle:
            handle.write(data)
        self.downloads[item_id] = self.downloads.get(item_id, 0) + 1
        return DownloadResult(path=dest_path, bytes_written=len(data),
                              sha256=hashlib.sha256(data).hexdigest())

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        from .graph import DriveItem as GraphItem

        self._next += 1
        item_id = f"out-{self._next}"
        self.items[item_id] = GraphItem.from_api(_graph_payload(item_id, name, data, parent_id))
        self.content[item_id] = data
        self.uploads.append(name)
        return self.items[item_id]


def _graph_payload(item_id: str, name: str, data: bytes, parent_id: str = "source-folder") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "size": len(data),
        "eTag": f'"{item_id}-1"',
        "cTag": f'"c{item_id}-1"',
        "webUrl": f"https://example.invalid/{item_id}",
        "createdDateTime": "2026-08-27T12:15:00Z",
        "lastModifiedDateTime": "2026-08-27T12:15:00Z",
        "parentReference": {"id": parent_id, "driveId": "drive-1"},
        "file": {"mimeType": "audio/mp4",
                 "hashes": {"sha256Hash": hashlib.sha256(data).hexdigest()}},
    }


def _drive_item(item_id: str, name: str, data: bytes) -> DriveItem:
    return DriveItem.from_graph(_graph_payload(item_id, name, data))


if __name__ == "__main__":
    sys.exit(main())
