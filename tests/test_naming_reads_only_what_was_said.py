"""Two ways the namer heard something nobody said.

Both are the same mistake in different clothes: text that merely *contains* a name being
read as text that *says* it. A title is a claim about where he was standing, and a claim
assembled from words nobody spoke is a misfile — the failure this whole feature is built to
avoid, since nobody goes looking for a note filed under the wrong job.
"""

from __future__ import annotations

import unittest

from transcriber.declaration import DEFAULT_WINDOW_S, opening
from transcriber.sitebook import SiteBook, site_vocab


def _book() -> SiteBook:
    """Three real shapes from the record: one name inside another, and a shared word.

    The vocabulary is built the way the record builds it, from the same fields, so the test
    exercises the terms the service will really be matching against.
    """
    sites = {
        "urban-artisan": {"title": "Urban Artisan", "slug": "urban-artisan"},
        "imam-haron-road": {"title": "277 Imam Haron Road", "slug": "imam-haron-road"},
        "beach-court": {"title": "Beach Court", "slug": "beach-court"},
    }
    return SiteBook(sites=sites, vocab=site_vocab({"sites": sites}))


class AWordInsideAnotherWordWasNeverSaid(unittest.TestCase):
    """"Durbanville" is not two mentions of Urban Artisan."""

    def test_a_site_is_not_counted_inside_a_longer_word(self) -> None:
        book = _book()
        said = "We were at Durbanville this morning, then Durbanville again after lunch."
        self.assertNotIn(
            "urban-artisan", book.mentions_of_each(said),
            "'urban' was counted inside 'Durbanville', so a site nobody named would be "
            "voting in the majority test that decides the title",
        )

    def test_a_persons_name_is_not_a_site(self) -> None:
        """Measured on the real book: "Sharon" was scoring 277 Imam Haron Road."""
        book = _book()
        said = "Sharon said she would call back about it on Thursday."
        self.assertNotIn("imam-haron-road", book.mentions_of_each(said))

    def test_but_the_site_he_actually_named_is_still_counted_every_time(self) -> None:
        book = _book()
        said = "This is a walk of Beach Court. Beach Court is three weeks out. Beach Court."
        self.assertEqual(book.mentions_of_each(said).get("beach-court"), 3)


class TheFirstMinuteIsTheFirstMinute(unittest.TestCase):
    """A body whose clock never moves is not a body with a one-minute opening."""

    def _lines(self, stamp: str, count: int) -> str:
        return "\n".join(
            f"[{stamp}] James: line {n} about the roof and the scaffold and the screed"
            for n in range(count)
        )

    def test_a_clock_that_never_advances_does_not_open_the_window_to_everything(self) -> None:
        # engines/openai.py maps a segment with no start to 0.0, so this is a real body
        # shape, not a contrived one: every line reads [00:00].
        body = self._lines("00:00", 80)
        window = opening(body)
        self.assertLess(
            len(window.text), len(body),
            "the whole recording was treated as its opening declaration, so a site named "
            "once at minute seventy would be read as him stating what the recording is",
        )

    def test_and_it_says_it_had_no_usable_clock(self) -> None:
        self.assertFalse(opening(self._lines("00:00", 80)).timed)

    def test_a_real_clock_still_cuts_at_the_window(self) -> None:
        body = "\n".join(
            f"[{m:02d}:00] James: line {m} about the roof and the scaffold and the screed"
            for m in range(10)
        )
        window = opening(body, window_s=DEFAULT_WINDOW_S)
        self.assertTrue(window.timed)
        self.assertIn("line 0", window.text)
        self.assertNotIn("line 5", window.text)


if __name__ == "__main__":
    unittest.main()
