"""This pipeline decides nothing, and no item it produces can say that it did.

The downstream record makes you say who decided: ``decided_by: <a person>`` is applied,
``observed_by: agent`` is filed as a question. That distinction is the record's whole
defence against a guess wearing a fact's clothes, and it only works if an agent physically
cannot produce the first form.

So this is asserted three ways, from weakest to strongest:

  * the record type has no ``decided_by`` field and refuses any ``observed_by`` but agent;
  * nothing rendered into any output contains the string, and the contract check refuses a
    file that does;
  * no module in ``src/transcriber`` writes the string at all, other than to refuse it.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import unittest

from transcriber import outputs
from transcriber.models import ExtractedItem, ITEM_KINDS, Transcript

from .test_output_contract import build_context


class TheRecordTypeCannotExpressADecision(unittest.TestCase):
    def test_there_is_no_decided_by_field(self) -> None:
        names = {f.name for f in dataclasses.fields(ExtractedItem)}
        self.assertNotIn("decided_by", names)
        self.assertIn("observed_by", names)

    def test_observed_by_is_agent_and_nothing_else(self) -> None:
        item = ExtractedItem(kind="commitment", text="x", quote="y")
        self.assertEqual(item.observed_by, "agent")

        for pretender in ("James Janeke", "James", "user", "system", ""):
            with self.subTest(observed_by=pretender):
                with self.assertRaises(ValueError):
                    ExtractedItem(kind="commitment", text="x", quote="y", observed_by=pretender)

    def test_setting_decided_by_at_construction_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            ExtractedItem(kind="commitment", text="x", quote="y", decided_by="James Janeke")

    def test_the_dictionary_it_writes_out_carries_observed_by_and_no_decision(self) -> None:
        item = ExtractedItem(kind="commitment", text="x", quote="y", speaker="James")
        as_dict = item.to_dict()

        self.assertEqual(as_dict["observed_by"], "agent")
        self.assertNotIn("decided_by", as_dict)
        self.assertNotIn("decided_by", json.dumps(as_dict))

    def test_an_item_without_a_quote_is_an_assertion_and_is_refused(self) -> None:
        """No quote means nothing to check it against, which is the same as deciding it."""
        with self.assertRaises(ValueError):
            ExtractedItem(kind="commitment", text="somebody will fix the roof", quote="")

    def test_an_item_must_say_what_kind_of_thing_it_is(self) -> None:
        with self.assertRaises(ValueError):
            ExtractedItem(kind="", text="x", quote="y")
        for kind in ITEM_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(ExtractedItem(kind=kind, text="x", quote="y").kind, kind)

    def test_an_address_in_an_item_is_removed_and_the_removal_is_recorded(self) -> None:
        item = ExtractedItem(
            kind="commitment",
            text="Send it to carel@example.co.za",
            quote="send it to carel@example.co.za",
        )
        self.assertNotIn("@", item.text)
        self.assertNotIn("@", item.quote)
        self.assertTrue(item.redacted)
        self.assertTrue(item.to_dict()["redacted"])


class NoOutputEverCarriesADecision(unittest.TestCase):
    def test_none_of_the_three_rendered_files_contains_the_string(self) -> None:
        for rendered in outputs.render_all(build_context()):
            with self.subTest(kind=rendered.kind):
                self.assertNotIn("decided_by", rendered.text)

    def test_the_actions_file_says_in_plain_words_that_nothing_is_decided(self) -> None:
        """It is read by a person, so the rule has to be legible, not only enforced."""
        actions = [f for f in outputs.render_all(build_context()) if f.kind == "actions"][0]

        self.assertIn("Nothing here has been decided", actions.text)
        self.assertIn("this pipeline cannot decide", actions.text)
        self.assertIn("proposed, not decided", actions.text)
        stamped = [l for l in actions.text.split("\n") if l == "- observed_by: agent"]
        self.assertEqual(len(stamped), actions.text.count("- Kind: ") + 1,
                         "every proposal, and the file itself, must be stamped observed_by: agent")

    def test_every_proposal_carries_the_words_it_came_from(self) -> None:
        actions = [f for f in outputs.render_all(build_context()) if f.kind == "actions"][0]
        headings = re.findall(r"^### \d+\. ", actions.text, re.MULTILINE)
        quotes = re.findall(r"^> ", actions.text, re.MULTILINE)

        self.assertEqual(len(headings), 2)
        self.assertEqual(len(quotes), len(headings), "a proposal was written without its quote")

    def test_a_file_containing_the_string_is_refused_by_the_contract_check(self) -> None:
        good = "Subject: A voice note\nDate: 2026-08-27 14:30:05 +02:00\n\n- observed_by: agent\n"
        self.assertEqual(outputs.check_contract(good), [])

        bad = good + "- decided_by: James Janeke\n"
        self.assertTrue(any("cannot decide" in p for p in outputs.check_contract(bad)))

    def test_an_unverified_item_is_refused_rather_than_written_with_a_caveat(self) -> None:
        """The quote guard is only a guard if it cannot be talked past at render time."""
        from . import support

        unverified = ExtractedItem(
            kind="commitment",
            text="Somebody said the retention was released",
            quote="the retention has been released",
            quote_verified=False,
        )
        ctx = build_context(
            extraction=support.StubExtraction(
                summary="A short note.",
                proposals=[support.StubProposal("commitments", unverified)],
            )
        )
        with self.assertRaises(outputs.OutputContractError):
            outputs.render_actions(ctx)


class NoModuleWritesTheString(unittest.TestCase):
    """Mechanical, across the whole service. 'We would never' is not an enforcement."""

    ROOT = pathlib.Path(outputs.__file__).parent

    #: The three ways a field actually gets produced in this codebase: as a keyword, as a
    #: dictionary key, or as an attribute. Prose about the rule is not one of them, which is
    #: why this looks for the shapes rather than for the word.
    PRODUCES = (
        re.compile(r"\bdecided_by\s*="),
        re.compile(r"""["']decided_by["']\s*:"""),
        re.compile(r"\.decided_by\b"),
    )

    def test_no_module_ever_produces_a_decided_by_field(self) -> None:
        offenders: list[str] = []
        for path in sorted(self.ROOT.rglob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if "decided_by" not in line:
                    continue
                if any(pattern.search(line) for pattern in self.PRODUCES):
                    offenders.append(f"{path.relative_to(self.ROOT)}:{number}: {line.strip()}")
        self.assertEqual(offenders, [], "a module produces 'decided_by' rather than refusing it")

    def test_the_rule_is_stated_where_the_record_type_is_defined(self) -> None:
        """A rule enforced with no explanation is a rule the next person deletes."""
        from transcriber import models

        self.assertIn("no ``decided_by`` field", models.ExtractedItem.__doc__ or "")
        self.assertIn("it cannot decide anything", models.__doc__ or "")

    def test_the_check_itself_would_catch_a_violation(self) -> None:
        """Proof the scan is not vacuous — it must fire on each shape it claims to find."""
        for sample in ('item = X(decided_by="James")', '{"decided_by": "James"}', "x = item.decided_by"):
            with self.subTest(sample=sample):
                self.assertTrue(any(p.search(sample) for p in self.PRODUCES))

    def test_no_module_writes_an_email_address_pattern_into_an_output(self) -> None:
        """The other absolute rule, checked the same way: addresses are only ever removed."""
        rendered = outputs.render_all(build_context())
        from .vendored_ingest import ADDR_RE

        for one in rendered:
            with self.subTest(kind=one.kind):
                self.assertEqual(ADDR_RE.findall(one.text), [])


class TheTranscriptIsEvidenceAndTheRestIsNot(unittest.TestCase):
    def test_each_file_says_what_it_is(self) -> None:
        files = {f.kind: f.text for f in outputs.render_all(build_context())}

        self.assertIn("Neither of those two files is evidence. This one is.", files["transcript"])
        flat = " ".join(files["summary"].split())
        self.assertIn("A machine's reading of the transcript", flat)
        self.assertIn("none of it is a status, a decision, or a fact about the job", flat)

    def test_the_transcript_does_not_invent_speakers_the_engine_did_not_give(self) -> None:
        """Naming "Speaker 1" would be the pipeline asserting something the audio does not."""
        ctx = build_context(
            transcript=Transcript(
                text="Just one run of words, no diarisation at all.",
                segments=[],
                language="en-ZA",
                engine="test-engine",
            )
        )
        text = outputs.render_transcript(ctx)
        self.assertNotIn("Speaker 1", text)
        self.assertIn("The engine returned no segment timings", text)


if __name__ == "__main__":
    unittest.main()
