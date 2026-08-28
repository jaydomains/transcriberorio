"""Two invariants about held words, both stated mechanically rather than by example.

**One: nothing this module says ever quotes what it is holding.**

``redact`` reports its problems as strings, and those strings do not stop at the developer.
:meth:`transcriber.pipeline.Pipeline._withhold` raises them as a fault; the fault is in
``_NEVER_RETRY``, so it is written to the ledger's ``quarantine_reason``; the 06:00 email
prints that verbatim under "Technical detail:"; and the same text is logged at ERROR. Four
places a held passage may never reach — and the path fires precisely when the masker has a
bug, which is the moment it matters. The trigger is ordinary: somebody repeating themselves
on a call leaves a second copy of the passage the redactor did not cut, and the problem
string that resulted quoted forty characters of it.

**Two: whatever the backstop would refuse, the masker cuts first.**

:func:`transcriber.redact.held_words_in` refuses a file over a run of five consecutive held
words anywhere in a passage. :meth:`Redaction.mask` used to cut only runs at a passage's
edges. Between the two sat a real and common shape — a model's summary reusing the middle of
a held sentence, which is what summarising a held staff matter looks like nearly every time.
The masker left it, the backstop refused the publish, and
:class:`transcriber.outputs.HeldTextWouldLeak` is never retried: the recording quarantined
forever with none of its three files ever written. Safe in direction and exactly the
availability cost the gate promised not to impose — nothing about a passage awaiting
approval delays the transcript.

So the relation is asserted as a relation. For any text and any held span,
``contains_any_held(mask(text), spans)`` is False.
"""

from __future__ import annotations

import unittest

from transcriber import redact
from transcriber.models import Segment, Transcript
from transcriber.withheld import HeldSpan

STAMP = "2026-08-24T09:00:00Z"

#: Held passages of the shapes that actually occur, one per category that can produce one.
PASSAGES = (
    ("staff_matter",
     "Don't write this down, but Sipho's second written warning was signed on Friday"),
    ("staff_matter", "Sipho got his second written warning on Friday, for the scaffold"),
    ("personal_circumstances",
     "Elmarie is off because her husband is having the bypass on Tuesday morning"),
    ("legal_exposure",
     "our attorney says we should not admit the slab was our own error at all"),
    ("bare_identifier", "8203155009087"),
    ("own_margin", "we raised R1.65m on that one and we'll land at R1.604m"),
    ("do_not_write_down", "moenie dit neerskryf nie, die ou is nie betaal nie"),
)

#: Strings a model might write about a recording carrying those passages. Deliberately
#: includes edge runs, interior runs, whole repeats, and prose that shares nothing.
DERIVED = (
    "A note that a second written warning was signed, and the chromadek lands Tuesday.",
    "Don't write this down, but the rest of it is fine.",
    "was signed on Friday",
    "Her husband is having the bypass on Tuesday morning, so she is away.",
    "The reference number given was 8203155009087 for the register.",
    "We raised R1.65m on that one and we'll land at R1.604m, so there is a bit in it.",
    "our attorney says we should not admit the slab was our own error at all",
    "The chromadek arrives Tuesday and the scaffold comes down Friday.",
    "",
    "   ",
    "The quote to the body corporate is R4,500 for the torch-on repair.",
    "moenie dit neerskryf nie",
    "second written warning was signed on Friday and her husband is having the bypass",
)


def _transcript() -> tuple[str, tuple[HeldSpan, ...]]:
    """One transcript carrying every passage above, and the spans over it."""
    lines = ["James: Right, I'm at Beach Court and the chromadek arrives Tuesday."]
    for _, words in PASSAGES:
        lines.append(f"James: {words}.")
    lines.append("James: The quote to the body corporate is R4,500 for the torch-on repair.")
    body = "\n".join(lines) + "\n"
    spans = []
    for category, words in PASSAGES:
        start = body.index(words)
        spans.append(
            HeldSpan(item_id="01ITEM", start=start, end=start + len(words), text=words,
                     category=category, recorded_at=STAMP)
        )
    return body, tuple(spans)


def _redaction(mode: str = "on") -> redact.Redaction:
    body, spans = _transcript()
    return redact.redact_text(body, spans, mode=mode, held_on=STAMP)


class NoStringThisModuleProducesQuotesAHeldPassage(unittest.TestCase):
    """The words go in the store and the audio. They do not go in an error message."""

    def _assert_clean(self, produced: str, where: str) -> None:
        for _category, words in PASSAGES:
            self.assertNotIn(
                words, produced,
                f"{where} quotes a held passage verbatim",
            )
            # Not only the whole passage: any run long enough for the backstop to call a
            # leak is long enough to put a staff matter in an inbox.
            fragments = words.split()
            for start in range(0, max(1, len(fragments) - redact.LEAK_MIN_RUN + 1)):
                run = " ".join(fragments[start:start + redact.LEAK_MIN_RUN])
                if len(fragments) < redact.LEAK_MIN_RUN:
                    continue
                self.assertNotIn(
                    run, produced,
                    f"{where} carries {redact.LEAK_MIN_RUN} consecutive words of a held "
                    f"passage",
                )

    def test_check_publishable_names_the_passage_and_does_not_quote_it(self) -> None:
        """Reproduces the reported path: a speaker repeating themselves.

        The redactor cuts the flagged span. The repeat is still there, the backstop finds
        it, and the sentence it writes travels to the morning email.
        """
        body = (
            "James: Sipho got his second written warning on Friday, for the scaffold.\n"
            "Other: Sorry, say again?\n"
            "James: I said Sipho got his second written warning, it is on file.\n"
        )
        words = "Sipho got his second written warning on Friday"
        start = body.index(words)
        span = HeldSpan(item_id="01ITEM", start=start, end=start + len(words), text=words,
                        category="staff_matter", recorded_at=STAMP)
        redaction = redact.redact_text(body, [span], mode="on", held_on=STAMP)

        problems = redaction.check_publishable(redaction.text)
        self.assertTrue(problems, "the backstop must still find the repeat")
        for problem in problems:
            self._assert_clean(problem, "check_publishable")
            self.assertIn(span.ref, problem, "it must still say which passage")
            self.assertIn(span.phrase, problem, "and what kind of thing it was")

    def test_every_problem_string_from_a_whole_run_is_clean(self) -> None:
        """The other two producers on the same path: ``problems`` and ``redact_transcript``."""
        body, spans = _transcript()
        # An offset that does not match its words is the case ``problems()`` reports.
        moved = HeldSpan(item_id="01ITEM", start=0, end=len("nobody said this"),
                         text="nobody said this", category="staff_matter", recorded_at=STAMP)
        redaction = redact.redact_text(body, list(spans) + [moved], mode="on", held_on=STAMP)
        for problem in redaction.problems():
            self._assert_clean(problem, "Redaction.problems")

        transcript = Transcript(
            text=body,
            segments=[Segment(0.0, 1.0, "James", line) for line in body.split("\n") if line],
            language="en-ZA",
            engine="test",
        )
        _cut, _redaction, problems = redact.redact_transcript(
            transcript, list(spans) + [moved], mode="on", held_on=STAMP
        )
        for problem in problems:
            self._assert_clean(problem, "redact_transcript")

    def test_a_leak_that_survives_is_reported_without_the_words(self) -> None:
        """The segment path writes its own sentence and must obey the same rule."""
        body, spans = _transcript()
        redaction = redact.redact_text(body, spans, mode="on", held_on=STAMP)
        # Segments the redaction knows nothing about, still carrying the passages whole.
        segments = [Segment(0.0, 1.0, "James", words) for _c, words in PASSAGES]
        _out, problems = redact.redact_segments(segments, redaction)
        for problem in problems:
            self._assert_clean(problem, "redact_segments")


class TheMaskerCoversWhateverTheBackstopRefuses(unittest.TestCase):
    """A guard the masker cannot satisfy is a guard that deletes recordings."""

    def test_the_reported_case_is_masked_rather_than_refused(self) -> None:
        body = (
            "James: Don't write this down, but Sipho's second written warning was signed "
            "on Friday, and the chromadek lands Tuesday.\n"
        )
        words = ("Don't write this down, but Sipho's second written warning was signed "
                 "on Friday")
        start = body.index(words)
        span = HeldSpan(item_id="01ITEM", start=start, end=start + len(words), text=words,
                        category="staff_matter", recorded_at=STAMP)
        redaction = redact.redact_text(body, [span], mode="on", held_on=STAMP)

        summary = "A note that a second written warning was signed, and the chromadek lands Tuesday."
        masked, refs = redaction.mask(summary)

        self.assertNotEqual(masked, summary, "the interior run must be cut, not left")
        self.assertEqual(refs, (span.ref,))
        self.assertFalse(
            redact.contains_any_held(masked, redaction.cut_spans),
            "the backstop would refuse this publish and quarantine the recording forever",
        )
        self.assertIn("chromadek lands Tuesday", masked, "the rest of the sentence stays")

    def test_the_relation_holds_for_every_passage_and_every_string(self) -> None:
        """The invariant itself: mask's coverage is a superset of the backstop's."""
        redaction = _redaction("on")
        for text in DERIVED:
            masked, _refs = redaction.mask(text)
            self.assertFalse(
                redact.contains_any_held(masked, redaction.cut_spans),
                f"masking left something the backstop refuses, from {text!r}",
            )

    def test_it_holds_for_every_sliding_window_of_every_passage(self) -> None:
        """Every run of held words that exists, at every position, on its own and in prose."""
        redaction = _redaction("on")
        for _category, words in PASSAGES:
            parts = words.split()
            for size in range(1, len(parts) + 1):
                for start in range(0, len(parts) - size + 1):
                    run = " ".join(parts[start:start + size])
                    for candidate in (run, f"He mentioned {run} in passing, and nothing else."):
                        masked, _refs = redaction.mask(candidate)
                        self.assertFalse(
                            redact.contains_any_held(masked, redaction.cut_spans),
                            f"masking left {run!r} (words {start}..{start + size}) readable",
                        )

    def test_masking_is_idempotent(self) -> None:
        """A second pass must not cut a hole in the first pass's marker."""
        redaction = _redaction("on")
        for text in DERIVED:
            once, _ = redaction.mask(text)
            twice, _ = redaction.mask(once)
            self.assertEqual(once, twice)

    def test_it_still_cuts_nothing_in_shadow(self) -> None:
        """The relation is about the armed gate. Shadow withholds nothing, by design."""
        redaction = _redaction("shadow")
        for text in DERIVED:
            self.assertEqual(redaction.mask(text), (text, ()))

    def test_ordinary_prose_is_left_alone(self) -> None:
        """The other half of precision: coverage must not become a shredder.

        A false cut is a withdrawal from the only budget that matters — his willingness to
        keep reviewing — and it also silently damages the record.
        """
        redaction = _redaction("on")
        clean = (
            "The chromadek arrives Tuesday and the bricks land Thursday.",
            "The quote to the body corporate is R4,500 for the torch-on repair.",
            "Thabo finished the flashing on block C before the inspection.",
            "The supplier rate is R92 a square and the contract sum is R840,000.",
            "He undertook to write to the trustees about the retention.",
        )
        for text in clean:
            masked, refs = redaction.mask(text)
            self.assertEqual(masked, text, f"ordinary site talk was masked: {text!r}")
            self.assertEqual(refs, ())

    def test_the_interior_pass_cuts_only_what_the_backstop_would_refuse(self) -> None:
        """The precision claim for the widened coverage, stated as an implication.

        The masker's first two passes are unchanged. The third exists solely to close the
        gap against the backstop, so it must add nothing anywhere the first two already
        produced text the backstop accepts. Anything else would be a new false cut, and a
        false cut damages the record as surely as a leak damages a person.

        Note what this does *not* claim: the edge passes have always cut a two-word prefix
        or suffix of a held passage, so ``"the scaffold"`` is masked out of an innocent
        sentence when a held passage happens to end with those two words. That is older than
        the widened coverage and is deliberately left where it is — this test pins the
        boundary of the change rather than papering over what sits on the other side of it.
        """
        redaction = _redaction("on")
        corpus = list(DERIVED) + [
            "The chromadek arrives Tuesday and the bricks land Thursday.",
            "The quote to the body corporate is R4,500 for the torch-on repair.",
            "He said the second written warning matters, and nothing else does.",
        ]
        for text in corpus:
            edges_only = text
            for applied in redaction.applied:
                words = applied.span.text
                found = redact._find_all(edges_only, words)
                if found:
                    for start, end in reversed(found):
                        edges_only = edges_only[:start] + applied.marker + edges_only[end:]
                    continue
                edges_only, _did = redact._cut_runs(
                    edges_only, words, applied.marker, part_marker=applied.marker,
                    min_run=2, edges_only=True,
                )
            if redact.contains_any_held(edges_only, redaction.cut_spans):
                # The gap the third pass exists to close. It must close it.
                self.assertFalse(
                    redact.contains_any_held(redaction.mask(text)[0], redaction.cut_spans),
                    f"the widened coverage did not close the gap on {text!r}",
                )
                continue
            self.assertEqual(
                redaction.mask(text)[0], edges_only,
                f"the widened coverage cut something the backstop never objected to, "
                f"in {text!r}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
