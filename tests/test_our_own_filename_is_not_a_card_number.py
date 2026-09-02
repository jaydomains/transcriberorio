"""The leak backstop reading this service's own filenames as somebody's card number.

The check exists to catch the model restating a held passage in prose of its own — the one
thing word-matching cannot see. It was reading the whole finished file, including the parts
this service wrote itself: the summary and the proposals each carry a backticked
cross-reference to the other two names.

An output stem like ``_20260827-143005-...`` strips to the fourteen digits
``20260827143005``, which sits in the 13-to-19 range the identifier rule calls a card and
passes Luhn about one time in ten. With the gate armed that refuses the publish, and
``HeldTextWouldLeak`` is in ``pipeline._NEVER_RETRY`` — so the recording is quarantined for
ever, every retry renders identical bytes and fails identically, and the morning email
reports a near-leak of a card number that never existed.
"""

from __future__ import annotations

import datetime
import random
import unittest

from transcriber.outputs import HeldTextWouldLeak, RenderedFile, refuse_written_down_again


def _files(stamp: str, body: str | None = None):
    stem = f"_{stamp}-Call Carel_260827_143005-76fc35b7"
    transcript, summary, actions = f"{stem}.md", f"{stem}-summary.md", f"{stem}-actions.md"
    text = body if body is not None else (
        f"Proposals from this recording: `{actions}`\n"
        f"Transcript: `{transcript}`\n"
        "- Audio sha256: 9f2b8c1d4e6a7b3c5d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8f9012345abcd\n"
    )
    return (
        RenderedFile("transcript", transcript, text),
        RenderedFile("summary", summary, text),
        RenderedFile("actions", actions, text),
    )


def _refused(stamp: str, body: str | None = None) -> bool:
    try:
        refuse_written_down_again(_files(stamp, body), source_name="a.m4a", armed=True)
    except HeldTextWouldLeak:
        return True
    return False


class OurOwnFilenameIsNotSomebodysCardNumber(unittest.TestCase):
    def test_the_measured_bad_timestamp_publishes(self) -> None:
        self.assertFalse(
            _refused("20260827-143005"),
            "the recording's own filename was read as an account number and the publish "
            "refused — permanently, since that refusal is never retried",
        )

    def test_no_recording_moment_in_a_year_trips_it(self) -> None:
        """Measured at 10.6% before the fix. Anything above zero is a day's work lost."""
        rng = random.Random(7)
        start = datetime.datetime(2026, 1, 1, 6, 0, 0)
        tripped = [
            stamp for stamp in (
                (start + datetime.timedelta(seconds=rng.randrange(0, 365 * 24 * 3600)))
                .strftime("%Y%m%d-%H%M%S")
                for _ in range(1500)
            ) if _refused(stamp)
        ]
        self.assertEqual(tripped, [], f"{len(tripped)} of 1500 refused; first {tripped[:3]}")

    def test_and_the_provenance_hash_does_not_trip_it_either(self) -> None:
        body = (
            "- Audio sha256: 4111111111111111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "The slab on level two cracked and we will carry the cost.\n"
        )
        self.assertFalse(_refused("20260826-091200", body))


class ButAGenuineNumberInTheModelsProseIsStillRefused(unittest.TestCase):
    """The check must keep doing the one job it was written for."""

    def test_a_bank_account_number_is_refused(self) -> None:
        self.assertTrue(_refused(
            "20260826-091200",
            "Account 62154893001 at Standard Bank for the payment.",
        ))

    def test_a_card_number_is_refused(self) -> None:
        self.assertTrue(_refused(
            "20260826-091200",
            "The card used was 4111 1111 1111 1111 on the day.",
        ))

    def test_ordinary_site_prose_still_publishes(self) -> None:
        self.assertFalse(_refused(
            "20260826-091200",
            "The slab on level two cracked and we will carry the cost ourselves.",
        ))

    def test_and_a_price_still_publishes_because_prices_flow(self) -> None:
        self.assertFalse(_refused(
            "20260826-091200",
            "The quote to the body corporate is R4,500 for the torch-on repair.",
        ))


if __name__ == "__main__":
    unittest.main()
