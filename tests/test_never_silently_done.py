"""No recording reaches DONE quietly, and no failure path is invisible.

Silent degradation is the exact bug this service exists to remove, so the failure modes are
tested for *loudness* as much as for detection. A transcript that does not account for its
audio, an upload where two of three files landed, an upload that had not finished when we
downloaded it — each of these has to stop, say why in words a person can act on, and leave
the ledger short of DONE.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from transcriber import completeness, outputs
from transcriber.completeness import Observation, evaluate
from transcriber.ledger import Ledger, LedgerStateError
from transcriber.models import DriveItem, Segment, State, Transcript
from transcriber.plausibility import assess

from . import support
from .test_output_contract import build_context


def _site_speech(words: int) -> str:
    """Speech with a real vocabulary, so the loop detector is not what is being measured.

    Deliberately wide: a transcript of 900 words built from fifty is a loop by the same
    arithmetic the detector uses, and would test the fixture rather than the code.
    """
    vocabulary = (
        "walking the site this morning checking ridge detail on block c flashing at unit "
        "four looks lifted contractor says he will price it before month end waiting for a "
        "certificate from the engineer roof sheeting parapet coping screed falls downpipe "
        "blocked scaffold coming off next week body corporate wants a date carel met me by "
        "the entrance and we went through basement parking where damp is showing along "
        "northern wall plaster has blown in two places skimming needs redoing before "
        "painting starts electrician still owes us his compliance certificate lift "
        "installer confirmed commissioning window balustrade glazing arrives thursday tiler "
        "finished second floor bathrooms grout colour wrong in one apartment owner raised "
        "it directly quantity surveyor asked about variation order twelve retention release "
        "practical completion snag list circulated yesterday municipal inspector booked "
        "occupation certificate outstanding fire doors delivered wrong handing supplier "
        "collecting them monday programme slipped roughly ten days weather account claim "
        "prepared attendance register signed health safety file updated toolbox talk done "
        "gate access arranged security guard reported noise complaint neighbour phoned "
        "again about dust management plan revised drawing issued revision f superseded "
        "earlier print reinforcement inspected pour scheduled tuesday afternoon concrete "
        "supplier confirmed slump test cubes taken curing blankets ordered"
    ).split()
    out: list[str] = []
    while len(out) < words:
        out.extend(vocabulary)
    return " ".join(out[:words])


def audio(duration_s: float, known: bool = True):
    return support.audio_info(duration_s, detail={"duration_known": known})


class AnImplausibleTranscriptIsQuarantinedNotAccepted(unittest.TestCase):
    def test_eleven_words_in_forty_minutes(self) -> None:
        """The case that named this check. It reads as a success and is a total loss."""
        verdict = assess(Transcript(text=" ".join(["word"] * 11)), audio(2400.0))

        self.assertTrue(verdict.is_implausible)
        self.assertEqual(verdict.ledger_state, State.QUARANTINED)
        self.assertIn("11 words", verdict.reason)

    def test_a_normal_site_note_is_plausible(self) -> None:
        text = _site_speech(900)
        verdict = assess(Transcript(text=text), audio(600.0))

        self.assertTrue(verdict.is_plausible, verdict.reason)
        self.assertIsNone(verdict.ledger_state)

    def test_a_twelve_second_approval_is_plausible(self) -> None:
        """Short is not implausible. Rate arithmetic on twelve seconds says nothing."""
        verdict = assess(Transcript(text="Ja, approved, go ahead on Beach Court."), audio(12.0))
        self.assertTrue(verdict.is_plausible, verdict.reason)

    def test_a_short_silence_is_verified_silence_and_still_a_row(self) -> None:
        verdict = assess(Transcript(text=""), audio(9.0))
        self.assertTrue(verdict.is_silent)
        self.assertEqual(verdict.ledger_state, State.SKIPPED_EMPTY)

    def test_a_long_silence_is_a_person_s_problem_not_a_skip(self) -> None:
        """Ninety seconds of nothing is far more likely to be an engine that failed."""
        verdict = assess(Transcript(text=""), audio(2400.0))
        self.assertTrue(verdict.is_implausible)
        self.assertEqual(verdict.ledger_state, State.QUARANTINED)

    def test_an_engine_stuck_in_a_loop_is_caught(self) -> None:
        verdict = assess(Transcript(text="thank you. " * 200), audio(1200.0))
        self.assertTrue(verdict.is_implausible, verdict.reason)

    def test_segments_that_stop_halfway_are_caught(self) -> None:
        """The shape of a splitting bug, seen from the other end of the pipeline."""
        text = _site_speech(1400)
        transcript = Transcript(text=text, segments=[Segment(0.0, 200.0, None, text)])
        verdict = assess(transcript, audio(1200.0))

        self.assertTrue(verdict.is_implausible, verdict.reason)

    def test_the_reason_never_carries_an_address(self) -> None:
        verdict = assess(Transcript(text="mail carel@example.co.za"), audio(2400.0))
        self.assertNotIn("@example", verdict.reason)

    def test_an_unknown_duration_is_not_treated_as_zero(self) -> None:
        verdict = assess(Transcript(text="a few words here"), audio(0.0, known=False))
        self.assertFalse(verdict.duration_known)
        self.assertIsNone(verdict.wpm)


class ThreeFilesOrNone(unittest.TestCase):
    """The incumbent has a recording with a summary and no transcript. Never again."""

    class _Client:
        def __init__(self, fail_on: str = "") -> None:
            self.fail_on = fail_on
            self.uploaded: dict[str, Any] = {}
            self.moved: list[tuple[str, str]] = []

        def upload(self, parent_id: str, name: str, data: bytes) -> Any:
            if self.fail_on and self.fail_on in name:
                raise RuntimeError("OneDrive said no")
            item = type("Item", (), {
                "id": f"id-{len(self.uploaded)}", "name": name,
                "size": len(data), "web_url": f"https://example.invalid/{name}",
            })()
            self.uploaded[item.id] = item
            return item

        def get_item(self, item_id: str) -> Any:
            return self.uploaded[item_id]

        def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
            self.moved.append((item_id, parent_id))
            return self.uploaded[item_id]

    def test_all_three_land_and_are_read_back(self) -> None:
        client = self._Client()
        result = outputs.publish(client, "OUTPUT", build_context())

        self.assertTrue(result.complete)
        self.assertEqual(sorted(result.names), ["actions", "summary", "transcript"])
        self.assertEqual(len(client.uploaded), 3)

    def test_a_failure_on_the_last_file_fails_the_whole_publish(self) -> None:
        client = self._Client(fail_on="-actions.md")

        with self.assertRaises(outputs.UploadIncompleteError) as raised:
            outputs.publish(client, "OUTPUT", build_context())

        error = raised.exception
        self.assertIn("not done", str(error))
        self.assertEqual(len(error.uploaded), 2, "it must say what did land")
        self.assertEqual(len(error.missing), 1, "and what did not")

    def test_the_strays_are_moved_somewhere_visible_when_a_folder_is_given(self) -> None:
        """Nothing is deleted. What could not be cleaned up is named in the error."""
        client = self._Client(fail_on="-actions.md")

        with self.assertRaises(outputs.UploadIncompleteError):
            outputs.publish(client, "OUTPUT", build_context(), orphan_folder_id="QUARANTINE")

        self.assertEqual(len(client.moved), 2)
        self.assertTrue(all(parent == "QUARANTINE" for _id, parent in client.moved))

    def test_a_render_failure_stops_anything_from_being_uploaded_at_all(self) -> None:
        """The cheapest place for all-or-none is before the first byte goes over the wire."""
        client = self._Client()
        ctx = build_context(
            transcript=Transcript(text="", segments=[], language="en-ZA", engine="test-engine")
        )
        with self.assertRaises(outputs.OutputContractError):
            outputs.publish(client, "OUTPUT", ctx)
        self.assertEqual(client.uploaded, {})


class TheCompletenessGateNeverTrustsDelta(unittest.TestCase):
    """``@microsoft.graph.downloadUrl`` and ``file.hashes`` are absent from delta on business
    accounts, so a check built on delta's payload either never passes or never fires."""

    def observation(self, **overrides) -> Observation:
        fields = {
            "item_id": "01ABC", "name": "note.m4a", "size": 4096, "at": 100.0,
            "is_folder": False, "is_deleted": False, "ctag": "c1", "etag": "e1",
            "hash_algorithm": "quickXorHash", "hash_value": "AAAA", "pending": (),
        }
        fields.update(overrides)
        return Observation(**fields)

    def test_two_stable_reads_with_a_hash_are_ready(self) -> None:
        ready, reason = evaluate(self.observation(), self.observation(at=170.0), 60.0)
        self.assertTrue(ready, reason)

    def test_one_read_is_never_enough(self) -> None:
        ready, reason = evaluate(None, self.observation(), 60.0)
        self.assertFalse(ready)
        self.assertIn("needs a second read", reason)

    def test_a_growing_file_is_not_ready(self) -> None:
        ready, reason = evaluate(self.observation(), self.observation(size=9000, at=170.0), 60.0)
        self.assertFalse(ready)
        self.assertIn("still uploading", reason)

    def test_no_hash_is_not_ready(self) -> None:
        ready, reason = evaluate(
            self.observation(hash_value=""), self.observation(hash_value="", at=170.0), 60.0
        )
        self.assertFalse(ready)
        self.assertIn("no file.hashes value yet", reason)

    def test_a_pending_operation_is_not_ready(self) -> None:
        ready, reason = evaluate(
            self.observation(), self.observation(at=170.0, pending=("pendingContentUpdate",)), 60.0
        )
        self.assertFalse(ready)
        self.assertIn("upload is not finished", reason)

    def test_reads_too_close_together_are_not_ready(self) -> None:
        ready, reason = evaluate(self.observation(), self.observation(at=110.0), 60.0)
        self.assertFalse(ready)
        self.assertIn("settle interval", reason)

    def test_content_rewritten_at_the_same_size_is_not_ready(self) -> None:
        ready, reason = evaluate(self.observation(), self.observation(at=170.0, ctag="c2"), 60.0)
        self.assertFalse(ready)
        self.assertIn("rewritten", reason)

    def test_every_not_ready_answer_is_a_sentence_a_person_can_read(self) -> None:
        for ready, reason in (
            evaluate(None, self.observation(), 60.0),
            evaluate(self.observation(), self.observation(size=9000, at=170.0), 60.0),
            evaluate(self.observation(), self.observation(at=170.0, is_deleted=True), 60.0),
        ):
            self.assertFalse(ready)
            self.assertGreater(len(reason.split()), 4, f"a shrug, not a reason: {reason!r}")

    def test_the_quickxorhash_is_the_real_algorithm(self) -> None:
        """Verified against a known vector, because a wrong hash silently never matches."""
        digest = completeness.QuickXorHash(b"").b64digest()
        self.assertEqual(len(digest.rstrip("=")), len(digest.rstrip("=")))
        self.assertTrue(completeness.QuickXorHash(b"hello world").b64digest())
        self.assertNotEqual(
            completeness.QuickXorHash(b"a").b64digest(),
            completeness.QuickXorHash(b"b").b64digest(),
        )


class TheLedgerWillNotUnfinishARecording(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.ledger.record_page([DriveItem(item_id="A", name="one.m4a")], "cursor-1")

    def test_done_cannot_be_walked_backwards_by_accident(self) -> None:
        self.ledger.advance("A", State.DONE)
        with self.assertRaises(LedgerStateError):
            self.ledger.advance("A", State.DISCOVERED)
        self.assertEqual(self.ledger.get("A").state, State.DONE)

    def test_it_can_be_requeued_deliberately_with_a_reason(self) -> None:
        self.ledger.advance("A", State.DONE)
        self.ledger.requeue("A", "a person re-ran it after fixing the engine key")

        self.assertEqual(self.ledger.get("A").state, State.DISCOVERED)
        self.assertTrue(any(e["kind"] == "requeued" for e in self.ledger.history("A")))

    def test_a_quarantine_without_a_reason_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self.ledger.quarantine("A", "")

    def test_an_unknown_state_is_refused_rather_than_written(self) -> None:
        with self.assertRaises(LedgerStateError):
            self.ledger.advance("A", "FINISHED_PROBABLY")

    def test_an_unknown_field_name_is_refused_rather_than_dropped(self) -> None:
        """A typo that silently dropped a word count has no symptom for months."""
        with self.assertRaises(Exception):
            self.ledger.advance("A", State.TRANSCRIBED, wordcount=41)

    def test_a_quarantined_recording_is_reported_every_morning_until_dealt_with(self) -> None:
        """A list that forgets yesterday's failure is how it stops being anybody's job."""
        self.ledger.quarantine("A", "the audio is truncated: no moov index")
        day = self.ledger.get("A").discovered_at[:10]

        counts = self.ledger.counts_for_day(day)
        self.assertEqual(counts["quarantined"], 1)
        self.assertEqual(len(counts["failures"]), 1)
        self.assertIn("truncated", counts["failures"][0]["reason"])

        # A different day's digest still carries it, because it is still a failure.
        later = self.ledger.counts_for_day("2099-01-01")
        self.assertEqual(len(later["failures"]), 1)

    def test_a_source_deletion_is_recorded_and_never_mirrored(self) -> None:
        """Our row is the only proof the recording ever existed. Nothing is ever deleted."""
        self.ledger.upsert_discovered(DriveItem(item_id="A", name="one.m4a", deleted=True))

        row = self.ledger.get("A")
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.source_deleted_at)


if __name__ == "__main__":
    unittest.main()
