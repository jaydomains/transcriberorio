"""The group view: one email about eight people, carrying counts and nothing else.

Two things are being tested, and the first is a safety property rather than a feature.

**A status file may not carry a name or a word of what was said.** The review page is built
so one person cannot read another's held passages, the principal included, because a staff
member who discovers the boss reads their held words stops keeping a folder — and then the
recordings are gone, which is the loss this service exists to cure. A shared status file
naming recordings would walk around that from the side, and it would do it quietly.

**A copy that has stopped running must be visible.** That is the whole reason the group view
exists: a stopped copy stops writing and stops being able to tell anybody so, and the only
person its own email would have reached is the one person whose record does not suffer.
"""

from __future__ import annotations

import json
import time
import unittest

from transcriber import group
from transcriber.group import GroupReport, PeerStatus

NOW = 1_788_000_000.0        # a fixed moment, so "hours ago" is arithmetic and not a clock
HOUR = 3600.0


def _stamp(offset_hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - offset_hours * HOUR))


class _Config:
    def __init__(self, **kw):
        self.instance_name = kw.get("instance_name", "James")
        self.group_folder_id = kw.get("group_folder_id", "folder-1")
        self.group_admin_to = kw.get("group_admin_to", ())
        self.group_silent_after_hours = kw.get("group_silent_after_hours", 36)


class _Counts:
    day = "2026-09-02"
    discovered = 23
    done = 20
    quarantined = 3
    in_flight = 0
    skipped_empty = 0
    #: The things a status must never pick up, present exactly as the real one carries them.
    failures = ({"item_id": "x", "name": "DISCIPLINARY HEARING NOTES.m4a",
                 "quarantine_reason": "the engine answered 503 on Carel's dismissal call"},)


class _Spend:
    day_usd = 2.37
    month_usd = 47.20
    unpriced = ()


class _Held:
    pending = 2


class _Uploads:
    """A drive that remembers what it was asked to write."""

    def __init__(self, fail: bool = False):
        self.written: dict[str, bytes] = {}
        self.fail = fail

    def upload_small(self, parent_id, name, data, **kw):
        if self.fail:
            raise RuntimeError("the drive said no")
        self.written[name] = data
        return None


class AStatusFileCarriesNoNames(unittest.TestCase):
    def test_the_written_file_contains_no_recording_name_and_no_reason(self) -> None:
        drive = _Uploads()
        status = group.status_of(_Config(), counts=_Counts(), spend=_Spend(), held=_Held())
        self.assertTrue(group.write_status(_Config(), status, client=drive))

        blob = next(iter(drive.written.values())).decode("utf-8")
        for forbidden in ("DISCIPLINARY", "HEARING", "Carel", "dismissal", "503", ".m4a"):
            self.assertNotIn(forbidden, blob, f"{forbidden!r} reached the group folder")
        # And it did carry the count of them, which is the useful half.
        self.assertEqual(json.loads(blob)["failed"], 3)

    def test_every_field_but_the_name_and_the_stamps_is_a_number(self) -> None:
        status = group.status_of(_Config(), counts=_Counts(), spend=_Spend(), held=_Held())
        for key, value in status.to_dict().items():
            if key in ("instance", "day", "written_at"):
                self.assertIsInstance(value, str)
            else:
                self.assertIsInstance(value, (int, float), f"{key} is {value!r}")

    def test_a_status_carrying_prose_is_refused_rather_than_scrubbed(self) -> None:
        """The last of the three layers, and the one a later edit would trip."""
        with self.assertRaises(ValueError) as caught:
            group._check_no_prose({"instance": "James", "failed": 3,
                                   "reason": "Carel's dismissal call"})
        self.assertIn("reason", str(caught.exception))

    def test_an_unexpected_key_is_refused_even_when_it_is_a_number(self) -> None:
        with self.assertRaises(ValueError):
            group._check_no_prose({"instance": "James", "recording_id": 7})

    def test_a_refused_status_is_not_written_and_does_not_raise(self) -> None:
        """Loud in the log, quiet to the caller: the morning email has already gone."""
        drive = _Uploads()
        bad = PeerStatus(instance="James")
        bad.to_dict = lambda: {"instance": "James", "note": "Carel's call"}  # type: ignore
        with self.assertLogs("transcriber.group", level="ERROR"):
            self.assertFalse(group.write_status(_Config(), bad, client=drive))
        self.assertEqual(drive.written, {})


class ItNeverBreaksThePersonalEmail(unittest.TestCase):
    def test_a_drive_that_refuses_the_write_returns_False(self) -> None:
        status = group.status_of(_Config(), counts=_Counts())
        self.assertFalse(group.write_status(_Config(), status, client=_Uploads(fail=True)))

    def test_no_folder_configured_is_simply_off(self) -> None:
        cfg = _Config(group_folder_id="")
        status = group.status_of(cfg, counts=_Counts())
        self.assertFalse(group.write_status(cfg, status, client=_Uploads()))

    def test_no_instance_name_keeps_this_copy_out_of_the_group(self) -> None:
        cfg = _Config(instance_name="")
        status = group.status_of(cfg, counts=_Counts())
        self.assertFalse(group.write_status(cfg, status, client=_Uploads()))

    def test_a_folder_it_cannot_list_reads_as_no_peers(self) -> None:
        class Broken:
            def list_children(self, folder_id):
                raise RuntimeError("gone")

        self.assertEqual(group.read_statuses(_Config(), client=Broken()), [])


class ASilentCopyIsVisible(unittest.TestCase):
    def _report(self, ages: dict[str, float | None]) -> GroupReport:
        peers = []
        for name, age in ages.items():
            peer = PeerStatus(instance=name, day="2026-09-02", arrived=10, done=10,
                              written_at="" if age is None else _stamp(age))
            peer.hours_old = 0.0 if age is None else age
            peers.append(peer)
        return GroupReport(day="2026-09-02", peers=tuple(peers), silent_after_hours=36)

    def test_a_copy_that_has_not_written_for_two_days_is_named(self) -> None:
        report = self._report({"James": 1.0, "Sipho": 48.0, "Nomsa": 2.0})
        self.assertEqual([p.instance for p in report.silent], ["Sipho"])
        self.assertIn("NO WORD FROM Sipho", group.subject_for_group(report))

    def test_a_copy_that_never_reported_is_silent_not_fine(self) -> None:
        report = self._report({"James": 1.0, "Thabo": None})
        self.assertEqual([p.instance for p in report.silent], ["Thabo"])

    def test_silence_outranks_a_failure_in_the_subject(self) -> None:
        """A failure is in somebody's own email. Silence is in nobody's."""
        peers = (
            PeerStatus(instance="James", arrived=5, done=2, failed=3, written_at=_stamp(1)),
            PeerStatus(instance="Sipho", arrived=5, done=5, written_at=_stamp(90)),
        )
        for peer, age in zip(peers, (1.0, 90.0)):
            peer.hours_old = age
        report = GroupReport(day="2026-09-02", peers=peers, silent_after_hours=36)
        self.assertIn("NO WORD FROM", group.subject_for_group(report))

    def test_a_good_morning_still_sends_and_says_so(self) -> None:
        report = self._report({"James": 1.0, "Sipho": 2.0})
        subject = group.subject_for_group(report)
        self.assertIn("all 20 done", subject)
        self.assertNotIn("⚠", subject)

    def test_nobody_reporting_at_all_is_its_own_message(self) -> None:
        report = GroupReport(day="2026-09-02", peers=())
        self.assertIn("no copy has reported yet", group.subject_for_group(report))


class TheEmailItselfCarriesNoNames(unittest.TestCase):
    def test_the_rendered_body_names_people_and_no_recordings(self) -> None:
        peers = []
        for name, failed in (("James", 3), ("Sipho", 0)):
            peer = PeerStatus(instance=name, day="2026-09-02", arrived=23, done=20,
                              failed=failed, held_pending=2, spend_day_usd=2.37,
                              spend_month_usd=47.20, written_at=_stamp(1))
            peer.hours_old = 1.0
            peers.append(peer)
        body = group.render_group_email(
            GroupReport(day="2026-09-02", peers=tuple(peers)), priced_on="2026-06-24")

        self.assertIn("James", body)
        self.assertIn("Sipho", body)
        self.assertIn("$94.40", body)          # both months summed
        self.assertIn("2026-06-24", body)
        self.assertIn("no recording names", body)
        self.assertNotIn(".m4a", body)

    def test_a_silent_copys_row_says_which_day_its_numbers_are_from(self) -> None:
        """"Sipho: 9 arrived, 9 done, nothing wrong" is the most reassuring possible way
        to display a machine that has been dead since Monday."""
        fresh = PeerStatus(instance="James", day="2026-09-02", arrived=23, done=20,
                           written_at=_stamp(1))
        fresh.hours_old = 1.0
        stale = PeerStatus(instance="Sipho", day="2026-08-31", arrived=9, done=9,
                           written_at=_stamp(52))
        stale.hours_old = 52.0
        body = group.render_group_email(
            GroupReport(day="2026-09-02", peers=(fresh, stale), silent_after_hours=36))

        row = next(line for line in body.splitlines() if "Sipho" in line and "9" in line)
        self.assertIn("STALE", row)
        self.assertIn("2026-08-31", row)
        # And James's row, which is current, carries no such mark.
        james = next(line for line in body.splitlines() if "James" in line and "23" in line)
        self.assertNotIn("STALE", james)

    def test_the_totals_admit_they_include_a_stale_row(self) -> None:
        stale = PeerStatus(instance="Sipho", day="2026-08-31", arrived=9, done=9,
                           written_at=_stamp(52))
        stale.hours_old = 52.0
        body = group.render_group_email(GroupReport(day="2026-09-02", peers=(stale,)))
        self.assertIn("includes the stale rows", body)

    def test_an_unpriced_model_anywhere_makes_the_group_total_a_stated_undercount(self) -> None:
        peer = PeerStatus(instance="James", spend_month_usd=10.0, unpriced=True,
                          written_at=_stamp(1))
        peer.hours_old = 1.0
        body = group.render_group_email(GroupReport(day="2026-09-02", peers=(peer,)))
        self.assertIn("UNDERCOUNT", body.upper())


class TheAdminRoleIsAssigned(unittest.TestCase):
    def test_a_copy_is_the_admin_because_it_was_given_somebody_to_tell(self) -> None:
        self.assertFalse(group.is_admin(_Config(group_admin_to=())))
        self.assertTrue(group.is_admin(_Config(group_admin_to=("boss@example.com",))))

    def test_the_filename_is_stable_across_edits_to_the_name(self) -> None:
        """It overwrites rather than accumulating a file per spelling."""
        self.assertEqual(group.status_filename("James"), group.status_filename("james"))
        self.assertEqual(group.status_filename("James "), group.status_filename("James"))

    def test_a_name_cannot_address_another_folder(self) -> None:
        self.assertEqual(group.status_filename("../../etc/passwd"), "status-etcpasswd.json")
        self.assertEqual(group.status_filename(""), "status-unnamed.json")


class AStatusFileFromAnotherVersionIsToleratedNotFatal(unittest.TestCase):
    def test_unknown_keys_are_ignored(self) -> None:
        got = PeerStatus.from_dict({"instance": "James", "done": 4, "something_new": 9})
        self.assertIsNotNone(got)
        self.assertEqual(got.done, 4)

    def test_rubbish_values_read_as_zero(self) -> None:
        got = PeerStatus.from_dict({"instance": "James", "done": "lots", "failed": None,
                                    "spend_day_usd": "free"})
        self.assertEqual((got.done, got.failed, got.spend_day_usd), (0, 0, 0.0))

    def test_a_file_with_no_instance_name_is_not_a_status(self) -> None:
        self.assertIsNone(PeerStatus.from_dict({"done": 4}))
        self.assertIsNone(PeerStatus.from_dict({"instance": "  ", "done": 4}))

    def test_an_absurdly_long_name_is_refused(self) -> None:
        self.assertIsNone(PeerStatus.from_dict({"instance": "x" * 500}))


if __name__ == "__main__":
    unittest.main()
