"""The command line: ``try``, ``once``, ``run``, ``sweep``, ``digest``, ``archive``,
``backfill``, ``selftest``, ``status``, ``routes``, ``config``, ``review``, ``held``, ``gate``.

    python3 -m transcriber try FILE     one local recording, with nothing set up
    python3 -m transcriber run          the service
    python3 -m transcriber once         one poll and one drain, then exit
    python3 -m transcriber status       what a person actually wants to know
    python3 -m transcriber selftest     prove the deploy is sane, offline
    python3 -m transcriber routes       the watched folders, and how to change them
    python3 -m transcriber config       one setting, read or changed, without an editor
    python3 -m transcriber review       serve the page held passages are approved on
    python3 -m transcriber held         the same held passages, from a terminal
    python3 -m transcriber gate         which mode the gate is in, and what it has measured

``review``, ``held`` and ``gate`` are the sensitivity gate's three faces, and there are
three of them on purpose. ``review`` is the page he taps yes or no on from a phone on site,
which is the only way it will ever actually be used. ``held`` is the same list, the same
words and the same two answers from a terminal on the service host — because a web page that
is down must never be the only way to reach information that exists nowhere else but the
audio and one SQLite file. ``gate`` reports which mode the gate is in and the fraction it
has actually measured, which is the number that decides whether it may be armed at all.

None of the three can decide anything. ``held release`` and ``held refuse`` both require the
name of the person answering, both are refused if that name looks like a scheduler, and
there is no command anywhere in this file that clears the queue, expires a passage or
answers one on age. The only thing that turns a held passage into a released one is a person
saying so.

Every command that acts on recordings — ``once``, ``sweep``, ``archive``, ``backfill``,
``status`` — takes ``--route <name>`` to act on one route rather than all of them. Omitted
means all, which is what the service itself does; naming a route that does not exist is
answered with a sentence and a non-zero exit, never with an empty run that reads as success.

``selftest`` is the important one. It proves parsing, the ledger's state machine, quote
verification, the markdown output contract, the truncation detector, the split guard and
mechanical secret redaction **with no credential and no network**, the same way
``graph_pull.py --selftest`` does downstream. It exits non-zero and names what failed. Run it
on the box, after deploying, before believing anything.

``try`` is the first command anybody should run, and the only one that works before the
service is configured at all. It takes an audio file on disk, transcribes it, reads it and
prints the three files it would have published — with two keys, no Microsoft credential, no
mail server and no ledger. It publishes nothing and writes nothing except the three files,
and only into a directory you name with ``--out``. It exists because the app registration is
the one setup step that can be held up by somebody else's IT department, and there is no
reason to wait on that to find out whether the transcription is any good on your own speech.
Both keys are read from the environment rather than from arguments, so neither is kept in
your shell history.

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
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import archive as archive_module
from . import audio as audio_module
from . import config_cmd, diskbudget, digest as digest_module, naming, outputs, plausibility
from . import ratelimit, redact, routes_cmd
from . import tryout
from .engines import EngineAuthError, EngineConfigError
from . import release as release_module
from . import review_server
from . import sweep as sweep_module
from .config import ENGINE_KEY_VARS, Config, ConfigError
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
from .withheld import (
    CATEGORY_DESCRIPTION,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    Decision,
    HeldRecord,
    WithheldError,
    WithheldStore,
    normalise_mode,
)
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


class CommandProblem(RuntimeError):
    """Something a person asked for that cannot be done, said in one sentence.

    Not a :class:`~transcriber.config.ConfigError` — that one exists to report every missing
    setting at once and prints its argument as a list of problems, so a sentence handed to
    it comes back out one character per line. This is the ordinary "that reference does not
    exist" case: printed as written, and a non-zero exit.
    """

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
    except CommandProblem as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
    except (release_module.ReleaseError, WithheldError) as exc:
        # Every one of these carries a whole sentence about a held passage, written to be
        # read by the person who just typed the command. A traceback in its place would be
        # the service refusing to explain itself about the one thing it holds.
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED
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

    forget = sub.add_parser(
        "forget",
        help="remove what is held about somebody, at their request — SHOWS first, asks second",
    )
    forget.add_argument("--name", default="", metavar="TEXT",
                        help="recordings whose name contains this (a client, a job, a person)")
    forget.add_argument("--route", default="", metavar="NAME",
                        help="recordings that arrived on this route")
    forget.add_argument("--id", dest="item_id", default="", metavar="ID",
                        help="one recording, by its id")
    forget.add_argument("--from", dest="since", default="", metavar="YYYY-MM-DD",
                        help="recordings from this date onwards")
    forget.add_argument("--to", dest="until", default="", metavar="YYYY-MM-DD",
                        help="recordings up to and including this date")
    forget.add_argument("--by", default="", metavar="NAME",
                        help="the PERSON who decided this. Required to remove anything.")
    forget.add_argument("--because", default="", metavar="TEXT",
                        help="what was asked for, in a sentence. Required to remove anything.")
    forget.add_argument(
        "--really", action="store_true",
        help="actually remove them. Without this the command only shows what it would do, "
             "which is the default because this is the one thing here that re-running "
             "cannot undo.",
    )
    forget.set_defaults(handler=cmd_forget)

    status = sub.add_parser("status", help="what is known, done, failed, and when it last worked")
    status.add_argument("--json", dest="as_json", action="store_true")
    status.add_argument("--day", default=None, help="the day to count (YYYY-MM-DD, default today)")
    status.add_argument(
        "--item", default=None, metavar="ID",
        help="one recording: its state, its reasons and everything that has happened to it",
    )
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

    review = sub.add_parser(
        "review",
        help="serve the page held passages are approved on",
        description=(
            "The page the morning email links to. It shows one person their own held "
            "passages and nothing else, and an answer on it is written in that person's "
            "name. Serve it behind a proxy that terminates HTTPS, or give it a certificate."
        ),
    )
    review.add_argument("--bind", default=os.environ.get("REVIEW_BIND", "127.0.0.1"),
                        help="address to listen on (default 127.0.0.1, for a proxy in front)")
    review.add_argument("--port", type=int, default=int(os.environ.get("REVIEW_PORT", "8443") or 8443))
    review.add_argument("--cert", default=os.environ.get("REVIEW_CERTFILE", ""),
                        help="TLS certificate, if this process terminates HTTPS itself")
    review.add_argument("--key", default=os.environ.get("REVIEW_KEYFILE", ""))
    review.add_argument("--trust-forwarded", action="store_true",
                        help="take the client address from X-Forwarded-For (only behind your own proxy)")
    review.add_argument("--allow-plaintext", action="store_true",
                        help="serve without TLS on a non-loopback address; almost never right")
    review.add_argument("--link", metavar="PERSON", default="",
                        help="mint one review link for one person and exit, without serving")
    review.add_argument("--revoke", metavar="PERSON", default="",
                        help="revoke every live link one person holds and exit (a lost phone)")
    review.add_argument("--hours", type=int, default=review_server.DEFAULT_TOKEN_HOURS,
                        help="how long a minted link is good for")
    review.set_defaults(handler=cmd_review)

    held = sub.add_parser(
        "held",
        help="held passages from a terminal: list, show, release, refuse, deliver",
        description=(
            "The same held passages the review page shows, reachable when the page is not. "
            "Every answer carries the name of the person giving it, and nothing here "
            "answers anything on age, on a count or on a timer."
        ),
    )
    held_sub = held.add_subparsers(dest="held_command", required=True)

    held_list = held_sub.add_parser(
        "list", help="what is waiting: counts, sites and ages — no words"
    )
    held_list.add_argument("--as", dest="who", default="",
                           help="your own name, to list your own passages rather than the totals")
    _add_route_option(held_list)
    held_list.add_argument("--json", dest="as_json", action="store_true")

    held_show = held_sub.add_parser("show", help="one held passage in full, with its words")
    held_show.add_argument("ref", help="the six-character reference, from the page or the email")
    held_show.add_argument("--as", dest="who", required=True,
                           help="your own name; only the person the passage belongs to may read it")

    held_release = held_sub.add_parser(
        "release", help="say these words may be written down, and write them into the record"
    )
    held_release.add_argument("ref")
    held_release.add_argument("--as", dest="who", required=True, help="your own name")
    held_release.add_argument("--note", default="", help="why, kept with the answer forever")

    held_refuse = held_sub.add_parser(
        "refuse", help="say these words stay out; nothing is written and the marker stays"
    )
    held_refuse.add_argument("ref")
    held_refuse.add_argument("--as", dest="who", required=True, help="your own name")
    held_refuse.add_argument("--note", default="", help="why, kept with the answer forever")

    held_deliver = held_sub.add_parser(
        "deliver",
        help="finish any release whose words have not reached the record yet",
    )
    held_deliver.add_argument("--ref", default="", help="just this one")
    held_deliver.add_argument("--limit", type=int, default=None)
    held_deliver.add_argument("--force", action="store_true",
                              help="write the file again even if it is already recorded as written")
    held.set_defaults(handler=cmd_held)

    gate = sub.add_parser(
        "gate",
        help="which mode the sensitivity gate is in, and the fraction it has measured",
        description=(
            "It ships dark. This is the number that has to be real before it is armed: how "
            "many recordings have been read, how many carried anything, and what share of "
            "the words that came to."
        ),
    )
    gate.add_argument("--status", action="store_true",
                      help="print the mode and the measurement (the default, and the only thing it does)")
    gate.add_argument("--since", default="", help="only count from this timestamp onwards")
    gate.add_argument("--until", default="", help="only count up to this timestamp")
    gate.add_argument("--days", type=int, default=None,
                      help="only count the last N days (a shorthand for --since)")
    gate.add_argument("--json", dest="as_json", action="store_true")
    gate.set_defaults(handler=cmd_gate)

    tryout_cmd = sub.add_parser(
        "try",
        help="transcribe and read ONE local audio file — no OneDrive, no email, no ledger",
        description=(
            "Point this at a recording on disk and see what the service would make of it. "
            "It needs two keys and no Microsoft anything, and it publishes nothing. Put "
            "the keys in the environment, not on this command line, where they would be "
            "kept in your shell history."
        ),
    )
    tryout_cmd.add_argument("file", help="path to an audio file (.m4a, .mp3, .wav)")
    tryout_cmd.add_argument(
        "--engine", default=None,
        help="transcription engine: " + ", ".join(sorted(ENGINE_KEY_VARS))
        + " (default: TRANSCRIBE_ENGINE, or openai)",
    )
    tryout_cmd.add_argument(
        "--out", default=None, metavar="DIR",
        help="write the three rendered files here. The only thing this command ever writes.",
    )
    tryout_cmd.add_argument(
        "--full", action="store_true",
        help="print the whole transcript rather than its first lines",
    )
    tryout_cmd.add_argument(
        "--vocabulary", default=None, metavar="WORDS",
        help="comma-separated site and person names to hint the engine with "
             "(default: VOCABULARY, or none)",
    )
    tryout_cmd.add_argument(
        "--languages", default=None, metavar="TAGS",
        help="comma-separated language tags, best first (default: LANGUAGES, or en-ZA,af-ZA)",
    )
    tryout_cmd.add_argument(
        "--no-ffprobe", action="store_true",
        help="do not use ffprobe even when it is installed",
    )
    tryout_cmd.set_defaults(handler=cmd_try)

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


def cmd_try(args: argparse.Namespace) -> int:
    """One recording, from a local file, with nothing set up.

    Like ``setup`` and ``routes`` this must NOT go through ``_config()``: the whole point is
    that it runs before there is a configuration, and a command that refuses to start until
    the thirteen settings are in place is no use to somebody deciding whether to bother
    assembling them.
    """
    engine_name = (args.engine or os.environ.get("TRANSCRIBE_ENGINE") or "openai").strip()
    try:
        engine_key, analysis_key = tryout.keys_from_environment(engine_name)
    except tryout.TryError as exc:
        raise CommandProblem(str(exc)) from exc

    started = time.monotonic()
    try:
        result = tryout.run_one(
            args.file,
            engine_name=engine_name,
            engine_key=engine_key,
            analysis_key=analysis_key,
            languages=_csv(args.languages) or _csv(os.environ.get("LANGUAGES")),
            vocabulary=_csv(args.vocabulary) or _csv(os.environ.get("VOCABULARY")),
            region=(os.environ.get("AZURE_SPEECH_REGION") or "").strip(),
            use_ffprobe=not args.no_ffprobe,
        )
    except tryout.TryError as exc:
        raise CommandProblem(str(exc)) from exc
    except EngineConfigError as exc:
        raise CommandProblem(f"the {engine_name} engine could not be built: {exc}") from exc
    except EngineAuthError as exc:
        # The likeliest failure of a first run, and the one that must not come back as a
        # stack trace: it names the variable holding the key that was refused, because
        # "401" tells a person nothing about which of the two keys is wrong.
        raise CommandProblem(
            f"the {engine_name} transcription key was refused: {exc}\n"
            f"That key is read from {ENGINE_KEY_VARS.get(engine_name, '?')}. Nothing else "
            "was tried and nothing was spent."
        ) from exc

    written = tryout.write_files(result, args.out) if args.out else ()
    print(tryout.render_report(result, full_transcript=args.full, written=written))
    print(f"\nthe whole run took {time.monotonic() - started:.1f}s")
    return EXIT_OK


def _csv(value: str | None) -> tuple[str, ...]:
    """A comma-separated option to a tuple, empties dropped."""
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


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


#: How long a shutdown waits before it stops being polite to threads that are queued behind
#: the engine rate limit. A stop means "finish what is running", and a thread waiting for a
#: token has not started anything — but it is still holding the process open, and a service
#: that will not restart is a service that is down. Nothing is lost when one is released:
#: its recording is in the ledger, unclaimed within a lease, and the next run picks it up.
RATE_LIMIT_RELEASE_AFTER_S = 30.0


def cmd_run(args: argparse.Namespace) -> int:
    config, ledger, graph = _service(args)
    with ledger:
        worker = Worker(config, ledger, graph)
        _release_rate_limited_threads_on_shutdown(worker)
        return worker.run()


def _release_rate_limited_threads_on_shutdown(
    worker: Worker, after_s: float = RATE_LIMIT_RELEASE_AFTER_S
) -> threading.Thread:
    """Watch for a stop, and after the grace window let queued threads stop waiting.

    A daemon thread rather than a signal handler: the worker owns SIGTERM and SIGINT, and
    two handlers for one signal is one handler. This only reads ``worker.stopping``, which
    is the worker's own public answer to "have we been asked to stop", so nothing here can
    change when or how the worker shuts down — it can only stop the engine rate limit from
    holding a thread past the point where the process is trying to leave.
    """

    def watch() -> None:
        while not worker.stopping:
            time.sleep(0.5)
        # In-flight work keeps its slot and finishes; this is only for threads that have not
        # started a request and are queued for one.
        deadline = time.monotonic() + max(0.0, after_s)
        while time.monotonic() < deadline:
            time.sleep(0.5)
        ratelimit.request_shutdown(
            f"shutting down and the engine rate limit still had threads queued after "
            f"{after_s:.0f}s; they stop waiting and their recordings stay in the ledger"
        )

    thread = threading.Thread(target=watch, name="rate-limit-release", daemon=True)
    thread.start()
    return thread


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
    """This lane's own delta cursor for one route: ``delta:backfill:calls``.

    Every route, ``default`` included. The bare ``delta:backfill`` this lane used to keep
    for the one route a pre-routes ``.env`` has was also, letter for letter, the live delta
    cursor of a route named ``backfill`` — and nothing refused that name. The two lanes
    would have shared one token: the ``backfill`` route would have polled Graph with a
    cursor belonging to a different folder's change feed and advanced it as though its own
    recordings had been seen. Schema step 3 carries the old value across to
    ``delta:backfill:default``, so an installation half way through its history does not
    start again from zero.
    """
    return f"{BACKFILL_CURSOR}:{route}"


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


# --------------------------------------------------------------- the sensitivity gate


def _held_store(config: Config) -> WithheldStore:
    """The one held-passage database, opened where the configuration actually says.

    ``Config.held_store_path`` honours ``GATE_HELD_STORE`` and is the path startup
    validation checked; ``WithheldStore.from_config`` does not read that attribute at all.
    Opening the wrong file here would show an empty queue while passages were being held in
    another one, which is the most convincing possible way to lose something.
    """
    return WithheldStore(review_server.store_path_for(config), scrub=config.scrub)


def _same_person(one: str, other: str) -> bool:
    """Whether two ways of writing a person's name are the same person.

    Reviewers are configured as addresses and typed at a terminal as whatever the person
    calls themselves, so ``james`` and ``james@kbc.invalid`` have to match. Nothing looser:
    two different people are never the same person here, and the only relaxation is the
    domain, which is the part a person does not type.
    """
    left, right = (one or "").strip(), (other or "").strip()
    if not left or not right:
        return False
    if left.casefold() == right.casefold():
        return True
    from .review_page import display_name

    return display_name(left).casefold() == display_name(right).casefold()


def _resolve_reviewer(store: WithheldStore, who: str, decision: str = Decision.PENDING) -> str:
    """The name the store files this person's passages under, given what they typed.

    ``queue_for`` answers for one named reviewer and matches exactly, which is right — it is
    the method that returns held text. But a person at a terminal types their own name, not
    the address the route was configured with, and a queue that came back empty because of
    a domain would look exactly like a queue that is empty. So the typed name is resolved
    against the reviewers the store actually knows, and anything ambiguous is refused by
    name rather than guessed at.
    """
    typed = (who or "").strip()
    if not typed:
        return typed
    try:
        known = [
            name
            for name, count in (store.overview(decision=decision).get("by_reviewer") or {}).items()
            if count and name and name != "unassigned"
        ]
    except Exception:  # noqa: BLE001 - fall back to what was typed rather than to nothing
        return typed
    if typed in known:
        return typed
    matches = [name for name in known if _same_person(typed, name)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CommandProblem(
            f"{typed!r} could be any of {', '.join(sorted(matches))}. Say which one — the "
            f"held text of one person is never shown under another person's name."
        )
    return typed


def _one_hold(store: WithheldStore, ref: str) -> HeldRecord:
    """The single passage this reference names, or a sentence saying why it is not single."""
    found = store.by_ref(ref)
    if not found:
        raise CommandProblem(
            f"there is no held passage with the reference {str(ref).strip().upper()!r}. "
            f"References are six characters and are printed beside the marker in the "
            f"transcript, on the review page and in the morning email."
        )
    if len(found) > 1:
        detail = ", ".join(f"{r.hold_id} ({r.item_id})" for r in found)
        raise CommandProblem(
            f"two held passages carry the reference {ref.upper()!r}: {detail}. That is a "
            f"collision rather than a mistake, and both are real — answer them on the review "
            f"page, where each is listed against its own recording."
        )
    return found[0]


def _releaser(config: Config, ledger: Ledger, store: WithheldStore, graph: Any) -> Any:
    return release_module.Releaser(config, ledger, store, graph)


def cmd_review(args: argparse.Namespace) -> int:
    """Serve the page, or mint and revoke one person's link.

    The page is wired to the releaser here and nowhere else: when somebody taps yes, the
    decision is written to the held-passage store by the page and the words are written into
    the record by :class:`transcriber.release.Releaser`. If that second step fails the first
    still stands, and ``transcriber held deliver`` finishes it — which is why the wiring is
    a callback rather than something the page has to succeed at.
    """
    config, ledger, graph = _service(args)
    with ledger:
        store = _held_store(config)
        service = review_server.service_from_config(
            config, store=store, on_decision=_releaser(config, ledger, store, graph).on_decision
        )

        if args.link and args.revoke:
            print("--link and --revoke do different things; ask for one of them.", file=sys.stderr)
            return EXIT_CONFIG
        if args.link:
            issued = service.tokens.issue(args.link, hours=args.hours)
            base = str(getattr(config, "gate_review_base_url", "") or "")
            from . import logging_setup

            logging_setup.add_secrets([issued.token])
            print(issued.url(base))
            print(f"good until {issued.expires_at}. Every earlier link for that person is now dead.")
            return EXIT_OK
        if args.revoke:
            killed = service.tokens.revoke_for(args.revoke, why="revoked from the command line")
            print(f"{killed} live link(s) revoked.")
            print("Nothing else changed. Nothing was released and nothing was discarded.")
            return EXIT_OK

        mode = normalise_mode(getattr(config, "gate_mode", MODE_SHADOW))
        if mode != MODE_ON:
            print(
                f"The gate is {_gate_mode_words(mode)}, so nothing is being withheld and the "
                f"page will say so. It is still worth serving: it shows what the gate would "
                f"have held."
            )
        pending = store.overview().get("count", 0)
        print(f"{pending} passage(s) waiting. Listening on {args.bind}:{args.port}.")
        print("Stop it with Ctrl-C; any answer still inside its undo window is written first.")
        review_server.serve(
            service,
            host=args.bind,
            port=args.port,
            certfile=args.cert,
            keyfile=args.key,
            allow_plaintext=args.allow_plaintext,
            trust_forwarded=args.trust_forwarded,
        )
    return EXIT_OK


def cmd_held(args: argparse.Namespace) -> int:
    """Held passages from a terminal, because the page can be down and these words cannot."""
    config, ledger, graph = _service(args)
    with ledger:
        store = _held_store(config)
        releaser = _releaser(config, ledger, store, graph)
        command = args.held_command
        if command == "list":
            return _held_list(config, ledger, store, args)
        if command == "show":
            return _held_show(store, args)
        if command in ("release", "refuse"):
            return _held_answer(store, releaser, args, command)
        if command == "deliver":
            return _held_deliver(releaser, args)
    print(f"{command!r} is not something `transcriber held` does.", file=sys.stderr)
    return EXIT_CONFIG


def _held_list(config: Config, ledger: Ledger, store: WithheldStore, args: argparse.Namespace) -> int:
    """What is waiting. Without ``--as`` this is counts, sites and ages and nothing else."""
    who = _resolve_reviewer(store, str(getattr(args, "who", "") or ""))
    route = getattr(args, "route", None)
    if who:
        records = store.queue_for(who, route=route or None)
        if getattr(args, "as_json", False):
            print(json.dumps([r.to_dict() for r in records], indent=2, sort_keys=True))
            return EXIT_OK
        print(f"{len(records)} passage(s) waiting for you.")
        if not records:
            print("Nothing of yours is being held.")
            return EXIT_OK
        print("")
        for record in records:
            print(f"  {record.ref}  {record.phrase}")
            print(
                f"          {record.site or 'no site named'} · "
                f"{record.source_name or record.item_id} · held {record.held_at[:10]} · "
                f"{record.age_days()} day(s) ago"
            )
        print("")
        print("Read one in full:  transcriber held show <reference> --as <your name>")
        print("Then:              transcriber held release <reference> --as <your name>")
        print("or:                transcriber held refuse  <reference> --as <your name>")
        return EXIT_OK

    overview = store.overview()
    owed = release_module.outstanding(store, ledger)
    if getattr(args, "as_json", False):
        payload = dict(overview)
        payload["outstanding"] = {
            "count": owed.count, "refs": list(owed.refs), "problems": list(owed.problems)
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return EXIT_OK

    mode = normalise_mode(getattr(config, "gate_mode", MODE_SHADOW))
    print(f"The gate is {_gate_mode_words(mode)}.")
    count = int(overview.get("count") or 0)
    if not count:
        print("Nothing is being held.")
    else:
        print(f"{count} passage(s) from {overview.get('recordings', 0)} recording(s) waiting,")
        print(f"the oldest for {overview.get('oldest_age_days', 0)} day(s).")
        print("")
        print("  by site")
        for site, n in sorted((overview.get("by_site") or {}).items(), key=lambda p: -p[1]):
            print(f"    {site}: {n}")
        print("")
        print("  whose list")
        from .review_page import display_name

        for person, n in sorted((overview.get("by_reviewer") or {}).items(), key=lambda p: -p[1]):
            print(f"    {display_name(person) or person}: {n}")
        print("")
        print("  what kind")
        for category, n in sorted((overview.get("by_category") or {}).items(), key=lambda p: -p[1]):
            print(f"    {CATEGORY_DESCRIPTION.get(category, category)}: {n}")
        print("")
        # Deliberately not the words, and deliberately not even the classifier's own summary
        # of them: this is the view somebody who does not own the passage is looking at, and
        # a staff member reviews their own. Add `--as <your name>` to see your own list.
        print("These are counts, sites and ages. To read the words of your own passages:")
        print("  transcriber held list --as <your name>")
    if owed.any:
        print("")
        for line in owed.lines():
            print(f"  {line}")
    return EXIT_OK


def _held_show(store: WithheldStore, args: argparse.Namespace) -> int:
    """One passage in full — the only command in this file that prints held words.

    It answers for the person the passage belongs to and refuses for anybody else, including
    the principal. That is decision 6: staff record voluntarily and can stop keeping a folder
    at all, and one who works out that their held words are read by somebody else has an
    obvious response, after which the recordings are gone entirely. A staff disciplinary
    matter is already routed to him by the classifier, so the passages that are genuinely
    his to hold are on his own list.
    """
    record = _one_hold(store, args.ref)
    who = str(args.who or "").strip()
    if not record.reviewer:
        # No reviewer was recorded against it — a classifier run before the routes named
        # one, or a route with nobody set. It is nobody's list, and a passage nobody can
        # reach is a passage whose words exist only in the audio, which is the loss this
        # service was built to prevent. So it is readable, and the gap is said out loud.
        print(
            f"NOTE: no reviewer is recorded against {record.ref}, so it is on nobody's "
            f"list and appears in no queue. Set ROUTE_{record.route.upper().replace('-', '_')}"
            f"_REVIEWER so the next one reaches somebody."
        )
    elif not _same_person(who, record.reviewer):
        from .review_page import display_name

        owner = display_name(record.reviewer) or "somebody else"
        print(
            f"{record.ref} is on {owner}'s list, not yours, so its words are not printed "
            f"here. What can be said about it: {CATEGORY_DESCRIPTION.get(record.category, record.category)}, "
            f"from {record.site or 'a site that was not named'}, held {record.age_days()} "
            f"day(s) ago, still waiting. Ask {owner} to answer it.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    print(f"held passage {record.ref}")
    print(f"  recording   {record.source_name or record.item_id}")
    print(f"  site        {record.site or 'not named'}")
    print(f"  route       {record.route}")
    print(f"  held        {record.held_at} ({record.age_days()} day(s) ago)")
    print(f"  kind        {CATEGORY_DESCRIPTION.get(record.category, record.category)}")
    if record.subject:
        print(f"  in short    {record.subject}")
    if record.reason:
        print(f"  why         {record.reason}")
    if record.confidence is not None:
        print(f"  confidence  {record.confidence:.2f}")
    print(f"  state       {record.decision}"
          + (f" by {record.answered_by} on {record.decided_at[:10]}" if record.decided_at else ""))
    if record.mode != MODE_ON:
        print("  NOTE        this was recorded in shadow: nothing was withheld, and there is")
        print("              nothing to release. It is here to be counted, not answered.")
    print("")
    print("  what was said, with a little either side:")
    print("")
    if record.context_before:
        print(f"    …{record.context_before.strip()}")
    print("")
    print(f"    >>> {record.text.strip()}")
    print("")
    if record.context_after:
        print(f"    {record.context_after.strip()}…")
    print("")
    if record.decision == Decision.PENDING:
        print(f"  transcriber held release {record.ref} --as {who}")
        print(f"  transcriber held refuse  {record.ref} --as {who}")
        print("")
        print("  Nothing happens to it until you run one of those. It is not released on a")
        print("  deadline, it is not discarded, and it does not expire.")
    return EXIT_OK


def _held_answer(store: WithheldStore, releaser: Any, args: argparse.Namespace, answer: str) -> int:
    """Release or refuse one passage, in the name of the person who said so.

    A release then writes the words into the record as their own document; a refusal writes
    nothing at all and leaves the marker in the transcript, because the record's rule is that
    absence is itself a record and a refused passage that had vanished would read exactly
    like one nobody ever noticed.
    """
    record = _one_hold(store, args.ref)
    who = str(args.who or "").strip()
    if record.reviewer and not _same_person(who, record.reviewer):
        from .review_page import display_name

        print(
            f"{record.ref} is on {display_name(record.reviewer) or 'somebody else'}'s list. "
            f"A held passage is answered by the person it belongs to and by nobody else.",
            file=sys.stderr,
        )
        return EXIT_FAILED
    try:
        answered = (
            store.release(record.hold_id, answered_by=who, note=args.note)
            if answer == "release"
            else store.refuse(record.hold_id, answered_by=who, note=args.note)
        )
    except WithheldError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAILED

    delivery = releaser.deliver_quietly(answered)
    print(delivery.line())
    if answer == "release" and not delivery.ok:
        print("")
        print("Your answer is recorded and will not be lost. The words have not reached the")
        print("record yet; run `transcriber held deliver` on the service host to finish it.")
        return EXIT_FAILED
    return EXIT_OK


def _held_deliver(releaser: Any, args: argparse.Namespace) -> int:
    """Finish every release whose words are not in the record yet. Safe to run twice."""
    if args.ref:
        deliveries = releaser.deliver_ref(args.ref, force=args.force)
    else:
        deliveries = releaser.deliver_outstanding(limit=args.limit)
    if not deliveries:
        print("Nothing is owed: every released passage is already written into the record.")
        return EXIT_OK
    failed = 0
    for delivery in deliveries:
        print(delivery.line())
        if not delivery.ok:
            failed += 1
    if failed:
        print("")
        print(f"{failed} of {len(deliveries)} could not be written. Every one of those")
        print("decisions still stands and is still recorded; nothing was lost.")
        return EXIT_FAILED
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    """Which mode the gate is in, and the fraction it has actually measured.

    ``--status`` is accepted because that is how it reads in the deployment notes, and the
    command does the same thing without it: there is nothing else for it to do, and a flag
    that could be forgotten must not be the difference between a number and silence.
    """
    config, ledger, _graph = _service(args)
    with ledger:
        store = _held_store(config)
        since = args.since
        if args.days:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
            since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        measured = store.measurement(since=since, until=args.until)
        overview = store.overview()
        stats = store.stats()
        owed = release_module.outstanding(store, ledger)
        mode = normalise_mode(getattr(config, "gate_mode", MODE_SHADOW))

        if args.as_json:
            print(json.dumps(
                {
                    "mode": mode,
                    "store": store.path,
                    "review_url": str(getattr(config, "gate_review_base_url", "") or ""),
                    "measurement": measured,
                    "pending": overview.get("count", 0),
                    "by_reviewer": overview.get("by_reviewer", {}),
                    "by_site": overview.get("by_site", {}),
                    "oldest_age_days": overview.get("oldest_age_days", 0),
                    "store_stats": stats,
                    "outstanding": {"count": owed.count, "refs": list(owed.refs)},
                },
                indent=2, sort_keys=True, default=str,
            ))
            return EXIT_OK

        print(f"GATE_MODE={mode} — {_gate_mode_sentence(mode)}")
        print(f"held passages are kept at {store.path}")
        url = str(getattr(config, "gate_review_base_url", "") or "")
        print(f"the review page is at {url}" if url else
              "GATE_REVIEW_BASE_URL is not set, so there is no page to send anybody to")
        print("")
        print("WHAT IT HAS MEASURED")
        window = "everything on record" if not (since or args.until) else f"{since or 'the start'} to {args.until or 'now'}"
        print(f"  window                        {window}")
        print(f"  recordings read               {measured['recordings_classified']}")
        print(f"  of those, carrying something  {measured['recordings_with_a_hold']}"
              f"  ({measured['fraction_of_recordings'] * 100:.1f}%)")
        print(f"  passages found                {measured['spans']}")
        print(f"  per day                       {measured['spans_per_day']:.2f}")
        print(f"  share of the words            {measured['fraction_of_text'] * 100:.4f}%")
        print(f"  days measured                 {measured['days_measured']}")
        if measured.get("by_category"):
            # Counted from the passages themselves rather than from the classifier's own
            # per-recording note, so the two disagreeing means passes were not recorded —
            # which is worth seeing rather than smoothing over.
            print("")
            print("  the passages, by kind")
            for name, count in sorted(measured["by_category"].items(), key=lambda p: -p[1]):
                print(f"    {CATEGORY_DESCRIPTION.get(name, name)}: {count}")
        print("")
        print("THE QUEUE")
        print(f"  waiting for a person          {overview.get('count', 0)}")
        print(f"  oldest, in days               {overview.get('oldest_age_days', 0)}")
        by_decision = dict(stats.get("by_decision") or {})
        print(f"  released, all time            {by_decision.get(Decision.RELEASED, 0)}")
        print(f"  refused, all time             {by_decision.get(Decision.REFUSED, 0)}")
        print(f"  recorded in shadow            {by_decision.get(Decision.NOT_WITHHELD, 0)}")
        if owed.any:
            print("")
            for line in owed.lines():
                print(f"  {line}")
        print("")
        for line in _gate_readiness(mode, measured):
            print(line)
    return EXIT_OK


def _gate_mode_words(mode: str) -> str:
    """The mode as it is said out loud: "on", "off", "in shadow"."""
    return "in shadow" if mode == MODE_SHADOW else mode


def _gate_mode_sentence(mode: str) -> str:
    if mode == MODE_OFF:
        return "nothing is read for sensitive passages and nothing is held"
    if mode == MODE_SHADOW:
        return (
            "every recording is read and what it would have held is written down, and "
            "NOTHING is withheld"
        )
    return "passages are actually withheld until a person answers them"


def _gate_readiness(mode: str, measured: Mapping[str, Any]) -> list[str]:
    """Whether the number is real enough to arm the gate on. A reading, never a decision.

    The whole reason it ships dark is that the design passes disagreed about how much this
    touches by a factor of twenty-five. So the only thing worth saying here is what the
    measurement is, how much of it there is, and what switching it on would cost per day —
    and then a person decides. Nothing in this service switches its own mode.
    """
    if mode == MODE_ON:
        return [
            "The gate is armed. Every passage above was actually taken out of a transcript,",
            "is marked in place where it was said, and is waiting for a person.",
        ]
    days = int(measured.get("days_measured") or 0)
    recordings = int(measured.get("recordings_classified") or 0)
    per_day = float(measured.get("spans_per_day") or 0.0)
    if mode == MODE_OFF:
        return [
            "The gate is off, so this measurement is not growing. Set GATE_MODE=shadow to",
            "start measuring without withholding anything.",
        ]
    if recordings == 0:
        return [
            "Nothing has been read yet, so there is no measurement. Leave it in shadow until",
            "there is one: arming it against an estimate is how the queue becomes a wall.",
        ]
    out = [
        f"Switching this on today would cost about {per_day:.1f} approval(s) a day, measured",
        f"over {days} day(s) and {recordings} recording(s).",
    ]
    if days < 5 or recordings < 50:
        out += [
            "That is not yet enough of a run to trust. Leave it in shadow: the estimates this",
            "replaces differed by a factor of twenty-five, which is exactly why it ships dark.",
        ]
    else:
        out += [
            "Read that as the number of approvals a day. If it is small and the categories",
            "above look right, GATE_MODE=on is a decision a person can now take on a real",
            "number rather than an estimate.",
        ]
    return out


# --------------------------------------------------------------------------- status


def cmd_forget(args: argparse.Namespace) -> int:
    """Show what would be forgotten; remove it only when told to, twice.

    Dry by default, and that is not politeness. Everything else in this service is safe to
    re-run — a requeue, a sweep, an archive pass all converge on the same answer however
    many times they happen. This one does not. So the default prints and the removal needs
    ``--really`` plus a person's name plus a reason, and the three are checked separately so
    a half-typed command cannot half-work.
    """
    from . import erase as erase_module

    config, ledger, graph_factory = _service(args)
    with ledger:
        try:
            rows = _forget_selection(ledger, args)
        except ValueError as exc:
            print(f"REFUSED: {exc}")
            return 2
        if not rows:
            print("Nothing matches. Nothing has been removed.")
            print()
            print("  Try `transcriber status --item <id>` or widen the search. A `forget`")
            print("  that matched nothing is not the same as one that removed nothing:")
            print("  check the search before deciding it is done.")
            return 0

        asked = _forget_asked(args)
        store = _held_store(config)
        plan = erase_module.plan(ledger, rows=rows, asked=asked, held_store=store)
        _print_forget_plan(plan)

        if not args.really:
            print("NOTHING HAS BEEN REMOVED. This was a look.")
            print()
            print("  To carry it out, run the same command again with:")
            print("    --really --by \"<your name>\" --because \"<what was asked>\"")
            return 0

        who, why = (args.by or "").strip(), (args.because or "").strip()
        if not who or not why:
            print("REFUSED, and nothing has been removed.")
            print()
            print("  --really needs both --by and --because. A recording removed at nobody's")
            print("  request cannot be told apart from one lost to a bug, and in a year the")
            print("  difference is the only thing that will matter.")
            return 2

        result = erase_module.erase(ledger, plan, by=who, because=why,
                                    client=graph_factory, held_store=store)
        _print_forget_result(result)
        return 0 if result.ok else 1


def _forget_selection(ledger: Any, args: argparse.Namespace) -> list:
    """The rows a `forget` matched. Every filter narrows; none of them widens.

    Unlimited on purpose. A capped search here would remove the newest twenty of somebody's
    two hundred recordings and report that they had been forgotten.
    """
    if args.item_id:
        row = ledger.get(args.item_id)
        return [row] if row else []
    if not args.name and not args.route:
        # A bare `forget` does not mean "everything". Dates alone do not narrow either: a
        # --from in 2020 is every recording there has ever been, typed in a way that does
        # not look like it. One of --id, --name or --route, always.
        raise ValueError(
            "say WHICH recordings: --id, --name or --route. Dates on their own are not a "
            "selection, and there is deliberately no way to spell 'forget everything'."
        )
    rows = ledger.find_by_name(args.name, limit=None) if args.name else ledger.rows_in_route(args.route)
    if args.route:
        rows = [r for r in rows if getattr(r, "route", "") == args.route]
    if args.since:
        rows = [r for r in rows if str(getattr(r, "created_at", "") or
                                       getattr(r, "discovered_at", "") or "") >= args.since]
    if args.until:
        rows = [r for r in rows if str(getattr(r, "created_at", "") or
                                       getattr(r, "discovered_at", "") or "")[:10] <= args.until]
    return list(rows)


def _forget_asked(args: argparse.Namespace) -> str:
    parts = []
    if args.item_id:
        parts.append(f"the recording {args.item_id}")
    if args.name:
        parts.append(f"recordings named like {args.name!r}")
    if args.route:
        parts.append(f"on the {args.route} route")
    if args.since:
        parts.append(f"from {args.since}")
    if args.until:
        parts.append(f"to {args.until}")
    return ", ".join(parts) or "everything"


def _print_forget_plan(plan: Any) -> None:
    print(f"WHAT WOULD BE FORGOTTEN — {plan.asked}")
    print("-" * 66)
    print(f"  recordings                 {plan.recordings}")
    print(f"  files in OneDrive          {plan.files}")
    if not plan.held_counted:
        print(f"  held passages              could not be counted — assume there are some")
    elif plan.held:
        # Said plainly and not as a bare number. These are staff matters, somebody's
        # health, an admission of liability: the part of this a person should be asked
        # about twice rather than told about once.
        print(f"  HELD PASSAGES              {plan.held}   "
              "— the words go, nothing survives them")
    else:
        print(f"  held passages              none")
    print()
    for candidate in plan.candidates[:20]:
        print(f"  {candidate.recorded_at[:10]:<12} {candidate.name[:44]:<44} "
              f"{candidate.reach} file(s)")
    if plan.recordings > 20:
        print(f"  ... and {plan.recordings - 20} more")
    print()
    missing = plan.unreachable_outputs
    if missing:
        print("  THESE PUBLISHED FILES CANNOT BE REMOVED BY THIS COMMAND")
        print("  (their ids were never recorded, so there is nothing to delete them by):")
        for name in missing[:10]:
            print(f"    {name}")
        print()


def _print_forget_result(result: Any) -> None:
    print()
    print(f"FORGOTTEN — at {result.by}'s decision, {result.at[:10]}")
    print("-" * 66)
    print(f"  recordings                 {result.recordings}")
    print(f"  files deleted              {result.files_deleted}")
    print(f"  files already gone         {result.files_already_gone}")
    print(f"  held passages emptied      {result.held_forgotten}")
    if result.files_refused:
        print()
        print("  SOME FILES WOULD NOT DELETE, so those recordings were left alone and can")
        print("  be re-run. Nothing was half-removed:")
        for line in result.files_refused[:10]:
            print(f"    {line}")
    print()
    print("  WHAT THIS DID NOT REACH — somebody still has to deal with these:")
    print()
    print("    The OneDrive recycle bin. Deleting a file puts it there, where it stays")
    print("    for up to 93 days and can be restored by an administrator the whole time.")
    print("    Until that bin is emptied, the recording is not gone.")
    print()
    if result.still_in_the_record:
        print("    The site record. It ingested these and derived documents from them,")
        print("    and this service is only allowed to read it:")
        for name in result.still_in_the_record[:10]:
            print(f"      {name}")
        if len(result.still_in_the_record) > 10:
            print(f"      ... and {len(result.still_in_the_record) - 10} more")
        print()
    print("    Anything already sent. A morning email, a transcript somebody saved.")
    print()


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
        # A person fixing the cause is what makes the previous attempts irrelevant, so
        # they are cleared here and nowhere else. The sweep's own requeue keeps them.
        ledger.requeue(args.item_id, args.reason, reset_attempts=True)
        print(f"{row.name or args.item_id}: {was} -> {State.DISCOVERED}; it will be picked up "
              f"on the next poll")
        return EXIT_OK


def _status_one_item(ledger: Ledger, needle: str) -> int:
    """One recording, end to end: where it is now and everything that happened to it.

    The morning email has been telling him to run ``transcriber status --item <id>`` since
    the day it was written, and until now that printed ``unrecognized arguments: --item``.
    The email is the one surface he reads, and the remedy it names has to work — an
    instruction that errors is worse than no instruction, because it costs him the trust he
    needs to act on the next one.

    Accepts the item id the email prints, or a filename, because the same email lists these
    recordings by name a few lines above and nobody transcribes a OneDrive id by hand.
    """
    row = ledger.get(needle)
    if row is None:
        matches = ledger.find_by_name(needle)
        if len(matches) > 1:
            print(f"{len(matches)} recordings match {needle!r}. Name one of them exactly, or "
                  f"use its id:", file=sys.stderr)
            for candidate in matches[:10]:
                print(f"  {candidate.item_id}  {candidate.name}", file=sys.stderr)
            return EXIT_FAILED
        if not matches:
            print(f"no recording here is called {needle!r} and none has that id. It may be on "
                  f"another route, or older than this ledger.", file=sys.stderr)
            return EXIT_FAILED
        row = matches[0]

    print(f"{row.name or '(no name of its own)'}")
    print(f"  id           {row.item_id}")
    print(f"  route        {row.route}")
    print(f"  state        {row.state}")
    if row.created_at:
        print(f"  recorded     {row.created_at}")
    if row.discovered_at:
        print(f"  first seen   {row.discovered_at}")
    if row.updated_at:
        print(f"  last change  {row.updated_at}")
    if row.duration_s:
        print(f"  length       {int(row.duration_s // 60)}m {int(row.duration_s % 60)}s")
    if row.size:
        print(f"  size         {row.size:,} bytes")
    if row.attempts:
        print(f"  attempts     {row.attempts}")
    if row.last_error:
        print(f"  last reason  {row.last_error}")

    events = ledger.history(row.item_id)
    if not events:
        print("\n  nothing has happened to it yet beyond being discovered.")
        return EXIT_OK
    print(f"\n  what has happened to it ({len(events)}):")
    for event in events:
        moved = ""
        if event.get("to_state"):
            moved = f" {event.get('from_state') or '-'} -> {event['to_state']}"
        detail = str(event.get("detail") or "").strip()
        # One line each. The detail is already scrubbed by the ledger on the way out; it is
        # printed to a terminal a person asked for it on, never into a file or an email.
        if len(detail) > 300:
            detail = detail[:300] + "..."
        print(f"    {event.get('at','')}  {event.get('kind','')}{moved}"
              + (f"  {detail}" if detail else ""))
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
        if args.item:
            return _status_one_item(ledger, str(args.item))
        stats = ledger.stats()
        # The work in hand, before anything else is printed: a person running `status` after
        # a busy morning is asking "where are the other forty?", and the answer is a queue
        # depth, not a total. Read once and shared by the table and the block below it.
        queue = digest_module.queue_report(config, ledger, day=day)
        routes = _route_status(config, ledger, stats, queue)
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
                 "routes": routes, "route": wanted, "queue": queue.as_dict(),
                 "config_problems": problems.splitlines()},
                indent=1, default=str,
            ))
        else:
            _print_status(ledger_path, stats, counts, marks, day, attention,
                          routes=routes, only=wanted, queue=queue)

    if problems:
        return EXIT_CONFIG
    return EXIT_FAILED if (counts.get("failures") or []) else EXIT_OK


def _route_status(
    config: Config | None,
    ledger: Ledger,
    stats: dict[str, Any],
    queue: Any = None,
) -> list[dict[str, Any]]:
    """One row per route, configured or merely remembered, in configuration order.

    A route taken out of ``ROUTES`` keeps every ledger row it ever wrote, so it is listed
    here too and marked as no longer watched. Dropping it would make its recordings
    disappear from the only place a person looks for them, which is the same thing as
    losing them.
    """
    configured: list[Route] = list(sweep_module.routes_of(config)) if config is not None else []
    by_route: dict[str, dict[str, int]] = dict(stats.get("by_route") or {})
    # Recordings two routes have both claimed. Written into the event log since routes
    # existed and read by nobody, which meant a recording being transcribed into the wrong
    # folder — and eventually archived into the wrong folder — was invisible unless somebody
    # opened SQLite by hand.
    try:
        clashes = ledger.route_disagreement_counts()
    except Exception:  # noqa: BLE001 - status must still print from a sick ledger
        clashes = {}

    ordered: list[str] = [r.name for r in configured]
    ordered.extend(name for name in sorted(by_route) if name not in ordered)
    queued_by_route = {entry.name: entry for entry in getattr(queue, "routes", ())}

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
            # The same number as "working", counted from the rows rather than subtracted
            # from the states, and carrying how long the oldest of them has been waiting.
            # A backlog and a loss look identical without this.
            "queued": int(getattr(queued_by_route.get(name), "queued", 0) or 0),
            "being_worked_on": int(getattr(queued_by_route.get(name), "started", 0) or 0),
            "oldest_queued_at": str(getattr(queued_by_route.get(name), "oldest_at", "") or ""),
            "oldest_queued_age_s": round(
                float(getattr(queued_by_route.get(name), "oldest_age_s", 0.0) or 0.0), 1
            ),
            "last_success": ledger.cursor_get(route_poll_ok_mark(name)) or "",
            "last_error": ledger.cursor_get(route_poll_error_mark(name)) or "",
            "delta_cursor_set": bool(
                (stats.get("cursors", {}).get(delta_cursor_name(name)) or {}).get("value_present")
            ),
            "route_disagreements": int(clashes.get(name, 0)),
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

    header = ("route", "watches", "writes to", "known", "done", "failed", "queued",
              "clashes", "last success")
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
            str(route.get("queued", 0)),
            str(route.get("route_disagreements", 0)),
            route["last_success"] or "never",
        ))
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    # The counts read as numbers, so they line up as numbers.
    numeric = {3, 4, 5, 6, 7}

    def cell(index: int, text: str) -> str:
        return text.rjust(widths[index]) if index in numeric else text.ljust(widths[index])

    print("\n  routes")
    print("    " + "  ".join(cell(i, h) for i, h in enumerate(header)).rstrip())
    for row in rows:
        print("    " + "  ".join(cell(i, c) for i, c in enumerate(row)).rstrip())

    for route in routes:
        name = route["route"]
        if route.get("queued"):
            waiting = digest_module.human_duration(route.get("oldest_queued_age_s") or 0.0)
            being = route.get("being_worked_on") or 0
            print(f"    - {name}: {route['queued']} queued, oldest waiting {waiting}"
                  + (f", {being} being worked on now" if being else "")
                  + ". Queued is work in hand, not work lost.")
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
    # Said once, however many routes are named in it: it is one fact about a pair of
    # routes, and repeating the whole explanation per route buries the counts above it.
    clashing = [r for r in routes if r.get("route_disagreements")]
    if clashing:
        named = ", ".join(f"{r['route']} ({r['route_disagreements']})" for r in clashing)
        print(f"    ! recordings claimed by two routes at once: {named}")
        print("      Either they were moved between watched folders, or one route's folder "
              "sits inside")
        print("      another's — OneDrive reports a folder and everything under it. Until "
              "that is sorted")
        print("      out their transcripts may be going to the wrong folder, and they are "
              "held back from")
        print("      archiving. Run: transcriber routes")


def _print_status(
    ledger_path: str,
    stats: dict[str, Any],
    counts: dict[str, Any],
    marks: dict[str, Any],
    day: str,
    attention: dict[str, Any] | None = None,
    routes: Sequence[dict[str, Any]] = (),
    only: str | None = None,
    queue: Any = None,
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
    _print_queue(queue, only)

    print(f"\n  {day}: {counts.get('discovered', 0)} arrived, {counts.get('done', 0)} done, "
          f"{counts.get('quarantined', 0)} quarantined, {counts.get('in_flight', 0)} unfinished"
          + (f"  (route {only})" if only else ""))

    failures = counts.get("failures") or []
    waiting = [f for f in failures if not State.is_terminal(str(f.get("state") or ""))]
    if failures:
        print(f"\n  {len(failures)} failure(s) waiting for a person:")
        if waiting:
            # Said before the list, not after it: these are the rows a person reads as
            # "lost", and they are the queue printed above under another heading.
            print(f"    ({len(waiting)} of these have simply not finished yet — they are in "
                  f"the queue above, not lost)")
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


def _print_queue(queue: Any, only: str | None = None) -> None:
    """What is in hand, in the words that stop a backlog reading as a loss.

    "42 queued, working through them" and "42 missing" are the same forty-two recordings to
    anybody looking at a total, and they are completely different problems. This is where
    the difference gets said out loud, so a person does not have to infer it from a number
    that has been going up.
    """
    if queue is None:
        return
    if getattr(queue, "unavailable", ""):
        print(f"\n  queue: could not be counted ({queue.unavailable})")
        return

    entries = [r for r in getattr(queue, "routes", ()) if not only or r.name == only]
    total = queue.queued if not only else sum(r.queued for r in entries)
    started = queue.started if not only else sum(r.started for r in entries)
    print("\n  queue")
    # Printed before the count, because "42 queued" with no explanation reads as a service
    # that has stopped. These are the worker's own words from its last drain, and they are
    # only set when the work directory actually held something back.
    if getattr(queue, "work_dir", ""):
        print(f"    why it is moving slowly: {queue.work_dir}")
    if getattr(queue, "work_dir_refused", ""):
        print(f"    ! needs you: {queue.work_dir_refused}")
    if not total:
        print("    nothing is queued: everything that has arrived has been dealt with")
        return
    being = f" ({started} being worked on right now)" if started else ""
    print(f"    {total} queued and being worked through{being} — work in hand, not work lost.")
    print("    Every one of them has a row in this ledger and will be transcribed.")
    for entry in entries:
        if not entry.queued:
            continue
        print(f"      {entry.name:<16} {entry.queued:>5} queued, oldest waiting "
              f"{digest_module.human_duration(entry.oldest_age_s)}")
    if queue.oldest_name and not only:
        print(f"    longest in the queue: {queue.oldest_name} "
              f"[{queue.oldest_route}], first seen "
              f"{digest_module.human_duration(queue.oldest_age_s)} ago")
    if queue.previous_queued is not None and not only:
        print(f"    it was {queue.previous_queued} when the {queue.previous_day} digest "
              f"went out")
    if queue.stale and not only:
        print(f"    ! the oldest has been waiting longer than "
              f"{digest_module.human_duration(queue.stale_after_s)}")
    if queue.growing_across_days and not only:
        print("    ! it has grown every morning for three mornings running")
    if queue.short_of_throughput and not only:
        print("      Recordings are arriving faster than they are being transcribed. Nothing")
        print("      is being lost — it is getting slower. More workers, or a higher engine")
        print("      limit, is what closes that gap.")


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
        _selftest_rate_limit(checks, config)
        _selftest_queue(checks, config, os.path.join(tmp, "queue.sqlite3"))
        _selftest_audio(checks)
        _selftest_plausibility(checks)
        _selftest_quotes(checks)
        _selftest_outputs(checks)
        _selftest_redaction(checks, config, level)
        _selftest_gate(checks)
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


def _selftest_rate_limit(checks: _Checks, config: Config) -> None:
    """The engine limiter: off unless configured, and unbypassable when it is on.

    Proved on an injected clock, so this section is instant and cannot be flaky: nothing
    here sleeps, and the token bucket's arithmetic is checked by moving the clock rather
    than by waiting for it.
    """
    checks.section("the engine rate limit")

    ticks = [1000.0]
    limiter = ratelimit.RateLimiter(clock=lambda: ticks[0], name="selftest")
    checks.check("an unconfigured limiter is off", not limiter.enabled)
    checks.check("and lets everything straight through",
                 all(limiter.try_acquire() for _ in range(10)))

    limiter.configure(max_concurrent=2, max_per_minute=0)
    took = [limiter.try_acquire(want_token=False) for _ in range(3)]
    checks.check("two at a time means two at a time", took == [True, True, False], repr(took))
    limiter.release_slot()
    checks.check("and a third goes as soon as one finishes",
                 limiter.try_acquire(want_token=False))

    bucket = ratelimit.RateLimiter(max_per_minute=2, clock=lambda: ticks[0], name="selftest")
    spent = [bucket.try_acquire(want_slot=False) for _ in range(3)]
    checks.check("a minute's allowance is a minute's allowance",
                 spent == [True, True, False], repr(spent))
    ticks[0] += 30.0
    checks.check("and it refills on the clock, not on a sleep",
                 bucket.try_acquire(want_slot=False))
    ticks[0] -= 600.0
    checks.check("a clock that goes backwards hands out nothing",
                 not bucket.try_acquire(want_slot=False))

    reentrant = ratelimit.RateLimiter(max_concurrent=1, clock=lambda: ticks[0], name="selftest")
    with reentrant.slot():
        with reentrant.slot():  # the HTTP client, inside an engine that already holds a slot
            deep = reentrant.snapshot().in_flight
    checks.check("one thread cannot deadlock against itself", deep == 1, f"in flight: {deep}")
    checks.check("and the slot is handed back at the end",
                 reentrant.snapshot().in_flight == 0)

    running = ratelimit.RateLimiter(max_concurrent=2, max_per_minute=60,
                                    clock=lambda: ticks[0], name="selftest")
    with running.slot():
        running.take_token()
        ratelimit.request_shutdown("selftest: stopping mid-transcription")
        try:
            running.take_token()   # an attempt inside a request that is already in flight
            spent = True
        except ratelimit.RateLimitShutdown:
            spent = False
        finally:
            ratelimit.clear_shutdown()
    checks.check("a transcription already running spends its allowance and finishes", spent)

    stopping = ratelimit.RateLimiter(max_concurrent=1, clock=lambda: ticks[0], name="selftest")
    stopping.try_acquire(want_token=False)
    ratelimit.request_shutdown("selftest")
    try:
        checks.raises(
            "a thread waiting for a turn is released by a shutdown",
            ratelimit.RateLimitShutdown,
            lambda: stopping.slot(timeout=5.0).__enter__(),
        )
    finally:
        ratelimit.clear_shutdown()

    from .engines.base import LimitedEngine, create_engine

    engine = create_engine(config)
    checks.check("every engine is built inside the rate limit",
                 isinstance(engine, LimitedEngine), type(engine).__name__)
    checks.check("and still answers as the engine it is", engine.name == config.engine,
                 engine.name)
    limits = ratelimit.limits_from_config(config)
    checks.check("the limits are read from the configuration", limits[0] >= 1, repr(limits))


def _selftest_queue(checks: _Checks, config: Config, path: str) -> None:
    """A backlog reports as a backlog. This is the sentence the service exists to say."""
    checks.section("the queue: work in hand, never work lost")

    with Ledger(path) as ledger:
        ledger.record_page(
            [_drive_item(f"q{n}", f"Call Someone_260827_1200{n:02d}.m4a", b"0123456789")
             for n in range(3)],
            "cursor-1",
        )
        ledger.advance("q0", State.DONE)
        report = digest_module.queue_report(config, ledger)
        checks.check("what is unfinished is what is queued", report.queued == 2,
                     f"{report.queued} queued")
        checks.check("a finished recording is not in the queue",
                     all(entry.queued <= 2 for entry in report.routes))
        checks.check("the queue reads as work in hand",
                     "lost" in report.headline() and "queued" in report.headline(),
                     report.headline())
        rendered = "\n".join(report.lines())
        checks.check("and says so in the words a person reads",
                     "will be transcribed" in rendered, rendered[:120])

        ledger.advance("q1", State.DONE)
        ledger.advance("q2", State.DONE)
        empty = digest_module.queue_report(config, ledger)
        checks.check("an empty queue says nothing is waiting", empty.queued == 0)
        checks.check("and is not called a failure", "lost" not in empty.headline(),
                     empty.headline())

        _selftest_work_dir(checks)

        digest_module.record_queue_depth(ledger, "2026-08-25", 4)
        digest_module.record_queue_depth(ledger, "2026-08-26", 9)
        remembered = digest_module.queue_history(ledger)
        checks.check("yesterday's depth is remembered so growth can be seen",
                     remembered.get("2026-08-26") == 9, repr(remembered))


def _selftest_work_dir(checks: _Checks) -> None:
    """The work directory's budget can always be got out of. It once could not.

    Kept scratch belongs to recordings that are finished with, and a finished recording is
    never coming back for it, so without this the work directory could cross its limit and
    stay there: nothing running, nothing claimed, and no report saying anything but "busy".
    """
    with tempfile.TemporaryDirectory(prefix="transcriber-workdir-") as tmp:
        items = os.path.join(tmp, "items")
        old_when = time.time() - 5 * 24 * 3600
        for name, aged in (("finished", True), ("still-queued", True), ("fresh", False)):
            os.makedirs(os.path.join(items, name), exist_ok=True)
            path = os.path.join(items, name, "audio.m4a")
            with open(path, "wb") as handle:
                handle.truncate(16 * diskbudget.MIB)
            if aged:
                os.utime(path, (old_when, old_when))
                os.utime(os.path.dirname(path), (old_when, old_when))

        budget = diskbudget.DiskBudget(tmp, 32 * diskbudget.MIB, ttl_s=0.0)
        cleared = budget.reclaim(keep=("still-queued",))
        checks.check("the audio of a finished recording stops holding the budget",
                     cleared.removed == ["finished"], repr(cleared.removed))
        checks.check("the audio of a recording still in the queue is kept",
                     os.path.exists(os.path.join(items, "still-queued", "audio.m4a")))
        checks.check("and so is this morning's failure, for somebody to listen to",
                     os.path.exists(os.path.join(items, "fresh", "audio.m4a")))

        candidates = [("a", 1 * diskbudget.MIB, "a.m4a"), ("b", 1 * diskbudget.MIB, "b.m4a")]
        waiting = diskbudget.admit(budget, candidates)
        checks.check("over budget, nothing new is claimed",
                     waiting.admitted == [] and len(waiting.held) == 2, repr(waiting.admitted))
        forced = diskbudget.admit(budget, candidates, force_one=True)
        checks.check("but over budget with nothing running is not a place it can be stuck",
                     forced.admitted == ["a"] and forced.forced, repr(forced.admitted))


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


# -- 7b. the gate: the redaction round trip and the shape of the marker --------------


#: One ordinary site call with exactly one thing in it that must not be written down, and
#: the traffic that has to keep flowing around it: a price, a supplier and a delivery.
#: Prices flow — that was his call, taken against his own first instinct, on the measurement
#: that 6.3% of the record's content lines carry a rand figure.
_GATE_TEXT = (
    "Right, I am at Beach Court now. Spoke to Carel about the roof leak at unit four. "
    "The new chap starts Monday, his ID number is 8001015009087 so put him on the site "
    "register. The remedial is coming in at R4,500 from Marius and the bricks land Thursday."
)
_GATE_HELD = "his ID number is 8001015009087"
_GATE_QUOTE = "The new chap starts Monday, his ID number is 8001015009087 so put him on the site register"
_GATE_PRICE = "The remedial is coming in at R4,500 from Marius"


def _selftest_gate(checks: _Checks) -> None:
    """The sensitivity gate, offline: the cut, the marker, the quote, and the fourth file.

    Everything here is the part of the gate that has no network in it, which is nearly all
    of it. The four properties proved are the four that were got wrong somewhere in the
    investigation before they were got right: the cut lands on the transcript text, the
    marker is a stated unknown the record's own harvester will carry, a redaction does not
    shred the action items that quote it, and a released passage reaches the record as a
    file the record will actually ingest.
    """
    from . import extract as extract_module
    from .withheld import HeldSpan, WithheldStore

    checks.section("the sensitivity gate, with nothing withheld and nothing on the wire")

    start = _GATE_TEXT.index(_GATE_HELD)
    span = HeldSpan(
        item_id="selftest-item",
        start=start,
        end=start + len(_GATE_HELD),
        text=_GATE_HELD,
        category="bare_identifier",
        route="calls",
        subject="an identity number",
        site="Beach Court",
        source_name="Call Carel_260827_141500.m4a",
        recorded_at="2026-08-27T12:15:00Z",
        recorded_by="selftest-person",
        reviewer="selftest-person",
    )

    # -- the marker ----------------------------------------------------------------
    marker = redact.marker_for(span)
    question = redact.harvestable(marker)
    checks.check(
        "the marker is phrased so the record's question harvester will lift it",
        bool(question),
        "a marker that only sits in the transcript is invisible: the record's read path is "
        "built from six sources and our inbox is not one of them",
    )
    checks.check("the harvested question is inside the record's own 15-240 character window",
                 15 <= len(question) <= 240, f"{len(question)} characters")
    checks.check("the marker carries exactly one question mark",
                 marker.count("?") == 1, f"{marker.count('?')} of them")
    checks.check("the marker names the passage's reference",
                 redact.refs_in(marker) == (span.ref,), str(redact.refs_in(marker)))
    checks.check("the marker says what kind of thing was held, not the words",
                 span.phrase.casefold() in marker.casefold() and _GATE_HELD not in marker,
                 marker)

    # -- the cut -------------------------------------------------------------------
    shadow = redact.redact_text(_GATE_TEXT, [span], mode="shadow")
    checks.check("shadow cuts nothing at all", shadow.text == _GATE_TEXT and shadow.cut == 0)
    checks.check("shadow still reports what it would have held", len(shadow.spans) == 1)

    cut = redact.redact_text(_GATE_TEXT, [span], mode="on")
    checks.check("the cut is applied to the transcript text", cut.ok and cut.cut == 1)
    checks.check("the held words are gone from the transcript", _GATE_HELD not in cut.text)
    checks.check("the mechanical backstop agrees they are gone",
                 not redact.contains_any_held(cut.text, [span]))
    checks.check("prices flow: the rand figure is untouched", _GATE_PRICE in cut.text)
    checks.check("the marker is where the words were", span.ref in cut.text)
    checks.check(
        "cutting the same transcript twice changes nothing",
        redact.redact_text(cut.text, [span], mode="on").text == cut.text,
    )

    # -- the masker and the backstop, which must not be able to drift apart ---------
    # The backstop refuses a file over a run of held words anywhere in a passage; the
    # masker must therefore cut anything it would refuse. When it did not, a model's
    # summary reusing the middle of a held sentence was masked by neither and refused by
    # the backstop — and that refusal is never retried, so the whole recording quarantined
    # permanently with none of its three files written. A gate is not allowed to delete
    # recordings; that is the loss this service exists to cure.
    interior = " ".join(_GATE_HELD.split()[1:])
    derived = (
        f"A note that {interior}, and the bricks land Thursday.",
        _GATE_HELD,
        f"He mentioned {_GATE_HELD} in passing.",
        _GATE_PRICE,
        "",
    )
    covered = all(
        not redact.contains_any_held(cut.mask(text)[0], cut.cut_spans) for text in derived
    )
    checks.check(
        "whatever the backstop would refuse, the masker cuts first",
        covered,
        "a passage the masker leaves and the backstop refuses quarantines the recording "
        "forever, which costs the record more than the leak it prevents",
    )
    checks.check(
        "and masking still leaves ordinary site talk alone",
        cut.mask(_GATE_PRICE)[0] == _GATE_PRICE,
        "prices flow; a masker that shreds the record is the same failure wearing a "
        "different coat",
    )
    checks.check(
        "nothing the redactor reports about a leak quotes the words it is holding",
        all(
            _GATE_HELD not in problem
            for problem in cut.check_publishable(_GATE_TEXT) + cut.problems()
        ),
        "these strings reach the ledger, the log and the morning email, and they are "
        "written exactly when the masker has a bug",
    )

    # -- the quote, which is where a redaction turns into a shredder ----------------
    before = extract_module.verify_quote(_GATE_QUOTE, _GATE_TEXT)
    checks.check("an honest quote verifies against the transcript as transcribed", before.ok,
                 before.reason)
    naive = extract_module.verify_quote(_GATE_QUOTE, cut.text)
    checks.check(
        "and would FAIL against the redacted one, which is the trap",
        not naive.ok,
        "if the item kept its original quote it would be discarded at render — a redaction "
        "that silently destroys action items",
    )
    rewritten = cut.apply_to_quote(_GATE_QUOTE)
    checks.check("the item's quote is rewritten with the same marker", span.ref in rewritten)
    checks.check("the rewritten quote carries none of the held words",
                 _GATE_HELD not in rewritten)
    checks.check("the rewritten quote keeps the words either side",
                 "The new chap starts Monday" in rewritten and "site register" in rewritten)
    after = extract_module.verify_quote(rewritten, cut.text)
    checks.check(
        "and it verifies against the published transcript, so the item survives",
        after.ok,
        after.reason,
    )

    # -- putting it back -------------------------------------------------------------
    restored, put_back = redact.restore_released(cut.text, {span.ref: span.text})
    checks.check("a released passage goes back exactly where it was said",
                 restored == _GATE_TEXT and put_back == (span.ref,))
    left = redact.restore_released(cut.text, {})[0]
    checks.check("a refused one does not: the marker stays and keeps saying so",
                 left == cut.text and span.ref in left)

    # -- the fourth file ---------------------------------------------------------------
    store = WithheldStore(":memory:")
    other_words = "Carel is off sick with something he does not want written down"
    other = HeldSpan(
        item_id="selftest-item", start=0, end=len(other_words), text=other_words,
        category="personal_circumstances", route="calls", site="Beach Court",
        source_name=span.source_name, recorded_at=span.recorded_at,
        recorded_by="selftest-person", reviewer="selftest-person",
    )
    held = store.hold(span, mode="on")
    still = store.hold(other, mode="on")
    checks.raises(
        "a machine cannot answer a held passage",
        Exception,
        lambda: store.decide(held.hold_id, "released", answered_by="scheduler"),
    )
    released = store.release(held.hold_id, answered_by="selftest-person", note="fine to write")
    rendered = release_module.render_release(
        released,
        transcript_name="20260827-141500-Call-Carel-a1b2c3d4.md",
        still_held=(still.as_span(),),
    )
    checks.check("the release file is named after the recording and the passage",
                 rendered.name.endswith(f"-released-{span.ref}.md"), rendered.name)
    checks.check("the release file is not named so the record will skip it",
                 not os.path.basename(rendered.name).startswith("_"))
    checks.check("the release file's name is one this service may write",
                 not outputs.check_name(rendered.name),
                 "; ".join(outputs.check_name(rendered.name)))
    problems = outputs.check_contract(rendered.text)
    checks.check("the record will read the release file as a transcript, not an email",
                 not problems, "; ".join(problems))
    checks.check("the release file carries the released words", _GATE_HELD in rendered.text)
    checks.check(
        "the release file does not repeat the marker's question",
        not redact.harvestable(rendered.text),
        "repeating it would put the same open question on the site's page twice, one of "
        "them already answered",
    )
    checks.check("the release file carries no other passage that is still held",
                 not redact.contains_any_held(rendered.text, [still.as_span()]))
    checks.raises(
        "a passage nobody has released has no release file",
        release_module.NotReleased,
        lambda: release_module.render_release(still, transcript_name="20260827-x.md"),
    )
    refused = store.refuse(
        store.hold(
            HeldSpan(
                item_id="selftest-other", start=0, end=len(other_words), text=other_words,
                category="personal_circumstances", route="calls",
                recorded_by="selftest-person", reviewer="selftest-person",
            ),
            mode="on",
        ).hold_id,
        answered_by="selftest-person",
    )
    checks.check("a refusal is kept as a refusal rather than deleted",
                 refused.decision == "refused" and refused.answered_by == "selftest-person")
    store.close()


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
