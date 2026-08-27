"""Splitting a recording that is larger than an engine will accept, and putting it back.

Only engines that declare a ``max_bytes`` ever come here. ElevenLabs and Azure take whole
files, so for them this module is dead code and that is the point: the safest split is the
one that never happens.

**The failure this module exists to prevent.** A splitting bug does not raise. It produces
a shorter transcript, which reads as a shorter conversation, which is filed as a success
and is invisible forever. So the arithmetic is checked rather than trusted, in two places:

  * the *plan* must tile the whole recording — first piece starts at zero, last piece ends
    at the duration, consecutive pieces overlap rather than leave a gap, and the pieces as
    actually written to disk must measure up to the duration plus the overlaps;
  * the *result*, when the engine returned timestamps, must account for the original
    duration within a small tolerance.

Either check failing raises :class:`SplitDurationError`. Nothing here ever returns a short
transcript with a warning attached.

**Where the cuts go.** On silence, never on a fixed offset — a cut through the middle of a
word loses the word at both ends. Silence is found in pure Python over PCM decoded by
ffmpeg. When there is no ffmpeg the fallback cuts on container frame edges (MP3, ADTS AAC,
WAV), which is a real cut but a blind one: it can land mid-sentence. That is recorded in
``Transcript.engine_metadata['split']['method']`` so the transcript itself says how it was
cut, rather than a person having to infer it from a sentence that stops halfway.
"""

from __future__ import annotations

import array
import json
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..models import Hints, Segment, Transcript
from .base import Engine, EngineError

__all__ = [
    "SplitError",
    "SplitDurationError",
    "SplitUnsupported",
    "Piece",
    "SplitPlan",
    "probe_duration",
    "split_audio",
    "stitch",
    "verify_result_duration",
    "transcribe_with_splitting",
    "DEFAULT_OVERLAP_S",
]

log = logging.getLogger("transcriber.engines.splitting")

#: Overlap between consecutive pieces. Long enough to contain a whole sentence, which is
#: what makes de-duplication possible; short enough not to double the cost of a long walk.
DEFAULT_OVERLAP_S = 6.0

#: Keep pieces comfortably under the engine's ceiling: a container's own headers are copied
#: into every piece, so a piece is always a little larger than its share of the source.
SIZE_SAFETY = 0.88

#: Silence hunting.
_FRAME_MS = 20
_MIN_SILENCE_S = 0.30
_SEARCH_FRACTION = 0.25      # how far either side of the target a cut may be moved
_DECODE_RATE = 8000          # speech/silence separates perfectly well at 8kHz


def _effective_overlap(requested_s: float, piece_capacity_s: float) -> float:
    """Never let the repeat eat the piece.

    A six-second overlap on an eight-second piece advances two seconds per request, which
    turns one recording into dozens of calls and multiplies both the cost and the number of
    joins that can go wrong. A quarter of the piece is the most that is ever useful.
    """
    return max(0.0, min(requested_s, piece_capacity_s / 4.0))


class SplitError(EngineError):
    """Splitting could not be done, or could not be shown to be correct."""


class SplitDurationError(SplitError):
    """The mandatory guard. The pieces do not account for the original recording.

    This is the loud failure that replaces a silently shortened transcript. It is never
    caught and downgraded anywhere in this service.
    """


class SplitUnsupported(SplitError):
    """This file cannot be split safely on this machine, and the remedy is named."""


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class Piece:
    """One piece of the split. ``start_s``/``end_s`` are positions in the ORIGINAL clock."""

    index: int
    path: str
    start_s: float
    end_s: float
    overlap_before_s: float
    size_bytes: int
    measured_duration_s: float

    @property
    def cut_at_s(self) -> float:
        """Where this piece's own content begins, ignoring the overlap it repeats."""
        return self.start_s + self.overlap_before_s


@dataclass
class SplitPlan:
    source_path: str
    duration_s: float
    pieces: list[Piece]
    method: str
    overlap_s: float
    metadata: dict[str, Any] = field(default_factory=dict)
    temp_dir: str | None = None

    @property
    def cut_points(self) -> list[float]:
        """The authoritative boundaries: where piece *i*'s content takes over from *i-1*."""
        return [p.cut_at_s for p in self.pieces[1:]]

    def cleanup(self) -> None:
        if self.temp_dir and os.path.isdir(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "pieces": len(self.pieces),
            "method": self.method,
            "overlap_s": self.overlap_s,
            "duration_s": round(self.duration_s, 3),
            "cuts_s": [round(c, 3) for c in self.cut_points],
            **self.metadata,
        }


# --------------------------------------------------------------------------- tooling


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _run(argv: Sequence[str], *, capture: bool = True, stdout_path: str | None = None) -> subprocess.CompletedProcess:
    """One place that shells out, so a missing binary and a non-zero exit read the same."""
    try:
        if stdout_path:
            with open(stdout_path, "wb") as sink:
                return subprocess.run(
                    list(argv), stdout=sink, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, check=False
                )
        return subprocess.run(
            list(argv),
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise SplitError(f"could not run {argv[0]}: {exc}") from exc


def probe_duration(path: str) -> float | None:
    """Duration in seconds, from ffprobe when it exists and from the container when it does not.

    Returns None rather than a guess. A guessed duration would be compared against the
    pieces by the guard below, which would then be checking one estimate against another.
    """
    ffprobe = _tool("ffprobe")
    if ffprobe:
        result = _run([
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-print_format", "json", path,
        ])
        if result.returncode == 0:
            try:
                doc = json.loads(result.stdout.decode("utf-8", "replace"))
                value = float(doc["format"]["duration"])
                if value > 0:
                    return value
            except (ValueError, KeyError, TypeError):
                pass
    reader = _frame_reader_for(path)
    if reader is not None:
        return reader.duration_s
    return None


# --------------------------------------------------------------------------- planning


def split_audio(
    path: str,
    max_bytes: int,
    *,
    duration_s: float | None = None,
    overlap_s: float = DEFAULT_OVERLAP_S,
    work_dir: str | None = None,
) -> SplitPlan:
    """Cut ``path`` into pieces that each fit inside ``max_bytes``.

    Raises rather than returning anything approximate: an unsplittable file is a visible
    problem for a person, not a transcript with a hole in it.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        raise SplitError(
            f"{os.path.basename(path)} is {size} bytes and the engine accepts {max_bytes} — "
            "nothing to split; the caller should not have come here"
        )
    duration = duration_s if (duration_s and duration_s > 0) else probe_duration(path)
    if not duration or duration <= 0:
        raise SplitUnsupported(
            f"the duration of {os.path.basename(path)} could not be established, and the "
            "split-duration guard cannot be checked against an unknown duration. Install "
            "ffmpeg, or route this recording to an engine that takes whole files."
        )

    temp_dir = tempfile.mkdtemp(prefix="split-", dir=work_dir or None)
    try:
        if _tool("ffmpeg"):
            plan = _split_on_silence(path, max_bytes, duration, overlap_s, temp_dir)
        else:
            plan = _split_on_frames(path, max_bytes, duration, overlap_s, temp_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    _verify_plan(plan, max_bytes)
    return plan


# -- the ffmpeg path ---------------------------------------------------------------


def _split_on_silence(
    path: str,
    max_bytes: int,
    duration: float,
    overlap_s: float,
    temp_dir: str,
) -> SplitPlan:
    size = os.path.getsize(path)
    bytes_per_second = size / duration
    capacity_s = (max_bytes * SIZE_SAFETY) / bytes_per_second
    overlap_s = _effective_overlap(overlap_s, capacity_s)
    target = capacity_s - overlap_s
    if target < 5.0:
        raise SplitUnsupported(
            f"{os.path.basename(path)} would have to be cut into pieces of {target:.1f}s to "
            "fit the engine's limit, which is too short to transcribe usefully; route it to "
            "an engine that takes whole files (elevenlabs, azure)"
        )

    silences = _silent_spans(path, duration, temp_dir)
    cuts = _choose_cuts(duration, target, silences)
    boundaries = [0.0] + cuts + [duration]

    ext = os.path.splitext(path)[1] or ".m4a"
    pieces: list[Piece] = []
    unmeasured = 0
    for index in range(len(boundaries) - 1):
        cut_at = boundaries[index]
        end = boundaries[index + 1]
        overlap = 0.0 if index == 0 else min(overlap_s, cut_at)
        start = cut_at - overlap
        out_path = os.path.join(temp_dir, f"piece-{index:03d}{ext}")
        _extract(path, out_path, start, end)
        measured = probe_duration(out_path)
        if measured is None:
            # ffmpeg without ffprobe. The piece is not independently measured, so the guard
            # below is comparing the plan against itself; that is recorded, not hidden.
            measured = end - start
            unmeasured += 1
        piece_size = os.path.getsize(out_path)
        if piece_size > max_bytes:
            # Stated rather than absorbed: a variable-bitrate stretch can defeat the
            # bytes-per-second estimate, and the honest answer is to say so, not to hope.
            raise SplitError(
                f"piece {index} of {os.path.basename(path)} came out at {piece_size} bytes, "
                f"over the engine's {max_bytes}-byte limit, despite being cut for "
                f"{target:.0f}s. The recording's bitrate is not uniform; route it to an "
                "engine that takes whole files."
            )
        pieces.append(
            Piece(
                index=index,
                path=out_path,
                start_s=start,
                end_s=end,
                overlap_before_s=overlap,
                size_bytes=piece_size,
                measured_duration_s=measured,
            )
        )
    forced = sum(1 for c in cuts if not _near_silence(c, silences))
    metadata: dict[str, Any] = {
        "silence_spans_found": len(silences),
        "cuts_forced_without_silence": forced,
        "target_piece_s": round(target, 1),
    }
    if unmeasured:
        metadata["pieces_not_independently_measured"] = unmeasured
        metadata["warning"] = (
            "ffprobe was not available, so the pieces' durations are the requested ones "
            "rather than measured ones and the duration guard could not check them"
        )
    return SplitPlan(
        source_path=path,
        duration_s=duration,
        pieces=pieces,
        method="silence (ffmpeg-decoded PCM)",
        overlap_s=overlap_s,   # after capping: what was actually used, not what was asked for
        metadata=metadata,
        temp_dir=temp_dir,
    )


def _extract(source: str, destination: str, start: float, end: float) -> None:
    """Copy the stream between two times. No re-encode: re-encoding a voice note twice is
    a second generation of loss for no gain, and the engine only needs the same audio."""
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:  # unreachable from split_audio, kept so a direct caller fails clearly
        raise SplitUnsupported("ffmpeg is not installed, so a time-based cut is not possible")
    result = _run([
        ffmpeg, "-nostdin", "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", source, "-c", "copy", destination,
    ])
    if result.returncode != 0 or not os.path.exists(destination) or os.path.getsize(destination) == 0:
        detail = result.stderr.decode("utf-8", "replace")[:300] if result.stderr else ""
        raise SplitError(
            f"ffmpeg could not cut {start:.1f}s-{end:.1f}s out of "
            f"{os.path.basename(source)}: {detail or 'no output produced'}"
        )


def _silent_spans(path: str, duration: float, temp_dir: str | None = None) -> list[tuple[float, float]]:
    """Find silence by decoding to PCM and measuring frame peaks, in pure Python.

    The threshold is derived from the recording rather than fixed: a site walk has wind,
    a generator and traffic in it, and a fixed dBFS floor finds either everything or
    nothing depending on the day.
    """
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg:
        return []
    # Into the run's own scratch directory, not the system temp directory. This file is the
    # recording fully decoded — minutes of a confidential conversation, world-readable while
    # the split runs — and a deployment that carefully put WORK_DIR on a private disk was
    # having it written outside that disk anyway.
    handle, pcm_path = tempfile.mkstemp(prefix="pcm-", suffix=".raw", dir=temp_dir or None)
    os.close(handle)
    try:
        result = _run(
            [
                ffmpeg, "-nostdin", "-v", "error", "-i", path,
                "-ac", "1", "-ar", str(_DECODE_RATE), "-f", "s16le", "-",
            ],
            stdout_path=pcm_path,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace")[:300] if result.stderr else ""
            log.warning(
                "could not decode %s to PCM for silence detection (%s); cuts will fall on "
                "the computed target instead of on silence",
                os.path.basename(path), detail,
            )
            return []
        peaks = _frame_peaks(pcm_path)
    finally:
        try:
            os.unlink(pcm_path)
        except OSError:
            pass
    if not peaks:
        return []

    ordered = sorted(peaks)
    floor = ordered[max(0, int(len(ordered) * 0.10) - 1)]
    # Three times the noise floor, but never below a hard minimum: an utterly clean
    # recording has a floor near zero, and 3*0 would call the whole file silent.
    threshold = max(floor * 3.0, 32768 * 0.015)
    frame_s = _FRAME_MS / 1000.0
    spans: list[tuple[float, float]] = []
    run_start: int | None = None
    for index, peak in enumerate(peaks):
        if peak <= threshold:
            if run_start is None:
                run_start = index
            continue
        if run_start is not None:
            _append_span(spans, run_start, index, frame_s)
            run_start = None
    if run_start is not None:
        _append_span(spans, run_start, len(peaks), frame_s)
    log.info(
        "%s: %d silent span(s) over %.0fs (threshold %.0f, floor %.0f)",
        os.path.basename(path), len(spans), duration, threshold, floor,
    )
    return spans


def _append_span(spans: list[tuple[float, float]], start_frame: int, end_frame: int, frame_s: float) -> None:
    start = start_frame * frame_s
    end = end_frame * frame_s
    if end - start >= _MIN_SILENCE_S:
        spans.append((start, end))


def _frame_peaks(pcm_path: str) -> list[int]:
    """Peak amplitude per 20ms frame of 16-bit mono PCM.

    Peak, not RMS: ``max``/``min`` over an ``array`` slice runs at C speed, which keeps a
    forty-minute recording under a second, and for the silent/not-silent decision the two
    measures agree.
    """
    samples_per_frame = int(_DECODE_RATE * _FRAME_MS / 1000)
    bytes_per_frame = samples_per_frame * 2
    peaks: list[int] = []
    with open(pcm_path, "rb") as handle:
        while True:
            block = handle.read(bytes_per_frame * 500)
            if not block:
                break
            usable = len(block) - (len(block) % 2)
            samples = array.array("h")
            samples.frombytes(block[:usable])
            for offset in range(0, len(samples), samples_per_frame):
                window = samples[offset:offset + samples_per_frame]
                if not window:
                    continue
                peaks.append(max(max(window), -min(window)))
    return peaks


def _choose_cuts(duration: float, target: float, silences: Sequence[tuple[float, float]]) -> list[float]:
    """Walk forward in target-sized strides, moving each cut onto the nearest silence."""
    cuts: list[float] = []
    position = 0.0
    window = max(2.0, target * _SEARCH_FRACTION)
    while duration - position > target:
        ideal = position + target
        cut = _best_silence(ideal, window, silences)
        if cut is None or cut <= position + 1.0:
            cut = ideal
        cuts.append(cut)
        position = cut
    return cuts


def _best_silence(ideal: float, window: float, silences: Sequence[tuple[float, float]]) -> float | None:
    best: float | None = None
    best_distance = window
    for start, end in silences:
        middle = (start + end) / 2.0
        distance = abs(middle - ideal)
        if distance <= best_distance:
            best, best_distance = middle, distance
    return best


def _near_silence(cut: float, silences: Sequence[tuple[float, float]], tolerance: float = 0.05) -> bool:
    return any(start - tolerance <= cut <= end + tolerance for start, end in silences)


# -- the no-ffmpeg path -------------------------------------------------------------


def _split_on_frames(
    path: str,
    max_bytes: int,
    duration: float,
    overlap_s: float,
    temp_dir: str,
) -> SplitPlan:
    """Cut on container frame edges, because without ffmpeg there is no PCM to measure.

    This is a blind cut: it can land in the middle of a sentence, and the overlap is the
    only thing that lets the two halves be rejoined. The method is written into the
    transcript's metadata so the record says how it was cut instead of implying it was cut
    on silence.
    """
    reader = _frame_reader_for(path)
    if reader is None:
        raise SplitUnsupported(
            f"{os.path.basename(path)} is larger than the engine accepts, ffmpeg is not "
            "installed, and this container (" + (os.path.splitext(path)[1] or "unknown") +
            ") has no walkable frame edges — an MP4/M4A cannot be cut at a byte boundary "
            "without destroying its index. Install ffmpeg, or set TRANSCRIBE_ENGINE to an "
            "engine that takes whole files (elevenlabs, azure)."
        )
    pieces = reader.split(
        temp_dir,
        max_bytes=int(max_bytes * SIZE_SAFETY),
        overlap_s=overlap_s,
    )
    used_overlap = max((p.overlap_before_s for p in pieces[1:]), default=0.0)
    if len(pieces) < 2:
        raise SplitError(
            f"frame-boundary splitting produced {len(pieces)} piece(s) for a file that is "
            "over the limit; refusing to send an oversized piece"
        )
    return SplitPlan(
        source_path=path,
        duration_s=reader.duration_s,
        pieces=pieces,
        method=f"frame-boundary, no ffmpeg ({reader.kind})",
        overlap_s=used_overlap,
        metadata={
            "cut_on_silence": False,
            "warning": (
                "ffmpeg was not available, so the cuts fall on container frame edges and "
                "may land mid-sentence; the overlap is what allows the pieces to be rejoined"
            ),
            "container_duration_s": round(reader.duration_s, 3),
        },
        temp_dir=temp_dir,
    )


class _FrameReader:
    """Base for the containers whose frames can be walked with the standard library."""

    kind = "unknown"

    def __init__(self, path: str) -> None:
        self.path = path
        self.duration_s = 0.0

    def split(self, temp_dir: str, *, max_bytes: int, overlap_s: float) -> list[Piece]:
        raise NotImplementedError


class _BlockFrameReader(_FrameReader):
    """Containers that are a header plus a sequence of self-contained frames (MP3, ADTS)."""

    def __init__(self, path: str, header: bytes, frames: Sequence[tuple[int, int, float]]) -> None:
        super().__init__(path)
        self.header = header
        # (offset, length, duration_s) for each frame, in order.
        self.frames = list(frames)
        self.duration_s = sum(f[2] for f in self.frames)

    def split(self, temp_dir: str, *, max_bytes: int, overlap_s: float) -> list[Piece]:
        if not self.frames:
            raise SplitError(f"no audio frames found in {os.path.basename(self.path)}")
        ext = os.path.splitext(self.path)[1] or ".mp3"
        # How much audio fits in one piece, from this file's own frames rather than from an
        # assumed bitrate — a variable-bitrate recording would make an assumption wrong.
        average_bytes = sum(f[1] for f in self.frames) / len(self.frames)
        average_seconds = self.duration_s / len(self.frames)
        capacity_s = ((max_bytes - len(self.header)) / average_bytes) * average_seconds
        overlap_s = _effective_overlap(overlap_s, capacity_s)
        pieces: list[Piece] = []
        index = 0
        first_frame = 0
        elapsed = 0.0
        with open(self.path, "rb") as source:
            while first_frame < len(self.frames):
                # How many frames of overlap to repeat from before this piece.
                overlap_frames = 0
                if index > 0:
                    taken = 0.0
                    while (
                        first_frame - overlap_frames - 1 >= 0
                        and taken < overlap_s
                    ):
                        overlap_frames += 1
                        taken += self.frames[first_frame - overlap_frames][2]
                begin = first_frame - overlap_frames
                budget = max_bytes - len(self.header)
                used = sum(self.frames[i][1] for i in range(begin, first_frame))
                last = first_frame
                while last < len(self.frames) and used + self.frames[last][1] <= budget:
                    used += self.frames[last][1]
                    last += 1
                if last == first_frame:
                    raise SplitError(
                        f"a single frame of {os.path.basename(self.path)} is larger than the "
                        "engine's limit; this file cannot be split"
                    )
                out_path = os.path.join(temp_dir, f"piece-{index:03d}{ext}")
                written = self._write(source, out_path, begin, last)
                start_s = sum(f[2] for f in self.frames[:begin])
                end_s = sum(f[2] for f in self.frames[:last])
                overlap = sum(f[2] for f in self.frames[begin:first_frame])
                pieces.append(
                    Piece(
                        index=index,
                        path=out_path,
                        start_s=start_s,
                        end_s=end_s,
                        overlap_before_s=overlap,
                        size_bytes=written,
                        measured_duration_s=end_s - start_s,
                    )
                )
                index += 1
                first_frame = last
        return pieces

    def _write(self, source, out_path: str, begin: int, end: int) -> int:
        with open(out_path, "wb") as sink:
            sink.write(self.header)
            for i in range(begin, end):
                offset, length, _ = self.frames[i]
                source.seek(offset)
                sink.write(source.read(length))
        return os.path.getsize(out_path)


class _WaveReader(_FrameReader):
    """WAV, via the standard library's own ``wave`` module. Sample-exact by construction."""

    kind = "wav"

    def __init__(self, path: str) -> None:
        super().__init__(path)
        with wave.open(path, "rb") as handle:
            self.channels = handle.getnchannels()
            self.sample_width = handle.getsampwidth()
            self.rate = handle.getframerate()
            self.frames = handle.getnframes()
        if self.rate <= 0 or self.frames <= 0:
            raise SplitError(f"{os.path.basename(path)} declares no usable WAV timing")
        self.duration_s = self.frames / float(self.rate)
        self.bytes_per_frame = self.channels * self.sample_width

    def split(self, temp_dir: str, *, max_bytes: int, overlap_s: float) -> list[Piece]:
        header_allowance = 512
        frames_per_piece = max(1, (max_bytes - header_allowance) // max(1, self.bytes_per_frame))
        overlap_frames = int(_effective_overlap(overlap_s, frames_per_piece / float(self.rate)) * self.rate)
        pieces: list[Piece] = []
        index = 0
        position = 0
        with wave.open(self.path, "rb") as source:
            while position < self.frames:
                begin = max(0, position - overlap_frames) if index else 0
                count = min(frames_per_piece, self.frames - begin)
                source.setpos(begin)
                payload = source.readframes(count)
                out_path = os.path.join(temp_dir, f"piece-{index:03d}.wav")
                with wave.open(out_path, "wb") as sink:
                    sink.setnchannels(self.channels)
                    sink.setsampwidth(self.sample_width)
                    sink.setframerate(self.rate)
                    sink.writeframes(payload)
                end = begin + count
                pieces.append(
                    Piece(
                        index=index,
                        path=out_path,
                        start_s=begin / float(self.rate),
                        end_s=end / float(self.rate),
                        overlap_before_s=(position - begin) / float(self.rate),
                        size_bytes=os.path.getsize(out_path),
                        measured_duration_s=count / float(self.rate),
                    )
                )
                if end <= position:
                    raise SplitError("WAV splitting made no progress; refusing to loop")
                position = end
                index += 1
        return pieces


#: MPEG audio tables, enough for Layer III which is every MP3 a phone produces.
_MP3_BITRATES_V1_L3 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BITRATES_V2_L3 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_RATES = {0: (44100, 22050, 11025), 1: (48000, 24000, 12000), 2: (32000, 16000, 8000)}
_AAC_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


def _frame_reader_for(path: str) -> _FrameReader | None:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".wav":
            return _WaveReader(path)
        if ext in (".mp3", ".mp2"):
            return _read_mpeg_frames(path)
        if ext == ".aac":
            return _read_adts_frames(path)
    except (wave.Error, OSError, SplitError) as exc:
        log.warning("could not walk the frames of %s: %s", os.path.basename(path), exc)
        return None
    return None


def _read_mpeg_frames(path: str) -> _BlockFrameReader:
    """Walk MPEG audio frame headers. Duration comes out of the frames themselves, which is
    exact for constant and variable bitrate alike."""
    with open(path, "rb") as handle:
        blob = handle.read()
    offset = 0
    header = b""
    if blob[:3] == b"ID3" and len(blob) >= 10:
        size = 0
        for byte in blob[6:10]:
            size = (size << 7) | (byte & 0x7F)
        offset = 10 + size
        header = blob[:offset]      # keep the tag: it costs nothing and keeps the file valid
    frames: list[tuple[int, int, float]] = []
    length = len(blob)
    while offset + 4 <= length:
        if blob[offset] != 0xFF or (blob[offset + 1] & 0xE0) != 0xE0:
            offset += 1
            continue
        version_bits = (blob[offset + 1] >> 3) & 0x03
        layer_bits = (blob[offset + 1] >> 1) & 0x03
        bitrate_index = (blob[offset + 2] >> 4) & 0x0F
        rate_index = (blob[offset + 2] >> 2) & 0x03
        padding = (blob[offset + 2] >> 1) & 0x01
        if version_bits == 1 or layer_bits == 0 or bitrate_index in (0, 15) or rate_index == 3:
            offset += 1
            continue
        version = {0: "2.5", 2: "2", 3: "1"}[version_bits]
        rate_column = {"1": 0, "2": 1, "2.5": 2}[version]
        sample_rate = _MP3_RATES[rate_index][rate_column]
        table = _MP3_BITRATES_V1_L3 if version == "1" else _MP3_BITRATES_V2_L3
        bitrate = table[bitrate_index] * 1000
        if not bitrate or not sample_rate:
            offset += 1
            continue
        samples = 1152 if version == "1" else 576
        frame_len = int((samples // 8) * bitrate / sample_rate) + padding
        if frame_len <= 4 or offset + frame_len > length:
            break
        frames.append((offset, frame_len, samples / float(sample_rate)))
        offset += frame_len
    if not frames:
        raise SplitError(f"no MPEG audio frames found in {os.path.basename(path)}")
    reader = _BlockFrameReader(path, header, frames)
    reader.kind = "mp3"
    return reader


def _read_adts_frames(path: str) -> _BlockFrameReader:
    """Walk ADTS AAC frames — each carries its own length, so the walk is exact."""
    with open(path, "rb") as handle:
        blob = handle.read()
    offset = 0
    length = len(blob)
    frames: list[tuple[int, int, float]] = []
    while offset + 7 <= length:
        if blob[offset] != 0xFF or (blob[offset + 1] & 0xF0) != 0xF0:
            offset += 1
            continue
        rate_index = (blob[offset + 2] >> 2) & 0x0F
        if rate_index >= len(_AAC_RATES):
            offset += 1
            continue
        frame_len = ((blob[offset + 3] & 0x03) << 11) | (blob[offset + 4] << 3) | (blob[offset + 5] >> 5)
        if frame_len <= 7 or offset + frame_len > length:
            break
        frames.append((offset, frame_len, 1024.0 / _AAC_RATES[rate_index]))
        offset += frame_len
    if not frames:
        raise SplitError(f"no ADTS AAC frames found in {os.path.basename(path)}")
    reader = _BlockFrameReader(path, b"", frames)
    reader.kind = "adts-aac"
    return reader


# --------------------------------------------------------------------------- guards


def _tolerance_for(duration: float) -> float:
    """Two percent, floored at two seconds. Loose enough for a container's rounding, tight
    enough that a lost piece — the failure this is here for — can never pass."""
    return max(2.0, duration * 0.02)


def _verify_plan(plan: SplitPlan, max_bytes: int) -> None:
    """The first half of the mandatory guard: do the pieces tile the recording?"""
    if not plan.pieces:
        raise SplitDurationError(f"splitting {os.path.basename(plan.source_path)} produced no pieces")
    tolerance = _tolerance_for(plan.duration_s)
    first, last = plan.pieces[0], plan.pieces[-1]
    if first.start_s > tolerance:
        raise SplitDurationError(
            f"the first piece of {os.path.basename(plan.source_path)} starts at "
            f"{first.start_s:.2f}s, not at the beginning — {first.start_s:.2f}s of audio "
            "would be lost"
        )
    if plan.duration_s - last.end_s > tolerance:
        raise SplitDurationError(
            f"the last piece of {os.path.basename(plan.source_path)} ends at {last.end_s:.2f}s "
            f"but the recording is {plan.duration_s:.2f}s — {plan.duration_s - last.end_s:.2f}s "
            "would be lost"
        )
    for previous, current in zip(plan.pieces, plan.pieces[1:]):
        if current.start_s > previous.end_s + 0.001:
            raise SplitDurationError(
                f"there is a {current.start_s - previous.end_s:.2f}s gap between piece "
                f"{previous.index} and piece {current.index} of "
                f"{os.path.basename(plan.source_path)} — audio would fall through it"
            )
    measured = sum(p.measured_duration_s for p in plan.pieces)
    overlaps = sum(p.overlap_before_s for p in plan.pieces)
    expected = plan.duration_s + overlaps
    if abs(measured - expected) > tolerance:
        raise SplitDurationError(
            f"the pieces of {os.path.basename(plan.source_path)} measure {measured:.2f}s but "
            f"should measure {expected:.2f}s ({plan.duration_s:.2f}s of audio plus "
            f"{overlaps:.2f}s of deliberate overlap). A piece was written short; the "
            "recording is not being transcribed until this is understood."
        )
    for piece in plan.pieces:
        if piece.size_bytes > max_bytes:
            raise SplitError(
                f"piece {piece.index} is {piece.size_bytes} bytes, over the engine's "
                f"{max_bytes}-byte limit"
            )
        if piece.size_bytes == 0:
            raise SplitDurationError(f"piece {piece.index} was written empty")


def verify_result_duration(
    transcript: Transcript,
    expected_duration_s: float,
    *,
    source_name: str = "",
    tolerance_s: float | None = None,
) -> None:
    """The second half: does the reassembled transcript account for the whole recording?

    Two ways of asking, because the first one is not always available. When the engine
    returned timestamps the assembled transcript is measured against the clock. When it did
    not — which is the **default** engine's behaviour, ``gpt-transcribe`` returns no
    ``segments`` — an early ``return`` would have made the guard the architecture calls
    mandatory do nothing at all on every split recording that actually happens. So the
    fallback runs instead: every piece must have come back with words in it. A piece that
    transcribes as nothing is either genuine silence in the middle of a site walk or the
    silent loss this module exists to prevent, and both endings are a person looking at it.
    """
    if not transcript.segments:
        _verify_pieces_have_text(transcript, expected_duration_s, source_name)
        return
    tolerance = tolerance_s if tolerance_s is not None else _tolerance_for(expected_duration_s)
    covered = transcript.covered_duration_s
    if expected_duration_s - covered > tolerance:
        raise SplitDurationError(
            f"the reassembled transcript of {source_name or 'the recording'} accounts for "
            f"{covered:.1f}s of a {expected_duration_s:.1f}s recording — "
            f"{expected_duration_s - covered:.1f}s is missing. This is the silent-loss "
            "failure the split guard exists to catch; the recording is not marked done."
        )


def _verify_pieces_have_text(
    transcript: Transcript, expected_duration_s: float, source_name: str
) -> None:
    """The guard that can still run when there are no timestamps at all.

    It reads what :func:`stitch` wrote down: one word count per piece. A zero there is a
    tenth of a site walk that reached the transcript as nothing, and the assembled text
    still reads as a complete conversation at a plausible word rate — which is precisely
    why nothing downstream can catch it.
    """
    split = dict((transcript.engine_metadata or {}).get("split") or {})
    counts = list(split.get("piece_word_counts") or ())
    if not counts:
        raise SplitDurationError(
            f"the reassembled transcript of {source_name or 'the recording'} carries no "
            "timestamps and no per-piece word counts, so there is no way to show that the "
            f"{expected_duration_s:.0f}s recording is all there. An unverifiable guard is a "
            "failure, not a pass."
        )
    empty = [index + 1 for index, count in enumerate(counts) if not int(count or 0)]
    if empty:
        raise SplitDurationError(
            f"piece(s) {', '.join(str(i) for i in empty)} of {len(counts)} of "
            f"{source_name or 'the recording'} came back with no text at all, so part of a "
            f"{expected_duration_s:.0f}s recording is unaccounted for. The assembled "
            "transcript would read as a complete conversation; it is not one."
        )


# --------------------------------------------------------------------------- stitching


def stitch(plan: SplitPlan, results: Sequence[Transcript]) -> Transcript:
    """Put the pieces back, removing what the overlap made us hear twice."""
    if len(results) != len(plan.pieces):
        raise SplitError(
            f"{len(results)} transcript(s) for {len(plan.pieces)} piece(s) — refusing to "
            "assemble a transcript from an incomplete set"
        )
    have_segments = any(r.segments for r in results)
    metadata: dict[str, Any] = {"split": plan.as_metadata()}
    metadata["split"]["piece_word_counts"] = [r.word_count for r in results]

    if have_segments:
        text, segments, notes = _stitch_by_time(plan, results)
    else:
        text, segments, notes = _stitch_by_text(plan, results)
        metadata["split"]["duration_guard"] = (
            "plan-only: the engine returned no timestamps, so the pieces were checked "
            "against the recording's duration but the assembled transcript could not be"
        )
    metadata["split"].update(notes)

    languages = [r.language for r in results if r.language]
    engines = {r.engine for r in results if r.engine}
    for index, result in enumerate(results):
        for key, value in (result.engine_metadata or {}).items():
            if key in ("dropped_fields", "degraded"):
                metadata.setdefault(f"piece_{index}_{key}", value)
    if any((r.engine_metadata or {}).get("degraded") for r in results):
        metadata["degraded"] = True

    return Transcript(
        text=text,
        segments=segments,
        language=languages[0] if languages else None,
        engine_metadata=metadata,
        engine=next(iter(engines)) if len(engines) == 1 else ",".join(sorted(engines)),
        duration_s=plan.duration_s,
    )


def _stitch_by_time(
    plan: SplitPlan,
    results: Sequence[Transcript],
) -> tuple[str, list[Segment], dict[str, Any]]:
    """Shift each piece onto the original clock and keep each stretch of audio once.

    The rule is *coverage first*. A segment entirely inside what an earlier piece already
    accounted for is the overlap being heard twice and is dropped. A segment that starts
    inside the overlap but runs past it is kept, with the words it repeats trimmed off its
    front — because dropping it would leave a hole in the timeline, and a hole is exactly
    the silent loss this whole module exists to prevent. Duplicating a phrase is visible;
    losing one is not.
    """
    kept: list[Segment] = []
    dropped = 0
    trimmed = 0
    covered = 0.0
    epsilon = 0.05
    for piece, result in zip(plan.pieces, results):
        shifted = sorted(
            (s.shifted(piece.start_s) for s in result.segments if s.text.strip()),
            key=lambda s: (s.start, s.end),
        )
        for segment in shifted:
            if segment.end <= covered + epsilon:
                dropped += 1
                continue
            if kept and segment.start < covered - epsilon:
                repeat = _longest_word_overlap(kept[-1].text, segment.text, min_words=1)
                words = segment.text.split()
                if repeat >= len(words):
                    # Every word of it was already heard. Keep the timeline honest by
                    # extending the segment that carried those words, and say nothing twice.
                    previous = kept[-1]
                    kept[-1] = Segment(previous.start, max(previous.end, segment.end),
                                       previous.speaker, previous.text)
                    covered = max(covered, segment.end)
                    dropped += 1
                    continue
                if repeat:
                    segment = Segment(segment.start, segment.end, segment.speaker,
                                      " ".join(words[repeat:]))
                    trimmed += 1
            kept.append(segment)
            covered = max(covered, segment.end)
    text = " ".join(s.text.strip() for s in kept if s.text.strip()).strip()
    return text, kept, {
        "overlap_segments_dropped": dropped,
        "overlap_segments_trimmed": trimmed,
        "join": "timestamps",
    }


def _stitch_by_text(
    plan: SplitPlan,
    results: Sequence[Transcript],
) -> tuple[str, list[Segment], dict[str, Any]]:
    """No timestamps: rejoin on the words the overlap made both pieces say.

    Where the repeat cannot be found the pieces are joined anyway and the fact is recorded.
    Guessing at a join and hiding it is how a duplicated or dropped sentence gets into the
    record with nothing to show for it.
    """
    for index, result in enumerate(results):
        if not (result.text or "").strip():
            piece = plan.pieces[index] if index < len(plan.pieces) else None
            seconds = piece.measured_duration_s if piece is not None else 0.0
            raise SplitDurationError(
                f"piece {index + 1} of {len(results)} came back with no text; "
                f"{seconds:.0f}s of the recording is unaccounted for. Skipping it would "
                "produce a transcript that reads whole and is not — the silent loss this "
                "module exists to prevent."
            )
    text = (results[0].text or "").strip()
    unmatched: list[int] = []
    for index in range(1, len(results)):
        piece_text = (results[index].text or "").strip()
        overlap = _longest_word_overlap(text, piece_text)
        if overlap:
            piece_text = " ".join(piece_text.split()[overlap:])
        else:
            unmatched.append(index)
        text = (text + " " + piece_text).strip() if piece_text else text
    notes: dict[str, Any] = {"join": "word-overlap (no timestamps)"}
    if unmatched:
        notes["overlap_not_matched_in_pieces"] = unmatched
    return text, [], notes


def _longest_word_overlap(left: str, right: str, max_words: int = 80, min_words: int = 4) -> int:
    """How many words at the start of ``right`` repeat the end of ``left``.

    ``min_words`` is the confidence dial. Where the timestamps already prove the two pieces
    cover the same audio, one repeated word is evidence enough; where all we have is text,
    four is the shortest run that is not a coincidence of ordinary English.
    """
    left_words = left.split()
    right_words = right.split()
    limit = min(max_words, len(left_words), len(right_words))
    if limit < min_words:
        return 0
    normalise = lambda words: [w.strip(".,;:!?\"'()").lower() for w in words]
    tail = normalise(left_words[-limit:])
    head = normalise(right_words[:limit])
    for size in range(limit, min_words - 1, -1):
        if tail[-size:] == head[:size]:
            return size
    return 0


# --------------------------------------------------------------------------- entry point


def transcribe_with_splitting(
    engine: Engine,
    path: str,
    hints: Hints,
    *,
    duration_s: float | None = None,
    work_dir: str | None = None,
    overlap_s: float = DEFAULT_OVERLAP_S,
) -> Transcript:
    """Transcribe ``path``, splitting first if the engine will not take it whole.

    This is the one call the worker needs: engines with ``max_bytes is None`` go straight
    through, and everything else is split, transcribed piece by piece, reassembled and
    checked against the clock before it is allowed to be a transcript.
    """
    max_bytes = getattr(engine, "max_bytes", None)
    size = os.path.getsize(path)
    if max_bytes is None or size <= max_bytes:
        return engine.transcribe(path, hints)

    duration = duration_s if (duration_s and duration_s > 0) else hints.duration_s
    plan = split_audio(path, max_bytes, duration_s=duration, overlap_s=overlap_s, work_dir=work_dir)
    log.info(
        "%s is %.1fMB, over %s's %.1fMB limit: cut into %d pieces by %s",
        os.path.basename(path), size / 1048576, getattr(engine, "name", "the engine"),
        max_bytes / 1048576, len(plan.pieces), plan.method,
    )
    try:
        results: list[Transcript] = []
        for piece in plan.pieces:
            piece_hints = _hints_for(hints, piece)
            results.append(engine.transcribe(piece.path, piece_hints))
        transcript = stitch(plan, results)
    finally:
        plan.cleanup()
    verify_result_duration(
        transcript,
        plan.duration_s,
        source_name=hints.source_name or os.path.basename(path),
    )
    return transcript


def _hints_for(hints: Hints, piece: Piece) -> Hints:
    """Each piece is offered the same vocabulary, and a filename the engine can read.

    The source name matters: OpenAI identifies the audio format from the filename, and a
    piece called ``piece-002.m4a`` is exactly as informative as the original was.
    """
    stem, ext = os.path.splitext(os.path.basename(hints.source_name or piece.path))
    return Hints(
        vocabulary=hints.vocabulary,
        counterparty=hints.counterparty,
        language=hints.language,
        languages=hints.languages,
        recorded_at=hints.recorded_at,
        source_name=f"{stem}-part{piece.index + 1:02d}{ext or os.path.splitext(piece.path)[1]}",
        duration_s=piece.measured_duration_s,
    )
