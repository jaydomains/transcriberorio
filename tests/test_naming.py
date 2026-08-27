"""Site-meeting filenames parse, and so do the two the phone writes.

A site meeting is named by hand — *BEACH COURT SITE WALK 270826.m4a* — and carries no
timestamp at all. Site meetings are an eighth of what gets lost, so a parser that assumes
the machine-written ``Call`` shape is blind to exactly the recordings that most need
catching. Every name has to come back as a usable record.

The trailing ``270826`` on that walk is 27 August written the way a person writes it.
Reading it as ``YYMMDD`` would file the recording in 2027, a year into the future, where
nobody will ever look for it — so a timestamp is taken only from the structured
``_YYMMDD_HHMMSS`` tail, which is machine-written and unambiguous. That distinction has its
own test below because it is the kind of thing a later reader "tidies up".
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from transcriber import naming
from transcriber.naming import (
    FORM_CALL,
    FORM_CALL_RECORDING,
    FORM_FREE_TEXT,
    SAST,
    TimestampUnavailable,
    output_names,
    parse_source_name,
    resolve_timestamp,
)


class TheThreeNamesThatMustWork(unittest.TestCase):
    def test_a_hand_typed_site_walk(self) -> None:
        parsed = parse_source_name("BEACH COURT SITE WALK 270826.m4a")

        self.assertEqual(parsed.form, FORM_FREE_TEXT)
        self.assertEqual(parsed.stem, "BEACH COURT SITE WALK 270826")
        self.assertEqual(parsed.extension, ".m4a")
        self.assertIsNone(parsed.party)
        self.assertIsNone(parsed.timestamp)
        self.assertFalse(parsed.matched_call_form)
        self.assertIn("hand-typed", parsed.timestamp_note)

    def test_a_call_from_the_current_handset(self) -> None:
        parsed = parse_source_name("Call Carel_260827_120055.m4a")

        self.assertEqual(parsed.form, FORM_CALL)
        self.assertEqual(parsed.party, "Carel")
        self.assertEqual(parsed.timestamp, datetime(2026, 8, 27, 12, 0, 55))
        self.assertTrue(parsed.matched_call_form)

    def test_a_call_from_the_older_handset(self) -> None:
        parsed = parse_source_name("Call recording Carel_250827_161049.m4a")

        self.assertEqual(parsed.form, FORM_CALL_RECORDING)
        self.assertEqual(parsed.party, "Carel")
        self.assertEqual(parsed.timestamp, datetime(2025, 8, 27, 16, 10, 49))

    def test_all_three_yield_a_usable_record(self) -> None:
        """The point of the whole module: no name is a name this service cannot file."""
        created = "2026-08-27T09:15:00Z"
        for name in (
            "BEACH COURT SITE WALK 270826.m4a",
            "Call Carel_260827_120055.m4a",
            "Call recording Carel_250827_161049.m4a",
        ):
            with self.subTest(name=name):
                parsed = parse_source_name(name)
                when, source = resolve_timestamp(parsed, created)
                names = output_names(when, parsed.stem)

                self.assertTrue(parsed.stem)
                self.assertTrue(source, "where the timestamp came from must always be stated")
                self.assertTrue(names.transcript.endswith(".md"))
                self.assertTrue(names.summary.endswith("-summary.md"))
                self.assertTrue(names.actions.endswith("-actions.md"))
                self.assertTrue(naming.is_output_name(names.transcript))


class YyMmDdIsYearMonthDay(unittest.TestCase):
    def test_the_structured_tail_is_read_year_first(self) -> None:
        self.assertEqual(
            parse_source_name("Call Carel_260827_120055.m4a").timestamp,
            datetime(2026, 8, 27, 12, 0, 55),
        )

    def test_it_is_not_read_day_first(self) -> None:
        """Day-first would make 260827 the 26th of the 8th, in year 27. It does not."""
        stamp = parse_source_name("Call Carel_260827_120055.m4a").timestamp
        self.assertEqual(stamp.year, 2026)
        self.assertEqual(stamp.month, 8)
        self.assertEqual(stamp.day, 27)

    def test_the_hand_typed_digits_are_not_read_as_a_timestamp(self) -> None:
        """270826 on a hand-typed walk is 27 August. Read as YYMMDD it would be 2027."""
        parsed = parse_source_name("BEACH COURT SITE WALK 270826.m4a")
        self.assertIsNone(parsed.timestamp)

        when, _source = resolve_timestamp(parsed, "2026-08-27T09:15:00Z")
        self.assertEqual(when.year, 2026, "a site walk was filed a year into the future")
        self.assertEqual((when.month, when.day), (8, 27))

    def test_digits_in_the_right_shape_that_are_not_a_real_moment_are_read_as_nothing(self) -> None:
        parsed = parse_source_name("Call Carel_260832_250000.m4a")
        self.assertIsNone(parsed.timestamp)
        self.assertIn("not a real", parsed.timestamp_note)


class TheAwkwardNames(unittest.TestCase):
    def test_a_onedrive_duplicate_suffix_does_not_demote_a_call(self) -> None:
        """Left on the stem, "(1)" pushes the timestamp tail off the end of the string."""
        parsed = parse_source_name("Call Carel_260827_120055 (1).m4a")

        self.assertEqual(parsed.form, FORM_CALL)
        self.assertEqual(parsed.copy_marker, 1)
        self.assertEqual(parsed.timestamp, datetime(2026, 8, 27, 12, 0, 55))

    def test_a_nameless_call_recording_does_not_invent_a_party_called_recording(self) -> None:
        parsed = parse_source_name("Call recording_260827_143005.m4a")
        self.assertIsNone(parsed.party)
        self.assertEqual(parsed.form, FORM_CALL_RECORDING)

    def test_a_party_whose_name_starts_with_the_word_recording(self) -> None:
        parsed = parse_source_name("Call Recordings Ltd_260827_143005.m4a")
        self.assertEqual(parsed.party, "Recordings Ltd")

    def test_a_party_containing_underscores_and_digits(self) -> None:
        parsed = parse_source_name("Call Unit_4_Body_Corporate_260827_143005.m4a")
        self.assertEqual(parsed.party, "Unit_4_Body_Corporate")
        self.assertEqual(parsed.timestamp, datetime(2026, 8, 27, 14, 30, 5))

    def test_a_machine_stamp_with_no_call_prefix_claims_no_party(self) -> None:
        parsed = parse_source_name("Voice 004_260827_143005.m4a")
        self.assertIsNone(parsed.party)
        self.assertEqual(parsed.timestamp, datetime(2026, 8, 27, 14, 30, 5))

    def test_a_name_that_is_nothing_but_an_extension_still_parses(self) -> None:
        parsed = parse_source_name(".m4a")
        self.assertEqual(parsed.form, FORM_FREE_TEXT)
        self.assertTrue(naming.safe_stem(parsed.stem))

    def test_an_afrikaans_hand_typed_name(self) -> None:
        parsed = parse_source_name("Werf besoek Chepstow 3 Sept.m4a")
        self.assertEqual(parsed.form, FORM_FREE_TEXT)
        self.assertIsNone(parsed.timestamp)


class WhereTheTimestampComesFrom(unittest.TestCase):
    def test_the_filename_wins_over_the_upload_time(self) -> None:
        """The phone's clock at the moment of recording beats OneDrive's receipt time."""
        parsed = parse_source_name("Call Carel_260827_120055.m4a")
        when, source = resolve_timestamp(parsed, "2026-08-27T19:00:00Z")

        self.assertEqual(when, datetime(2026, 8, 27, 12, 0, 55, tzinfo=SAST))
        self.assertIn("read from the filename", source)

    def test_a_hand_typed_name_falls_back_to_the_item_s_created_time(self) -> None:
        parsed = parse_source_name("BEACH COURT SITE WALK 270826.m4a")
        when, source = resolve_timestamp(parsed, "2026-08-27T09:15:00Z")

        self.assertEqual(when, datetime(2026, 8, 27, 11, 15, tzinfo=SAST))
        self.assertIn("OneDrive recorded the file as created", source)

    def test_no_timestamp_anywhere_is_refused_rather_than_defaulted_to_now(self) -> None:
        """A fabricated timestamp files the recording on a day it did not happen."""
        parsed = parse_source_name("BEACH COURT SITE WALK 270826.m4a")
        with self.assertRaises(TimestampUnavailable):
            resolve_timestamp(parsed, None)
        with self.assertRaises(TimestampUnavailable):
            resolve_timestamp(parsed, "not a date")

    def test_graph_datetimes_with_too_many_subsecond_digits_still_read(self) -> None:
        for value in ("2026-08-27T09:15:00.1234567Z", "2026-08-27T09:15:00.12345678901Z"):
            with self.subTest(value=value):
                when = naming.parse_graph_datetime(value)
                self.assertIsNotNone(when, f"Graph's own timestamp {value} would not read")
                self.assertEqual(when.replace(microsecond=0),
                                 datetime(2026, 8, 27, 9, 15, tzinfo=timezone.utc))

    def test_south_africa_keeps_one_offset_all_year(self) -> None:
        self.assertEqual(SAST.utcoffset(None), timedelta(hours=2))


class TheNamesWeWrite(unittest.TestCase):
    def test_the_stamp_prefix_keeps_two_recordings_in_one_second_apart(self) -> None:
        when = datetime(2026, 8, 27, 14, 30, 5, tzinfo=SAST)
        first = output_names(when, "BEACH COURT SITE WALK 270826")
        second = output_names(when, "Call Carel_260827_143005")

        self.assertTrue(first.transcript.startswith("20260827-143005-"))
        self.assertNotEqual(first.transcript, second.transcript)

    def test_illegal_characters_are_replaced_rather_than_the_name_refused(self) -> None:
        cleaned = naming.safe_stem('BEACH COURT: roof/leak?*"<>|')
        for illegal in ':/?*"<>|':
            self.assertNotIn(illegal, cleaned)
        self.assertTrue(cleaned)

    def test_a_very_long_name_is_cut_back_to_a_word_boundary(self) -> None:
        """A name sliced mid-token reads as a whole one and names nothing."""
        long_name = " ".join(["Chepstow"] * 40)
        cleaned = naming.safe_stem(long_name)
        self.assertLessEqual(len(cleaned), 120)
        self.assertFalse(cleaned.endswith("Chep"))
        self.assertTrue(cleaned.endswith("Chepstow"))

    def test_our_own_output_is_recognisable_so_the_sweep_does_not_loop(self) -> None:
        self.assertTrue(naming.is_output_name("20260827-143005-BEACH COURT SITE WALK 270826.md"))
        self.assertTrue(naming.is_output_name("20260827-143005-x-summary.md"))
        self.assertFalse(naming.is_output_name("BEACH COURT SITE WALK 270826.m4a"))
        self.assertFalse(naming.is_output_name("notes.md"))


if __name__ == "__main__":
    unittest.main()
