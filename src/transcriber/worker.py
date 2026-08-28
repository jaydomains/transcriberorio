"""The loop: poll every route's change feed, process what is claimable, run the jobs.

Four things here are worth knowing before changing any of it.

**The cursor never advances past a row.** :meth:`Worker.poll_route` hands each delta page to
``Ledger.record_page``, which writes that page's rows and that route's cursor in one
transaction. If a page comes back with no cursor at all, the rows are still recorded and the
cursor is deliberately left where it was, so the next poll re-reads a page we already have
rather than skipping one we do not. Re-reading costs a second of work; skipping loses a
recording, which is the failure this service exists to remove.

**One broken folder is not a dead service.** Every enabled route is polled in turn, each
from its own cursor, and a route that fails to poll is written down, named in the report and
stepped over — the routes after it are still polled, and the work already discovered on
every route is still processed. The difference between "WhatsApp is broken" and "the
transcriber is down" is this loop's job to make visible. The one exception is a fault in the
*service* rather than in a route — a rejected credential, a broken ledger — which stops
everything on purpose and says so.

**The concurrency limit is the whole service's, not each route's.** Discovery is per route;
the queue is one queue. Three routes must not put three times the load on Graph, so the
drain reads every route's claimable work together and runs ``CONCURRENCY`` of it at a time.

**One queue, but every route gets a turn in it.** The claimable rows are oldest first, and
oldest-first across eight people means one person uploading forty files buries the other
seven behind them for an hour. So the drain reads each route's claimable work separately
and interleaves them, a slice from each route in turn, with the route that goes first
rotating cycle by cycle. A route with nothing pending costs no turn, and nothing here is
random: the same rows in the same order come out the same way every time.

**Pressure slows the drain down; it never drops anything.** The work directory has a size
budget (``WORK_DIR_MAX_BYTES``), measured before anything is claimed. Over budget, the
worker claims nothing this cycle and says so — in the cycle line, in the log and in a mark
the digest reads. The rows stay claimable, the recordings stay in OneDrive, and the queue
starts moving again as the work in progress finishes and its scratch files are removed.
Being over budget is a normal, reportable state: never an error, never a failed attempt,
never a quarantine. The single exception that needs a person is a recording whose own
working set is larger than the entire budget — it cannot be started however long it waits,
so it is refused loudly, by name, with the variable to raise.

**Slowing down has to end somewhere.** Every drain first clears the scratch of recordings
that are finished with — done, quarantined, or written off as silence — once it is older
than the keep window, because that audio is kept for a retry that is never coming and it
was otherwise kept until somebody deleted it by hand. And if the work directory is still
over budget an hour later with nothing running at all, one recording is started anyway and
the fact is logged as something a person should look at: there is nothing left to wait for,
and a queue that only a person can restart has stopped rather than slowed.

**Shutdown is not a drop.** SIGTERM and SIGINT stop new work being submitted, wait for what
is already running, and then release every claim this worker still holds, so a redeploy
leaves the queue exactly as it found it instead of stranding files behind a lease that has
to expire. A second signal exits immediately and says so — the leases expire on their own,
which is what they are for.

**A service fault stops the service.** A bad credential or an unusable configuration raises
out of the pipeline as :class:`~transcriber.pipeline.PipelineFatal`; the loop stops, pings
the heartbeat's failure endpoint, and exits non-zero. It does not keep running and it does
not quarantine the backlog on the way down.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

from . import archive as archive_module
from . import sweep as sweep_module
from .diskbudget import (
    DEFAULT_KEEP_FINISHED_S,
    Admission,
    DiskBudget,
    Reclaimed,
    format_bytes,
)
from .diskbudget import admit as admit_to_budget
from .graph import ResyncRequired
from .heartbeat import Heartbeat
from .ledger import Ledger, delta_cursor_name
from .logging_setup import get_logger, item_context
from .models import DEFAULT_ROUTE, DriveItem, Route, Row, State, utc_now_iso
from .pipeline import _FATAL as PIPELINE_FATAL_ERRORS
from .pipeline import _NOT_THE_RECORDINGS_FAULT as PIPELINE_NOT_THE_RECORDINGS_FAULT
from .pipeline import Outcome, Pipeline, PipelineFatal

__all__ = [
    "Worker",
    "CycleReport",
    "PollResult",
    "SpaceReport",
    "DigestUnavailable",
    "run_digest",
    "digest_should_run",
    "LAST_CYCLE_OK",
    "LAST_CYCLE_ERROR",
    "LAST_POLL_OK",
    "WORK_DIR_NOTE",
    "WORK_DIR_NOTE_AT",
    "WORK_DIR_REFUSED",
    "WORK_DIR_STUCK_SINCE",
    "ROUND_ROBIN_START",
    "STUCK_AFTER_S",
    "route_poll_ok_mark",
    "route_poll_error_mark",
    "claimable_now",
    "interleave_routes",
]

log = get_logger(__name__)

#: Bookkeeping marks. ``status`` reads them to answer "when did this last work?", which is
#: the question a person actually has when they run it.
LAST_CYCLE_OK = "worker:last_cycle_ok"
LAST_CYCLE_ERROR = "worker:last_cycle_error"
LAST_POLL_OK = "worker:last_poll_ok"

#: How the work directory looked on the last drain, in the words the digest and ``status``
#: print. Written every drain, empty when there is nothing to say, because a mark that could
#: only ever be set would report a busy Tuesday for the rest of the year.
WORK_DIR_NOTE = "worker:work_dir"
WORK_DIR_NOTE_AT = "worker:work_dir_at"
#: Recordings refused because one of them alone needs more scratch space than the whole
#: budget. Cleared the moment none is refused, and a person has to raise a limit to clear it.
WORK_DIR_REFUSED = "worker:work_dir_refused"

#: When the work directory first went over budget with nothing at all running. Kept in the
#: ledger rather than in memory so that a service driven by `transcriber once` on a cron —
#: a fresh process every time — can still tell "busy" from "stopped", which is a difference
#: only visible across cycles.
WORK_DIR_STUCK_SINCE = "worker:work_dir_stuck_since"

#: Which route the interleave offers first. In the ledger for the same reason: `once
#: --limit 3` is a whole process, and a rotation that lived in one worker's memory started
#: at the first route every single run, so routes past the third were never served at all.
ROUND_ROBIN_START = "worker:round_robin_start"

#: How long the work directory may be over budget with nothing running before that stops
#: being pacing and starts being a standstill. Over budget *while recordings are running* is
#: the guard doing its job and has no time limit at all; over budget with nothing running is
#: waiting for something that is never going to happen, and after this long the drain starts
#: one recording anyway and says loudly that somebody should look at the work directory.
STUCK_AFTER_S = 3600.0

#: The fewest recordings a drain will take on however low ``CONCURRENCY`` is. The budget
#: needs a few more candidates than it can start so that it can hold some back and say so,
#: and one route's worth of work is not a cycle.
_MINIMUM_DRAIN_BATCH = 4


def route_poll_ok_mark(route: str) -> str:
    """When this route last polled cleanly. One mark per route, never one for the lot.

    A single service-wide mark said "the last poll worked" while one folder had been failing
    for a week, because some other route polled fine a minute ago. Per route, "site meetings
    last worked on Tuesday" is a sentence the status page and the digest can actually say.
    """
    return f"worker:last_poll_ok:{route}"


def route_poll_error_mark(route: str) -> str:
    """The reason this route's last poll failed, or empty once it works again.

    Cleared on success rather than only written on failure: a mark that could only ever be
    set would leave a route looking broken forever after one bad afternoon.
    """
    return f"worker:last_poll_error:{route}"


@dataclass
class PollResult:
    """One walk of the change feed — one route's, or every route's added together.

    ``per_route`` is empty on a single route's result and holds one entry per route on the
    combined one, so a caller can print the total and still name the folder that failed.
    """

    route: str = ""
    pages: int = 0
    items_seen: int = 0
    recorded: int = 0
    new: list[str] = field(default_factory=list)
    resynced: bool = False
    cursor_held_back: int = 0
    skipped_as_ours: int = 0
    error: str = ""
    per_route: list["PollResult"] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def failed_routes(self) -> tuple[str, ...]:
        """The routes that could not be polled, by name. Empty when every one worked."""
        if self.per_route:
            return tuple(r.route for r in self.per_route if not r.ok)
        return () if self.ok else (self.route,)

    @property
    def polled_routes(self) -> tuple[str, ...]:
        if self.per_route:
            return tuple(r.route for r in self.per_route)
        return (self.route,) if self.route else ()

    @classmethod
    def combine(cls, results: Sequence["PollResult"]) -> "PollResult":
        """Add up one cycle's routes, keeping each route's own result alongside the total.

        The failures are joined **named**: "whatsapp: TimeoutError ..." rather than a bare
        message, because the first question about a failed poll is which folder it was.
        """
        combined = cls(per_route=list(results))
        for one in results:
            combined.pages += one.pages
            combined.items_seen += one.items_seen
            combined.recorded += one.recorded
            combined.new.extend(one.new)
            combined.resynced = combined.resynced or one.resynced
            combined.cursor_held_back += one.cursor_held_back
            combined.skipped_as_ours += one.skipped_as_ours
        combined.error = "; ".join(
            f"{one.route}: {one.error}" if one.route else one.error
            for one in results
            if not one.ok
        )
        return combined

    def line(self) -> str:
        if len(self.per_route) > 1:
            # Several routes: the total first, then each route by name — including the ones
            # that worked, so "site meetings fine, WhatsApp broken" is one line rather than
            # an inference from a number that got smaller.
            head = f"polled {len(self.per_route)} routes, {len(self.new)} new"
            return head + " — " + "; ".join(f"{r.route}: {r.own_line()}" for r in self.per_route)
        if len(self.per_route) == 1:
            # One route — the ordinary shape of a legacy `.env` — reads exactly as it did
            # before routes existed, with no name in front of a sentence about one folder.
            return self.per_route[0].own_line()
        return self.own_line()

    def own_line(self) -> str:
        """This one walk, without the per-route roll-up."""
        if self.error:
            return f"poll failed: {self.error}"
        parts = [f"{self.pages} page(s)", f"{self.items_seen} item(s)", f"{len(self.new)} new"]
        if self.skipped_as_ours:
            # Readable rather than inferred. "the poll saw 14 items and recorded 0" is the
            # symptom of an output folder pointed at the source folder, and without this line
            # it looks identical to a quiet morning.
            parts.append(f"{self.skipped_as_ours} skipped as our own output")
        if self.resynced:
            parts.append("after a full resync")
        if self.cursor_held_back:
            parts.append(f"{self.cursor_held_back} page(s) recorded without advancing the cursor")
        return "polled " + ", ".join(parts)


@dataclass
class SpaceReport:
    """What the work directory's budget did to one drain.

    Data rather than a log line, because "the queue did not move because the disk is full"
    has to be assertable in a test and printable in the morning email, and a sentence that
    only ever went to a log file is a sentence nobody read.
    """

    #: 0 when ``WORK_DIR_MAX_BYTES`` is 0 — no limit, and nothing here was measured.
    max_bytes: int = 0
    used_bytes: int = 0
    #: True when the work directory is at or over its budget, so nothing new was claimed.
    #: Not an error and not a failure: the queue is intact and it drains as space frees up.
    over_budget: bool = False
    #: Claimable rows this drain deliberately left for the next one, for want of space.
    held: int = 0
    #: ``(item_id, reason)`` for recordings that cannot start at any time, because one of
    #: them alone needs more space than the whole budget. These need a person.
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: True when the drain was over budget with nothing running and started one recording
    #: regardless, rather than leave a queue that only a person could restart.
    forced: bool = False
    #: Bytes of scratch this drain cleared away before it measured anything: the audio of
    #: recordings that are finished with — done, quarantined, or written off as silence.
    reclaimed_bytes: int = 0
    reclaimed_items: int = 0
    #: The whole thing in one plain sentence.
    note: str = ""

    @property
    def limited(self) -> bool:
        """True when the budget changed what this drain did."""
        return bool(self.over_budget or self.held or self.refused)

    def line(self) -> str:
        parts: list[str] = []
        if self.reclaimed_items:
            parts.append(
                f"cleared {format_bytes(self.reclaimed_bytes)} of scratch from "
                f"{self.reclaimed_items} finished recording(s)"
            )
        if self.over_budget:
            parts.append(
                f"work directory full at {format_bytes(self.used_bytes)} of "
                f"{format_bytes(self.max_bytes)}, claiming nothing this cycle"
            )
        if self.forced:
            parts.append(
                "over budget with nothing running, so one recording was started anyway — "
                "the work directory needs looking at"
            )
        if self.held:
            parts.append(f"{self.held} waiting for space")
        if self.refused:
            parts.append(f"{len(self.refused)} too large for the work directory")
        return "; ".join(parts)


@dataclass
class CycleReport:
    """One pass of the loop: a poll, a drain, and whatever scheduled jobs were due."""

    started_at: str = ""
    poll: PollResult = field(default_factory=PollResult)
    outcomes: list[Outcome] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: The work directory's budget, and what it held back. Never folded into ``errors``:
    #: being over budget is the service pacing itself, not the service failing.
    space: SpaceReport = field(default_factory=SpaceReport)

    @property
    def done(self) -> int:
        return sum(1 for o in self.outcomes if o.result == "done")

    @property
    def quarantined(self) -> int:
        return sum(1 for o in self.outcomes if o.needs_a_person)

    @property
    def ok(self) -> bool:
        return self.poll.ok and not self.errors

    @property
    def failed_routes(self) -> tuple[str, ...]:
        """The routes that could not be polled this cycle, by name."""
        return self.poll.failed_routes

    def line(self) -> str:
        parts = [
            self.poll.line(),
            f"{len(self.outcomes)} processed",
            f"{self.done} done",
            f"{self.quarantined} quarantined",
        ]
        if self.failed_routes:
            # Named, every cycle. A count of errors does not tell anybody which folder to
            # go and look at, and that is the only useful thing to say about a failed poll.
            parts.append("could not poll " + ", ".join(self.failed_routes))
        if self.space.limited:
            # In the ordinary cycle line rather than only in a log of its own: a drain that
            # processed nothing because the disk is full reads exactly like a drain that had
            # nothing to do, and those two are the difference between busy and broken.
            parts.append(self.space.line())
        if self.jobs:
            parts.append("ran " + ", ".join(self.jobs))
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "; ".join(parts)


def claimable_now(
    ledger: Ledger, limit: int, clock: float, route: str | None = None
) -> list[Row]:
    """Claimable rows whose backoff has elapsed, oldest first.

    The backoff lives in the row's meta rather than in a column, so it is filtered here
    rather than in SQL: a row that failed a minute ago is claimable as far as the lease is
    concerned, and hammering it again immediately is how a throttled provider stays throttled.

    ``route`` narrows it to one route, which is what ``once --route`` wants. Left unset — the
    loop's own case — every route's work is one queue, because the concurrency limit protects
    Graph and the engine, and they do not care which folder a recording came from.
    """
    ready: list[Row] = []
    for row in ledger.claimable(limit=limit, now=clock, route=route):
        if float(row.meta.get("retry_at") or 0.0) > clock:
            continue
        ready.append(row)
    return ready


def interleave_routes(
    queues: Sequence[tuple[str, Sequence[Row]]],
    limit: int,
    *,
    slice_size: int = 1,
    start: int = 0,
) -> list[Row]:
    """One queue per route, taken a slice at a time in turn: every route makes progress.

    ``claimable_now`` hands back the oldest rows in the whole ledger, which is the right
    answer for one person and the wrong one for eight: forty files uploaded by one of them
    are all older than the seven files uploaded by everybody else, so the other seven wait
    behind the lot. Here each route's own claimable rows arrive as their own queue — still
    oldest first *within* a route, which is what fairness inside a folder means — and this
    takes ``slice_size`` from each in turn until ``limit`` rows are picked or every queue is
    empty.

    ``start`` rotates which route is offered first, so a cycle that only has room for two
    recordings does not pick the same two routes every time until the end of the week. It is
    a counter, not a shuffle: nothing here is random, and the same queues with the same
    ``start`` always produce the same list.

    A route with nothing pending costs no turn — it is skipped rather than being handed an
    empty slice, so three quiet routes do not slow the busy one down.
    """
    pending = [list(rows) for _name, rows in queues]
    if not pending or limit <= 0:
        return []
    offset = start % len(pending)
    pending = pending[offset:] + pending[:offset]
    per_turn = max(1, int(slice_size))

    taken: list[Row] = []
    while len(taken) < limit and any(pending):
        for queue in pending:
            if not queue:
                continue
            for _ in range(per_turn):
                if not queue or len(taken) >= limit:
                    break
                taken.append(queue.pop(0))
            if len(taken) >= limit:
                break
    return taken


class Worker:
    """Poll, process, schedule. One per process."""

    def __init__(
        self,
        config: Any,
        ledger: Ledger,
        graph: Any,
        *,
        pipeline: Pipeline | None = None,
        heartbeat: Heartbeat | None = None,
        clock: Callable[[], float] = time.time,
        owner: str | None = None,
        disk: DiskBudget | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.graph = graph
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        self.pipeline = pipeline or Pipeline(config, ledger, graph, owner=self.owner)
        self.heartbeat = heartbeat if heartbeat is not None else Heartbeat.from_config(config)
        self.poll_interval_s = max(1, int(getattr(config, "poll_interval_s", 120) or 120))
        #: The whole service's limit, deliberately not each route's. Three routes are three
        #: folders to watch, not three times the load on Graph and the engine.
        self.concurrency = max(1, int(getattr(config, "concurrency", 2) or 2))
        #: The first route's folders, kept under their old names for anything that still
        #: reads them. Every decision this class makes goes through ``routes`` instead.
        self.source_folder_id = str(getattr(config, "source_folder_id", "") or "") or None
        self.output_folder_id = str(getattr(config, "output_folder_id", "") or "")
        #: How much scratch the work directory may hold. Measured before anything is
        #: claimed, so a burst of hour-long recordings slows the drain down instead of
        #: filling the disk half way through a download.
        self.disk = disk if disk is not None else DiskBudget(
            str(getattr(config, "work_dir", "") or ""),
            int(getattr(config, "work_dir_max_bytes", 0) or 0),
        )
        #: How long scratch is kept for a recording that is finished with — done,
        #: quarantined, or written off as silence — before the drain clears it away. It is
        #: what stops "kept on purpose so a retry is cheap" turning into a work directory
        #: that is permanently over budget with nothing running.
        self.keep_finished_s = max(
            0.0,
            float(getattr(config, "work_dir_keep_finished_hours", 0) or 0) * 3600.0
            or DEFAULT_KEEP_FINISHED_S,
        )
        #: How many rows are taken from one route before the next route gets a turn.
        self.route_slice = 1
        #: How many recordings one drain will take on. Deliberately a small multiple of the
        #: concurrency rather than the whole queue: everything else the loop does — the
        #: delta poll, the nightly sweep, the morning email and the heartbeat ping that
        #: tells the outside world this service is alive — happens between drains, and a
        #: drain that swallowed the entire backlog held all of it up for as long as the
        #: backlog took. A cycle's work, then round again.
        self.drain_batch = max(_MINIMUM_DRAIN_BATCH, self.concurrency)
        #: Which route is offered first. Advanced by one each drain, so a cycle with room
        #: for two recordings does not serve the same two routes forever. Kept in the ledger
        #: as well as here, so the rotation survives a restart and a `once` process.
        self._round_robin_start = 0

        self._stop = threading.Event()
        self._stop_reason = ""
        self._hard_stop = False
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._last_sweep: Any = None
        self._last_archive: Any = None

    # -- routes --------------------------------------------------------------------

    @property
    def routes(self) -> tuple[Route, ...]:
        """Every route the configuration describes, paused ones included.

        Read from the config each time rather than copied at construction: the config is the
        one place a route is defined, and a worker holding a stale copy of it would poll a
        folder the person who edited the ``.env`` believes is no longer watched.
        """
        found = tuple(getattr(self.config, "routes", ()) or ())
        if found:
            return found
        # A stand-in config with no routes at all is the pre-routes shape. One route, named
        # exactly what the ledger's migration calls those rows, so nothing is orphaned.
        return (
            Route(
                name=DEFAULT_ROUTE,
                source_folder_id=str(getattr(self.config, "source_folder_id", "") or ""),
                output_folder_id=str(getattr(self.config, "output_folder_id", "") or ""),
                archive_folder_id=str(getattr(self.config, "archive_folder_id", "") or ""),
            ),
        )

    @property
    def enabled_routes(self) -> tuple[Route, ...]:
        """The routes actually watched. A paused route keeps its cursor and its history."""
        return tuple(r for r in self.routes if r.enabled)

    def _routes_to_poll(self, route: Route | str | None) -> tuple[Route, ...]:
        """Which routes this call covers: one named route, or every enabled one.

        A named route is polled whether or not it is enabled — asking for it by name is the
        deliberate act that ``--route`` is, and refusing to do what was explicitly asked for
        while saying nothing would be worse than doing it.
        """
        if route is None:
            return self.enabled_routes
        if isinstance(route, Route):
            return (route,)
        wanted = str(route).strip()
        for candidate in self.routes:
            if candidate.name == wanted:
                return (candidate,)
        raise LookupError(
            f"there is no route called {wanted!r} in this configuration — the routes it "
            f"describes are: {', '.join(r.name for r in self.routes) or '(none)'}"
        )

    # -- signals -------------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT finish what is running; a second one exits at once."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Not the main thread, or a platform without the signal. The loop still
                # stops through stop(); it just cannot be asked to by the operating system.
                log.warning("signal-handler-unavailable", f"could not install a handler for {sig}")

    def _on_signal(self, signum: int, _frame: Any) -> None:
        name = signal.Signals(signum).name
        if self._stop.is_set():
            self._hard_stop = True
            log.error(
                "shutdown-forced",
                f"a second {name} arrived; exiting now. Claims held by this worker expire "
                f"with their lease and the work is picked up by the next run.",
            )
            raise SystemExit(1)
        self.stop(f"{name} received")

    def stop(self, reason: str = "asked to stop") -> None:
        self._stop_reason = reason
        self._stop.set()
        log.info("shutdown-requested", f"{reason}; finishing in-flight work and releasing claims")

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- the loop ------------------------------------------------------------------

    def run(self) -> int:
        """Poll, process and schedule until asked to stop. Returns a process exit code."""
        self.install_signal_handlers()
        watched = self.enabled_routes
        log.info(
            "started",
            f"polling {len(watched)} route(s) every {self.poll_interval_s}s, "
            f"{self.concurrency} recording(s) at a time across all of them, a turn each in "
            f"rotation, "
            + (
                f"within {format_bytes(self.disk.max_bytes)} of scratch space: "
                if self.disk.enabled
                else "with no work-directory size limit: "
            )
            + ("; ".join(r.describe() for r in watched) or "none — every route is paused"),
            owner=self.owner, poll_interval_s=self.poll_interval_s,
            concurrency=self.concurrency, routes=",".join(r.name for r in watched),
            work_dir_max_bytes=self.disk.max_bytes,
        )
        code = 0
        try:
            while not self.stopping:
                began = self.clock()
                report = self.run_once()
                log.info("cycle", report.line(), done=report.done, quarantined=report.quarantined)
                if self.stopping:
                    break
                self._wait(self.poll_interval_s - (self.clock() - began))
        except PipelineFatal as exc:
            code = 1
            self._fatal(exc)
        except SystemExit as exc:  # the second signal
            code = int(exc.code or 0)
        finally:
            self.release_claims()
            log.info("stopped", self._stop_reason or "the loop ended", exit_code=code)
        return code

    def run_once(
        self, *, limit: int | None = None, route: Route | str | None = None
    ) -> CycleReport:
        """One cycle: poll every route, drain what is claimable, run the jobs that are due.

        ``route`` narrows the whole cycle to one route — ``once --route whatsapp``. The
        scheduled jobs still run for the service as a whole, because a nightly sweep or a
        morning digest that covered one folder would be a report nobody could trust.
        """
        report = CycleReport(started_at=utc_now_iso())
        report.poll = self.poll(route)
        if not report.poll.ok:
            # One error per failed route, each naming its route, rather than one line that
            # says "the poll failed" while three of the four folders are perfectly fine.
            for failed in report.poll.per_route or [report.poll]:
                if not failed.ok:
                    report.errors.append(
                        f"{failed.route}: {failed.error}" if failed.route else failed.error
                    )

        # Before the drain as well as after it. A drain is the one part of a cycle with no
        # upper bound on how long it takes — eight staff, a morning of hour-long calls and
        # three transcriptions at a time is most of an hour — and everything that tells the
        # outside world this service is alive runs between drains: the morning email, and
        # the heartbeat ping inside it that an external watchdog is waiting for. Run only
        # after the drain, a busy morning made a healthy service look dead and delivered the
        # email that would have said "42 queued, working through them" hours late.
        self._run_due_jobs(report)

        report.outcomes = self.drain(limit, route=route, report=report)

        self._run_due_jobs(report)

        mark = LAST_CYCLE_OK if report.ok else LAST_CYCLE_ERROR
        self.ledger.cursor_set(mark, utc_now_iso())
        # Cleared on a good cycle, not only written on a bad one. The digest reads this mark
        # and tells the external monitor the morning is not fine while it is set, so a mark
        # that could only ever be written would hold the alarm red forever after one blip —
        # and an alarm that is always red is not an alarm.
        self.ledger.cursor_set(
            "worker:last_cycle_error_detail",
            "" if report.ok else "; ".join(report.errors)[:400],
        )
        return report

    def _run_due_jobs(self, report: CycleReport) -> None:
        """Run whatever scheduled job is due, and write what happened into the report.

        Called twice a cycle. Each job is due-gated on its own mark in the ledger, so the
        second call runs nothing the first one already did — the cost of asking twice is two
        reads, and the cost of asking once is a watchdog alarm on a busy morning.
        """
        for name, ran, error in self.run_scheduled_jobs():
            if ran and name not in report.jobs:
                report.jobs.append(name)
            if error:
                report.errors.append(f"{name}: {error}")

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self._stop.wait(seconds)

    # -- polling -------------------------------------------------------------------

    def poll(self, route: Route | str | None = None) -> PollResult:
        """Poll every enabled route in turn, each from its own cursor.

        A route that fails is written down, named and stepped over: the routes after it are
        still polled, and their recordings still reach the ledger. The combined result
        carries every route's own result, so the caller can report the total and still say
        which folder is broken.
        """
        try:
            routes = self._routes_to_poll(route)
        except LookupError as exc:
            # Asked for a route that does not exist. Visible, and not fatal: the service is
            # fine, the request was not.
            failed = PollResult(route=str(route), error=str(exc))
            log.error("route-unknown", str(exc), route=str(route))
            return PollResult.combine([failed])

        if not routes:
            message = (
                "no route is enabled, so nothing is being watched — every route in this "
                "configuration is paused. Nothing has been lost: their cursors and their "
                "ledger history are untouched, and enabling one starts it where it stopped."
            )
            log.error("no-enabled-routes", message)
            return PollResult(error=message)

        own_outputs = _output_ids(self.ledger)
        results = [self.poll_route(one, own_outputs=own_outputs) for one in routes]
        combined = PollResult.combine(results)

        if combined.ok:
            # The service-wide mark means *every* route polled cleanly. Each route also has
            # its own mark, set in poll_route, so one broken folder is visible on its own.
            self.ledger.cursor_set(LAST_POLL_OK, utc_now_iso())
        log.info(
            "polled", combined.line(), pages=combined.pages, seen=combined.items_seen,
            new=len(combined.new), routes=len(results),
            failed_routes=",".join(combined.failed_routes),
        )
        return combined

    def poll_route(
        self, route: Route, *, own_outputs: frozenset[str] | None = None
    ) -> PollResult:
        """Walk one route's delta from that route's cursor, rows and cursor together.

        The invariant is unchanged and it is now per route: ``record_page`` writes this
        route's rows and this route's cursor in one transaction, so a page this route loses
        cannot move any other route's mark, and no route's mark can move past a recording
        that was not recorded.
        """
        result = PollResult(route=route.name)
        cursor_name = delta_cursor_name(route.name)
        cursor = self.ledger.cursor_get(cursor_name)
        if own_outputs is None:
            own_outputs = _output_ids(self.ledger)

        def on_resync(exc: ResyncRequired) -> None:
            result.resynced = True
            self.ledger.rewind_cursor(
                cursor_name,
                f"Microsoft Graph rejected the stored delta cursor for route "
                f"{route.name} (HTTP 410); re-enumerating that folder from zero",
            )
            log.warning("delta-resync", f"{route.display}: {exc}", route=route.name)

        try:
            for page in self.graph.delta_with_resync(
                route.source_folder_id or None, cursor, on_resync
            ):
                result.pages += 1
                rows: list[DriveItem] = []
                for item in page.items:
                    result.items_seen += 1
                    deleted = bool(getattr(item, "is_deleted", False))
                    if not deleted and self._is_ours(item, own_outputs, route):
                        # Only live items are filtered as ours. A deletion is tested first
                        # because ``classify`` calls one STRUCTURE before anything else, so
                        # dropping it here meant a recording deleted or moved out of /CALLS
                        # mid-flight never reached the ledger's source-deletion branch and
                        # never had ``source_deleted_at`` stamped on it.
                        result.skipped_as_ours += 1
                        continue
                    if getattr(item, "is_folder", False) and not deleted:
                        continue
                    if not str(getattr(item, "id", "") or ""):
                        log.error(
                            "delta-item-without-id",
                            f"Graph returned an item with no id (name {getattr(item, 'name', '')!r}) "
                            f"on route {route.name}; it cannot be tracked and has not been recorded",
                            route=route.name,
                        )
                        continue
                    rows.append(DriveItem.from_graph_item(item))

                if page.cursor:
                    new = self.ledger.record_page(rows, page.cursor, route=route.name)
                    result.recorded += len(rows)
                    result.new.extend(new)
                else:
                    # No deltaLink and no nextLink. Record the rows; leave the cursor where
                    # it is. Re-reading a page is free; advancing past one is not.
                    for row in rows:
                        if self.ledger.upsert_discovered(row, route.name):
                            result.new.append(row.item_id)
                    result.recorded += len(rows)
                    result.cursor_held_back += 1
                    log.error(
                        "delta-page-without-cursor",
                        f"a delta page on route {route.name} carried no cursor, so the rows "
                        f"were recorded and the cursor was left unchanged; the next poll "
                        f"re-reads this page",
                        route=route.name,
                    )
        except PIPELINE_FATAL_ERRORS as exc:
            # A rejected credential, an unusable configuration or a broken ledger is a fault
            # in the *service*, and it surfaces here — every 120 seconds — long before any
            # recording reaches the pipeline that knows how to classify it. Folding it into a
            # cycle error left the loop spinning on a failing poll indefinitely, never
            # pinging the heartbeat's failure endpoint, while the morning email said only
            # "nothing arrived yesterday". One list of fatal classes, not two.
            #
            # This is the one thing a route does NOT survive on its own: the credential it
            # failed on is the credential every other route uses, so carrying on would mean
            # reporting the same fault once per route, forever, and stopping for none of it.
            raise PipelineFatal(f"route {route.name}: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - one bad folder is not the whole service
            result.error = f"{type(exc).__name__}: {exc}"
            self._remember_route_poll(route, result.error)
            log.exception(
                "poll-failed",
                f"{route.display} ({route.name}) could not be polled: {result.error}. "
                f"The other routes are unaffected and this route's cursor has not moved, so "
                f"nothing in that folder has been skipped — it is re-read when it recovers.",
                route=route.name,
            )
            return result

        self._remember_route_poll(route, "")
        for item_id in result.new:
            row = self.ledger.get(item_id)
            log.info("discovered", row.name if row else item_id, item=item_id, route=route.name)
        log.info(
            "polled-route", f"{route.name}: {result.own_line()}", route=route.name,
            pages=result.pages, seen=result.items_seen, new=len(result.new),
        )
        return result

    def _remember_route_poll(self, route: Route, error: str) -> None:
        """Write down how this route's poll went, where a restart cannot lose it.

        Never raises: a mark that could not be written must not turn a working poll into a
        failed one, and the log already carries the same fact.
        """
        try:
            self.ledger.cursor_set(route_poll_error_mark(route.name), error[:400])
            if not error:
                self.ledger.cursor_set(route_poll_ok_mark(route.name), utc_now_iso())
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the poll
            log.warning("route-mark-not-recorded", f"{route.name}: {exc}", route=route.name)

    def _is_ours(
        self, item: Any, own_outputs: frozenset[str], route: Route | None = None
    ) -> bool:
        """Our own markdown, seen because an output folder is inside a watched tree.

        Every route's output folder is considered, not only the one being polled: routes may
        share an output folder, and the folder that sits inside somebody's watched tree is
        not necessarily the one whose recordings we are reading. Missing one of them means
        transcribing our own transcript.
        """
        for folder in self._output_folders(route):
            kind = sweep_module.classify(
                item, output_folder_id=folder, own_output_ids=own_outputs
            )
            if kind in (sweep_module.OUR_OUTPUT, sweep_module.STRUCTURE):
                return True
        return False

    def _output_folders(self, route: Route | None = None) -> tuple[str, ...]:
        """Every folder this service writes into, the polled route's first. Never empty."""
        ordered = [str(getattr(route, "output_folder_id", "") or "")]
        ordered.extend(str(r.output_folder_id or "") for r in self.routes)
        ordered.append(self.output_folder_id)
        return tuple(dict.fromkeys(ordered))

    # -- processing ----------------------------------------------------------------

    def drain(
        self,
        limit: int | None = None,
        *,
        route: Route | str | None = None,
        report: CycleReport | None = None,
    ) -> list[Outcome]:
        """Process what is claimable and there is room for, ``concurrency`` at a time.

        One pool across every route on purpose — each row carries the route that decides
        where its outputs go, so nothing is lost by mixing them, and a pool per route would
        multiply the load on Graph and on the engine by the number of folders he watches.
        What is *not* shared is the order: the rows come out of
        :meth:`claimable` interleaved route by route, so no one folder buries the rest.

        The work directory's budget is measured before anything is claimed. Over budget,
        this claims nothing and says so through ``report`` — the rows are untouched and
        still claimable, and the next cycle picks them up when space has come free.
        """
        wanted = route.name if isinstance(route, Route) else (str(route) if route else None)
        space = report.space if report is not None else SpaceReport()
        space.max_bytes = self.disk.max_bytes

        # Before anything is measured, and so before anything is held back for want of
        # space: the audio of recordings nothing is coming back for is not work in progress,
        # and counting it as work in progress is how a drain waits forever for a recording
        # that finished last week to finish.
        self._reclaim_finished_scratch(space)

        rows = self.claimable(limit, route=wanted)
        if self.stopping:
            if rows:
                log.info("drain-skipped",
                         f"{len(rows)} claimable, but this worker is shutting down")
            return []

        admitted = self._admit(rows, space)
        if not admitted:
            return []
        return self.process_rows(admitted, self.concurrency)

    def claimable(
        self, limit: int | None = None, *, route: str | None = None
    ) -> list[Row]:
        """The rows this cycle may work on, a slice from each route in turn.

        Every route with pending work is read separately and the queues are interleaved by
        :func:`interleave_routes`, so seven people are not held up for an hour behind one
        person's forty uploads. Routes that are no longer in the configuration are read too,
        after the enabled ones: taking a route out of ``ROUTES`` stops it being watched and
        deletes nothing, and its unfinished recordings still have to be finished.
        """
        wanted = limit or self.drain_batch
        order = self._claim_order(route)
        if not order:
            return []
        now = self.clock()
        queues = [
            (name, claimable_now(self.ledger, wanted, now, route=name)) for name in order
        ]
        start = self._rotation()
        rows = interleave_routes(
            queues, wanted, slice_size=self.route_slice, start=start
        )
        if len(order) > 1:
            self._advance_rotation(start + 1, len(order))
        return rows

    def _rotation(self) -> int:
        """Which route goes first this time, read from the ledger and not from memory.

        In memory it started at 0 in every new process. ``transcriber once --limit 3`` is a
        new process on every run, so eight routes with a cron driving three at a time served
        the first three routes and never once reached the other five — the same burial
        fairness exists to prevent, moved from inside one queue to across processes. In the
        ledger it is a property of the work rather than of the process, and a restart picks
        the rotation up where the last cycle left it.
        """
        try:
            stored = self.ledger.cursor_get(ROUND_ROBIN_START)
        except Exception as exc:  # noqa: BLE001 - a bookkeeping read is never a lost row
            log.warning("rotation-not-read", f"{exc}; this cycle rotates from memory")
            return self._round_robin_start
        try:
            value = int(str(stored).strip())
        except (TypeError, ValueError):
            value = self._round_robin_start
        self._round_robin_start = max(0, value)
        return self._round_robin_start

    def _advance_rotation(self, value: int, routes: int) -> None:
        """Write the next starting point down. Never raises: fairness is not worth a crash."""
        self._round_robin_start = value % max(1, routes)
        try:
            self.ledger.cursor_set(ROUND_ROBIN_START, str(self._round_robin_start))
        except Exception as exc:  # noqa: BLE001
            log.warning("rotation-not-recorded", str(exc))

    def _claim_order(self, route: str | None) -> tuple[str, ...]:
        """Which routes are offered work, in the order they are offered it.

        The enabled routes in the order they were configured, then every other route the
        ledger has rows for, by name. Deterministic on purpose: "route A goes before route
        B" has to be a thing a test can assert rather than a thing that happens to be true.
        """
        if route:
            return (str(route),)
        names = [r.name for r in self.enabled_routes]
        known = set(names)
        try:
            names.extend(sorted(n for n in self.ledger.routes_seen() if n and n not in known))
        except Exception as exc:  # noqa: BLE001 - a listing that failed is not a lost row
            log.warning(
                "route-list-unavailable",
                f"could not list the routes the ledger has rows for ({exc}); this cycle "
                f"works through the {len(names)} configured route(s) only",
            )
        return tuple(names)

    def _admit(self, rows: Sequence[Row], space: SpaceReport) -> list[Row]:
        """How many of these there is disk room to start, and what to say about the rest.

        Nothing here changes a ledger row. A recording that is not admitted is a recording
        this cycle did not claim: it stays exactly where it was, in the state it was in, and
        the next cycle offers it again.
        """
        candidates = [(row.item_id, int(row.size or 0), _describe(row)) for row in rows]
        stuck_for = self._stuck_for(bool(candidates))
        plan = admit_to_budget(self.disk, candidates, force_one=stuck_for >= STUCK_AFTER_S)

        if plan.usage is not None:
            space.used_bytes = plan.usage.used_bytes
        space.over_budget = bool(plan.decision is not None and not plan.decision.ok)
        space.forced = bool(plan.forced)
        space.held = len(plan.held)
        space.refused = list(plan.refused)
        space.note = self._space_note(plan)
        self._remember_space(space)

        for item_id, reason in plan.refused:
            # The one state here that waiting cannot fix, so it is said at the level that
            # gets somebody's attention rather than folded into a busy-afternoon line.
            log.error("recording-too-large-for-work-dir", reason,
                      item=item_id, needs_a_person=True)
        if space.forced or (space.over_budget and stuck_for >= STUCK_AFTER_S):
            # Over budget while recordings run is pacing. Over budget for an hour with
            # nothing running is a standstill, and a standstill that only reports itself as
            # "busy" is indistinguishable from the silent failure this service exists to
            # remove. So it is said at the level that reaches a person.
            log.error(
                "work-dir-stuck",
                f"the work directory has been over its "
                f"{format_bytes(space.max_bytes)} budget for "
                f"{stuck_for / 3600.0:.1f} hour(s) with no recording running, so nothing is "
                f"going to free it up on its own. What is in there is scratch kept for "
                f"recordings that failed or were quarantined. Nothing has been lost — every "
                f"recording is still in OneDrive and still queued — but the queue is only "
                f"moving one recording at a time until somebody clears the work directory "
                f"or raises WORK_DIR_MAX_BYTES.",
                used_bytes=space.used_bytes, max_bytes=space.max_bytes,
                waiting=space.held, needs_a_person=True,
            )
        elif space.over_budget:
            log.info("work-dir-over-budget", space.note,
                     used_bytes=space.used_bytes, max_bytes=space.max_bytes,
                     waiting=space.held)
        elif space.held:
            log.info("work-dir-waiting", space.note,
                     used_bytes=space.used_bytes, max_bytes=space.max_bytes,
                     waiting=space.held)

        by_id = {row.item_id: row for row in rows}
        return [by_id[item_id] for item_id in plan.admitted if item_id in by_id]

    def _stuck_for(self, has_work: bool) -> float:
        """How long the drain has been over budget with nothing running. 0 when it is not.

        The distinction this makes is the whole difference between the guard working and the
        guard failing: over budget *while recordings run* is the service pacing itself and
        needs no time limit at all, because the work in progress will finish and free its
        space. Over budget with nothing running is a wait for something that is never going
        to arrive.
        """
        if not self.disk.enabled or not has_work or self.in_flight or self._claimed_elsewhere():
            self._forget_stuck()
            return 0.0
        usage = self.disk.usage()
        if usage.used_bytes < self.disk.max_bytes:
            self._forget_stuck()
            return 0.0
        now = self.clock()
        try:
            since = float(self.ledger.cursor_get(WORK_DIR_STUCK_SINCE) or 0.0)
        except Exception as exc:  # noqa: BLE001 - bookkeeping, never a lost row
            log.warning("stuck-mark-not-read", str(exc))
            return 0.0
        if since <= 0.0 or since > now:
            self._remember_stuck(now)
            return 0.0
        return max(0.0, now - since)

    def _claimed_elsewhere(self) -> bool:
        """Is anything holding a live claim — this worker's or another's?

        A second worker on the same machine is work in progress even though this worker's
        own in-flight set is empty, and starting a recording on top of it would be reading
        "nothing is running" off the wrong thing.
        """
        now = self.clock()
        try:
            return any(
                row.claimed_by and (row.lease_until or 0.0) > now
                for row in self.ledger.unfinished()
            )
        except Exception as exc:  # noqa: BLE001 - unknown means "assume something is running"
            log.warning("claims-not-read", str(exc))
            return True

    def _remember_stuck(self, at: float) -> None:
        try:
            self.ledger.cursor_set(WORK_DIR_STUCK_SINCE, f"{at:.0f}")
        except Exception as exc:  # noqa: BLE001
            log.warning("stuck-mark-not-recorded", str(exc))

    def _forget_stuck(self) -> None:
        try:
            if self.ledger.cursor_get(WORK_DIR_STUCK_SINCE):
                self.ledger.cursor_set(WORK_DIR_STUCK_SINCE, "")
        except Exception as exc:  # noqa: BLE001
            log.warning("stuck-mark-not-cleared", str(exc))

    def _reclaim_finished_scratch(self, space: SpaceReport) -> Reclaimed:
        """Clear the scratch of recordings that are finished with. Never touches the rest.

        What is kept is every unfinished ledger row and everything running this second —
        their audio is what makes a retry cheap. What goes is the audio of recordings that
        are done, quarantined, written off as silence, or have no row at all, and only once
        it is older than the keep window, so this morning's quarantine is still there to be
        listened to. Without it a quarantined recording's 58 MB stayed in the budget for
        good: a quarantined row is finished, nothing ever cleans up after it, and enough of
        them put the work directory permanently over its limit with nothing running — the
        drain then claimed nothing, forever, while the queue grew and the reports said only
        that the service was busy.
        """
        result = Reclaimed()
        if not self.disk.work_dir:
            return result
        try:
            keep = {row.item_id for row in self.ledger.unfinished()}
        except Exception as exc:  # noqa: BLE001 - never clear on a guess
            log.warning(
                "scratch-not-reclaimed",
                f"could not list the unfinished recordings ({exc}), so no scratch was "
                f"cleared this cycle; nothing has been removed",
            )
            return result
        keep |= set(self.in_flight)
        try:
            result = self.disk.reclaim(keep=keep, older_than_s=self.keep_finished_s)
        except Exception as exc:  # noqa: BLE001 - housekeeping must not fail a drain
            log.warning("scratch-not-reclaimed", str(exc))
            return result

        space.reclaimed_bytes = result.freed_bytes
        space.reclaimed_items = len(result.removed)
        if result.removed:
            log.info("work-dir-cleared", result.line(),
                     freed_bytes=result.freed_bytes, items=len(result.removed))
        for problem in result.errors:
            log.warning("scratch-not-removed", problem)
        return result

    def _space_note(self, plan: Admission) -> str:
        """One plain sentence about the work directory, or '' when it did not get in the way.

        Empty on an ordinary drain on purpose. The digest and ``status`` print this mark
        whenever it is set, and a note that were always set would put "the work directory
        holds 300 MiB of its 4 GiB budget" in the morning email every day until it stopped
        being read — at which point the morning it says the directory is full would be
        read the same way.
        """
        if not self.disk.enabled:
            return ""
        return plan.held_reason or ""

    def _remember_space(self, space: SpaceReport) -> None:
        """Keep the work-directory state where ``status`` and the digest can read it.

        Written every drain and cleared when there is nothing to say, for the same reason
        the poll marks are: a mark that could only ever be set reports last Tuesday's
        problem forever.
        """
        try:
            self.ledger.cursor_set(WORK_DIR_NOTE, _trim(space.note, 1200))
            self.ledger.cursor_set(WORK_DIR_NOTE_AT, utc_now_iso())
            self.ledger.cursor_set(
                WORK_DIR_REFUSED,
                _trim("; ".join(reason for _item, reason in space.refused), 1600),
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the drain
            log.warning("work-dir-note-not-recorded", str(exc))

    def process_rows(self, rows: Sequence[Row], concurrency: int) -> list[Outcome]:
        """Run the pipeline over these rows with bounded concurrency.

        Submission stops the moment a shutdown is asked for, so a redeploy waits only for
        what is genuinely mid-flight rather than for the whole queue.
        """
        outcomes: list[Outcome] = []
        fatal: PipelineFatal | None = None
        with ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="rec") as pool:
            futures: dict[Future[Outcome], Row] = {}
            for row in rows:
                if self.stopping:
                    log.info("submission-stopped",
                             f"not starting {row.name or row.item_id}: shutting down")
                    continue
                futures[pool.submit(self._process, row)] = row
            for future, row in futures.items():
                try:
                    outcomes.append(future.result())
                except PipelineFatal as exc:
                    fatal = fatal or exc
                except Exception as exc:  # noqa: BLE001 - one bad recording is not the loop
                    log.exception("worker-error", f"{row.name or row.item_id}: {exc}",
                                  item=row.item_id)
                    outcomes.append(self._record_escaped(row, exc))
        if fatal is not None:
            raise fatal
        return outcomes

    def _record_escaped(self, row: Row, exc: BaseException) -> Outcome:
        """Give a recording whose exception escaped the pipeline the same two endings.

        ``process_one`` handles most faults itself, but not every path: an exception inside
        ``_fail``, inside ``_release_if_ours`` or inside the lease keeper lands here. Logging
        and returning "retry" wrote **nothing** to the ledger — no attempt counted, no error
        stored, no backoff stamped — so the row's lease was already clear and it came back on
        the very next 120-second cycle, failed identically, and did that forever, spending a
        concurrency slot each time and never reaching max_attempts.
        """
        reason = f"{type(exc).__name__}: {exc}"
        result = "retry"
        if isinstance(exc, PIPELINE_NOT_THE_RECORDINGS_FAULT):
            # The service stopped while this recording was queued behind the engine rate
            # limit. It was never started, so it has not failed: the claim goes back and the
            # attempt count does not move. Charging it here was three redeploys from a
            # quarantine, and a quarantine needs a person.
            reason = (
                f"the service stopped before this recording was started, so nothing has "
                f"been counted against it and it is still queued ({exc})"
            )
            log.info("interrupted", f"{row.name or row.item_id}: {reason}", item=row.item_id)
            try:
                self.ledger.release(row.item_id, reason)
            except Exception as ledger_exc:  # noqa: BLE001 - the lease expires anyway
                log.warning("release-failed", f"{row.item_id}: {ledger_exc}")
            return Outcome(item_id=row.item_id, name=row.name, result=result,
                           state=row.state, reason=reason, route=row.route,
                           attempts=row.attempts)
        try:
            attempts = self.ledger.record_attempt(row.item_id, reason, owner=self.owner)
            if attempts >= self.pipeline.max_attempts:
                why = (
                    f"{reason} (gave up after {attempts} attempt(s); this failure escaped the "
                    f"pipeline itself, so there is nothing more specific to say about it)"
                )
                self.ledger.quarantine(row.item_id, why, owner=self.owner)
                reason, result = why, "quarantined"
        except Exception as ledger_exc:  # noqa: BLE001 - reported, never recursive
            log.error(
                "attempt-not-recorded",
                f"{row.name or row.item_id}: the failure above could not be written to the "
                f"ledger either ({ledger_exc}), so this recording will be retried without a "
                f"counted attempt",
                item=row.item_id,
            )
        return Outcome(item_id=row.item_id, name=row.name, result=result,
                       state=row.state, reason=reason, route=row.route)

    def _process(self, row: Row) -> Outcome:
        with self._tracking(row.item_id), item_context(row.item_id):
            try:
                outcome = self.pipeline.process_one(row)
            finally:
                # A finished recording's scratch directory has just been removed, and a
                # failed one's has just grown. Either way the size remembered for it is
                # now a guess, and the next claim must not be made against it.
                self.disk.forget(row.item_id)
            log.info("outcome", outcome.line(), result=outcome.result,
                     elapsed_s=outcome.elapsed_s, state=outcome.state)
            return outcome

    @contextmanager
    def _tracking(self, item_id: str) -> Iterator[None]:
        """What is genuinely mid-flight, so a shutdown knows what it is waiting for."""
        with self._in_flight_lock:
            self._in_flight.add(item_id)
        try:
            yield
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(item_id)

    @property
    def in_flight(self) -> frozenset[str]:
        with self._in_flight_lock:
            return frozenset(self._in_flight)

    # -- scheduled jobs ------------------------------------------------------------

    def run_scheduled_jobs(self, *, force: Iterable[str] = ()) -> list[tuple[str, bool, str]]:
        """The nightly sweep, the 06:00 digest and the monthly archive, each when it is due.

        Each is anchored on a mark in the ledger rather than on a timer, so a restart neither
        skips a night nor runs it twice, and each reports its own failure rather than taking
        the loop down with it.
        """
        forced = set(force)
        results: list[tuple[str, bool, str]] = []
        for name, due, run in (
            ("sweep", sweep_module.should_run, self._run_sweep),
            ("digest", digest_should_run, self._run_digest),
            ("archive", archive_module.should_run, self._run_archive),
        ):
            try:
                if name not in forced and not due(self.config, self.ledger):
                    results.append((name, False, ""))
                    continue
            except Exception as exc:  # noqa: BLE001
                results.append((name, False, f"could not tell whether it is due: {exc}"))
                log.exception("schedule-check-failed", f"{name}: {exc}")
                continue
            try:
                run()
                results.append((name, True, ""))
            except PipelineFatal:
                raise
            except PIPELINE_FATAL_ERRORS as exc:
                # Same rule as the poll: a credential or a broken ledger is the service, not
                # the job, and the service stops rather than reporting it once an hour.
                raise PipelineFatal(f"{name}: {type(exc).__name__}: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - a failed job is reported, not fatal
                results.append((name, True, f"{type(exc).__name__}: {exc}"))
                log.exception("scheduled-job-failed", f"{name}: {exc}")
        return results

    def _run_sweep(self) -> None:
        report = sweep_module.sweep(self.config, self.ledger, self.graph)
        self._last_sweep = report
        # The sweep runs at 01:00 and the digest at 06:00 in the same process, so holding the
        # report is enough for the ordinary case; the mark is what survives a restart between
        # the two. Its findings — a recording at source with no ledger row, an unfinished one
        # that has left the folder, a DONE row whose outputs cannot be named — change no
        # ledger state, so they appear in no count and reached nobody at all when they went
        # only to the log.
        self._remember_report("sweep", report)
        if not report.ok:
            log.error("sweep-failed", report.headline(), needs_a_person=True)
        else:
            log.info("sweep", report.headline(), needs_a_person=report.needs_a_person)

    def _run_archive(self) -> None:
        report = archive_module.archive(self.config, self.ledger, self.graph)
        self._last_archive = report
        self._remember_report("archive", report)
        log.info("archive", report.headline())

    def _remember_report(self, name: str, report: Any) -> None:
        """Keep the rendered report where a restart cannot lose it, and never raise for it."""
        try:
            rendered = str(report.render())
        except Exception as exc:  # noqa: BLE001
            rendered = f"(the {name} report could not be rendered: {type(exc).__name__}: {exc})"
        try:
            self.ledger.cursor_set(f"{name}:last_report", rendered[:8000])
            self.ledger.cursor_set(f"{name}:last_report_at", utc_now_iso())
            self.ledger.cursor_set(
                f"{name}:last_error",
                "" if getattr(report, "ok", True) else f"{utc_now_iso()} {rendered[:400]}",
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the job
            log.warning("report-not-recorded", f"{name}: {exc}")

    def _run_digest(self) -> None:
        try:
            summary = run_digest(
                self.config, self.ledger, heartbeat=self.heartbeat,
                sweep_report=self._last_sweep, archive_report=self._last_archive,
            )
        except DigestUnavailable as exc:
            # Loud, and recorded where `status` will show it. A morning with no digest is
            # indistinguishable from a morning with nothing wrong, which is the whole reason
            # the digest is sent on good days too.
            self.ledger.cursor_set("digest:last_error", f"{utc_now_iso()} {exc}"[:400])
            log.error("digest-unavailable", str(exc))
            raise
        log.info("digest", summary)

    # -- shutdown ------------------------------------------------------------------

    def release_claims(self) -> int:
        """Hand back every claim this worker still holds, so nothing waits out a lease."""
        if self._hard_stop:
            return 0
        released = 0
        try:
            for row in self.ledger.unfinished():
                if row.claimed_by == self.owner and not row.is_terminal:
                    self.ledger.release(row.item_id, "the worker shut down cleanly")
                    released += 1
        except Exception as exc:  # noqa: BLE001 - shutdown must not raise
            log.error("release-failed", f"could not release every claim: {exc}")
        if released:
            log.info("claims-released", f"{released} recording(s) handed back", count=released)
        return released

    def _fatal(self, exc: PipelineFatal) -> None:
        message = str(exc)
        log.error(
            "fatal",
            f"the service cannot continue: {message}. No recording was quarantined for this — "
            f"it is not the recordings' fault.",
            exc_info=exc,
        )
        self.ledger.cursor_set(LAST_CYCLE_ERROR, utc_now_iso())
        self.ledger.cursor_set("worker:last_cycle_error_detail", message[:400])
        if self.heartbeat.configured:
            self.heartbeat.fail(f"transcriber stopped: {message}")


# --------------------------------------------------------------------------- the digest


class DigestUnavailable(RuntimeError):
    """The 06:00 email could not even be attempted, and somebody has to know.

    Never swallowed: a digest that only arrives when it feels like it is worse than no
    digest, because a quiet morning then means either "all is well" or "the service died"
    and nothing tells you which.
    """


def _digest_module() -> Any:
    try:
        from . import digest  # noqa: PLC0415 - only the digest paths pay for importing it
    except ImportError as exc:
        raise DigestUnavailable(
            f"the digest module could not be imported ({exc}), so no morning email can be "
            f"sent. Every other part of the service is unaffected, and this is the only "
            f"thing that will say so."
        ) from exc
    return digest


def run_digest(
    config: Any,
    ledger: Ledger,
    *,
    graph: Any = None,
    heartbeat: Any = None,
    now: float | None = None,
    dry_run: bool = False,
    day: str | None = None,
    sweep_report: Any = None,
    archive_report: Any = None,
) -> str:
    """Send the morning digest. Returns a one-line summary; raises if it did not go out.

    ``digest.run`` marks the day as sent and pings the heartbeat itself, and only on a
    successful send — so this does neither, and a failure here leaves the day unmarked and
    the digest due again on the next cycle.
    """
    module = _digest_module()
    if dry_run:
        built = module.build(config, ledger, day=day, now=now)
        return f"(dry run — nothing was sent)\n{built.subject}\n\n{built.body}"

    result = module.run(
        config, ledger, day=day, now=now, heartbeat=heartbeat,
        sweep_report=sweep_report, archive_report=archive_report,
    )
    if not result.ok:
        raise DigestUnavailable(
            f"the morning digest was built but not sent: {result.sent.detail}"
        )
    return f"{result.digest.subject} (to {result.sent.recipients} recipient(s))"


def digest_should_run(config: Any, ledger: Ledger, *, now: float | None = None) -> bool:
    """Once per local day from ``DIGEST_HOUR``, good days included.

    Delegated to the digest module, which owns the mark it sets and the throttle that stops
    a wrong SMTP password becoming a mail loop. A deploy missing that module answers True at
    the hour it was due, so the failure is reported rather than never mentioned.
    """
    try:
        module = _digest_module()
    except DigestUnavailable:
        return sweep_module.local_now(config, now).hour >= int(getattr(config, "digest_hour", 6) or 0)
    return bool(module.should_run(config, ledger, now=now))


def _trim(text: str, limit: int) -> str:
    """Keep a note short without cutting a word in half.

    These notes are read in the morning email and in ``status``, so a mark that ends mid
    sentence — "the audio kept for recordings that failed or we" — reads as a bug in the
    report rather than as a note that was too long. Whole words, and an ellipsis to say
    that there was more.
    """
    note = str(text or "").strip()
    if len(note) <= limit:
        return note
    cut = note[:limit].rsplit(" ", 1)[0].rstrip(" ,;.")
    return f"{cut} …"


def _describe(row: Row) -> str:
    """What a recording is called in a sentence somebody reads, never an id if it has a name."""
    name = str(getattr(row, "name", "") or "").strip()
    return name or str(getattr(row, "item_id", "") or "this recording")


def _output_ids(ledger: Ledger) -> frozenset[str]:
    """Graph ids of the markdown this service has written, so a poll never re-queues one."""
    ids: set[str] = set()
    for state in (State.DONE, State.SKIPPED_EMPTY):
        for row in ledger.rows_in_state(state):
            ids.update(str(v) for v in (row.output_item_ids or {}).values() if v)
    return frozenset(ids)
