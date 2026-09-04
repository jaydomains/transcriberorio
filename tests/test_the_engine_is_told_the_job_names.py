"""The engine is told what the jobs are called, because a word it never heard is gone.

This is the fix for a failure found in a real recording rather than imagined. On one HQ
site walk the firm's Lonehill job is discussed twice, and the transcription wrote:

    "your consumption is also going to be wrong **on loan**"
    "we've been the same issue **at lo**"

Both are Lonehill. Obvious to anybody who was there; invisible to any matcher, however
clever, because *the name is not in the text*. No downstream cleverness recovers a word the
engine never wrote down — which is why this happens before the transcription and not after.

The record already knows every job's name. The book was loaded on this very code path to
*name* a recording after the fact, and never handed to the engine that was mishearing the
names. One function call apart.

**Two features now read that file, and each keeps its own off-switch.** ``NAMING=0`` is a
rollback promise — the naming feature inert, the published bytes back to exactly what they
were — and this changes the transcript, so it must not hang off that switch. The class at
the bottom is the other half of that promise: ``ENGINE_SITE_NAMES=0`` restores the previous
hint list exactly, on its own.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from transcriber import sitebook
from transcriber.engines.base import safe_vocabulary
from transcriber.models import Hints

from tests import support

BOOK_DIR = tempfile.mkdtemp(prefix="engine-hints-book-")


def write_book(name: str, sites: dict) -> str:
    path = os.path.join(BOOK_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"vocab_contract": sitebook.CONTRACT,
                   "generated_at": "2026-09-04", "sites": sites}, fh)
    return path


#: Four of his real jobs, including the one the engine actually got wrong.
REAL_JOBS = {
    "lonehill-shopping-centre": {"title": "Lonehill Shopping Centre"},
    "beach-court":              {"title": "Beach Court"},
    "250-voortrekker-road":     {"title": "250 Voortrekker Road"},
    "amidal":                   {"title": "Amidal"},
}
BOOK = write_book("sites.json", REAL_JOBS)


def pipeline_with(**over):
    from transcriber.pipeline import Pipeline

    config = support.make_config(
        naming_sites_file=over.pop("sites_file", BOOK),
        vocabulary=over.pop("vocabulary", ()),
        engine_site_names=over.pop("engine_site_names", True),
        **over,
    )
    return Pipeline.__new__(Pipeline), config


def vocabulary_of(**over) -> tuple[str, ...]:
    """What ``_hints`` would put in front of the engine, without running a pipeline."""
    from transcriber.pipeline import Pipeline

    pipe, config = pipeline_with(**over)
    pipe.config = config
    pipe._site_book = sitebook.EMPTY
    import threading
    pipe._site_book_lock = threading.Lock()
    return Pipeline._vocabulary(pipe)


class TheJobNamesReachTheEngine(unittest.TestCase):
    def test_the_job_the_engine_got_wrong_is_now_in_the_hint_list(self) -> None:
        self.assertIn("Lonehill Shopping Centre", vocabulary_of())

    def test_every_job_in_the_book_is_offered(self) -> None:
        got = vocabulary_of()
        for entry in REAL_JOBS.values():
            self.assertIn(entry["title"], got)

    def test_the_operators_own_words_come_first(self) -> None:
        """The list is capped downstream and a cap cuts the tail. Terms somebody typed on
        purpose must not sit behind fifty-six job names to reach the engine."""
        got = vocabulary_of(vocabulary=("polycarb", "chromadek"))
        self.assertEqual(got[:2], ("polycarb", "chromadek"))
        self.assertIn("Beach Court", got)

    def test_a_name_in_both_places_is_not_sent_twice(self) -> None:
        got = vocabulary_of(vocabulary=("Beach Court",))
        self.assertEqual(len([t for t in got if t.lower() == "beach court"]), 1)

    def test_longer_names_first_so_a_cap_drops_the_easy_ones(self) -> None:
        """A one-word job name is the one an engine is least likely to mangle, so it is
        the one to lose if something has to go."""
        names = sitebook.load(BOOK).spoken_names()
        self.assertEqual(list(names), sorted(names, key=lambda t: (-len(t), t.lower())))

    def test_it_survives_the_trip_through_the_provider_guard(self) -> None:
        """safe_vocabulary caps and de-duplicates on the way out. The job names have to
        still be there afterwards, or this whole thing helps nothing."""
        got = safe_vocabulary(Hints(vocabulary=vocabulary_of()))
        self.assertIn("Lonehill Shopping Centre", got)


class NothingHereCanCostARecording(unittest.TestCase):
    """A hint is a nicety. Losing a recording over one would be absurd."""

    def test_no_book_configured_means_the_configured_vocabulary_alone(self) -> None:
        self.assertEqual(vocabulary_of(sites_file="", vocabulary=("polycarb",)),
                         ("polycarb",))

    def test_an_unreadable_book_costs_the_job_names_and_nothing_else(self) -> None:
        missing = os.path.join(BOOK_DIR, "not-here.json")
        self.assertEqual(vocabulary_of(sites_file=missing, vocabulary=("polycarb",)),
                         ("polycarb",))

    def test_a_book_that_explodes_is_not_allowed_to_propagate(self) -> None:
        from transcriber.pipeline import Pipeline

        pipe, config = pipeline_with(vocabulary=("polycarb",))
        pipe.config = config
        with mock.patch.object(
            type(pipe), "site_book",
            property(lambda self: (_ for _ in ()).throw(RuntimeError("book on fire"))),
        ):
            self.assertEqual(Pipeline._vocabulary(pipe), ("polycarb",))


class TheEngineHintSwitchIsAlsoAnOffSwitch(unittest.TestCase):
    """The other half of the NAMING=0 promise.

    Splitting one off-switch into two is only honest if the second one is proved to work.
    """

    def test_off_means_exactly_the_configured_vocabulary(self) -> None:
        self.assertEqual(
            vocabulary_of(engine_site_names=False, vocabulary=("polycarb", "chromadek")),
            ("polycarb", "chromadek"),
        )

    def test_off_means_no_job_name_reaches_the_engine(self) -> None:
        got = vocabulary_of(engine_site_names=False)
        self.assertEqual(got, ())
        for entry in REAL_JOBS.values():
            self.assertNotIn(entry["title"], got)

    def test_off_does_not_even_read_the_book(self) -> None:
        """Not merely 'discards the answer'. A deployment that switched this off should not
        be doing file I/O on the publish path for a feature it is not using."""
        from transcriber.pipeline import Pipeline

        pipe, config = pipeline_with(engine_site_names=False)
        pipe.config = config
        with mock.patch.object(sitebook, "load",
                               side_effect=AssertionError("must not read the site list")):
            self.assertEqual(Pipeline._vocabulary(pipe), ())


if __name__ == "__main__":
    unittest.main()
