"""The split-duration guard: reassembled pieces must account for the whole recording.

A splitting bug does not raise. It produces a shorter transcript, which reads as a shorter
conversation, which is filed as a success and is invisible forever. So the arithmetic is
checked in two places and both are exercised here:

  * the **plan** must tile the recording — no gap between pieces, no missing tail, and the
    pieces as written to disk must measure up to the recording plus the deliberate overlap;
  * the **result**, when the engine returned timestamps, must account for the recording's
    duration within a small tolerance.

Both failures raise :class:`SplitDurationError`. Nothing in this service catches it and
downgrades it to a warning, and the last test here says so mechanically.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber import audio
from transcriber.engines import splitting
from transcriber.engines.splitting import (
    Piece,
    SplitDurationError,
    SplitError,
    SplitPlan,
    stitch,
    transcribe_with_splitting,
    verify_result_duration,
)
from transcriber.models import Hints, Segment, Transcript


def piece(index: int, start: float, end: float, overlap: float = 0.0, measured: float | None = None) -> Piece:
    return Piece(
        index=index,
        path=f"/tmp/piece-{index:03d}.wav",
        start_s=start,
        end_s=end,
        overlap_before_s=overlap,
        size_bytes=1024,
        measured_duration_s=(end - start) if measured is None else measured,
    )


def plan(pieces, duration_s: float = 600.0, overlap_s: float = 6.0) -> SplitPlan:
    return SplitPlan(
        source_path="/tmp/site-walk.m4a",
        duration_s=duration_s,
        pieces=list(pieces),
        method="test",
        overlap_s=overlap_s,
    )


class ThePlanMustTileTheRecording(unittest.TestCase):
    """``_verify_plan`` is the guard itself, so it is called directly rather than around."""

    MAX_BYTES = 25 * 1024 * 1024

    def verify(self, a_plan: SplitPlan) -> None:
        splitting._verify_plan(a_plan, self.MAX_BYTES)

    def test_a_correct_plan_passes(self) -> None:
        self.verify(plan([
            piece(0, 0.0, 300.0),
            piece(1, 294.0, 600.0, overlap=6.0),
        ]))

    def test_a_gap_between_two_pieces_is_caught(self) -> None:
        """Audio would fall straight through it and nothing else would ever say so."""
        with self.assertRaises(SplitDurationError) as raised:
            self.verify(plan([
                piece(0, 0.0, 300.0),
                piece(1, 340.0, 600.0),
            ]))
        self.assertIn("gap", str(raised.exception))

    def test_a_missing_tail_is_caught(self) -> None:
        with self.assertRaises(SplitDurationError) as raised:
            self.verify(plan([
                piece(0, 0.0, 300.0),
                piece(1, 294.0, 540.0, overlap=6.0),
            ]))
        self.assertIn("would be lost", str(raised.exception))

    def test_a_missing_head_is_caught(self) -> None:
        with self.assertRaises(SplitDurationError) as raised:
            self.verify(plan([
                piece(0, 30.0, 300.0),
                piece(1, 294.0, 600.0, overlap=6.0),
            ]))
        self.assertIn("not at the beginning", str(raised.exception))

    def test_a_piece_written_short_is_caught_even_though_the_plan_is_sound(self) -> None:
        """The boundaries are perfect and one file on disk is half of what it should be."""
        with self.assertRaises(SplitDurationError) as raised:
            self.verify(plan([
                piece(0, 0.0, 300.0, measured=300.0),
                piece(1, 294.0, 600.0, overlap=6.0, measured=150.0),
            ]))
        message = str(raised.exception)
        self.assertIn("A piece was written short", message)
        self.assertIn("not being transcribed until this is understood", message)

    def test_an_empty_piece_is_caught(self) -> None:
        empty = Piece(1, "/tmp/piece-001.wav", 294.0, 600.0, 6.0, 0, 306.0)
        with self.assertRaises(SplitDurationError):
            self.verify(plan([piece(0, 0.0, 300.0), empty]))

    def test_no_pieces_at_all_is_caught(self) -> None:
        with self.assertRaises(SplitDurationError):
            self.verify(plan([]))

    def test_a_piece_over_the_engine_s_limit_is_caught(self) -> None:
        too_big = Piece(0, "/tmp/piece-000.wav", 0.0, 600.0, 0.0, self.MAX_BYTES + 1, 600.0)
        with self.assertRaises(SplitError):
            self.verify(plan([too_big]))

    def test_ordinary_rounding_does_not_fire_it(self) -> None:
        """A guard that fires on a container's rounding is a guard that gets switched off."""
        self.verify(plan([
            piece(0, 0.0, 300.04),
            piece(1, 294.0, 600.0, overlap=6.0, measured=306.03),
        ]))


class TheResultMustAccountForTheRecording(unittest.TestCase):
    def test_a_reassembled_transcript_that_is_short_raises(self) -> None:
        short = Transcript(
            text="only the first half of it",
            segments=[Segment(0.0, 300.0, None, "only the first half of it")],
        )
        with self.assertRaises(SplitDurationError) as raised:
            verify_result_duration(short, 600.0, source_name="BEACH COURT SITE WALK 270826.m4a")

        message = str(raised.exception)
        self.assertIn("300.0s of a 600.0s recording", message)
        self.assertIn("is not marked done", message)

    def test_a_transcript_that_covers_the_recording_passes(self) -> None:
        whole = Transcript(
            text="all of it",
            segments=[Segment(0.0, 300.0, None, "first"), Segment(300.0, 599.0, None, "second")],
        )
        verify_result_duration(whole, 600.0)

    def test_an_engine_that_returned_no_timestamps_is_still_checked_and_says_how(self) -> None:
        """An unverifiable guard is a failure, not a pass — and it is visible in the metadata.

        The default engine (``gpt-transcribe``) returns no segments at all, so an early
        return here would have made the guard the architecture calls mandatory do nothing on
        every split recording that actually happens.
        """
        no_times = Transcript(text="words with no clock", segments=[])
        with self.assertRaises(SplitDurationError) as raised:
            verify_result_duration(no_times, 600.0)
        self.assertIn("no per-piece word counts", str(raised.exception))

        a_plan = plan([piece(0, 0.0, 300.0), piece(1, 294.0, 600.0, overlap=6.0)])
        stitched = stitch(a_plan, [Transcript(text="first half"), Transcript(text="second half")])
        self.assertIn("duration_guard", stitched.engine_metadata["split"])
        self.assertIn("plan-only", stitched.engine_metadata["split"]["duration_guard"])
        # And what it can check, it does: both pieces came back with words in them.
        verify_result_duration(stitched, 600.0)

    def test_stitch_refuses_an_incomplete_set_of_results(self) -> None:
        a_plan = plan([piece(0, 0.0, 300.0), piece(1, 294.0, 600.0, overlap=6.0)])
        with self.assertRaises(SplitError):
            stitch(a_plan, [Transcript(text="only one piece came back")])


class _ShortsecondPieceEngine:
    """An engine whose second piece comes back with half its audio missing.

    That is what a splitting bug looks like from the outside: nothing raises, the words are
    real, and the transcript is short.
    """

    name = "short-second-piece"

    def __init__(self, max_bytes: int, *, lose_the_tail: bool) -> None:
        self.max_bytes = max_bytes
        self.lose_the_tail = lose_the_tail
        self.calls: list[str] = []

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        self.calls.append(os.path.basename(path))
        length = float(hints.duration_s or 0.0)
        if self.lose_the_tail and len(self.calls) > 1:
            length = length / 2.0
        return Transcript(
            text=f"piece {len(self.calls)}",
            segments=[Segment(0.0, length, None, f"piece {len(self.calls)}")],
            engine=self.name,
            duration_s=length,
        )


class EndToEndOverARealFile(unittest.TestCase):
    """A real WAV, cut by the real splitter, with only the engine faked.

    WAV is used because it can be split with nothing but the standard library, so this runs
    on a machine with no ffmpeg — which is the machine the fallback path exists for.
    """

    DURATION_S = 120.0

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "site-walk.wav")
        with open(self.path, "wb") as handle:
            handle.write(audio.build_wav_bytes(self.DURATION_S, sample_rate=8000))
        self.size = os.path.getsize(self.path)

    def test_a_correctly_split_recording_comes_back_whole(self) -> None:
        engine = _ShortsecondPieceEngine(self.size // 3, lose_the_tail=False)

        transcript = transcribe_with_splitting(
            engine, self.path, Hints(source_name="site-walk.wav"),
            duration_s=self.DURATION_S, work_dir=self.dir.name,
        )

        self.assertGreaterEqual(len(engine.calls), 3, "the file should have been cut up")
        self.assertGreaterEqual(transcript.covered_duration_s, self.DURATION_S - 2.0)
        self.assertEqual(transcript.engine_metadata["split"]["pieces"], len(engine.calls))

    def test_a_piece_that_comes_back_short_fails_loudly(self) -> None:
        engine = _ShortsecondPieceEngine(self.size // 3, lose_the_tail=True)

        with self.assertRaises(SplitDurationError) as raised:
            transcribe_with_splitting(
                engine, self.path, Hints(source_name="site-walk.wav"),
                duration_s=self.DURATION_S, work_dir=self.dir.name,
            )
        self.assertIn("is missing", str(raised.exception))

    def test_the_temporary_pieces_are_cleaned_up_even_when_the_guard_fires(self) -> None:
        engine = _ShortsecondPieceEngine(self.size // 3, lose_the_tail=True)
        before = set(os.listdir(self.dir.name))

        with self.assertRaises(SplitDurationError):
            transcribe_with_splitting(
                engine, self.path, Hints(source_name="site-walk.wav"),
                duration_s=self.DURATION_S, work_dir=self.dir.name,
            )

        self.assertEqual(set(os.listdir(self.dir.name)), before)

    def test_a_file_the_engine_can_take_whole_is_never_split(self) -> None:
        engine = _ShortsecondPieceEngine(self.size * 10, lose_the_tail=True)
        transcript = transcribe_with_splitting(
            engine, self.path, Hints(source_name="site-walk.wav"), duration_s=self.DURATION_S,
        )
        self.assertEqual(len(engine.calls), 1)
        self.assertNotIn("split", transcript.engine_metadata)

    def test_an_engine_with_no_limit_is_never_split(self) -> None:
        class WholeFileEngine:
            name = "whole-file"
            max_bytes = None
            def transcribe(self, path, hints):
                return Transcript(text="all of it")

        transcript = transcribe_with_splitting(
            WholeFileEngine(), self.path, Hints(source_name="site-walk.wav"),
            duration_s=self.DURATION_S,
        )
        self.assertEqual(transcript.text, "all of it")

    def test_a_recording_of_unknown_duration_is_refused_rather_than_split_blind(self) -> None:
        """Without a duration the guard cannot be checked, so the split does not happen."""
        engine = _ShortsecondPieceEngine(self.size // 3, lose_the_tail=False)
        stripped = os.path.join(self.dir.name, "unmeasurable.dat")
        with open(stripped, "wb") as handle:
            handle.write(os.urandom(self.size))

        with self.assertRaises(splitting.SplitUnsupported):
            transcribe_with_splitting(
                engine, stripped, Hints(source_name="unmeasurable.dat"), work_dir=self.dir.name,
            )


class TheGuardIsNeverDowngraded(unittest.TestCase):
    def test_nothing_in_the_service_catches_a_split_duration_error(self) -> None:
        """Mechanical, because 'we would never do that' is how it gets done in a hurry."""
        import pathlib

        root = pathlib.Path(splitting.__file__).parent.parent
        offenders = [
            path
            for path in root.rglob("*.py")
            if "SplitDurationError" in path.read_text()
            and "except" in path.read_text()
            and any(
                line.strip().startswith("except") and "SplitDurationError" in line
                for line in path.read_text().splitlines()
            )
        ]
        self.assertEqual(offenders, [], "a module catches the split guard's error")


if __name__ == "__main__":
    unittest.main()
