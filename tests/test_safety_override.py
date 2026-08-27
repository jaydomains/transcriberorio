"""The safety override: a twelve-second approval must never be thrown away as chatter.

The recording this whole service was built around is short. *"Ja, approved, go ahead on
Beach Court"* is twelve seconds long, and every heuristic that has ever been applied to
voice notes — too short, too small, no keywords — discards it. So the override is not
judgement, it is a regex pass over the transcript, and it can only ESCALATE: there is no
pattern in the table that makes a recording less likely to be read.

The two tiers are exercised here with an injected transport, so the whole pass runs offline
with no credential and no network.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, Mapping

from transcriber import prompts
from transcriber.extract import (
    AnalysisSettings,
    AnalysisTransportError,
    Extractor,
    route_precheck,
)
from transcriber.models import Hints

BEACH_COURT = "Ja, approved, go ahead on Beach Court."


def settings(**overrides: Any) -> AnalysisSettings:
    values: dict[str, Any] = {
        "provider": "anthropic",
        "api_key": "not-a-real-key",
        "model_cheap": "router-model",
        "model_strong": "reader-model",
    }
    values.update(overrides)
    return AnalysisSettings(**values)


def anthropic_answer(payload: Mapping[str, Any]) -> dict[str, Any]:
    """One Anthropic Messages response carrying schema-constrained JSON."""
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def classifier_says(label: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "label": label,
        "one_line": "A short voice note.",
        "languages": ["English"],
        "mentions": {
            key: False
            for key in ("person", "site", "number", "date", "amount", "approval", "promise")
        },
        "reason": "it is very short",
    }
    body.update(extra)
    return anthropic_answer(body)


def empty_extraction(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary_en": "Somebody said an instruction was given about a site.",
        "languages": ["English"],
        "participants": [],
        "site": {"name": "", "quote": ""},
        "unclear_passages": [],
    }
    for category in prompts.EXTRACTION_CATEGORIES:
        body[category] = []
    body.update(extra)
    return anthropic_answer(body)


class _Caller:
    """A scripted transport. Returns each queued answer in turn and records the calls."""

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self.answers = list(answers)
        self.models: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.models.append(str(body.get("model")))
        if not self.answers:
            raise AssertionError("the pass made more model calls than the test scripted")
        return self.answers.pop(0)


class TheTwelveSecondApproval(unittest.TestCase):
    def test_the_precheck_alone_forces_it_substantive(self) -> None:
        triggers = route_precheck(BEACH_COURT)

        self.assertTrue(triggers, "nothing in the safety table saw the approval")
        categories = {t.category for t in triggers}
        self.assertIn("approval", categories)
        self.assertIn("person", categories, "'Beach Court' must register as a name or a place")

    def test_the_router_calling_it_trivial_is_overruled(self) -> None:
        """The case this exists for. The model's answer may promote, never demote."""
        caller = _Caller(classifier_says("trivial"), empty_extraction())
        extraction = Extractor(settings(), caller=caller).extract(BEACH_COURT)

        self.assertTrue(extraction.routing.substantive)
        self.assertTrue(extraction.routing.escalated)
        self.assertEqual(extraction.routing.model_label, "trivial")
        self.assertFalse(extraction.trivial)
        self.assertEqual(caller.models, ["router-model", "reader-model"],
                         "the strong model must actually have been asked to read it")

    def test_why_it_was_read_is_said_in_plain_words(self) -> None:
        """A person reads this line in the actions file. It cannot be a category code."""
        caller = _Caller(classifier_says("trivial"), empty_extraction())
        extraction = Extractor(settings(), caller=caller).extract(BEACH_COURT)

        why = extraction.routing.why()
        self.assertIn("the router model called this trivial", why)
        self.assertIn("the safety check disagreed", why)

    def test_the_router_being_unreachable_escalates_rather_than_skips(self) -> None:
        """A model that cannot be reached must never mean a recording is not read."""

        def unreachable(url, headers, body):
            raise AnalysisTransportError("the analysis provider could not be reached")

        routing = Extractor(settings(), caller=unreachable).classify(BEACH_COURT)

        self.assertTrue(routing.substantive)
        self.assertTrue(routing.forced)
        self.assertEqual(routing.model_label, "unavailable")
        self.assertTrue(any("could not be reached" in note for note in routing.notes))

    def test_a_label_the_router_is_not_supposed_to_produce_escalates(self) -> None:
        caller = _Caller(classifier_says("maybe"))
        routing = Extractor(settings(), caller=caller).classify("Mmm. Yeah. Okay then.")

        self.assertTrue(routing.substantive)
        self.assertTrue(any("not one of its three labels" in note for note in routing.notes))


class EverythingTheOverrideMustCatch(unittest.TestCase):
    """One case per category, each phrased the way somebody actually says it on a site."""

    CASES = (
        ("approval", "Ja, approved, go ahead."),
        ("approval", "Hy het gesê ons kan maar gaan voort."),
        ("promise", "I'll send it through."),
        ("person", "Told Carel."),
        ("site", "At the unit."),
        ("number", "Forty two."),
        ("date", "Tomorrow morning."),
        ("amount", "It's about eighteen thousand rand."),
        ("trade", "The torch-on is lifting."),
    )

    def test_each_category_fires(self) -> None:
        for category, text in self.CASES:
            with self.subTest(category=category, text=text):
                categories = {t.category for t in route_precheck(text)}
                self.assertIn(category, categories, f"{text!r} did not trigger {category}")

    def test_a_named_site_from_the_deployment_s_own_vocabulary_fires(self) -> None:
        triggers = route_precheck("quick one on chepstow mews", extra_terms=("Chepstow Mews",))
        self.assertIn("trade", {t.category for t in triggers})
        # And without it in the deployment's vocabulary, the same words are still caught by
        # the general site pattern — the vocabulary widens the net, it does not carry it.
        self.assertTrue(route_precheck("quick one on chepstow mews"))

    def test_the_override_finds_nothing_in_genuine_chatter(self) -> None:
        """It must be capable of staying silent, or 'it always fires' proves nothing."""
        self.assertEqual(route_precheck("mmm. yeah. eh. hmm."), ())
        self.assertEqual(route_precheck(""), ())

    def test_a_recording_with_no_triggers_is_left_to_the_router(self) -> None:
        caller = _Caller(classifier_says("trivial"))
        routing = Extractor(settings(), caller=caller).classify("Mmm. Yeah. Eh. Hmm.")

        self.assertFalse(routing.substantive)
        self.assertFalse(routing.forced)
        self.assertFalse(routing.escalated)

    def test_the_router_may_still_promote_what_the_precheck_missed(self) -> None:
        caller = _Caller(classifier_says("substantive"))
        routing = Extractor(settings(), caller=caller).classify("Mmm. Yeah. Eh. Hmm.")
        self.assertTrue(routing.substantive)

    def test_a_transcript_too_long_to_route_whole_is_read_in_full_anyway(self) -> None:
        """An excerpt may cost a model call; it may never cost a recording."""
        caller = _Caller(classifier_says("trivial"), empty_extraction())
        long_text = "mmm yeah eh hmm " * 20_000
        extractor = Extractor(settings(classify_excerpt_chars=1000), caller=caller)

        extraction = extractor.extract(long_text)

        self.assertTrue(extraction.routing.substantive)
        self.assertTrue(any("only its beginning and end" in n for n in extraction.notes))


class TheOverrideCanOnlyEscalate(unittest.TestCase):
    def test_no_pattern_in_the_table_marks_anything_trivial(self) -> None:
        """Read as: a bad regex here can cost money and can never cost a recording."""
        from transcriber.extract import SAFETY_CATEGORIES, SAFETY_PATTERNS

        for category, _pattern, why in SAFETY_PATTERNS:
            with self.subTest(category=category):
                self.assertIn(category, SAFETY_CATEGORIES)
                self.assertNotIn("trivial", why.lower())
                self.assertNotIn("skip", why.lower())

    def test_hints_never_carry_an_address_into_a_prompt(self) -> None:
        hints = Hints(counterparty="Carel (carel@example.com)", vocabulary=("ridge",))
        self.assertNotIn("@", hints.prompt_text())


if __name__ == "__main__":
    unittest.main()
