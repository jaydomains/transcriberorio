"""The sensitivity gate where it actually runs: the pipeline, and the three files.

``test_sensitivity_gate.py`` proves the classifier. This proves the *wiring* — the part that
decides whether a held passage ever reaches OneDrive, and the part that decides whether
holding one costs somebody their action items.

Six properties, and every one of them is something that went wrong somewhere in the
investigation before it went right:

* All three files are written, on time, in every mode. Only the words wait.
* ``shadow`` is byte-for-byte what ``off`` writes, and still produces the measurement.
* The cut lands on the transcript, and the held words are gone from all three files.
* Prices flow. A rand figure is in all three files, unmarked.
* Quote verification does not become a shredder: an item quoting a held passage keeps its
  place, carrying the same marker the transcript carries.
* Nothing is published on a doubt and nothing is withheld on one. A passage that could not
  be cut, or that survived into a rendered file, stops all three and goes to a person.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import unittest
from typing import Any

from tests import support
from transcriber import naming, outputs, redact
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, ExtractedItem, Route, Segment, State, Transcript
from transcriber.pipeline import Pipeline
from transcriber.withheld import Decision, WithheldStore

ROUTE = Route(
    name="calls",
    label="Phone calls",
    source_folder_id="S-CALLS",
    output_folder_id="O-CALLS",
    archive_folder_id="",
    engine="",
    enabled=True,
)

#: An ordinary site call with one thing in it that must not be written down. Everything else
#: is the traffic that has to keep flowing: a price, a supplier, a delivery, a named person
#: doing their job.
TEXT = (
    "Right, I'm at Beach Court now. Spoke to Carel about the roof leak at unit four. "
    "The new chap starts Monday, his ID number is 8001015009087 so put him on the site "
    "register. The remedial is coming in at R4,500 from Marius and the bricks land "
    "Thursday."
)
STRADDLES_THE_HOLD = "his ID number is 8001015009087 so put him on the site register"
CLEAR_OF_THE_HOLD = "The remedial is coming in at R4,500 from Marius"


class _Drive:
    """A OneDrive that remembers exactly what was written into it."""

    def __init__(self, fail_on: str = "") -> None:
        self.written: list[tuple[str, str, str]] = []
        self.items: dict[str, Any] = {}
        self.fail_on = fail_on

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        if self.fail_on and self.fail_on in name:
            raise RuntimeError("the drive refused this one")
        text = data.decode("utf-8")
        self.written.append((parent_id, name, text))
        item = type(
            "Item",
            (),
            {"id": f"out-{len(self.items)}", "name": name, "size": len(data), "web_url": ""},
        )()
        self.items[item.id] = item
        return item

    def get_item(self, item_id: str) -> Any:
        return self.items[item_id]

    def text_of(self, kind: str) -> str:
        for _parent, name, text in self.written:
            if kind == "transcript" and "-summary" not in name and "-actions" not in name:
                return text
            if kind != "transcript" and f"-{kind}" in name:
                return text
        raise AssertionError(f"no {kind} was written")

    @property
    def names(self) -> list[str]:
        return [name for _p, name, _t in self.written]


class _BrokenStore:
    """A held-passage store that cannot be written to at all."""

    def hold_many(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the disk is full")

    def record_pass(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the disk is full")


class _Run:
    """One recording taken from a transcript to three uploaded files."""

    def __init__(
        self,
        mode: str = "on",
        *,
        text: str = TEXT,
        summary: str = "He walked Beach Court; a new chap starts Monday.",
        quotes: tuple[str, ...] = (STRADDLES_THE_HOLD, CLEAR_OF_THE_HOLD),
        store: Any = None,
        drive: _Drive | None = None,
    ) -> None:
        self.dir = tempfile.mkdtemp()
        self.config = support.make_config(
            routes=(ROUTE,),
            work_dir=os.path.join(self.dir, "work"),
            ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
            gate_mode=mode,
            gate_held_store=os.path.join(self.dir, "held.sqlite3"),
            gate_review_base_url="https://review.invalid/held",
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.drive = drive or _Drive()
        self.pipeline = Pipeline(self.config, self.ledger, self.drive, withheld=store)
        self.ledger.record_page(
            [
                DriveItem(
                    item_id="C1",
                    name="Call Carel_260827_120055.m4a",
                    size=4096,
                    etag='"C1"',
                    created_at="2026-08-27T09:00:00Z",
                )
            ],
            "cursor-1",
            route="calls",
        )
        self.row = self.ledger.get("C1")
        assert self.row is not None
        self.transcript = Transcript(
            text=text,
            segments=[Segment(0.0, 30.0, "James", text)],
            language="en-ZA",
            engine="test-engine",
        )
        self.extraction = support.StubExtraction(
            summary=summary,
            proposals=[
                support.StubProposal(
                    "commitments",
                    ExtractedItem(
                        kind="commitment",
                        text=f"Something was said about this: {index}",
                        quote=quote,
                        quote_verified=True,
                    ),
                )
                for index, quote in enumerate(quotes)
            ],
        )

    def classify(self) -> Any:
        report = self.pipeline._assess(self.row, self.transcript)
        return self.pipeline._assess(self.row, self.transcript, self.extraction, standing=report)

    def gate(self) -> Any:
        return self.pipeline._withhold(
            self.row, ROUTE, self.transcript, self.extraction, self.classify()
        )

    def publish(self, gate: Any = None) -> Any:
        held = gate if gate is not None else self.gate()
        return self.pipeline._publish(
            self.row,
            naming.parse_source_name(self.row.name),
            held.transcript,
            held.extraction,
            support.audio_info(120.0),
            ROUTE,
            notes=held.notes,
            held=held.held,
        )

    def store(self) -> WithheldStore:
        return WithheldStore(self.config.gate_held_store)

    def close(self) -> None:
        self.ledger.close()


class AllThreeFilesAreWrittenOnTimeInEveryMode(unittest.TestCase):
    """Only the words wait, never the recording. That is what makes an open hold safe."""

    def _publish_in(self, mode: str) -> _Drive:
        run = _Run(mode)
        self.addCleanup(run.close)
        run.publish()
        return run.drive

    def test_off(self) -> None:
        self.assertEqual(len(self._publish_in("off").written), 3)

    def test_shadow(self) -> None:
        self.assertEqual(len(self._publish_in("shadow").written), 3)

    def test_on_with_a_passage_actually_held(self) -> None:
        drive = self._publish_in("on")
        self.assertEqual(len(drive.written), 3, "a held passage must not delay a recording")
        self.assertEqual(
            len({name for name in drive.names}), 3, "three distinct files, every time"
        )

    def test_the_transcript_is_still_the_first_file_on_the_wire(self) -> None:
        drive = self._publish_in("on")
        self.assertNotIn("-summary", drive.names[0])
        self.assertNotIn("-actions", drive.names[0])


class ItShipsDark(unittest.TestCase):
    """Shadow classifies everything, withholds nothing, and changes nothing else."""

    def test_shadow_writes_byte_for_byte_what_off_writes(self) -> None:
        off = _Run("off")
        self.addCleanup(off.close)
        shadow = _Run("shadow")
        self.addCleanup(shadow.close)

        off.publish()
        shadow.publish()

        self.assertEqual(
            [text for _p, _n, text in off.drive.written],
            [text for _p, _n, text in shadow.drive.written],
            "shadow mode changed a published file; it may only measure",
        )

    def test_shadow_still_records_what_it_would_have_held(self) -> None:
        run = _Run("shadow")
        self.addCleanup(run.close)

        gate = run.gate()

        self.assertTrue(gate.report.would_hold(), "shadow must still classify")
        self.assertEqual(gate.held, (), "shadow may not cut anything out")
        self.assertEqual(gate.transcript.text, TEXT)
        held = run.store().for_recording("C1")
        self.assertTrue(held, "shadow's whole point is recording what it would have held")
        self.assertEqual(
            {record.decision for record in held},
            {Decision.NOT_WITHHELD},
            "a shadow row is not somebody's pending approval",
        )

    def test_shadow_produces_the_measurement_the_arming_decision_needs(self) -> None:
        run = _Run("shadow")
        self.addCleanup(run.close)

        run.gate()

        numbers = run.store().measurement()
        self.assertEqual(numbers["recordings_classified"], 1)
        self.assertEqual(numbers["recordings_with_a_hold"], 1)
        self.assertGreater(numbers["characters_read"], 0, "there is no fraction without one")
        self.assertLess(numbers["fraction_of_text"], 1.0)

    def test_a_recording_counted_once_is_not_counted_again_when_it_retries(self) -> None:
        """The denominator decides whether the gate may be armed. It must not drift."""
        run = _Run("shadow")
        self.addCleanup(run.close)
        first = run.gate()
        run.ledger.set_fields("C1", meta={"sensitivity": first.note})
        run.row = run.ledger.get("C1")

        second = run.gate()

        self.assertTrue(first.measured)
        self.assertFalse(second.measured, "a retried recording was counted twice")
        self.assertEqual(run.store().measurement()["recordings_classified"], 1)

    def test_off_reads_nothing_and_stores_nothing(self) -> None:
        run = _Run("off")
        self.addCleanup(run.close)

        gate = run.gate()

        self.assertEqual(gate.report.findings, ())
        self.assertEqual(gate.held, ())
        self.assertEqual(gate.note, {}, "a gate that was never asked must not look like one that found nothing")
        self.assertFalse(os.path.exists(run.config.gate_held_store), "off may not even open the store")


class TheCutLandsOnTheTranscript(unittest.TestCase):
    """A redaction in the actions file is not a redaction: only the transcript is ingested."""

    def setUp(self) -> None:
        self.run = _Run("on")
        self.addCleanup(self.run.close)
        self.gate = self.run.gate()
        self.run.publish(self.gate)

    def test_the_held_words_are_not_in_the_transcript(self) -> None:
        self.assertNotIn("8001015009087", self.run.drive.text_of("transcript"))

    def test_the_held_words_are_in_none_of_the_three_files(self) -> None:
        for _parent, name, text in self.run.drive.written:
            self.assertNotIn("8001015009087", text, f"{name} carries a held passage")

    def test_the_speaker_labelled_body_was_cut_too(self) -> None:
        """The body is rendered from the segments when the engine returned them."""
        self.assertTrue(self.gate.transcript.segments)
        for segment in self.gate.transcript.segments:
            self.assertNotIn("8001015009087", segment.text)

    def test_the_words_are_in_the_store_before_they_leave_the_transcript(self) -> None:
        held = self.run.store().for_recording("C1")
        self.assertEqual(len(held), 1)
        self.assertIn("8001015009087", held[0].text)
        self.assertEqual(held[0].decision, Decision.PENDING)

    def test_nothing_is_decided_for_him(self) -> None:
        record = self.run.store().for_recording("C1")[0]
        self.assertEqual(record.decision, Decision.PENDING)
        self.assertEqual(record.answered_by, "", "no machine may answer a hold")


class TheMarkerIsAStatedUnknown(unittest.TestCase):
    """A hole that says nothing is indistinguishable from a call in which nothing was said."""

    def setUp(self) -> None:
        self.run = _Run("on")
        self.addCleanup(self.run.close)
        self.gate = self.run.gate()
        self.run.publish(self.gate)
        self.transcript = self.run.drive.text_of("transcript")

    def test_the_marker_is_where_the_words_were(self) -> None:
        self.assertIn(f"[held {self.gate.held[0].ref}]", self.transcript)

    def test_the_records_own_question_scan_would_lift_it_out(self) -> None:
        marker = redact.marker_for(self.gate.held[0])
        self.assertTrue(
            redact.harvestable(marker),
            "the marker must reach the site's live page, or the record answers 'no record'",
        )
        self.assertIn(redact.harvestable(marker), self.transcript)

    def test_all_three_files_say_a_passage_is_held(self) -> None:
        for kind in ("transcript", "summary", "actions"):
            self.assertIn(
                "Passages held for review",
                self.run.drive.text_of(kind),
                f"the {kind} file claims to be complete and is not",
            )

    def test_the_line_that_says_so_carries_no_words_of_it(self) -> None:
        for _parent, _name, text in self.run.drive.written:
            for line in text.split("\n"):
                if line.startswith("- Passages held for review"):
                    self.assertNotIn("8001015009087", line)
                    self.assertIn(self.gate.held[0].ref, line, "a person needs the reference")


class PricesFlow(unittest.TestCase):
    """He decided this against his own first instinct, on the measurement. Do not widen it."""

    def test_a_rand_figure_and_a_supplier_reach_all_three_files(self) -> None:
        run = _Run("on")
        self.addCleanup(run.close)

        run.publish()

        for kind in ("transcript", "summary", "actions"):
            text = run.drive.text_of(kind)
            if kind == "transcript":
                self.assertIn("R4,500", text)
                self.assertIn("Marius", text)
        self.assertIn("R4,500", run.drive.text_of("transcript"))

    def test_an_ordinary_site_call_holds_nothing_at_all(self) -> None:
        """This has to be the answer most of the time, or the queue buries what matters."""
        ordinary = (
            "I'm at Beach Court. The bricks land Thursday, Marius is quoting R18,000 for "
            "the remedial and Carel is fixing the roof leak at unit four on Monday."
        )
        run = _Run("on", text=ordinary, quotes=("The bricks land Thursday",))
        self.addCleanup(run.close)

        gate = run.gate()

        self.assertEqual(gate.held, (), "ordinary site talk was treated as sensitive")
        self.assertEqual(gate.transcript.text, ordinary)


class QuoteVerificationDoesNotBecomeAShredder(unittest.TestCase):
    """A redaction that silently destroys action items is the bug this ordering exists for."""

    def setUp(self) -> None:
        self.run = _Run("on")
        self.addCleanup(self.run.close)
        self.gate = self.run.gate()
        self.run.publish(self.gate)

    def test_an_item_whose_quote_straddles_a_hold_is_kept(self) -> None:
        quotes = [p.item.quote for p in self.gate.extraction.proposals]
        self.assertEqual(len(quotes), 2, "an action item was discarded by the redaction")

    def test_its_quote_carries_the_same_marker_the_transcript_carries(self) -> None:
        straddling = [q for q in (p.item.quote for p in self.gate.extraction.proposals)
                      if "site register" in q]
        self.assertEqual(len(straddling), 1)
        self.assertIn(f"[held {self.gate.held[0].ref}]", straddling[0])
        self.assertNotIn("8001015009087", straddling[0])

    def test_the_item_clear_of_the_hold_is_untouched(self) -> None:
        self.assertIn(
            CLEAR_OF_THE_HOLD,
            [p.item.quote for p in self.gate.extraction.proposals],
            "an item nowhere near a held passage was rewritten",
        )

    def test_the_rewritten_quote_still_passes_the_render_time_check(self) -> None:
        """It passes because it is true, not because the check was loosened."""
        self.assertIn("site register", self.run.drive.text_of("actions"))

    def test_an_item_quoting_only_held_words_is_held_and_named_not_dropped(self) -> None:
        run = _Run("on", quotes=("8001015009087",))
        self.addCleanup(run.close)

        gate = run.gate()
        run.publish(gate)

        self.assertEqual(len(gate.extraction.proposals), 0)
        notes = " ".join(gate.extraction.notes)
        self.assertIn("held pending review", notes)
        self.assertIn(gate.held[0].ref, notes)
        self.assertIn("held pending review", run.drive.text_of("actions"))


class NothingIsPublishedOnADoubt(unittest.TestCase):
    """Under doubt: do not withhold silently, do not publish silently. Surface it."""

    def test_a_file_that_still_carries_a_held_passage_stops_all_three(self) -> None:
        """A partial redaction is worse than none, because it looks redacted."""
        run = _Run("on")
        self.addCleanup(run.close)
        gate = run.gate()
        ctx = outputs.OutputContext(
            item_id="C1",
            source_name=run.row.name,
            parsed=naming.parse_source_name(run.row.name),
            recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
            timestamp_source="from the filename",
            # The unredacted transcript, as if the masking had silently failed.
            transcript=run.transcript,
            extraction=None,
            audio=support.audio_info(120.0),
            held=gate.held,
        )

        with self.assertRaises(outputs.HeldTextWouldLeak) as caught:
            outputs.render_all(ctx)

        self.assertEqual(caught.exception.refs, (gate.held[0].ref,))
        self.assertIn("none of the three has been written", str(caught.exception))
        self.assertNotIn("8001015009087", str(caught.exception), "the refusal must not leak it")
        self.assertEqual(run.drive.written, [], "nothing may be uploaded on this path")

    def test_a_summary_restating_a_held_passage_never_reaches_the_drive(self) -> None:
        """The summary is a model's prose about the *unredacted* text — it has to be
        unredacted, or quote verification cannot tell an invented quote from a masked one —
        so it can and does restate what the transcript masks. Either the mask catches it or
        the backstop refuses the publish. What may never happen is that it is uploaded."""
        run = _Run(
            "on",
            summary="The new chap's ID number is 8001015009087, so put him on the register.",
        )
        self.addCleanup(run.close)

        gate = run.gate()
        try:
            run.publish(gate)
        except outputs.HeldTextWouldLeak:
            self.assertEqual(run.drive.written, [], "the transcript must not go up alone")
            return
        self.assertNotIn("8001015009087", run.drive.text_of("summary"))
        self.assertIn(gate.held[0].ref, gate.extraction.summary, "it must say a passage is held")

    def test_a_leak_in_the_summary_alone_still_refuses_the_transcript(self) -> None:
        """The one the whole all-or-none rule is for.

        The transcript here is properly masked and would publish on its own; only the
        summary — a model's prose about the unredacted text — restates the held words. Two
        clean files and one that is not is the worst possible remainder, because the set
        presents itself as redacted and nobody looks twice at it. So all three stop.
        """
        run = _Run("on")
        self.addCleanup(run.close)
        gate = run.gate()
        self.assertNotIn("8001015009087", gate.transcript.text, "the transcript is clean")

        ctx = outputs.OutputContext(
            item_id="C1",
            source_name=run.row.name,
            parsed=naming.parse_source_name(run.row.name),
            recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
            timestamp_source="from the filename",
            transcript=gate.transcript,
            # As if the mask over the model's own prose had silently done nothing.
            extraction=support.StubExtraction(
                summary="His ID number is 8001015009087 and he starts on Monday."
            ),
            audio=support.audio_info(120.0),
            held=gate.held,
        )

        with self.assertRaises(outputs.HeldTextWouldLeak) as caught:
            outputs.render_all(ctx)

        self.assertIn("summary", str(caught.exception))
        self.assertIn("none of the three has been written", str(caught.exception))
        self.assertEqual(caught.exception.refs, (gate.held[0].ref,))
        self.assertEqual(run.drive.written, [], "not one of the three may be uploaded")

    def test_the_backstop_runs_again_at_the_wire(self) -> None:
        """A guard on the way in is not a guard on the way out."""
        run = _Run("on")
        self.addCleanup(run.close)
        gate = run.gate()
        good = outputs.render_all(
            outputs.OutputContext(
                item_id="C1",
                source_name=run.row.name,
                parsed=naming.parse_source_name(run.row.name),
                recorded_at=_dt.datetime(2026, 8, 27, 12, 0, 55),
                timestamp_source="from the filename",
                transcript=gate.transcript,
                extraction=gate.extraction,
                audio=support.audio_info(120.0),
                held=gate.held,
            )
        )

        with self.assertRaises(outputs.HeldTextWouldLeak):
            outputs.upload_outputs(run.drive, "O-CALLS", good, held=(run.gate().held[0],
                                                                     _leaky_span(gate)))

        self.assertEqual(run.drive.written, [], "a refusal at the wire uploads nothing")

    def test_a_refusal_never_retries_and_goes_to_a_person(self) -> None:
        from transcriber.pipeline import _NEVER_RETRY

        self.assertTrue(issubclass(outputs.HeldTextWouldLeak, _NEVER_RETRY))


def _leaky_span(gate: Any) -> Any:
    """The same held passage, pointed at words the rendered files definitely contain."""
    from transcriber.withheld import HeldSpan

    words = "the bricks land"
    start = TEXT.lower().index(words)
    return HeldSpan(
        item_id="C1",
        start=start,
        end=start + len(words),
        text=TEXT[start:start + len(words)],
        category="bare_identifier",
    )


class HeldWordsNeverReachTheLedger(unittest.TestCase):
    """``transcriber status`` prints the row, and a person pastes it into an email."""

    def test_the_rows_gate_note_is_counts_and_references_only(self) -> None:
        run = _Run("on")
        self.addCleanup(run.close)

        gate = run.gate()

        self.assertNotIn("8001015009087", repr(gate.note))
        self.assertEqual(gate.note["would_hold"], 1)
        self.assertEqual(gate.note["cut"], 1)
        self.assertEqual(gate.note["refs"], [gate.held[0].ref])
        self.assertEqual(gate.note["observed_by"], "agent")

    def test_a_review_item_quoting_a_held_passage_is_masked_before_the_row(self) -> None:
        """The review list keeps an unverifiable quote deliberately — it is the evidence a
        model produced words the recording does not contain. That is not a licence to keep a
        held one: the row is printed by ``transcriber status`` and pasted into emails."""
        from transcriber.extract import ReviewItem
        from transcriber.pipeline import _analysis_note

        run = _Run("on")
        self.addCleanup(run.close)
        run.extraction.review = (
            ReviewItem(
                category="money",
                summary="an item nobody could verify",
                offered_quote="his ID number is 8001015009087",
                reason="the words are not in the transcript",
                ratio=0.4,
                nearest="his ID number is 8001015009087 so put him",
            ),
        )

        gate = run.gate()

        self.assertNotIn("8001015009087", repr(gate.extraction.review))
        self.assertNotIn("8001015009087", repr(_analysis_note(gate.extraction)))
        self.assertIn(
            gate.held[0].ref,
            repr(gate.extraction.review),
            "a masked review item must still say which passage was taken out of it",
        )


class TheStoreIsWrittenBeforeAnythingIsCut(unittest.TestCase):
    """After the cut the words are in two places: the audio, and that database."""

    def test_a_store_that_will_not_take_them_stops_the_publish_when_armed(self) -> None:
        run = _Run("on", store=_BrokenStore())
        self.addCleanup(run.close)

        with self.assertRaises(RuntimeError):
            run.gate()

        self.assertEqual(run.drive.written, [], "nothing may be written with nowhere to hold it")

    def test_a_store_that_will_not_take_them_never_stops_a_measuring_run(self) -> None:
        """A measurement that can break the service is one nobody leaves switched on."""
        run = _Run("shadow", store=_BrokenStore())
        self.addCleanup(run.close)

        gate = run.gate()
        run.publish(gate)

        self.assertEqual(len(run.drive.written), 3)
        self.assertEqual(gate.held, ())


class TheWholeStateMachineStillWorks(unittest.TestCase):
    """The gate sits in the middle of ``process_one``; the endings must not have moved."""

    def test_a_recording_with_a_held_passage_still_reaches_done(self) -> None:
        run = _Run("on")
        self.addCleanup(run.close)
        gate = run.gate()
        run.publish(gate)
        run.ledger.advance("C1", State.TRANSCRIBED)
        run.ledger.advance("C1", State.ANALYSED)
        run.ledger.advance(
            "C1",
            State.DONE,
            transcript_name=run.drive.names[0],
            summary_name=run.drive.names[1],
            actions_name=run.drive.names[2],
        )

        row = run.ledger.get("C1")

        self.assertEqual(row.state, State.DONE)
        self.assertTrue(row.outputs_present, "a held passage may not leave a recording unfinished")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
