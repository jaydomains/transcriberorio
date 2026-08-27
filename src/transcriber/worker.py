"""The loop: poll the change feed, process what is claimable, run the scheduled jobs.

Three things here are worth knowing before changing any of it.

**The cursor never advances past a row.** :meth:`Worker.poll` hands each delta page to
``Ledger.record_page``, which writes that page's rows and that page's cursor in one
transaction. If a page comes back with no cursor at all, the rows are still recorded and the
cursor is deliberately left where it was, so the next poll re-reads a page we already have
rather than skipping one we do not. Re-reading costs a second of work; skipping loses a
recording, which is the failure this service exists to remove.

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
from .graph import ResyncRequired
from .heartbeat import Heartbeat
from .ledger import DELTA_CURSOR, Ledger
from .logging_setup import get_logger, item_context
from .models import DriveItem, Row, State, utc_now_iso
from .pipeline import _FATAL as PIPELINE_FATAL_ERRORS
from .pipeline import Outcome, Pipeline, PipelineFatal

__all__ = [
    "Worker",
    "CycleReport",
    "PollResult",
    "DigestUnavailable",
    "run_digest",
    "digest_should_run",
    "LAST_CYCLE_OK",
    "LAST_CYCLE_ERROR",
    "LAST_POLL_OK",
    "claimable_now",
]

log = get_logger(__name__)

#: Bookkeeping marks. ``status`` reads them to answer "when did this last work?", which is
#: the question a person actually has when they run it.
LAST_CYCLE_OK = "worker:last_cycle_ok"
LAST_CYCLE_ERROR = "worker:last_cycle_error"
LAST_POLL_OK = "worker:last_poll_ok"


@dataclass
class PollResult:
    """One walk of the change feed."""

    pages: int = 0
    items_seen: int = 0
    recorded: int = 0
    new: list[str] = field(default_factory=list)
    resynced: bool = False
    cursor_held_back: int = 0
    skipped_as_ours: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def line(self) -> str:
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
class CycleReport:
    """One pass of the loop: a poll, a drain, and whatever scheduled jobs were due."""

    started_at: str = ""
    poll: PollResult = field(default_factory=PollResult)
    outcomes: list[Outcome] = field(default_factory=list)
    jobs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for o in self.outcomes if o.result == "done")

    @property
    def quarantined(self) -> int:
        return sum(1 for o in self.outcomes if o.needs_a_person)

    @property
    def ok(self) -> bool:
        return self.poll.ok and not self.errors

    def line(self) -> str:
        parts = [
            self.poll.line(),
            f"{len(self.outcomes)} processed",
            f"{self.done} done",
            f"{self.quarantined} quarantined",
        ]
        if self.jobs:
            parts.append("ran " + ", ".join(self.jobs))
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return "; ".join(parts)


def claimable_now(ledger: Ledger, limit: int, clock: float) -> list[Row]:
    """Claimable rows whose backoff has elapsed, oldest first.

    The backoff lives in the row's meta rather than in a column, so it is filtered here
    rather than in SQL: a row that failed a minute ago is claimable as far as the lease is
    concerned, and hammering it again immediately is how a throttled provider stays throttled.
    """
    ready: list[Row] = []
    for row in ledger.claimable(limit=limit, now=clock):
        if float(row.meta.get("retry_at") or 0.0) > clock:
            continue
        ready.append(row)
    return ready


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
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.graph = graph
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        self.pipeline = pipeline or Pipeline(config, ledger, graph, owner=self.owner)
        self.heartbeat = heartbeat if heartbeat is not None else Heartbeat.from_config(config)
        self.poll_interval_s = max(1, int(getattr(config, "poll_interval_s", 120) or 120))
        self.concurrency = max(1, int(getattr(config, "concurrency", 2) or 2))
        self.source_folder_id = str(getattr(config, "source_folder_id", "") or "") or None
        self.output_folder_id = str(getattr(config, "output_folder_id", "") or "")

        self._stop = threading.Event()
        self._stop_reason = ""
        self._hard_stop = False
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._last_sweep: Any = None
        self._last_archive: Any = None

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
        log.info(
            "started",
            f"polling every {self.poll_interval_s}s, {self.concurrency} at a time",
            owner=self.owner, poll_interval_s=self.poll_interval_s, concurrency=self.concurrency,
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

    def run_once(self, *, limit: int | None = None) -> CycleReport:
        """One cycle: poll, drain what is claimable, run whatever scheduled job is due."""
        report = CycleReport(started_at=utc_now_iso())
        report.poll = self.poll()
        if not report.poll.ok:
            report.errors.append(report.poll.error)

        report.outcomes = self.drain(limit)

        for name, ran, error in self.run_scheduled_jobs():
            if ran:
                report.jobs.append(name)
            if error:
                report.errors.append(f"{name}: {error}")

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

    def _wait(self, seconds: float) -> None:
        if seconds > 0:
            self._stop.wait(seconds)

    # -- polling -------------------------------------------------------------------

    def poll(self) -> PollResult:
        """Walk delta from the stored cursor, recording rows and cursor together."""
        result = PollResult()
        cursor = self.ledger.cursor_get(DELTA_CURSOR)

        def on_resync(exc: ResyncRequired) -> None:
            result.resynced = True
            self.ledger.rewind_cursor(
                DELTA_CURSOR,
                "Microsoft Graph rejected the stored delta cursor (HTTP 410); "
                "re-enumerating the folder from zero",
            )
            log.warning("delta-resync", str(exc))

        own_outputs = _output_ids(self.ledger)
        try:
            for page in self.graph.delta_with_resync(self.source_folder_id, cursor, on_resync):
                result.pages += 1
                rows: list[DriveItem] = []
                for item in page.items:
                    result.items_seen += 1
                    deleted = bool(getattr(item, "is_deleted", False))
                    if not deleted and self._is_ours(item, own_outputs):
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
                            f"Graph returned an item with no id (name {getattr(item, 'name', '')!r}); "
                            f"it cannot be tracked and has not been recorded",
                        )
                        continue
                    rows.append(DriveItem.from_graph_item(item))

                if page.cursor:
                    new = self.ledger.record_page(rows, page.cursor)
                    result.recorded += len(rows)
                    result.new.extend(new)
                else:
                    # No deltaLink and no nextLink. Record the rows; leave the cursor where
                    # it is. Re-reading a page is free; advancing past one is not.
                    for row in rows:
                        if self.ledger.upsert_discovered(row):
                            result.new.append(row.item_id)
                    result.recorded += len(rows)
                    result.cursor_held_back += 1
                    log.error(
                        "delta-page-without-cursor",
                        "a delta page carried no cursor, so the rows were recorded and the "
                        "cursor was left unchanged; the next poll re-reads this page",
                    )
        except PIPELINE_FATAL_ERRORS as exc:
            # A rejected credential, an unusable configuration or a broken ledger is a fault
            # in the *service*, and it surfaces here — every 120 seconds — long before any
            # recording reaches the pipeline that knows how to classify it. Folding it into a
            # cycle error left the loop spinning on a failing poll indefinitely, never
            # pinging the heartbeat's failure endpoint, while the morning email said only
            # "nothing arrived yesterday". One list of fatal classes, not two.
            raise PipelineFatal(f"{type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - the loop must survive one bad poll
            result.error = f"{type(exc).__name__}: {exc}"
            log.exception("poll-failed", result.error)
            return result

        self.ledger.cursor_set(LAST_POLL_OK, utc_now_iso())
        for item_id in result.new:
            row = self.ledger.get(item_id)
            log.info("discovered", row.name if row else item_id, item=item_id)
        log.info("polled", result.line(), pages=result.pages, seen=result.items_seen,
                 new=len(result.new))
        return result

    def _is_ours(self, item: Any, own_outputs: frozenset[str]) -> bool:
        """Our own markdown, seen because the output folder is inside the watched tree."""
        kind = sweep_module.classify(
            item, output_folder_id=self.output_folder_id, own_output_ids=own_outputs
        )
        return kind in (sweep_module.OUR_OUTPUT, sweep_module.STRUCTURE)

    # -- processing ----------------------------------------------------------------

    def drain(self, limit: int | None = None) -> list[Outcome]:
        """Process everything claimable, ``concurrency`` at a time, then return."""
        rows = claimable_now(self.ledger, limit or (self.concurrency * 8), self.clock())
        if not rows:
            return []
        if self.stopping:
            log.info("drain-skipped", f"{len(rows)} claimable, but this worker is shutting down")
            return []
        return self.process_rows(rows, self.concurrency)

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
                       state=row.state, reason=reason)

    def _process(self, row: Row) -> Outcome:
        with self._tracking(row.item_id), item_context(row.item_id):
            outcome = self.pipeline.process_one(row)
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


def _output_ids(ledger: Ledger) -> frozenset[str]:
    """Graph ids of the markdown this service has written, so a poll never re-queues one."""
    ids: set[str] = set()
    for state in (State.DONE, State.SKIPPED_EMPTY):
        for row in ledger.rows_in_state(state):
            ids.update(str(v) for v in (row.output_item_ids or {}).values() if v)
    return frozenset(ids)
