"""Quote verification: a fabricated quote must never reach an output.

Every extracted item carries the words that produced it, and those words are confirmed to
be in the transcript before the item is allowed anywhere near a file a person will read.
Without this, one misheard word hardens into somebody's task and the record's own rule —
that a proposal is only as good as the quote behind it — becomes decoration.

Two directions, and both matter:

  * a quote the recording does not contain is REJECTED, however plausible it reads;
  * a quote that differs only in whitespace, case, or the shape of a punctuation mark is
    ACCEPTED, because a model typing a straight apostrophe has invented nothing.
"""

from __future__ import annotations

import unittest

from transcriber.extract import (
    MIN_FUZZY_CHARS,
    QuoteCheck,
    normalise_for_match,
    verify_quote,
)

TRANSCRIPT = (
    "Right, I'm at Beach Court now. Spoke to Carel about the roof leak at unit four — he "
    "says the sheeting was never sealed at the ridge. I told him we'd get a price for the "
    "remedial before the end of the month. He's asking whether the retention gets released "
    "on practical completion or after the defects period. I said I'd check with James and "
    "come back to him on Tuesday."
)


class AFabricationIsRejected(unittest.TestCase):
    def test_a_quote_that_is_simply_not_there(self) -> None:
        check = verify_quote("he confirmed the retention has been released", TRANSCRIPT)

        self.assertFalse(check.ok)
        self.assertFalse(check, "QuoteCheck must be falsy when the quote was not found")
        self.assertEqual(check.method, "none")
        self.assertIn("not found in the transcript", check.reason)

    def test_a_quote_made_of_the_transcript_s_own_words_in_an_order_it_never_used(self) -> None:
        """The dangerous shape: every word is real, the sentence is not."""
        check = verify_quote(
            "the retention was released at practical completion on the roof at unit four",
            TRANSCRIPT,
        )
        self.assertFalse(check.ok, f"a recombined sentence was accepted at ratio {check.ratio}")

    def test_a_quote_that_changes_one_number(self) -> None:
        """"unit 4" and "unit 14" are the same shape and a different flat."""
        check = verify_quote("the roof leak at unit fourteen", TRANSCRIPT)
        self.assertFalse(check.ok)

    def test_a_quote_that_reverses_the_meaning_by_one_word(self) -> None:
        check = verify_quote("the sheeting was always sealed at the ridge", TRANSCRIPT)
        self.assertFalse(check.ok, "a negation flip was accepted as the same quote")

    def test_a_near_miss_still_records_what_it_nearly_matched(self) -> None:
        """A person reading the review list needs the closest real passage, not a bare no."""
        check = verify_quote("the sheeting was never sealed at the parapet upstand", TRANSCRIPT)
        self.assertFalse(check.ok)
        self.assertGreater(check.ratio, 0.0)
        self.assertIn("sheeting", check.matched_text)

    def test_an_added_word_never_survives_into_the_quote_that_is_written(self) -> None:
        """The fuzzy path tolerates a stray word; the file still carries the recording's own.

        This is the load-bearing half of the accept branch. A quote may be admitted on
        similarity, but what gets written out is the transcript's span — so a word the model
        added cannot end up inside quotation marks in front of a person.
        """
        check = verify_quote("the sheeting was never sealed at the ridge cap", TRANSCRIPT)
        self.assertTrue(check.ok, check.reason)
        self.assertNotIn("cap", check.matched_text)
        self.assertIn(check.matched_text, TRANSCRIPT)

    def test_an_empty_quote_is_rejected_rather_than_treated_as_trivially_true(self) -> None:
        self.assertFalse(verify_quote("", TRANSCRIPT).ok)
        self.assertFalse(verify_quote("   ", TRANSCRIPT).ok)

    def test_punctuation_alone_is_rejected(self) -> None:
        self.assertFalse(verify_quote("...", TRANSCRIPT).ok)

    def test_nothing_matches_against_an_empty_transcript(self) -> None:
        check = verify_quote("spoke to Carel about the roof leak", "")
        self.assertFalse(check.ok)
        self.assertIn("no transcript", check.reason)

    def test_a_short_quote_must_match_exactly_or_not_at_all(self) -> None:
        """Fuzzy matching on a handful of characters means nothing, so it is not offered."""
        self.assertLess(len("the roof"), MIN_FUZZY_CHARS)
        check = verify_quote("the roofs", TRANSCRIPT)
        self.assertFalse(check.ok)
        self.assertIn("too short for a fuzzy match", check.reason)


class WhitespaceAndCaseAreAccepted(unittest.TestCase):
    def test_an_exact_quote(self) -> None:
        check = verify_quote("the sheeting was never sealed at the ridge", TRANSCRIPT)
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.method, "exact")
        self.assertEqual(check.ratio, 1.0)

    def test_a_quote_differing_only_in_case(self) -> None:
        check = verify_quote("THE SHEETING WAS NEVER SEALED AT THE RIDGE", TRANSCRIPT)
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.method, "exact")

    def test_a_quote_differing_only_in_whitespace(self) -> None:
        check = verify_quote(
            "  the sheeting   was never\n\tsealed at the ridge  ", TRANSCRIPT
        )
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.method, "exact")

    def test_a_quote_differing_only_in_the_shape_of_an_apostrophe(self) -> None:
        """A curly quote in the transcript and a straight one from the model are one quote."""
        check = verify_quote("I told him we'd get a price for the remedial", TRANSCRIPT)
        self.assertTrue(check.ok, check.reason)

        curly = TRANSCRIPT.replace("we'd", "we’d").replace("I'm", "I’m")
        self.assertTrue(verify_quote("I told him we'd get a price", curly).ok)
        self.assertTrue(verify_quote("I told him we’d get a price", TRANSCRIPT).ok)

    def test_a_quote_differing_only_in_the_shape_of_a_dash(self) -> None:
        check = verify_quote("the roof leak at unit four - he says the sheeting", TRANSCRIPT)
        self.assertTrue(check.ok, check.reason)

    def test_the_words_written_out_are_the_transcript_s_own(self) -> None:
        """We write what the recording says, never the model's retyping of it."""
        check = verify_quote("SPOKE TO CAREL ABOUT THE ROOF LEAK", TRANSCRIPT)
        self.assertTrue(check.ok)
        self.assertEqual(check.matched_text, "Spoke to Carel about the roof leak")
        self.assertIn(check.matched_text, TRANSCRIPT)

    def test_normalisation_is_what_it_says_it_is(self) -> None:
        self.assertEqual(normalise_for_match("  A  B’s—c  "), "a b's-c")


class FuzzyMatchingStaysHonest(unittest.TestCase):
    """The fuzzy path exists for transcription noise, not for rewriting."""

    def test_a_single_transposed_character_in_a_long_quote_is_accepted(self) -> None:
        check = verify_quote(
            "he is asking whether the retention gets released on practical completion",
            TRANSCRIPT,
        )
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.method, "fuzzy")
        self.assertGreaterEqual(check.ratio, 0.92)

    def test_fuzzy_can_be_switched_off_where_only_exactness_will_do(self) -> None:
        check = verify_quote(
            "he is asking whether the retention gets released on practical completion",
            TRANSCRIPT,
            allow_fuzzy=False,
        )
        self.assertFalse(check.ok)

    def test_the_check_reports_itself_in_full(self) -> None:
        as_dict = verify_quote("the sheeting was never sealed at the ridge", TRANSCRIPT).to_dict()
        self.assertEqual(
            sorted(as_dict), ["coverage", "method", "ok", "ratio", "reason"]
        )
        self.assertIsInstance(QuoteCheck(False, "none", 0.0).to_dict(), dict)


if __name__ == "__main__":
    unittest.main()
