"""The model half of the gate: is the question actually asked, and does the answer arrive?

Every other gate test in this suite hands the pipeline a stand-in extraction object that
already carries ``sensitive_passages``. That shape is the one production could not produce:
``extract.py`` sent the plain extraction prompt and the plain extraction schema, so the field
was never asked for, never returned, and never on an ``Extraction``. Four of the six held
categories — a staff matter, an identifiable person's health, KBC's attorney strategy, and
its own cost set against its own charge — can only ever be seen by whatever reads the
transcript, and nothing was reading it for them.

That failure is invisible from the outside and worse than it looks. In ``shadow``, which is
what ships, the morning email would report a real-looking measurement over real recordings
and say the gate is ready to arm. It would be near-zero because the question was never put,
not because these recordings are clean.

So these tests run the REAL :class:`transcriber.extract.Extractor` against a stub transport
and assert on the bytes that would have gone out on the wire, and on what comes back:

* the outbound schema carries ``sensitive_passages`` and the outbound system prompt carries
  the sensitivity instructions, whenever the gate is not switched off;
* with ``GATE_MODE=off`` neither is sent — off means not in the way;
* the returned passages survive onto the ``Extraction``;
* they reach :meth:`transcriber.pipeline.Pipeline._assess` and produce a held finding for a
  category no mechanical rule can see;
* "the model said nothing" and "the model was never asked" stay two different answers all
  the way through, because the measurement that decides whether to arm the gate is built on
  the difference.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from transcriber import prompts, sensitivity
from transcriber.extract import AnalysisSettings, Extraction, Extractor
from transcriber.models import Transcript

#: A recording carrying one of each of the four categories that need a reading to be seen,
#: and nothing a mechanical rule would trip over: no "don't write this down", no identity
#: number. If the question is not asked, this transcript classifies as completely clean.
TEXT = (
    "Right, I'm at Beach Court. Marius has his disciplinary hearing on Friday and he will "
    "probably be dismissed. Elmarie is off this week because her husband is having the "
    "bypass on Tuesday. Our attorney says we must not admit the slab was our error. On the "
    "money, we raised R1.65m and we'll land at R1.604m. The chromadek arrives Tuesday and "
    "the quote to the body corporate is R4,500 for the torch-on repair."
)

HELD_QUOTES = {
    "staff_matter": "Marius has his disciplinary hearing on Friday",
    "personal_circumstances": "her husband is having the bypass on Tuesday",
    "legal_exposure": "we must not admit the slab was our error",
    "own_margin": "we raised R1.65m and we'll land at R1.604m",
}


class _Wire:
    """A stand-in transport that records every request body it is handed."""

    def __init__(self, settings: AnalysisSettings, *, passages: Any = "default") -> None:
        self.settings = settings
        self.passages = passages
        self.bodies: list[dict[str, Any]] = []

    @property
    def strong_body(self) -> dict[str, Any]:
        for body in self.bodies:
            if body.get("model") == self.settings.model_strong:
                return body
        raise AssertionError("the strong model was never called")

    def __call__(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        if body.get("model") == self.settings.model_cheap:
            data: dict[str, Any] = {
                "label": "substantive",
                "one_line": "A site call about a hearing, an absence, the attorney and the money.",
                "languages": ["English"],
                "mentions": {"person": True, "site": True, "number": True, "date": True,
                             "amount": True, "approval": False, "promise": False},
                "reason": "it names a site, people, dates and amounts",
            }
        else:
            data = {
                "summary_en": "A site call at Beach Court. Several matters were discussed.",
                "languages": ["English"],
                "participants": [],
                "site": {"name": "Beach Court", "quote": "Right, I'm at Beach Court."},
                "decisions": [], "money": [], "materials": [], "defects": [], "safety": [],
                "programme": [], "open_questions": [], "follow_ups": [], "commitments": [],
                "unclear_passages": [],
            }
            if self.passages == "default":
                data["sensitive_passages"] = [
                    {
                        "quote": quote,
                        "category": category,
                        "who_is_harmed": "a person",
                        "what_it_is": "a staff matter",
                        "reason": "it would harm somebody if it were repeated",
                        "confidence": 0.95,
                    }
                    for category, quote in HELD_QUOTES.items()
                ]
            elif self.passages is not None:
                data["sensitive_passages"] = self.passages
        return {
            "content": [{"type": "text", "text": json.dumps(data)}],
            "usage": {"input_tokens": 10, "output_tokens": 10},
            "stop_reason": "end_turn",
        }


def _run(*, mode: str, passages: Any = "default") -> tuple[Extraction, _Wire]:
    """The real extractor, with the gate configured exactly as ``GATE_MODE`` would."""
    settings = AnalysisSettings(
        provider="anthropic",
        api_key="offline-not-a-key",
        sensitivity=mode != "off",
    )
    wire = _Wire(settings, passages=passages)
    extraction = Extractor(settings, caller=wire).extract(
        Transcript(text=TEXT, engine="test")
    )
    return extraction, wire


def _pipeline_and_row(*, mode: str) -> tuple[Any, Any]:
    """A real pipeline over a temporary ledger, for the ``_assess`` seam."""
    import os
    import tempfile

    from tests import support
    from transcriber.ledger import Ledger
    from transcriber.models import DriveItem, Route
    from transcriber.pipeline import Pipeline

    directory = tempfile.mkdtemp()
    route = Route(
        name="calls", label="Phone calls", source_folder_id="S", output_folder_id="O",
        archive_folder_id="", engine="", enabled=True,
    )
    config = support.make_config(
        routes=(route,),
        work_dir=os.path.join(directory, "work"),
        ledger_path=os.path.join(directory, "ledger.sqlite3"),
        gate_mode=mode,
        gate_held_store=os.path.join(directory, "held.sqlite3"),
        gate_review_base_url="https://review.invalid/held",
    )
    ledger = Ledger(config.ledger_path)
    pipeline = Pipeline(config, ledger, None)
    ledger.record_page(
        [DriveItem(item_id="C1", name="Call Carel_260827_120055.m4a", size=4096,
                   etag='"C1"', created_at="2026-08-27T09:00:00Z")],
        "cursor-1", route="calls",
    )
    row = ledger.get("C1")
    assert row is not None
    return pipeline, row


class TheQuestionIsOnTheWire(unittest.TestCase):
    """What actually leaves this process when the gate is running."""

    def test_the_schema_asks_for_sensitive_passages(self) -> None:
        _, wire = _run(mode="shadow")
        schema = wire.strong_body["output_config"]["format"]["schema"]
        self.assertIn(
            "sensitive_passages", schema["properties"],
            "the outbound schema does not ask for sensitive_passages, so the model cannot "
            "return any and the gate runs on its mechanical rules alone",
        )
        self.assertIn("sensitive_passages", schema["required"])

    def test_the_system_prompt_carries_the_sensitivity_instructions(self) -> None:
        _, wire = _run(mode="shadow")
        system = "".join(block["text"] for block in wire.strong_body["system"])
        self.assertIn("SENSITIVE PASSAGES", system)
        # The four categories no mechanical rule can see. Each has to be described to the
        # model or it cannot report one.
        for category in ("staff_matter", "personal_circumstances", "legal_exposure",
                         "own_margin"):
            self.assertIn(category, system)

    def test_shadow_asks_the_same_question_as_on(self) -> None:
        """Shadow measures the real classifier or it measures nothing.

        The whole reason the gate ships dark is that the estimates of how much it touches
        differ by a factor of twenty-five. A shadow run that asks a smaller question than
        the armed run produces a number that is not about the thing being armed.
        """
        _, shadow = _run(mode="shadow")
        _, armed = _run(mode="on")
        self.assertEqual(shadow.strong_body["system"], armed.strong_body["system"])
        self.assertEqual(
            shadow.strong_body["output_config"]["format"]["schema"],
            armed.strong_body["output_config"]["format"]["schema"],
        )

    def test_off_sends_nothing_extra_at_all(self) -> None:
        """``off`` is not "inactive", it is "not in the way"."""
        extraction, wire = _run(mode="off", passages=None)
        schema = wire.strong_body["output_config"]["format"]["schema"]
        system = "".join(block["text"] for block in wire.strong_body["system"])
        self.assertNotIn("sensitive_passages", schema["properties"])
        self.assertNotIn("sensitive_passages", schema["required"])
        self.assertEqual(system, prompts.EXTRACTION_SYSTEM)
        self.assertIsNone(extraction.sensitive_passages)


class TheAnswerSurvives(unittest.TestCase):
    """What comes back, and whether it reaches the gate."""

    def test_the_passages_reach_the_extraction(self) -> None:
        extraction, _ = _run(mode="shadow")
        self.assertIsNotNone(extraction.sensitive_passages)
        self.assertEqual(len(extraction.sensitive_passages or ()), len(HELD_QUOTES))
        returned = {entry["category"] for entry in extraction.sensitive_passages or ()}
        self.assertEqual(returned, set(HELD_QUOTES))

    def test_the_four_unseeable_categories_are_actually_held(self) -> None:
        """The end of the wire: a real answer, through the real classifier, to a held band.

        Run the mechanical rules on their own over this transcript and they find nothing —
        it contains no instruction not to write something down and no bare identifier. Every
        hold here exists only because the question was asked.
        """
        extraction, _ = _run(mode="shadow")
        settings = sensitivity.GateSettings(mode="shadow")

        rules_only = sensitivity.assess(TEXT, None, settings=settings)
        self.assertEqual(
            rules_only.would_hold(), (),
            "this transcript is meant to be invisible to the mechanical rules, so that the "
            "test proves the model's answer and not the rules'",
        )

        report = sensitivity.assess(TEXT, extraction.sensitive_passages, settings=settings)
        self.assertTrue(report.model_answered)
        held = {finding.category for finding in report.would_hold()}
        self.assertEqual(held, set(HELD_QUOTES))

    def test_it_reaches_the_pipeline_assessment(self) -> None:
        """``_assess`` is the seam the answer has to cross, and it is duck-typed.

        The gate reads ``getattr(extraction, "sensitive_passages", None)``. Before this was
        wired that ``getattr`` returned ``None`` on every recording ever made, and the
        rules-only branch fired every time — silently, because a duck-typed read of a field
        that does not exist looks exactly like a model that answered nothing.
        """
        extraction, _ = _run(mode="shadow")
        pipeline, row = _pipeline_and_row(mode="shadow")
        self.addCleanup(pipeline.ledger.close)
        transcript = Transcript(text=TEXT, engine="test")

        standing = pipeline._assess(row, transcript)
        report = pipeline._assess(row, transcript, extraction, standing=standing)

        self.assertTrue(report.model_answered)
        self.assertEqual(
            {finding.category for finding in report.would_hold()}, set(HELD_QUOTES)
        )

    def test_the_old_shape_still_falls_back_rather_than_pretending(self) -> None:
        """An extraction carrying no answer leaves the standing rules-only report in place."""
        pipeline, row = _pipeline_and_row(mode="shadow")
        self.addCleanup(pipeline.ledger.close)
        transcript = Transcript(text=TEXT, engine="test")

        standing = pipeline._assess(row, transcript)
        report = pipeline._assess(
            row, transcript, Extraction(routing=_run(mode="off", passages=None)[0].routing),
            standing=standing,
        )
        self.assertFalse(report.model_answered)
        self.assertEqual(report.would_hold(), ())


class AskedAndNotAskedStayDifferent(unittest.TestCase):
    """The distinction the measurement is built on.

    ``()`` is "asked, and this recording is clean". ``None`` is "nobody asked". They read
    identically as a count of holds and need opposite responses: the first is the number
    James is meant to arm the gate on, and the second means there is no number yet.
    """

    def test_an_empty_answer_is_an_answer(self) -> None:
        extraction, _ = _run(mode="shadow", passages=[])
        self.assertEqual(extraction.sensitive_passages, ())
        report = sensitivity.assess(
            TEXT, extraction.sensitive_passages, settings=sensitivity.GateSettings(mode="shadow")
        )
        self.assertTrue(report.model_answered)
        self.assertNotIn(
            "did not answer", " ".join(report.notes),
            "an empty list is the model saying 'nothing here', not the model staying silent",
        )

    def test_a_dropped_field_is_not_an_answer_and_does_not_lose_the_recording(self) -> None:
        """A provider that stops honouring the schema costs the measurement, never the record.

        ``sensitive_passages`` is in the schema's ``required`` list, and a missing required
        field otherwise fails the whole analysis. It may not do that here: in shadow nothing
        is being withheld, so quarantining a recording over the gate's own field would let a
        measurement destroy exactly what the service exists to preserve.
        """
        extraction, _ = _run(mode="shadow", passages=None)
        self.assertIsNone(extraction.sensitive_passages)
        # The recording is intact: this is a normal, complete extraction.
        self.assertEqual(extraction.site, "Beach Court")
        self.assertTrue(extraction.summary)

        report = sensitivity.assess(
            TEXT, extraction.sensitive_passages, settings=sensitivity.GateSettings(mode="shadow")
        )
        self.assertFalse(report.model_answered)
        self.assertIn("did not answer", " ".join(report.notes))

    def test_a_trivial_recording_was_never_asked(self) -> None:
        """The router never sends a trivial recording to the strong model, so nobody asked.

        It must not therefore count as a recording the classifier read and cleared.
        """
        settings = AnalysisSettings(
            provider="anthropic", api_key="offline-not-a-key", sensitivity=True
        )

        def trivial(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
            data = {"label": "trivial", "one_line": "Nothing was said.", "languages": ["English"],
                    "mentions": {}, "reason": "nothing in it"}
            return {"content": [{"type": "text", "text": json.dumps(data)}],
                    "usage": {}, "stop_reason": "end_turn"}

        extraction = Extractor(settings, caller=trivial).extract(
            Transcript(text="Uh. Ja. Okay then.", engine="test")
        )
        self.assertTrue(extraction.trivial)
        self.assertIsNone(extraction.sensitive_passages)


class NothingHeldReachesTheLedgerRow(unittest.TestCase):
    """``Extraction.to_dict`` is written into the ledger and printed by ``status``."""

    def test_only_a_count_is_recorded(self) -> None:
        extraction, _ = _run(mode="shadow")
        rendered = json.dumps(extraction.to_dict())
        for quote in HELD_QUOTES.values():
            self.assertNotIn(quote, rendered)
        self.assertEqual(extraction.to_dict()["sensitive_passages_returned"], len(HELD_QUOTES))

    def test_not_asked_is_recorded_as_not_asked(self) -> None:
        extraction, _ = _run(mode="off", passages=None)
        self.assertIsNone(extraction.to_dict()["sensitive_passages_returned"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
