"""The number he is meant to arm the gate on, and whether it is allowed to say it is ready.

Correction 4 of the brief: it ships dark because the estimates of how much the gate touches
differ across the design passes by a factor of twenty-five, and arming it before that number
is real is how the queue becomes a wall he bounces off.

So the shadow measurement is the whole basis of the decision — and it did not report the one
thing that decides whether it means anything: **how many of those recordings the classifier
actually read.** Four of the six held categories cannot be seen by the mechanical rules at
all. A gate whose model half is not running produces a small held fraction, which is exactly
what a well-tuned gate produces; the two are indistinguishable in the morning email, and the
email then printed "If it is small and the categories look right, it is ready" underneath
either of them.

That is not hypothetical: it is what this codebase did. ``extract.py`` never asked the
sensitivity question at all, so every recording was classified by rules alone, and the email
would have told him the measurement looked good.

Second half: in ``shadow`` — the mode that ships — the classifier's own notes reached
nobody. ``pipeline._withhold`` carried ``report.notes`` into the output files only when the
gate was armed, and in shadow no file is written for them, so "the model did not answer the
sensitivity question for this recording" and "the words it quoted are not in the transcript,
a person should look at this recording" existed only in a ledger row's meta that nobody
opens at 06:00.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests import support
from transcriber import digest as digest_module
from transcriber.ledger import Ledger
from transcriber.models import Route
from transcriber.withheld import HeldSpan, WithheldStore

ROUTE = Route(
    name="calls", label="Phone calls", source_folder_id="S", output_folder_id="O",
    archive_folder_id="", engine="", enabled=True,
)

MODEL = "claude-cheap,claude-strong"


class _Store:
    def __init__(self, mode: str = "shadow") -> None:
        self.dir = tempfile.mkdtemp()
        self.config = support.make_config(
            routes=(ROUTE,),
            work_dir=os.path.join(self.dir, "work"),
            ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
            gate_mode=mode,
            gate_held_store=os.path.join(self.dir, "held.sqlite3"),
            gate_review_base_url="https://review.invalid/held",
            route_reviewers={"calls": "sipho@example.invalid"},
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.store = WithheldStore(self.config.gate_held_store)

    def pass_of(self, item_id: str, *, classifier: str, notes=(), spans=()) -> None:
        self.store.record_pass(
            item_id, route="calls", mode=self.config.gate_mode, spans=spans,
            transcript_chars=2000, classifier=classifier, notes=notes,
            at=f"2026-08-27T10:00:00Z",
        )

    def report(self):
        return digest_module.held_report(
            self.config, self.ledger, store=self.store, day="2026-08-27",
            now="2026-08-27T23:00:00Z",
        )

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


class ItSaysHowManyTheClassifierActuallyRead(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _Store("shadow")
        self.addCleanup(self.world.close)

    def test_the_measurement_reports_both_denominators(self) -> None:
        for index in range(8):
            self.world.pass_of(f"item-{index}", classifier=MODEL)
        for index in range(8, 10):
            self.world.pass_of(f"item-{index}", classifier="rules")

        measurement = self.world.store.measurement()
        self.assertEqual(measurement["recordings_classified"], 10)
        self.assertEqual(measurement["recordings_the_model_read"], 8)
        self.assertEqual(measurement["recordings_rules_only"], 2)
        self.assertAlmostEqual(measurement["fraction_the_model_read"], 0.8)

    def test_the_email_prints_both(self) -> None:
        for index in range(6):
            self.world.pass_of(f"item-{index}", classifier=MODEL)
        for index in range(6, 10):
            self.world.pass_of(f"item-{index}", classifier="rules")

        body = "\n".join(self.world.report().lines())
        self.assertIn("read by the model", body)
        self.assertIn("read by the rules alone", body)


class ItRefusesToCallAnUnrealNumberReady(unittest.TestCase):
    """The sentence that tells him what to do with the figure, and when it must not appear."""

    def setUp(self) -> None:
        self.world = _Store("shadow")
        self.addCleanup(self.world.close)

    def _body(self) -> str:
        return "\n".join(self.world.report().lines())

    def test_a_classifier_that_never_ran_is_named_as_the_reason(self) -> None:
        """The state this codebase was actually in: every recording read by rules alone."""
        for index in range(40):
            self.world.pass_of(f"item-{index}", classifier="rules")

        body = self._body()
        self.assertIn("THIS NUMBER IS NOT YET REAL", body)
        self.assertIn("do not switch the gate on against it", body)
        self.assertIn("the question was not asked", body)
        self.assertNotIn(
            "it is ready", body,
            "the email told him a measurement produced by a classifier that never ran "
            "looked good",
        )

    def test_it_names_the_four_categories_the_rules_cannot_see(self) -> None:
        for index in range(40):
            self.world.pass_of(f"item-{index}", classifier="rules")

        body = self._body()
        for what in ("staff matter", "person's health", "attorney", "cost set against"):
            self.assertIn(what, body)

    def test_a_real_measurement_is_allowed_to_say_it_is_ready(self) -> None:
        for index in range(40):
            self.world.pass_of(f"item-{index}", classifier=MODEL)

        body = self._body()
        self.assertIn("it is ready", body)
        self.assertNotIn("THIS NUMBER IS NOT YET REAL", body)

    def test_a_partial_classifier_is_still_refused(self) -> None:
        """Half the recordings read is not half a measurement; it is not a measurement."""
        for index in range(20):
            self.world.pass_of(f"item-{index}", classifier=MODEL)
        for index in range(20, 40):
            self.world.pass_of(f"item-{index}", classifier="rules")

        body = self._body()
        self.assertIn("THIS NUMBER IS NOT YET REAL", body)
        self.assertIn("20 of 40", body)

    def test_the_json_says_so_too(self) -> None:
        for index in range(10):
            self.world.pass_of(f"item-{index}", classifier="rules")

        payload = self.world.report().as_dict()
        self.assertFalse(payload["measurement_is_real"])
        self.assertEqual(payload["model_read"], 0)
        self.assertEqual(payload["rules_only"], 10)


class TheClassifiersNotesReachHimInShadowToo(unittest.TestCase):
    """In the mode that ships there is no file for them, so the email is the only place."""

    def setUp(self) -> None:
        self.world = _Store("shadow")
        self.addCleanup(self.world.close)

    def test_a_note_written_in_shadow_reaches_the_morning_email(self) -> None:
        note = (
            "the model reported a staff matter in this recording, but the words it quoted "
            "are not in the transcript, so nothing was held on it — a person should look at "
            "this recording"
        )
        for index in range(10):
            self.world.pass_of(f"item-{index}", classifier=MODEL)
        self.world.pass_of("item-x", classifier=MODEL, notes=(note,))

        body = "\n".join(self.world.report().lines())
        self.assertIn("What it could not stand behind", body)
        # The email wraps at 68 columns, so the note is asserted on its collapsed text.
        flat = " ".join(body.split())
        self.assertIn("a person should look at this recording", flat)
        self.assertIn("on 1 recording(s)", flat)

    def test_notes_are_counted_rather_than_listed_once_per_recording(self) -> None:
        note = "the model did not answer the sensitivity question for this recording"
        for index in range(5):
            self.world.pass_of(f"item-{index}", classifier="rules", notes=(note,))

        measurement = self.world.store.measurement()
        self.assertEqual(measurement["notes"][note], 5)

    def test_a_store_written_before_this_column_existed_still_reads(self) -> None:
        """The migration path, exercised: an old row has no notes and must not raise."""
        self.world.pass_of("item-old", classifier=MODEL)
        self.world.store._conn().execute("UPDATE passes SET notes='' WHERE item_id='item-old'")

        measurement = self.world.store.measurement()
        self.assertEqual(measurement["recordings_classified"], 1)
        self.assertEqual(measurement["notes"], {})


class NothingHereDecidesAnything(unittest.TestCase):
    """A threshold that printed a different sentence is not a threshold that acts."""

    def test_the_report_only_ever_produces_sentences(self) -> None:
        world = _Store("shadow")
        self.addCleanup(world.close)
        words = "he asked that this not be written down"
        world.store.hold(
            HeldSpan(item_id="i", start=0, end=len(words), text=words,
                     category="do_not_write_down", route="calls", reviewer="sipho@example.invalid"),
            mode="shadow",
        )
        for index in range(40):
            world.pass_of(f"item-{index}", classifier="rules")

        before = world.store.overview(decision="pending")["count"]
        report = world.report()
        _ = report.lines(), report.as_dict(), report.heading()
        after = world.store.overview(decision="pending")["count"]
        self.assertEqual(before, after)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TheOverviewSaysCountsAndNotSubjects(unittest.TestCase):
    """``held list --json`` prints ``overview()`` verbatim, so ``oldest`` is a publishing surface.

    ``overview()["oldest"]`` is ``HeldRecord.to_dict()`` on a record that has been through
    ``without_words()`` — the projection whose entire job is that James sees the count and
    the site and never what was said. It blanked the text, the context and the reason, and
    kept the classifier's ``subject``: its own noun phrase for the passage. The
    human-readable branch of the very same command is careful to print "deliberately not
    even the classifier's own summary of them"; the JSON branch, taken first, contradicted it.

    The check on that subject is shallow — digits, ``@``, mid-string capitals, a length cap —
    so "the foreman's drinking problem" passes it. It is safe in the marker, standing in for
    words that are gone, and it is not safe as a description of somebody else's passage
    handed to whoever pipes the command into a ticket.
    """

    def setUp(self) -> None:
        self.world = _Store("on")
        self.addCleanup(self.world.close)
        words = "the foreman has been drinking on site again since the divorce"
        self.record = self.world.store.hold(
            HeldSpan(
                item_id="i", start=0, end=len(words), text=words,
                category="personal_circumstances", route="calls", site="Beach Court",
                subject="the foreman's drinking problem",
                reason="it is about his life rather than his work",
                context_before="We were talking about block C. ",
                context_after=" Anyway, the chromadek lands Tuesday.",
                reviewer="sipho@example.invalid",
            ),
            mode="on",
        )

    def test_the_oldest_in_the_overview_carries_no_subject(self) -> None:
        oldest = self.world.store.overview(decision="pending")["oldest"]

        self.assertEqual(oldest["subject"], "")
        self.assertEqual(oldest["phrase"], "a person's personal circumstances")
        self.assertNotIn("drinking", str(oldest))

    def test_it_still_carries_everything_needed_to_find_the_recording(self) -> None:
        oldest = self.world.store.overview(decision="pending")["oldest"]

        for key in ("hold_id", "ref", "item_id", "site", "reviewer", "category", "held_at"):
            self.assertIn(key, oldest)
        self.assertEqual(oldest["site"], "Beach Court")
        self.assertEqual(oldest["category"], "personal_circumstances")

    def test_the_whole_overview_carries_no_word_of_the_passage(self) -> None:
        import json

        rendered = json.dumps(self.world.store.overview(decision="pending"), default=str)
        for leak in ("drinking", "divorce", "foreman", "block C", "chromadek"):
            self.assertNotIn(leak, rendered)

    def test_the_owner_of_the_passage_still_sees_their_own_subject(self) -> None:
        """Hiding it from a third party must not hide it from the person reviewing it."""
        payload = self.record.to_dict(include_words=True)

        self.assertEqual(payload["subject"], "the foreman's drinking problem")
        self.assertEqual(payload["text"], self.record.text)

    def test_without_words_means_what_its_name_says(self) -> None:
        payload = self.record.without_words().to_dict(include_words=True)

        self.assertEqual(payload["subject"], "")
        self.assertNotIn("text", payload)
