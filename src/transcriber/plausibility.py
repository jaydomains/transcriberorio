"""Does this transcript plausibly account for that audio?

A forty-minute recording that comes back as eleven words has failed. Nothing errored: the
engine returned 200, the text is grammatical, the file is intact, and if nobody compares
the words against the seconds it is filed as a success and the recording is gone. That
comparison is this module, and it is the last net in the pipeline — ``audio.py`` catches a
container that is visibly broken; this catches the ones that are not.

Three answers, and the middle one is the point:

  * **plausible** — the words and the seconds agree. Carry on.
  * **implausible** — they do not. Quarantine, loudly, with the arithmetic in the reason.
    Never accepted quietly, never truncated to "we got what we got".
  * **silent** — the engine heard nothing, on audio short enough for that to be true. This
    is a distinct, *visible* state (``SKIPPED_EMPTY``), not a deletion and not a skip: the
    ledger keeps the row, the digest counts it, and a person can look at it.

Every threshold below is a named constant with the reasoning attached, because a bare
number in a comparison is a decision nobody can review. They are conservative in one
direction on purpose: a false quarantine costs a person a minute, and a false pass costs a
recording.

This module decides nothing about the business. It compares words to seconds and reports
what it found; it never concludes anything about what was said.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import AudioInfo, State, Transcript, strip_emails

__all__ = [
    "PLAUSIBLE",
    "IMPLAUSIBLE",
    "SILENT",
    "Thresholds",
    "DEFAULT_THRESHOLDS",
    "Plausibility",
    "assess",
    "count_words",
    "excerpt",
]

PLAUSIBLE = "plausible"
IMPLAUSIBLE = "implausible"
SILENT = "silent"

#: Words per minute below which a transcript cannot be an account of the audio. Ordinary
#: conversation runs 110-160 wpm; a site note into wind, with pauses and walking, still runs
#: well over 60. 25 is far under any human speech and far over what a broken transcript
#: produces — the eleven-words-in-forty-minutes case is 0.3.
MIN_WORDS_PER_MINUTE = 25.0

#: And the other end. Nobody speaks 400 words a minute; an engine stuck in a repetition loop
#: does. It is the same silent-degradation failure wearing the opposite sign.
MAX_WORDS_PER_MINUTE = 400.0

#: Below this many seconds the words-per-minute rate is meaningless — a three-second "ja,
#: approved, go ahead on Beach Court" is three words, and any rate computed from it says
#: nothing. Short recordings are judged on having words at all, and the AI pass is where a
#: twelve-second approval is protected from being treated as trivial.
SHORT_AUDIO_S = 20.0

#: An empty transcript is credible on audio up to this long: a pocket recording, a misfire,
#: a false start, a walk to the car. Beyond it, "no speech at all" is far more likely to be
#: an engine that failed quietly than ninety seconds of genuine silence, so it stops being
#: verified silence and becomes something for a person.
SILENCE_MAX_S = 90.0

#: When an engine returns segments, the last one should land somewhere near the end of the
#: recording. Segments that stop halfway mean the tail was lost — the exact shape of a
#: splitting bug, which shortens a transcript without raising anything. Set low because a
#: genuinely quiet tail (a phone left running in a pocket) is normal and must not fire.
MIN_SEGMENT_COVERAGE = 0.5

#: Distinct words as a fraction of total. Human speech, even repetitive site dictation,
#: does not go near this; a model looping "thank you. thank you. thank you." does. Only
#: applied above LOOP_MIN_WORDS, because a short transcript is legitimately repetitive.
MIN_DISTINCT_WORD_RATIO = 0.08
LOOP_MIN_WORDS = 60

#: How much of the transcript to put in the reason as evidence. Enough for a person to see
#: at a glance what came back, short enough for a digest line.
EXCERPT_CHARS = 160

_WHITESPACE = re.compile(r"\s+")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass(frozen=True)
class Thresholds:
    """The numbers above, overridable in one place for a test or an unusual engine."""

    min_words_per_minute: float = MIN_WORDS_PER_MINUTE
    max_words_per_minute: float = MAX_WORDS_PER_MINUTE
    short_audio_s: float = SHORT_AUDIO_S
    silence_max_s: float = SILENCE_MAX_S
    min_segment_coverage: float = MIN_SEGMENT_COVERAGE
    min_distinct_word_ratio: float = MIN_DISTINCT_WORD_RATIO
    loop_min_words: int = LOOP_MIN_WORDS


DEFAULT_THRESHOLDS = Thresholds()


def count_words(text: str | None) -> int:
    """Words, not tokens: anything with no letter or digit in it is punctuation.

    Counting ``"..."`` and ``"—"`` as words is how a transcript of nothing scores three.
    """
    return sum(1 for token in (text or "").split() if any(ch.isalnum() for ch in token))


def excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str:
    """A short, single-line sample of the transcript for a human to look at.

    Through :func:`strip_emails` without exception: a quarantine reason is written to the
    ledger and read out in the morning digest, and this pipeline emits no address anywhere,
    for any reason — including in evidence of its own failure.
    """
    flat = _WHITESPACE.sub(" ", strip_emails(text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Plausibility:
    """The verdict, the arithmetic behind it, and a sentence a person can act on."""

    verdict: str
    reason: str
    words: int
    duration_s: float
    wpm: float | None = None
    duration_known: bool = True
    checks: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_plausible(self) -> bool:
        return self.verdict == PLAUSIBLE

    @property
    def is_implausible(self) -> bool:
        return self.verdict == IMPLAUSIBLE

    @property
    def is_silent(self) -> bool:
        return self.verdict == SILENT

    @property
    def ledger_state(self) -> str | None:
        """Where this verdict sends the row. ``None`` means stay on the happy path.

        Neither terminal state here is a success, and neither is reached quietly:
        ``QUARANTINED`` wants a person, ``SKIPPED_EMPTY`` records verified silence.
        """
        if self.verdict == IMPLAUSIBLE:
            return State.QUARANTINED
        if self.verdict == SILENT:
            return State.SKIPPED_EMPTY
        return None


def _duration_known(audio: AudioInfo) -> bool:
    """``duration_s == 0`` means "zero seconds" or "would not say"; they differ here.

    ``audio.probe`` records which in ``detail``. An older or hand-built AudioInfo without
    the flag is read the only safe way: a positive duration is known, a zero one is not.
    """
    flag = audio.detail.get("duration_known")
    if flag is None:
        return audio.duration_s > 0
    return bool(flag)


def assess(
    transcript: Transcript,
    audio: AudioInfo,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> Plausibility:
    """Judge a transcript against the audio it claims to be a transcript of."""
    words = count_words(transcript.text)
    duration = max(0.0, float(audio.duration_s or 0.0))
    known = _duration_known(audio)
    minutes = duration / 60.0
    wpm = (words / minutes) if (known and minutes > 0) else None
    sample = excerpt(transcript.text)

    detail: dict[str, Any] = {
        "words": words,
        "duration_s": round(duration, 2),
        "duration_known": known,
        "wpm": round(wpm, 1) if wpm is not None else None,
        "container": audio.container,
        "engine": transcript.engine,
        "language": transcript.language,
        "segments": len(transcript.segments),
        "audio_bytes": audio.size_bytes,
    }
    checks: list[str] = []
    faults: list[str] = []

    # The gate before the gate. A truncated file should have been quarantined before it was
    # ever transcribed; if one reaches here, the answer is not "well, the words look fine".
    if audio.truncated:
        return Plausibility(
            verdict=IMPLAUSIBLE,
            reason=(
                f"the audio itself is not intact, so no transcript of it can be trusted — "
                f"{audio.reason}"
            ),
            words=words,
            duration_s=duration,
            wpm=wpm,
            duration_known=known,
            checks=("audio integrity: failed",),
            detail=detail,
        )
    checks.append("audio integrity: intact")

    # --- nothing came back ---------------------------------------------------------
    if words == 0:
        if not known:
            return Plausibility(
                verdict=IMPLAUSIBLE,
                reason=(
                    f"the engine returned no words and the {audio.container} container does "
                    f"not say how long the recording is, so silence cannot be verified — this "
                    f"needs a person, not a guess"
                ),
                words=0,
                duration_s=duration,
                duration_known=False,
                checks=tuple(checks + ["empty transcript, duration unknown"]),
                detail=detail,
            )
        if duration <= thresholds.silence_max_s:
            return Plausibility(
                verdict=SILENT,
                reason=(
                    f"{duration:.1f}s of audio and no speech in it — recorded as verified "
                    f"silence, not deleted and not marked done. Engine "
                    f"{transcript.engine or 'unknown'} returned an empty transcript for "
                    f"{audio.size_bytes} bytes of {audio.container}"
                ),
                words=0,
                duration_s=duration,
                wpm=0.0,
                duration_known=True,
                checks=tuple(checks + [f"empty transcript on {duration:.1f}s: within the silence window"]),
                detail=detail,
            )
        return Plausibility(
            verdict=IMPLAUSIBLE,
            reason=(
                f"{duration:.1f}s of audio and not one word came back. That is longer than "
                f"{thresholds.silence_max_s:.0f}s of plausible silence, so this is far more "
                f"likely a failed transcription than a silent recording"
            ),
            words=0,
            duration_s=duration,
            wpm=0.0,
            duration_known=True,
            checks=tuple(checks + [f"empty transcript on {duration:.1f}s: beyond the silence window"]),
            detail=detail,
        )

    # --- words came back, but do they account for the audio? ------------------------
    if not known:
        checks.append("word density: not checked, the container declares no duration")
        return Plausibility(
            verdict=PLAUSIBLE,
            reason=(
                f"{words} words from {audio.container}, which declares no duration — the "
                f"density check could not be run. Accepted on the words alone: “{sample}”"
            ),
            words=words,
            duration_s=duration,
            duration_known=False,
            checks=tuple(checks),
            detail=detail,
        )

    if duration <= 0:
        faults.append(
            f"the container reports zero seconds of audio yet the engine returned "
            f"{_plural(words, 'word')} — one of the two is wrong"
        )
    elif duration <= thresholds.short_audio_s:
        checks.append(
            f"word density: not checked, {duration:.1f}s is under the {thresholds.short_audio_s:.0f}s "
            f"floor where a rate means anything"
        )
    else:
        rate = words / minutes
        if rate < thresholds.min_words_per_minute:
            faults.append(
                f"{_plural(words, 'word')} for {duration / 60:.1f} minutes of audio is "
                f"{rate:.1f} words a minute, against a floor of "
                f"{thresholds.min_words_per_minute:.0f} — the "
                f"transcript does not account for the recording"
            )
        elif rate > thresholds.max_words_per_minute:
            faults.append(
                f"{words} words in {duration / 60:.1f} minutes is {rate:.0f} words a minute, "
                f"faster than anyone speaks — the engine is repeating itself rather than "
                f"transcribing"
            )
        else:
            checks.append(f"word density: {rate:.0f} words a minute")

    # A transcript whose segments stop halfway through the audio has lost its tail — the
    # signature of a splitting bug, which never raises anything on its own.
    if transcript.segments and duration > thresholds.short_audio_s:
        covered = transcript.covered_duration_s
        coverage = covered / duration if duration else 0.0
        detail["segment_coverage"] = round(coverage, 3)
        if coverage < thresholds.min_segment_coverage:
            faults.append(
                f"the segments account for {covered:.0f}s of a {duration:.0f}s recording "
                f"({coverage * 100:.0f}%) — the end of the audio is not in the transcript"
            )
        else:
            checks.append(f"segment coverage: {coverage * 100:.0f}% of the recording")

    # Repetition, measured rather than eyeballed. Only above a word count where a low
    # distinct ratio cannot be ordinary speech.
    if words >= thresholds.loop_min_words:
        tokens = [t.strip(".,;:!?\"'()[]").lower() for t in (transcript.text or "").split()]
        tokens = [t for t in tokens if any(ch.isalnum() for ch in t)]
        ratio = (len(set(tokens)) / len(tokens)) if tokens else 1.0
        detail["distinct_word_ratio"] = round(ratio, 3)
        if ratio < thresholds.min_distinct_word_ratio:
            faults.append(
                f"only {_plural(len(set(tokens)), 'distinct word')} in {len(tokens)} — the "
                f"transcript is a loop, not speech"
            )
        else:
            checks.append(
                f"vocabulary: {_plural(len(set(tokens)), 'distinct word')} in {len(tokens)}"
            )

    if faults:
        return Plausibility(
            verdict=IMPLAUSIBLE,
            reason="; ".join(faults) + f". What came back: “{sample}”",
            words=words,
            duration_s=duration,
            wpm=wpm,
            duration_known=True,
            checks=tuple(checks),
            detail=detail,
        )
    return Plausibility(
        verdict=PLAUSIBLE,
        reason=(
            f"{words} words over {duration:.1f}s of {audio.container}"
            + (f" — {wpm:.0f} words a minute" if wpm is not None and duration > thresholds.short_audio_s
               else " — too short to rate, words present")
        ),
        words=words,
        duration_s=duration,
        wpm=wpm,
        duration_known=True,
        checks=tuple(checks),
        detail=detail,
    )
