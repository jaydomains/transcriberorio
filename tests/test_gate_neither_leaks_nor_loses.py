"""The two failures the sensitivity gate can have, proved separately: leaking, and losing.

Everything here is written from the outside in. It builds a recording, runs it through the
real pipeline, reads the bytes that would have reached OneDrive, and asks two questions of
them — *did a held word get out?* and *did anything that was not held get destroyed on the
way?* Those are the only two ways this feature can fail, and they fail in opposite
directions, so a test that only ever asserts one of them will happily pass a service that
holds everything or a service that holds nothing.

The second question is the one that is easy to forget. A gate that withheld the whole
transcript would satisfy every "no held words in the output" assertion in this file. So
every leak test here has a companion that names something which **must** still be in the
bytes: the price, the supplier, the delivery date, the action item whose quote sits in
clear text. His decision on prices was taken against his own instinct on a measurement, and
it is the decision most likely to be quietly walked back by a well-meaning tightening of
the classifier — so it is asserted on the rendered files, twice, with his own examples.

The record's own parser and its own question harvester are vendored (``tests/
vendored_ingest.py``, and the two patterns in :data:`RECORD_QUESTION` /
:data:`RECORD_NOISE` below, copied from ``kbc-site-memory/tools/transcripts.py``). Both are
copies rather than imports on purpose: that repository is read-only to this service and is
not on the path in CI. A marker that the record cannot see is a hole in the record that
nothing announces, which decision-note 2 says is worse than the leak it prevents.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import stat
import tempfile
import unittest
from typing import Any

from tests import support, vendored_ingest
from transcriber import extract, naming, outputs, redact, release, review_page, review_server
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, ExtractedItem, Route, Segment, Transcript
from transcriber.pipeline import Pipeline
from transcriber.withheld import Decision, WithheldStore, held_spans_from

# --------------------------------------------------------------------------- the record's own scan

#: ``kbc-site-memory/tools/transcripts.py``, verbatim as of 2026-08-28. This is the scan
#: that puts a stated unknown onto ``09-portfolio/12-ask-james.md``, which is one of the six
#: sources the assistant actually reads on site. The inbox is not one of them, so a marker
#: this pattern does not catch is invisible to the person on the roof.
RECORD_QUESTION = re.compile(r"(?:^|(?<=[.!?\n]))\s*([^.!?\n]{15,240}\?)", re.M)
RECORD_NOISE = re.compile(
    r"^(you know|right|okay|ok|sorry|what|hey|huh|hmm|yeah|ja)\b[\s,?]*$", re.I
)


def questions_in(text: str) -> list[str]:
    """What the record would lift out of this text and file as a question. Its own logic."""
    out: list[str] = []
    for match in RECORD_QUESTION.finditer(text or ""):
        question = re.sub(r"\s+", " ", match.group(1)).strip()
        if len(question) < 15 or RECORD_NOISE.match(question):
            continue
        if question not in out:
            out.append(question)
    return out


# --------------------------------------------------------------------------- the routes

CALLS = Route(
    name="calls",
    label="Phone calls",
    source_folder_id="S-CALLS",
    output_folder_id="O-CALLS",
    archive_folder_id="",
    engine="",
    enabled=True,
)
SITE_MEETINGS = Route(
    name="site-meetings",
    label="Site meetings",
    source_folder_id="S-SITE",
    output_folder_id="O-SITE",
    archive_folder_id="",
    engine="",
    enabled=True,
)

JAMES = "james@invalid"
THABO = "thabo@invalid"

# --------------------------------------------------------------------------- the recording
#
# One call, holding one of everything. The held band and the let-through band are
# deliberately interleaved sentence by sentence, because a classifier that cuts on
# paragraph boundaries, or a masker that widens a span to the nearest turn, passes a fixture
# where the sensitive material is all in one place and fails this one.

PRICE = "I told Reno it's R4,500 for the waterproofing to the north elevation."
MARGIN = "We raised R1.65m and we'll land at R1.604m on this one."
STAFF = "Marius has his disciplinary hearing on Friday and he will probably be dismissed."
HEALTH = "Elmarie is off because her husband is having the bypass on Tuesday."
LEGAL = "Our own site instruction is what caused the crack, so this must not leave the firm."
IDENT = "The new chap's ID number is 8001015009087, put him on the site register."
ORDINARY = "The bricks land Thursday and Carel has the scaffold up at unit four."
SUPPLIER = "Marius quoted R820 a square for the screed and the invoice is 4501234567890."

#: Two throwaway sentences with nothing in them, either side of the passage that says it
#: must not leave the firm. They are there because that rule holds the sentence either side
#: of itself — an instruction is about the words around it — and without them it would
#: swallow the margin and the identity number into one span, and this fixture would stop
#: proving that each band is caught on its own terms.
BUFFER_BEFORE = "Anyway, the scaffold comes down at the end of the month."
BUFFER_AFTER = "So that is where we are on that one."

TEXT = " ".join(
    [ORDINARY, PRICE, STAFF, SUPPLIER, HEALTH, MARGIN, BUFFER_BEFORE, LEGAL, BUFFER_AFTER, IDENT]
)

#: An action item whose quote sits entirely in text nothing holds. It must survive.
CLEAR_QUOTE = "The bricks land Thursday"
#: An action item whose quote runs from clear text across a held identifier and out the
#: other side. It must not be silently destroyed either — the words that were held go, the
#: item stays.
STRADDLING_QUOTE = "ID number is 8001015009087, put him on the site register"

#: What the model returns on the one call that is already being made. The mechanical rules
#: catch the "between you and me" and the identity number on their own; everything else here
#: is the model's reading, which is exactly the split the design describes.
MODEL_ANSWER = {
    "sensitive_passages": [
        {
            "category": "commercial_figure",
            "quote": "R4,500 for the waterproofing to the north elevation",
            "confidence": 0.95,
            "what_it_is": "a price for waterproofing",
            "reason": "a price quoted to a client",
        },
        {
            "category": "commercial_figure",
            "quote": "R820 a square for the screed",
            "confidence": 0.95,
            "what_it_is": "a supplier rate for screed",
            "reason": "a supplier rate",
        },
        {
            "category": "staff_matter",
            "quote": "has his disciplinary hearing on Friday and he will probably be dismissed",
            "confidence": 0.95,
            "what_it_is": "a disciplinary matter",
            "reason": "a hearing and a likely dismissal",
            "who_is_harmed": "the employee",
        },
        {
            "category": "personal_circumstances",
            "quote": "is off because her husband is having the bypass on Tuesday",
            "confidence": 0.95,
            "what_it_is": "somebody's family health",
            "reason": "a named person's family health",
            "who_is_harmed": "her family",
        },
        {
            "category": "own_margin",
            "quote": "We raised R1.65m and we'll land at R1.604m",
            "confidence": 0.95,
            "what_it_is": "a cost set against a charge",
            "reason": "our own cost against our own charge in one breath",
        },
        {
            "category": "legal_exposure",
            "quote": "our own site instruction is what caused the crack",
            "confidence": 0.95,
            "what_it_is": "an admission of our own liability",
            "reason": "an admission of liability",
        },
    ]
}

#: Every held-band passage's own words, as they appear in the transcript. Nothing in this
#: list may appear in any rendered file when the gate is armed — and every one of them must
#: appear in all three when it is not.
HELD_WORDS = (
    "disciplinary hearing on Friday",
    "probably be dismissed",
    "husband is having the bypass",
    "R1.65m",
    "R1.604m",
    "own site instruction is what caused the crack",
    "8001015009087",
)

#: The traffic. None of this may ever be held, in any mode, for any reason.
FLOWS = (
    "R4,500",
    "waterproofing",
    "R820 a square",
    "bricks land Thursday",
    "Carel",
    "unit four",
    "4501234567890",
)


class _Drive:
    """A OneDrive that remembers precisely what was written, and can refuse a name."""

    def __init__(self, refuse: str = "") -> None:
        self.written: list[tuple[str, str, str]] = []
        self.items: dict[str, Any] = {}
        self.refuse = refuse

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        if self.refuse and self.refuse in name:
            raise RuntimeError("the drive refused this one")
        self.written.append((parent_id, name, data.decode("utf-8")))
        item = type(
            "Item",
            (),
            {"id": f"out-{len(self.items)}", "name": name, "size": len(data), "web_url": ""},
        )()
        self.items[item.id] = item
        return item

    def get_item(self, item_id: str) -> Any:
        return self.items[item_id]

    @property
    def names(self) -> list[str]:
        return [name for _p, name, _t in self.written]

    @property
    def all_text(self) -> str:
        return "\n".join(text for _p, _n, text in self.written)

    def text_of(self, kind: str) -> str:
        for _parent, name, text in self.written:
            derived = "-summary" in name or "-actions" in name or "-released-" in name
            if kind == "transcript" and not derived:
                return text
            if kind != "transcript" and f"-{kind}" in name:
                return text
        raise AssertionError(f"no {kind} file was written; got {self.names}")


class _Extraction(support.StubExtraction):
    """The analysis answer, carrying the sensitivity reading on the same call."""

    def __init__(self, *, sensitive_passages: Any = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.sensitive_passages = sensitive_passages


def _item(quote: str, text: str) -> Any:
    return support.StubProposal(
        "commitments",
        ExtractedItem(kind="commitment", text=text, quote=quote, quote_verified=True),
    )


class Recording:
    """One recording, from a transcript to the bytes that would reach the drive."""

    def __init__(
        self,
        mode: str = "on",
        *,
        text: str = TEXT,
        model: Any = MODEL_ANSWER,
        route: Route = CALLS,
        summary: str = "He walked Beach Court. The bricks land Thursday.",
        site: str = "Beach Court",
        quotes: tuple[str, ...] = (CLEAR_QUOTE, STRADDLING_QUOTE),
        reviewers: dict[str, str] | None = None,
        drive: _Drive | None = None,
        store: Any = None,
        item_id: str = "REC-1",
        borrow: "Recording | None" = None,
    ) -> None:
        self.route = route
        if borrow is not None:
            # A second recording in the same deployment: one config, one ledger, one held
            # store, one drive. Two people's passages have to be able to land in one store
            # for the scoping to be worth asserting at all.
            self.dir = borrow.dir
            self.config = borrow.config
            self.ledger = borrow.ledger
            self.drive = borrow.drive
            self.pipeline = borrow.pipeline
        else:
            self.dir = tempfile.mkdtemp(prefix="gate-")
            self.config = support.make_config(
                routes=(CALLS, SITE_MEETINGS),
                smtp_to=(JAMES,),
                route_reviewers=dict(reviewers or {}),
                work_dir=os.path.join(self.dir, "work"),
                ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
                gate_mode=mode,
                gate_held_store=os.path.join(self.dir, "held.sqlite3"),
                gate_review_base_url="https://review.invalid/held",
            )
            self.ledger = Ledger(self.config.ledger_path)
            self.drive = drive or _Drive()
            self.pipeline = Pipeline(self.config, self.ledger, self.drive, withheld=store)
        self.item_id = item_id
        self.ledger.record_page(
            [
                DriveItem(
                    item_id=item_id,
                    name="Call Carel_260824_091500.m4a",
                    size=4096,
                    etag=f'"{item_id}"',
                    created_at="2026-08-24T07:15:00Z",
                )
            ],
            "cursor-1",
            route=route.name,
        )
        self.row = self.ledger.get(item_id)
        assert self.row is not None
        self.transcript = Transcript(
            text=text,
            segments=[Segment(0.0, 60.0, "James", text)],
            language="en-ZA",
            engine="test-engine",
        )
        self.extraction = _Extraction(
            summary=summary,
            site=site,
            proposals=[_item(q, f"Something was said about this ({n})") for n, q in enumerate(quotes)],
            sensitive_passages=model,
        )
        self._gate: Any = None

    # -- the three steps the pipeline takes, in its own order ----------------------

    def report(self) -> Any:
        rules_only = self.pipeline._assess(self.row, self.transcript)
        return self.pipeline._assess(
            self.row, self.transcript, self.extraction, standing=rules_only
        )

    def gate(self) -> Any:
        if self._gate is None:
            self._gate = self.pipeline._withhold(
                self.row, self.route, self.transcript, self.extraction, self.report()
            )
        return self._gate

    def publish(self) -> Any:
        held = self.gate()
        return self.pipeline._publish(
            self.row,
            naming.parse_source_name(self.row.name),
            held.transcript,
            held.extraction,
            support.audio_info(300.0),
            self.route,
            notes=held.notes,
            held=held.held,
        )

    def run(self) -> _Drive:
        self.publish()
        return self.drive

    def store(self) -> WithheldStore:
        return WithheldStore(self.config.held_store_path)

    def close(self) -> None:
        try:
            self.ledger.close()
        except Exception:  # noqa: BLE001 - a borrowed ledger may already be shut
            pass


class _GateCase(unittest.TestCase):
    """Assertions both halves of this file need, said once."""

    def assert_absent(self, text: str, needles: tuple[str, ...], where: str) -> None:
        for needle in needles:
            self.assertNotIn(needle, text, f"{needle!r} reached {where}")

    def assert_present(self, text: str, needles: tuple[str, ...], where: str) -> None:
        for needle in needles:
            self.assertIn(needle, text, f"{needle!r} is missing from {where}")


# =========================================================================== 1. shadow


class ShadowWithholdsNothing(_GateCase):
    """Default configuration, a transcript full of the held band, and nothing is cut.

    This is the claim the whole rollout rests on: it can be switched on in front of his
    recordings without changing a single byte that reaches the record, and it produces the
    number that decides whether to arm it. The estimates of how much this touches differ by
    a factor of twenty-five, and the only way to close that gap is to run the real
    classifier over real recordings while it is incapable of withholding anything.
    """

    def setUp(self) -> None:
        self.recording = Recording("shadow")
        self.addCleanup(self.recording.close)
        self.drive = self.recording.run()

    def test_the_default_configuration_is_this_one(self) -> None:
        fresh = support.make_config(routes=(CALLS,))
        self.assertEqual(fresh.gate_mode, "shadow")

    def test_all_three_files_carry_the_held_words_in_full(self) -> None:
        self.assertEqual(len(self.drive.written), 3)
        transcript = self.drive.text_of("transcript")
        self.assert_present(transcript, HELD_WORDS, "the shadow-mode transcript")
        self.assert_present(transcript, FLOWS, "the shadow-mode transcript")

    def test_nothing_anywhere_is_marked_as_held(self) -> None:
        self.assertEqual(redact.refs_in(self.drive.all_text), ())
        self.assertNotIn("[held ", self.drive.all_text)

    def test_the_store_records_what_it_would_have_held(self) -> None:
        with self.recording.store() as store:
            records = store.for_recording(self.recording.item_id)
        self.assertTrue(records, "shadow recorded nothing, so it measured nothing")
        categories = sorted({r.category for r in records})
        self.assertEqual(
            categories,
            ["bare_identifier", "legal_exposure", "own_margin",
             "personal_circumstances", "staff_matter"],
        )

    def test_and_marks_every_one_of_them_as_never_actually_withheld(self) -> None:
        with self.recording.store() as store:
            records = store.for_recording(self.recording.item_id)
            self.assertTrue(all(r.decision == Decision.NOT_WITHHELD for r in records))
            self.assertTrue(all(not r.withheld for r in records))
            # And there is therefore nothing on anybody's list to answer.
            self.assertEqual(store.queue_for(JAMES), ())

    def test_the_measurement_has_a_denominator_and_not_only_a_numerator(self) -> None:
        with self.recording.store() as store:
            measured = store.measurement()
        self.assertEqual(measured["recordings_classified"], 1)
        self.assertEqual(measured["recordings_with_a_hold"], 1)
        self.assertGreater(measured["characters_read"], 0)
        self.assertGreater(measured["spans"], 0)
        self.assertGreater(measured["fraction_of_text"], 0.0)

    def test_a_shadow_row_cannot_be_answered_by_anybody(self) -> None:
        with self.recording.store() as store:
            record = store.for_recording(self.recording.item_id)[0]
            with self.assertRaises(Exception) as caught:
                store.release(record.hold_id, answered_by="James Janeke")
        self.assertIn("shadow", str(caught.exception))

    def test_shadow_writes_what_off_writes_byte_for_byte(self) -> None:
        off = Recording("off")
        self.addCleanup(off.close)
        dark = off.run()
        self.assertEqual(
            [(n, t) for _p, n, t in dark.written],
            [(n, t) for _p, n, t in self.drive.written],
        )


# =========================================================================== 2. no leak


class HeldWordsReachNoOutput(_GateCase):
    """Armed. Nothing held is in any of the three files, and the traffic is untouched."""

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        self.drive = self.recording.run()

    def test_the_transcript_is_the_file_this_is_actually_about(self) -> None:
        # Three of five design passes put the mask in the actions file. Only the transcript
        # reaches the record, so this is the assertion that matters most in this file.
        self.assert_absent(self.drive.text_of("transcript"), HELD_WORDS, "the transcript")

    def test_nor_the_summary_nor_the_actions_file(self) -> None:
        self.assert_absent(self.drive.text_of("summary"), HELD_WORDS, "the summary")
        self.assert_absent(self.drive.text_of("actions"), HELD_WORDS, "the actions file")

    def test_and_not_in_the_bytes_of_any_file_taken_together(self) -> None:
        self.assert_absent(self.drive.all_text, HELD_WORDS, "the three files")

    def test_the_ordinary_site_talk_is_all_still_there(self) -> None:
        self.assert_present(self.drive.text_of("transcript"), FLOWS, "the armed transcript")

    def test_the_speaker_labelled_body_was_cut_and_not_only_the_flat_text(self) -> None:
        # The rendered body is built from the segments when the engine returned them, so a
        # cut applied to ``text`` alone leaves the whole passage standing in the file.
        body = self.drive.text_of("transcript")
        self.assertIn("James:", body)
        self.assert_absent(body, HELD_WORDS, "the speaker-labelled body")

    def test_the_words_are_in_the_store_before_they_leave_the_transcript(self) -> None:
        with self.recording.store() as store:
            queued = store.queue_for(JAMES)
        self.assertTrue(queued)
        held = "\n".join(r.text for r in queued)
        for needle in HELD_WORDS:
            self.assertIn(needle, held, f"{needle!r} was cut and is nowhere to ask for")


class ABrokenRedactionRefusesTheWholePublish(_GateCase):
    """A masker that silently does nothing must stop all three files, not two of them.

    Here the cut is computed and then thrown away — the shape a masking bug actually takes,
    where the redaction reports success and the text is untouched. Whichever guard notices
    first, the requirement is the same and it is about the *set*: a set with the transcript
    masked and the summary not is worse than a set with neither masked, because it presents
    itself as redacted and nobody looks at it twice.
    """

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        real = redact.redact_transcript

        def broken(transcript: Any, spans: Any, **kw: Any) -> Any:
            _cut, redaction, problems = real(transcript, spans, **kw)
            return transcript, redaction, problems  # the cut is computed and discarded

        redact.redact_transcript = broken  # type: ignore[assignment]
        self.addCleanup(setattr, redact, "redact_transcript", real)

    def _refused(self) -> Exception:
        with self.assertRaises(outputs.OutputContractError) as caught:
            self.recording.publish()
        return caught.exception

    def test_the_publish_is_refused_and_says_nothing_was_written(self) -> None:
        self.assertIn("Nothing was written", str(self._refused()))

    def test_and_not_one_file_was_written(self) -> None:
        self._refused()
        self.assertEqual(self.recording.drive.written, [])

    def test_the_words_are_still_in_the_store_to_be_asked_for(self) -> None:
        self._refused()
        with self.recording.store() as store:
            self.assertTrue(store.queue_for(JAMES))

    def test_the_recording_did_not_reach_done(self) -> None:
        self._refused()
        row = self.recording.ledger.get(self.recording.item_id)
        self.assertIsNotNone(row)
        self.assertNotEqual(row.state, "DONE")


class ARestatementThatEscapedTheMaskRefusesTheWholePublish(_GateCase):
    """The backstop, on the file it is really for.

    The transcript is cut at exact offsets and is the easy case. The summary is a model's
    prose about the *unredacted* transcript — it has to be, or quote verification cannot
    tell an invented quote from a masked one — so it is free to restate in other words what
    the transcript no longer says. It is masked by searching for the held words; this is the
    occasion when that search misses, simulated by putting the summary back afterwards.
    """

    def setUp(self) -> None:
        self.leak = "Marius has his disciplinary hearing on Friday"
        self.recording = Recording("on", summary=f"{self.leak}. The bricks land Thursday.")
        self.addCleanup(self.recording.close)
        real = redact.redact_extraction
        leaked = self.leak

        def missed_it(extraction: Any, redaction: Any) -> Any:
            masked, outcomes = real(extraction, redaction)
            masked.summary = f"{leaked}. The bricks land Thursday."
            return masked, outcomes

        redact.redact_extraction = missed_it  # type: ignore[assignment]
        self.addCleanup(setattr, redact, "redact_extraction", real)

    def test_the_leak_is_named_and_the_whole_set_is_refused(self) -> None:
        with self.assertRaises(outputs.HeldTextWouldLeak) as caught:
            self.recording.publish()
        message = str(caught.exception)
        self.assertIn("summary", message)
        self.assertIn("none of the three has been written", message)
        self.assertTrue(caught.exception.refs)

    def test_the_transcript_was_masked_correctly_and_is_refused_anyway(self) -> None:
        # The point of the assertion: the file that was masked properly does not get to go
        # out on its own. All three or none.
        with self.assertRaises(outputs.HeldTextWouldLeak):
            self.recording.publish()
        self.assertEqual(self.recording.drive.written, [])

    def test_the_message_a_person_reads_carries_none_of_the_words(self) -> None:
        with self.assertRaises(outputs.HeldTextWouldLeak) as caught:
            self.recording.publish()
        self.assert_absent(str(caught.exception), HELD_WORDS, "the refusal message")


class ASpanThatCannotBeCutStopsEverything(_GateCase):
    """The other half of the doubt rule: neither withheld silently nor published silently."""

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        real = redact.redact_transcript

        def cannot_find_it(transcript: Any, spans: Any, **kw: Any) -> Any:
            cut, redaction, _problems = real(transcript, spans, **kw)
            return cut, redaction, ["a held passage could not be found in the transcript"]

        redact.redact_transcript = cannot_find_it  # type: ignore[assignment]
        self.addCleanup(setattr, redact, "redact_transcript", real)

    def test_nothing_is_uploaded_and_a_person_is_told_why(self) -> None:
        with self.assertRaises(Exception) as caught:
            self.recording.publish()
        message = str(caught.exception)
        self.assertIn("could not all be taken out", message)
        self.assertIn("none of its three files", message)
        self.assertEqual(self.recording.drive.written, [])
        self.assert_absent(message, HELD_WORDS, "the message a person reads")


# =========================================================================== 3. no loss


class QuoteVerificationSurvivesRedaction(_GateCase):
    """A redaction that shreds action items is a redaction that loses the record.

    ``extract.verify_quote`` checks an item's quote against the transcript and discards the
    item when it cannot find it. Mask the transcript first and every item quoting a masked
    passage fails that check and is destroyed without a word said about it. The order — 
    verify against the original, then mask both the transcript and the quotes from the same
    redaction — is what stops that, and this is the test of it.
    """

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        self.drive = self.recording.run()
        self.actions = self.drive.text_of("actions")
        self.transcript = self.drive.text_of("transcript")

    def test_the_item_quoting_clear_text_is_still_published(self) -> None:
        self.assertIn(CLEAR_QUOTE, self.actions)

    def test_and_its_quote_still_verifies_against_the_published_transcript(self) -> None:
        # The real check, run at render time against the file that was actually written.
        check = extract.verify_quote(CLEAR_QUOTE, self.transcript)
        self.assertTrue(check.ok, check.reason)
        self.assertEqual(check.method, "exact")

    def test_the_item_straddling_a_hold_is_kept_and_marked_rather_than_dropped(self) -> None:
        # Its quote runs from clear text into a held identifier. The words that were held
        # are gone; the item is not.
        self.assertIn("put him on the site register", self.actions)
        self.assert_absent(self.actions, ("8001015009087",), "the actions file")
        # And it carries the same reference the transcript carries, so a reader can join
        # the two up rather than seeing two unrelated holes.
        self.assertTrue(set(redact.refs_in(self.actions)) <= set(redact.refs_in(self.transcript)))
        self.assertTrue(redact.refs_in(self.actions))

    def test_both_items_are_still_in_the_file(self) -> None:
        self.assertEqual(self.actions.count("Said, verbatim"), 2, self.actions)

    def test_nothing_was_dropped_for_want_of_a_quote(self) -> None:
        gate = self.recording.gate()
        kept = tuple(gate.extraction.proposals)
        self.assertEqual(len(kept), 2)

    def test_verifying_after_the_mask_is_what_would_have_destroyed_it(self) -> None:
        # Stated as a fact about the ordering rather than left implicit: this is what the
        # pipeline would do if it verified quotes against the masked text instead.
        original = self.recording.transcript.text
        self.assertTrue(extract.verify_quote(STRADDLING_QUOTE, original).ok)
        self.assertFalse(extract.verify_quote(STRADDLING_QUOTE, self.transcript).ok)

# =========================================================================== 4. the marker


class TheMarkerIsVisibleNotAbsent(_GateCase):
    """A hole that says nothing is indistinguishable from a recording where nothing was said.

    The record's read path is built from six sources and this service's inbox is not one of
    them, so a marker that only sits in the transcript file is invisible to the assistant
    answering a question on a roof. The marker is therefore phrased to be caught by the
    record's *own* question scan — vendored at the top of this file, straight from
    ``tools/transcripts.py`` — so that the site's live page says a rate was recorded and is
    held, rather than the assistant saying there is no record of a rate. A confident answer
    built on a quietly partial record is worse than the leak it prevents.
    """

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        self.drive = self.recording.run()
        self.transcript = self.drive.text_of("transcript")
        self.refs = redact.refs_in(self.transcript)

    def test_there_is_a_marker_where_the_words_were(self) -> None:
        self.assertTrue(self.refs, "the words were cut and nothing says so")

    def test_it_is_never_a_bare_redacted(self) -> None:
        for bare in ("[redacted]", "[REDACTED]", "***", "[removed]", "[…]"):
            self.assertNotIn(bare, self.transcript)

    def test_it_says_what_kind_of_thing_was_held(self) -> None:
        # Every marker names its kind: the classifier's own public phrase where it offered
        # one that carries no name, figure or address, and the dull phrase for the category
        # where it did not. Both are checked before they are allowed to stand in for words.
        for phrase in (
            "A disciplinary matter was recorded",
            "Somebody's family health was recorded",
            "A cost set against a charge was recorded",
            "A legal matter was recorded",
            "An identity or account number was recorded",
        ):
            self.assertIn(phrase, self.transcript)

    def test_a_phrase_carrying_detail_of_its_own_is_replaced_by_the_dull_one(self) -> None:
        # The marker goes onto a page a client may read, in place of words that were held
        # because they must not be read. A phrase with a name or a figure in it has smuggled
        # the detail into the hole.
        sneaky = dict(MODEL_ANSWER)
        sneaky = {
            "sensitive_passages": [
                dict(entry, what_it_is="Marius being dismissed on R42,000 a month")
                if entry["category"] == "staff_matter"
                else entry
                for entry in MODEL_ANSWER["sensitive_passages"]
            ]
        }
        recording = Recording("on", model=sneaky)
        self.addCleanup(recording.close)
        transcript = recording.run().text_of("transcript")
        self.assertNotIn("R42,000", transcript)
        self.assertIn("A staff matter was recorded", transcript)

    def test_it_says_when_the_conversation_happened_not_when_the_gate_ran(self) -> None:
        # The recording was made on 2026-08-24. What a person needs in order to place a
        # hole in the record is the day of the conversation.
        self.assertIn("24 Aug 2026", self.transcript)

    def test_it_says_how_to_ask_for_it(self) -> None:
        for ref in self.refs:
            self.assertIn(f"held passage {ref}", self.transcript)

    def test_it_is_phrased_as_a_question_the_record_will_harvest(self) -> None:
        questions = questions_in(self.transcript)
        self.assertTrue(questions, "the record's own scan found nothing to file")
        for ref in self.refs:
            self.assertTrue(
                any(ref in q and "released into the record" in q for q in questions),
                f"the record would not carry a question about {ref}: {questions}",
            )

    def test_the_harvested_question_carries_the_date_and_the_kind(self) -> None:
        harvested = [q for q in questions_in(self.transcript) if "released into the record" in q]
        self.assertTrue(harvested)
        for question in harvested:
            self.assertIn("24 Aug 2026", question)
        # The sentence before it is the one that names the kind, and both are in the file.
        self.assertRegex(self.transcript, r"\[held [0-9A-F]{6}\] [A-Z][^.]*was recorded on 24 Aug 2026")

    def test_the_marker_carries_none_of_the_words_it_replaced(self) -> None:
        markers = re.findall(r"\[held [0-9A-F]{6}\][^\n]*", self.transcript)
        self.assertTrue(markers)
        self.assert_absent("\n".join(markers), HELD_WORDS, "the markers themselves")

    def test_it_survives_the_records_real_parser(self) -> None:
        # Written to disk under the real name and parsed by the record's own code.
        name = self.drive.names[0]
        path = os.path.join(self.recording.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.transcript)
        parsed = vendored_ingest.parse_texty(path)
        self.assertEqual(parsed["kind"], "transcript")
        self.assertEqual(parsed["from_addr"], "")
        body = parsed["body"]
        for ref in self.refs:
            self.assertIn(f"[held {ref}]", body, "the marker was swallowed by the header block")
        self.assertTrue(
            [q for q in questions_in(body) if "released into the record" in q],
            "the marker did not survive into the body the record reads",
        )

    def test_all_three_files_say_a_passage_is_being_held(self) -> None:
        for kind in ("transcript", "summary", "actions"):
            self.assertIn("held", self.drive.text_of(kind).lower())


# =========================================================================== 5. prices


class PricesFlow(_GateCase):
    """His decision, taken against his own instinct, on a number he had measured.

    21% of his record's content lines mention money and 6.3% carry a rand figure. Holding
    prices is ten to fifteen approvals every day, and a gate he stops opening does not fail
    safely — it silently swallows the record. So a price flows and is labelled, and only his
    own cost set against his own charge, in one breath, is held.

    This is the assertion most likely to be quietly walked back by somebody tightening the
    classifier, so it is made on the rendered files with his own two examples.
    """

    def setUp(self) -> None:
        self.recording = Recording("on")
        self.addCleanup(self.recording.close)
        self.drive = self.recording.run()
        self.transcript = self.drive.text_of("transcript")

    def test_a_price_quoted_to_a_client_reaches_the_record(self) -> None:
        self.assertIn("R4,500 for the waterproofing", self.transcript)
        self.assertIn("Reno", self.transcript)

    def test_a_supplier_rate_reaches_the_record(self) -> None:
        self.assertIn("R820 a square for the screed", self.transcript)

    def test_an_invoice_number_is_not_a_bank_account(self) -> None:
        self.assertIn("4501234567890", self.transcript)

    def test_our_own_cost_against_our_own_charge_is_held(self) -> None:
        self.assertNotIn("R1.65m", self.transcript)
        self.assertNotIn("R1.604m", self.transcript)
        self.assertIn("A cost set against a charge was recorded", self.transcript)
        with self.recording.store() as store:
            categories = {r.category for r in store.for_recording(self.recording.item_id)}
        self.assertIn("own_margin", categories)

    def test_neither_price_is_on_anybodys_review_list(self) -> None:
        with self.recording.store() as store:
            queued = "\n".join(r.text for r in store.queue_for(JAMES))
        self.assertNotIn("R4,500", queued)
        self.assertNotIn("R820", queued)
        self.assertIn("R1.65m", queued)

    def test_the_price_is_labelled_so_the_outbound_check_can_read_it(self) -> None:
        # A label nothing reads is decoration, so the label has to exist to be read.
        labelled = self.recording.report().labelled()
        kinds = {f.category for f in labelled}
        self.assertIn("commercial_figure", kinds)
        self.assertTrue(all(not f.held for f in labelled))

    def test_an_ordinary_site_call_holds_nothing_at_all(self) -> None:
        # The answer most of the time. Treating ordinary site talk as sensitive buries the
        # few items that matter under the many that do not.
        plain = Recording(
            "on",
            text=(
                "Morning. I'm at Beach Court. The bricks came Thursday, Carel has the "
                "scaffold up at unit four and the screed is R820 a square from Marius. "
                "Slab pour is Monday if the weather holds. The waterproofing to the north "
                "elevation is R4,500 and I've told Reno that."
            ),
            model={"sensitive_passages": []},
            quotes=("The bricks came Thursday",),
            summary="An ordinary walk of Beach Court.",
        )
        self.addCleanup(plain.close)
        drive = plain.run()
        self.assertEqual(redact.refs_in(drive.all_text), ())
        with plain.store() as store:
            self.assertEqual(store.queue_for(JAMES), ())


# =========================================================================== 6. don't write this down


class DoNotWriteThisDownHolds(_GateCase):
    """A person's own instruction about their own words, in any language, with no model.

    It is held by a mechanical rule rather than a judgement, it is never downgraded for want
    of confidence, and it runs on every recording — including the short ones the router
    never sends to the strong model, which is exactly where this gets said.
    """

    def _run(self, text: str, quote: str) -> _Drive:
        recording = Recording(
            "on",
            text=text,
            model=None,                     # the model was never asked, or never answered
            quotes=(quote,),
            summary="A short call.",
        )
        self.addCleanup(recording.close)
        self.recording = recording
        return recording.run()

    def test_in_english(self) -> None:
        drive = self._run(
            "Morning, I'm at Beach Court. The bricks land Thursday and Carel has the "
            "scaffold up at unit four. Right. Don't write this down, the client is going "
            "to sue the plumber over the geyser. Anyway. The screed is R820 a square from "
            "Marius and the slab pour is Monday.",
            "The bricks land Thursday",
        )
        transcript = drive.text_of("transcript")
        self.assertNotIn("going to sue the plumber", transcript)
        self.assertIn("Something a person asked not be written down", transcript)
        # And the ordinary site talk either side of it is untouched.
        self.assertIn("The bricks land Thursday", transcript)
        self.assertIn("R820 a square", transcript)

    def test_in_afrikaans(self) -> None:
        drive = self._run(
            "Môre, ek is by Beach Court. Die stene kom Donderdag en Carel het die steier "
            "op by eenheid vier. Ja. Moenie dit neerskryf nie, die kliënt gaan die "
            "loodgieter dagvaar oor die geiser. Nou ja. Die screed is R820 'n vierkant "
            "van Marius af.",
            "Die stene kom Donderdag",
        )
        transcript = drive.text_of("transcript")
        self.assertNotIn("loodgieter dagvaar", transcript)
        self.assertIn("Something a person asked not be written down", transcript)
        self.assertIn("Die stene kom Donderdag", transcript)
        self.assertIn("R820", transcript)

    def test_it_holds_the_words_it_is_about_and_not_only_the_phrase(self) -> None:
        # Holding the instruction alone would withhold nothing at all and still cost an
        # approval, which is the worst of both.
        self._run(
            "We were at unit four. Right. Don't write this down, he is being paid cash on "
            "the side. Anyway. The bricks land Thursday.",
            "The bricks land Thursday",
        )
        with self.recording.store() as store:
            words = "\n".join(r.text for r in store.queue_for(JAMES))
        self.assertIn("paid cash on the side", words)

    def test_on_a_very_short_recording_it_can_take_the_whole_thing_and_says_so(self) -> None:
        # The instruction holds the sentence either side of itself, so on a three-sentence
        # voice note that is the whole recording. Nothing is released on account of that —
        # releasing on a threshold is the silent-emptying failure this design refuses — but
        # it is said out loud in the file a person reads, rather than left to be noticed.
        drive = self._run(
            "Quick one. Don't write this down, the insurer says admit nothing. That's all.",
            "the insurer says admit nothing",
        )
        report = self.recording.report()
        self.assertTrue(any("far more than a recording normally contains" in n for n in report.notes))
        self.assertIn("the classifier needs looking at", drive.text_of("transcript"))
        with self.recording.store() as store:
            self.assertEqual(len(store.queue_for(JAMES)), 1)

    def test_an_instruction_about_a_letter_is_not_one_about_the_record(self) -> None:
        # "Don't write to the trustees yet" is about correspondence. A rule that cannot tell
        # them apart holds a large part of an ordinary week.
        drive = self._run(
            "Don't write to the trustees yet, I want the report first. The bricks land "
            "Thursday.",
            "The bricks land Thursday",
        )
        self.assertEqual(redact.refs_in(drive.all_text), ())

    def test_it_is_held_even_though_the_model_never_answered(self) -> None:
        self._run(
            "Quick one. Don't write this down, the insurer has told us not to admit "
            "anything. That's all.",
            "the insurer has told us not to admit anything",
        )
        report = self.recording.report()
        self.assertFalse(report.model_answered)
        self.assertTrue(report.would_hold())
        self.assertTrue(any("did not answer" in note for note in report.notes))

# =========================================================================== the estate

#: A staff member's own recording. One passage that is theirs to answer — a colleague's
#: family circumstances — and one that is his, because a disciplinary matter is genuinely
#: his to hold whoever recorded the call.
STAFF_TEXT = (
    "Site meeting at Reno, Tuesday. Slab pour went fine and the scaffold comes down "
    "Friday. Pieter is booked off until the fifteenth after his back operation. "
    "Marius has his disciplinary hearing on Friday and he will probably be dismissed. "
    "The screed is R820 a square from Marius."
)
STAFF_MODEL = {
    "sensitive_passages": [
        {
            "category": "personal_circumstances",
            "quote": "is booked off until the fifteenth after his back operation",
            "confidence": 0.95,
            "what_it_is": "somebody's health",
            "reason": "a named person's own health",
        },
        {
            "category": "staff_matter",
            "quote": "has his disciplinary hearing on Friday and he will probably be dismissed",
            "confidence": 0.95,
            "what_it_is": "a disciplinary matter",
            "reason": "a hearing and a likely dismissal",
        },
    ]
}
#: The words on the staff member's own passage, and nowhere else in this deployment. James
#: may never see any of them.
THABOS_WORDS = ("back operation", "booked off until the fifteenth")


class _Estate:
    """One deployment: his recording, a staff member's recording, one store, one drive."""

    def __init__(self) -> None:
        self.james = Recording(
            "on",
            route=CALLS,
            reviewers={"site-meetings": THABO},
            item_id="REC-JAMES",
        )
        self.james.run()
        self.staff = Recording(
            "on",
            route=SITE_MEETINGS,
            borrow=self.james,
            item_id="REC-STAFF",
            text=STAFF_TEXT,
            model=STAFF_MODEL,
            quotes=("Slab pour went fine",),
            summary="A site meeting at Reno.",
            site="Reno Body Corporate",
        )
        self.staff.run()
        self.config = self.james.config
        self.ledger = self.james.ledger
        self.drive = self.james.drive
        self.store = WithheldStore(self.config.held_store_path)
        # What the pipeline records when a real run reaches DONE, done here because these
        # tests drive the publish step directly rather than the whole state machine.
        self.ledger.advance(
            "REC-JAMES", "DONE", transcript_name=self._transcript_name("REC-JAMES")
        )
        self.ledger.advance(
            "REC-STAFF", "DONE", transcript_name=self._transcript_name("REC-STAFF")
        )
        self.tokens = review_server.TokenStore(
            review_server.TokenStore.path_beside(self.config.held_store_path)
        )

    def _transcript_name(self, item_id: str) -> str:
        for _parent, name, text in self.drive.written:
            if "-summary" in name or "-actions" in name:
                continue
            if item_id in text:
                return name
        raise AssertionError(f"no transcript was written for {item_id}")

    def service(self, **kw: Any) -> review_server.ReviewService:
        options: dict[str, Any] = {
            "principal": JAMES,
            "mode": "on",
            "undo_seconds": 0,
            "route_labels": {"calls": "Phone calls", "site-meetings": "Site meetings"},
        }
        options.update(kw)
        return review_server.ReviewService(self.store, self.tokens, **options)

    def page_bytes(self, service: review_server.ReviewService, reviewer: str) -> str:
        issued = self.tokens.issue(reviewer)
        session = self.tokens.verify(issued.token, principal=JAMES)
        assert session is not None
        return review_page.render(service.page_for(session), nonce="test-nonce")

    def holds_for(self, reviewer: str) -> tuple[Any, ...]:
        return self.store.queue_for(reviewer)

    def close(self) -> None:
        self.store.close()
        self.tokens.close()
        self.james.close()


# =========================================================================== 7. whose words


class EachPersonSeesOnlyTheirOwn(_GateCase):
    """Decision 6, asserted on the bytes rather than on the intention.

    Staff record voluntarily and choose whether to keep a folder at all. If they work out
    that he reads the held text from their calls, the rational response is to stop keeping
    a folder — and then the recordings are gone. That is the original loss failure arriving
    as a social effect, and it is not fixable in code afterwards. So the test is on what the
    browser would actually receive, not on which query was called.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.estate = _Estate()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.estate.close()

    def setUp(self) -> None:
        self.service = self.estate.service()
        self.his = self.estate.page_bytes(self.service, JAMES)
        self.theirs = self.estate.page_bytes(self.service, THABO)

    def test_the_staff_members_passage_is_on_the_staff_members_page(self) -> None:
        self.assert_present(self.theirs, THABOS_WORDS, "the staff member's own page")

    def test_and_not_one_word_of_it_is_in_his(self) -> None:
        self.assert_absent(self.his, THABOS_WORDS, "James's page")

    def test_nor_the_classifiers_own_description_of_it(self) -> None:
        # The reason and the public phrase paraphrase the passage, so they are as
        # disclosing as the words and are equally not his to read.
        self.assertNotIn("own health", self.his)
        self.assertNotIn("back operation", self.his)

    def test_nor_in_the_surround_of_a_passage_that_is_his(self) -> None:
        # The disciplinary matter is his to read, and it was said one sentence after the
        # staff member's own. The context window that makes it answerable in seconds runs
        # straight across the other passage — so the other passage is shown as its own
        # reference, the same thing the transcript says in the same place, and not as words.
        his_staff_matter = [
            record for record in self.estate.holds_for(JAMES)
            if record.item_id == "REC-STAFF"
        ]
        self.assertEqual(len(his_staff_matter), 1)
        surround = his_staff_matter[0].context_before + his_staff_matter[0].context_after
        self.assert_absent(surround, THABOS_WORDS, "the context stored against his passage")
        self.assertRegex(surround, r"\[held [0-9A-F]{6}\]")
        self.assertIn("[held ", self.his)

    def test_he_is_told_how_many_and_where_though(self) -> None:
        # The count, the sites and how old the oldest is. Never a word of any of it.
        self.assertIn("Sites with something waiting", self.his)
        self.assertIn("Reno Body Corporate", self.his)
        self.assertIn("thabo", self.his.lower())

    def test_the_disciplinary_matter_is_his_whoever_recorded_it(self) -> None:
        # The one exception he named. It is on his page, in full, from a recording that is
        # not his.
        self.assertIn("disciplinary hearing on Friday", self.his)
        self.assertNotIn("disciplinary hearing on Friday", self.theirs)

    def test_a_staff_member_is_shown_nothing_at_all_about_anybody_else(self) -> None:
        self.assertNotIn("Sites with something waiting", self.theirs)
        self.assert_absent(self.theirs, HELD_WORDS[:4], "the staff member's page")

    def test_neither_page_types_an_email_address(self) -> None:
        # Checked with the record's own address pattern, because the house rule is the
        # record's: never type an address anywhere, for any reason.
        for rendered, whose in ((self.his, "his page"), (self.theirs, "theirs")):
            self.assertEqual(vendored_ingest.ADDR_RE.findall(rendered), [], whose)

    def test_answering_somebody_elses_passage_is_refused(self) -> None:
        issued = self.estate.tokens.issue(JAMES)
        session = self.estate.tokens.verify(issued.token, principal=JAMES)
        assert session is not None
        theirs = self.estate.holds_for(THABO)[0]
        outcome = self.service.answer(session, theirs.hold_id, "release")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "refused-scope")
        self.assertEqual(self.estate.store.get(theirs.hold_id).decision, Decision.PENDING)

    def test_the_scoping_is_in_the_query_and_not_in_the_template(self) -> None:
        # ``queue_for`` is the only method that returns words and it cannot be asked for
        # everybody. A page that filtered in the renderer would be one edit from a leak.
        with self.assertRaises(Exception):
            self.estate.store.queue_for("")


# =========================================================================== 8. the link


class TheLinkIsForOnePersonAndForToday(_GateCase):
    """A capability, not a password: it is good for one person, for one day, and expires."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.estate = _Estate()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.estate.close()

    def setUp(self) -> None:
        self.tokens = self.estate.tokens
        self.yesterday = _dt.datetime(2026, 8, 27, 5, 0, tzinfo=_dt.timezone.utc).timestamp()
        self.today = self.yesterday + 24 * 3600

    def test_yesterdays_link_no_longer_opens_the_page(self) -> None:
        issued = self.tokens.issue(JAMES, hours=12, now=self.yesterday)
        self.assertIsNotNone(self.tokens.verify(issued.token, now=self.yesterday + 60))
        self.assertIsNone(self.tokens.verify(issued.token, now=self.today))

    def test_this_mornings_link_kills_yesterdays_even_before_it_expires(self) -> None:
        old = self.tokens.issue(THABO, hours=48, now=self.yesterday)
        self.assertIsNotNone(self.tokens.verify(old.token, now=self.yesterday + 60))
        self.tokens.issue(THABO, hours=48, now=self.today)
        self.assertIsNone(self.tokens.verify(old.token, now=self.today + 60))

    def test_a_forged_verifier_against_a_real_selector_is_refused(self) -> None:
        issued = self.tokens.issue(JAMES)
        selector = issued.token.split(".", 1)[0]
        self.assertIsNone(self.tokens.verify(f"{selector}.not-the-right-half"))
        self.assertIsNone(self.tokens.verify(f"{selector}."))
        self.assertIsNone(self.tokens.verify(selector))

    def test_a_wholly_invented_token_is_refused_without_raising(self) -> None:
        for rubbish in ("", ".", "..", "a.b", "x" * 400, "sel.ver", "%00.%00"):
            self.assertIsNone(self.tokens.verify(rubbish))

    def test_the_secret_half_is_compared_in_constant_time(self) -> None:
        # ``==`` on a secret leaks its prefix a byte at a time to anybody who can time the
        # answer. The comparison is asserted rather than read: the call is counted.
        calls: list[tuple[str, str]] = []
        real = review_server.hmac.compare_digest

        def counted(left: Any, right: Any) -> bool:
            calls.append((str(left), str(right)))
            return real(left, right)

        review_server.hmac.compare_digest = counted  # type: ignore[assignment]
        self.addCleanup(setattr, review_server.hmac, "compare_digest", real)

        issued = self.tokens.issue(JAMES)
        self.assertIsNotNone(self.tokens.verify(issued.token))
        self.assertEqual(len(calls), 1)
        # Fixed-length digests on both sides, so the comparison itself carries no length.
        self.assertEqual(len(calls[0][0]), len(calls[0][1]))

    def test_an_unknown_selector_still_runs_the_comparison(self) -> None:
        # "No such token" and "wrong token" must not answer at measurably different speeds,
        # so the unknown selector is compared against a dummy digest rather than short-cut.
        calls: list[Any] = []
        real = review_server.hmac.compare_digest

        def counted(left: Any, right: Any) -> bool:
            calls.append((left, right))
            return real(left, right)

        review_server.hmac.compare_digest = counted  # type: ignore[assignment]
        self.addCleanup(setattr, review_server.hmac, "compare_digest", real)

        self.assertIsNone(self.tokens.verify("nosuchselector.nosuchverifier"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(str(calls[0][0])), len(str(calls[0][1])))

    def test_the_token_itself_is_never_stored(self) -> None:
        issued = self.tokens.issue(JAMES)
        with open(self.tokens.path, "rb") as handle:
            on_disk = handle.read()
        _selector, _, verifier = issued.token.partition(".")
        self.assertNotIn(verifier.encode(), on_disk)
        self.assertNotIn(issued.token.encode(), on_disk)

    def test_a_revoked_link_stops_working_at_once(self) -> None:
        issued = self.tokens.issue(JAMES)
        session = self.tokens.verify(issued.token)
        assert session is not None
        self.assertTrue(self.tokens.revoke(session.selector))
        self.assertIsNone(self.tokens.verify(issued.token))

# =========================================================================== 9. nothing drains itself


class NothingReleasesItself(_GateCase):
    """Decision 4, which is the one that makes the queue load-bearing state.

    Nothing is decided for him on a timer, ever. There is no deadline, no daily cap that
    commits the overflow unasked and no rule that writes itself, and the reason is a count:
    in the design proposals five paths defaulted to committing and none to withholding, so
    the thing that would silently empty under fatigue was not the record but the *gate*,
    while still presenting itself as one.

    Ageing is simulated by writing the hold thirty days back and asking every question the
    service asks about it. It has to still be waiting, still be surfaced, and still be
    nobody's but his to answer.
    """

    def setUp(self) -> None:
        self.estate = _Estate()
        self.addCleanup(self.estate.close)
        self.store = self.estate.store
        self.thirty_days_ago = "2026-07-29T09:00:00Z"
        self.now = "2026-08-28T09:00:00Z"
        with self.store._tx() as tx:  # the only way to age a row without inventing a setter
            tx.execute("UPDATE holds SET held_at=?", (self.thirty_days_ago,))

    def _pending(self) -> tuple[Any, ...]:
        return self.store.queue_for(JAMES)

    def test_it_is_still_pending_after_thirty_days(self) -> None:
        records = self._pending()
        self.assertTrue(records)
        for record in records:
            self.assertEqual(record.decision, Decision.PENDING)
            self.assertTrue(record.pending)
            self.assertEqual(record.age_days(self.now), 30)

    def test_it_was_not_discarded_either(self) -> None:
        for record in self._pending():
            self.assertTrue(record.text, "the only copy of these words outside the audio")

    def test_it_is_louder_rather_than_quieter_as_it_ages(self) -> None:
        overview = self.store.overview(now=self.now)
        self.assertEqual(overview["oldest_age_days"], 30)
        self.assertGreater(overview["count"], 0)
        self.assertIsNotNone(overview["oldest"])
        self.assertNotIn("text", overview["oldest"])

    def test_the_page_still_shows_it_and_still_asks(self) -> None:
        rendered = self.estate.page_bytes(self.estate.service(), JAMES)
        self.assertIn("waiting 30 days", rendered)
        self.assertIn(review_page.RELEASE_LABEL, rendered)
        self.assertIn(review_page.REFUSE_LABEL, rendered)
        self.assertIn("Nothing here is decided by a timer", rendered)

    def test_running_every_scheduled_thing_the_service_has_changes_nothing(self) -> None:
        before = {r.hold_id: r.decision for r in self._pending()}
        service = self.estate.service()
        for _ in range(3):
            service.commit_due(force=True)
        releaser = release.Releaser(self.estate.config, self.estate.ledger, self.store)
        # The one method whose name sounds like it might drain the queue. It delivers files
        # for passages a person already released, and there are none.
        self.assertEqual(releaser.deliver_outstanding(), ())
        after = {r.hold_id: r.decision for r in self._pending()}
        self.assertEqual(before, after)

    def test_there_is_no_expiry_column_to_age_against(self) -> None:
        columns = {
            row["name"]
            for row in self.store._conn().execute("PRAGMA table_info(holds)")
        }
        for absent in ("expires_at", "deadline", "auto_release_at", "release_by", "ttl"):
            self.assertNotIn(absent, columns)

    def test_and_nothing_can_answer_in_a_machines_name(self) -> None:
        record = self._pending()[0]
        for machine in ("auto", "system", "timer", "scheduler", "transcriber", "gate", ""):
            with self.assertRaises(Exception):
                self.store.release(record.hold_id, answered_by=machine)
        self.assertEqual(self.store.get(record.hold_id).decision, Decision.PENDING)


# =========================================================================== 10. idempotent


class ADecisionIsIdempotent(_GateCase):
    """The same approval twice releases once. He is on a roof with one bar of signal."""

    def setUp(self) -> None:
        self.estate = _Estate()
        self.addCleanup(self.estate.close)
        self.deliveries: list[Any] = []
        self.releaser = release.Releaser(
            self.estate.config, self.estate.ledger, self.estate.store, self.estate.drive
        )
        self.service = self.estate.service(
            undo_seconds=0, on_decision=self._deliver
        )
        issued = self.estate.tokens.issue(JAMES)
        session = self.estate.tokens.verify(issued.token, principal=JAMES)
        assert session is not None
        self.session = session
        self.record = [
            r for r in self.estate.holds_for(JAMES) if r.item_id == "REC-JAMES"
        ][0]
        self.before = len(self.estate.drive.written)

    def _deliver(self, record: Any) -> None:
        self.deliveries.append(self.releaser.on_decision(record))

    def test_the_second_tap_is_not_an_error(self) -> None:
        first = self.service.answer(self.session, self.record.hold_id, "release")
        second = self.service.answer(self.session, self.record.hold_id, "release")
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.state, "already")

    def test_and_the_words_are_released_exactly_once(self) -> None:
        self.service.answer(self.session, self.record.hold_id, "release")
        self.service.answer(self.session, self.record.hold_id, "release")
        stored = self.estate.store.get(self.record.hold_id)
        self.assertEqual(stored.decision, Decision.RELEASED)
        self.assertEqual(stored.decisions_made, 1)

    def test_and_the_fourth_file_is_written_exactly_once(self) -> None:
        self.service.answer(self.session, self.record.hold_id, "release")
        self.service.answer(self.session, self.record.hold_id, "release")
        written = [n for _p, n, _t in self.estate.drive.written[self.before:]]
        self.assertEqual(len(written), 1, written)
        # And asking the releaser again does not write a second copy either.
        again = self.releaser.deliver(self.estate.store.get(self.record.hold_id))
        self.assertEqual(again.state, "already-there")
        self.assertEqual(len(self.estate.drive.written[self.before:]), 1)

    def test_the_history_keeps_both_taps(self) -> None:
        self.service.answer(self.session, self.record.hold_id, "release")
        self.service.answer(self.session, self.record.hold_id, "release")
        events = self.estate.store.history(self.record.hold_id)
        self.assertTrue(any(e["kind"] == Decision.RELEASED for e in events))
        self.assertTrue(all(e["actor"] != "agent" for e in events))

    def test_a_second_device_answering_the_other_way_is_a_conflict_not_an_overwrite(self) -> None:
        self.service.answer(self.session, self.record.hold_id, "release")
        outcome = self.service.answer(self.session, self.record.hold_id, "refuse")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.state, "conflict")
        self.assertEqual(
            self.estate.store.get(self.record.hold_id).decision, Decision.RELEASED
        )


# =========================================================================== 11. the store on disk


class TheStoreIsReadableByNobodyElse(_GateCase):
    """It is the single most revealing file this service writes, so it is checked on disk.

    Every held passage's full text lives here and, after the cut, nowhere else but the
    audio. SQLite writes ``-wal`` and ``-shm`` beside it carrying the same content, so all
    three are checked — a database at 0600 with its write-ahead log at 0644 is a database
    at 0644.
    """

    def setUp(self) -> None:
        self.estate = _Estate()
        self.addCleanup(self.estate.close)
        self.path = self.estate.config.held_store_path

    def _mode(self, suffix: str) -> int:
        return stat.S_IMODE(os.stat(self.path + suffix).st_mode)

    def test_the_database_itself(self) -> None:
        self.assertEqual(self._mode(""), 0o600, oct(self._mode("")))

    def test_the_write_ahead_log_beside_it(self) -> None:
        self.assertTrue(os.path.exists(self.path + "-wal"))
        self.assertEqual(self._mode("-wal"), 0o600, oct(self._mode("-wal")))

    def test_and_the_shared_memory_file(self) -> None:
        self.assertTrue(os.path.exists(self.path + "-shm"))
        self.assertEqual(self._mode("-shm"), 0o600, oct(self._mode("-shm")))

    def test_it_is_not_the_ledger_and_not_in_the_work_directory(self) -> None:
        self.assertNotEqual(self.path, self.estate.config.ledger_path)
        work = os.path.abspath(self.estate.config.work_dir)
        self.assertFalse(os.path.abspath(self.path).startswith(work + os.sep))

    def test_a_reopened_store_sets_them_again(self) -> None:
        # The permissions are re-applied on every connection, because a WAL deleted by a
        # checkpoint and recreated by the next write would otherwise come back at 0644.
        for suffix in ("", "-wal", "-shm"):
            try:
                os.chmod(self.path + suffix, 0o644)
            except FileNotFoundError:
                pass
        reopened = WithheldStore(self.path)
        self.addCleanup(reopened.close)
        reopened.stats()
        for suffix in ("", "-wal", "-shm"):
            self.assertEqual(self._mode(suffix), 0o600, suffix)

    def test_the_key_to_it_is_locked_down_the_same_way(self) -> None:
        # The token database is the key to the held text: a live token opens the page that
        # shows the words. Its own write-ahead log carries the same rows as the database.
        path = self.estate.tokens.path
        self.estate.tokens.issue(JAMES)
        for suffix in ("", "-wal", "-shm"):
            target = path + suffix
            if not os.path.exists(target):
                continue
            self.assertEqual(
                stat.S_IMODE(os.stat(target).st_mode), 0o600, os.path.basename(target)
            )


# =========================================================================== 12. the fourth file


class ReleaseWritesTheFourthFileAndARefusalWritesNothing(_GateCase):
    """He asked for a held passage to be released *in place*. This is the in-place part."""

    def setUp(self) -> None:
        self.estate = _Estate()
        self.addCleanup(self.estate.close)
        self.releaser = release.Releaser(
            self.estate.config, self.estate.ledger, self.estate.store, self.estate.drive
        )
        self.record = [
            r for r in self.estate.holds_for(JAMES)
            if r.item_id == "REC-JAMES" and r.category == "own_margin"
        ][0]
        self.transcript = self.estate.drive.text_of("transcript")
        self.before = len(self.estate.drive.written)

    def test_a_release_writes_one_more_file_carrying_the_words(self) -> None:
        released = self.estate.store.release(self.record.hold_id, answered_by="James Janeke")
        delivery = self.releaser.deliver(released)
        self.assertTrue(delivery.ok)
        self.assertEqual(delivery.state, "written")
        written = self.estate.drive.written[self.before:]
        self.assertEqual(len(written), 1)
        _parent, name, text = written[0]
        self.assertIn(self.record.ref, name)
        self.assertIn("R1.65m", text)

    def test_it_goes_to_the_folder_that_recordings_three_files_went_to(self) -> None:
        released = self.estate.store.release(self.record.hold_id, answered_by="James Janeke")
        self.releaser.deliver(released)
        parent, _name, _text = self.estate.drive.written[self.before]
        self.assertEqual(parent, CALLS.output_folder_id)

    def test_the_record_reads_it_as_a_transcript_and_not_as_an_email(self) -> None:
        released = self.estate.store.release(self.record.hold_id, answered_by="James Janeke")
        self.releaser.deliver(released)
        _parent, name, text = self.estate.drive.written[self.before]
        path = os.path.join(self.estate.james.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        parsed = vendored_ingest.parse_texty(path)
        self.assertEqual(parsed["kind"], "transcript")
        self.assertIn("R1.65m", parsed["body"])
        self.assertEqual(vendored_ingest.ADDR_RE.findall(text), [])

    def test_it_carries_no_other_passage_that_is_still_held(self) -> None:
        released = self.estate.store.release(self.record.hold_id, answered_by="James Janeke")
        self.releaser.deliver(released)
        _parent, _name, text = self.estate.drive.written[self.before]
        self.assert_absent(
            text,
            ("disciplinary hearing on Friday", "8001015009087", "caused the crack"),
            "the release file",
        )

    def test_a_refusal_writes_nothing_at_all(self) -> None:
        refused = self.estate.store.refuse(self.record.hold_id, answered_by="James Janeke")
        delivery = self.releaser.deliver(refused)
        self.assertTrue(delivery.ok)
        self.assertEqual(delivery.state, "nothing-to-write")
        self.assertEqual(self.estate.drive.written[self.before:], [])

    def test_and_the_marker_stays_standing_in_the_transcript(self) -> None:
        self.estate.store.refuse(self.record.hold_id, answered_by="James Janeke")
        self.releaser.deliver(self.estate.store.get(self.record.hold_id))
        # The published transcript is untouched by a refusal: absence is itself a record,
        # and a marker tidied away on a refusal would read like a recording where nothing
        # was said, which is the opposite of the truth.
        self.assertIn(f"[held {self.record.ref}]", self.transcript)
        self.assertEqual(
            self.estate.drive.text_of("transcript"), self.transcript
        )

    def test_a_refusal_is_kept_as_a_refusal_rather_than_deleted(self) -> None:
        self.estate.store.refuse(self.record.hold_id, answered_by="James Janeke")
        stored = self.estate.store.get(self.record.hold_id)
        self.assertEqual(stored.decision, Decision.REFUSED)
        self.assertTrue(stored.text)
        self.assertEqual(stored.answered_by, "James Janeke")

    def test_an_unanswered_passage_has_no_file_and_says_why(self) -> None:
        delivery = self.releaser.deliver(self.record)
        self.assertFalse(delivery.ok)
        self.assertEqual(delivery.state, "refused-to-write")
        self.assertEqual(self.estate.drive.written[self.before:], [])
        self.assertIn("because a person read it and said so", delivery.detail)

    def test_a_release_the_drive_would_not_take_is_owed_rather_than_lost(self) -> None:
        released = self.estate.store.release(self.record.hold_id, answered_by="James Janeke")
        self.estate.drive.refuse = "-released-"
        delivery = self.releaser.deliver_quietly(released)
        self.assertFalse(delivery.ok)
        owed = self.releaser.outstanding()
        self.assertTrue(owed.any)
        self.assert_absent("\n".join(owed.lines()), ("R1.65m", "R1.604m"), "the owed list")
        # And it finishes the job when the drive comes back.
        self.estate.drive.refuse = ""
        again = self.releaser.deliver_outstanding()
        self.assertTrue(again[0].ok)
        self.assertFalse(self.releaser.outstanding().any)


# =========================================================================== the surround itself


class _Finding:
    """The shape :mod:`transcriber.sensitivity` hands to the store, and nothing more."""

    def __init__(self, start: int, end: int, text: str, category: str = "staff_matter") -> None:
        self.start = start
        self.end = end
        self.text = text
        self.category = category
        self.held = True
        self.subject = ""
        self.reason = ""
        self.confidence = 0.9


class TheSurroundOfOneHoldNeverCarriesAnother(_GateCase):
    """The adapter that cuts the context, on its own, at the awkward offsets.

    Two passages of one recording can be a sentence apart and belong to two people. The
    context window is what makes a passage answerable in seconds without opening the
    transcript, and it is also the one place in this design where one person's queue can
    reach into another's — so it is asserted here directly rather than only through a page.
    """

    def _spans(self, transcript: str, findings: list[_Finding], **kw: Any) -> tuple[Any, ...]:
        return held_spans_from(
            findings, item_id="R", transcript=transcript, principal=JAMES, **kw
        )

    def test_a_neighbouring_hold_is_shown_as_its_own_reference(self) -> None:
        text = "aaa SECRET-ONE bbb SECRET-TWO ccc"
        first, second = self._spans(
            text,
            [_Finding(4, 14, "SECRET-ONE"), _Finding(19, 29, "SECRET-TWO")],
        )
        self.assertNotIn("SECRET-TWO", first.context_before + first.context_after)
        self.assertNotIn("SECRET-ONE", second.context_before + second.context_after)
        self.assertIn(f"[held {second.ref}]", first.context_after)
        self.assertIn(f"[held {first.ref}]", second.context_before)

    def test_the_ordinary_words_between_them_are_kept(self) -> None:
        text = "aaa SECRET-ONE bbb SECRET-TWO ccc"
        first, _second = self._spans(
            text,
            [_Finding(4, 14, "SECRET-ONE"), _Finding(19, 29, "SECRET-TWO")],
        )
        self.assertIn("bbb", first.context_after)
        self.assertIn("ccc", first.context_after)
        self.assertIn("aaa", first.context_before)

    def test_a_hold_that_fills_the_whole_window_leaves_only_its_reference(self) -> None:
        text = "x" * 5 + "SECRET" + "y" * 5 + "OTHERSECRET"
        first, second = self._spans(
            text,
            [_Finding(5, 11, "SECRET"), _Finding(16, 27, "OTHERSECRET")],
            context_chars=11,
        )
        self.assertEqual(first.context_after, "yyyyy" + f"[held {second.ref}]")
        self.assertNotIn("OTHERSECRET", first.context_after)

    def test_a_hold_only_half_inside_the_window_is_still_taken_out(self) -> None:
        text = "SECRET" + "y" * 4 + "OTHERSECRET"
        first, second = self._spans(
            text,
            [_Finding(0, 6, "SECRET"), _Finding(10, 21, "OTHERSECRET")],
            context_chars=6,
        )
        self.assertNotIn("OTHER", first.context_after)
        self.assertIn(f"[held {second.ref}]", first.context_after)

    def test_the_same_words_held_twice_are_told_apart_by_where_they_are(self) -> None:
        text = "aaa SECRET bbb SECRET ccc"
        first, second = self._spans(
            text, [_Finding(4, 10, "SECRET"), _Finding(15, 21, "SECRET")]
        )
        # Different offsets, so different references — and each one's surround names the
        # other rather than repeating the words.
        self.assertNotEqual(first.ref, second.ref)
        self.assertNotIn("SECRET", first.context_after.replace(f"[held {second.ref}]", ""))
        self.assertNotIn("SECRET", second.context_before.replace(f"[held {first.ref}]", ""))

    def test_one_hold_on_its_own_keeps_its_surround_verbatim(self) -> None:
        text = "the bricks land Thursday SECRET and Carel has the scaffold up"
        (only,) = self._spans(text, [_Finding(25, 31, "SECRET")])
        self.assertEqual(only.context_before, "the bricks land Thursday ")
        self.assertEqual(only.context_after, " and Carel has the scaffold up")

    def test_the_reference_in_a_surround_is_the_one_the_store_actually_holds(self) -> None:
        # Otherwise the surround names a passage nobody can look up, which is worse than a
        # gap: it reads as a reference and answers to nothing.
        estate = _Estate()
        self.addCleanup(estate.close)
        known = {record.ref for record in estate.store.for_recording("REC-JAMES")}
        for record in estate.store.for_recording("REC-JAMES"):
            for ref in redact.refs_in(record.context_before + record.context_after):
                self.assertIn(ref, known)
