"""What the word search cannot see, and what is done about it.

The summary and the actions file are both written by a model reading the **unredacted**
transcript — they have to be, or quote verification cannot tell an invented quote from a
masked one. So the model is free to say in its own words what the transcript no longer says.

Both guards against that were word searches. :meth:`Redaction.mask` looks for the held words
whole or as a run of them; :func:`outputs.refuse_held_text` re-checks with the same test.
A restatement satisfies neither: "there is a hearing for Marius on Friday and he will
probably lose his job" shares no five-word run with "Marius has his disciplinary hearing on
Friday". It was published into the route's output folder — which is where James looks — so
decision 6 was broken for a staff member's passage by a paraphrase alone. Both functions'
docstrings described themselves as catching "the occasion when that search missed", which is
false for the only case that matters.

There is no complete mechanical answer, and this file does not pretend there is one. There
are two partial ones, and they are asserted separately:

* the prompt now tells the model to keep its own prose clear of anything it flags — the only
  thing in the system that knows two sentences mean the same thing;
* :func:`outputs.refuse_written_down_again` re-reads the derived files with the mechanical
  rules, which catches the part of a rewriting that survives it: an explicit request that
  something not be written down, restated, and a bare identifier, which cannot be
  paraphrased because a number has no synonyms.

The last test states the residual limit out loud rather than leaving somebody to find it.
"""

from __future__ import annotations

import datetime as _dt
import unittest

from tests import support
from transcriber import naming, outputs, prompts, redact
from transcriber.models import Segment, Transcript
from transcriber.withheld import HeldSpan

STAMP = "2026-08-24T09:00:00Z"


def _context(body: str, held_words: str, category: str, *, summary: str, quotes=()):
    start = body.index(held_words)
    span = HeldSpan(item_id="C1", start=start, end=start + len(held_words), text=held_words,
                    category=category, site="Beach Court", recorded_at=STAMP)
    transcript = Transcript(
        text=body,
        segments=[Segment(0.0, 1.0, "James", line) for line in body.split("\n") if line],
        language="en-ZA",
        engine="test",
    )
    cut, redaction, problems = redact.redact_transcript(
        transcript, [span], mode="on", held_on=STAMP
    )
    assert not problems, problems
    extraction = support.StubExtraction(summary=summary)
    masked, _outcomes = redact.redact_extraction(extraction, redaction)
    return outputs.OutputContext(
        item_id="C1",
        source_name="Call Carel_260827_120055.m4a",
        parsed=naming.parse_source_name("Call Carel_260827_120055.m4a"),
        recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
        timestamp_source="from the filename",
        transcript=cut,
        extraction=masked,
        audio=support.audio_info(120.0),
        held=redaction.cut_spans,
    )


class TheModelIsToldToKeepItsProseClear(unittest.TestCase):
    """The only part of the system that can recognise a restatement is the thing writing it."""

    def test_the_instruction_is_in_the_note(self) -> None:
        note = prompts.SENSITIVITY_NOTE
        self.assertIn("KEEP THE REST OF YOUR ANSWER CLEAR OF WHAT YOU FLAG", note)
        self.assertIn("summary_en", note)
        self.assertIn("in your own words", note)

    def test_it_is_carried_by_the_system_prompt_the_gate_actually_sends(self) -> None:
        armed = prompts.extraction_system(sensitivity=True)
        self.assertIn("KEEP THE REST OF YOUR ANSWER CLEAR OF WHAT YOU FLAG", armed)
        self.assertNotIn(
            "KEEP THE REST OF YOUR ANSWER CLEAR OF WHAT YOU FLAG",
            prompts.extraction_system(),
            "with the gate off, nothing about the analysis pass may differ",
        )

    def test_it_does_not_tell_the_model_to_suppress_prices(self) -> None:
        """Prices flow. A gate that gutted every summary mentioning money is the other failure."""
        note = prompts.SENSITIVITY_NOTE
        self.assertIn("A commercial_figure or a conduct_or_quality", note)
        self.assertIn("prices flow", note.lower())

    def test_it_does_not_tell_the_model_to_thin_the_rest_of_the_summary(self) -> None:
        self.assertIn(
            "still gets a full and useful summary of the rest of it",
            prompts.SENSITIVITY_NOTE,
        )


class TheRulesReadTheDerivedFilesAgain(unittest.TestCase):
    """The half of a restatement that is mechanically decidable."""

    def test_a_summary_restating_a_request_not_to_write_it_down_stops_the_publish(self) -> None:
        body = (
            "James: Right, I'm at Beach Court and the chromadek lands Tuesday.\n"
            "James: Don't write this down, but the engineer signed off a slab he never saw.\n"
        )
        held = "Don't write this down, but the engineer signed off a slab he never saw"
        ctx = _context(
            body, held, "do_not_write_down",
            # Not a quotation of the held words: the model's own sentence, saying the same
            # thing. The word search passes it.
            summary="He asked to keep it between us that a slab went through uninspected.",
        )
        self.assertFalse(
            redact.contains_any_held(ctx.extraction.summary, ctx.held),
            "this test is only meaningful while the word search does NOT catch it",
        )

        with self.assertRaises(outputs.HeldTextWouldLeak) as caught:
            outputs.render_all(ctx)
        self.assertIn("summary", str(caught.exception))
        self.assertIn("in words of its own", str(caught.exception))

    def test_a_summary_reformatting_an_identity_number_stops_the_publish(self) -> None:
        """A number has no synonyms, but it does have other spacings.

        Repeated character for character, the word search catches it — that is the easy
        case and it is already covered. Written the way a person writes an identity number,
        ``800101 5009 087``, it matches neither the whole-passage search nor a word run,
        because the redactor deliberately refuses to match loosely. The digits are all still
        there and the rules read them straight off.
        """
        body = (
            "James: The new chap starts Monday.\n"
            "James: His ID number is 8001015009087 for the site register.\n"
        )
        ctx = _context(
            body, "8001015009087", "bare_identifier",
            summary="Please add the new man, identity number 800101 5009 087, to the register.",
        )
        self.assertFalse(
            redact.contains_any_held(ctx.extraction.summary, ctx.held),
            "this test is only meaningful while the word search does NOT catch it",
        )

        with self.assertRaises(outputs.HeldTextWouldLeak):
            outputs.render_all(ctx)

    def test_an_ordinary_recording_is_not_disturbed(self) -> None:
        """Precision. This guard runs on every armed publish and must almost never fire."""
        body = (
            "James: The chromadek arrives Tuesday and the scaffold comes down Friday.\n"
            "James: His ID number is 8001015009087 for the register.\n"
            "James: The quote to the body corporate is R4,500 for the torch-on repair.\n"
        )
        ctx = _context(
            body, "8001015009087", "bare_identifier",
            summary=(
                "The chromadek arrives Tuesday, the scaffold comes down Friday, and the "
                "quote to the body corporate is R4,500 for the torch-on repair."
            ),
        )
        files = outputs.render_all(ctx)
        self.assertEqual(len(files), 3)
        summary = next(f for f in files if f.kind == "summary")
        self.assertIn("R4,500", summary.text, "prices flow")

    def test_it_never_runs_when_nothing_was_withheld(self) -> None:
        """Shadow measures. A measurement that stops a publish is one somebody switches off."""
        body = "James: Don't write this down, but the slab was never inspected.\n"
        transcript = Transcript(text=body, segments=(), language="en-ZA", engine="test")
        ctx = outputs.OutputContext(
            item_id="C1",
            source_name="Call Carel_260827_120055.m4a",
            parsed=naming.parse_source_name("Call Carel_260827_120055.m4a"),
            recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
            timestamp_source="from the filename",
            transcript=transcript,
            extraction=support.StubExtraction(
                summary="He asked that it not be written down that the slab went uninspected."
            ),
            audio=support.audio_info(120.0),
            held=(),
        )
        files = outputs.render_all(ctx)
        self.assertEqual(len(files), 3)

    def test_the_transcript_itself_is_never_refused_by_this_guard(self) -> None:
        """It is the recording's own words, cut on exact offsets. Refusing it loses the lot."""
        body = (
            "James: Don't write this down, but the engineer signed off a slab he never saw.\n"
            "James: Also, don't put in the minutes that we were late on block C.\n"
        )
        held = "Don't write this down, but the engineer signed off a slab he never saw"
        ctx = _context(body, held, "do_not_write_down", summary="A site call about block C.")
        # The second instruction is still in the transcript — only the first was held —
        # and the transcript must publish regardless.
        files = outputs.render_all(ctx)
        transcript = next(f for f in files if f.kind == "transcript")
        self.assertIn("don't put in the minutes", transcript.text.lower())

    def test_it_is_asked_again_on_the_way_out(self) -> None:
        """A guard on the way in is not a guard on the way out."""
        good = outputs.RenderedFile("summary", "x-summary.md", "Ordinary site talk.\n")
        bad = outputs.RenderedFile(
            "summary", "x-summary.md",
            "He asked that this be kept off the record entirely.\n",
        )
        outputs.refuse_written_down_again([good], armed=True)
        with self.assertRaises(outputs.HeldTextWouldLeak):
            outputs.refuse_written_down_again([bad], armed=True)


class TheResidualLimitIsStatedRatherThanHidden(unittest.TestCase):
    """What neither guard catches, asserted so that nobody re-reads the code as covering it."""

    def test_a_reworded_staff_matter_is_not_mechanically_detectable(self) -> None:
        """There is no rule that sees this, and the docstrings must not claim there is.

        This is the honest boundary: the prompt is what addresses it, and the prompt is an
        instruction to a model rather than a mechanism. Recording it as a test means a later
        reader finds the limit here instead of in the record.
        """
        held = "Marius has his disciplinary hearing on Friday"
        restated = "There is a hearing for Marius on Friday and he will probably lose his job."
        span = HeldSpan(item_id="C1", start=0, end=len(held), text=held,
                        category="staff_matter", recorded_at=STAMP)
        redaction = redact.redact_text(
            f"James: {held}.\n", [span], mode="on", held_on=STAMP
        )
        self.assertFalse(redact.contains_any_held(restated, redaction.cut_spans))
        self.assertEqual(
            outputs.refuse_written_down_again(
                [outputs.RenderedFile("summary", "x-summary.md", restated)], armed=True
            ),
            None,
        )

    def test_the_docstrings_say_so(self) -> None:
        self.assertIn("a restatement", outputs.refuse_held_text.__doc__ or "")
        self.assertIn("not restatements", redact.redact_extraction.__doc__ or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
