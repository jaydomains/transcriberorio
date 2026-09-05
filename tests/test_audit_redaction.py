"""An address is an address in every spelling, on every surface, at every cut.

The promise the service makes about an email address is unconditional: it never writes one
down. What it actually enforced was narrower — one spelling of the separator, the literal
``@`` — so the same address written any of the ordinary other ways went through the masker,
through the contract check, into all three published files and, through the filename, into
OneDrive and into a commit downstream where it can no longer be taken out.

This module is the audit's own check on that, and it asserts four separate things because
they can fail separately:

* **the two renderers remove it.** The transcript and the summary are checked one at a
  time and not through :func:`~transcriber.outputs.render_all`, because they take the words
  from different places — the transcript from the recording, the summary from what the
  analysis pass wrote — and a masker wired into one of those paths and not the other looks
  fine from the outside.
* **the backstop finds it on its own.** :func:`~transcriber.outputs.check_contract` is the
  guard that runs after the masking, and for it to be worth anything it has to be able to
  disagree with the masker. It used to call the masker's own two detectors, so it agreed
  with the masker by construction and every spelling the masker missed it missed too. The
  test takes those detectors away and requires the refusal anyway.
* **a pause in the middle of an address does not defeat either of them.** Segments break on
  a pause over 0.9 s, which is exactly where somebody dictating an address stops.
* **a filename is refused rather than published.** Everything else this service writes can
  be corrected afterwards. That one cannot.

The controls at the end matter as much as the assertions: a redaction that also eats "look
at the roof" is a redaction whose marker a reader learns to scroll past.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from transcriber import outputs
from transcriber.models import (
    Segment,
    Transcript,
    contains_dictated_email,
    strip_dictated_emails,
    strip_emails,
)

from . import support
from .test_output_contract import build_context

#: One address, written with the separator spelled every way that still delivers mail. Each
#: is labelled with where it comes from, because the labels are the argument for the list:
#: none of these is exotic, and every one of them was published untouched.
SYMBOL_SPELLINGS = (
    ("the plain one", "carel@example.co.za"),
    ("round brackets round the at", "carel(at)example.co.za"),
    ("square brackets round the at", "carel[at]example.co.za"),
    ("braces, with spaces", "carel {at} example.co.za"),
    ("angle brackets, with spaces", "carel <at> example.co.za"),
    ("a space either side of the symbol", "carel @ example.co.za"),
    ("the fullwidth symbol a CJK keyboard writes", "carel＠example.co.za"),
    ("the small commercial at", "carel﹫example.co.za"),
    ("pasted out of a URL", "carel%40example.co.za"),
    ("pasted out of a web page", "carel&#64;example.co.za"),
    ("the same entity, zero padded", "carel&#064;example.co.za"),
    ("the hexadecimal entity", "carel&#x40;example.co.za"),
    ("the named entity", "carel&commat;example.co.za"),
)

#: The same address said out loud, and the half-and-half forms a person produces when they
#: are reading one off a screen to somebody who is writing it down.
SPOKEN_SPELLINGS = (
    ("said out loud", "carel at example dot co dot za"),
    ("with the comma a reader puts in", "Carel, at example dot co dot za"),
    ("symbol, then the words", "carel@example dot co dot za"),
    ("the word at, then real full stops", "carel at example.co.za"),
    ("the words, with one real full stop", "carel at example.co dot za"),
)

ALL_SPELLINGS = SYMBOL_SPELLINGS + SPOKEN_SPELLINGS

#: The spellings that carry a symbol somewhere in them, in any of its encodings. These are
#: the ones the backstop can recover on its own, by reading a copy of the file with the
#: spellings folded into one — see :class:`TheBackstopDoesNotShareTheMaskersEyes`.
SYMBOL_BEARING = SYMBOL_SPELLINGS + (
    ("symbol, then the words", "carel@example dot co dot za"),
)

#: Ordinary site speech that has the shape of an address and is not one. Every redaction
#: pattern here has to leave all of it alone.
NOT_ADDRESSES = (
    "Look at the roof before you price it.",
    "meet me at the roof dot com",
    "set it at 3 dot 5 metres",
    "The slab is 3 dot 5 metres at the east end.",
    "Sit at reception. Ok then.",
    "We were at Beach Court. Then we left.",
    "Meeting at 08h00. Thanks.",
    "Ring James on 021 555 0000 about the scaffold.",
)

#: A header block the record reads back the way we wrote it, so a test that is about an
#: address is not also a test about the header.
HEADER = "Subject: A site walk — voice note transcript\nDate: 2026-08-27 14:30:05 +02:00\n\n"

#: The pieces of the address a reader could still put back together. Checked instead of the
#: whole string because a half-removed address is the failure that looks most like a
#: success: "carel[address removed]" reads as handled and is not.
RECONSTRUCTABLE = (
    "carel",
    "example.co",
    "example dot co",
    "%40",
    "&#64;",
    "&#064;",
    "&#x40;",
    "&commat;",
    "＠",
    "﹫",
)


def residue(text: str) -> list[str]:
    """Whatever is left of the address in this text. Empty is the only passing answer."""
    return [piece for piece in RECONSTRUCTABLE if piece.lower() in (text or "").lower()]


def context_for(said: str, summary: str):
    """A recording whose only address is the one under test, in both of the two paths.

    The transcript carries the spoken words and the summary carries what the analysis pass
    wrote about them. Nothing else in the context mentions the name or the host, so anything
    :func:`residue` finds in a rendered file came from the address and from nowhere else.
    """
    return build_context(
        transcript=Transcript(
            text=said,
            segments=[Segment(0.0, 8.0, "James", said)],
            language="en-ZA",
            engine="test-engine",
        ),
        extraction=support.StubExtraction(summary=summary, site="Beach Court"),
    )


class TheTranscriptCarriesNoSpellingOfAnAddress(unittest.TestCase):
    """Through the real renderer, because the renderer is what writes the published bytes."""

    def rendered(self, spelling: str) -> str:
        said = f"Right. Send it to {spelling} when you can."
        return outputs.render_transcript(context_for(said, "He gave an address to write to."))

    def test_every_spelling_is_gone_from_the_file(self) -> None:
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                text = self.rendered(spelling)
                self.assertEqual(
                    residue(text), [],
                    f"the transcript published the address written as {spelling!r} "
                    f"({label})",
                )

    def test_and_the_marker_is_left_where_the_words_were(self) -> None:
        """Visible, not silent. A reader who can see the hole can ask what was in it; a
        reader looking at a quietly shortened sentence cannot."""
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                self.assertIn("[address removed]", self.rendered(spelling))

    def test_and_the_sentence_around_it_survives(self) -> None:
        """A redaction that eats the words either side of the address has corrupted the
        evidence, which is the one thing the transcript exists to be."""
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                text = self.rendered(spelling)
                self.assertIn("Send it to", text)
                self.assertIn("when you can.", text)

    def test_and_the_file_says_in_words_that_something_was_taken_out(self) -> None:
        """The note is the difference between a redaction and a quiet corruption of a
        quote. A reader who is told an address was removed can ask what it was; a reader
        looking at a sentence that is simply shorter than it was has nothing to go on."""
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                note = [
                    line
                    for line in self.rendered(spelling).split("\n")
                    if line.startswith("- Note:") and "removed" in line
                ]
                self.assertTrue(
                    note,
                    f"the address written as {spelling!r} ({label}) was taken out of the "
                    f"transcript without the file saying so",
                )


class TheSummaryCarriesNoSpellingOfAnAddress(unittest.TestCase):
    """The second path, and a separate test on purpose.

    The summary is not the recording: it is what the analysis pass wrote about it. A model
    that copies an address out of a transcript into a sentence of its own puts a working
    address into the record just as surely as the recording would have, and it arrives at
    the renderer through different code.
    """

    def rendered(self, spelling: str) -> str:
        said = f"Right. Send it to {spelling} when you can."
        return outputs.render_summary(context_for(said, f"He said to write to {spelling}."))

    def test_every_spelling_is_gone_from_the_file(self) -> None:
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                text = self.rendered(spelling)
                self.assertEqual(
                    residue(text), [],
                    f"the summary published the address written as {spelling!r} ({label})",
                )

    def test_and_the_marker_is_left_where_the_words_were(self) -> None:
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                self.assertIn("[address removed]", self.rendered(spelling))

    def test_and_the_file_says_in_words_that_something_was_taken_out(self) -> None:
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                note = [
                    line
                    for line in self.rendered(spelling).split("\n")
                    if line.startswith("- Note:") and "removed" in line
                ]
                self.assertTrue(
                    note,
                    f"the address written as {spelling!r} ({label}) was taken out of the "
                    f"summary without the file saying so",
                )


class TheBackstopDoesNotShareTheMaskersEyes(unittest.TestCase):
    """The one property that makes a backstop a backstop: it can catch what the masker missed.

    The check used to ask :func:`transcriber.models.contains_email` and
    :func:`transcriber.models.contains_dictated_email` — the masker's own two detectors — the
    question the masker had just asked itself with the same two patterns. Anything invisible
    to the masker was therefore invisible to the guard, and the two of them failed together
    silently, which is worse than having no guard at all: a publish that passed a check reads
    as a publish that was checked.

    So both detectors and both patterns are taken away, and the file has to be refused
    anyway, out of the backstop's own separate reading.
    """

    NEVER_MATCHES = re.compile(r"(?!x)x")

    def blinded_masker(self):
        return mock.patch.multiple(
            outputs,
            contains_email=lambda text: False,
            contains_dictated_email=lambda text: False,
            EMAIL_RE=self.NEVER_MATCHES,
            DICTATED_EMAIL_RE=self.NEVER_MATCHES,
        )

    def test_the_blinding_is_real(self) -> None:
        """If the patch missed a name, every assertion below would pass for the wrong
        reason — the masker would be doing the work and the backstop would still be
        untested. So the blinding is proved first, on the one spelling nobody disputes."""
        with self.blinded_masker():
            self.assertFalse(outputs.contains_email("carel@example.co.za"))
            self.assertFalse(outputs.contains_dictated_email("carel at example dot co dot za"))
            self.assertIsNone(outputs.EMAIL_RE.search("carel@example.co.za"))

    def test_a_file_carrying_a_symbol_spelling_is_still_refused(self) -> None:
        for label, spelling in SYMBOL_BEARING:
            with self.subTest(label):
                with self.blinded_masker():
                    problems = outputs.check_contract(HEADER + spelling + "\n")
                self.assertTrue(
                    any("address" in problem for problem in problems),
                    f"with the masker blinded the backstop did not see {spelling!r} "
                    f"({label}), which is how a spelling reaches the record",
                )

    def test_and_it_reads_across_a_line_break_the_masker_never_saw(self) -> None:
        """The published file is one line per segment. An address the engine cut in half sits
        whole across two of those lines, and no pattern reads across a timestamp and a
        speaker name. The backstop reads a copy with the line prefixes taken off."""
        leaked = (
            HEADER
            + "## What was said\n\n"
            + "[00:06:10] James: Send it to carel@\n"
            + "[00:06:17] James: example.co.za when you can.\n"
        )
        with self.blinded_masker():
            problems = outputs.check_contract(leaked)
        self.assertTrue(
            any("address" in problem for problem in problems),
            "an address split across two segment lines was published silently",
        )

    def test_the_word_at_alone_stays_the_maskers_job(self) -> None:
        """Stated as a test so it is a decision and not an oversight.

        Nothing separates "carel at example.co.za" from "the drawings are at kbc.co.za"
        without knowing which words are names. A backstop that folded the bare word ``at``
        into a symbol would refuse ordinary sentences, and a refusal here is never retried —
        the recording quarantines with none of its three files written. That spelling is
        covered by the masker's own dictated pattern instead, which is asserted here so the
        pair of them still covers it.
        """
        for label, spelling in SPOKEN_SPELLINGS:
            if "@" in spelling:
                continue
            with self.subTest(label):
                self.assertTrue(
                    contains_dictated_email(spelling),
                    f"nothing at all sees {spelling!r} ({label})",
                )

    def test_and_the_backstop_does_not_refuse_an_ordinary_sentence(self) -> None:
        """A backstop wider than the masker is the other way of losing a recording: it
        refuses a publish the masker can never satisfy, forever."""
        for said in NOT_ADDRESSES:
            with self.subTest(said=said):
                self.assertEqual(outputs.check_contract(HEADER + said + "\n"), [])


class APauseInTheMiddleOfAnAddress(unittest.TestCase):
    """The exact recording that got through: an address dictated with a breath in it.

    Segments are cut on a speaker change or a pause over 0.9 s. Five seconds of silence
    between "carel@" and "example.co.za" is a person thinking, and it put the two halves of
    a working address into the published transcript one line under the other with a
    timestamp between them, where the contract check could not see either half.
    """

    def context(self):
        return build_context(
            transcript=Transcript(
                text="Send it to carel@example.co.za when you can.",
                segments=[
                    Segment(370.0, 372.0, "James", "Send it to carel@"),
                    Segment(377.0, 380.0, "James", "example.co.za when you can."),
                ],
                language="en-ZA",
                engine="test-engine",
            ),
            extraction=support.StubExtraction(summary="He gave an address.", site="Beach Court"),
        )

    def test_neither_half_reaches_the_file(self) -> None:
        text = outputs.render_transcript(self.context())
        self.assertEqual(
            residue(text), [],
            "the two halves of the address were published one line under the other",
        )

    def test_and_the_marker_is_in_the_place_it_was_said(self) -> None:
        text = outputs.render_transcript(self.context())
        self.assertIn("[address removed]", text)

    def test_and_the_words_either_side_of_the_cut_survive(self) -> None:
        text = outputs.render_transcript(self.context())
        self.assertIn("Send it to", text)
        self.assertIn("when you can.", text)

    def test_and_both_timestamps_are_still_there(self) -> None:
        """The masking works on the segment texts, so it must not disturb the lines
        themselves: a transcript that loses a segment loses the timing of what was said."""
        text = outputs.render_transcript(self.context())
        self.assertIn("[00:06:10]", text)
        self.assertIn("[00:06:17]", text)

    def test_and_it_is_still_gone_once_the_lines_are_joined_back_up(self) -> None:
        """The reader of this file is not obliged to read it line by line. Anybody who
        copies the transcript into an email, and every tool that flattens it, joins the two
        segment lines back into one run of text — and that is where an address masked only
        as far as the end of a line becomes an address again."""
        flattened = re.sub(r"\s+", " ", outputs.render_transcript(self.context()))
        self.assertEqual(residue(flattened), [])

    def test_and_the_spoken_form_split_across_a_cut_goes_too(self) -> None:
        """Same cut, said out loud instead of spelled with a symbol."""
        ctx = build_context(
            transcript=Transcript(
                text="Send it to carel at example dot co dot za when you can.",
                segments=[
                    Segment(370.0, 372.0, "James", "Send it to carel at example"),
                    Segment(377.0, 380.0, "James", "dot co dot za when you can."),
                ],
                language="en-ZA",
                engine="test-engine",
            ),
            extraction=support.StubExtraction(summary="He gave an address.", site="Beach Court"),
        )
        text = outputs.render_transcript(ctx)
        self.assertEqual(residue(text), [])
        self.assertIn("[address removed]", text)


class TheFilenameIsTheSurfaceThatCannotBeCorrected(unittest.TestCase):
    """It goes into OneDrive, into the ledger, into the URL the downstream flow writes to and
    into a commit in the record. Nothing edits it afterwards, so the answer here is a loud
    refusal rather than a quiet fix.
    """

    def test_check_name_refuses_every_spelling(self) -> None:
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                name = f"20260827-143005-Call {spelling}-1a2b3c4d.md"
                problems = outputs.check_name(name)
                self.assertTrue(
                    any("address" in problem for problem in problems),
                    f"a filename carrying the address written as {spelling!r} ({label}) "
                    f"would have been published, and a filename cannot be taken back",
                )

    def test_the_lookalike_symbols_are_refused_the_way_the_plain_one_is(self) -> None:
        """``"@" in name`` was the whole check, and it is blind to two of the three symbols a
        phone can produce."""
        for symbol in ("@", "＠", "﹫"):
            with self.subTest(symbol=symbol):
                name = f"20260827-143005-Call carel{symbol}example.co.za-1a2b3c4d.md"
                self.assertTrue(
                    any("address" in problem for problem in outputs.check_name(name)),
                    "one spelling of the symbol was refused and another was published",
                )

    def test_a_recording_named_after_an_address_still_gets_three_clean_names(self) -> None:
        """Refusing is the last resort, not the first. The redaction runs on the source stem
        before the names are built, so an ordinary recording named this way is published
        under redacted names rather than quarantined.

        The ".za" left behind by the redaction is deliberate and is checked for elsewhere:
        it is a top-level domain on its own and nothing can be reconstructed from it.
        """
        for label, spelling in ALL_SPELLINGS:
            with self.subTest(label):
                ctx = build_context(source_name=f"Call {spelling}_260806_150000.m4a")
                for rendered in outputs.render_all(ctx):
                    self.assertEqual(
                        residue(rendered.name), [],
                        f"the address written as {spelling!r} ({label}) reached the "
                        f"{rendered.kind} filename",
                    )
                    self.assertIn("address-removed", rendered.name)
                    self.assertEqual(outputs.check_name(rendered.name), [])

    def test_and_an_ordinary_name_is_still_accepted(self) -> None:
        """The cost of this guard is a quarantine, so it must not fire on his own filenames."""
        for name in (
            "20260827-143005-Call Carel about the roof-1a2b3c4d.md",
            "20260827-143005-BEACH COURT SITE WALK 270826-1a2b3c4d.md",
            "20260827-143005-Meeting at 8 on site-1a2b3c4d.md",
        ):
            with self.subTest(name=name):
                self.assertEqual(outputs.check_name(name), [])


class OrdinarySiteSpeechIsLeftAlone(unittest.TestCase):
    """The control, and it carries as much weight as anything above it.

    The patterns were widened to catch a real full stop where a spoken "dot" was expected,
    which is a big widening. If the price of it were that "look at the roof" comes back with
    a marker in it, the marker would stop meaning anything and a reader would learn to
    scroll past the one that mattered.
    """

    def test_the_maskers_leave_it_byte_for_byte(self) -> None:
        for said in NOT_ADDRESSES:
            with self.subTest(said=said):
                self.assertEqual(strip_emails(said), said)
                self.assertEqual(strip_dictated_emails(said), said)

    def test_and_it_reaches_the_published_transcript_unaltered(self) -> None:
        for said in NOT_ADDRESSES:
            with self.subTest(said=said):
                text = outputs.render_transcript(context_for(said, "Nothing much was said."))
                self.assertIn(said, text)
                self.assertNotIn("[address removed]", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
