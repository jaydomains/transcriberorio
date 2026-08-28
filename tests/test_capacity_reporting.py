"""What a person is told while the service is under pressure.

A backlog and a loss are the same number on a screen. Everything here is about the service
saying which of the two it is — per route, with the age of the oldest recording, and with
the reason the drain is going slowly when it is going slowly. Three surfaces carry it and
all three are asserted: the morning email, ``transcriber status``, and the settings that
have to be refused before they reach either.

The counting is asserted alongside the wording. A reassuring sentence printed over a wrong
number is worse than no sentence, because it is believed.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import os
import tempfile
import unittest

from transcriber import __main__ as cli
from transcriber import config_cmd, digest
from transcriber.config import Config, ConfigError
from transcriber.diskbudget import GIB, MIB, DiskBudget, format_bytes, parse_bytes
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, State, utc_now_iso
from transcriber.worker import (
    WORK_DIR_NOTE,
    WORK_DIR_REFUSED,
    CycleReport,
    Worker,
)

from . import support
from .test_capacity_no_drop import (
    HOUR_LONG,
    STAFF,
    _NoGraph,
    _NoHeartbeat,
    _Pipeline,
    fill,
    routes_for,
)


#: A fixed morning, so "waiting three hours" is arithmetic rather than a wall clock.
NOW = 1787000000.0


def no_smtp(host: str, port: int, timeout: float = 0.0):  # pragma: no cover - never called
    raise RuntimeError("the suite never sends mail")


class _Case(unittest.TestCase):
    ROUTES: tuple[Route, ...] = routes_for()

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.work = os.path.join(self.dir.name, "work")
        os.makedirs(self.work, exist_ok=True)
        self.config = support.make_config(routes=self.ROUTES, work_dir=self.work)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def discover(self, item_id: str, route: str, *, ago_s: float = 0.0,
                 size: int = HOUR_LONG) -> None:
        self.ledger.upsert_discovered(
            DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=size), route
        )
        if ago_s:
            # discovered_at is deliberately not writable through the ledger's own API — it
            # is identity, not a field — so a test that needs an older row writes the column
            # itself rather than pretending the guard is not there.
            conn = self.ledger._conn()
            conn.execute("UPDATE items SET discovered_at=? WHERE item_id=?",
                         (utc_now_iso(NOW - ago_s), item_id))
            conn.commit()

    def report(self, **kwargs) -> digest.QueueReport:
        kwargs.setdefault("now", NOW)
        return digest.queue_report(self.config, self.ledger, **kwargs)


class TheQueueIsCountedRouteByRoute(_Case):
    def test_eight_people_are_eight_counts_not_one_total(self) -> None:
        for n in range(1, 41):
            self.discover(f"alice-{n:02d}", "alice")
        for name in STAFF[1:4]:
            self.discover(f"{name}-01", name)

        report = self.report()

        counts = {entry.name: entry.queued for entry in report.routes}
        self.assertEqual(counts["alice"], 40)
        self.assertEqual(counts["ben"], 1)
        self.assertEqual(counts["cara"], 1)
        self.assertEqual(counts["dan"], 1)
        # A quiet route is listed at zero rather than left out: absent reads as unknown.
        self.assertEqual(counts["hana"], 0)
        self.assertEqual(report.queued, 43)
        self.assertEqual(sorted(counts), sorted(STAFF))

    def test_the_age_of_the_oldest_is_the_age_of_the_oldest(self) -> None:
        self.discover("alice-01", "alice", ago_s=3 * 3600)
        self.discover("alice-02", "alice", ago_s=90)
        self.discover("ben-01", "ben", ago_s=45 * 60)

        report = self.report()

        by_name = {entry.name: entry for entry in report.routes}
        self.assertAlmostEqual(by_name["alice"].oldest_age_s, 3 * 3600, delta=1.0)
        self.assertAlmostEqual(by_name["ben"].oldest_age_s, 45 * 60, delta=1.0)
        self.assertEqual(by_name["alice"].oldest_name, "alice-01.m4a")
        # And for the service as a whole, the oldest of the lot.
        self.assertEqual(report.oldest_name, "alice-01.m4a")
        self.assertEqual(report.oldest_route, "alice")
        self.assertIn("3.0 hours", digest.human_duration(report.oldest_age_s))

    def test_finished_quarantined_and_silent_recordings_are_not_the_queue(self) -> None:
        """The queue is work in hand. Anything terminal has left it, one way or another."""
        for n, state in enumerate((State.DONE, State.QUARANTINED, State.SKIPPED_EMPTY)):
            self.discover(f"gone-{n}", "alice")
            if state == State.QUARANTINED:
                self.ledger.quarantine(f"gone-{n}", "a truncated recording")
            else:
                self.ledger.advance(f"gone-{n}", state)
        self.discover("waiting", "ben")

        report = self.report()

        self.assertEqual(report.queued, 1)
        self.assertEqual(report.oldest_name, "waiting.m4a")

    def test_a_claim_that_lapsed_is_waiting_again_not_being_worked_on(self) -> None:
        self.discover("running", "alice")
        self.discover("stranded", "ben")
        self.ledger.claim("running", 900, owner="worker-1", now=NOW)
        self.ledger.claim("stranded", 900, owner="worker-2", now=NOW - 4000)

        report = self.report()

        self.assertEqual(report.queued, 2)
        self.assertEqual(report.started, 1, "a dead worker's claim was counted as progress")

    def test_the_wording_never_lets_a_queue_be_read_as_a_loss(self) -> None:
        for n in range(1, 43):
            self.discover(f"alice-{n:02d}", "alice", ago_s=600)

        # Flattened: the section is wrapped for an email, and a sentence that falls over a
        # line break is still the sentence a person reads.
        rendered = " ".join("\n".join(self.report().lines()).split())

        self.assertIn("42", rendered)
        self.assertIn("queued and being worked through", rendered)
        self.assertIn("Nothing here is lost or missing", rendered)
        self.assertIn("will be transcribed", rendered)
        for phrase in ("missing from", "never arrived", "disappeared"):
            self.assertNotIn(phrase, rendered)


class TheQueueSaysWhenItIsGenuinelyNotKeepingUp(_Case):
    def test_a_busy_morning_is_not_reported_as_a_problem(self) -> None:
        for n in range(1, 31):
            self.discover(f"alice-{n:02d}", "alice", ago_s=1800)

        report = self.report(day="2026-08-28")

        self.assertFalse(report.stale)
        self.assertFalse(report.short_of_throughput)
        self.assertNotIn("not keeping up", "\n".join(report.lines()))

    def test_a_recording_still_waiting_a_day_later_is_said_plainly(self) -> None:
        self.discover("alice-01", "alice", ago_s=30 * 3600)

        report = self.report()

        self.assertTrue(report.stale)
        self.assertTrue(report.short_of_throughput)
        rendered = "\n".join(report.lines())
        self.assertIn("not moving as fast as recordings are arriving", rendered)
        self.assertIn("Nothing is being lost", rendered)

    def test_three_mornings_of_growth_is_arithmetic_not_a_busy_tuesday(self) -> None:
        for n in range(1, 13):
            self.discover(f"alice-{n:02d}", "alice", ago_s=600)
        digest.record_queue_depth(self.ledger, "2026-08-25", 3)
        digest.record_queue_depth(self.ledger, "2026-08-26", 6)
        digest.record_queue_depth(self.ledger, "2026-08-27", 9)

        report = self.report(day="2026-08-28")

        self.assertTrue(report.growing_across_days)
        self.assertTrue(report.short_of_throughput)
        rendered = "\n".join(report.lines())
        self.assertIn("grown every morning for three mornings running", rendered)
        self.assertIn("more capacity", rendered)

    def test_a_queue_that_is_shrinking_says_that_instead(self) -> None:
        for n in range(1, 5):
            self.discover(f"alice-{n:02d}", "alice", ago_s=600)
        digest.record_queue_depth(self.ledger, "2026-08-25", 20)
        digest.record_queue_depth(self.ledger, "2026-08-26", 30)
        digest.record_queue_depth(self.ledger, "2026-08-27", 40)

        report = self.report(day="2026-08-28")

        self.assertFalse(report.growing_across_days)
        self.assertFalse(report.short_of_throughput)
        self.assertIn("shorter than it was", "\n".join(report.lines()))

    def test_a_deployment_may_set_its_own_threshold(self) -> None:
        """Two hours is a long wait for a voice note and a short one for a batch engine."""
        self.discover("alice-01", "alice", ago_s=3 * 3600)
        impatient = support.make_config(routes=self.ROUTES, work_dir=self.work)
        object.__setattr__(impatient, "queue_stale_hours", 2)

        self.assertTrue(
            digest.queue_report(impatient, self.ledger, now=NOW).stale
        )
        self.assertFalse(self.report().stale)


class TheMorningEmailCarriesTheQueue(_Case):
    #: The rows are discovered by the real clock, so the day being reported is today's.
    DAY = datetime.date.today().isoformat()

    def build(self, day: str | None = None) -> digest.Digest:
        return digest.build(self.config, self.ledger, day=day or self.DAY)

    def test_a_deep_queue_is_broken_down_by_route_in_the_body(self) -> None:
        for n in range(1, 43):
            self.discover(f"alice-{n:02d}", "alice")
        self.discover("ben-01", "ben")

        body = " ".join(self.build().body.split())

        self.assertIn("THE QUEUE", body)
        self.assertIn("43 recording(s) queued", body)
        self.assertIn("Alice (alice): 42 queued", body)
        self.assertIn("Ben (ben): 1 queued", body)
        self.assertIn("Nothing here is lost or missing", body)

    def test_unfinished_recordings_under_needs_you_are_named_as_queued(self) -> None:
        """They appear there because the day ended with them unfinished. That is not a loss.

        This is the one place in the email where a queue and a loss are printed under the
        same heading, so it is the one place the difference has to be spelled out.
        """
        for n in range(1, 4):
            self.discover(f"alice-{n:02d}", "alice")

        body = " ".join(self.build().body.split())

        self.assertIn("NEEDS YOU", body)
        self.assertIn("3 of these had not finished by the end of the day rather than failed",
                      body)
        self.assertIn("they are in the queue", body)
        self.assertIn("nothing about them is lost", body)

    def test_an_empty_queue_is_said_rather_than_left_blank(self) -> None:
        self.discover("alice-01", "alice")
        self.ledger.advance("alice-01", State.DONE)

        body = self.build().body

        self.assertIn("Nothing is queued", body)
        self.assertIn("all 1 done", self.build().subject)

    def test_the_depth_is_written_down_only_when_the_email_actually_goes_out(self) -> None:
        """A `status` or a dry run that rewrote the history would make "growing" a lie."""
        for n in range(1, 6):
            self.discover(f"alice-{n:02d}", "alice")

        self.build()
        self.assertEqual(digest.queue_history(self.ledger), {})

        digest.run(self.config, self.ledger, day=self.DAY, smtp_factory=no_smtp)
        self.assertEqual(digest.queue_history(self.ledger).get(self.DAY), 5)


class TheWorkDirectoryIsReportedWhereSomebodyWillSeeIt(_Case):
    """A drain that claimed nothing because the disk is full must say so out loud.

    The worker writes the sentence into the ledger every drain. That is only worth doing if
    something reads it: a note nobody renders is a log line with extra steps, and "the
    queue stopped moving" would be left looking like the service having died.
    """

    ROUTES = routes_for("calls")

    def worker(self, max_bytes: int) -> Worker:
        config = support.make_config(
            routes=self.ROUTES, work_dir=self.work, work_dir_max_bytes=max_bytes,
            concurrency=2,
        )
        self.pipeline = _Pipeline(self.ledger, self.work)
        return Worker(config, self.ledger, _NoGraph(), pipeline=self.pipeline,
                      heartbeat=_NoHeartbeat(),
                      disk=DiskBudget(self.work, max_bytes, ttl_s=0.0))

    def drain(self, worker: Worker) -> CycleReport:
        report = CycleReport()
        report.outcomes = worker.drain(report=report)
        return report

    def test_a_full_work_directory_reaches_the_morning_email(self) -> None:
        fill(os.path.join(self.work, "items", "kept-for-retry", "audio.m4a"), 400 * MIB)
        self.discover("call-1", "calls")
        self.drain(self.worker(256 * MIB))

        body = digest.build(self.config, self.ledger,
                             day=datetime.date.today().isoformat()).body

        flat = " ".join(body.split())
        self.assertIn("work directory", flat)
        self.assertIn("no new recording is being started", flat)
        self.assertIn("Nothing has been dropped", flat)
        # Said in the queue section, where "why is nothing moving?" is asked.
        self.assertIn("THE QUEUE", body)

    def test_a_recording_too_large_for_the_budget_reaches_the_morning_email(self) -> None:
        self.discover("marathon", "calls", size=3 * GIB)
        self.drain(self.worker(1 * GIB))

        body = digest.build(self.config, self.ledger,
                             day=datetime.date.today().isoformat()).body

        flat = " ".join(body.split())
        self.assertIn("WORK_DIR_MAX_BYTES", flat)
        self.assertIn("marathon.m4a", flat)
        self.assertIn("cannot be started at all", flat)

    def test_status_prints_the_same_sentence(self) -> None:
        fill(os.path.join(self.work, "items", "kept-for-retry", "audio.m4a"), 400 * MIB)
        self.discover("call-1", "calls")
        self.drain(self.worker(256 * MIB))

        printed = self.printed_status()

        self.assertIn("work directory", printed)
        self.assertIn("1 queued", printed)

    def test_nothing_is_said_about_the_disk_when_there_is_nothing_to_say(self) -> None:
        self.discover("call-1", "calls")
        self.drain(self.worker(4 * GIB))

        printed = self.printed_status()
        body = digest.build(self.config, self.ledger,
                             day=datetime.date.today().isoformat()).body

        self.assertEqual(self.ledger.cursor_get(WORK_DIR_REFUSED), "")
        self.assertNotIn("no new recording is being started", " ".join(printed.split()))
        self.assertNotIn("no new recording is being started", " ".join(body.split()))
        self.assertEqual(self.ledger.cursor_get(WORK_DIR_NOTE), "")

    def test_the_note_the_worker_wrote_is_the_note_that_is_read(self) -> None:
        """One key, written in one module and read in another: drift would silence it."""
        fill(os.path.join(self.work, "items", "kept-for-retry", "audio.m4a"), 400 * MIB)
        self.discover("call-1", "calls")
        self.drain(self.worker(256 * MIB))

        written = self.ledger.cursor_get(WORK_DIR_NOTE) or ""

        self.assertTrue(written)
        self.assertIn(written.split(",")[0], self.report().work_dir)
        self.assertEqual(WORK_DIR_NOTE, digest.WORK_DIR_MARK)
        self.assertEqual(WORK_DIR_REFUSED, digest.WORK_DIR_REFUSED_MARK)

    def printed_status(self) -> str:
        queue = self.report()
        routes = cli._route_status(self.config, self.ledger, self.ledger.stats(), queue)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_route_table(routes)
            cli._print_queue(queue, None)
        return buffer.getvalue()


class StatusShowsEachPersonsQueue(_Case):
    def test_per_route_pending_counts_and_the_oldest_wait(self) -> None:
        for n in range(1, 41):
            self.discover(f"alice-{n:02d}", "alice", ago_s=2 * 3600)
        self.discover("ben-01", "ben", ago_s=120)

        queue = self.report()
        routes = cli._route_status(self.config, self.ledger, self.ledger.stats(), queue)

        by_name = {route["route"]: route for route in routes}
        self.assertEqual(by_name["alice"]["queued"], 40)
        self.assertEqual(by_name["ben"]["queued"], 1)
        self.assertEqual(by_name["cara"]["queued"], 0)
        self.assertAlmostEqual(by_name["alice"]["oldest_queued_age_s"], 7200, delta=2.0)

    def test_the_printed_table_says_work_in_hand_not_a_bare_number(self) -> None:
        for n in range(1, 41):
            self.discover(f"alice-{n:02d}", "alice", ago_s=2 * 3600)
        self.discover("ben-01", "ben", ago_s=120)

        queue = self.report()
        routes = cli._route_status(self.config, self.ledger, self.ledger.stats(), queue)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_route_table(routes)
            cli._print_queue(queue, None)
        printed = buffer.getvalue()

        self.assertIn("queued", printed)
        self.assertIn("41 queued and being worked through", printed)
        self.assertIn("work in hand, not work lost", printed)
        self.assertIn("alice", printed)


class SizesAreReadTheWayPeopleWriteThem(unittest.TestCase):
    def test_the_forms_a_person_actually_types(self) -> None:
        for text, expected in (
            ("4GiB", 4 * GIB),
            ("4 GiB", 4 * GIB),
            ("4gib", 4 * GIB),
            ("500MB", 500_000_000),
            ("500 mb", 500_000_000),
            ("1048576", 1024 * 1024),
            ("1_000_000", 1_000_000),
            ("2.5GiB", int(2.5 * GIB)),
            ("512KiB", 512 * 1024),
            ("0", 0),
            ("  8g  ", 8 * GIB),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_bytes(text), expected)

    def test_the_two_conventions_are_not_confused_with_each_other(self) -> None:
        """MB is a million bytes on the side of the tin; MiB is 1048576. Both are real."""
        self.assertEqual(parse_bytes("1MB"), 1_000_000)
        self.assertEqual(parse_bytes("1MiB"), 1_048_576)
        self.assertNotEqual(parse_bytes("1GB"), parse_bytes("1GiB"))

    def test_something_that_is_not_a_size_is_refused_with_an_example(self) -> None:
        for text in ("as much as it needs", "4 potatoes", "-1", "lots", "GiB", "4GiB4"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as caught:
                    parse_bytes(text)
                self.assertTrue(str(caught.exception))

    def test_a_size_is_printed_in_the_units_the_budget_is_set_in(self) -> None:
        self.assertEqual(format_bytes(4 * GIB), "4.0 GiB")
        self.assertEqual(format_bytes(256 * MIB), "256.0 MiB")
        self.assertEqual(format_bytes(0), "0 bytes")


class TheNewSettingsAreRefusedBeforeTheyCanBreakAMorning(unittest.TestCase):
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

    def config(self, **extra: str) -> Config:
        return Config.from_env(dict(self.env, **extra))

    def test_the_defaults_are_the_ones_that_change_nothing_for_one_person(self) -> None:
        config = self.config()

        self.assertEqual(config.work_dir_max_bytes, 4 * GIB)
        self.assertEqual(config.engine_max_concurrent, 3)
        self.assertEqual(config.engine_max_per_minute, 0)

    def test_a_human_size_is_accepted_and_zero_means_no_limit(self) -> None:
        self.assertEqual(self.config(WORK_DIR_MAX_BYTES="8GiB").work_dir_max_bytes, 8 * GIB)
        self.assertEqual(self.config(WORK_DIR_MAX_BYTES="0").work_dir_max_bytes, 0)

    def test_a_size_that_is_not_a_size_stops_the_service_starting(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config(WORK_DIR_MAX_BYTES="as much as it needs")

        message = str(caught.exception)
        self.assertIn("WORK_DIR_MAX_BYTES", message)
        self.assertIn("4GiB", message)

    def test_a_budget_too_small_to_work_in_is_refused_with_the_smallest_that_works(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config(WORK_DIR_MAX_BYTES="100MB")

        message = str(caught.exception)
        self.assertIn("WORK_DIR_MAX_BYTES", message)
        self.assertIn("256.0 MiB", message)

    def test_the_engine_limits_are_checked_too(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            self.config(ENGINE_MAX_CONCURRENT="0", ENGINE_MAX_PER_MINUTE="-5")

        message = str(caught.exception)
        self.assertIn("ENGINE_MAX_CONCURRENT", message)
        self.assertIn("ENGINE_MAX_PER_MINUTE", message)

    def test_every_problem_is_listed_in_one_pass(self) -> None:
        """An operator who has to restart once per mistake stops reading the message."""
        with self.assertRaises(ConfigError) as caught:
            self.config(WORK_DIR_MAX_BYTES="1MB", ENGINE_MAX_CONCURRENT="0",
                        CONCURRENCY="0")

        message = str(caught.exception)
        for variable in ("WORK_DIR_MAX_BYTES", "ENGINE_MAX_CONCURRENT", "CONCURRENCY"):
            self.assertIn(variable, message)


class ConfigSetRefusesWhatStartupWouldRefuse(unittest.TestCase):
    """``config set`` exists so a bad value is caught now, not at 06:00 on a Tuesday.

    A setting it accepts and the service then refuses to start on is the exact failure this
    command was written to prevent, so every rule ``Config.from_env`` has must be a rule
    here too.
    """

    def test_a_size_that_is_not_a_size_is_refused(self) -> None:
        problem = config_cmd.check_value("WORK_DIR_MAX_BYTES", "as much as it needs", {})

        self.assertTrue(problem, "config set would have written a value that stops startup")
        self.assertIn("WORK_DIR_MAX_BYTES", problem)

    def test_a_budget_below_the_workable_minimum_is_refused(self) -> None:
        problem = config_cmd.check_value("WORK_DIR_MAX_BYTES", "100MB", {})

        self.assertTrue(problem)
        self.assertIn("256.0 MiB", problem)

    def test_the_sizes_that_do_work_are_accepted(self) -> None:
        for value in ("4GiB", "500MB", "1073741824", "0"):
            with self.subTest(value=value):
                self.assertEqual(config_cmd.check_value("WORK_DIR_MAX_BYTES", value, {}), "")

    def test_the_engine_limits_keep_their_ranges(self) -> None:
        self.assertTrue(config_cmd.check_value("ENGINE_MAX_CONCURRENT", "0", {}))
        self.assertTrue(config_cmd.check_value("ENGINE_MAX_PER_MINUTE", "-1", {}))
        self.assertEqual(config_cmd.check_value("ENGINE_MAX_CONCURRENT", "8", {}), "")
        self.assertEqual(config_cmd.check_value("ENGINE_MAX_PER_MINUTE", "0", {}), "")

    def test_anything_it_accepts_starts_the_service(self) -> None:
        """The two validators are asserted against each other, not against a list."""
        base = dict(TheNewSettingsAreRefusedBeforeTheyCanBreakAMorning.BASE)
        for value in ("4GiB", "500MB", "0", "256MiB"):
            with self.subTest(value=value):
                if config_cmd.check_value("WORK_DIR_MAX_BYTES", value, {}):
                    continue
                config = Config.from_env(dict(base, WORK_DIR_MAX_BYTES=value))
                self.assertEqual(config.work_dir_max_bytes, parse_bytes(value))

    def test_what_it_shows_is_readable_rather_than_a_row_of_digits(self) -> None:
        self.assertEqual(config_cmd.SETTINGS["WORK_DIR_MAX_BYTES"].default, "4.0 GiB")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
