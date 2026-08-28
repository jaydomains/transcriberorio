"""The command line: ``once``, ``run``, ``sweep``, ``digest``, ``archive``, ``backfill``,
``selftest``, ``status``, ``routes``, ``config``.

    python3 -m transcriber run          the service
    python3 -m transcriber once         one poll and one drain, then exit
    python3 -m transcriber status       what a person actually wants to know
    python3 -m transcriber selftest     prove the deploy is sane, offline
    python3 -m transcriber routes       the watched folders, and how to change them
    python3 -m transcriber config       one setting, read or changed, without an editor

Every command that acts on recordings — ``once``, ``sweep``, ``archive``, ``backfill``,
``status`` — takes ``--route <name>`` to act on one route rather than all of them. Omitted
means all, which is what the service itself does; naming a route that does not exist is
answered with a sentence and a non-zero exit, never with an empty run that reads as success.

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
from typing import Any, Callable, Iterable, Sequence

from . import archive as archive_module
from . import audio as audio_module
from . import config_cmd, naming, outputs, plausibility, routes_cmd, sweep as sweep_module
from .config import Config, ConfigError
from .ledger import DELTA_CURSOR, Ledger, delta_cursor_name, sweep_cursor_name
from .logging_setup import configure as configure_logging
from .models import (
    AudioInfo,
    DEFAULT_ROUTE,
    DriveItem,
    Hints,
    Route,
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
    route_poll_error_mark,
    route_poll_ok_mark,
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


#: The one help string for ``--route``, so five subcommands cannot describe it five ways.
_ROUTE_HELP = (
    "act on this route only, by its short name (see `transcriber routes`). "
    "Omitted means every route."
)


def _add_route_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--route", default=None, metavar="SLUG", help=_ROUTE_HELP)


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
    _add_route_option(once)
    once.set_defaults(handler=cmd_once)

    run = sub.add_parser("run", help="the service loop")
    run.set_defaults(handler=cmd_run)

    sweep = sub.add_parser("sweep", help="re-enumerate the folder and re-queue anything unfinished")
    sweep.add_argument("--dry-run", action="store_true")
    _add_route_option(sweep)
    sweep.set_defaults(handler=cmd_sweep)

    digest = sub.add_parser("digest", help="send the morning digest now")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--day", default=None, help="the day to report on (YYYY-MM-DD)")
    digest.set_defaults(handler=cmd_digest)

    archive = sub.add_parser("archive", help="move aged, finished, output-confirmed recordings")
    archive.add_argument("--dry-run", action="store_true")
    archive.add_argument("--limit", type=int, default=None)
    archive.add_argument("--age-days", type=int, default=None)
    _add_route_option(archive)
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
    _add_route_option(backfill)
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
    _add_route_option(status)
    status.set_defaults(handler=cmd_status)

    selftest = sub.add_parser("selftest", help="prove the deploy offline, with no credential")
    selftest.add_argument("--verbose", action="store_true", help="print every check, not only failures")
    selftest.set_defaults(handler=cmd_selftest)

    routes = sub.add_parser(
        "routes",
        help="the watched folders: list, add, edit, remove, enable, disable",
        description=(routes_cmd.__doc__ or "").strip().split("\n\n")[0],
    )
    routes_cmd.add_arguments(routes)
    routes.set_defaults(handler=cmd_routes)

    settings = sub.add_parser(
        "config",
        help="read or change one setting, checked before it is written",
        description=(config_cmd.__doc__ or "").strip().split("\n\n")[0],
    )
    config_cmd.add_arguments(settings)
    settings.set_defaults(handler=cmd_config)

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


def cmd_routes(args: argparse.Namespace) -> int:
    """Manage the watched folders.

    Like ``setup``, this must NOT go through ``_config()``: it edits the very file whose
    incompleteness would make ``Config.from_env`` refuse, and a command that cannot run
    until the configuration is already correct is no use for making it correct.
    """
    return routes_cmd.run(args)


def cmd_config(args: argparse.Namespace) -> int:
    """Read or change one setting. Same reason as above for not loading the config first."""
    return config_cmd.run(args)


def cmd_once(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        worker = Worker(config, ledger, graph)
        report = worker.run_once(limit=args.limit, route=args.route)
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
        report = sweep_module.sweep(
            config, ledger, graph, dry_run=args.dry_run, route=args.route
        )
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
            config, ledger, graph, dry_run=args.dry_run, limit=args.limit,
            age_days=args.age_days, route=args.route,
        )
        print(report.render())
        return EXIT_OK if report.ok else EXIT_FAILED


def cmd_backfill(args: argparse.Namespace) -> int:
    """History, newest first, in a lane of its own.

    Enumeration is delta from a zero cursor — never ``/children``, which comes back short
    while the phone is writing. Ordering is newest first because the most recent history is
    the most likely to be asked about. Resumption needs no bookmark: the ledger already
    says which recordings are finished, so a re-run continues rather than restarts.

    Every enabled route is enumerated, each from its own backfill cursor, and each route's
    rows are recorded **as that route's** — which is what makes the transcripts come out in
    the folder that route writes to rather than the first one's. ``--route`` narrows it to
    one. A route whose folder cannot be enumerated is named and stepped over: the rest are
    still walked, and the run still exits non-zero so nobody reads it as a clean sweep.
    """
    config, ledger, graph = _service(args)
    with ledger:
        routes, problems = sweep_module.select_routes(config, args.route)
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        if not routes:
            return EXIT_CONFIG

        seen, failures = _enumerate_all(ledger, graph, routes)
        for route in routes:
            if route.name in failures:
                continue
            print(f"{sweep_module.route_display(route)}: enumerated "
                  f"{seen.get(route.name, 0)} item(s) from its source folder")
        for name, error in failures.items():
            print(f"  ERROR {name}: could not be enumerated — {error}", file=sys.stderr)
        if args.enumerate_only:
            return EXIT_FAILED if failures else EXIT_OK

        # Only the routes actually walked. A paused route's history is not this lane's
        # work, and a route that failed to enumerate has not been counted yet.
        covered = {r.name for r in routes}
        cutoff = time.time() - max(0, args.min_age_days) * 86400.0
        cutoff_iso = utc_now_iso(cutoff)
        worker = Worker(config, ledger, graph)
        worker.install_signal_handlers()

        processed = 0
        quarantined = 0
        while not worker.stopping:
            pending = _backfill_queue(ledger, cutoff_iso, covered)
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

        outstanding = _backfill_outstanding(ledger, cutoff_iso, covered)
        # "Finished" is a claim about the whole backfill, so it is only made when the whole
        # backfill is what ran: every route, none of them failing, nothing left. A run
        # narrowed to one route that finishes it has finished that route, not the history.
        whole_service = args.route is None and not failures
        if not outstanding and whole_service:
            ledger.cursor_set(BACKFILL_FINISHED, utc_now_iso())
        print(
            f"backfill: {processed} processed, {quarantined} quarantined, "
            f"{len(outstanding)} still to do (older than {args.min_age_days} day(s))"
        )
        by_route = _count_by_route(outstanding)
        if len(covered) > 1 and by_route:
            print("  still to do, by route: "
                  + ", ".join(f"{name} {count}" for name, count in sorted(by_route.items())))
        worker.release_claims()
        return EXIT_OK if (quarantined == 0 and not failures) else EXIT_FAILED


def backfill_cursor_name(route: str) -> str:
    """This lane's own delta cursor for one route.

    The one route a pre-routes ``.env`` has keeps the name the cursor has always had, so an
    installation that has already backfilled half its history does not start again from
    zero the first time it runs a version that knows about routes.
    """
    return BACKFILL_CURSOR if route == DEFAULT_ROUTE else f"{BACKFILL_CURSOR}:{route}"


def _enumerate_all(
    ledger: Ledger, graph: Any, routes: Sequence[Route]
) -> tuple[dict[str, int], dict[str, str]]:
    """Delta from a zero cursor for each route: rows and that route's cursor, page by page.

    Returns what each route saw, and what went wrong on the ones that failed. The
    invariant is per route and unchanged by being run in a loop: one ``record_page`` call
    covers exactly one route's page, so a route that throws half way leaves its own cursor
    where it was and leaves every other route's alone.
    """
    seen: dict[str, int] = {}
    failures: dict[str, str] = {}
    for route in routes:
        count = 0
        try:
            for page in graph.delta(route.source_folder_id or None, None):
                rows = [
                    DriveItem.from_graph_item(item)
                    for item in page.items
                    if str(getattr(item, "id", "") or "") and not getattr(item, "is_folder", False)
                ]
                count += len(rows)
                if page.cursor:
                    ledger.record_page(
                        rows, page.cursor,
                        route=route.name, cursor_name=backfill_cursor_name(route.name),
                    )
                else:
                    for row in rows:
                        ledger.upsert_discovered(row, route=route.name)
        except Exception as exc:  # noqa: BLE001 - one folder's failure is not the others'
            failures[route.name] = f"{type(exc).__name__}: {exc}"
        seen[route.name] = count
    return seen, failures


def _backfill_queue(ledger: Ledger, cutoff_iso: str, routes: Iterable[str]) -> list[Row]:
    """Unfinished recordings older than the cutoff, on these routes, newest first."""
    wanted = set(routes)
    rows = [
        row
        for row in claimable_now(ledger, 10_000, time.time())
        if row.route in wanted and (row.created_at or row.discovered_at or "") < cutoff_iso
    ]
    rows.sort(key=lambda r: (r.created_at or r.discovered_at or ""), reverse=True)
    return rows


def _backfill_outstanding(ledger: Ledger, cutoff_iso: str, routes: Iterable[str]) -> list[Row]:
    """Everything in this lane that has not finished — including what is between attempts.

    Counted separately from the queue on purpose: a recording waiting out its backoff is
    still unfinished, and reporting it as nothing left to do is exactly the kind of quiet
    wrong answer this service exists to remove.
    """
    wanted = set(routes)
    return [
        row
        for row in ledger.unfinished()
        if row.route in wanted and (row.created_at or row.discovered_at or "") < cutoff_iso
    ]


def _count_by_route(rows: Iterable[Row]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.route] = counts.get(row.route, 0) + 1
    return counts


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
    """Counts, failures with their reasons, and when this last worked — per route.

    Runs even when the configuration is broken — that is often exactly when somebody runs
    it — but says so loudly and exits non-zero rather than pretending the service is fine.

    The per-route table is the point of it now. "23 done, 3 failed" across the whole
    service does not say that all three failures are one folder that stopped working a week
    ago, and that sentence is the one a person needs.
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
        routes = _route_status(config, ledger, stats)
        wanted = (args.route or "").strip() or None
        if wanted and not any(r["route"] == wanted for r in routes):
            known = ", ".join(r["route"] for r in routes) or "none"
            print(f"there is no route called {wanted!r}, in the configuration or in the "
                  f"ledger. The ones there are: {known}", file=sys.stderr)
            return EXIT_FAILED
        if wanted:
            routes = [r for r in routes if r["route"] == wanted]

        counts = ledger.counts_for_day(day, wanted)
        attention = ledger.attention_for_day(day, wanted)
        marks = {
            name: ledger.cursor_get(name)
            for name in (LAST_CYCLE_OK, LAST_CYCLE_ERROR, LAST_POLL_OK,
                         "worker:last_cycle_error_detail", "digest:last_sent_day",
                         "digest:last_attempt_at", "digest:last_error",
                         BACKFILL_POSITION, BACKFILL_FINISHED,
                         "sweep:last_error", "sweep:last_report_at",
                         "archive:last_report_at")
        }

        if args.as_json:
            print(json.dumps(
                {"ledger": stats, "day": counts, "marks": marks, "attention": attention,
                 "routes": routes, "route": wanted,
                 "config_problems": problems.splitlines()},
                indent=1, default=str,
            ))
        else:
            _print_status(ledger_path, stats, counts, marks, day, attention,
                          routes=routes, only=wanted)

    if problems:
        return EXIT_CONFIG
    return EXIT_FAILED if (counts.get("failures") or []) else EXIT_OK


def _route_status(config: Config | None, ledger: Ledger, stats: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per route, configured or merely remembered, in configuration order.

    A route taken out of ``ROUTES`` keeps every ledger row it ever wrote, so it is listed
    here too and marked as no longer watched. Dropping it would make its recordings
    disappear from the only place a person looks for them, which is the same thing as
    losing them.
    """
    configured: list[Route] = list(sweep_module.routes_of(config)) if config is not None else []
    by_route: dict[str, dict[str, int]] = dict(stats.get("by_route") or {})

    ordered: list[str] = [r.name for r in configured]
    ordered.extend(name for name in sorted(by_route) if name not in ordered)

    known_routes = {r.name: r for r in configured}
    out: list[dict[str, Any]] = []
    for name in ordered:
        route = known_routes.get(name)
        states = by_route.get(name, {})
        total = sum(states.values())
        done = states.get(State.DONE, 0)
        quarantined = states.get(State.QUARANTINED, 0)
        silent = states.get(State.SKIPPED_EMPTY, 0)
        out.append({
            "route": name,
            "label": (route.label if route is not None else "") or "",
            "configured": route is not None,
            "enabled": bool(route.enabled) if route is not None else False,
            "source_folder_id": route.source_folder_id if route is not None else "",
            "output_folder_id": route.output_folder_id if route is not None else "",
            "archive_folder_id": route.archive_folder_id if route is not None else "",
            "known": total,
            "done": done,
            "failed": quarantined,
            "verified_silence": silent,
            "working": total - done - quarantined - silent,
            "last_success": ledger.cursor_get(route_poll_ok_mark(name)) or "",
            "last_error": ledger.cursor_get(route_poll_error_mark(name)) or "",
            "delta_cursor_set": bool(
                (stats.get("cursors", {}).get(delta_cursor_name(name)) or {}).get("value_present")
            ),
            "sweep_cursor_at": (
                stats.get("cursors", {}).get(sweep_cursor_name(name)) or {}
            ).get("updated_at") or "",
        })
    return out


def _short_id(value: str) -> str:
    """A driveItem id at a width a table can carry, still recognisable against the .env."""
    text = str(value or "")
    if not text:
        return "—"
    return text if len(text) <= 16 else text[:9] + "…" + text[-4:]


def _print_route_table(routes: Sequence[dict[str, Any]]) -> None:
    """Route, folders, known, done, failed, last success — the whole point of `status`."""
    if not routes:
        print("\n  no routes: nothing is configured and the ledger has no history")
        return

    header = ("route", "watches", "writes to", "known", "done", "failed", "last success")
    rows: list[tuple[str, ...]] = []
    for route in routes:
        name = route["route"]
        if not route["configured"]:
            name += " (gone)"
        elif not route["enabled"]:
            name += " (paused)"
        rows.append((
            name,
            _short_id(route["source_folder_id"]),
            _short_id(route["output_folder_id"]),
            str(route["known"]),
            str(route["done"]),
            str(route["failed"]),
            route["last_success"] or "never",
        ))
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    # The three counts read as numbers, so they line up as numbers.
    numeric = {3, 4, 5}

    def cell(index: int, text: str) -> str:
        return text.rjust(widths[index]) if index in numeric else text.ljust(widths[index])

    print("\n  routes")
    print("    " + "  ".join(cell(i, h) for i, h in enumerate(header)).rstrip())
    for row in rows:
        print("    " + "  ".join(cell(i, c) for i, c in enumerate(row)).rstrip())

    for route in routes:
        name = route["route"]
        if route["last_error"]:
            print(f"    ! {name} last failed to poll: {route['last_error']}")
        if route["configured"] and route["enabled"] and not route["delta_cursor_set"]:
            print(f"    ! {name} has no change-feed cursor stored — its next poll "
                  "enumerates that folder from zero")
        if route["configured"] and not route["enabled"]:
            print(f"    - {name} is paused: its folder is not being watched. Its cursor and "
                  "its history are untouched.")
        if not route["configured"]:
            print(f"    - {name} is no longer one of the configured routes. Its "
                  f"{route['known']} row(s) are kept, and nothing of it was deleted.")


def _print_status(
    ledger_path: str,
    stats: dict[str, Any],
    counts: dict[str, Any],
    marks: dict[str, Any],
    day: str,
    attention: dict[str, Any] | None = None,
    routes: Sequence[dict[str, Any]] = (),
    only: str | None = None,
) -> None:
    by_state = stats.get("by_state", {})
    print(f"transcriber — {ledger_path}")
    if only:
        print(f"  (route {only} only; the totals below are the whole ledger)")
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

    _print_route_table(routes)

    print(f"\n  {day}: {counts.get('discovered', 0)} arrived, {counts.get('done', 0)} done, "
          f"{counts.get('quarantined', 0)} quarantined, {counts.get('in_flight', 0)} unfinished"
          + (f"  (route {only})" if only else ""))

    failures = counts.get("failures") or []
    if failures:
        print(f"\n  {len(failures)} failure(s) waiting for a person:")
        for failure in failures[:25]:
            where = f"  [{failure.get('route')}]" if failure.get("route") else ""
            print(f"    {failure.get('name') or failure.get('item_id')}  "
                  f"[{failure.get('state')}]{where}")
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
    _print_mark("last successful poll of every route", marks.get(LAST_POLL_OK))
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

    swept = [r for r in routes if r.get("sweep_cursor_at")]
    if swept:
        print("  last nightly re-enumeration: "
              + ", ".join(f"{r['route']} {r['sweep_cursor_at']}" for r in swept))
    # A whole-ledger fact, so it is left out of a run narrowed to one route rather than
    # printed under a heading that says "whatsapp" while naming a recording from calls.
    oldest = stats.get("oldest_unfinished")
    if oldest and not only:
        print(f"  oldest unfinished: {oldest.get('name')} "
              f"[{oldest.get('route', DEFAULT_ROUTE)}] "
              f"(discovered {oldest.get('discovered_at')})")


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
        _selftest_routes(checks, tmp)
        _selftest_route_cursors(checks, os.path.join(tmp, "routes.sqlite3"))
        _selftest_settings(checks, tmp)
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


# -- 2a. routes: parsing, and the validation that prevents a destructive misconfiguration


def _selftest_env(work_dir: str, **extra: str) -> dict[str, str]:
    """A complete, obviously-fake environment, so route parsing is proved through the real Config.

    Everything here is deliberately not a credential. The point is to exercise the same
    ``Config.from_env`` the service starts with rather than a private parsing helper: a
    route that parses in the selftest and not at startup would be worse than no check.
    """
    env = {
        "GRAPH_TENANT_ID": "offline-tenant",
        "GRAPH_CLIENT_ID": "offline-client",
        "GRAPH_CLIENT_SECRET": "offline-not-a-secret",
        "GRAPH_USER_ID": "offline-user",
        "TRANSCRIBE_ENGINE": "openai",
        "OPENAI_API_KEY": "offline-not-a-key",
        "ANALYSIS_API_KEY": "offline-not-a-key",
        "SMTP_HOST": "localhost",
        "SMTP_USER": "offline",
        "SMTP_PASSWORD": "offline-not-a-secret",
        "SMTP_FROM": "offline@invalid",
        "SMTP_TO": "offline@invalid",
        "HEARTBEAT_URL": "https://example.invalid/heartbeat",
        "LEDGER_PATH": os.path.join(work_dir, "selftest-routes.sqlite3"),
        "WORK_DIR": os.path.join(work_dir, "work"),
    }
    env.update(extra)
    return env


def _config_problems_for(env: dict[str, str]) -> list[str]:
    try:
        Config.from_env(env)
    except ConfigError as exc:
        return list(exc.problems)
    return []


def _selftest_routes(checks: _Checks, work_dir: str) -> None:
    checks.section("routes: how they are read, and what is refused")

    # 1. The shape every installation in the field has: no ROUTES at all.
    legacy = Config.from_env(_selftest_env(
        work_dir, SOURCE_FOLDER_ID="src-1", OUTPUT_FOLDER_ID="out-1", ARCHIVE_FOLDER_ID="arc-1",
    ))
    checks.check(
        "a .env written before routes existed is exactly one route called 'default'",
        len(legacy.routes) == 1 and legacy.routes[0].name == DEFAULT_ROUTE,
        ", ".join(r.name for r in legacy.routes),
    )
    checks.check(
        "that route's folders are the three single-folder settings",
        (legacy.routes[0].source_folder_id, legacy.routes[0].output_folder_id,
         legacy.routes[0].archive_folder_id) == ("src-1", "out-1", "arc-1"),
        repr(legacy.routes[0]),
    )
    checks.check(
        "the old attribute names still read as that route's folders",
        legacy.source_folder_id == "src-1" and legacy.output_folder_id == "out-1",
        legacy.source_folder_id,
    )

    # 2. Declared routes, including the hyphen-to-underscore variable naming.
    declared = Config.from_env(_selftest_env(
        work_dir,
        ROUTES="calls,site-meetings,whatsapp",
        ROUTE_CALLS_LABEL="Phone calls",
        ROUTE_CALLS_SOURCE="calls-src", ROUTE_CALLS_OUTPUT="pool-out",
        ROUTE_CALLS_ARCHIVE="calls-arc",
        ROUTE_SITE_MEETINGS_LABEL="Site meetings",
        ROUTE_SITE_MEETINGS_SOURCE="meet-src", ROUTE_SITE_MEETINGS_OUTPUT="pool-out",
        ROUTE_WHATSAPP_LABEL="WhatsApp voice notes",
        ROUTE_WHATSAPP_SOURCE="wa-src", ROUTE_WHATSAPP_OUTPUT="wa-out",
        ROUTE_WHATSAPP_ENABLED="false",
    ))
    checks.check("ROUTES names every route, in order",
                 declared.route_names == ("calls", "site-meetings", "whatsapp"),
                 str(declared.route_names))
    checks.check("a hyphenated route reads its folders from ROUTE_SITE_MEETINGS_*",
                 (declared.route("site-meetings") or Route("")).source_folder_id == "meet-src")
    checks.check("ENABLED=false pauses a route without removing it",
                 declared.route_names == ("calls", "site-meetings", "whatsapp")
                 and tuple(r.name for r in declared.enabled_routes) == ("calls", "site-meetings"))
    checks.check("two routes may share one output folder — pooling is allowed on purpose",
                 declared.route("calls").output_folder_id
                 == declared.route("site-meetings").output_folder_id == "pool-out")
    checks.check("with ROUTES set, the legacy attributes mirror the FIRST route",
                 declared.source_folder_id == "calls-src", declared.source_folder_id)

    # 3. The feedback loop — the one misconfiguration that is silently destructive.
    loop = _config_problems_for(_selftest_env(
        work_dir,
        ROUTES="calls,site-meetings",
        ROUTE_CALLS_SOURCE="calls-src", ROUTE_CALLS_OUTPUT="meet-src",
        ROUTE_SITE_MEETINGS_SOURCE="meet-src", ROUTE_SITE_MEETINGS_OUTPUT="meet-out",
    ))
    checks.check(
        "a route writing its transcripts into a folder something watches is refused",
        any("read its own transcripts" in p for p in loop),
        "; ".join(loop) or "it was accepted",
    )
    checks.check(
        "and the refusal names both routes, so it is fixable without guessing",
        any("calls" in p and "site-meetings" in p for p in loop),
        "; ".join(loop),
    )
    own = _config_problems_for(_selftest_env(
        work_dir, ROUTES="calls",
        ROUTE_CALLS_SOURCE="calls-src", ROUTE_CALLS_OUTPUT="calls-src",
    ))
    checks.check("a route writing into the folder it watches is refused too",
                 any("read its own transcripts" in p for p in own),
                 "; ".join(own) or "it was accepted")

    # 4. Two cursors over one folder is two claims on one recording.
    shared = _config_problems_for(_selftest_env(
        work_dir, ROUTES="calls,other",
        ROUTE_CALLS_SOURCE="one-src", ROUTE_CALLS_OUTPUT="calls-out",
        ROUTE_OTHER_SOURCE="one-src", ROUTE_OTHER_OUTPUT="other-out",
    ))
    checks.check("the same source folder on two enabled routes is refused",
                 any("watch" in p and "same folder" in p for p in shared),
                 "; ".join(shared) or "it was accepted")

    # 5. Everything else that must be said rather than guessed at.
    checks.check("a route name that cannot be a cursor key or a variable name is refused",
                 any("not a usable route name" in p for p in _config_problems_for(
                     _selftest_env(work_dir, ROUTES="Site Meetings"))),
                 "it was accepted")
    checks.check("an enabled route with no output folder is refused",
                 any("nowhere to write its transcripts" in p for p in _config_problems_for(
                     _selftest_env(work_dir, ROUTES="calls", ROUTE_CALLS_SOURCE="calls-src"))),
                 "it was accepted")
    checks.check("every route switched off is refused, rather than started watching nothing",
                 any("switched off" in p for p in _config_problems_for(_selftest_env(
                     work_dir, ROUTES="calls", ROUTE_CALLS_SOURCE="s", ROUTE_CALLS_OUTPUT="o",
                     ROUTE_CALLS_ENABLED="false"))),
                 "it was accepted")
    both = Config.from_env(_selftest_env(
        work_dir, ROUTES="calls", ROUTE_CALLS_SOURCE="calls-src", ROUTE_CALLS_OUTPUT="calls-out",
        SOURCE_FOLDER_ID="stale-src",
    ))
    checks.check("ROUTES alongside the old settings is said out loud, not silently preferred",
                 any("ignored completely" in n for n in both.notices),
                 "; ".join(both.notices) or "nothing was said")


# -- 2b. one cursor per route, and no route able to move another's


def _selftest_route_cursors(checks: _Checks, path: str) -> None:
    checks.section("per-route cursors: the invariant, once per route")

    with Ledger(path) as ledger:
        calls = _drive_item("calls-1", "Call Ulrich_260827_090000.m4a", b"a" * 1024)
        meets = _drive_item("meet-1", "Beach Court walkthrough.m4a", b"b" * 2048)

        ledger.record_page([calls], "cursor-calls-1", route="calls")
        checks.check("a route's page moves that route's cursor",
                     ledger.cursor_get(delta_cursor_name("calls")) == "cursor-calls-1")
        checks.check("and moves nobody else's",
                     ledger.cursor_get(delta_cursor_name("site-meetings")) is None,
                     str(ledger.cursor_get(delta_cursor_name("site-meetings"))))
        checks.check("the recording is recorded as that route's",
                     (ledger.get("calls-1") or Row("")).route == "calls")

        ledger.record_page([meets], "cursor-meet-1", route="site-meetings")
        checks.check("a second route keeps its own cursor",
                     ledger.cursor_get(delta_cursor_name("site-meetings")) == "cursor-meet-1"
                     and ledger.cursor_get(delta_cursor_name("calls")) == "cursor-calls-1")

        # The load-bearing invariant, per route: the cursor cannot move on its own.
        checks.raises(
            "a route's delta cursor cannot be set without its rows",
            Exception,
            lambda: ledger.cursor_set(delta_cursor_name("calls"), "cursor-calls-2"),
        )
        checks.raises(
            "and a page with no cursor is refused rather than committed half way",
            Exception,
            lambda: ledger.record_page([calls], "", route="calls"),
        )
        checks.check("the refused writes left the cursor exactly where it was",
                     ledger.cursor_get(delta_cursor_name("calls")) == "cursor-calls-1")

        # One route failing must not carry another past a recording it never saw.
        broken = DriveItem(item_id="", name="nameless.m4a")
        try:
            ledger.record_page([meets, broken], "cursor-meet-2", route="site-meetings")
        except Exception:  # noqa: BLE001 - the failure is the point of the check
            pass
        checks.check("a page that could not be committed leaves its own route's cursor alone",
                     ledger.cursor_get(delta_cursor_name("site-meetings")) == "cursor-meet-1",
                     str(ledger.cursor_get(delta_cursor_name("site-meetings"))))
        checks.check("and leaves the other route's cursor alone as well",
                     ledger.cursor_get(delta_cursor_name("calls")) == "cursor-calls-1")

        checks.check("unfinished() answers per route",
                     [r.item_id for r in ledger.unfinished("calls")] == ["calls-1"]
                     and [r.item_id for r in ledger.unfinished("site-meetings")] == ["meet-1"])
        day = (ledger.get("calls-1") or Row("")).discovered_at[:10]
        checks.check("so does the day's count the digest reads",
                     ledger.counts_for_day(day, "calls")["discovered"] == 1
                     and ledger.counts_for_day(day)["discovered"] == 2,
                     str(ledger.counts_for_day(day, "calls")))
        checks.check("every route that has recorded anything is findable",
                     ledger.routes_seen() == ("calls", "site-meetings"),
                     str(ledger.routes_seen()))


# -- 2c. `config set` refuses before it writes


def _selftest_settings(checks: _Checks, work_dir: str) -> None:
    checks.section("config set: refused before anything is written")

    env = {
        "ANALYSIS_MODEL_STRONG": "claude-opus-5",
        "ANALYSIS_MODEL_CHEAP": "claude-haiku-4-5",
        "DIGEST_HOUR": "6",
    }
    checks.check("a setting this service does not read is refused",
                 "not a setting this service reads" in config_cmd.check_value("ANALISIS_KEY", "x", env))
    checks.check("a model id nobody documented is refused, with the real ones named",
                 all(model in config_cmd.check_value("ANALYSIS_MODEL_STRONG", "claude-sonnet-9", env)
                     for model in config_cmd.ANALYSIS_MODELS),
                 config_cmd.check_value("ANALYSIS_MODEL_STRONG", "claude-sonnet-9", env))
    checks.check("a documented model id is accepted",
                 config_cmd.check_value("ANALYSIS_MODEL_STRONG", "claude-opus-5", env) == "",
                 config_cmd.check_value("ANALYSIS_MODEL_STRONG", "claude-opus-5", env))
    checks.check("an hour outside 0-23 is refused",
                 "23" in config_cmd.check_value("DIGEST_HOUR", "25", env),
                 config_cmd.check_value("DIGEST_HOUR", "25", env))
    checks.check("a number where a number belongs is not",
                 config_cmd.check_value("DIGEST_HOUR", "7", env) == "")
    checks.check("an engine nobody implements is refused, with the real ones named",
                 "elevenlabs" in config_cmd.check_value("TRANSCRIBE_ENGINE", "whisper.cpp", env))
    checks.check("a route variable is answered with the command that owns it",
                 "transcriber routes" in config_cmd.check_value("ROUTE_CALLS_SOURCE", "x", env))
    checks.check("a single-folder setting is refused once routes have taken over",
                 "routes edit" in config_cmd.check_value(
                     "SOURCE_FOLDER_ID", "x", {"ROUTES": "calls"}))
    checks.check("every setting is printed by `config list` — none is unreachable",
                 not [n for n, s in config_cmd.SETTINGS.items() if s.group == "other"],
                 ", ".join(n for n, s in config_cmd.SETTINGS.items() if s.group == "other"))
    checks.check("no secret is ever shown in full",
                 all("offline-not-a-key" not in config_cmd._shown(config_cmd.SETTINGS[name],
                                                                 "offline-not-a-key")
                     for name in ("OPENAI_API_KEY", "ANALYSIS_API_KEY", "GRAPH_CLIENT_SECRET",
                                  "SMTP_PASSWORD", "SMTP_TO", "GRAPH_USER_ID")))


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
