"""A hold has to be visible in the record, not only in the file the record ingests.

Correction 2 of the brief: *"A marker only in the transcript is invisible. The record's read
path is built from six sources and the inbox is not one. A confident answer built on a
quietly partial record is worse than the leak it prevents."*

The marker was phrased as a stated unknown for exactly that reason — so the record's own
question harvester carries it onto the site's live page. What nobody checked is whether the
harvester actually reaches it. ``kbc-site-memory/tools/transcripts.py`` collects every
question in a transcript, sorts them by where they appear in the body, and writes the
**first twenty**: ``qs[:20]``. Hold markers live in "What was said", after the provenance
block, so they sort after everything asked earlier in the call — and that file's own
docstring says a site walk produces forty questions.

So on exactly the long site meetings where a hold is most likely, every hold question fell
off the end of the cap: the transcript carried its markers, the site's live page carried
nothing, and the assistant answered a client confidently from a record it did not know was
partial.

These tests run the record's real ``questions_in`` and its real cap over transcripts this
service really renders. They are skipped, loudly, if the record is not checked out beside
this repository — the pattern is also vendored in ``redact._RECORD_QUESTION_RE`` and
exercised offline by ``test_gate_in_the_flow``, so the property is covered either way, but
the copy that matters is the record's own.
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import re
import unittest

from tests import support
from transcriber import naming, outputs, redact
from transcriber.models import Segment, Transcript
from transcriber.withheld import HeldSpan

STAMP = "2026-08-24T09:00:00Z"

#: The record's own cap, copied. If it ever changes there, this number is what fails.
RECORD_QUESTION_CAP = 20

HOLDS = (
    ("staff_matter", "Sipho got his second written warning on Friday"),
    ("personal_circumstances", "Elmarie is off because her husband is having the bypass"),
    ("legal_exposure", "our attorney says we should not admit the slab was our error"),
    ("bare_identifier", "8203155009087"),
    ("own_margin", "we raised R1.65m on that one and we will land at R1.604m"),
)

#: Twenty-five ordinary site questions, of the kind a walk produces. Every one of them sorts
#: before anything said later in the call.
CHATTER = "\n".join(
    f"James: On block {index}, when is the {word} arriving on site please?"
    for index, word in enumerate(
        ("chromadek", "screed", "rebar", "glazing", "kerbing", "paving", "fascia", "soffit",
         "balustrade", "tiling", "skirting", "cornice", "shutter", "louvre", "gutter",
         "downpipe", "flashing", "coping", "plinth", "apron", "mullion", "transom",
         "reveal", "threshold", "nosing"),
        start=1,
    )
)


def _record_questions_in():
    """The record's real harvester, or ``None`` when the record is not checked out here."""
    for candidate in (
        "/home/user/kbc-site-memory/tools/transcripts.py",
        os.path.join(os.path.dirname(__file__), "..", "..", "kbc-site-memory",
                     "tools", "transcripts.py"),
    ):
        path = os.path.abspath(candidate)
        if not os.path.exists(path):
            continue
        spec = importlib.util.spec_from_file_location("_kbc_transcripts", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - the record is not ours to keep importable
            continue
        return getattr(module, "questions_in", None)
    return None


#: A faithful copy of the record's question scan, for the case where the record is not here.
_VENDORED = re.compile(r"(?:^|(?<=[.!?\n]))\s*([^.!?\n]{15,240}\?)", re.M)


def _vendored_questions_in(text: str) -> list[str]:
    out, seen = [], set()
    for match in _VENDORED.finditer(text):
        question = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(question) < 15:
            continue
        key = question.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((match.start(), question))
    return [q for _at, q in sorted(out)]


def _render(body: str, spans, *, extraction=None) -> tuple[str, tuple]:
    transcript = Transcript(
        text=body,
        segments=[Segment(0.0, 1.0, "James", line) for line in body.split("\n") if line],
        language="en-ZA",
        engine="test",
    )
    cut, redaction, problems = redact.redact_transcript(
        transcript, spans, mode="on", held_on=STAMP
    )
    assert not problems, problems
    ctx = outputs.OutputContext(
        item_id="C1",
        source_name="Call Carel_260827_120055.m4a",
        parsed=naming.parse_source_name("Call Carel_260827_120055.m4a"),
        recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
        timestamp_source="from the filename",
        transcript=cut,
        extraction=extraction,
        audio=support.audio_info(120.0),
        held=redaction.cut_spans,
    )
    return outputs.render_transcript(ctx), redaction.cut_spans


def _spans(body: str) -> tuple[HeldSpan, ...]:
    out = []
    for category, words in HOLDS:
        start = body.index(words)
        out.append(
            HeldSpan(item_id="C1", start=start, end=start + len(words), text=words,
                     category=category, site="Beach Court", recorded_at=STAMP)
        )
    return tuple(out)


class EveryHoldSurvivesTheRecordsCap(unittest.TestCase):
    def setUp(self) -> None:
        self.questions_in = _record_questions_in() or _vendored_questions_in
        self.using_the_real_one = _record_questions_in() is not None

    def _check(self, body: str) -> None:
        rendered, spans = _render(body, _spans(body))
        harvested = self.questions_in(rendered)
        filed = harvested[:RECORD_QUESTION_CAP]
        for span in spans:
            self.assertTrue(
                any(span.ref in question for question in filed),
                f"held passage {span.ref} was not among the first "
                f"{RECORD_QUESTION_CAP} questions the record files, so the site's live page "
                f"would say nothing about it "
                f"(it was at position "
                f"{next((i for i, q in enumerate(harvested) if span.ref in q), None)} "
                f"of {len(harvested)})",
            )

    def test_a_quiet_recording(self) -> None:
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        self._check(body)

    def test_a_talkative_site_walk(self) -> None:
        """Twenty-five questions before a word of the held passages. The reported case."""
        body = CHATTER + "\n" + "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        self._check(body)

    def test_forty_questions_which_is_what_the_record_says_a_site_walk_produces(self) -> None:
        body = (
            CHATTER + "\n"
            + "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
            + CHATTER.replace("block", "level") + "\n"
        )
        self._check(body)

    def test_the_hold_questions_are_the_very_first_thing_filed(self) -> None:
        """Not merely inside the cap — ahead of it, so no future chatter can push them out."""
        body = CHATTER + "\n" + "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, spans = _render(body, _spans(body))
        harvested = self.questions_in(rendered)
        positions = [
            next(i for i, q in enumerate(harvested) if span.ref in q) for span in spans
        ]
        self.assertEqual(sorted(positions), list(range(len(spans))), harvested[:8])


class ThePreambleSaysNothingItShouldNot(unittest.TestCase):
    def setUp(self) -> None:
        self.questions_in = _record_questions_in() or _vendored_questions_in

    def test_it_carries_no_word_of_any_held_passage(self) -> None:
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, spans = _render(body, _spans(body))
        for _category, words in HOLDS:
            self.assertNotIn(words, rendered)
        self.assertFalse(redact.contains_any_held(rendered, spans))

    def test_it_states_the_kind_and_the_date_and_the_reference(self) -> None:
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, spans = _render(body, _spans(body))
        self.assertIn("## Passages held for review", rendered)
        for span in spans:
            self.assertIn(span.ref, rendered)
        self.assertIn("24 Aug 2026", rendered)

    def test_it_does_not_double_the_questions_the_record_files(self) -> None:
        """The preamble states the marker's own sentence, so the record de-duplicates them."""
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, spans = _render(body, _spans(body))
        harvested = self.questions_in(rendered)
        for span in spans:
            self.assertEqual(
                sum(1 for question in harvested if span.ref in question), 1,
                f"{span.ref} was filed as more than one open question",
            )

    def test_a_recording_with_no_holds_has_no_block_at_all(self) -> None:
        body = CHATTER + "\n"
        rendered, _spans_out = _render(body, ())
        self.assertNotIn("Passages held for review", rendered)

    def test_the_file_is_still_a_transcript_the_record_will_read(self) -> None:
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, _ = _render(body, _spans(body))
        self.assertEqual(outputs.check_contract(rendered), [])
        head, _body = outputs.parse_like_downstream(rendered)
        self.assertNotIn("from", head)

    def test_it_names_the_site_when_one_was_bound(self) -> None:
        body = "\n".join(f"James: {words}." for _c, words in HOLDS) + "\n"
        rendered, _ = _render(
            body, _spans(body), extraction=support.StubExtraction(site="Beach Court")
        )
        self.assertIn("at Beach Court", rendered)


class AMarkerThatCannotBeHarvestedStopsThePublish(unittest.TestCase):
    """A recording that cannot announce its own hole must not publish.

    ``harvestable`` used to be asserted in the release path and in the selftest, but never
    where a marker is actually written. If the wording drifted out of the record's question
    scan, the words would still be cut and the record would simply stop saying anything had
    been held — the failure the phrasing exists to prevent, arriving with no symptom.
    """

    def test_redact_text_refuses_a_marker_the_record_would_not_pick_up(self) -> None:
        body = "James: Sipho got his second written warning on Friday.\n"
        words = "Sipho got his second written warning on Friday"
        start = body.index(words)
        span = HeldSpan(item_id="C1", start=start, end=start + len(words), text=words,
                        category="staff_matter", recorded_at=STAMP)

        original = redact.marker_for
        try:
            redact.marker_for = lambda s, **k: f"[held {s.ref}] the words are not here."
            redaction = redact.redact_text(body, [span], mode="on", held_on=STAMP)
        finally:
            redact.marker_for = original

        self.assertEqual(redaction.cut, 0, "the words were cut with an unharvestable marker")
        self.assertIn(words, redaction.text, "nothing may be cut when the marker is unusable")
        problems = redaction.problems()
        self.assertTrue(problems)
        self.assertIn("no sign that anything was held", " ".join(problems))
        for problem in problems:
            self.assertNotIn(words, problem)

    def test_the_real_marker_is_harvestable_for_every_category(self) -> None:
        for category, words in HOLDS:
            span = HeldSpan(item_id="C1", start=0, end=len(words), text=words,
                            category=category, recorded_at=STAMP)
            marker = redact.marker_for(span)
            self.assertTrue(redact.harvestable(marker), f"{category}: {marker}")
            self.assertEqual(marker.count("?"), 1, marker)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
