"""One address, thirteen spellings, and the three surfaces it used to reach anyway.

The service's headline promise is that an email address never reaches a published file.
It was true of exactly one spelling. Seven ordinary ways of writing the same address —
brackets round the ``at``, spaces round the ``@``, the fullwidth ``@`` a phone with a CJK
keyboard writes, the ``%40`` left behind by a URL, the ``&#64;`` left behind by a web
page — went through the masker, through the contract check and into all three files
unaltered, and a comma before the word ``at`` was enough on its own to defeat the
spoken-out-loud pattern.

Three things are asserted here, and they are different things:

* **the masker removes it**, in every spelling, from every one of the three files;
* **the masker is left alone by ordinary site speech** — "look at the roof",
  "set it at 3 dot 5 metres" — because a redaction that fires on a sentence about a roof
  makes the service unusable and teaches its reader to ignore the marker;
* **the backstop finds it without the masker's help.** :func:`outputs.check_contract` used
  to ask the masker's own two detectors the question the masker had just asked itself, so a
  spelling the masker could not see was a spelling the backstop could not see either: they
  failed together by construction, which is the one thing a backstop may not do. The test
  for that takes the masker's detectors away and asserts the file is still refused.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from transcriber import outputs
from transcriber.models import Segment, Transcript, strip_dictated_emails, strip_emails

from . import support
from .test_output_contract import build_context

#: The same address, written the way it actually turns up. The last three are mixed
#: spellings — half symbol and half words — which is what a person reading an address off a
#: screen to somebody writing it down produces.
SPELLINGS = (
    "carel@example.co.za",
    "carel(at)example.co.za",
    "carel[at]example.co.za",
    "carel {at} example.co.za",
    "carel @ example.co.za",
    "carel at example.co.za",
    "carel＠example.co.za",
    "carel﹫example.co.za",
    "carel%40example.co.za",
    "carel&#64;example.co.za",
    "carel at example dot co dot za",
    "Carel, at example dot co dot za",
    "carel@example dot co dot za",
    "carel at example.co dot za",
)

#: Ordinary site speech that shares the shape and is not an address. Every one of these is
#: a sentence he actually says.
NOT_ADDRESSES = (
    "Look at the roof before you price it.",
    "meet me at the roof dot com",
    "set it at 3 dot 5 metres",
    "look at the parapet on the north side",
    "The slab is 3 dot 5 metres at the east end.",
    "Ring James on 021 555 0000 about the scaffold.",
    "Sit at reception. Ok then.",
    "We were at Beach Court. Then we left.",
    "Meeting at 08h00. Thanks.",
)

_HEADER = "Subject: A site walk — voice note transcript\nDate: 2026-08-27 14:30:05 +02:00\n\n"


def _remains(text: str) -> list[str]:
    """The parts of the address a reader could still put back together."""
    return [
        piece
        for piece in ("carel", "example.co", "example dot co", "%40", "&#64;")
        if piece.lower() in text.lower()
    ]


class NoSpellingOfAnAddressReachesAFile(unittest.TestCase):
    """Through the real renderers, because that is the only place it matters."""

    def _rendered(self, spelling: str) -> dict[str, outputs.RenderedFile]:
        said = f"Right, at Beach Court. Send it to {spelling} when you can."
        ctx = build_context(
            transcript=Transcript(
                text=said,
                segments=[Segment(0.0, 8.0, "James", said)],
                language="en-ZA",
                engine="test-engine",
            ),
            extraction=support.StubExtraction(
                summary=f"He said to send it to {spelling}.",
                site="Beach Court",
            ),
        )
        return {f.kind: f for f in outputs.render_all(ctx)}

    def test_none_of_the_three_files_carries_it(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                for kind, rendered in self._rendered(spelling).items():
                    self.assertEqual(
                        _remains(rendered.text), [],
                        f"the {kind} published the address written as {spelling!r}",
                    )

    def test_and_the_file_says_an_address_was_taken_out(self) -> None:
        """Visible, not silent: a reader who can see the hole can ask what was in it."""
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                transcript = self._rendered(spelling)["transcript"].text
                self.assertIn("address", transcript.lower())

    def test_and_the_contract_check_is_clean_on_what_we_did_publish(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                for kind, rendered in self._rendered(spelling).items():
                    self.assertEqual(
                        outputs.check_contract(rendered.text), [],
                        f"the {kind} was published and then refused by its own check",
                    )

    def test_ordinary_site_speech_is_left_exactly_as_it_was(self) -> None:
        for said in NOT_ADDRESSES:
            with self.subTest(said=said):
                self.assertEqual(strip_emails(said), said)
                self.assertEqual(strip_dictated_emails(said), said)
                self.assertEqual(outputs.check_contract(_HEADER + said + "\n"), [])


class TheBackstopFindsItWithoutTheMasker(unittest.TestCase):
    """The masker is the thing that might have a bug. A guard that shares its reasoning
    with the masker guards nothing, and this one shared both of its detectors with it.

    So the detectors are taken away entirely. What is left is the backstop's own reading of
    a normalised copy of the file, and it has to be enough on its own.
    """

    NEVER_MATCHES = re.compile(r"(?!x)x")

    def _blind_masker(self):
        return mock.patch.multiple(
            outputs,
            contains_email=lambda text: False,
            contains_dictated_email=lambda text: False,
            EMAIL_RE=self.NEVER_MATCHES,
            DICTATED_EMAIL_RE=self.NEVER_MATCHES,
        )

    def test_a_file_carrying_an_address_is_still_refused(self) -> None:
        for spelling in SPELLINGS:
            if " at " in f" {spelling} " and "@" not in spelling:
                # The word "at" on its own is the one spelling the backstop leaves to the
                # masker's own pattern, and deliberately: nothing can tell "carel at
                # example.co.za" from "the drawings are at kbc.co.za" without knowing which
                # words are names. A backstop that guessed would refuse ordinary sentences,
                # and a refusal here is never retried — the recording would be lost to it.
                continue
            with self.subTest(spelling=spelling):
                with self._blind_masker():
                    problems = outputs.check_contract(_HEADER + spelling + "\n")
                self.assertTrue(
                    any("address" in p for p in problems),
                    f"the backstop missed {spelling!r} with the masker blinded, which is "
                    f"how seven spellings reached the record",
                )

    def test_and_it_does_not_refuse_an_ordinary_sentence(self) -> None:
        """A backstop wider than the masker is the other way of losing the recording.

        It refuses a publish the masker can never satisfy, and this module's refusals are
        not retried, so the recording quarantines with none of its three files written.
        """
        for said in NOT_ADDRESSES:
            with self.subTest(said=said):
                self.assertEqual(outputs.check_contract(_HEADER + said + "\n"), [])


class AnAddressCutInHalfBySilence(unittest.TestCase):
    """Segments break on a speaker change or a pause over 0.9 s — so on a pause in the
    middle of an address, which is where people pause when they are dictating one.

    The two halves were published one under the other, whole and adjacent, and the contract
    check saw nothing: it reads the finished file, where a timestamp and a speaker name sit
    between them and no pattern can read across it.
    """

    def _ctx(self):
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

    def test_neither_half_is_published(self) -> None:
        transcript = [f for f in outputs.render_all(self._ctx()) if f.kind == "transcript"][0]
        self.assertEqual(_remains(transcript.text), [])
        self.assertIn("[address removed]", transcript.text)

    def test_and_the_words_either_side_of_it_survive(self) -> None:
        """A redaction that eats the sentence around it is a corruption of the evidence."""
        transcript = [f for f in outputs.render_all(self._ctx()) if f.kind == "transcript"][0]
        self.assertIn("Send it to", transcript.text)
        self.assertIn("when you can.", transcript.text)

    def test_and_the_backstop_refuses_it_if_the_masking_ever_stops_happening(self) -> None:
        leaked = (
            _HEADER
            + "## What was said\n\n"
            + "[00:06:10] James: Send it to carel@\n"
            + "[00:06:17] James: example.co.za when you can.\n"
        )
        self.assertTrue(
            any("address" in p for p in outputs.check_contract(leaked)),
            "a silent publish is what this check exists to turn into a loud refusal",
        )


class TheFilenameIsTheOneThatCannotBeCorrected(unittest.TestCase):
    """It goes into OneDrive, into the ledger, into the URL the downstream flow PUTs to and
    into a git commit in the record. Nothing edits it afterwards, so it gets the loud answer.
    """

    def test_no_spelling_survives_into_any_of_the_three_names(self) -> None:
        for spelling in SPELLINGS:
            with self.subTest(spelling=spelling):
                ctx = build_context(source_name=f"Call {spelling}_260806_150000.m4a")
                for rendered in outputs.render_all(ctx):
                    self.assertEqual(
                        _remains(rendered.name), [],
                        f"the address written as {spelling!r} reached a filename",
                    )
                    self.assertIn("address-removed", rendered.name)

    def test_check_name_refuses_the_lookalike_symbols_by_name(self) -> None:
        for symbol in ("@", "＠", "﹫"):
            with self.subTest(symbol=symbol):
                name = f"20260827-143005-Call carel{symbol}example.co.za-1a2b3c4d.md"
                self.assertTrue(
                    outputs.check_name(name),
                    "a filename carrying an address must be refused, not published",
                )

    def test_and_an_ordinary_name_is_still_accepted(self) -> None:
        self.assertEqual(outputs.check_name("20260827-143005-Call Carel-1a2b3c4d.md"), [])
        self.assertEqual(
            outputs.check_name("20260827-143005-BEACH COURT SITE WALK 270826-1a2b3c4d.md"), []
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
