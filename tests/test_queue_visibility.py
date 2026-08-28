"""A backlog and a loss look identical from outside. That confusion is the whole disease.

Forty-two recordings waiting to be transcribed and forty-two recordings that never arrived
are the same number on a screen, and they are completely different mornings: one needs
patience, the other needs somebody to go and look at a phone. Every test here is about the
service saying which of the two it is, in words, without being asked.

The counting is asserted as well as the wording, because a reassuring sentence over a wrong
number is worse than no sentence at all.
"""

from __future__ import annotations

import io
import contextlib
import os
import tempfile
import time
import unittest

from transcriber import __main__ as cli
from transcriber import digest
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, State

from . import support


def _routes() -> tuple[Route, ...]:
    return (
        Route(name="calls", label="Phone calls", source_folder_id="S1",
              output_folder_id="O1", archive_folder_id="", engine="", enabled=True),
        Route(name="site-meetings", label="Site meetings", source_folder_id="S2",
              output_folder_id="O1", archive_folder_id="", engine="", enabled=True),
        Route(name="whatsapp", label="WhatsApp voice notes", source_folder_id="S3",
              output_folder_id="O1", archive_folder_id="", engine="", enabled=True),
    )


class QueueDepthIsCountedPerRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name, routes=_routes())
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def discover(self, route: str, count: int, prefix: str = "rec") -> None:
        self.ledger.record_page(
            [DriveItem(item_id=f"{prefix}-{route}-{n}", name=f"{prefix}_{route}_{n}.m4a")
             for n in range(count)],
            f"cursor-{route}",
            route=route,
        )

    def test_one_persons_forty_files_do_not_hide_everybody_elses(self) -> None:
        self.discover("calls", 40)
        self.discover("site-meetings", 2)

        report = digest.queue_report(self.config, self.ledger)

        self.assertEqual(report.queued, 42)
        depths = {entry.name: entry.queued for entry in report.routes}
        self.assertEqual(depths["calls"], 40)
        self.assertEqual(depths["site-meetings"], 2)
        self.assertEqual(depths["whatsapp"], 0)

    def test_a_finished_recording_leaves_the_queue(self) -> None:
        self.discover("calls", 3)
        self.ledger.advance("rec-calls-0", State.DONE)
        self.ledger.advance("rec-calls-1", State.SKIPPED_EMPTY, skipped_reason="silence")
        self.ledger.quarantine("rec-calls-2", "truncated")

        self.assertEqual(digest.queue_report(self.config, self.ledger).queued, 0)

    def test_what_is_being_worked_on_now_is_told_apart_from_what_is_waiting(self) -> None:
        self.discover("calls", 3)
        self.ledger.claim("rec-calls-0", 900, owner="host:1")

        report = digest.queue_report(self.config, self.ledger)

        self.assertEqual(report.queued, 3)
        self.assertEqual(report.started, 1)

    def test_a_lapsed_claim_is_waiting_again_and_not_counted_as_running(self) -> None:
        """A worker that died mid-job leaves work waiting, not work in progress."""
        self.discover("calls", 1)
        self.ledger.claim("rec-calls-0", 900, owner="host:1", now=time.time() - 4000)

        report = digest.queue_report(self.config, self.ledger)

        self.assertEqual(report.queued, 1)
        self.assertEqual(report.started, 0)

    def test_the_age_of_the_oldest_is_reported(self) -> None:
        self.discover("calls", 1)
        now = time.time() + 3 * 3600

        report = digest.queue_report(self.config, self.ledger, now=now)

        self.assertGreater(report.oldest_age_s, 2.9 * 3600)
        self.assertIn("hours", digest.human_duration(report.oldest_age_s))

    def test_a_route_the_config_forgot_still_has_its_queue_counted(self) -> None:
        """Taking a route out of ROUTES must never make its recordings invisible."""
        self.discover("retired", 2)

        report = digest.queue_report(self.config, self.ledger)

        self.assertEqual(report.queued, 2)
        self.assertIn("retired", {entry.name for entry in report.routes})

    def test_a_ledger_that_cannot_be_read_says_so_rather_than_reporting_zero(self) -> None:
        class BrokenLedger:
            def unfinished(self, route: str | None = None) -> list:
                raise RuntimeError("database is locked")

            def cursor_get(self, name: str) -> str:
                return ""

        report = digest.queue_report(self.config, BrokenLedger())

        self.assertEqual(report.queued, 0)
        self.assertIn("database is locked", report.unavailable)
        self.assertIn("could not be counted", report.headline())
        self.assertNotIn("Nothing is queued", report.headline())


class TheWordingSaysWaitingRatherThanLost(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name, routes=_routes())
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def discover(self, count: int, route: str = "calls") -> str:
        self.ledger.record_page(
            [DriveItem(item_id=f"i{n}", name=f"Call Carel_260827_1200{n:02d}.m4a")
             for n in range(count)],
            "cursor-1",
            route=route,
        )
        return self.ledger.get("i0").discovered_at[:10]

    def test_the_digest_says_queued_and_being_worked_through(self) -> None:
        day = self.discover(42)

        built = digest.build(self.config, self.ledger, day=day)

        self.assertIn("THE QUEUE", built.body)
        self.assertIn("42 recording(s) queued and being worked through", built.body)
        self.assertIn("will be transcribed", built.body)
        self.assertEqual(built.queue.queued, 42)

    def test_the_failure_list_says_which_of_them_are_only_queued(self) -> None:
        """They appear under NEEDS YOU as unfinished; a person must not read that as lost."""
        day = self.discover(5)
        self.ledger.quarantine("i0", "the audio is truncated: no moov index")

        built = digest.build(self.config, self.ledger, day=day)

        self.assertIn("4 of these had not finished by the end of the day rather than failed",
                      built.body)
        self.assertLess(built.body.index("NEEDS YOU"), built.body.index("THE QUEUE"))

    def test_an_empty_queue_is_said_plainly_and_is_not_a_failure(self) -> None:
        day = self.discover(2)
        self.ledger.advance("i0", State.DONE)
        self.ledger.advance("i1", State.DONE)

        built = digest.build(self.config, self.ledger, day=day)

        self.assertIn("Nothing is queued", built.body)
        self.assertFalse(built.queue.short_of_throughput)
        self.assertFalse(built.needs_a_person)

    def test_a_queue_older_than_the_threshold_is_called_out(self) -> None:
        self.discover(3)
        later = time.time() + 40 * 3600

        report = digest.queue_report(self.config, self.ledger, now=later)

        self.assertTrue(report.stale)
        self.assertTrue(report.short_of_throughput)
        self.assertIn("not keeping up", "\n".join(report.lines()))

    def test_a_busy_morning_is_not_called_a_failure_of_throughput(self) -> None:
        """Deep but young, and no worse than yesterday: that is a busy Tuesday."""
        self.discover(30)
        digest.record_queue_depth(self.ledger, "2026-08-26", 40)

        report = digest.queue_report(self.config, self.ledger, day="2026-08-27")

        self.assertFalse(report.stale)
        self.assertFalse(report.growing)
        self.assertFalse(report.short_of_throughput)
        self.assertIn("shorter than it was", "\n".join(report.lines()))

    def test_one_morning_bigger_than_the_last_is_not_yet_a_trend(self) -> None:
        self.discover(10)
        digest.record_queue_depth(self.ledger, "2026-08-26", 8)

        report = digest.queue_report(self.config, self.ledger, day="2026-08-27")

        self.assertTrue(report.growing)
        self.assertFalse(report.growing_across_days)
        self.assertFalse(report.short_of_throughput)

    def test_three_mornings_of_growth_is_reported_as_throughput_running_short(self) -> None:
        self.discover(10)
        digest.record_queue_depth(self.ledger, "2026-08-24", 2)
        digest.record_queue_depth(self.ledger, "2026-08-25", 4)
        digest.record_queue_depth(self.ledger, "2026-08-26", 7)

        report = digest.queue_report(self.config, self.ledger, day="2026-08-27")

        self.assertTrue(report.growing_across_days)
        self.assertTrue(report.short_of_throughput)
        rendered = "\n".join(report.lines())
        self.assertIn("grown every morning for three mornings running", rendered)
        self.assertIn("Nothing is being lost", rendered)

    def test_the_depth_is_only_written_down_when_the_digest_actually_runs(self) -> None:
        """Building a report is a read: a `status` that rewrote history would corrupt it."""
        day = self.discover(4)

        digest.build(self.config, self.ledger, day=day)
        self.assertEqual(digest.queue_history(self.ledger), {})

        digest.run(self.config, self.ledger, day=day, smtp_factory=_no_smtp)
        self.assertEqual(digest.queue_history(self.ledger).get(day), 4)

    def test_the_history_is_kept_short(self) -> None:
        for n in range(1, 25):
            digest.record_queue_depth(self.ledger, f"2026-08-{n:02d}", n)

        history = digest.queue_history(self.ledger)

        self.assertEqual(len(history), digest.QUEUE_HISTORY_DAYS)
        self.assertIn("2026-08-24", history)
        self.assertNotIn("2026-08-01", history)

    def test_a_corrupted_history_is_started_again_rather_than_raising(self) -> None:
        self.ledger.cursor_set(digest.QUEUE_DEPTH_MARK, "not json at all")

        with self.assertLogs("transcriber.digest", level="WARNING"):
            self.assertEqual(digest.queue_history(self.ledger), {})


class StatusPrintsTheQueuePerRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name, routes=_routes())
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.ledger.record_page(
            [DriveItem(item_id=f"c{n}", name=f"call_{n}.m4a") for n in range(40)],
            "cursor-calls", route="calls",
        )
        self.ledger.record_page(
            [DriveItem(item_id=f"s{n}", name=f"site_{n}.m4a") for n in range(2)],
            "cursor-site", route="site-meetings",
        )

    def _printed(self) -> str:
        queue = digest.queue_report(self.config, self.ledger)
        routes = cli._route_status(self.config, self.ledger, self.ledger.stats(), queue)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_route_table(routes)
            cli._print_queue(queue, None)
        return buffer.getvalue()

    def test_each_route_has_its_own_pending_count(self) -> None:
        queue = digest.queue_report(self.config, self.ledger)
        routes = cli._route_status(self.config, self.ledger, self.ledger.stats(), queue)

        by_name = {route["route"]: route for route in routes}
        self.assertEqual(by_name["calls"]["queued"], 40)
        self.assertEqual(by_name["site-meetings"]["queued"], 2)
        self.assertEqual(by_name["whatsapp"]["queued"], 0)

    def test_the_table_carries_a_queued_column(self) -> None:
        printed = self._printed()

        self.assertIn("queued", printed)
        self.assertIn("40", printed)

    def test_it_says_work_in_hand_rather_than_leaving_a_number_to_be_read_as_loss(self) -> None:
        printed = self._printed()

        self.assertIn("42 queued and being worked through", printed)
        self.assertIn("work in hand, not work lost", printed)
        self.assertIn("will be transcribed", printed)

    def test_an_empty_queue_says_so(self) -> None:
        for row in self.ledger.unfinished():
            self.ledger.advance(row.item_id, State.DONE)

        self.assertIn("nothing is queued", self._printed())


def _no_smtp(host: str, port: int, timeout: float = 0.0):
    raise RuntimeError("the suite never sends mail")


if __name__ == "__main__":
    unittest.main()
