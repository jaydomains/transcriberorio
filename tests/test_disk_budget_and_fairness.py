"""Two capacity guards, and the one rule both of them serve: slow down, never drop.

Eight members of staff record into eight folders and eighty files can arrive in one poll.
Nothing about that is allowed to lose a recording, so both guards are tested for the same
property twice over: what they hold back stays claimable, in the state it was in, and is
picked up on a later cycle.

  * **the work directory's budget** — measured before anything is claimed, so a burst of
    hour-long calls slows the drain down rather than filling a small VM's disk halfway
    through a download. Over budget is a reportable state, not an error and not a
    quarantine. The one thing that needs a person is a recording whose own working set is
    bigger than the whole budget: waiting cannot fix that, so it says so by name;
  * **fairness across routes** — claimable rows are oldest first, and oldest-first across
    eight people means one person's forty uploads bury the other seven. The drain
    interleaves the routes instead, deterministically, so every route makes progress every
    cycle and a test can assert the exact order.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber import diskbudget
from transcriber.config import Config, ConfigError
from transcriber.diskbudget import DiskBudget, NO_ROOM, OVER_BUDGET, TOO_LARGE
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, State
from transcriber.worker import CycleReport, Worker, interleave_routes

from . import support


MIB = diskbudget.MIB
GIB = diskbudget.GIB

CALLS = Route(name="calls", label="Phone calls",
              source_folder_id="S-CALLS", output_folder_id="O-CALLS")
SITE = Route(name="site-meetings", label="Site meetings",
             source_folder_id="S-SITE", output_folder_id="O-SITE")
WHATSAPP = Route(name="whatsapp", label="WhatsApp voice notes",
                 source_folder_id="S-WA", output_folder_id="O-WA")


class _NoGraph:
    """Nothing in these tests polls; a Graph call here would be a test that lies."""

    def delta_with_resync(self, *_args, **_kwargs):  # pragma: no cover - never called
        raise AssertionError("the drain must not poll")


class _NoHeartbeat:
    configured = False

    def success(self, note: str = ""): return None
    def start(self, note: str = ""): return None
    def fail(self, note: str = ""): return None
    def log(self, note: str = ""): return None


class _RecordingPipeline:
    """Takes whatever it is given and remembers the order, without touching a network."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.seen: list[str] = []

    def process_one(self, row):
        self.seen.append(row.item_id)
        return type("Outcome", (), {
            "item_id": row.item_id, "name": row.name, "result": "done", "state": row.state,
            "reason": "", "route": row.route, "elapsed_s": 0.0, "ok": True,
            "needs_a_person": False, "line": lambda self=None: "done",
        })()


def _fill(path: str, size: int) -> None:
    """A file of exactly this size, sparse — the measurement reads the size, not the bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


class ASizeIsReadTheWayPeopleWriteIt(unittest.TestCase):
    def test_the_human_forms_and_a_plain_integer(self) -> None:
        self.assertEqual(diskbudget.parse_bytes("4GiB"), 4 * GIB)
        self.assertEqual(diskbudget.parse_bytes("500MB"), 500_000_000)
        self.assertEqual(diskbudget.parse_bytes("512 KiB"), 512 * 1024)
        self.assertEqual(diskbudget.parse_bytes("2.5g"), int(2.5 * GIB))
        self.assertEqual(diskbudget.parse_bytes("4294967296"), 4 * GIB)
        self.assertEqual(diskbudget.parse_bytes(4 * GIB), 4 * GIB)
        self.assertEqual(diskbudget.parse_bytes(""), 0)

    def test_something_that_is_not_a_size_says_what_a_size_looks_like(self) -> None:
        with self.assertRaises(ValueError) as caught:
            diskbudget.parse_bytes("as much as it needs")
        self.assertIn("4GiB", str(caught.exception))

    def test_a_unit_nobody_uses_names_the_ones_that_work(self) -> None:
        with self.assertRaises(ValueError) as caught:
            diskbudget.parse_bytes("4 potatoes")
        self.assertIn("GiB", str(caught.exception))


class TheNewSettingsAreValidatedWithEverythingElse(unittest.TestCase):
    """All problems at once, or an operator restarts once per mistake."""

    BASE = {
        "GRAPH_TENANT_ID": "t", "GRAPH_CLIENT_ID": "c", "GRAPH_CLIENT_SECRET": "s",
        "GRAPH_USER_ID": "u", "SOURCE_FOLDER_ID": "S", "OUTPUT_FOLDER_ID": "O",
        "TRANSCRIBE_ENGINE": "openai", "OPENAI_API_KEY": "k", "SMTP_HOST": "h",
        "SMTP_USER": "u", "SMTP_PASSWORD": "p", "SMTP_FROM": "f", "SMTP_TO": "t",
        "HEARTBEAT_URL": "https://example.invalid/hb", "LEDGER_PATH": ":memory:",
    }

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = dict(self.BASE, WORK_DIR=os.path.join(self.dir.name, "work"))

    def test_the_defaults_are_right_for_one_person(self) -> None:
        """Nothing changes for the current deployment until he turns something up."""
        config = Config.from_env(self.env)

        self.assertEqual(config.work_dir_max_bytes, diskbudget.DEFAULT_WORK_DIR_MAX_BYTES)
        self.assertEqual(config.engine_max_concurrent, 3)
        self.assertEqual(config.engine_max_per_minute, 0)

    def test_a_human_size_is_accepted(self) -> None:
        config = Config.from_env(dict(self.env, WORK_DIR_MAX_BYTES="500MB"))
        self.assertEqual(config.work_dir_max_bytes, 500_000_000)

    def test_zero_means_no_limit_and_is_not_a_problem(self) -> None:
        config = Config.from_env(dict(self.env, WORK_DIR_MAX_BYTES="0"))
        self.assertEqual(config.work_dir_max_bytes, 0)

    def test_every_problem_is_reported_in_one_pass(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            Config.from_env(dict(
                self.env,
                WORK_DIR_MAX_BYTES="10MB",
                ENGINE_MAX_CONCURRENT="0",
                ENGINE_MAX_PER_MINUTE="-1",
            ))

        problems = " | ".join(caught.exception.problems)
        self.assertIn("WORK_DIR_MAX_BYTES", problems)
        self.assertIn("ENGINE_MAX_CONCURRENT", problems)
        self.assertIn("ENGINE_MAX_PER_MINUTE", problems)
        self.assertEqual(len(caught.exception.problems), 3, problems)


class TheWorkDirectoryIsMeasuredCheaply(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = 1000.0
        self.budget = DiskBudget(self.dir.name, 4 * GIB, ttl_s=5.0, clock=lambda: self.now)

    def test_it_adds_up_what_is_actually_there(self) -> None:
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 3 * MIB)
        _fill(os.path.join(self.dir.name, "items", "B", "call.m4a"), 5 * MIB)
        _fill(os.path.join(self.dir.name, "readback.tmp"), 1 * MIB)

        usage = self.budget.usage()

        self.assertEqual(usage.used_bytes, 9 * MIB)
        self.assertEqual(usage.items, 2)
        self.assertTrue(usage.complete)

    def test_a_missing_work_directory_is_empty_not_an_error(self) -> None:
        budget = DiskBudget(os.path.join(self.dir.name, "never-created"), 4 * GIB)
        self.assertEqual(budget.usage().used_bytes, 0)

    def test_a_second_look_within_the_ttl_does_not_walk_the_tree_again(self) -> None:
        """The claim path asks this constantly; walking eighty directories each time is
        more expensive than the thing it is guarding."""
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 3 * MIB)
        self.assertEqual(self.budget.usage().used_bytes, 3 * MIB)

        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 9 * MIB)

        self.assertEqual(self.budget.usage().used_bytes, 3 * MIB, "the cache should hold")
        self.now += 6.0
        self.assertEqual(self.budget.usage().used_bytes, 9 * MIB, "the TTL should expire")

    def test_a_finished_recording_frees_its_space_immediately(self) -> None:
        """``forget`` is what the worker calls as each recording ends: the next claim has to
        see the space back rather than wait out a cache."""
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 3 * MIB)
        self.budget.usage()

        import shutil
        shutil.rmtree(os.path.join(self.dir.name, "items", "A"))
        self.budget.forget("A")

        self.assertEqual(self.budget.usage().used_bytes, 0)

    def test_a_directory_that_cannot_be_read_is_reported_not_fatal(self) -> None:
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 1 * MIB)
        locked = os.path.join(self.dir.name, "items", "B")
        os.makedirs(locked)
        _fill(os.path.join(locked, "call.m4a"), 1 * MIB)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)

        usage = self.budget.usage()

        if os.geteuid() == 0:  # root reads it anyway; the assertion below is not available
            self.skipTest("running as root, so an unreadable directory cannot be made")
        self.assertFalse(usage.complete)
        self.assertGreaterEqual(usage.used_bytes, 1 * MIB)


class ThereIsRoomOrThereIsAReason(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def budget(self, max_bytes: int) -> DiskBudget:
        return DiskBudget(self.dir.name, max_bytes, ttl_s=0.0)

    def test_room_is_room(self) -> None:
        decision = self.budget(1 * GIB).check(needed_bytes=100 * MIB)
        self.assertTrue(decision.ok)

    def test_over_budget_says_so_in_words_and_says_nothing_was_lost(self) -> None:
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 40 * MIB)

        decision = self.budget(32 * MIB).check()

        self.assertFalse(decision.ok)
        self.assertEqual(decision.kind, OVER_BUDGET)
        self.assertIn("no new recording is being started", decision.reason)
        self.assertIn("nothing has been dropped", decision.reason.lower())
        self.assertFalse(decision.permanent, "space frees up as work finishes")

    def test_a_recording_that_does_not_fit_right_now_waits(self) -> None:
        _fill(os.path.join(self.dir.name, "items", "A", "call.m4a"), 900 * MIB)

        decision = self.budget(1 * GIB).check(
            needed_bytes=500 * MIB, what="Call Carel_260827_120055.m4a"
        )

        self.assertEqual(decision.kind, NO_ROOM)
        self.assertIn("Call Carel_260827_120055.m4a", decision.reason)
        self.assertIn("waits", decision.reason)
        self.assertFalse(decision.permanent)

    def test_a_recording_bigger_than_the_whole_budget_says_what_to_change(self) -> None:
        decision = self.budget(1 * GIB).check(
            needed_bytes=4 * GIB, what="BEACH COURT SITE WALK.m4a"
        )

        self.assertEqual(decision.kind, TOO_LARGE)
        self.assertTrue(decision.permanent, "waiting cannot free more than the whole budget")
        self.assertIn("BEACH COURT SITE WALK.m4a", decision.reason)
        self.assertIn("WORK_DIR_MAX_BYTES", decision.reason)
        self.assertIn("untouched", decision.reason)

    def test_no_limit_means_no_refusals(self) -> None:
        """0 is the behaviour every installation had before this existed."""
        budget = self.budget(0)
        self.assertFalse(budget.enabled)
        self.assertTrue(budget.check(needed_bytes=500 * GIB).ok)


class WorkIsInterleavedRouteByRoute(unittest.TestCase):
    """The pure ordering, without a ledger: what fairness across routes actually means."""

    def rows(self, route: str, count: int) -> list:
        return [type("Row", (), {"item_id": f"{route}-{n}", "route": route})()
                for n in range(1, count + 1)]

    def test_one_route_s_backlog_does_not_bury_the_others(self) -> None:
        queues = [
            ("calls", self.rows("calls", 40)),
            ("site-meetings", self.rows("site-meetings", 1)),
            ("whatsapp", self.rows("whatsapp", 1)),
        ]

        picked = [row.item_id for row in interleave_routes(queues, 6)]

        self.assertEqual(
            picked,
            ["calls-1", "site-meetings-1", "whatsapp-1", "calls-2", "calls-3", "calls-4"],
        )

    def test_a_route_with_nothing_pending_costs_no_turn(self) -> None:
        queues = [
            ("calls", self.rows("calls", 2)),
            ("site-meetings", []),
            ("whatsapp", self.rows("whatsapp", 2)),
        ]

        picked = [row.item_id for row in interleave_routes(queues, 4)]

        self.assertEqual(picked, ["calls-1", "whatsapp-1", "calls-2", "whatsapp-2"])

    def test_within_a_route_it_is_still_oldest_first(self) -> None:
        queues = [("calls", self.rows("calls", 3))]
        picked = [row.item_id for row in interleave_routes(queues, 3)]
        self.assertEqual(picked, ["calls-1", "calls-2", "calls-3"])

    def test_the_rotation_is_a_counter_not_a_shuffle(self) -> None:
        """Deterministic: the same queues and the same start always give the same list."""
        def picked(start: int) -> list[str]:
            queues = [("calls", self.rows("calls", 2)),
                      ("site-meetings", self.rows("site-meetings", 2)),
                      ("whatsapp", self.rows("whatsapp", 2))]
            return [row.item_id for row in interleave_routes(queues, 2, start=start)]

        self.assertEqual(picked(0), ["calls-1", "site-meetings-1"])
        self.assertEqual(picked(1), ["site-meetings-1", "whatsapp-1"])
        self.assertEqual(picked(2), ["whatsapp-1", "calls-1"])
        self.assertEqual(picked(3), picked(0), "it wraps, and it repeats exactly")


class TheDrainGivesEveryRouteATurn(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.config = support.make_config(
            routes=(CALLS, SITE, WHATSAPP),
            work_dir=os.path.join(self.dir.name, "work"),
            work_dir_max_bytes=0,
            concurrency=1,
        )
        self.pipeline = _RecordingPipeline(self.ledger)
        self.worker = Worker(self.config, self.ledger, _NoGraph(),
                             pipeline=self.pipeline, heartbeat=_NoHeartbeat())

    def discover(self, route: str, count: int, *, size: int = 1024) -> None:
        for n in range(1, count + 1):
            self.ledger.upsert_discovered(
                DriveItem(item_id=f"{route}-{n}", name=f"{route}-{n}.m4a", size=size), route
            )

    def test_forty_files_from_one_person_do_not_bury_seven_colleagues(self) -> None:
        self.discover("calls", 40)
        self.discover("site-meetings", 1)
        self.discover("whatsapp", 1)

        rows = self.worker.claimable(6)

        self.assertEqual(
            [row.item_id for row in rows],
            ["calls-1", "site-meetings-1", "whatsapp-1", "calls-2", "calls-3", "calls-4"],
        )

    def test_the_next_cycle_starts_with_the_next_route(self) -> None:
        """Otherwise a cycle with room for two recordings serves the same two forever."""
        self.discover("calls", 5)
        self.discover("site-meetings", 5)
        self.discover("whatsapp", 5)

        first = [row.item_id for row in self.worker.claimable(2)]
        second = [row.item_id for row in self.worker.claimable(2)]
        third = [row.item_id for row in self.worker.claimable(2)]

        self.assertEqual(first, ["calls-1", "site-meetings-1"])
        self.assertEqual(second, ["site-meetings-1", "whatsapp-1"])
        self.assertEqual(third, ["whatsapp-1", "calls-1"])

    def test_a_route_taken_out_of_the_configuration_is_still_drained(self) -> None:
        """Removing a route stops it being watched and deletes nothing. Its unfinished
        recordings still have to be finished, or taking a route out loses them."""
        self.discover("calls", 1)
        self.ledger.upsert_discovered(
            DriveItem(item_id="old-1", name="old-1.m4a", size=1024), "retired-route"
        )

        rows = self.worker.claimable(10)

        self.assertEqual(sorted(row.item_id for row in rows), ["calls-1", "old-1"])

    def test_a_named_route_narrows_the_whole_drain_to_it(self) -> None:
        self.discover("calls", 2)
        self.discover("whatsapp", 2)

        rows = self.worker.claimable(10, route="whatsapp")

        self.assertEqual([row.item_id for row in rows], ["whatsapp-1", "whatsapp-2"])


class UnderPressureItSlowsDownAndNeverDrops(unittest.TestCase):
    HOUR_LONG = 58 * 1000 * 1000  # an hour at this recorder's measured 16,240 bytes/second

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "work")
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def worker(self, max_bytes: int) -> Worker:
        config = support.make_config(
            routes=(CALLS, SITE), work_dir=self.work, work_dir_max_bytes=max_bytes,
            concurrency=2,
        )
        self.pipeline = _RecordingPipeline(self.ledger)
        return Worker(config, self.ledger, _NoGraph(), pipeline=self.pipeline,
                      heartbeat=_NoHeartbeat(),
                      disk=DiskBudget(self.work, max_bytes, ttl_s=0.0))

    def discover(self, item_id: str, size: int, route: str = "calls") -> None:
        self.ledger.upsert_discovered(
            DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=size), route
        )

    def drain(self, worker: Worker) -> CycleReport:
        """One cycle's drain, without the poll — there is no Graph in these tests."""
        report = CycleReport()
        report.outcomes = worker.drain(report=report)
        return report

    def test_over_budget_claims_nothing_and_loses_nothing(self) -> None:
        _fill(os.path.join(self.work, "items", "busy", "call.m4a"), 400 * MIB)
        self.discover("A", self.HOUR_LONG)
        self.discover("B", self.HOUR_LONG, route="site-meetings")
        worker = self.worker(256 * MIB)

        report = self.drain(worker)

        self.assertEqual(self.pipeline.seen, [], "nothing may be claimed while over budget")
        self.assertTrue(report.space.over_budget)
        self.assertIn("work directory", report.line())
        # Not an error, not a failed cycle, and nothing was quarantined for it.
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.errors, [])
        for item_id in ("A", "B"):
            row = self.ledger.get(item_id)
            self.assertEqual(row.state, State.DISCOVERED)
            self.assertEqual(row.attempts, 0)
            self.assertIsNone(row.quarantine_reason)
        # And still claimable: the next cycle, with space free, picks them straight up.
        self.assertEqual(
            sorted(r.item_id for r in self.ledger.claimable(now=0.0)), ["A", "B"]
        )

    def test_the_queue_moves_again_once_space_comes_free(self) -> None:
        busy = os.path.join(self.work, "items", "busy", "call.m4a")
        _fill(busy, 400 * MIB)
        self.discover("A", self.HOUR_LONG)
        worker = self.worker(256 * MIB)
        self.drain(worker)
        self.assertEqual(self.pipeline.seen, [])

        os.remove(busy)
        worker.disk.invalidate()
        report = self.drain(worker)

        self.assertEqual(self.pipeline.seen, ["A"])
        self.assertFalse(report.space.over_budget)

    def test_only_as_many_as_there_is_room_for_are_started(self) -> None:
        """The rest are held for the next cycle, not started and starved of disk."""
        for n in range(1, 5):
            self.discover(f"A{n}", self.HOUR_LONG)
        worker = self.worker(512 * MIB)

        report = self.drain(worker)

        need = worker.disk.estimate_for(self.HOUR_LONG)
        self.assertEqual(len(self.pipeline.seen), (512 * MIB) // need)
        self.assertEqual(report.space.held, 4 - len(self.pipeline.seen))
        self.assertIn("waiting for space", report.line())
        self.assertTrue(report.ok)
        # The disk is not full — this cycle's own reservations are what filled the budget,
        # and saying "the work directory holds 500 MiB" of a directory holding nothing yet
        # would send somebody looking at the wrong thing.
        self.assertFalse(report.space.over_budget)
        self.assertIn("waits", report.space.note)
        # And the ones held are untouched, still claimable, and not counted as failures.
        for row in self.ledger.claimable(now=0.0):
            self.assertEqual(row.attempts, 0)
            self.assertIsNone(row.quarantine_reason)

    def test_a_recording_too_large_for_the_budget_is_refused_by_name(self) -> None:
        self.discover("huge", 3 * GIB)
        self.discover("ordinary", self.HOUR_LONG)
        worker = self.worker(1 * GIB)

        report = self.drain(worker)

        self.assertEqual([item for item, _reason in report.space.refused], ["huge"])
        reason = report.space.refused[0][1]
        self.assertIn("huge.m4a", reason)
        self.assertIn("WORK_DIR_MAX_BYTES", reason)
        # Refused, never quarantined and never counted as an attempt: it is a limit that is
        # too low, not a recording that is broken.
        row = self.ledger.get("huge")
        self.assertEqual(row.state, State.DISCOVERED)
        self.assertEqual(row.attempts, 0)
        self.assertIsNone(row.quarantine_reason)
        # And it does not block the ordinary recording queued behind it.
        self.assertEqual(self.pipeline.seen, ["ordinary"])

    def test_the_state_of_the_work_directory_is_written_down_for_the_digest(self) -> None:
        _fill(os.path.join(self.work, "items", "busy", "call.m4a"), 400 * MIB)
        self.discover("A", self.HOUR_LONG)
        worker = self.worker(256 * MIB)

        self.drain(worker)

        note = self.ledger.cursor_get("worker:work_dir") or ""
        self.assertIn("work directory", note)
        self.assertTrue(self.ledger.cursor_get("worker:work_dir_at"))

    def test_a_mark_that_could_only_ever_be_set_would_stay_red_forever(self) -> None:
        self.discover("huge", 3 * GIB)
        worker = self.worker(1 * GIB)
        self.drain(worker)
        self.assertIn("WORK_DIR_MAX_BYTES", self.ledger.cursor_get("worker:work_dir_refused"))

        self.ledger.quarantine("huge", "a person took it away by hand")
        self.drain(worker)

        self.assertEqual(self.ledger.cursor_get("worker:work_dir_refused"), "")

    def test_no_limit_behaves_exactly_as_it_did_before_any_of_this(self) -> None:
        for n in range(1, 4):
            self.discover(f"A{n}", 4 * GIB)
        worker = self.worker(0)

        report = self.drain(worker)

        self.assertEqual(sorted(self.pipeline.seen), ["A1", "A2", "A3"])
        self.assertFalse(report.space.limited)
        self.assertEqual(report.space.refused, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
