"""Eight people, eighty files, a small disk — and not one recording lost.

The four capacity guards exist so that a busy morning slows the service down instead of
breaking it. That is only worth having if the slowing down is provably harmless, so every
test here drives the guard until it bites and then asks the same three questions of the
recordings it held back:

  * is the row still exactly as it was — same state, no extra attempt, no quarantine, and
    above all not DONE and not SKIPPED_EMPTY;
  * is it still claimable, so a later cycle picks it up;
  * does it in fact get picked up and processed exactly once when the pressure lifts.

They are deliberately end-to-end over the worker rather than unit tests of the budget: the
question is not whether :mod:`transcriber.diskbudget` computes the right number, it is
whether a drain under pressure can lose a recording, and that can only be asked of the
drain. The companion file ``test_disk_budget_and_fairness.py`` asserts the pieces; this one
runs them until they hurt.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.diskbudget import GIB, MIB, DiskBudget
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, State
from transcriber.worker import CycleReport, Worker

from . import support


#: An hour-long call at this recorder's measured 16,240 bytes/second.
HOUR_LONG = 58 * 1000 * 1000

#: Eight members of staff, each recording into their own OneDrive folder.
STAFF = ("alice", "ben", "cara", "dan", "eve", "faz", "gus", "hana")


def routes_for(*names: str) -> tuple[Route, ...]:
    return tuple(
        Route(name=name, label=name.title(), source_folder_id=f"S-{name}",
              output_folder_id=f"O-{name}", archive_folder_id="", engine="", enabled=True)
        for name in (names or STAFF)
    )


class _NoGraph:
    """A drain must not poll. If one of these tests reaches Graph, it is lying."""

    def delta_with_resync(self, *_args, **_kwargs):  # pragma: no cover - never called
        raise AssertionError("the drain must not poll")


class _NoHeartbeat:
    configured = False

    def success(self, note: str = ""): return None
    def start(self, note: str = ""): return None
    def fail(self, note: str = ""): return None
    def log(self, note: str = ""): return None


class _Outcome:
    """Shaped like :class:`transcriber.pipeline.Outcome`, with nothing behind it."""

    def __init__(self, row) -> None:
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
    """Processes a recording the way the real one does, minus every byte of network.

    Two behaviours matter to these tests and both are real: it advances the ledger row to
    DONE, so a processed recording leaves the queue rather than being picked up again next
    cycle; and it can be told to leave scratch behind in the work directory, which is what
    a failed or quarantined recording really does — the audio is kept on purpose so a retry
    does not download it again, and that kept audio is what fills a work directory up.
    """

    def __init__(self, ledger: Ledger, work_dir: str, *, leaves_bytes: int = 0) -> None:
        self.ledger = ledger
        self.work_dir = work_dir
        self.leaves_bytes = int(leaves_bytes)
        self.seen: list[str] = []

    def process_one(self, row):
        self.seen.append(row.item_id)
        if self.leaves_bytes:
            fill(os.path.join(self.work_dir, "items", row.item_id, "audio.m4a"),
                 self.leaves_bytes)
        self.ledger.advance(row.item_id, State.DONE)
        return _Outcome(row)


def fill(path: str, size: int) -> None:
    """A sparse file of exactly this size — the measurement reads sizes, not bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


class _Fixture(unittest.TestCase):
    """A ledger, a work directory and a worker whose budget a test can set."""

    ROUTES: tuple[Route, ...] = routes_for("calls", "site-meetings")

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "work")
        os.makedirs(self.work, exist_ok=True)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def worker(self, max_bytes: int, *, concurrency: int = 2,
               leaves_bytes: int = 0, routes: tuple[Route, ...] | None = None) -> Worker:
        config = support.make_config(
            routes=routes or self.ROUTES, work_dir=self.work,
            work_dir_max_bytes=max_bytes, concurrency=concurrency,
        )
        self.pipeline = _Pipeline(self.ledger, self.work, leaves_bytes=leaves_bytes)
        return Worker(
            config, self.ledger, _NoGraph(), pipeline=self.pipeline,
            heartbeat=_NoHeartbeat(),
            # ttl 0: a test that changes the work directory between drains must see it.
            disk=DiskBudget(self.work, max_bytes, ttl_s=0.0),
        )

    def discover(self, item_id: str, size: int = HOUR_LONG, route: str = "calls") -> None:
        self.ledger.upsert_discovered(
            DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=size), route
        )

    def drain(self, worker: Worker, limit: int | None = None) -> CycleReport:
        report = CycleReport()
        report.outcomes = worker.drain(limit, report=report)
        return report

    # -- the three questions, asked of every recording a guard held back ---------------

    def assert_untouched(self, *item_ids: str) -> None:
        for item_id in item_ids:
            row = self.ledger.get(item_id)
            self.assertIsNotNone(row, f"{item_id} vanished from the ledger")
            self.assertEqual(row.state, State.DISCOVERED,
                             f"{item_id} changed state without being processed")
            self.assertEqual(row.attempts, 0, f"{item_id} was charged a failed attempt")
            self.assertIsNone(row.quarantine_reason, f"{item_id} was quarantined")
            self.assertIsNone(row.skipped_reason, f"{item_id} was written off as silence")
            self.assertIsNone(row.done_at, f"{item_id} was marked done without being done")

    def assert_still_claimable(self, *item_ids: str) -> None:
        claimable = {row.item_id for row in self.ledger.claimable(limit=500, now=0.0)}
        for item_id in item_ids:
            self.assertIn(item_id, claimable,
                          f"{item_id} was held back and is no longer claimable")


class AFullWorkDirectoryStopsTheDrainAndLosesNothing(_Fixture):
    def test_over_budget_claims_nothing_reports_it_and_keeps_every_row(self) -> None:
        # 400 MiB of scratch already there, on a 256 MiB budget: genuinely over.
        fill(os.path.join(self.work, "items", "in-progress", "audio.m4a"), 400 * MIB)
        for n in range(1, 5):
            self.discover(f"call-{n}")
        self.discover("site-1", route="site-meetings")
        worker = self.worker(256 * MIB)

        report = self.drain(worker)

        self.assertEqual(self.pipeline.seen, [])
        self.assertTrue(report.space.over_budget)
        # Reported, and reported as pacing rather than as breakage.
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, [])
        self.assertIn("work directory full", report.line())
        self.assertIn("Nothing has been dropped", report.space.note)
        self.assertIn("work directory", self.ledger.cursor_get("worker:work_dir") or "")
        self.assert_untouched("call-1", "call-2", "call-3", "call-4", "site-1")
        self.assert_still_claimable("call-1", "call-2", "call-3", "call-4", "site-1")

    def test_a_long_full_spell_never_turns_into_a_lost_recording(self) -> None:
        """Ten cycles over budget is ten cycles of no progress and nothing broken.

        This is the shape of a real bad afternoon: the disk is full because recordings that
        failed are keeping their audio, and every two minutes the worker looks again. What
        must not happen anywhere in those ten looks is a row quietly moving.
        """
        fill(os.path.join(self.work, "items", "kept-for-retry", "audio.m4a"), 900 * MIB)
        for n in range(1, 9):
            self.discover(f"call-{n}", route=STAFF[n % 2])
        worker = self.worker(512 * MIB, routes=routes_for(STAFF[0], STAFF[1]))

        for _ in range(10):
            report = self.drain(worker)
            self.assertTrue(report.space.over_budget)
            self.assertTrue(report.ok)

        self.assertEqual(self.pipeline.seen, [])
        self.assert_untouched(*[f"call-{n}" for n in range(1, 9)])
        self.assertEqual(len(self.ledger.unfinished()), 8)

    def test_the_whole_queue_drains_once_the_space_comes_back(self) -> None:
        blocker = os.path.join(self.work, "items", "kept-for-retry", "audio.m4a")
        fill(blocker, 900 * MIB)
        for n in range(1, 7):
            self.discover(f"call-{n}", route=STAFF[n % 2])
        worker = self.worker(512 * MIB, routes=routes_for(STAFF[0], STAFF[1]))
        self.drain(worker)
        self.assertEqual(self.pipeline.seen, [])

        os.remove(blocker)
        for _ in range(10):
            self.drain(worker)
            if not self.ledger.claimable(limit=50, now=0.0):
                break

        # Every recording processed, each exactly once, and the queue is empty.
        self.assertEqual(sorted(self.pipeline.seen),
                         sorted(f"call-{n}" for n in range(1, 7)))
        self.assertEqual(len(self.pipeline.seen), len(set(self.pipeline.seen)))
        self.assertEqual(self.ledger.unfinished(), [])

    def test_a_burst_bigger_than_the_budget_is_worked_through_over_cycles(self) -> None:
        """Twenty hour-long calls, room for a few at a time: slower, never shorter.

        The pipeline here leaves nothing behind, which is what a *successful* recording
        does — so the budget frees up as the work finishes and the queue drains at the rate
        the disk allows.
        """
        for n in range(1, 21):
            self.discover(f"call-{n:02d}", route=STAFF[n % 8])
        worker = self.worker(512 * MIB, concurrency=8, routes=routes_for())

        cycles = 0
        while self.ledger.claimable(limit=100, now=0.0) and cycles < 40:
            report = self.drain(worker)
            cycles += 1
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.space.refused, [])

        self.assertEqual(sorted(self.pipeline.seen),
                         sorted(f"call-{n:02d}" for n in range(1, 21)))
        self.assertEqual(len(self.pipeline.seen), len(set(self.pipeline.seen)),
                         "a recording was processed twice")
        self.assertGreater(cycles, 1, "the budget did not actually pace anything")
        self.assertEqual(self.ledger.unfinished(), [])

    def test_the_recordings_held_for_want_of_space_are_the_next_ones_taken(self) -> None:
        """Held is a queue position, not a penalty: nothing goes to the back of the line."""
        for n in range(1, 7):
            self.discover(f"call-{n}")
        worker = self.worker(512 * MIB, concurrency=8, routes=routes_for("calls"))

        first = self.drain(worker)
        held = first.space.held
        self.assertGreater(held, 0, "this budget was meant to hold something back")
        taken_first = list(self.pipeline.seen)

        self.drain(worker)
        taken_second = self.pipeline.seen[len(taken_first):]

        self.assertEqual(taken_first + taken_second,
                         sorted(taken_first + taken_second),
                         "the held recordings were not taken in the order they waited")


class ARecordingTooBigForTheBudgetIsRefusedNotDownloaded(_Fixture):
    def test_it_is_refused_by_name_with_the_setting_to_change(self) -> None:
        self.discover("marathon", 3 * GIB)
        worker = self.worker(1 * GIB)

        report = self.drain(worker)

        self.assertEqual([item for item, _ in report.space.refused], ["marathon"])
        reason = report.space.refused[0][1]
        self.assertIn("marathon.m4a", reason)
        self.assertIn("WORK_DIR_MAX_BYTES", reason)
        self.assertIn("Waiting will not help", reason)
        # Never started, so never downloaded: the guard is before the claim, not during it.
        self.assertEqual(self.pipeline.seen, [])
        self.assert_untouched("marathon")
        self.assert_still_claimable("marathon")

    def test_refusing_it_every_cycle_never_creeps_towards_a_quarantine(self) -> None:
        """MAX_ATTEMPTS is about recordings that fail. This one has not been tried."""
        self.discover("marathon", 3 * GIB)
        worker = self.worker(1 * GIB)

        for _ in range(6):
            self.drain(worker)

        row = self.ledger.get("marathon")
        self.assertEqual(row.attempts, 0)
        self.assertEqual(row.state, State.DISCOVERED)
        self.assertIsNone(row.last_error)
        self.assertIn("WORK_DIR_MAX_BYTES",
                      self.ledger.cursor_get("worker:work_dir_refused") or "")

    def test_raising_the_limit_is_all_it_takes_to_pick_it_up(self) -> None:
        """The refusal names a variable, so the refusal has to be undone by that variable."""
        self.discover("marathon", 3 * GIB)
        self.drain(self.worker(1 * GIB))

        bigger = self.worker(16 * GIB)
        report = self.drain(bigger)

        self.assertEqual(self.pipeline.seen, ["marathon"])
        self.assertEqual(report.space.refused, [])
        self.assertEqual(self.ledger.get("marathon").state, State.DONE)

    def test_one_oversized_recording_does_not_block_the_ordinary_ones(self) -> None:
        self.discover("marathon", 3 * GIB)
        for n in range(1, 4):
            self.discover(f"call-{n}")
        worker = self.worker(2 * GIB, concurrency=4)

        report = self.drain(worker)

        self.assertEqual(sorted(self.pipeline.seen), ["call-1", "call-2", "call-3"])
        self.assertEqual([item for item, _ in report.space.refused], ["marathon"])
        self.assert_untouched("marathon")

    def test_a_recording_whose_size_is_unknown_is_never_refused_on_a_guess(self) -> None:
        """Graph occasionally hands back an item with no size. It is not oversized."""
        self.discover("no-size", 0)
        worker = self.worker(1 * GIB)

        report = self.drain(worker)

        self.assertEqual(report.space.refused, [])
        self.assertEqual(self.pipeline.seen, ["no-size"])


class EveryRouteGetsATurnEveryCycle(_Fixture):
    """Eight people, and one of them has just uploaded a morning's worth of calls."""

    ROUTES = routes_for()

    def setUp(self) -> None:
        super().setUp()
        # Forty files from one person, one each from the other seven — the exact shape the
        # oldest-first queue got wrong.
        for n in range(1, 41):
            self.discover(f"alice-{n:02d}", route="alice")
        for name in STAFF[1:]:
            self.discover(f"{name}-01", route=name)
        self.worker_ = self.worker(0, concurrency=8)  # no disk limit: fairness on its own

    def test_the_first_cycle_serves_every_route_and_serves_them_in_order(self) -> None:
        rows = self.worker_.claimable(16)

        served = [row.item_id for row in rows]
        # One from each route in configured order, then round again into the backlog.
        self.assertEqual(served[:8], [
            "alice-01", "ben-01", "cara-01", "dan-01",
            "eve-01", "faz-01", "gus-01", "hana-01",
        ])
        self.assertEqual(served[8:], [f"alice-{n:02d}" for n in range(2, 10)])
        self.assertEqual(
            sorted({row.route for row in rows}), sorted(STAFF),
            "a route was left out of the first cycle",
        )

    def test_no_route_waits_behind_the_backlog_for_more_than_its_turn(self) -> None:
        """The colleague who uploaded one file is done in the first cycle, not the ninth."""
        self.drain(self.worker_, limit=8)

        for name in STAFF[1:]:
            self.assertEqual(self.ledger.get(f"{name}-01").state, State.DONE,
                             f"{name} was still waiting behind the backlog")
        self.assertEqual(len(self.ledger.rows_in_state(State.DONE)), 8)

    def test_a_cycle_with_room_for_two_still_reaches_everybody_within_a_round(self) -> None:
        """The rotation is what stops a small cycle serving the same two routes forever."""
        served: list[str] = []
        for _ in range(8):
            rows = self.worker_.claimable(2)
            served.extend(row.route for row in rows)

        self.assertEqual(sorted(set(served)), sorted(STAFF),
                         "eight small cycles did not reach all eight routes")

    def test_a_route_with_nothing_pending_costs_nobody_a_turn(self) -> None:
        """Three quiet folders must not slow the busy one down by three places."""
        for name in ("ben", "cara", "dan"):
            for row in self.ledger.rows_in_state(State.DISCOVERED, route=name):
                self.ledger.advance(row.item_id, State.DONE)

        served = [row.item_id for row in self.worker_.claimable(8)]

        self.assertEqual(served, [
            "alice-01", "eve-01", "faz-01", "gus-01", "hana-01",
            "alice-02", "alice-03", "alice-04",
        ])

    def test_fairness_survives_the_disk_budget(self) -> None:
        """Room for four recordings must be four people's, not four of one person's."""
        budget = self.worker(4 * (int(HOUR_LONG * 2.2) + 16 * MIB) + MIB, concurrency=8)

        report = self.drain(budget)

        self.assertEqual(len(self.pipeline.seen), 4)
        routes = [self.ledger.get(item).route for item in self.pipeline.seen]
        self.assertEqual(len(set(routes)), 4, f"the four admitted were {routes}")
        self.assertGreater(report.space.held, 0)
        # And what was held is intact and still first in line.
        for row in self.ledger.claimable(limit=100, now=0.0):
            self.assertEqual(row.attempts, 0)
            self.assertIsNone(row.quarantine_reason)

    def test_every_recording_is_transcribed_exactly_once_under_both_guards(self) -> None:
        """The whole point, in one test: eighty files, a tight disk, nothing lost.

        Fairness reorders the queue and the budget shortens it. Neither may drop a row,
        duplicate one, or leave one behind, so this runs the whole burst through both and
        counts what came out the other end against what went in.
        """
        expected = {row.item_id for row in self.ledger.unfinished()}
        self.assertEqual(len(expected), 47)
        worker = self.worker(700 * MIB, concurrency=8)

        cycles = 0
        while self.ledger.claimable(limit=200, now=0.0) and cycles < 200:
            self.drain(worker)
            cycles += 1

        self.assertEqual(set(self.pipeline.seen), expected)
        self.assertEqual(len(self.pipeline.seen), len(expected),
                         "a recording went through the pipeline twice")
        self.assertEqual(self.ledger.unfinished(), [])
        self.assertEqual(len(self.ledger.rows_in_state(State.QUARANTINED)), 0)
        self.assertEqual(len(self.ledger.rows_in_state(State.SKIPPED_EMPTY)), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
