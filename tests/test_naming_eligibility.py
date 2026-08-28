"""The one filename this service may touch, and the timestamp it was throwing away.

Two things are proved here, and they guard the two failures that cost the most.

**He named it, so it is his.** A recording that still carries the voice recorder's own
default name — ``Voice 260806_162219.m4a`` — may be given a worked-out title. *Every other
name is his*, and a service that retitles what a person chose is a service he switches off,
after which he is back to losing recordings. ``CJ.m4a``, ``Q.m4a``, ``JORDS.m4a``, ``ALB.m4a``
and ``Morne Interview.m4a`` all look nameless to a machine and are not: they are what he
calls those people. The table in :class:`OnlyTheRecordersOwnDefaultNameIsEligible` runs the
real names off his drive and the near-misses that surround them through the real rule.

**The timestamp fix.** ``Voice 260806_162219.m4a`` writes the same machine ``YYMMDD_HHMMSS``
the call form writes, but with a SPACE where the call form has an underscore — so it fell
through to the hand-typed branch and every unnamed recording was dated by when OneDrive
finished *receiving* it. On a site walk uploaded on the drive home that is hours late, and
often enough across midnight, which files the note on a day it did not happen. The record
derives its month folder and its item id from that date, so the wrong day is a note filed
where nobody will look for it.

The fix has a trap on either side of it, and both are asserted:

* the trailing digits of a name he typed are **never** read. ``BEACH COURT SITE WALK
  270826`` is 27 August written the way a person writes it; read as ``YYMMDD`` it is 2027,
  and the recording files itself a year into the future;
* a moment recovered from the recorder's name is the one timestamp nobody agreed to, so it
  is checked against the one fact that cannot be argued with — a recording cannot have been
  made after it was uploaded. More than a day later means the digits are not what they look
  like. *Earlier* is normal and is the whole point: that is the drive home.

**Six tests in here fail, and none of them is bent to make it stop.** They are three
defects, marked where they sit and written up in the report:

* :class:`ApplyingANameNeedsASiteListFirst` — three of them. ``transcriber config set
  NAMING_APPLY true`` raises ``NameError`` on an undefined name in
  ``config_cmd.check_value``, so the one command that turns this feature on cannot run at
  all;
* :class:`DigitsInTheRightShapeThatAreNotADate` — one. ``autoname`` calls a name the voice
  recorder could not have written the recorder's own default, and would retitle it;
* :class:`NamingMinSecondsHasNoFloor` — two. The length floor that separates a site named
  twice from an engine repeating itself can be set to zero or below, in writing, unopposed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

from transcriber import autoname, config_cmd, naming, outputs, sitebook
from transcriber.config import Config, ConfigError
from transcriber.models import Segment, Transcript
from transcriber.naming import SAST, parse_source_name, resolve_timestamp
from transcriber.setup_wizard import write_env_file

from tests.support import StubExtraction, audio_info

# --------------------------------------------------------------------------- his drive

#: The fourteen names off his own OneDrive, exactly as he typed them. Kept verbatim,
#: including the inconsistent date styles — ``270826``, ``2408``, ``14 AUGUST``, ``2508`` —
#: because the inconsistency is the point: no rule can read them, and no rule may try.
HIS_OWN_NAMES = (
    "BEACH COURT SITE WALK 270826",
    "22 CHEPSTOW SITE INSPECTION 2408",
    "CANTERBURY SNAG WALK 14 AUGUST",
    "PIAZZA ST JOHN 6 AUGUST",
    "SBV MEETING WITH JOHAN",
    "Morne Interview",
    "PRODUCTION 29 JUNE",
    "CJ",
    "Q",
    "JORDS",
    "ALB",
    "BEACH COURT UNIT 606 INSPECTION",
    "AMIDAL SITE WALK 260826",
    "BEACH COURT SITE WALK INTERNAL 2508",
)

#: The three shapes his phones write. All machine-written, all carrying a real timestamp,
#: and none of them ever eligible: a call already knows who it was with.
HIS_CALL_NAMES = (
    "Call Carel_260827_120055.m4a",              # the current handset
    "Call recording Carel_250827_161049.m4a",    # the older handset
    "Call +27817957457_260420_133533.m4a",       # a number he has not saved
)

#: Every filename this file judges, and whether the service may work out a name for it.
#: ``(filename, may_be_named, what it is)``. The whole feature lives on the True rows.
ELIGIBILITY_TABLE: tuple[tuple[str, bool, str], ...] = tuple(
    (f"{stem}.m4a", False, "a name he typed himself") for stem in HIS_OWN_NAMES
) + tuple(
    (name, False, "a call, named by the phone") for name in HIS_CALL_NAMES
) + (
    # --- the recorder's own default: the only shape that may ever be renamed ---------
    ("Voice 260806_162219.m4a", True, "the recorder's default, untouched"),
    ("Voice 260806_162219 (1).m4a", True, "the same, re-uploaded, with OneDrive's own (1)"),
    ("Voice 260806_162219 (10).m4a", True, "OneDrive's marker in double digits"),
    ("Voice 241121_193045.m4a", True, "the recorder's default from November 2024"),
    ("Voice 250806_074512.m4a", True, "the recorder's default from August 2025"),
    ("Voice 260806_162219.mp3", True, "the recorder's default, a different container"),
    # --- the near-misses, every one of them his ------------------------------------
    ("voice 260806_162219.m4a", False, "lower case: not the shape the recorder writes"),
    ("Voice 260806_162219 CANTERBURY.m4a", False, "he started naming it and it uploaded"),
    ("VOICE NOTE FOR CAREL.m4a", False, "the word VOICE in a name he typed"),
    ("Voice 26080_162219.m4a", False, "five date digits, so not the recorder's shape"),
    ("PTT-20260806-WA0002.m4a", False, "a WhatsApp push-to-talk note"),
    ("AUD-20260806-WA0002.m4a", False, "a WhatsApp audio file"),
    ("Voice  260806_162219.m4a", False, "two spaces: not the shape the recorder writes"),
    ("Voice260806_162219.m4a", False, "no space at all"),
    ("Voice 260806-162219.m4a", False, "a hyphen where the recorder writes an underscore"),
    ("Recording 260806_162219.m4a", False, "a different device's default word"),
    ("New Recording 12.m4a", False, "another device's default, which he still may have named"),
)

#: The recorder's default, and what its digits mean. The middle row is the live bug: read
#: as anything but year-month-day it is a day the recording did not happen on.
THE_THREE_REAL_EXAMPLES = (
    ("Voice 241121_193045.m4a", datetime(2024, 11, 21, 19, 30, 45)),
    ("Voice 250806_074512.m4a", datetime(2025, 8, 6, 7, 45, 12)),
    ("Voice 260806_162219.m4a", datetime(2026, 8, 6, 16, 22, 19)),
)

#: Long enough to clear the length floor, so nothing in this file is refused for being short
#: when what is being asserted is the filename.
LONG_ENOUGH_S = 754.0


def substantive() -> StubExtraction:
    """An extraction that passes everything except the filename rule."""
    return StubExtraction(site="Wolroy House", summary="He walked the site.")


def judge(name: str, *, duration_s: float = LONG_ENOUGH_S,
          min_seconds: int = 120) -> tuple[bool, str, str]:
    """The real eligibility rule over one real filename. ``(ok, code, why)``."""
    return autoname.eligible(
        parse_source_name(name), substantive(), duration_s, min_seconds=min_seconds
    )


# --------------------------------------------------------------------------- 1. the table


class OnlyTheRecordersOwnDefaultNameIsEligible(unittest.TestCase):
    """The first rule, over his real drive and everything that sits next to it.

    Every row here is a filename that has been on his OneDrive or is one character away
    from one. The rule may say yes to six of them and must say no to the other twenty-five.
    """

    def test_the_table_covers_his_names_and_the_near_misses_around_them(self) -> None:
        # A guard on the table itself. Deleting rows is how a table like this stops
        # meaning anything, and it would do it silently.
        self.assertGreaterEqual(len(ELIGIBILITY_TABLE), 28)
        self.assertEqual(
            len({row[0] for row in ELIGIBILITY_TABLE}), len(ELIGIBILITY_TABLE),
            "a duplicated row is a row that was meant to be a different case",
        )
        for stem in HIS_OWN_NAMES:
            self.assertIn(f"{stem}.m4a", [row[0] for row in ELIGIBILITY_TABLE],
                          "one of his real fourteen has fallen out of the table")

    def test_exactly_the_recorders_own_default_shape_may_be_named(self) -> None:
        for name, may_be_named, what in ELIGIBILITY_TABLE:
            with self.subTest(name=name, what=what):
                ok, code, why = judge(name)

                self.assertEqual(ok, may_be_named, f"{name!r} — {what}")
                if not may_be_named:
                    # E1 and nothing else. A refusal for any other reason would mean this
                    # name was only saved by the length floor or by a thin transcript, and
                    # would start renaming his files the day either of those moved.
                    self.assertEqual(code, "E1", f"{name!r} was refused for the wrong reason")
                    self.assertEqual(why, "he named this one himself")
                else:
                    self.assertEqual(code, "")

    def test_the_filename_rule_and_the_eligibility_rule_never_disagree(self) -> None:
        for name, may_be_named, what in ELIGIBILITY_TABLE:
            with self.subTest(name=name, what=what):
                stem = parse_source_name(name).stem
                # Two doors into the same decision — the pipeline asks eligible(), the
                # morning email and the wizard's copy ask is_recorder_default(). If they
                # ever part company, one surface renames a file the other says is his.
                self.assertEqual(autoname.is_recorder_default(stem), may_be_named, name)

    def test_none_of_his_own_fourteen_names_is_the_recorders_default(self) -> None:
        for stem in HIS_OWN_NAMES:
            with self.subTest(stem=stem):
                self.assertFalse(autoname.is_recorder_default(stem),
                                 f"{stem!r} is a name he typed and would have been renamed")

    def test_a_name_he_typed_is_refused_before_anything_else_is_consulted(self) -> None:
        """Ordering, not just outcome: his name wins over an empty site list and a silent
        recording, so nothing downstream can ever get the chance to overturn it."""
        for stem in HIS_OWN_NAMES:
            with self.subTest(stem=stem):
                decision = autoname.decide(
                    parsed=parse_source_name(f"{stem}.m4a"),
                    extraction=substantive(),
                    spoken="Right, I am at Canterbury this morning. Canterbury again.",
                    duration_s=LONG_ENOUGH_S,
                    book=sitebook.EMPTY,
                    render=lambda _name: (_ for _ in ()).throw(
                        AssertionError("the renderer must never be reached for his own name")
                    ),
                    apply=True,
                    min_seconds=120,
                )

                self.assertEqual(decision.code, "E1")
                self.assertEqual(decision.name, "")
                self.assertFalse(decision.applied)

    def test_the_shape_is_case_sensitive_on_purpose(self) -> None:
        for spelling in ("voice 260806_162219", "VOICE 260806_162219", "vOice 260806_162219"):
            with self.subTest(spelling=spelling):
                # Case is the cheapest signal that a person has been at the name. Widening
                # this to case-insensitive is the single-line change that would start
                # renaming his files, so it is pinned.
                self.assertFalse(autoname.is_recorder_default(spelling))
        self.assertTrue(autoname.is_recorder_default("Voice 260806_162219"))

    def test_a_name_with_anything_after_the_digits_is_his(self) -> None:
        for tail in (" CANTERBURY", " canterbury", " 2", " -", "x"):
            with self.subTest(tail=tail):
                # He starts typing over the recorder's name and it uploads mid-edit. What
                # he got as far as typing is more his than anything a model would work out.
                self.assertFalse(autoname.is_recorder_default(f"Voice 260806_162219{tail}"))


# --------------------------------------------------- 2. the timestamp that was being lost


class TheRecorderDefaultCarriesTheMomentItWasRecorded(unittest.TestCase):
    """The fix. Before it, every unnamed recording was dated by its upload."""

    def test_the_recorders_default_name_reads_as_year_month_day_hour_minute_second(self) -> None:
        parsed = parse_source_name("Voice 260806_162219.m4a")

        self.assertEqual(parsed.timestamp, datetime(2026, 8, 6, 16, 22, 19))
        self.assertTrue(parsed.timestamp_recovered,
                        "the moment must be flagged as recovered, or the day guard is skipped")

    def test_the_three_real_examples_resolve_to_the_days_they_happened_on(self) -> None:
        for name, expected in THE_THREE_REAL_EXAMPLES:
            with self.subTest(name=name):
                parsed = parse_source_name(name)

                self.assertEqual(parsed.timestamp, expected)
                # The date is what the record derives its month folder and its item id
                # from. A day out is a note in the wrong month folder; a year out is a note
                # nobody finds again.
                self.assertEqual(parsed.timestamp.date().isoformat(),
                                 expected.date().isoformat())

    def test_the_digits_are_never_read_day_first(self) -> None:
        parsed = parse_source_name("Voice 241121_193045.m4a")

        # 241121 day-first is November 2021, before any of this existed. Year-first it is
        # 21 November 2024, which is a Thursday he was on site.
        self.assertEqual(parsed.timestamp.year, 2024)
        self.assertEqual(parsed.timestamp.month, 11)
        self.assertEqual(parsed.timestamp.day, 21)

    def test_the_recovered_moment_beats_the_upload_time(self) -> None:
        parsed = parse_source_name("Voice 260806_162219.m4a")
        when, note = resolve_timestamp(parsed, "2026-08-06T15:00:00Z")

        # 16:22 SAST recorded, 17:00 SAST received. Before the fix this was filed at 17:00,
        # and a walk that ran past 22:00 was filed on the following day.
        self.assertEqual(when, datetime(2026, 8, 6, 16, 22, 19, tzinfo=SAST))
        self.assertIn("voice recorder", note)

    def test_the_note_says_the_moment_came_from_the_recorders_name(self) -> None:
        parsed = parse_source_name("Voice 260806_162219.m4a")
        _when, note = resolve_timestamp(parsed, "2026-08-06T15:00:00Z")

        # Printed in the provenance row of all three files. A reader has to be able to see
        # which clock dated the recording without opening the ledger.
        self.assertIn("rather than when it finished uploading", note)

    def test_a_recording_that_crossed_midnight_is_filed_on_the_day_it_happened(self) -> None:
        parsed = parse_source_name("Voice 260806_231500.m4a")
        when, _note = resolve_timestamp(parsed, "2026-08-07T00:40:00Z")

        # Recorded 23:15 on the 6th, received 02:40 on the 7th. This is the case the fix
        # exists for: the record's month folder and its item id both come off this date.
        self.assertEqual(when.date().isoformat(), "2026-08-06")

    def test_the_recorders_default_still_claims_no_party(self) -> None:
        parsed = parse_source_name("Voice 260806_162219.m4a")

        # A recovered moment must not promote the file to a call. "Call with 260806" in a
        # subject line is a party that does not exist.
        self.assertIsNone(parsed.party)
        self.assertEqual(parsed.form, naming.FORM_FREE_TEXT)
        self.assertFalse(parsed.matched_call_form)


class TheTrapInHisOwnTrailingDigits(unittest.TestCase):
    """``270826`` is 27 August. Read as ``YYMMDD`` it is 2027 and the note is gone."""

    def test_all_fourteen_of_his_names_carry_no_timestamp_at_all(self) -> None:
        for stem in HIS_OWN_NAMES:
            with self.subTest(stem=stem):
                parsed = parse_source_name(f"{stem}.m4a")

                # Any of these read as a date files the recording somewhere it did not
                # happen. 270826 is the worst: a year into the future, in a month folder
                # nobody opens, on a note he will look for next week.
                self.assertIsNone(parsed.timestamp, f"{stem!r} was read as a timestamp")
                self.assertFalse(parsed.timestamp_recovered)
                self.assertIn("no timestamp", parsed.timestamp_note)

    def test_his_names_fall_back_to_the_upload_time_and_say_so(self) -> None:
        for stem in HIS_OWN_NAMES:
            with self.subTest(stem=stem):
                parsed = parse_source_name(f"{stem}.m4a")
                when, note = resolve_timestamp(parsed, "2026-08-27T09:15:00Z")

                self.assertEqual(when, datetime(2026, 8, 27, 11, 15, tzinfo=SAST))
                # The fallback is honest about being a fallback, because it is the one the
                # record's month folder is derived from when the filename cannot say.
                self.assertIn("OneDrive recorded the file as created", note)

    def test_the_fix_did_not_widen_what_counts_as_a_timestamp(self) -> None:
        for stem in ("SITE WALK 260806 162219", "260806_162219", "Voice note 260806_162219"):
            with self.subTest(stem=stem):
                # Each of these is a hand-typed name with the recorder's digits somewhere
                # in it. The shape is anchored at both ends for exactly this reason.
                self.assertIsNone(parse_source_name(f"{stem}.m4a").timestamp)


class TheDayGuardOnARecoveredMoment(unittest.TestCase):
    """A recording cannot have been made after it was uploaded — by more than a day."""

    NAME = "Voice 260806_162219.m4a"          # reads as 2026-08-06 16:22:19 SAST

    def test_a_moment_earlier_than_the_upload_is_kept_because_that_is_the_drive_home(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, note = resolve_timestamp(parsed, "2026-08-06T15:00:00Z")

        # Recorded on site, uploaded when the phone found signal. This is the ordinary case
        # and the entire reason the filename is read at all — if this ever starts falling
        # back to the created time, the fix has been undone.
        self.assertEqual(when, datetime(2026, 8, 6, 16, 22, 19, tzinfo=SAST))
        self.assertIn("voice recorder", note)

    def test_a_moment_days_earlier_is_still_kept(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, _note = resolve_timestamp(parsed, "2026-08-20T09:00:00Z")

        # A phone left in a jacket pocket for a fortnight. Late is normal; the filename is
        # still the only thing that knows when he was on site.
        self.assertEqual(when.date().isoformat(), "2026-08-06")

    def test_a_moment_more_than_a_day_after_the_upload_is_discarded(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, note = resolve_timestamp(parsed, "2026-08-01T09:00:00Z")

        # The digits are not what they look like — another handset's naming scheme, or a
        # name that happens to fall in the shape. The created time is late but it is real.
        self.assertEqual(when, datetime(2026, 8, 1, 11, 0, 0, tzinfo=SAST))
        self.assertEqual(when.date().isoformat(), "2026-08-01")

    def test_the_note_says_which_clock_was_used_and_why_the_other_was_not(self) -> None:
        parsed = parse_source_name(self.NAME)
        _when, note = resolve_timestamp(parsed, "2026-08-01T09:00:00Z")

        # This sentence is printed in the provenance row of all three files. Without it a
        # reader sees a date that disagrees with the filename and has nothing to go on.
        self.assertIn("2026-08-06 16:22:19", note)
        self.assertIn("after OneDrive received the file", note)
        self.assertIn("2026-08-01 09:00:00 UTC", note)

    def test_exactly_a_day_later_is_still_trusted(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, _note = resolve_timestamp(parsed, "2026-08-05T14:22:19Z")

        # The boundary is "more than a day", not "a day". A phone an hour out and an upload
        # that waited overnight is a real Tuesday, and it must not lose its real date.
        self.assertEqual(when, datetime(2026, 8, 6, 16, 22, 19, tzinfo=SAST))

    def test_one_second_past_a_day_is_not(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, _note = resolve_timestamp(parsed, "2026-08-05T14:22:18Z")

        self.assertEqual(when.date().isoformat(), "2026-08-05")

    def test_with_no_upload_time_at_all_the_recovered_moment_stands(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, note = resolve_timestamp(parsed, None)

        # Graph occasionally returns an item with no usable createdDateTime. A recovered
        # moment is better than refusing the recording, which is a quarantine.
        self.assertEqual(when, datetime(2026, 8, 6, 16, 22, 19, tzinfo=SAST))
        self.assertIn("voice recorder", note)

    def test_the_guard_is_only_on_the_moment_nobody_agreed_to(self) -> None:
        parsed = parse_source_name("Call Carel_260827_120055.m4a")
        when, _note = resolve_timestamp(parsed, "2026-08-01T09:00:00Z")

        # A call's timestamp is written by the dialler beside the number it dialled, so it
        # is not second-guessed. Only the recovered moment is, because only it was inferred.
        self.assertFalse(parsed.timestamp_recovered)
        self.assertEqual(when, datetime(2026, 8, 27, 12, 0, 55, tzinfo=SAST))

    def test_the_guard_never_raises_and_never_leaves_a_recording_undated(self) -> None:
        parsed = parse_source_name(self.NAME)
        for created in ("2026-08-01T09:00:00Z", "2026-08-06T15:00:00Z", "not a date",
                        "", None, "2026-08-06T15:00:00.1234567Z",
                        datetime(2026, 8, 1, 9, tzinfo=timezone.utc)):
            with self.subTest(created=created):
                when, note = resolve_timestamp(parsed, created)

                # Every branch has to produce a date and a sentence. A raise here is a
                # recording that stops rather than one with a plainer title.
                self.assertIsInstance(when, datetime)
                self.assertIsNotNone(when.tzinfo)
                self.assertTrue(note)


# ------------------------------------------------- 3. digits that are not a date at all


class DigitsInTheRightShapeThatAreNotADate(unittest.TestCase):
    """``Voice 260832_250000`` — a 32nd of August at 25:00.

    The parser refuses it, which is right. The eligibility rule accepts it, which is not:
    the voice recorder cannot write an impossible date, so a file in that shape did not
    come from the voice recorder — it came from a person or from some other tool, and it
    is therefore his.
    """

    NAME = "Voice 260832_250000.m4a"

    def test_the_impossible_digits_are_read_as_no_timestamp_at_all(self) -> None:
        parsed = parse_source_name(self.NAME)

        # Not nudged to the 31st, not nudged to midnight. A guessed date is a note filed on
        # a day nobody can check.
        self.assertIsNone(parsed.timestamp)
        self.assertFalse(parsed.timestamp_recovered)

    def test_it_still_parses_and_still_gets_a_date_from_the_upload(self) -> None:
        parsed = parse_source_name(self.NAME)
        when, note = resolve_timestamp(parsed, "2026-08-27T09:15:00Z")

        self.assertEqual(when, datetime(2026, 8, 27, 11, 15, tzinfo=SAST))
        self.assertTrue(note)

    def test_nothing_in_the_naming_path_raises_on_it(self) -> None:
        parsed = parse_source_name(self.NAME)
        decision = autoname.decide(
            parsed=parsed,
            extraction=substantive(),
            spoken="Right, I am at Wolroy House. Back at Wolroy House tomorrow.",
            duration_s=LONG_ENOUGH_S,
            book=sitebook.EMPTY,
            render=lambda name: f"Subject: {name}\n\nbody",
            apply=True,
            min_seconds=120,
        )

        # Whatever it decides, it decides it without throwing. An exception here would be
        # caught by the pipeline and cost nothing, but only because the pipeline catches it.
        self.assertTrue(decision.decided)
        self.assertEqual(decision.name, "")

    def test_a_name_the_voice_recorder_could_not_have_written_is_not_eligible(self) -> None:
        """FAILS TODAY — see the report. ``autoname._RECORDER_DEFAULT`` checks the digit
        shape and not the date, so it calls this the recorder's own default while
        ``naming._RECORDER_DEFAULT_RE`` has already refused to read a moment out of it.
        The two definitions of "the recorder's own name" disagree on exactly this input,
        and the one that decides whether a file may be retitled is the loose one."""
        ok, code, _why = judge(self.NAME)

        # The recorder writes a real clock reading every time. Hour 25 on the 32nd came
        # from somewhere else, which means somebody chose it — and a name he chose is never
        # touched. This is the E1 rule's own stated reason, applied one step further out.
        self.assertFalse(ok, "a name the recorder could not have written was accepted")
        self.assertEqual(code, "E1")


# ------------------------------------------------------- 4. two uploads, two recordings


#: An ordinary walk, one line per segment as the engine returns them. Two mentions of the
#: site, one near the top and one at the end, which is what the naming rules ask for.
WOLROY_WALK = (
    "Right, I am at Wolroy House this morning with the managing agent.",
    "The scaffold is up to the third floor and the crew started yesterday.",
    "That is Wolroy House done for today, I will send the photographs tonight.",
)


def transcript_of(lines: tuple[str, ...]) -> Transcript:
    """One segment per line, spread evenly across the recording."""
    step = LONG_ENOUGH_S / max(len(lines), 1)
    return Transcript(
        text=" ".join(lines),
        segments=[
            Segment(index * step, index * step + step * 0.9, "James", line)
            for index, line in enumerate(lines)
        ],
        language="en-ZA",
        engine="test-engine",
    )


def context_for(
    name: str,
    item_id: str,
    *,
    display_name: str = "",
    lines: tuple[str, ...] = WOLROY_WALK,
    site: str = "Wolroy House",
) -> outputs.OutputContext:
    """One recording's render context, built from its real filename."""
    parsed = parse_source_name(name)
    when, source = resolve_timestamp(parsed, "2026-08-06T15:00:00Z")
    return outputs.OutputContext(
        item_id=item_id,
        source_name=parsed.original_name,
        parsed=parsed,
        recorded_at=when,
        timestamp_source=source,
        transcript=transcript_of(lines),
        extraction=StubExtraction(site=site, summary="He walked the site."),
        audio=audio_info(LONG_ENOUGH_S),
        content_hash="a" * 64,
        graph_hash="QUlDSA==",
        web_url=f"https://example.invalid/{item_id}",
        engine="test-engine",
        display_name=display_name,
    )


class TwoOneDriveDuplicatesAreTwoRecordings(unittest.TestCase):
    """``Voice 260806_162219 (1).m4a`` is a second recording, not a second name.

    His phone re-uploads after an interrupted sync, so the folder holds both. Two Graph
    items, two ledger rows, two recordings — and one set of output names would have the
    second replace the first in place with every check still passing. That is the exact
    failure this service was built to remove, committed by the service itself.
    """

    ORIGINAL = "Voice 260806_162219.m4a"
    DUPLICATE = "Voice 260806_162219 (1).m4a"

    def test_the_copy_marker_is_read_off_the_name_and_the_stem_is_the_recorders(self) -> None:
        parsed = parse_source_name(self.DUPLICATE)

        self.assertEqual(parsed.copy_marker, 1)
        # The marker is stripped before the shape is judged, or OneDrive's own suffix would
        # silently demote the file to a hand-typed name and it would never be dated.
        self.assertEqual(parsed.stem, "Voice 260806_162219")
        self.assertEqual(parsed.timestamp, datetime(2026, 8, 6, 16, 22, 19))
        self.assertTrue(parsed.timestamp_recovered)

    def test_a_duplicate_of_the_recorders_default_is_still_eligible(self) -> None:
        ok, code, _why = judge(self.DUPLICATE)

        # The marker is OneDrive's, not his. Refusing on it would leave exactly the
        # re-uploaded recordings — the ones that already had a sync problem — unnamed.
        self.assertTrue(ok)
        self.assertEqual(code, "")

    def test_the_duplicate_keeps_the_copy_marker_in_all_three_output_names(self) -> None:
        names = context_for(self.DUPLICATE, "01BBB").names

        for name in names.as_tuple():
            with self.subTest(name=name):
                # Meaningful to a person opening the output folder: two files an hour apart
                # with the same title are indistinguishable without it.
                self.assertIn("-copy1-", name)

    def test_the_two_uploads_write_six_different_files(self) -> None:
        first = context_for(self.ORIGINAL, "01AAA").names
        second = context_for(self.DUPLICATE, "01BBB").names

        written = list(first.as_tuple()) + list(second.as_tuple())
        # Six names, six files. A collision here is one recording's transcript ceasing to
        # exist anywhere while the ledger says it was written — a silent loss, which is the
        # thing this whole service replaced.
        self.assertEqual(len(set(written)), 6, written)

    def test_the_two_uploads_share_the_moment_they_were_recorded(self) -> None:
        first = context_for(self.ORIGINAL, "01AAA")
        second = context_for(self.DUPLICATE, "01BBB")

        # Same recording, same clock reading, so both file into the same month folder in
        # the record. Only the item id and the marker separate them.
        self.assertEqual(first.recorded_at, second.recorded_at)
        self.assertNotEqual(first.names.transcript, second.names.transcript)

    def test_a_worked_out_name_never_reaches_the_three_filenames(self) -> None:
        plain = context_for(self.DUPLICATE, "01BBB")
        titled = context_for(self.DUPLICATE, "01BBB", display_name="WOLROY HOUSE")

        # The filenames are recovered by writing them again after a half-failed publish. A
        # name that could change between attempts leaves three files nobody can delete and
        # a second document in the record for one recording.
        self.assertEqual(plain.names.as_tuple(), titled.names.as_tuple())
        self.assertEqual(titled.label, "WOLROY HOUSE")
        self.assertEqual(plain.label, "Voice 260806_162219")


# ------------------------------------------------------------------ 5. the four settings

BASE_ENV = {
    "GRAPH_TENANT_ID": "tenant-for-tests",
    "GRAPH_CLIENT_ID": "client-for-tests",
    "GRAPH_CLIENT_SECRET": "not-a-real-secret",
    "GRAPH_USER_ID": "drive-owner",
    "SOURCE_FOLDER_ID": "SOURCE",
    "OUTPUT_FOLDER_ID": "OUTPUT",
    "ARCHIVE_FOLDER_ID": "ARCHIVE",
    "TRANSCRIBE_ENGINE": "openai",
    "OPENAI_API_KEY": "not-a-real-engine-key",
    "ANALYSIS_API_KEY": "not-a-real-analysis-key",
    "SMTP_HOST": "smtp.invalid",
    "SMTP_USER": "digest",
    "SMTP_PASSWORD": "not-a-real-password",
    "SMTP_FROM": "digest@invalid",
    "SMTP_TO": "someone@invalid",
    "HEARTBEAT_URL": "https://example.invalid/beat",
    "LEDGER_PATH": ":memory:",
}

THE_FOUR = ("NAMING", "NAMING_APPLY", "NAMING_SITES_FILE", "NAMING_MIN_SECONDS")


def env(**overrides: str) -> dict[str, str]:
    """A complete environment with nothing real in it, plus whatever this test needs."""
    values = dict(BASE_ENV)
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def problems_from(**overrides: str) -> list[str]:
    """Every complaint this environment produces, or an empty list if it is usable."""
    try:
        Config.from_env(env(**overrides))
    except ConfigError as exc:
        return list(exc.problems)
    return []


class _EnvOnDisk:
    """A real ``.env`` written the way the wizard writes one."""

    def __init__(self, directory: str, **overrides: str) -> None:
        self.path = os.path.join(directory, ".env")
        values = env(**overrides)
        values["LEDGER_PATH"] = os.path.join(directory, "ledger.sqlite3")
        write_env_file(self.path, values)
        self.before = self.read_bytes()

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as handle:
            return handle.read()

    @property
    def unchanged(self) -> bool:
        return self.read_bytes() == self.before


def set_args(**kw) -> argparse.Namespace:
    """The namespace ``config set`` gets from argparse, with every alias defaulted off."""
    fields = {"action": "set", "key": None, "value": None, "env": ""}
    fields.update({alias.replace("-", "_"): None for alias in config_cmd.ALIASES})
    fields.update(kw)
    return argparse.Namespace(**fields)


class TheFourSettingsAreVisibleAndEditable(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = _EnvOnDisk(self.dir.name)
        self.out = io.StringIO()

    def listing(self) -> str:
        config_cmd.cmd_list(
            argparse.Namespace(action="list", key=None, value=None, env=self.env.path),
            self.out,
        )
        return self.out.getvalue()

    def test_all_four_settings_appear_in_config_list(self) -> None:
        printed = self.listing()

        for name in THE_FOUR:
            with self.subTest(name=name):
                # A setting that cannot be found is a setting he cannot turn off at 06:30
                # when it has started titling things wrongly.
                self.assertIn(name, printed)

    def test_config_list_puts_them_under_a_heading_a_person_can_find(self) -> None:
        printed = self.listing()

        self.assertIn("naming a recording that arrived without one", printed)

    def test_config_list_says_what_each_one_defaults_to(self) -> None:
        lines = {
            name: next(line for line in self.listing().splitlines() if name in line)
            for name in THE_FOUR
        }

        # Reporting-only is the shipped state, and he has to be able to read that off the
        # listing rather than take it on trust — it is the difference between a service
        # that is writing titles into the record and one that is only talking about them.
        self.assertIn("false", lines["NAMING_APPLY"])
        self.assertIn("true", lines["NAMING"])
        self.assertIn("120", lines["NAMING_MIN_SECONDS"])
        self.assertIn("not set", lines["NAMING_SITES_FILE"])

    def test_every_one_of_the_four_is_a_setting_this_command_knows(self) -> None:
        for name in THE_FOUR:
            with self.subTest(name=name):
                self.assertIn(name, config_cmd.SETTINGS)
                self.assertTrue(config_cmd.SETTINGS[name].description)


class ApplyingANameNeedsASiteListFirst(unittest.TestCase):
    """``NAMING_APPLY`` writes into the subject line. Without a site list there is
    nothing to write, and the setting is a promise the service cannot keep."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = io.StringIO()

    def test_turning_it_on_without_a_site_list_is_refused_at_the_keyboard(self) -> None:
        problem = config_cmd.check_value("NAMING_APPLY", "true", env(NAMING_SITES_FILE=""))

        # Refused where he is standing, by name, rather than accepted and quietly doing
        # nothing until he asks why the titles never changed.
        self.assertTrue(problem, "NAMING_APPLY=true was accepted with no site list")
        self.assertIn("NAMING_SITES_FILE", problem)

    def test_config_set_refuses_it_and_writes_nothing(self) -> None:
        on_disk = _EnvOnDisk(self.dir.name)

        code = config_cmd.cmd_set(
            set_args(env=on_disk.path, key="NAMING_APPLY", value="true"), self.out
        )

        self.assertEqual(code, config_cmd.EXIT_FAILED, self.out.getvalue())
        self.assertTrue(on_disk.unchanged, "the .env was rewritten for a refused setting")
        self.assertIn("Nothing was written", self.out.getvalue())

    def test_with_a_site_list_named_it_is_allowed(self) -> None:
        book = os.path.join(self.dir.name, "sites.json")
        with open(book, "w", encoding="utf-8") as handle:
            json.dump({"generated_at": "2026-08-27", "sites": []}, handle)
        on_disk = _EnvOnDisk(self.dir.name, NAMING_SITES_FILE=book)

        code = config_cmd.cmd_set(
            set_args(env=on_disk.path, key="NAMING_APPLY", value="true"), self.out
        )

        # The refusal has to be about the missing list and nothing else, or turning the
        # feature on becomes impossible and he never gets the titles at all.
        self.assertEqual(code, config_cmd.EXIT_OK, self.out.getvalue())

    def test_turning_it_off_is_never_refused(self) -> None:
        problem = config_cmd.check_value("NAMING_APPLY", "false", env(NAMING_SITES_FILE=""))

        # Switching a feature off must never be blocked by a rule about switching it on.
        # This is the line he reaches for when something has gone wrong.
        self.assertEqual(problem, "")

    def test_a_service_with_apply_on_and_no_site_list_still_starts(self) -> None:
        problems = problems_from(NAMING_APPLY="true", NAMING_SITES_FILE="")

        # Deliberate, and it outranks tidiness: a configuration error stops the service,
        # and a stopped service is recordings not being transcribed. The mistake costs a
        # plainer title; refusing to start would cost the recordings.
        self.assertEqual(problems, [])

    def test_but_it_says_so_out_loud(self) -> None:
        config = Config.from_env(env(NAMING_APPLY="true", NAMING_SITES_FILE=""))

        joined = " ".join(config.notices)
        self.assertIn("NAMING_APPLY", joined)
        self.assertIn("nothing to apply", joined)


class NamingMinSecondsIsAWholeNumberOfSeconds(unittest.TestCase):
    def test_it_is_read_as_an_int(self) -> None:
        config = Config.from_env(env(NAMING_MIN_SECONDS="240"))

        self.assertIsInstance(config.naming_min_seconds, int)
        self.assertEqual(config.naming_min_seconds, 240)

    def test_it_defaults_to_two_minutes(self) -> None:
        self.assertEqual(Config.from_env(env()).naming_min_seconds, 120)

    def test_something_that_is_not_a_number_is_refused_at_startup(self) -> None:
        problems = problems_from(NAMING_MIN_SECONDS="two minutes")

        self.assertTrue(problems)
        self.assertTrue(any("NAMING_MIN_SECONDS" in p for p in problems), problems)

    def test_something_that_is_not_a_number_is_refused_at_the_keyboard(self) -> None:
        problem = config_cmd.check_value("NAMING_MIN_SECONDS", "two minutes", env())

        self.assertIn("whole number", problem)

    def test_the_floor_is_what_stops_a_hallucination_loop_being_named(self) -> None:
        ok, code, why = judge("Voice 260806_162219.m4a", duration_s=40.0, min_seconds=120)

        # Forty seconds of wind noise comes back as "Canterbury Square. Thank you for
        # watching. Canterbury Square, thank you for watching" — which is mentioned twice,
        # mentioned early and spread across the recording. The two conditions that look
        # like evidence ARE the signature of the failure. Length is the only cheap thing
        # that separates them.
        self.assertFalse(ok)
        self.assertEqual(code, "E4")
        self.assertIn("40s", why)

    def test_a_recording_with_no_measured_duration_is_never_named(self) -> None:
        ok, code, why = judge("Voice 260806_162219.m4a", duration_s=None, min_seconds=120)

        # An unprobed container is not a short recording and is not a long one. Unknown has
        # to fall the same way as too-short, or the floor is skippable by breaking ffprobe.
        self.assertFalse(ok)
        self.assertEqual(code, "E4")
        self.assertIn("no duration", why)


class NamingMinSecondsHasNoFloor(unittest.TestCase):
    """FAILS TODAY — see the report.

    Every other whole-number setting in ``config_cmd._RULES`` carries a minimum.
    ``NAMING_MIN_SECONDS`` carries none, and ``Config.from_env`` does not range-check it
    either, so ``0`` and ``-1`` are both written into the ``.env`` without a word.
    """

    def test_zero_is_refused(self) -> None:
        # Zero switches the length floor off. Every forty-second recording of wind noise
        # becomes eligible, and the engine's own repetitions are then indistinguishable
        # from a site being named twice — which is a confidently wrong title on a note that
        # then files itself under the wrong site, or under no site at all.
        problem = config_cmd.check_value("NAMING_MIN_SECONDS", "0", env())

        self.assertTrue(problem, "NAMING_MIN_SECONDS=0 was accepted")

    def test_a_negative_number_of_seconds_is_refused(self) -> None:
        # A negative floor is not a setting, it is a typo, and it reaches the rule intact:
        # the pipeline's `or 120` guard rewrites 0 and lets -1 straight through.
        problem = config_cmd.check_value("NAMING_MIN_SECONDS", "-1", env())

        self.assertTrue(problem, "NAMING_MIN_SECONDS=-1 was accepted")

    def test_a_negative_floor_really_would_name_a_forty_second_recording(self) -> None:
        ok, _code, _why = judge("Voice 260806_162219.m4a", duration_s=40.0, min_seconds=-1)

        # The cost of the missing range check, stated as behaviour rather than as a rule.
        # This one passes today: it is what the two above are protecting against.
        self.assertTrue(ok)


# ------------------------------------------------------------------- 6. the real record

#: Where the record is checked out. Overridable so this suite can be pointed at a copy,
#: never at a fixture: the whole value of the class below is that the vocabulary is his.
RECORD = os.environ.get("KBC_SITE_MEMORY", "/home/user/kbc-site-memory")

_BOOK_DIR: tempfile.TemporaryDirectory | None = None
BOOK: sitebook.SiteBook = sitebook.EMPTY


def setUpModule() -> None:
    """Project the real record through the real ops script, exactly as the service does."""
    global _BOOK_DIR, BOOK
    builder = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "ops", "build-site-book.py")
    if not (os.path.isdir(RECORD) and os.path.exists(builder)):
        return
    _BOOK_DIR = tempfile.TemporaryDirectory()
    path = os.path.join(_BOOK_DIR.name, "sites.json")
    result = subprocess.run([sys.executable, builder, RECORD, path],
                            capture_output=True, text=True)
    if result.returncode == 0:
        BOOK = sitebook.load(path)


def tearDownModule() -> None:
    if _BOOK_DIR is not None:
        _BOOK_DIR.cleanup()


class HisOwnNamesSurviveTheRealRecord(unittest.TestCase):
    """The fourteen names, against the real 56 sites, over a walk that WOULD be named.

    A vocabulary invented for a test agrees with whatever rule was written beside it. The
    real one has ``beach`` in one site title, ``house`` in five and ``square`` in two, and
    it is the only thing that can say whether the first rule really holds — because if it
    is ever loosened, this is the walk that would retitle ``CJ.m4a`` to CANTERBURY.
    """

    #: A walk the naming rules really do title, so a refusal below is the filename rule
    #: doing its job rather than the transcript being too thin to name from.
    CANTERBURY_WALK = (
        "Right, I am at Canterbury this morning and the scaffold is up on the sea side.",
        "The painters have finished the second and third floors and the snag list is short.",
        "Marius says the mesh comes off on Thursday if the wind drops, otherwise Monday.",
        "I have asked for a price on the balustrade repairs before the trustees meet.",
        "The lift lobby ceiling still shows a damp mark and somebody has to get above it.",
        "I will be back at Canterbury next week to walk the last of it and close it off.",
    )

    @classmethod
    def setUpClass(cls) -> None:
        assert BOOK, "the real site book failed to load"
        # The real record, not a fixture. If these go, this class is measuring an invented
        # vocabulary and proving nothing.
        assert BOOK.size >= 50, f"only {BOOK.size} sites — this is not the real record"
        assert "canterbury-square" in BOOK.sites, "canterbury-square is not in the record"

    def context(self, name: str, title: str = "") -> outputs.OutputContext:
        return context_for(name, "01REAL", display_name=title,
                           lines=self.CANTERBURY_WALK, site="Canterbury")

    def decide_for(self, name: str) -> autoname.NameDecision:
        ctx = self.context(name)
        return autoname.decide(
            parsed=ctx.parsed,
            extraction=ctx.extraction,
            # The published words, not the engine's prose — the same string the renderer
            # writes into the file, which is what the record actually scores.
            spoken=outputs.spoken_body(ctx.transcript),
            duration_s=LONG_ENOUGH_S,
            book=BOOK,
            render=lambda title: outputs.render_transcript(self.context(name, title)),
            apply=True,
            min_seconds=120,
        )

    def test_the_recorders_own_default_really_is_named_by_this_walk(self) -> None:
        decision = self.decide_for("Voice 260806_162219.m4a")

        # The control. Without it every refusal below could be the transcript's fault
        # rather than the filename rule's doing, and the class would pass proving nothing.
        self.assertEqual(decision.code, "ok", decision.why)
        self.assertEqual(decision.name, "CANTERBURY")
        self.assertEqual(decision.site, "canterbury-square")

    def test_not_one_of_his_fourteen_names_is_touched_by_that_same_walk(self) -> None:
        for stem in HIS_OWN_NAMES:
            with self.subTest(stem=stem):
                decision = self.decide_for(f"{stem}.m4a")

                # Same words, same real record, same rules — and no name, because the file
                # already has one. A CANTERBURY title on CJ.m4a is a note filed under a
                # site it has nothing to do with, and nobody ever notices.
                self.assertEqual(decision.code, "E1")
                self.assertEqual(decision.name, "")
                self.assertFalse(decision.applied)

    def test_a_recording_he_started_renaming_himself_is_left_alone(self) -> None:
        decision = self.decide_for("Voice 260806_162219 CANTERBURY.m4a")

        # He got as far as typing the site before it uploaded. What he typed is his, even
        # when the service would have reached the same answer.
        self.assertEqual(decision.code, "E1")
        self.assertEqual(decision.name, "")

    def test_a_duplicate_upload_of_the_recorders_default_is_still_named(self) -> None:
        decision = self.decide_for("Voice 260806_162219 (1).m4a")

        # The re-uploaded copy is a recording too. Leaving it unnamed would single out
        # exactly the recordings that already had a sync problem.
        self.assertEqual(decision.code, "ok", decision.why)
        self.assertEqual(decision.name, "CANTERBURY")


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main()
