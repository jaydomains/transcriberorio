"""Three places the service reported success it had not earned.

The pattern in all three is the same and it is the one this design is most afraid of: a
sentence that reads like good news and is really the absence of a check. A sweep that was
never run, a queue that could not be read, a remedy that does not exist — each of them
looked, from the outside, exactly like everything being fine.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from transcriber import digest, sweep
from transcriber.ledger import Ledger
from transcriber.models import DriveItem


class _Config:
    """The little a sweep needs to decide whether tonight's sweep already happened."""

    sweep_hour = 1
    timezone = "Africa/Johannesburg"

    def __init__(self, ledger_path: str) -> None:
        self.ledger_path = ledger_path


class OneRouteIsNotEveryRoute(unittest.TestCase):
    """`transcriber sweep --route dan` used to cancel that night's sweep for everyone.

    The mark the sweep writes is what ``should_run`` reads, and it silences the sweep for
    the rest of the local day. Writing it after a one-route run meant that checking on one
    person in the morning switched off the backstop for the other seven, silently. The
    sweep is what makes "a recording cannot be missed" true rather than "a recording is
    unlikely to be missed", so switching it off is not a small thing.
    """

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        self.config = _Config(os.path.join(tmp, "ledger.sqlite"))
        # Well after sweep_hour, so should_run turns only on the mark.
        self.noon = _noon_local(self.config)

    def _sweep(self, route):
        report = sweep.SweepReport(started_at="x", finished_at="y", route=route or "one")
        with mock.patch.object(sweep, "select_routes", return_value=((_Route(route or "dan"),), ())), \
             mock.patch.object(sweep, "sweep_route", return_value=report), \
             mock.patch.object(sweep, "_report_unswept_routes"):
            return sweep.sweep(self.config, self.ledger, graph=None, route=route, now=self.noon)

    def test_a_one_route_sweep_does_not_mark_the_night_done(self) -> None:
        self._sweep("dan")
        self.assertIsNone(
            self.ledger.cursor_get(sweep.SWEEP_DAY_MARK),
            "a single-route sweep marked the whole night done, so the nightly sweep of "
            "every other route was skipped without anything saying so",
        )

    def test_and_so_tonights_sweep_is_still_due(self) -> None:
        self._sweep("dan")
        # Past the hour-long back-off that follows any attempt, so what is being asked here
        # is the day mark and nothing else.
        later = self.noon + sweep.RETRY_AFTER_S + 60
        self.assertTrue(sweep.should_run(self.config, self.ledger, now=later))

    def test_a_sweep_of_everything_does_mark_the_night_done(self) -> None:
        self._sweep(None)
        self.assertIsNotNone(self.ledger.cursor_get(sweep.SWEEP_DAY_MARK))
        later = self.noon + sweep.RETRY_AFTER_S + 60
        self.assertFalse(sweep.should_run(self.config, self.ledger, now=later))


class _Route:
    def __init__(self, name: str) -> None:
        self.name = name
        self.label = name
        self.enabled = True


def _noon_local(config) -> float:
    import datetime
    import time as _time

    today = sweep.local_now(config, _time.time()).date()
    noon = datetime.datetime.combine(today, datetime.time(12, 0), sweep.zone_of(config.timezone))
    return noon.timestamp()


class AQueueThatCannotBeReadIsNotAnEmptyQueue(unittest.TestCase):
    """The held store failing to open used to render as "nothing has been held".

    Both arrived as ``None``, so a corrupt file, a permission that changed or a disk fault
    printed the same sentence as a service that has genuinely never held anything. The
    words were never at risk — nothing was read — but the queue's existence stopped being
    reported, and the morning email is the only place anybody would find out.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "held.sqlite")
        with open(self.path, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        ledger_path = os.path.join(self.tmp, "ledger.sqlite")
        self.ledger = Ledger(ledger_path)
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)

        class Config:
            held_store_path = self.path
            gate_mode = "shadow"
            gate_review_base_url = ""
            scrub = None

        Config.ledger_path = ledger_path
        self.config = Config()

    def test_the_report_says_it_could_not_be_read(self) -> None:
        report = digest.held_report(self.config, self.ledger)
        self.assertTrue(
            report.unavailable,
            "an unreadable held queue reported as an empty one, which is the one sentence "
            "this design exists to stop anybody reading when it is not true",
        )

    def test_and_the_email_says_so_in_words(self) -> None:
        rendered = " ".join(digest.held_report(self.config, self.ledger).lines())
        self.assertIn("could not be read", rendered)
        self.assertIn("Nothing has been released", rendered)


class TheRemedyTheEmailNamesActuallyRuns(unittest.TestCase):
    """The morning email has always said `transcriber status --item <id>`.

    Until now that printed "unrecognized arguments: --item". The email is the one surface
    he reads; an instruction in it that errors costs the trust needed to act on the next
    one.
    """

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.path = os.path.join(tmp, "ledger.sqlite")
        self.ledger = Ledger(self.path)
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        item = DriveItem(
            item_id="01ABCDEF", name="BEACH COURT SITE WALK 060826 1622.m4a",
            size=9_400_000, created_at="2026-08-26T14:22:00Z",
            modified_at="2026-08-26T14:22:00Z",
        )
        self.ledger.upsert_discovered(item, route="james")
        self.ledger.quarantine("01ABCDEF", "the transcript does not account for the audio")

    def test_the_option_exists_at_all(self) -> None:
        from transcriber.__main__ import _parser

        args = _parser().parse_args(["status", "--item", "01ABCDEF"])
        self.assertEqual(args.item, "01ABCDEF")

    def test_it_finds_the_recording_by_the_name_the_email_printed(self) -> None:
        rows = self.ledger.find_by_name("BEACH COURT")
        self.assertEqual([r.item_id for r in rows], ["01ABCDEF"])

    def test_and_the_reason_it_stopped_is_in_what_it_prints(self) -> None:
        from transcriber.__main__ import _status_one_item

        import io
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _status_one_item(self.ledger, "01ABCDEF")
        printed = out.getvalue()
        self.assertIn("QUARANTINED", printed)
        self.assertIn("does not account for the audio", printed)


if __name__ == "__main__":
    unittest.main()
