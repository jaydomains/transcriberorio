"""Three ways a held passage or an address reached the record anyway.

None of these was a hole in the gate's judgement — the classifier held the right passage
every time. They were holes in the machinery that carries the decision out: a sentence cut
in the wrong place, a name spelled with an accent, a speaker saying the same thing twice.
Each one published something the gate had already decided must not be published, or
destroyed the recording trying not to.
"""

from __future__ import annotations

import unittest

from transcriber import redact
from transcriber.models import strip_emails
from transcriber.sensitivity import _sentence_spans
from transcriber.withheld import HeldSpan

STAMP = "2026-08-26T09:00:00Z"


class ADecimalPointIsNotTheEndOfASentence(unittest.TestCase):
    """The gate holds whole sentences, so where a sentence ends decides what is held."""

    def test_a_rand_amount_does_not_cut_the_held_sentence_in_half(self) -> None:
        said = "Don't minute this: the settlement is R1.5 million and we accept fault."
        spans = _sentence_spans(said)
        self.assertEqual(
            [said[s:e] for s in [spans[0][0]] for e in [spans[0][1]]],
            [said],
            "the sentence split at the decimal, so a hold on it would cover only the part "
            "before the amount and publish 'and we accept fault'",
        )
        self.assertEqual(len(spans), 1)

    def test_the_ordinary_full_stop_still_ends_a_sentence(self) -> None:
        said = "The slab is 3.5 metres. Do not write this down. We move on."
        self.assertEqual(
            [said[s:e].strip() for s, e in _sentence_spans(said)],
            ["The slab is 3.5 metres.", "Do not write this down.", "We move on."],
        )

    def test_a_run_of_stops_still_ends_one(self) -> None:
        said = "Wait... that is wrong. Really? Yes."
        self.assertEqual(len(_sentence_spans(said)), 4)

    def test_a_stop_touching_a_digit_on_one_side_only_still_ends_one(self) -> None:
        self.assertEqual(len(_sentence_spans("The slab is 3. Next item.")), 2)


class AnAddressWithAnAccentIsStillAnAddress(unittest.TestCase):
    """This is the Cape. Müller, José, Voëlklip — and the record must see none of them."""

    ADDRESSES = (
        "jose@example.co.za",
        "josé@example.co.za",
        "müller@site.co.za",
        "joão.silva@kbc.co.za",
        "carel@voëlklip.co.za",
    )

    def test_every_one_of_them_is_removed(self) -> None:
        for address in self.ADDRESSES:
            with self.subTest(address=address):
                self.assertEqual(strip_emails(address), "[address removed]")

    def test_none_of_them_comes_out_half_scrubbed(self) -> None:
        """The worst outcome, worse than doing nothing: it looks handled and is not."""
        for address in self.ADDRESSES:
            with self.subTest(address=address):
                cleaned = strip_emails(address)
                local = address.split("@")[0]
                self.assertNotIn(local, cleaned)
                self.assertNotIn("@", cleaned)

    def test_and_ordinary_speech_is_left_alone(self) -> None:
        for said in (
            "The slab is 3 dot 5 metres at the east end.",
            "Ring James on 021 555 0000 about the scaffold.",
            "Look at the roof before you price it.",
        ):
            with self.subTest(said=said):
                self.assertEqual(strip_emails(said), said)


class ASpeakerRepeatingThemselves(unittest.TestCase):
    """The ordinary case on a phone call, and it used to destroy the recording.

    The masker cut the whole passage, stopped there, and left the repeat in the file. The
    backstop found it and refused the publish, and that refusal is in ``_NEVER_RETRY`` — so
    the recording quarantined for good, with no transcript, no summary and nothing in the
    record, and running it again reached the same answer.
    """

    WORDS = "Sipho got his second written warning on Friday and it is on his file"
    BODY = (
        "James: Right, at Beach Court.\n"
        f"James: {WORDS}.\n"
        "Other: Sorry, say again?\n"
        "James: I said he got his second written warning on Friday, it is done.\n"
    )

    def _span(self) -> HeldSpan:
        start = self.BODY.index(self.WORDS)
        return HeldSpan(
            item_id="01ITEM", start=start, end=start + len(self.WORDS), text=self.WORDS,
            category="staff_matter", recorded_at=STAMP,
        )

    def test_the_repeat_is_cut_too(self) -> None:
        span = self._span()
        redaction = redact.redact_text(self.BODY, (span,), mode="on", held_on=STAMP)
        masked, _touched = redaction.mask(self.BODY)
        self.assertEqual(
            redact.held_words_in(masked, (span,)), [],
            "the masker left held words the backstop can see, which refuses the publish "
            "and quarantines the recording permanently",
        )

    def test_and_the_words_themselves_are_gone_from_the_file(self) -> None:
        span = self._span()
        masked, _ = redact.redact_text(
            self.BODY, (span,), mode="on", held_on=STAMP
        ).mask(self.BODY)
        self.assertNotIn("second written warning", masked)


if __name__ == "__main__":
    unittest.main()
