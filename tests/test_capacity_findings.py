"""The capacity guards, reviewed — the five ways they could themselves lose a recording.

Every guard in this service exists to make a busy morning slower rather than lossy, so a
guard that drops, quarantines or strands a recording is worse than no guard at all: it
manufactures the exact silent failure the whole pipeline is built to remove. These are the
five findings from the review of the guards, each one driven until it bites.

  * A work directory that could go over budget and never come back under it — the drain
    then claimed nothing, forever, while discovery kept writing rows and every report said
    only that the service was busy.
  * A shutdown that charged a failed attempt to a recording it had never started, so three
    redeploys during a backlog quarantined it, and a quarantine needs a person.
  * A shutdown that abandoned transcriptions already running, with allowance left to spend.
  * A drain that held the morning email and the heartbeat behind exactly the backlog they
    exist to report.
  * A fairness rotation that lived in one process's memory, so `transcriber once --limit 3`
    on a cron served the first three routes and never reached the other five.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from typing import Any

from transcriber import ratelimit
from transcriber.diskbudget import GIB, MIB, DiskBudget
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, State
from transcriber.pipeline import Pipeline
from transcriber.ratelimit import RateLimitShutdown
from transcriber.worker import (
    ROUND_ROBIN_START,
    WORK_DIR_STUCK_SINCE,
    CycleReport,
    Worker,
)

from . import support

#: An hour-long call at this recorder's measured 16,240 bytes/second.
HOUR_LONG = 58 * 1000 * 1000


def routes(*names: str) -> tuple[Route, ...]:
    return tuple(
        Route(name=name, label=name.title(), source_folder_id=f"S-{name}",
              output_folder_id=f"O-{name}", archive_folder_id="", engine="", enabled=True)
        for name in names
    )


class _NoGraph:
    def delta_with_resync(self, *_args, **_kwargs):  # pragma: no cover - never called
        raise AssertionError("a drain must not poll")


class _NoHeartbeat:
    configured = False

    def success(self, note: str = "") -> None: return None
    def start(self, note: str = "") -> None: return None
    def fail(self, note: str = "") -> None: return None
    def log(self, note: str = "") -> None: return None


class _Outcome:
    def __init__(self, row: Any) -> None:
        self.item_id = row.item_id
        self.name = row.name
        self.result = "done"
        self.state = State.DONE
        self.reason = ""
        self.route = row.route
        self.elapsed_s = 0.0
        self.ok = True
        self.needs_a_person = False

    def line(self) -> str:
        return f"{self.name}: done"


class _Pipeline:
    """Advances a row to DONE and nothing else. No network, no disk, no engine."""

    max_attempts = 3

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.seen: list[str] = []

    def process_one(self, row: Any) -> Any:
        self.seen.append(row.item_id)
        self.ledger.advance(row.item_id, State.DONE)
        return _Outcome(row)


def fill(path: str, size: int, *, age_s: float = 0.0) -> None:
    """A sparse file of exactly this size, optionally aged: the measurement reads sizes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
        os.utime(os.path.dirname(path), (when, when))


class _WorkDir(unittest.TestCase):
    ROUTES = routes("calls", "site-meetings")

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "work")
        os.makedirs(os.path.join(self.work, "items"), exist_ok=True)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def worker(self, max_bytes: int, *, concurrency: int = 2, **overrides: Any) -> Worker:
        config = support.make_config(
            routes=self.ROUTES, work_dir=self.work, work_dir_max_bytes=max_bytes,
            concurrency=concurrency, **overrides,
        )
        self.pipeline = _Pipeline(self.ledger)
        return Worker(config, self.ledger, _NoGraph(), pipeline=self.pipeline,
                      heartbeat=_NoHeartbeat(),
                      disk=DiskBudget(self.work, max_bytes, ttl_s=0.0))

    def discover(self, item_id: str, size: int = HOUR_LONG, route: str = "calls") -> None:
        self.ledger.upsert_discovered(
            DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=size), route
        )

    def drain(self, worker: Worker, limit: int | None = None) -> CycleReport:
        report = CycleReport()
        report.outcomes = worker.drain(limit, report=report)
        return report

    def assert_untouched(self, *item_ids: str) -> None:
        for item_id in item_ids:
            row = self.ledger.get(item_id)
            self.assertIsNotNone(row, f"{item_id} vanished from the ledger")
            self.assertEqual(row.state, State.DISCOVERED, f"{item_id} moved state")
            self.assertEqual(row.attempts, 0, f"{item_id} was charged a failed attempt")
            self.assertIsNone(row.quarantine_reason, f"{item_id} was quarantined")
            self.assertIsNone(row.done_at, f"{item_id} was marked done without being done")


class AFullWorkDirectoryIsNeverPermanent(_WorkDir):
    """CRITICAL. Scratch kept for finished recordings could wedge the drain for good.

    ``_cleanup`` runs on DONE and on verified silence. A quarantined recording keeps its
    downloaded audio on purpose — a truncated battery recording is downloaded in full,
    probed, and quarantined with all 58 MB of it on disk — and a quarantined row is
    terminal, so ``process_one`` returns "already finished" for it and nothing ever removes
    that directory. The budget counted every directory under ``items/`` regardless of what
    the ledger said about it, so enough failures put the work directory permanently over its
    limit with nothing running: ``admit`` returned nothing on that cycle and on every cycle
    after it, while discovery carried on writing rows and the reports said only that the
    work directory was full. Reproduced at five leftover 60 MiB directories against a
    256 MiB budget: eight fresh recordings, none admitted, no end to it.
    """

    def quarantine_with_audio(self, item_id: str, size: int, age_s: float) -> None:
        self.discover(item_id)
        self.ledger.quarantine(item_id, "the audio is not a whole recording")
        fill(os.path.join(self.work, "items", item_id, "audio.m4a"), size, age_s=age_s)

    def test_the_audio_of_quarantined_recordings_stops_holding_the_budget(self) -> None:
        for n in range(5):
            self.quarantine_with_audio(f"truncated-{n}", 60 * MIB, age_s=3 * 24 * 3600)
        for n in range(1, 9):
            self.discover(f"call-{n}", size=8 * MIB)
        worker = self.worker(256 * MIB, concurrency=8)

        report = self.drain(worker)

        self.assertEqual(report.space.reclaimed_items, 5)
        self.assertGreater(report.space.reclaimed_bytes, 250 * MIB)
        self.assertFalse(report.space.over_budget)
        self.assertTrue(self.pipeline.seen, "the drain never restarted")
        for n in range(5):
            self.assertFalse(
                os.path.exists(os.path.join(self.work, "items", f"truncated-{n}")),
                "the audio of a finished recording is still holding the budget",
            )
        # And the quarantined rows are exactly as they were. Clearing the scratch is not
        # closing the case: the recordings are untouched in OneDrive and the row still says
        # a person has to look at it.
        for n in range(5):
            row = self.ledger.get(f"truncated-{n}")
            self.assertEqual(row.state, State.QUARANTINED)
            self.assertIsNotNone(row.quarantine_reason)

    def test_a_queue_of_eighty_still_drains_after_a_week_of_failures(self) -> None:
        """The whole shape of the wedge: nothing running, over budget, and no way out."""
        for n in range(5):
            self.quarantine_with_audio(f"truncated-{n}", 60 * MIB, age_s=5 * 24 * 3600)
        for n in range(1, 21):
            self.discover(f"call-{n:02d}", size=8 * MIB, route="site-meetings")
        worker = self.worker(256 * MIB, concurrency=8)

        cycles = 0
        while self.ledger.claimable(limit=200, now=time.time()) and cycles < 40:
            self.drain(worker)
            cycles += 1

        self.assertEqual(sorted(self.pipeline.seen),
                         sorted(f"call-{n:02d}" for n in range(1, 21)))
        self.assertEqual(self.ledger.unfinished(), [])

    def test_this_mornings_quarantine_still_has_its_audio_to_listen_to(self) -> None:
        """Kept for a retry has to mean kept — just not kept until the disk is full."""
        self.quarantine_with_audio("truncated", 60 * MIB, age_s=600)
        self.discover("call-1", size=8 * MIB)
        worker = self.worker(4 * GIB)

        report = self.drain(worker)

        self.assertEqual(report.space.reclaimed_items, 0)
        self.assertTrue(os.path.exists(os.path.join(self.work, "items", "truncated")))

    def test_the_audio_of_a_recording_still_in_the_queue_is_never_cleared(self) -> None:
        """A row waiting out its backoff keeps its download however old it is."""
        self.discover("waiting")
        fill(os.path.join(self.work, "items", "waiting", "audio.m4a"),
             60 * MIB, age_s=30 * 24 * 3600)
        worker = self.worker(4 * GIB)

        report = self.drain(worker)

        self.assertEqual(report.space.reclaimed_items, 0)
        self.assertTrue(
            os.path.exists(os.path.join(self.work, "items", "waiting", "audio.m4a")),
            "the download of a recording that has not been transcribed yet was deleted",
        )


class OverBudgetWithNothingRunningDoesNotLastForever(_WorkDir):
    """CRITICAL, the second half. Waiting is only a plan while there is work to wait for.

    Over budget with recordings running is the guard pacing itself, and it needs no time
    limit: the work in progress finishes and frees its space. Over budget with *nothing*
    running is a wait for something that is never going to arrive, and claiming nothing
    forever is a stopped service wearing the clothes of a busy one. So after an hour of it,
    one recording is started anyway — one, not the queue — and it is said out loud.
    """

    def stuck_since(self, seconds_ago: float) -> None:
        self.ledger.cursor_set(WORK_DIR_STUCK_SINCE, f"{time.time() - seconds_ago:.0f}")

    def test_one_recording_is_started_anyway_and_the_rest_are_untouched(self) -> None:
        fill(os.path.join(self.work, "items", "left-behind", "audio.m4a"), 400 * MIB)
        for n in range(1, 6):
            self.discover(f"call-{n}", size=8 * MIB)
        worker = self.worker(256 * MIB, concurrency=4)
        self.stuck_since(2 * 3600)

        report = self.drain(worker)

        self.assertEqual(len(self.pipeline.seen), 1, "exactly one, not the queue")
        self.assertTrue(report.space.forced)
        self.assertIn("needs looking at", report.line())
        self.assertIn("Nothing has been dropped", report.space.note)
        self.assertTrue(report.ok, report.errors)
        started = self.pipeline.seen[0]
        self.assert_untouched(*[f"call-{n}" for n in range(1, 6) if f"call-{n}" != started])

    def test_the_queue_drains_one_at_a_time_rather_than_not_at_all(self) -> None:
        fill(os.path.join(self.work, "items", "left-behind", "audio.m4a"), 400 * MIB)
        for n in range(1, 6):
            self.discover(f"call-{n}", size=8 * MIB)
        worker = self.worker(256 * MIB, concurrency=4)

        for _ in range(5):
            self.stuck_since(2 * 3600)
            self.drain(worker)

        self.assertEqual(sorted(self.pipeline.seen),
                         sorted(f"call-{n}" for n in range(1, 6)))
        self.assertEqual(self.ledger.unfinished(), [])

    def test_a_busy_hour_is_still_just_a_busy_hour(self) -> None:
        """The first cycles over budget hold everything back, exactly as they did before."""
        fill(os.path.join(self.work, "items", "left-behind", "audio.m4a"), 400 * MIB)
        for n in range(1, 6):
            self.discover(f"call-{n}", size=8 * MIB)
        worker = self.worker(256 * MIB, concurrency=4)

        report = self.drain(worker)

        self.assertEqual(self.pipeline.seen, [])
        self.assertTrue(report.space.over_budget)
        self.assertFalse(report.space.forced)
        # And the clock on it started, so an hour from now this is a standstill and not a
        # busy afternoon. Without the mark there is nothing to tell the two apart.
        self.assertTrue(self.ledger.cursor_get(WORK_DIR_STUCK_SINCE))

    def test_a_recording_too_large_for_the_budget_is_never_the_one_forced(self) -> None:
        fill(os.path.join(self.work, "items", "left-behind", "audio.m4a"), 400 * MIB)
        self.discover("marathon", size=3 * GIB)
        self.discover("call-1", size=8 * MIB)
        worker = self.worker(256 * MIB)
        self.stuck_since(2 * 3600)

        self.drain(worker)

        self.assertEqual(self.pipeline.seen, ["call-1"])
        self.assert_untouched("marathon")


class AShutdownCostsARecordingNothing(unittest.TestCase):
    """HIGH. A stop during a wait charged an attempt to a recording never started.

    ``RateLimitShutdown`` is raised at threads that are merely *waiting* for a slot or a
    token, by design, 30 seconds into any stop. It was not in the never-retry list and not
    in the fatal list, so it fell through to the generic branch: an attempt recorded, and at
    ``MAX_ATTEMPTS`` a quarantine. With ``CONCURRENCY`` at 8 and ``ENGINE_MAX_CONCURRENT``
    at 3, five threads are queued at the limiter at any moment under load, so one stop
    charged five recordings at once — and three redeploys during a backlog, which is exactly
    when a service is restarted, quarantined them. A quarantine needs a person; on the
    automated path the recording is lost. The guard was doing the dropping.
    """

    def pipeline(self, ledger: Ledger, **overrides: Any) -> Pipeline:
        config = support.make_config(**overrides)
        built = Pipeline(config, ledger, support.FakeGraph([]), owner="worker-A")

        def stopped(_row: Any, _started: float) -> Any:
            raise RateLimitShutdown(
                "the service is stopping; this recording was still waiting for a turn at "
                "the engine rate limit and was not started."
            )

        built._walk = stopped  # type: ignore[method-assign]
        return built

    def test_the_attempt_count_does_not_move_and_the_row_stays_claimable(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))

            outcome = self.pipeline(ledger).process_one("A")

            row = ledger.get("A")
            self.assertEqual(row.attempts, 0, "a recording that was never started was charged")
            self.assertFalse(row.is_terminal, "a stop finished a recording it never started")
            self.assertIsNone(row.claimed_by, "the claim was not handed back")
            self.assertIsNone(row.quarantine_reason)
            self.assertEqual(outcome.result, "retry")
            self.assertIn("still queued", outcome.reason)
            self.assertIn("A", {r.item_id for r in ledger.claimable(limit=10, now=time.time())})

    def test_three_redeploys_during_a_backlog_quarantine_nothing(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            pipeline = self.pipeline(ledger, max_attempts=3)

            for _ in range(5):
                pipeline.process_one("A")

            row = ledger.get("A")
            self.assertEqual(row.attempts, 0)
            self.assertFalse(row.is_terminal)
            self.assertEqual(len(ledger.rows_in_state(State.QUARANTINED)), 0,
                             "restarting the service quarantined a recording")
            self.assertIn("A", {r.item_id for r in ledger.claimable(limit=10, now=time.time())})

    def test_an_ordinary_failure_is_still_counted_and_still_quarantines(self) -> None:
        """The exemption is for the service stopping, not for failures in general."""
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            config = support.make_config(max_attempts=2)
            pipeline = Pipeline(config, ledger, support.FakeGraph([]), owner="worker-A")
            pipeline._backoff = lambda _attempts: 0.0  # type: ignore[method-assign]

            def broken(_row: Any, _started: float) -> Any:
                raise RuntimeError("the engine returned nonsense")

            pipeline._walk = broken  # type: ignore[method-assign]

            pipeline.process_one("A")
            pipeline.process_one("A")

            row = ledger.get("A")
            self.assertEqual(row.attempts, 2)
            self.assertEqual(row.state, State.QUARANTINED)


class AStopDoesNotAbandonWhatIsAlreadyRunning(unittest.TestCase):
    """MEDIUM. With a per-minute limit set, a stop killed in-flight transcriptions.

    ``take_token`` is called at the top of every attempt inside the HTTP client's retry
    loop, on a thread that already holds a slot and is mid-transcription. The wait checked
    the stop flag before it checked whether a token was even needed, so the moment the
    release watcher fired — 30 seconds into an ordinary redeploy — every in-flight engine
    request died on its next attempt with allowance still in the bucket, including an
    hour-long call forty minutes into being split and transcribed piece by piece. Both the
    module's docstring and the watcher's own comment promise the opposite.
    """

    def setUp(self) -> None:
        self.addCleanup(ratelimit.clear_shutdown)

    def test_a_transcription_in_flight_spends_its_allowance_and_finishes(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=3, max_per_minute=60,
                                        clock=lambda: 1000.0, name="test")
        spent = 0
        with limiter.slot():                       # what LimitedEngine.transcribe holds
            limiter.take_token()
            spent += 1
            ratelimit.request_shutdown("the process is going")
            for _ in range(3):                     # the retry loop's next attempts
                limiter.take_token()
                spent += 1

        self.assertEqual(spent, 4)

    def test_nothing_new_is_started_once_the_stop_is_set(self) -> None:
        """The other half: a stop still means no new work, room or no room."""
        limiter = ratelimit.RateLimiter(max_concurrent=3, max_per_minute=60,
                                        clock=lambda: 1000.0, name="test")
        ratelimit.request_shutdown("the process is going")

        with self.assertRaises(RateLimitShutdown):
            with limiter.slot():
                pass  # pragma: no cover - the acquire raises first

    def test_an_in_flight_thread_with_no_allowance_left_is_still_let_go(self) -> None:
        """It is waiting, and waiting is the one thing a stop is allowed to interrupt."""
        limiter = ratelimit.RateLimiter(max_concurrent=3, max_per_minute=1,
                                        clock=lambda: 1000.0, name="test", poll_s=0.005)
        raised: list[BaseException] = []

        def working() -> None:
            with limiter.slot():
                limiter.take_token()               # spends the only one
                ratelimit.request_shutdown("the process is going")
                try:
                    limiter.take_token()           # nothing left; this waits
                except RateLimitShutdown as exc:
                    raised.append(exc)

        thread = threading.Thread(target=working, daemon=True)
        thread.start()
        thread.join(2.0)

        self.assertFalse(thread.is_alive(), "the stop never reached the waiting thread")
        self.assertEqual(len(raised), 1)


class TheMorningEmailIsNotStuckBehindTheBacklog(_WorkDir):
    """MEDIUM. The drain is synchronous and unbounded on the loop thread.

    ``run_once`` drained and only then ran the scheduled jobs, so the 06:00 digest, the
    nightly sweep and the heartbeat ping inside the digest all waited behind exactly the
    backlog they exist to report. Eight staff and a morning of hour-long calls at three
    transcriptions at a time is the better part of an hour with no poll, no sweep and no
    heartbeat — the external watchdog fires a false alarm precisely when the service is
    busiest, and the email that would have said "42 queued, working through them" arrives
    hours late. Nothing is lost; the visibility guard is disabled by the load it describes.
    """

    class _Ordered(Worker):
        order: list[str]

        def run_scheduled_jobs(self, *, force: Any = ()) -> list[tuple[str, bool, str]]:
            self.order.append("jobs")
            return []

        def drain(self, limit: Any = None, **kwargs: Any) -> list[Any]:
            self.order.append("drain")
            return super().drain(limit, **kwargs)

    def test_the_scheduled_jobs_run_before_the_drain_as_well_as_after(self) -> None:
        worker = self._Ordered(
            support.make_config(routes=self.ROUTES, work_dir=self.work,
                                work_dir_max_bytes=0, concurrency=2),
            self.ledger, support.FakeGraph([]),
            pipeline=_Pipeline(self.ledger), heartbeat=_NoHeartbeat(),
        )
        worker.order = []

        worker.run_once()

        self.assertEqual(worker.order, ["jobs", "drain", "jobs"],
                         "the digest and its heartbeat ping wait behind the whole backlog")

    def test_one_drain_takes_on_a_cycles_work_and_not_the_whole_queue(self) -> None:
        for n in range(1, 41):
            self.discover(f"call-{n:02d}", size=8 * MIB)
        worker = self.worker(0, concurrency=8)

        self.drain(worker)

        self.assertEqual(len(self.pipeline.seen), 8,
                         "a single drain swallowed the backlog and held the loop up")
        self.assertEqual(len(self.ledger.claimable(limit=100, now=time.time())), 32)


class FairnessSurvivesARestart(_WorkDir):
    """MEDIUM. The rotation lived in one worker's memory and started at 0 every process.

    ``transcriber once --limit 3`` on a cron is a documented way to drive this service and
    builds a fresh worker every run. With eight routes, the interleave was therefore offered
    the same starting point every single time: routes four to eight were never served, no
    matter how long they waited — the same burial fairness exists to prevent, moved from
    inside one queue to across processes. In the long-running loop it advanced, but a
    restart still reset it, so a service redeployed often favoured whichever routes happen
    to be listed first.
    """

    ROUTES = routes("alice", "ben", "cara", "dan", "eve", "faz", "gus", "hana")

    def setUp(self) -> None:
        super().setUp()
        for name in ("alice", "ben", "cara", "dan", "eve", "faz", "gus", "hana"):
            for n in range(1, 4):
                self.discover(f"{name}-{n}", size=MIB, route=name)

    def test_a_fresh_once_process_carries_on_where_the_last_one_stopped(self) -> None:
        served: list[str] = []
        for _ in range(3):
            # A new Worker each time is the whole point: this is `once --limit 3` on a cron.
            worker = self.worker(0, concurrency=8)
            served.extend(row.route for row in worker.claimable(3))

        # One place further along on each run, which is what the rotation does inside the
        # loop too. What matters is that it moved at all: before, every one of these three
        # processes started at alice and routes past cara were never served.
        self.assertEqual(served, [
            "alice", "ben", "cara",
            "ben", "cara", "dan",
            "cara", "dan", "eve",
        ])

    def test_every_route_is_reached_within_a_round_of_small_processes(self) -> None:
        served: list[str] = []
        for _ in range(8):
            worker = self.worker(0, concurrency=8)
            served.extend(row.route for row in worker.claimable(3))

        self.assertEqual(sorted(set(served)), sorted(r.name for r in self.ROUTES),
                         "a route was never served, however long it waited")

    def test_the_rotation_is_written_down_rather_than_remembered(self) -> None:
        self.worker(0, concurrency=8).claimable(3)

        self.assertEqual(self.ledger.cursor_get(ROUND_ROBIN_START), "1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
