"""Regressions for the failures three adversarial reviews found in the assembled service.

Every test here is a fault that got all the way through the design, the module contracts and
the first test suite, and that a reader had to reconstruct by running the real code. Each one
had the same shape: the service reported success while something was lost, or reported
nothing at all while something was broken. They are gathered in one module deliberately —
a fix without a test is a fix that comes back, and these are the ones worth being able to
find again.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from transcriber import digest as digest_module
from transcriber import naming, outputs, sweep, worker
from transcriber.engines.splitting import (
    Piece,
    SplitPlan,
    SplitDurationError,
    stitch,
    verify_result_duration,
)
from transcriber.graph import GraphAuthError
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, ExtractedItem, Segment, State, Transcript
from transcriber.outputs import OutputContractError
from transcriber.pipeline import Pipeline, PipelineFatal

from . import support
from .test_output_contract import build_context
from .vendored_ingest import parse_texty

SAST = timezone(timedelta(hours=2), "SAST")


# ===========================================================================  naming


class TwoRecordingsNeverWriteOneFile(unittest.TestCase):
    """CRITICAL. OneDrive's ``(n)`` marker was stripped and then never used again.

    Two different recordings produced byte-identical output names, the second upload replaced
    the first in place, and both ledger rows said DONE with every read-back, sweep check and
    archive check passing. One recording's transcript no longer existed anywhere.
    """

    def setUp(self) -> None:
        self.when = datetime(2026, 8, 27, 14, 30, 5, tzinfo=SAST)

    def test_a_duplicate_upload_writes_different_files(self) -> None:
        first = naming.parse_source_name("Call Carel_260827_143005.m4a")
        second = naming.parse_source_name("Call Carel_260827_143005 (1).m4a")
        self.assertEqual(first.stem, second.stem, "the parsed stems are identical — that is the trap")

        a = naming.output_names(self.when, first.stem, copy_marker=first.copy_marker, item_id="ITEM-A")
        b = naming.output_names(self.when, second.stem, copy_marker=second.copy_marker, item_id="ITEM-B")

        self.assertTrue(set(a.as_tuple()).isdisjoint(b.as_tuple()))

    def test_two_recordings_with_the_same_hand_typed_name_still_differ(self) -> None:
        """No copy marker either — the item id is what makes it unique by construction."""
        a = naming.output_names(self.when, "BEACH COURT SITE WALK 270826", item_id="ITEM-A")
        b = naming.output_names(self.when, "BEACH COURT SITE WALK 270826", item_id="ITEM-B")
        self.assertNotEqual(a.transcript, b.transcript)

    def test_the_ledger_refuses_to_write_over_another_recordings_output(self) -> None:
        """The mechanical backstop under the naming fix, tested through the ledger."""
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="ITEM-A", name="a.m4a"))
            ledger.advance(
                "ITEM-A", State.DONE,
                transcript_name="20260827-143005-a-1111.md",
                summary_name="_20260827-143005-a-1111-summary.md",
                actions_name="_20260827-143005-a-1111-actions.md",
            )
            self.assertEqual(ledger.owner_of_output_name("20260827-143005-a-1111.md"), "ITEM-A")
            self.assertIsNone(ledger.owner_of_output_name("20260827-143005-b-2222.md"))


class AnAddressNeverReachesAFilename(unittest.TestCase):
    """CRITICAL. ``safe_stem`` stripped illegal characters and left ``@`` alone.

    The three output names carried the address verbatim into OneDrive, into the ledger, into
    the URL the downstream flow PUTs to, and from there into a git commit in the record —
    permanently. The header and the body were both defended; the name was not, and no
    contract check ever looked at it.
    """

    def test_an_address_in_the_source_name_is_removed_from_all_three(self) -> None:
        parsed = naming.parse_source_name("Call carel@example.co.za_260827_120055.m4a")
        when, _ = naming.resolve_timestamp(parsed, None)
        names = naming.output_names(when, parsed.stem, item_id="ITEM-A")
        for name in names.as_tuple():
            with self.subTest(name=name):
                self.assertNotIn("@", name)
                self.assertIn("address-removed", name)

    def test_a_dictated_address_in_the_source_name_is_removed_too(self) -> None:
        names = naming.output_names(
            datetime(2026, 8, 27, 14, 30, 5, tzinfo=SAST),
            "Call carel at example dot co dot za",
            item_id="ITEM-A",
        )
        self.assertNotIn("example dot co dot za", names.transcript)

    def test_check_name_refuses_a_name_carrying_an_address(self) -> None:
        self.assertTrue(outputs.check_name("20260827-143005-Call carel@example.co.za.md"))
        self.assertFalse(outputs.check_name("20260827-143005-Call Carel-1a2b3c4d.md"))

    def test_render_all_refuses_rather_than_uploading_such_a_name(self) -> None:
        ctx = build_context(source_name="Call Carel_260827_120055.m4a")
        broken = _ContextWithNames(ctx, ("bad@name.md", "_b-summary.md", "_b-actions.md"))
        with self.assertRaises(OutputContractError):
            outputs.render_all(broken)


class _ContextWithNames:
    """An OutputContext whose names are forced, so the guard can be tested on its own."""

    def __init__(self, ctx: Any, names: tuple[str, str, str]) -> None:
        self._ctx = ctx
        self._names = naming.OutputNames(stem="x", transcript=names[0], summary=names[1],
                                         actions=names[2])

    @property
    def names(self) -> naming.OutputNames:
        return self._names

    def __getattr__(self, item: str) -> Any:
        return getattr(self._ctx, item)


class OnlyTheTranscriptIsIngestedAsEvidence(unittest.TestCase):
    """HIGH. All three files landed in the watched folder under names the record ingests.

    The record's intake (``tools/transcripts.py:waiting``) skips a name beginning with ``_``
    and takes everything else as a verbatim source file — so one recording became three
    source files, three correspondence rows and three copies of the same question, and the
    model's unquoted prose in the summary satisfied a proof meant to hold only for words
    somebody actually said.
    """

    def test_the_two_derived_files_are_skipped_by_the_records_intake(self) -> None:
        files = outputs.render_all(build_context())
        by_kind = {f.kind: f.name for f in files}

        self.assertFalse(_record_would_ingest(by_kind["summary"]))
        self.assertFalse(_record_would_ingest(by_kind["actions"]))
        self.assertTrue(_record_would_ingest(by_kind["transcript"]))

    def test_the_transcript_still_parses_back_as_a_transcript(self) -> None:
        files = {f.kind: f for f in outputs.render_all(build_context())}
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, files["transcript"].name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(files["transcript"].text)
            self.assertEqual(parse_texty(path)["kind"], "transcript")

    def test_the_transcript_cross_references_the_names_the_files_really_have(self) -> None:
        files = {f.kind: f for f in outputs.render_all(build_context())}
        body = files["transcript"].text
        self.assertIn(files["summary"].name, body)
        self.assertIn(files["actions"].name, body)


def _record_would_ingest(name: str) -> bool:
    """``tools/transcripts.py:waiting`` — the real rule, copied."""
    return not os.path.basename(name).startswith((".", "_"))


# ===========================================================================  outputs


class TheOwnersAddressIsNotWrittenAsAPath(unittest.TestCase):
    """CRITICAL. OneDrive for Business puts the owner's UPN in ``webUrl``.

    ``/personal/james_kbc_co_za/`` is an email address with ``@`` and ``.`` rewritten as
    ``_``. It reverses by splitting on the underscore, and neither our address check nor the
    record's own recognises it — so it was a permanent leak in the one encoding nothing saw.
    """

    URL = "https://kbc-my.sharepoint.com/personal/james_kbc_co_za/Documents/CALLS/x.m4a"

    def test_the_owner_segment_is_removed_from_the_rendered_link(self) -> None:
        text = outputs.render_transcript(build_context(web_url=self.URL))
        self.assertNotIn("james_kbc_co_za", text)
        self.assertIn("sharepoint.com", text, "the link is still useful, just not identifying")

    def test_the_morning_email_does_not_carry_it_in_a_link(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a", web_url=self.URL))
            ledger._conn().execute("UPDATE items SET discovered_at=?", ("2026-08-26T09:00:00Z",))
            ledger.quarantine("A", "the audio is not a whole recording")
            built = digest_module.build(support.make_config(), ledger, day="2026-08-26")
        self.assertNotIn("james_kbc_co_za", built.body)
        self.assertIn("[owner removed]", built.body)

    def test_the_contract_check_refuses_such_a_path(self) -> None:
        problems = outputs.check_contract(
            "Subject: A site walk\nDate: 2026-08-27 14:30:05 +02:00\n\n- see " + self.URL + "\n"
        )
        self.assertTrue(any("personal" in p for p in problems))


class ARecordingThatDictatesAnAddressIsStillFiled(unittest.TestCase):
    """HIGH. The redacted quote was compared against the unredacted transcript.

    ``ExtractedItem`` rewrites a quote containing an address; ``_refuse_unverified`` then
    re-tested those redacted words against the raw transcript, failed, and quarantined the
    whole recording with a reason saying the words were never said — which is false, and
    sends whoever reads it hunting a hallucination that did not happen.
    """

    TEXT = ("Right, I'm at Beach Court. Ja, send the certificate to carel@example.co.za, "
            "he'll sign it off before Friday.")

    def test_the_recording_renders_instead_of_being_quarantined(self) -> None:
        item = ExtractedItem(
            kind="commitment",
            text="send the certificate for signature",
            quote="send the certificate to carel@example.co.za, he'll sign it off",
            quote_verified=True,
        )
        self.assertIn("[address removed]", item.quote)

        ctx = build_context(
            transcript=Transcript(
                text=self.TEXT,
                segments=[Segment(0.0, 9.0, "James", self.TEXT)],
                language="en-ZA",
                engine="test",
                duration_s=9.0,
            ),
            extraction=support.StubExtraction(
                summary="A certificate has to be signed.",
                proposals=[support.StubProposal("commitments", item)],
            ),
        )
        text = outputs.render_actions(ctx)
        self.assertIn("[address removed]", text)
        self.assertNotIn("carel@example.co.za", text)

    def test_a_genuinely_fabricated_quote_is_still_refused(self) -> None:
        """The guard has to stay a guard: only the comparison was wrong, not the rule."""
        item = ExtractedItem(
            kind="commitment",
            text="something nobody said",
            quote="he agreed to demolish the north wing on Tuesday",
            quote_verified=True,
        )
        ctx = build_context(
            transcript=Transcript(text=self.TEXT, segments=[], engine="test", duration_s=9.0),
            extraction=support.StubExtraction(proposals=[support.StubProposal("commitments", item)]),
        )
        with self.assertRaises(OutputContractError):
            outputs.render_actions(ctx)


class AnAddressSaidOutLoudIsEvidenceNotOutput(unittest.TestCase):
    """MEDIUM. ``EMAIL_RE`` needs an ``@``, so a dictated address passed every guard.

    In the transcript that is defensible — the words are what was said. In the summary it is
    not: a machine-authored line carrying a reconstructable address reaches a record whose
    first rule is that an address may only be reused if it is already in the tree.
    """

    SAID = ("Right, at Beach Court. His address is carel at example dot co dot za, "
            "write it down.")

    def _ctx(self) -> Any:
        return build_context(
            transcript=Transcript(text=self.SAID, segments=[Segment(0.0, 8.0, "James", self.SAID)],
                                  engine="test", duration_s=8.0),
            extraction=support.StubExtraction(
                summary="He said to send it to carel at example dot co dot za.",
            ),
        )

    def test_no_file_carries_it_and_every_file_says_it_was_removed(self) -> None:
        """The same trade the ``@`` spelling has always made here, made visibly.

        The rule against writing an address down carries no exception for the spelling, and
        the transcript already loses an ``@`` address the same way. What a reader gets
        instead is a line saying something was taken out, which is the whole difference
        between a redaction and a quiet corruption.
        """
        rendered = {f.kind: f.text for f in outputs.render_all(self._ctx())}
        for kind, text in rendered.items():
            with self.subTest(kind=kind):
                self.assertNotIn("carel at example dot co dot za", text)
                self.assertEqual(outputs.check_contract(text), [])
        # The two files whose text actually contained one say so on their face. The actions
        # file did not carry one, so it correctly says nothing.
        self.assertIn("spoken aloud", rendered["transcript"])
        self.assertIn("spoken aloud", rendered["summary"])

    def test_ordinary_site_speech_is_not_touched(self) -> None:
        for phrase in ("meet me at the roof dot com", "set it at 3 dot 5 metres",
                       "look at the parapet on the north side"):
            with self.subTest(phrase=phrase):
                from transcriber.models import strip_dictated_emails
                self.assertEqual(strip_dictated_emails(phrase), phrase)


class ASpokenFieldNameDoesNotQuarantineTheEvidence(unittest.TestCase):
    """MEDIUM, twice reported. ``"decided_by" in text`` was applied to the whole file.

    He works daily with a record whose vocabulary is literally ``decided_by:``; a voice note
    saying "put decided_by James on that one" is ordinary speech. The guard is aimed at
    metadata this pipeline generates, and it was enforced against the transcript it exists
    to protect — non-retryably, so the recording could never be filed at all.
    """

    def test_a_person_saying_the_words_is_filed(self) -> None:
        said = "So on the form where it says decided_by, put the trustees, not me."
        ctx = build_context(
            transcript=Transcript(text=said, segments=[Segment(0.0, 6.0, "James", said)],
                                  engine="test", duration_s=6.0),
        )
        text = outputs.render_transcript(ctx)
        self.assertIn("decided_by", text)
        self.assertEqual(outputs.check_contract(text), [])

    def test_the_pipeline_emitting_the_field_is_still_refused(self) -> None:
        header = "Subject: A site walk\nDate: 2026-08-27 14:30:05 +02:00\n\n"
        for body in ("decided_by: James Janeke\n", "- decided_by: James Janeke\n",
                     "  decided_by : James Janeke\n"):
            with self.subTest(body=body):
                self.assertTrue(outputs.check_contract(header + body))


class AnUnsupportedSiteReadingSaysSoOnItsFace(unittest.TestCase):
    """HIGH. A site name whose quote failed verification rendered like a verified one.

    The record scores site vocabulary out of the summary body and binds the recording to a
    real site on the strength of it, and its own rule is that filing to the wrong site is
    worse than filing to none. The explanation lived in ``extraction.notes``, which nothing
    ever rendered.
    """

    def test_a_site_with_no_quote_is_marked_as_unsupported(self) -> None:
        ctx = build_context(
            extraction=support.StubExtraction(
                summary="Roof works were discussed.",
                site="Beach Court",
                site_quote="",
                notes=("the site was read as 'Beach Court' but the words offered as evidence "
                       "are not in the transcript",),
            )
        )
        text = outputs.render_summary(ctx)
        self.assertIn("Beach Court", text)
        self.assertIn("No words in the transcript were found supporting this name", text)

    def test_the_analysis_notes_reach_the_file(self) -> None:
        ctx = build_context(
            extraction=support.StubExtraction(
                summary="Roof works were discussed.",
                notes=("an email address was removed from the summary",),
            )
        )
        self.assertIn("an email address was removed from the summary", outputs.render_summary(ctx))

    def test_a_supported_site_still_shows_its_evidence(self) -> None:
        ctx = build_context(
            extraction=support.StubExtraction(
                summary="Roof works.", site="Beach Court",
                site_quote="I'm at Beach Court now",
            )
        )
        text = outputs.render_summary(ctx)
        self.assertIn("On the strength of", text)
        self.assertNotIn("No words in the transcript were found", text)


class TheSubjectKeepsTheFileKind(unittest.TestCase):
    """MEDIUM. The 90-character cut fell after the suffix, so all three subjects matched.

    The record writes the subject as the Substance column of the site's correspondence log:
    three identical rows for one site walk, with no way to tell the evidence from the
    machine's reading of it.
    """

    LONG = ("BEACH COURT BODY CORPORATE PODIUM WATERPROOFING AND BALUSTRADE REMEDIAL WORKS "
            "SITE WALK WITH THE TRUSTEES AND THE MAIN CONTRACTOR 270826.m4a")

    def test_the_three_subjects_differ(self) -> None:
        ctx = build_context(source_name=self.LONG)
        subjects = [outputs.parse_like_downstream(f.text)[0]["subject"]
                    for f in outputs.render_all(ctx)]
        self.assertEqual(len(set(subjects)), 3, subjects)
        self.assertTrue(any("transcript" in s for s in subjects))
        self.assertTrue(any("summary" in s for s in subjects))


# ===========================================================================  splitting


def _piece(index: int, start: float, end: float, overlap: float = 0.0) -> Piece:
    return Piece(index=index, path=f"/tmp/piece-{index}.m4a", start_s=start, end_s=end,
                 overlap_before_s=overlap, size_bytes=1024,
                 measured_duration_s=end - start)


def _plan(pieces: list[Piece]) -> SplitPlan:
    return SplitPlan(source_path="/tmp/walk.m4a", duration_s=pieces[-1].end_s,
                     pieces=list(pieces), method="silence", overlap_s=6.0, temp_dir=None)


class APieceThatCameBackEmptyIsNeverSwallowed(unittest.TestCase):
    """CRITICAL. The mandatory duration guard returned silently with no timestamps.

    That is the *default* engine's behaviour — ``gpt-transcribe`` returns no segments — so on
    every recording over 25 MB the guard did nothing, and a piece the engine returned empty
    was dropped by the stitcher with no exception, no note and not even an entry in its
    unmatched list. Ten minutes of a site walk vanished, the transcript read as a complete
    conversation at a plausible word rate, and the ledger said DONE.
    """

    def test_the_stitcher_refuses_a_piece_with_no_text(self) -> None:
        plan = _plan([_piece(0, 0.0, 600.0), _piece(1, 594.0, 1200.0, overlap=6.0),
                      _piece(2, 1194.0, 1800.0, overlap=6.0)])
        results = [Transcript(text="a" * 10), Transcript(text=""), Transcript(text="b" * 10)]
        with self.assertRaises(SplitDurationError) as raised:
            stitch(plan, results)
        message = str(raised.exception)
        self.assertIn("piece 2 of 3", message)
        self.assertIn("unaccounted for", message)

    def test_a_first_piece_with_no_text_is_refused_too(self) -> None:
        plan = _plan([_piece(0, 0.0, 600.0), _piece(1, 594.0, 1200.0, overlap=6.0)])
        with self.assertRaises(SplitDurationError):
            stitch(plan, [Transcript(text=""), Transcript(text="words")])

    def test_the_guard_runs_when_the_engine_gave_no_timestamps(self) -> None:
        no_times = Transcript(
            text="a plausible sounding transcript",
            segments=[],
            engine_metadata={"split": {"piece_word_counts": [1300, 0, 1300]}},
        )
        with self.assertRaises(SplitDurationError) as raised:
            verify_result_duration(no_times, 1800.0, source_name="walk.m4a")
        self.assertIn("no text at all", str(raised.exception))

    def test_a_complete_set_of_pieces_passes(self) -> None:
        whole = Transcript(
            text="all of it",
            segments=[],
            engine_metadata={"split": {"piece_word_counts": [1300, 1200, 1300]}},
        )
        verify_result_duration(whole, 1800.0)


# ===========================================================================  ledger


class TheLedgerSurvivesAFailedCommit(unittest.TestCase):
    """LOW. ``COMMIT`` sat outside the try/except that guarantees ``ROLLBACK``.

    SQLite does not roll back a failed COMMIT, so the transaction stayed open on the cached
    thread-local connection and every later write from that thread failed with "cannot start
    a transaction within a transaction" — turning a transient full disk into a dead service.
    """

    def test_a_failed_commit_leaves_the_ledger_usable(self) -> None:
        import sqlite3

        class _FlakyCommit:
            """A connection whose first COMMIT fails the way a full disk makes it fail."""

            def __init__(self, conn: sqlite3.Connection) -> None:
                self._conn = conn
                self.fail_next_commit = False

            def execute(self, sql: str, *args: Any) -> Any:
                if sql == "COMMIT" and self.fail_next_commit:
                    self.fail_next_commit = False
                    raise sqlite3.OperationalError("database or disk is full")
                return self._conn.execute(sql, *args)

            def __getattr__(self, item: str) -> Any:
                return getattr(self._conn, item)

        with tempfile.TemporaryDirectory() as scratch:
            with Ledger(os.path.join(scratch, "ledger.sqlite")) as ledger:
                real = ledger._new_connection
                wrappers: list[Any] = []

                def wrapped() -> Any:
                    proxy = _FlakyCommit(real())
                    wrappers.append(proxy)
                    return proxy

                ledger._new_connection = wrapped  # type: ignore[method-assign]
                ledger._local.conn = None
                ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))

                wrappers[-1].fail_next_commit = True
                with self.assertRaises(sqlite3.OperationalError):
                    ledger.upsert_discovered(DriveItem(item_id="B", name="b.m4a"))

                # The point: the very next write works rather than failing forever with
                # "cannot start a transaction within a transaction".
                self.assertTrue(ledger.upsert_discovered(DriveItem(item_id="C", name="c.m4a")))


class ARequeueIsActedOnNow(unittest.TestCase):
    """LOW. ``requeue`` left the previous attempt's backoff in the row's meta.

    So the sweep's "re-queued from the start" was a half-truth and a person's manual requeue
    appeared to do nothing for up to an hour, which invites a second intervention.
    """

    def test_requeue_clears_the_backoff(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger.set_fields("A", meta={"retry_at": 9e12, "retry_reason": "throttled",
                                         "keep_me": 1})
            ledger.requeue("A", "a person fixed the credential")
            row = ledger.get("A")
            assert row is not None
            self.assertNotIn("retry_at", row.meta)
            self.assertNotIn("retry_reason", row.meta)
            self.assertEqual(row.meta.get("keep_me"), 1)
            self.assertEqual(worker.claimable_now(ledger, 10, 0.0)[0].item_id, "A")


class OneWorkerNeverStripsAnothersClaim(unittest.TestCase):
    """MEDIUM. ``release``/``record_attempt``/``quarantine`` cleared any claim, unconditionally.

    Two workers on one ledger is the ordinary shape of a redeploy on a shared volume: worker
    A failing on a recording cleared worker B's live lease on it while B was still
    transcribing, and B's next write then took the whole service down.
    """

    def test_release_with_an_owner_only_releases_our_own(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            self.assertTrue(ledger.claim("A", 900, owner="worker-B", now=1000.0))

            ledger.release("A", "worker A gave up", owner="worker-A")
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.claimed_by, "worker-B")

            ledger.release("A", "worker B finished", owner="worker-B")
            row = ledger.get("A")
            assert row is not None
            self.assertIsNone(row.claimed_by)

    def test_record_attempt_still_counts_even_when_the_claim_is_not_ours(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger.claim("A", 900, owner="worker-B", now=1000.0)
            self.assertEqual(ledger.record_attempt("A", "boom", owner="worker-A"), 1)
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.claimed_by, "worker-B")

    def test_a_quarantine_is_never_vetoed_by_somebody_elses_claim(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger.claim("A", 900, owner="worker-B", now=1000.0)
            ledger.quarantine("A", "the audio is truncated", owner="worker-A")
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.state, State.QUARANTINED)


class TheLedgerFiltersWhatItStores(unittest.TestCase):
    """LOW. The ledger was the one sink with no mechanical redaction, and it is never pruned."""

    def test_an_address_in_a_failure_reason_is_removed(self) -> None:
        with Ledger(":memory:", scrub=lambda t: t.replace("hunter2", "***")) as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger.quarantine("A", "refused for carel@example.co.za with password hunter2")
            row = ledger.get("A")
            assert row is not None
            self.assertNotIn("carel@example.co.za", row.quarantine_reason or "")
            self.assertNotIn("hunter2", row.quarantine_reason or "")


# ===========================================================================  sweep


class TheSweepMeasuresRealProgress(unittest.TestCase):
    """HIGH. Staleness came from ``updated_at``, which claiming and deferring both write.

    A row the worker picked up and failed on every 120-second cycle always looked freshly
    touched, so the backstop's re-queue and quarantine arms could never fire on exactly the
    rows that were stuck.
    """

    def test_a_row_touched_every_cycle_but_never_advancing_is_seen_as_stuck(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            # It advanced once, long ago, and has been claimed and released ever since.
            ledger.advance("A", State.CLAIMED)
            ledger._conn().execute(
                "UPDATE events SET at=? WHERE item_id='A' AND kind='advanced'",
                ("2026-08-01T00:00:00Z",),
            )
            for _ in range(5):
                ledger.claim("A", 1, owner="w", now=1000.0)
                ledger.release("A", "failed again", owner="w")

            row = ledger.get("A")
            assert row is not None
            fresh = sweep.parse_stamp(row.updated_at)
            self.assertIsNotNone(fresh)

            progress = ledger.last_advanced_at()
            self.assertEqual(progress["A"], "2026-08-01T00:00:00Z")

    def test_the_sweep_requeues_it(self) -> None:
        config = support.make_config(source_folder_id="SOURCE", output_folder_id="OUTPUT",
                                     archive_folder_id="ARCHIVE")
        with Ledger(":memory:") as ledger:
            item = support.FakeGraph.item("A", "a.m4a")
            graph = support.FakeGraph([([item], "cursor-1")])
            ledger.upsert_discovered(DriveItem.from_graph_item(item))
            ledger.advance("A", State.FETCHED)
            ledger._conn().execute(
                "UPDATE events SET at=? WHERE item_id='A' AND kind='advanced'",
                ("2026-08-01T00:00:00Z",),
            )
            # Ordinary activity right up to the moment the sweep runs.
            ledger.claim("A", 1, owner="w", now=1000.0)
            ledger.release("A", "failed", owner="w")

            report = sweep.sweep(config, ledger, graph, now=2_000_000_000.0)
            self.assertGreaterEqual(report.requeued + report.quarantined, 1, report.render())


# ===========================================================================  worker


class AnEscapedFailureStillLeavesAMark(unittest.TestCase):
    """HIGH. The thread-pool catch-all wrote nothing to the ledger at all.

    No attempt counted, no error stored, no backoff stamped — so the row came back on the
    very next 120-second cycle forever, could never reach ``max_attempts``, and spent a
    concurrency slot each time. With CONCURRENCY=2, two such rows starve the whole queue.
    """

    class _Exploding:
        max_attempts = 2

        def process_one(self, row: Any) -> Any:
            raise RuntimeError("something escaped process_one entirely")

    def _worker(self, ledger: Ledger) -> worker.Worker:
        return worker.Worker(
            support.make_config(max_attempts=2), ledger, support.FakeGraph([]),
            pipeline=self._Exploding(), heartbeat=_SilentHeartbeat(),
        )

    def test_the_attempt_is_counted_and_the_row_eventually_quarantines(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            hand = self._worker(ledger)

            hand.process_rows(ledger.claimable(), 1)
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.attempts, 1)
            self.assertIn("something escaped", row.last_error or "")

            hand.process_rows(ledger.claimable(), 1)
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.state, State.QUARANTINED)
            self.assertIn("escaped the pipeline", row.quarantine_reason or "")


class AnExpiredCredentialStopsTheService(unittest.TestCase):
    """HIGH. ``GraphAuthError`` surfaces in the poll, and the poll swallowed it.

    The loop kept running on a failing poll every two minutes, never pinged the heartbeat's
    failure endpoint, and the morning email said only "nothing arrived yesterday" — telling
    the one person who can fix it that the phone might not have synced.
    """

    class _RefusingGraph:
        def delta_with_resync(self, folder_id=None, cursor=None, on_resync=None):
            raise GraphAuthError(
                "invalid_client AADSTS7000222: the provided client secret keys are expired"
            )
            yield  # pragma: no cover - makes this a generator

    def test_the_poll_raises_rather_than_recording_a_cycle_error(self) -> None:
        with Ledger(":memory:") as ledger:
            hand = worker.Worker(support.make_config(), ledger, self._RefusingGraph(),
                                 heartbeat=_SilentHeartbeat())
            with self.assertRaises(PipelineFatal) as raised:
                hand.poll()
            self.assertIn("expired", str(raised.exception))

    def test_a_good_cycle_clears_the_mark_so_the_alarm_can_recover(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.cursor_set("worker:last_cycle_error_detail", "something was wrong")
            graph = support.FakeGraph([([support.FakeGraph.item("A", "a.m4a")], "c1")])
            cfg = support.make_config()
            # A "good cycle" means nothing failed. Leave the digest due and it fires here,
            # fails with no network, and writes the very mark this asserts is cleared.
            support.quiesce_scheduled_jobs(cfg, ledger)
            hand = worker.Worker(cfg, ledger, graph, heartbeat=_SilentHeartbeat())
            report = hand.run_once(limit=0)
            self.assertTrue(report.poll.ok)
            self.assertEqual(ledger.cursor_get("worker:last_cycle_error_detail"), "")

    def test_the_digest_names_the_credential_rather_than_the_phone(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.cursor_set(
                "worker:last_cycle_error_detail",
                "GraphAuthError: invalid_client AADSTS7000222: client secret keys are expired",
            )
            built = digest_module.build(support.make_config(), ledger, day="2026-08-26")
            self.assertIn("credentials", built.body)
            self.assertIn("SERVICE ITSELF REPORTED A FAULT", built.body)


class TheMonitorIsNotGreenOnABadMorning(unittest.TestCase):
    """HIGH. ``monitor.success`` was pinged whenever SMTP accepted the message.

    A credential expiring on a Friday, a weekend of nothing processed, and three mornings of
    "nothing arrived" all reset the external monitor's timer — making it a check on the mail
    server rather than on the pipeline, and leaving the whole thing resting on somebody
    reading a weekend email on a phone.
    """

    def test_a_nothing_arrived_morning_pings_fail(self) -> None:
        monitor = _RecordingHeartbeat()
        with Ledger(":memory:") as ledger:
            result = digest_module.run(
                support.make_config(), ledger, day="2026-08-26", heartbeat=monitor,
                smtp_factory=_accepting_smtp,
            )
        self.assertTrue(result.sent.ok)
        self.assertTrue(result.digest.alarm)
        self.assertEqual(monitor.pings, ["fail"])

    def test_an_ordinary_good_morning_still_pings_success(self) -> None:
        monitor = _RecordingHeartbeat()
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a",
                                               created_at="2026-08-26T09:00:00Z"))
            ledger._conn().execute("UPDATE items SET discovered_at=?", ("2026-08-26T09:00:00Z",))
            ledger.advance("A", State.DONE, transcript_name="t.md", summary_name="_s.md",
                           actions_name="_a.md")
            result = digest_module.run(
                support.make_config(), ledger, day="2026-08-26", heartbeat=monitor,
                smtp_factory=_accepting_smtp,
            )
        self.assertFalse(result.digest.alarm, result.digest.subject)
        self.assertEqual(monitor.pings, ["success"])

    def test_an_expiring_secret_reaches_the_subject_line(self) -> None:
        import datetime as _dt

        soon = (_dt.date.today() + _dt.timedelta(days=7)).isoformat()
        config = support.make_config(graph_secret_expires_on=soon)
        with Ledger(":memory:") as ledger:
            built = digest_module.build(config, ledger, day="2026-08-26")
        self.assertIn("expires in 7 day(s)", built.subject)
        self.assertIn("A CREDENTIAL IS RUNNING OUT", built.body)


class TheSweepReportReachesTheMorningEmail(unittest.TestCase):
    """MEDIUM. The sweep's report was logged and thrown away.

    Its findings — a recording at source with no ledger row, an unfinished one that has left
    the folder, a DONE row whose outputs cannot be named — change no ledger state, so they
    appear in no count and reached nobody at all.
    """

    def test_a_stored_sweep_report_is_rendered_into_the_digest(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.cursor_set("sweep:last_report", "nightly sweep: 1 recorded\n  ! found a gap")
            built = digest_module.build(support.make_config(), ledger, day="2026-08-26")
        self.assertIn("LAST NIGHT'S SWEEP", built.body)
        self.assertIn("found a gap", built.body)

    def test_the_worker_stores_it(self) -> None:
        config = support.make_config()
        with Ledger(":memory:") as ledger:
            graph = support.FakeGraph([([support.FakeGraph.item("A", "a.m4a")], "cursor-1")])
            hand = worker.Worker(config, ledger, graph, heartbeat=_SilentHeartbeat())
            hand._run_sweep()
            self.assertIsNotNone(hand._last_sweep)
            self.assertIn("sweep", (ledger.cursor_get("sweep:last_report") or ""))


# ===========================================================================  pipeline


class WhatCouldNotBeCheckedIsWrittenDown(unittest.TestCase):
    """HIGH. ``Transcript.engine_metadata`` was dropped at the TRANSCRIBED transition.

    Every "this check could not be run" note the engines and the splitter recorded lived only
    in the work directory, which ``_cleanup`` deletes the moment a recording reaches DONE. So
    on the default engine every split recording carried a written statement that its duration
    guard did not run, and success destroyed it.
    """

    META = {
        "split": {
            "piece_word_counts": [1300, 1200],
            "duration_guard": "plan-only: the engine returned no timestamps",
        },
        "degraded": True,
    }

    def test_the_notes_reach_the_rendered_files(self) -> None:
        from transcriber.pipeline import _engine_notes

        notes = _engine_notes(self.META)
        self.assertEqual(len(notes), 2)
        ctx = build_context(notes=notes)
        text = outputs.render_transcript(ctx)
        self.assertIn("too large for the transcription engine", text)
        self.assertIn("less accurate than usual", text)

    def test_the_metadata_is_stored_on_the_row(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger.advance("A", State.TRANSCRIBED, meta={"engine": self.META})
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.meta["engine"]["split"]["piece_word_counts"], [1300, 1200])

    def test_the_digest_counts_them(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            ledger._conn().execute("UPDATE items SET discovered_at=?", ("2026-08-26T09:00:00Z",))
            ledger.set_fields("A", meta={"engine": self.META,
                                         "analysis": {"review": 2, "review_items": []}})
            facts = ledger.attention_for_day("2026-08-26")
            self.assertEqual(facts["unverified_duration_guard"], 1)
            self.assertEqual(facts["degraded_transcripts"], 1)
            self.assertEqual(facts["review"], 2)

            built = digest_module.build(support.make_config(), ledger, day="2026-08-26")
            self.assertIn("WORTH A LOOK", built.body)
            self.assertIn("withheld", built.body)


class ALostRaceIsNotAnOutage(unittest.TestCase):
    """MEDIUM. A ``LedgerStateError`` became ``PipelineFatal`` and exited the service.

    A duplicate-processing race — the one thing leases are meant to make survivable — took
    the whole service down instead.
    """

    def test_a_row_that_another_worker_finished_returns_not_claimed(self) -> None:
        config = support.make_config()
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a"))
            pipeline = Pipeline(config, ledger, support.FakeGraph([]), owner="worker-A")

            def finish_behind_us(row: Any, started: float) -> Any:
                ledger.advance("A", State.DONE, transcript_name="t.md",
                               summary_name="_s.md", actions_name="_a.md")
                ledger.advance("A", State.FETCHED)   # what B does next; the ledger refuses

            pipeline._walk = finish_behind_us  # type: ignore[method-assign]
            outcome = pipeline.process_one("A")
        self.assertEqual(outcome.result, "not-claimed")
        self.assertIn("another worker", outcome.reason)


class ADeletedRecordingIsRecordedAsDeleted(unittest.TestCase):
    """MEDIUM. The poll dropped deleted items before they could reach the ledger.

    ``classify`` calls a deleted item STRUCTURE before any other test, so the ledger's
    careful source-deletion branch was dead code from the live path and ``source_deleted_at``
    was never set — which is the field the archive pass has dedicated handling for.
    """

    def test_a_deletion_from_the_change_feed_stamps_the_row(self) -> None:
        live = support.FakeGraph.item("A", "a.m4a")
        gone = support.FakeGraph.item("A", "a.m4a", deleted={"state": "deleted"})
        with Ledger(":memory:") as ledger:
            graph = support.FakeGraph([([live], "c1"), ([gone], "c2")])
            hand = worker.Worker(support.make_config(), ledger, graph,
                                 heartbeat=_SilentHeartbeat())
            hand.poll()
            row = ledger.get("A")
            assert row is not None
            self.assertIsNotNone(row.source_deleted_at)
            self.assertEqual(row.state, State.DISCOVERED, "the row is kept, never mirrored")


# ===========================================================================  stand-ins


class _SilentHeartbeat:
    configured = False

    def success(self, note: str = "") -> None:
        return None

    def fail(self, note: str = "") -> None:
        return None


class _RecordingHeartbeat(_SilentHeartbeat):
    def __init__(self) -> None:
        self.pings: list[str] = []

    def success(self, note: str = "") -> None:
        self.pings.append("success")

    def fail(self, note: str = "") -> None:
        self.pings.append("fail")


class _AcceptingSMTP:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_message(self, message: Any) -> None:
        self.sent.append(message)

    def __enter__(self) -> "_AcceptingSMTP":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _accepting_smtp(host: str, port: int, timeout: float | None = None) -> _AcceptingSMTP:
    return _AcceptingSMTP()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
