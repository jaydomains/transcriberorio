"""The markdown contract: what the downstream record will actually do with our files.

``kbc-site-memory/tools/ingest.py:parse_texty`` is the reader on the other end, and it is
not ours to change. Its behaviour decides the format, so this module parses our rendered
output with a **vendored copy of that real parser** (``tests/vendored_ingest.py``) and
asserts what the record recovers — not what we intended it to.

Four things have to hold, and each of them has already cost somebody information somewhere:

  * the file comes back as ``kind == "transcript"``, which means **no** ``From:`` line —
    one would reclassify a site walk as an email from a sender who does not exist;
  * the ``Subject:`` and the ``Date:`` survive, the date as the day it was recorded and not
    the first of that month;
  * the body is intact, and **no metadata line is silently swallowed** — a non-matching
    line inside the header block reaches neither the header nor the body and disappears
    without erroring, which is the one failure with no symptom at all;
  * no string that looks like an email address appears anywhere in any rendered output.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from transcriber import naming, outputs
from transcriber.models import ExtractedItem, Segment, Transcript
from transcriber.outputs import OutputContext, OutputContractError, render_all

from . import support
from .vendored_ingest import ADDR_RE, parse_texty

SAST = timezone(timedelta(hours=2), "SAST")

TRANSCRIPT_TEXT = (
    "Right, I'm at Beach Court now. Spoke to Carel about the roof leak at unit four. "
    "He says the sheeting was never sealed at the ridge. I told him we'd get a price for "
    "the remedial before the end of the month. Ja, approved, go ahead on the scaffolding."
)


def _item(kind: str, text: str, quote: str, **extra) -> ExtractedItem:
    return ExtractedItem(kind=kind, text=text, quote=quote, quote_verified=True, **extra)


def build_context(**overrides) -> OutputContext:
    parsed = naming.parse_source_name(overrides.pop("source_name", "BEACH COURT SITE WALK 270826.m4a"))
    proposals = [
        support.StubProposal(
            "commitments",
            _item(
                "commitment",
                "James was said to be doing this: get a price for the remedial",
                "I told him we'd get a price for the remedial before the end of the month",
                speaker="James",
                site="Beach Court",
                due="before the end of the month",
                confidence=0.8,
            ),
        ),
        support.StubProposal(
            "open_questions",
            _item(
                "question",
                "Whether the ridge sheeting was ever sealed",
                "the sheeting was never sealed at the ridge",
                site="Beach Court",
            ),
        ),
    ]
    fields = {
        "item_id": "01ABCDEF",
        "source_name": parsed.original_name,
        "parsed": parsed,
        "recorded_at": datetime(2026, 8, 27, 14, 30, 5, tzinfo=SAST),
        "timestamp_source": "the filename carries no timestamp (it is a hand-typed name)",
        "transcript": Transcript(
            text=TRANSCRIPT_TEXT,
            segments=[
                Segment(0.0, 9.0, "James", "Right, I'm at Beach Court now."),
                Segment(9.0, 24.0, "James", "Spoke to Carel about the roof leak at unit four."),
            ],
            language="en-ZA",
            engine="test-engine",
        ),
        "extraction": support.StubExtraction(
            summary="He walked Beach Court and spoke to Carel about a roof leak.",
            proposals=proposals,
            site="Beach Court",
            site_quote="Right, I'm at Beach Court now",
        ),
        "audio": support.audio_info(754.0),
        "content_hash": "a" * 64,
        "graph_hash": "QUlDSA==",
        "web_url": "https://example.invalid/items/01ABCDEF",
        "engine": "test-engine",
    }
    fields.update(overrides)
    return OutputContext(**fields)


class RenderedOutputParsesBackAsATranscript(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ctx = build_context()
        self.files = render_all(self.ctx)
        self.assertEqual([f.kind for f in self.files], ["transcript", "summary", "actions"])

    def parse(self, rendered) -> dict:
        """Through a real file on disk, because that is how the record receives it."""
        path = os.path.join(self.dir.name, rendered.name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rendered.text)
        return parse_texty(path)

    # -- kind ----------------------------------------------------------------------

    def test_every_file_is_read_as_a_transcript_not_an_email(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(self.parse(rendered)["kind"], "transcript")

    def test_no_file_carries_a_from_line(self) -> None:
        """A From: header is the single change that reclassifies the whole file."""
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                parsed = self.parse(rendered)
                self.assertEqual(parsed["from_addr"], "")
                self.assertEqual(parsed["from_name"], "")
                for line in rendered.text.split("\n"):
                    self.assertFalse(
                        re.match(r"(?i)^\s*from\s*:", line),
                        f"a From: line reached the {rendered.kind}: {line!r}",
                    )

    # -- subject and date ----------------------------------------------------------

    def test_the_subject_survives(self) -> None:
        parsed = self.parse(self.files[0])
        self.assertTrue(parsed["subject"])
        self.assertIn("BEACH COURT SITE WALK", parsed["subject"])
        self.assertIn("transcript", parsed["subject"])

    def test_the_three_subjects_tell_the_files_apart(self) -> None:
        subjects = [self.parse(f)["subject"] for f in self.files]
        self.assertEqual(len(set(subjects)), 3, subjects)

    def test_the_date_survives_as_the_day_it_was_recorded(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(self.parse(rendered)["date"], "2026-08-27")

    def test_the_date_is_not_filed_on_the_first_of_the_month(self) -> None:
        """The bug this format exists to avoid, asserted against the record's own parser.

        ``parse_date`` looks for a full ISO date on a word boundary and otherwise falls
        through to a year-month pattern that answers the first of the month. An ISO ``T``
        after the day has no word boundary, so ``2026-08-27T14:30:05`` files as 2026-08-01 —
        and that date becomes the recording's id and its month folder in the record.
        """
        from .vendored_ingest import parse_date

        self.assertEqual(parse_date("2026-08-27T14:30:05"), "2026-08-01")
        header_date = [
            line for line in self.files[0].text.split("\n") if line.startswith("Date:")
        ][0]
        self.assertEqual(parse_date(header_date), "2026-08-27")

    # -- the body ------------------------------------------------------------------

    def test_the_body_is_intact(self) -> None:
        body = self.parse(self.files[0])["body"]
        self.assertIn("Right, I'm at Beach Court now.", body)
        self.assertIn("Spoke to Carel about the roof leak at unit four.", body)
        self.assertIn("## What was said", body)

    def test_no_metadata_line_is_silently_swallowed(self) -> None:
        """The failure with no symptom: a line above the blank line vanishes without error.

        The whole rendered body is compared, line for line, against what the record
        recovers. Anything the header scan ate would be missing here and nowhere else.
        """
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                recovered = self.parse(rendered)["body"]
                header, _, body = rendered.text.partition("\n\n")
                self.assertEqual(recovered, body)
                self.assertEqual(header.split("\n"), [header.split("\n")[0], header.split("\n")[1]])
                for line in body.split("\n"):
                    if line.strip():
                        self.assertIn(line, recovered)

    def test_the_metadata_is_in_the_body_where_it_can_be_seen(self) -> None:
        body = self.parse(self.files[0])["body"]
        for expected in ("Recording:", "Recorded:", "Transcribed by:", "OneDrive item:", "observed_by: agent"):
            self.assertIn(expected, body, f"{expected!r} did not survive into the body")

    def test_a_third_header_line_would_be_caught_at_render_time(self) -> None:
        """Proof the check has teeth: the mistake somebody will reasonably make is refused."""
        good = self.files[0].text
        with_extra = good.replace(
            "Date:", "Recording: BEACH COURT SITE WALK 270826.m4a\nDate:", 1
        )
        # The record swallows it and still recovers the body, so nothing downstream complains.
        path = os.path.join(self.dir.name, "with-extra.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(with_extra)
        swallowed = parse_texty(path)
        eaten = "Recording: BEACH COURT SITE WALK 270826.m4a"
        self.assertNotIn(eaten, swallowed["body"].split("\n"), "the line should have vanished")
        self.assertNotIn("recording", swallowed, "and it reached no header key either")
        # Our own check does complain, before anything is uploaded.
        problems = outputs.check_contract(with_extra)
        self.assertTrue(problems)
        self.assertTrue(any("disappears without erroring" in p for p in problems), problems)

    # -- no addresses --------------------------------------------------------------

    def test_no_rendered_output_contains_anything_shaped_like_an_address(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(ADDR_RE.findall(rendered.text), [])

    def test_an_address_in_the_source_material_is_removed_and_the_removal_is_stated(self) -> None:
        """Visible, not silent: a reader who can see something was taken out can ask."""
        ctx = build_context(
            transcript=Transcript(
                text="Send it to carel@example.co.za and copy james@example.co.za please.",
                segments=[],
                language="en-ZA",
                engine="test-engine",
            ),
            extraction=support.StubExtraction(summary="He gave two addresses."),
        )
        rendered = outputs.render_transcript(ctx)

        self.assertEqual(ADDR_RE.findall(rendered), [])
        self.assertIn("[address removed]", rendered)
        self.assertIn("2 email addresses were removed", rendered)

    def test_an_address_in_a_filename_never_reaches_the_output(self) -> None:
        """And the recording is still written, rather than quarantined over its own name."""
        ctx = build_context(source_name="Call carel@example.co.za_260827_120055.m4a")
        files = render_all(ctx)

        self.assertEqual(len(files), 3)
        for rendered in files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(ADDR_RE.findall(rendered.text), [])
                self.assertIn("[address removed]", rendered.text.split("\n")[0])
                self.assertIn("removed from this text", rendered.text)

    # -- the file itself -----------------------------------------------------------

    def test_the_three_files_share_one_stamp_and_one_stem(self) -> None:
        names = [f.name for f in self.files]
        self.assertTrue(names[0].startswith("20260827-143005-BEACH COURT SITE WALK 270826-"))
        self.assertTrue(names[0].endswith(".md"))
        self.assertTrue(names[1].endswith("-summary.md"))
        self.assertTrue(names[2].endswith("-actions.md"))
        stem = names[0][: -len(".md")]
        # The two derived files are prefixed so the record's intake skips them: only the
        # transcript is ingested as a source file.
        self.assertTrue(all(name.lstrip("_").startswith(stem) for name in names))
        self.assertFalse(names[0].startswith("_"))
        self.assertTrue(names[1].startswith("_"))
        self.assertTrue(names[2].startswith("_"))

    def test_every_file_begins_with_the_subject_and_ends_with_a_newline(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertTrue(rendered.text.startswith("Subject: "))
                self.assertTrue(rendered.text.endswith("\n"))
                lines = rendered.text.split("\n")
                self.assertTrue(lines[1].startswith("Date: "))
                self.assertEqual(lines[2], "", "the header must be two lines and one blank")

    def test_check_contract_passes_on_what_we_actually_render(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(outputs.check_contract(rendered.text), [])


class TheContractCheckerCatchesEachWayItCanBreak(unittest.TestCase):
    """Each of these was a real way to lose information into the record, silently."""

    GOOD = "Subject: A voice note\nDate: 2026-08-27 14:30:05 +02:00\n\n- Recording: x.m4a\n"

    def test_the_good_file_passes(self) -> None:
        self.assertEqual(outputs.check_contract(self.GOOD), [])

    def test_a_from_header(self) -> None:
        broken = self.GOOD.replace("Subject:", "From: nobody\nSubject:", 1)
        self.assertTrue(any("reclassifies this file as an email" in p
                            for p in outputs.check_contract(broken)))

    def test_a_missing_subject(self) -> None:
        broken = "Date: 2026-08-27 14:30:05 +02:00\n\nbody\n"
        self.assertTrue(any("no Subject:" in p for p in outputs.check_contract(broken)))

    def test_an_iso_t_in_the_date(self) -> None:
        broken = self.GOOD.replace("2026-08-27 14:30:05", "2026-08-27T14:30:05")
        problems = outputs.check_contract(broken, expected_date="2026-08-27")
        self.assertTrue(any("first of the month" in p for p in problems), problems)

    def test_an_email_address_anywhere(self) -> None:
        broken = self.GOOD + "- Reply to somebody@example.com\n"
        self.assertTrue(any("email address" in p for p in outputs.check_contract(broken)))

    def test_the_word_decided_by(self) -> None:
        broken = self.GOOD + "- decided_by: James Janeke\n"
        self.assertTrue(any("cannot decide" in p for p in outputs.check_contract(broken)))

    def test_no_trailing_newline(self) -> None:
        self.assertTrue(any("newline" in p for p in outputs.check_contract(self.GOOD.rstrip())))

    def test_a_transcript_with_no_words_is_refused_rather_than_written_empty(self) -> None:
        ctx = build_context(
            transcript=Transcript(text="", segments=[], language="en-ZA", engine="test-engine")
        )
        with self.assertRaises(OutputContractError):
            outputs.render_transcript(ctx)


if __name__ == "__main__":
    unittest.main()
