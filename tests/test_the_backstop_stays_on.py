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


class _AcceptingSMTP:
    """SMTP that takes the message and sends nothing anywhere."""

    def __enter__(self) -> "_AcceptingSMTP":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, *a: object, **k: object) -> None:
        return None

    def login(self, *a: object, **k: object) -> None:
        return None

    def send_message(self, *a: object, **k: object) -> None:
        return None

    def sendmail(self, *a: object, **k: object) -> None:
        return None

    def quit(self) -> None:
        return None


def _accepting_smtp(*a: object, **k: object) -> _AcceptingSMTP:
    return _AcceptingSMTP()


if __name__ == "__main__":
    unittest.main()


class LookingUpARecordingByNameIsExact(unittest.TestCase):
    """`status --item` takes a filename, and a filename can contain LIKE's own characters.

    The lookup wraps the needle in wildcards to make it a "contains" search. The needle's
    own `%` and `_` were left as wildcards too, so searching for "100%" matched every
    recording in the ledger, and the caller then asked which of them was meant.
    """

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        for n, name in enumerate((
            "100% SNAG LIST.m4a", "BEACH COURT WALK.m4a", "ANYTHING AT ALL.m4a", "A_B NOTE.m4a",
        )):
            self.ledger.upsert_discovered(
                DriveItem(item_id=f"0{n}", name=name, size=10,
                          created_at="2026-08-26T09:00:00Z", modified_at="2026-08-26T09:00:00Z"),
                route="james",
            )

    def _names(self, needle: str) -> list[str]:
        return [r.name for r in self.ledger.find_by_name(needle)]

    def test_a_percent_in_the_name_is_not_a_wildcard(self) -> None:
        self.assertEqual(self._names("%"), ["100% SNAG LIST.m4a"])

    def test_an_underscore_is_not_a_single_character_wildcard(self) -> None:
        self.assertEqual(self._names("A_B"), ["A_B NOTE.m4a"])
        self.assertEqual(self._names("AXB"), [])

    def test_a_lone_backslash_does_not_eat_the_escape(self) -> None:
        self.assertEqual(self._names("\\"), [])

    def test_and_an_ordinary_fragment_still_finds_its_recording(self) -> None:
        self.assertEqual(self._names("BEACH"), ["BEACH COURT WALK.m4a"])


class WhatTheRecordIsToldAboutWhyItWasRead(unittest.TestCase):
    """The sentence in the actions file, which goes into the record and stays there.

    A router outage escalates to a full read — the right behaviour, since a router that
    cannot be reached must never mean a recording is skipped. But the sentence explaining it
    asserted a judgement no model had made, and with nothing to list it broke mid-clause.
    """

    def _routing(self, **kw):
        from transcriber.extract import Routing

        base = dict(label="substantive", forced=True, triggers=(), escalated=True,
                    model="cheap", notes=())
        base.update(kw)
        return Routing(**base)

    def test_an_outage_is_not_reported_as_a_judgement(self) -> None:
        said = self._routing(model_label="unavailable", model_reason="503 from the provider").why()
        self.assertIn("could not be reached", said)
        self.assertNotIn(
            "called this trivial", said,
            "the record was told the model judged this recording, when it was unreachable",
        )

    def test_and_no_sentence_breaks_off_mid_clause(self) -> None:
        for label in ("unavailable", "trivial"):
            with self.subTest(model_label=label):
                said = self._routing(model_label=label).why()
                self.assertNotIn(
                    "because it ,", said,
                    "an empty reason list left a half-written sentence in the record",
                )
                self.assertTrue(said.endswith(("full", "anyway", "skipped")), said)

    def test_a_real_override_still_says_what_it_saw(self) -> None:
        from transcriber.extract import SafetyTrigger

        said = self._routing(
            model_label="trivial",
            triggers=(SafetyTrigger("money", "R40k", "names an amount"),),
        ).why()
        self.assertIn("the safety check disagreed", said)
        self.assertIn("names an amount", said)


class ReadingBackAnOldMorningDoesNotCancelThisOne(unittest.TestCase):
    """`transcriber digest --day <an older day>` used to suppress today's real email.

    The mark that says "today's digest has gone out" is stamped with TODAY's date whatever
    day was asked for. So looking back at an older morning — which is the entire purpose of
    the option — told the service this morning was already done, and the 06:00 email never
    went out. The scheduled run passes no day at all, so it is unaffected either way.
    """

    def setUp(self) -> None:
        import datetime as _dt

        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        self.today = digest.local_now(_Config(""), None).date().isoformat()
        self.old_day = (
            _dt.date.fromisoformat(self.today) - _dt.timedelta(days=6)
        ).isoformat()

    def test_reading_back_an_old_day_leaves_this_morning_still_due(self) -> None:
        from . import support

        digest.run(support.make_config(), self.ledger, day=self.old_day,
                   smtp_factory=_accepting_smtp)
        self.assertIsNone(
            self.ledger.cursor_get(digest.DIGEST_DAY_MARK),
            "looking at an older morning marked TODAY as sent, so the real 06:00 email "
            "for today never went out",
        )

    def test_but_the_scheduled_run_still_marks_the_day(self) -> None:
        from . import support

        digest.run(support.make_config(), self.ledger, smtp_factory=_accepting_smtp)
        self.assertEqual(self.ledger.cursor_get(digest.DIGEST_DAY_MARK), self.today)

    def test_todays_date_is_what_the_mark_would_carry(self) -> None:
        """The mark is date-stamped from the clock, not from the day that was asked for.

        This is the mechanism, pinned so that a future change cannot quietly make the
        stamp follow the requested day and reintroduce the other half of the bug.
        """
        digest.mark_run(_Config(""), self.ledger)
        self.assertEqual(self.ledger.cursor_get(digest.DIGEST_DAY_MARK), self.today)
        self.assertNotEqual(self.old_day, self.today)
