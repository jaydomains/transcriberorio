"""Cutting held passages out of the transcript text, before any file is written.

Three things about this module are load-bearing, and each one was got wrong somewhere in the
investigation before it was got right:

**1. The cut lands on the transcript text.** Not on the actions file. The actions file is
deliberately named so the record never ingests it, so a mask applied there protects nothing:
only the transcript reaches ``kbc-site-memory``. :func:`redact_transcript` therefore takes
the :class:`~transcriber.models.Transcript` — text *and* segments, because the rendered body
is built from the segments when an engine returned them — and everything downstream is
rendered from what it returns.

**2. The marker is a stated unknown, not a black box.** The record's read path is built from
six sources and our inbox is not one of them, so a marker that only sits in the transcript
file is invisible to the assistant answering a question on site. What *is* read is
``09-portfolio/12-ask-james.md``, fed by ``tools/transcripts.py:questions_in`` — a scan for
questions and stated unknowns, quoted verbatim, filed as proposals. So the marker is written
to be caught by that scan: a short sentence saying something was held, then a question
naming the reference and the date. The result is that the site's live page says *"a staff
matter was recorded on 24 Aug 2026 and is held pending review"* instead of the assistant
saying *"there is no record of that"*. A confident answer built on a quietly partial record
is worse than the leak it prevents.

  The shape of that scan constrains the wording, and the constraint is mechanical: its
  question pattern captures a run of 15–240 characters containing no ``.``, ``!``, ``?`` or
  newline and ending in ``?``, starting at a line break or after a full stop. Hence the
  marker's one internal full stop, hence no abbreviations, no decimals and no other question
  marks anywhere in it. :func:`marker_for` is the only place that wording exists, and
  :func:`harvestable` is the check that it still is what the record will pick up.

**3. Quote verification must not become a shredder.** ``extract.py`` verifies that every
extracted item's quote genuinely appears in the transcript — the guard that stops a misheard
word hardening into a task — and ``outputs.py`` checks it a second time at render. Redact
the transcript and leave the items alone and both checks fail on real items, which are then
discarded: a redaction that silently destroys action items. The answer, in full, because it
is the part most likely to be undone by somebody later:

  *Verify against the unredacted text, then redact the items with the same spans and the
  same markers.* Extraction and verification run first, on the text as transcribed, because
  their question is "did the model invent this?" and only the unredacted text can answer it.
  The gate then rewrites each item's quote through :meth:`Redaction.apply_to_quote`, which
  replaces exactly the held part of the quote with exactly the marker that replaced it in
  the transcript. The rewritten quote is therefore still a literal substring of the
  published transcript, so ``outputs._refuse_unverified`` passes for the right reason rather
  than by being weakened, no item is discarded, and no held word rides out on a quote. An
  item whose quote lies *entirely* inside a held span is a different case: it is wholly
  derived from held words, so it is withheld as an item — kept, listed as held, never
  silently dropped, and never written into a file.

And the two properties everything above rests on: cutting twice does not corrupt (a span
whose marker is already present is skipped), and the offsets are checked rather than
trusted — a span whose recorded offsets do not hold the words it claims is located by its
words instead, and if it cannot be found at all it is reported rather than guessed at.
Nothing here decides anything: in ``shadow`` and ``off`` this module cuts nothing at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from .models import Segment, Transcript, utc_now_iso
from .withheld import (
    CATEGORY_PHRASE,
    MODE_ON,
    HeldRecord,
    HeldSpan,
    normalise_mode,
)

__all__ = [
    "RedactionError",
    "marker_for",
    "MARKER_RE",
    "FULL_MARKER_RE",
    "PART_MARKER_RE",
    "part_marker_for",
    "refs_in",
    "harvestable",
    "held_preamble",
    "Applied",
    "Redaction",
    "redact_text",
    "redact_transcript",
    "redact_segments",
    "redact_extraction",
    "ItemOutcome",
    "contains_any_held",
    "held_words_in",
    "restore_released",
    "spans_of",
]


class RedactionError(Exception):
    """The redactor cannot do the job it was asked to do, and will not pretend otherwise.

    Raised only for the case where a marker this module wrote could not be read back as the
    stated unknown the record harvests. That is the one failure that would be invisible
    afterwards: the words would still be cut, and the record would simply stop saying that
    anything had been held. A recording that cannot announce its own hole must not publish.
    """


# -- the marker ----------------------------------------------------------------------

#: Just enough to find a marker — whole or continued — and read its reference back out.
MARKER_RE = re.compile(r"\[held (?P<ref>[0-9A-F]{6})(?: continues)?\]")

#: The whole marker, for putting the words back when a passage is released. Brackets are
#: excluded from the middle so one marker can never swallow the next.
FULL_MARKER_RE = re.compile(
    r"\[held (?P<ref>[0-9A-F]{6})\][^\[\]]{0,400}?may it be released into the record\?"
)

#: The tail of a held passage that straddles two speaker segments. The full marker goes
#: where the passage began and says everything; repeating all of it on the next line would
#: say the same sentence twice and — worse — would put the words back twice when the passage
#: is released.
PART_MARKER_RE = re.compile(r"\[held (?P<ref>[0-9A-F]{6}) continues\]")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: The tail of the question, also the thing FULL_MARKER_RE anchors on. One constant so the
#: wording and the pattern that finds it cannot drift apart.
_QUESTION_TAIL = "may it be released into the record?"


def _human_day(stamp: str, fallback: str = "") -> str:
    """``2026-08-24T09:12:00Z`` -> ``24 Aug 2026``.

    Written out with a month table rather than ``strftime``: ``%b`` follows the machine's
    locale, and a marker that says ``24 Aug`` on one host and ``24 aug.`` on another would
    put a full stop inside the sentence the record's question scan is trying to read.
    """
    text = str(stamp or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            year, month, day = int(text[:4]), int(text[5:7]), int(text[8:10])
            if 1 <= month <= 12:
                return f"{day} {_MONTHS[month - 1]} {year}"
        except ValueError:
            pass
    return fallback


def _sentence_case(phrase: str) -> str:
    return phrase[:1].upper() + phrase[1:] if phrase else phrase


def marker_for(span: HeldSpan | HeldRecord, *, held_on: str = "") -> str:
    """What replaces the words: dated, categorised, referenced and requestable.

    Never a bare ``[redacted]``. A reader — a person, or the assistant answering from the
    record — has to be able to see *that* something was held, *what kind* of thing it was,
    *when* it was said and *how to ask for it*, because a hole that says nothing is
    indistinguishable from a recording where nothing was said.

    The date is the day the recording was made where we know it, not the day the gate ran:
    what a person needs to place is the conversation.
    """
    ref = span.ref
    # The classifier's own public subject where it gave one — "a rate for the remedial" —
    # and the category's dull phrase where it did not. The subject is checked over there for
    # names, figures and addresses before it is allowed to stand in for the words.
    phrase = span.phrase or CATEGORY_PHRASE.get(span.category, "something held for review")
    day = _human_day(getattr(span, "recorded_at", ""), "") or _human_day(held_on or utc_now_iso(), "an earlier date")
    return (
        f"[held {ref}] {_sentence_case(phrase)} was recorded on {day} and is held pending "
        f"review, so the words are not written here. "
        f"What was said in held passage {ref} on {day}, and {_QUESTION_TAIL}"
    )


def part_marker_for(span: HeldSpan | HeldRecord) -> str:
    """What marks the rest of a passage whose beginning is marked on the line above."""
    return f"[held {span.ref} continues]"


def refs_in(text: str) -> tuple[str, ...]:
    """Every hold reference already present in this text, in order, de-duplicated."""
    seen: list[str] = []
    for match in MARKER_RE.finditer(text or ""):
        ref = match.group("ref")
        if ref not in seen:
            seen.append(ref)
    return tuple(seen)


#: A faithful copy of ``kbc-site-memory/tools/transcripts.py:questions_in``'s question
#: pattern. Copied rather than imported for the same reason ``tests/vendored_ingest.py``
#: vendors the record's parser: that repository is not importable from here, and the
#: property we need — that the marker survives into the record's question list — has to be
#: checkable offline, in a unit test, without a network or a checkout.
_RECORD_QUESTION_RE = re.compile(r"(?:^|(?<=[.!?\n]))\s*([^.!?\n]{15,240}\?)", re.M)


def held_preamble(
    spans: Sequence[HeldSpan | HeldRecord],
    *,
    held_on: str = "",
    site: str = "",
) -> list[str]:
    """The held-passage block that goes at the TOP of the transcript, before the body.

    The in-place marker is what makes a hole readable where it happened, and it is not
    enough on its own. The record harvests every question in a transcript, sorts them by
    where they appear in the body, and files **the first twenty**: ``qs[:20]`` in
    ``kbc-site-memory/tools/transcripts.py``. A hold marker sits inside "What was said", so
    it sorts after everything asked earlier on the call — and the record's own docstring
    says a site walk produces forty questions. On exactly the long site meetings where a
    hold is most likely, every hold question fell off the end of the cap: the transcript
    carried its markers, the site's live page carried nothing, and the assistant answered a
    client confidently from a record it did not know was partial. That is the failure the
    brief calls worse than the leak it prevents.

    So the same question is stated once more, ahead of the body, where it cannot be pushed
    off. It is not a second question: the record de-duplicates on the question's own text,
    and each line here is built from :func:`marker_for` through :func:`harvestable`, so it
    is character-for-character the sentence the marker carries. Being earlier in the body,
    it is the copy that survives — and the marker below it is the copy that says *where*.

    One wording, one source. If ``marker_for`` ever changes, this changes with it, and
    :func:`harvestable` is what proves the record can still lift either.
    """
    wanted = spans_of(spans)
    if not wanted:
        return []
    stamp = held_on or utc_now_iso()
    where = f" at {site}" if str(site or "").strip() else ""
    count = len(wanted)
    lines = [
        "## Passages held for review",
        "",
        f"{count} passage{'' if count == 1 else 's'} of this recording{where} "
        f"{'was' if count == 1 else 'were'} taken out and {'is' if count == 1 else 'are'} "
        "waiting for a person to approve them. Each is marked in place below, where it was "
        "said. Nothing was deleted, and nothing is released until somebody says so.",
        "",
    ]
    for span in wanted:
        marker = marker_for(span, held_on=stamp)
        question = harvestable(marker)
        if not question:
            # Unreachable while marker_for and harvestable agree, and asserted rather than
            # assumed: a recording that cannot announce its own hole must not publish.
            raise RedactionError(
                f"the marker written for held passage {span.ref} is not phrased as a "
                "question the record can harvest, so the record would show no sign that "
                "anything was held. Nothing has been written."
            )
        day = _human_day(getattr(span, "recorded_at", ""), "") or _human_day(stamp, "an earlier date")
        lines.append(
            f"- Held passage {span.ref}, {span.phrase}, recorded {day}. {question}"
        )
    lines.append("")
    return lines


def harvestable(marker: str) -> str:
    """The question the record's harvester would lift out of this marker, or ``''``.

    Used as an assertion rather than as behaviour: if this ever returns empty, the marker
    still cuts the words but the record stops saying that anything was held, and the whole
    point of phrasing it as a stated unknown has quietly gone.
    """
    found = _RECORD_QUESTION_RE.findall(marker or "")
    for candidate in found:
        text = candidate.strip()
        if _QUESTION_TAIL in text:
            return text
    return ""


# -- text matching -------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ']+")

#: How many consecutive words of a held passage may appear in a file before that file is
#: refused. Five is chosen to sit above ordinary coincidence ("and we will have to") and
#: below any real fragment of a held passage.
LEAK_MIN_RUN = 5

#: A single word that gives the game away on its own: eight digits or more is an ID number,
#: a bank account or a phone number, which is exactly the ``bare-identifier`` category.
#: Deliberately not "any word with a digit in it" — ``R1.65m`` is a price, prices flow, and
#: a backstop that refused every file mentioning one would be a gate switched off by Friday.
_ID_DIGITS = 8


def _char_class(ch: str) -> str:
    """One character of a needle, as a pattern that also matches its other shapes."""
    if ch in "'’‘`´":
        return "['’‘`´]"
    if ch in '"“”':
        return '["“”]'
    if ch in "-–—":
        return "[-–—]"
    return re.escape(ch)


def _flexible(needle: str) -> re.Pattern[str] | None:
    """``needle`` as a pattern tolerant of whitespace, case and quote or dash shapes.

    Nothing else is relaxed. No word is dropped, reordered or stemmed, for the same reason
    ``extract.verify_quote`` refuses to: "unit 4" and "unit 14" must not become the same
    string, and a redactor that matched loosely would cut a sentence nobody held.
    """
    chunks = [c for c in _WS_RE.split((needle or "").strip()) if c]
    if not chunks:
        return None
    body = r"\s+".join("".join(_char_class(ch) for ch in chunk) for chunk in chunks)
    try:
        return re.compile(body, re.IGNORECASE)
    except re.error:  # pragma: no cover - _char_class escapes everything it does not map
        return None


def _marker_ranges(text: str) -> list[tuple[int, int]]:
    """Where the markers already in this text begin and end.

    Everything that cuts or looks for held words skips these. A marker is this module's own
    sentence, not the recording's, and a held passage that happened to start with words the
    marker also uses — "was recorded", "held pending" — would otherwise have a second pass
    cut a hole in the first pass's marker, which is exactly the corruption idempotence is
    supposed to rule out.
    """
    ranges = [(m.start(), m.end()) for m in FULL_MARKER_RE.finditer(text or "")]
    ranges += [(m.start(), m.end()) for m in PART_MARKER_RE.finditer(text or "")]
    return _merge(ranges)


def _inside(start: int, end: int, protected: Sequence[tuple[int, int]]) -> bool:
    return any(start < p_end and end > p_start for p_start, p_end in protected)


def _outside(ranges: Sequence[tuple[int, int]], protected: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    return [
        (start, end)
        for start, end in ranges
        if not any(start < p_end and end > p_start for p_start, p_end in protected)
    ]


def _find_all(text: str, needle: str) -> list[tuple[int, int]]:
    pattern = _flexible(needle)
    if pattern is None or not text:
        return []
    found = [(m.start(), m.end()) for m in pattern.finditer(text)]
    return _outside(found, _marker_ranges(text))


def _words(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).casefold(), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def _common_runs(haystack: Sequence[tuple[str, int, int]], needle: Sequence[str]) -> list[tuple[int, int, int]]:
    """Every maximal run of ``needle``'s words appearing in order in ``haystack``.

    Returned as ``(index in haystack, index in needle, length)``. Written out rather than
    handed to :class:`difflib.SequenceMatcher` because that produces one alignment of the
    two sequences, and what is wanted here is every place the held words surface — including
    the second time the same phrase is said.
    """
    positions: dict[str, list[int]] = {}
    for index, word in enumerate(needle):
        positions.setdefault(word, []).append(index)
    runs: list[tuple[int, int, int]] = []
    i = 0
    limit = len(haystack)
    while i < limit:
        best_length = 0
        best_j = -1
        for j in positions.get(haystack[i][0], ()):
            length = 0
            while (
                i + length < limit
                and j + length < len(needle)
                and haystack[i + length][0] == needle[j + length]
            ):
                length += 1
            if length > best_length:
                best_length, best_j = length, j
        if best_length:
            runs.append((i, best_j, best_length))
            i += best_length
        else:
            i += 1
    return runs


def _run_is_a_leak(run: tuple[int, int, int], needle: Sequence[str], min_run: int) -> bool:
    _, j, length = run
    if length >= min_run or length >= len(needle):
        return True
    if length / max(1, len(needle)) >= 0.7:
        return True
    if length == 1:
        word = needle[j]
        return sum(ch.isdigit() for ch in word) >= _ID_DIGITS
    return False


def _run_is_an_edge(run: tuple[int, int, int], needle: Sequence[str]) -> bool:
    """True when the run is a prefix or a suffix of the held passage.

    That is the shape a held span takes when it straddles two segments: the first segment
    ends with its opening words and the second starts with the rest. Restricting the cut to
    that shape is what stops an incidental four-word coincidence in the middle of a long
    passage from taking an innocent sentence with it.
    """
    _, j, length = run
    return j == 0 or j + length == len(needle)


def _merge(ranges: Sequence[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def held_words_in(
    text: str,
    spans: Iterable[HeldSpan | HeldRecord],
    *,
    min_run: int = LEAK_MIN_RUN,
) -> list[tuple[Any, str, str]]:
    """Every held passage still readable in ``text``, with the words found and why.

    Three ways a held passage can still be there, and all three count: the whole passage;
    a run of at least ``min_run`` of its consecutive words, or most of it when it is short;
    and a single word carrying eight digits or more, which is an identity number rather
    than a figure.
    """
    found: list[tuple[Any, str, str]] = []
    if not text:
        return found
    protected = _marker_ranges(text)
    haystack = [w for w in _words(text) if not _inside(w[1], w[2], protected)]
    for span in spans:
        words = str(getattr(span, "text", "") or "")
        if not words.strip():
            continue
        whole = _find_all(text, words)
        if whole:
            start, end = whole[0]
            found.append((span, text[start:end], "the whole held passage is still here"))
            continue
        needle = [w for w, _, _ in _words(words)]
        if not needle:
            continue
        for run in _common_runs(haystack, needle):
            if not _run_is_a_leak(run, needle, min_run):
                continue
            i, _, length = run
            start, end = haystack[i][1], haystack[i + length - 1][2]
            found.append((span, text[start:end], f"{length} consecutive words of it are still here"))
            break
    return found


def contains_any_held(text: str, spans: Iterable[HeldSpan | HeldRecord]) -> bool:
    """The mechanical backstop: does this text still contain any held words?

    Every module about to write a file calls this and refuses to publish if it answers yes.
    It is deliberately a dumb, separate check rather than trust in the redactor having
    worked: the redactor is the thing that might have a bug, and a guard that shares its
    reasoning guards nothing.
    """
    return bool(held_words_in(text, spans))


# -- the redaction ---------------------------------------------------------------------


@dataclass(frozen=True)
class Applied:
    """One cut that was actually made: where, what replaced it, and by which span."""

    span: HeldSpan
    start: int
    end: int
    marker: str
    located_by: str = "offsets"    # "offsets" | "words" | "words-elsewhere"

    @property
    def ref(self) -> str:
        return self.span.ref


@dataclass(frozen=True)
class ItemOutcome:
    """What the gate did to one extracted item, and why."""

    item: Any
    action: str          # "unchanged" | "masked" | "held"
    reason: str = ""
    refs: tuple[str, ...] = ()

    @property
    def publishable(self) -> bool:
        return self.action != "held"


@dataclass
class Redaction:
    """The result of cutting one transcript, and the means to cut everything derived from it.

    ``source`` is the *unredacted* text. It is kept because the item pass needs to locate a
    quote in the words as transcribed, and it must never be written to a file, logged, or
    uploaded — which is why both texts are left out of this object's ``repr``.
    """

    mode: str
    text: str = field(repr=False, default="")
    source: str = field(repr=False, default="")
    spans: tuple[HeldSpan, ...] = ()
    applied: tuple[Applied, ...] = ()
    already: tuple[HeldSpan, ...] = ()
    unapplied: tuple[tuple[HeldSpan, str], ...] = ()
    held_on: str = ""

    @property
    def cut(self) -> int:
        return len(self.applied)

    @property
    def armed(self) -> bool:
        """True only when this run actually withheld anything. Shadow cuts nothing."""
        return self.mode == MODE_ON

    @property
    def ok(self) -> bool:
        """Every span this run was given was either cut or already cut."""
        return not self.unapplied

    @property
    def markers(self) -> dict[str, str]:
        return {a.ref: a.marker for a in self.applied}

    @property
    def cut_spans(self) -> tuple[HeldSpan, ...]:
        return tuple(a.span for a in self.applied)

    def problems(self) -> list[str]:
        """Everything a person has to be told about this run, in plain words.

        Never empty just because a run went badly and never invented when it went well: a
        span that could not be found is the one case where the service neither withheld nor
        published silently, and it says so here so the caller can quarantine rather than
        guess.
        """
        out: list[str] = []
        for span, why in self.unapplied:
            out.append(
                f"a passage held as {span.ref} ({span.phrase}) could not be found in the "
                f"transcript to cut it out — {why}"
            )
        return out

    def check_publishable(self, text: str) -> list[str]:
        """Problems with writing ``text`` out, given what this run held. Empty means clean.

        In shadow and off it is always empty: nothing was withheld, so there is nothing that
        could leak, and a backstop that fired in shadow would stop the measurement run dead.

        **Nothing returned from here quotes the passage.** These strings are raised as a
        fault, written to the ledger's ``quarantine_reason``, printed under "Technical
        detail:" in the 06:00 email and logged at ERROR — four places a held passage may
        never reach. Quoting the words here would put a staff matter in James's inbox on
        exactly the occasions the masker has a bug, which is when it matters most, and an
        ordinary trigger is somebody repeating themselves on a call. The reference and the
        category's public phrase are enough to find the recording, and both are already
        printed in the transcript beside the marker.
        """
        if not self.armed:
            return []
        return [
            f"held passage {getattr(span, 'ref', '?')} "
            f"({getattr(span, 'phrase', 'a held passage')}) is still readable in this file "
            f"— {why}"
            for span, _words, why in held_words_in(text, self.cut_spans)
        ]

    # -- deriving ------------------------------------------------------------------

    def mask(
        self,
        text: str,
        *,
        min_run: int = 2,
        continuations: bool = False,
    ) -> tuple[str, tuple[str, ...]]:
        """Replace any held words in an arbitrary string with their markers.

        Used on everything a model wrote about the recording — a summary, an item's own
        prose, the nearest-passage note on a rejected quote. Three passes, in order:

        1. Whole occurrences of the passage.
        2. A run that is a prefix or a suffix of it, down to ``min_run`` words. That is the
           shape a passage takes when it straddles two speaker segments, and cutting it
           there is cheap because an incidental short run at an edge is still an edge.
        3. **Any run the backstop would refuse, wherever it sits in the passage.**

        The third pass is not belt-and-braces, it is the thing that keeps this method and
        :func:`held_words_in` from disagreeing. The backstop refuses a file over a run of
        :data:`LEAK_MIN_RUN` consecutive held words *anywhere*; masking only the edges left
        a window where a model's summary reusing the middle of a held sentence — which is
        what summarising a held staff matter looks like almost every time — was not masked
        here and was then refused there. That refusal is
        :class:`transcriber.outputs.HeldTextWouldLeak`, which the pipeline never retries, so
        the whole recording quarantined permanently: no transcript, no summary, no actions.
        The failure direction is safe and the cost is the one thing James was promised he
        would not pay — nothing about a passage awaiting approval delays the transcript.

        So the rule is: **whatever the backstop would refuse, this masks first.** The two
        share ``_run_is_a_leak``, and ``selftest`` asserts the coverage relation directly.
        """
        if not self.armed or not (text or "").strip():
            return text, ()
        out = text
        touched: list[str] = []
        for applied in self.applied:
            words = applied.span.text
            if not words.strip():
                continue
            part = part_marker_for(applied.span) if continuations else applied.marker
            found = _find_all(out, words)
            if found:
                for start, end in reversed(found):
                    out = out[:start] + applied.marker + out[end:]
                touched.append(applied.ref)
                continue
            cut, did = _cut_runs(
                out, words, applied.marker, part_marker=part, min_run=min_run, edges_only=True,
            )
            if did:
                out = cut
            # The backstop's own threshold, over the whole passage rather than its edges.
            # Runs the pass above already cut are gone from ``out``, and the marker it left
            # is protected, so this only ever reaches what the first pass did not.
            cut, did_interior = _cut_runs(
                out, words, applied.marker, part_marker=part,
                min_run=LEAK_MIN_RUN, edges_only=False,
            )
            if did_interior:
                out = cut
            if did or did_interior:
                touched.append(applied.ref)
        return out, tuple(dict.fromkeys(touched))

    def apply_to_quote(self, quote: str) -> str:
        """One extracted item's verbatim quote, with exactly the held part replaced.

        The quote was verified against the unredacted transcript, which is the only text
        that can answer "did the model invent this?". This rewrites it so that what is
        published quotes what the published transcript actually says: the same words either
        side, the same marker in the middle. It stays a literal substring of the transcript
        as written out, so the render-time check in ``outputs.py`` passes because it is true
        and not because it was loosened.
        """
        if not self.armed or not (quote or "").strip() or not self.applied:
            return quote
        for start, end in _find_all(self.source, quote):
            overlapping = [a for a in self.applied if a.start < end and a.end > start]
            if not overlapping:
                continue
            if self.source[start:end] == quote:
                # The quote is the transcript's own characters, so the cut can be made in
                # the quote itself and nothing else about it changes — including an address
                # the item had already had removed, which rebuilding from the transcript
                # would quietly put back.
                pieces: list[str] = []
                cursor = 0
                for applied in sorted(overlapping, key=lambda a: a.start):
                    local_start = max(applied.start, start) - start
                    local_end = min(applied.end, end) - start
                    if local_start < cursor:
                        local_start = cursor
                    if local_end <= local_start:
                        continue
                    pieces.append(quote[cursor:local_start])
                    pieces.append(applied.marker)
                    cursor = local_end
                pieces.append(quote[cursor:])
                return "".join(pieces)
            break
        # Whitespace or punctuation differ from the transcript, or the quote was matched
        # fuzzily in the first place: fall back to cutting the held words out of the quote
        # itself, which keeps the item's own characters and still removes the passage.
        return self.mask(quote)[0]

    def wholly_held(self, quote: str) -> HeldSpan | None:
        """The span that swallows this quote entirely, if one does.

        An item quoting only held words is wholly derived from them: masking its quote would
        leave a proposal whose evidence is a marker, and whose own sentence is the model's
        paraphrase of a staff matter. Those are withheld as items instead.
        """
        if not self.armed or not (quote or "").strip():
            return None
        for start, end in _find_all(self.source, quote):
            for applied in self.applied:
                if applied.start <= start and applied.end >= end:
                    return applied.span
        stripped = (quote or "").strip()
        for applied in self.applied:
            if stripped and stripped.casefold() in applied.span.text.casefold():
                return applied.span
        return None


def _cut_runs(
    text: str,
    words: str,
    marker: str,
    *,
    part_marker: str = "",
    min_run: int,
    edges_only: bool,
) -> tuple[str, bool]:
    """Cut the runs of ``words`` that appear in ``text``, replacing each with a marker.

    A run that starts at the passage's first word gets the full marker; a run that is the
    tail of it — the other half of a passage split across two speaker segments — gets
    ``part_marker`` where one is given, so the explanation is written once.
    """
    protected = _marker_ranges(text)
    haystack = [w for w in _words(text) if not _inside(w[1], w[2], protected)]
    needle = [w for w, _, _ in _words(words)]
    if not haystack or not needle:
        return text, False
    ranges: list[tuple[int, int]] = []
    for run in _common_runs(haystack, needle):
        i, _, length = run
        if length < min_run and length < len(needle):
            continue
        if not _run_is_a_leak(run, needle, min_run):
            continue
        if edges_only and length < len(needle) and not _run_is_an_edge(run, needle):
            continue
        _, j, _ = run
        whole = length >= len(needle)
        ranges.append((haystack[i][1], haystack[i + length - 1][2], j == 0 or whole))
    if not ranges:
        return text, False
    out = text
    merged = _merge([(s, e) for s, e, _ in ranges], gap=3)
    for start, end in reversed(merged):
        opens = any(flag for r_start, r_end, flag in ranges if r_start >= start and r_end <= end)
        replacement = marker if opens or not part_marker else part_marker
        out = out[:start] + replacement + out[end:]
    return out, True


def spans_of(items: Iterable[HeldSpan | HeldRecord]) -> tuple[HeldSpan, ...]:
    """Whatever the caller has — spans or stored records — as spans.

    A classifier finding is deliberately *not* accepted here. It carries no recording id,
    and the reference printed in the marker is derived from one: converting a finding
    without it would put a marker in the transcript under a reference the store has never
    heard of. :func:`transcriber.withheld.held_spans_from` is the conversion, and it asks
    for the recording.
    """
    out: list[HeldSpan] = []
    for item in items:
        if isinstance(item, HeldRecord):
            out.append(item.as_span())
        elif isinstance(item, HeldSpan):
            out.append(item)
        else:
            raise TypeError(
                f"{type(item).__name__} is not a held span. A classifier finding becomes one "
                "through withheld.held_spans_from(findings, item_id=...), which is where the "
                "recording, the reviewer and the reference are attached"
            )
    return tuple(out)


def redact_text(
    text: str,
    spans: Sequence[HeldSpan | HeldRecord],
    *,
    mode: str = MODE_ON,
    held_on: str = "",
) -> Redaction:
    """Cut every held span out of ``text`` and put its marker in the gap.

    Exact on offsets, and suspicious of them: a span is cut at its recorded offsets only
    when the text there is the text it says it holds. Otherwise the words are searched for
    and the occurrence nearest the recorded offsets is cut, because an offset computed
    against a slightly different version of the transcript is a plausible mistake and
    cutting at it blindly takes the wrong words.

    Idempotent: a span whose marker is already in the text is left alone, so redacting twice
    changes nothing and re-publishing a corrected transcript does not double-cut it.

    In ``shadow`` and ``off`` nothing is cut at all — the spans come back as the run's
    record of what it *would* have held, and the text is returned unchanged.
    """
    mode = normalise_mode(mode, MODE_ON)
    wanted = spans_of(spans)
    stamp = held_on or utc_now_iso()
    if mode != MODE_ON:
        return Redaction(mode=mode, text=text, source=text, spans=wanted, held_on=stamp)

    present = set(refs_in(text))
    applied: list[Applied] = []
    already: list[HeldSpan] = []
    unapplied: list[tuple[HeldSpan, str]] = []
    taken: list[tuple[int, int]] = []

    for span in wanted:
        marker = marker_for(span, held_on=stamp)
        if not harvestable(marker):
            # Asserted where the marker is written, not only where one is inspected. If the
            # wording ever drifts out of the record's question scan, the words still get cut
            # and the record silently stops saying anything was held — which is the whole
            # point of phrasing it as a stated unknown, gone without a symptom. Reported as
            # an unapplied span so the established path takes over: nothing is cut, nothing
            # is published, and a person is told in plain words.
            unapplied.append(
                (
                    span,
                    "the marker written for it could not be read back as a question the "
                    "record will pick up, so cutting the words would leave the record with "
                    "no sign that anything was held",
                )
            )
            continue
        located = _locate(text, span)
        if located is None:
            if span.ref in present:
                already.append(span)
            else:
                unapplied.append(
                    (
                        span,
                        "the words it holds are not in this transcript at the offsets it "
                        "recorded or anywhere else in it",
                    )
                )
            continue
        (start, end), how = located
        if any(start < t_end and end > t_start for t_start, t_end in taken):
            # Two spans over the same words. The first cut already removed them, and cutting
            # again would put a marker inside a marker.
            already.append(span)
            continue
        taken.append((start, end))
        applied.append(Applied(span=span, start=start, end=end, marker=marker, located_by=how))

    order = sorted(applied, key=lambda a: a.start)
    pieces: list[str] = []
    cursor = 0
    for item in order:
        pieces.append(text[cursor : item.start])
        pieces.append(item.marker)
        cursor = item.end
    pieces.append(text[cursor:])
    cut_text = "".join(pieces)

    # The same words said twice. The classifier flagged one occurrence; leaving the other
    # would mean the transcript still says what was held, with a marker beside it claiming
    # otherwise. Only whole, exact repeats are taken, so nothing is cut that is not itself
    # the held passage.
    for item in order:
        for start, end in reversed(_find_all(cut_text, item.span.text)):
            cut_text = cut_text[:start] + item.marker + cut_text[end:]

    return Redaction(
        mode=mode,
        text=cut_text,
        source=text,
        spans=wanted,
        applied=tuple(order),
        already=tuple(already),
        unapplied=tuple(unapplied),
        held_on=stamp,
    )


def _locate(text: str, span: HeldSpan) -> tuple[tuple[int, int], str] | None:
    """Where this span's words actually are in this text, and how that was decided."""
    start, end = int(span.start), int(span.end)
    if 0 <= start < end <= len(text) and text[start:end] == span.text:
        return (start, end), "offsets"
    found = _find_all(text, span.text)
    if not found:
        return None
    nearest = min(found, key=lambda pair: abs(pair[0] - start))
    return nearest, "words"


def redact_segments(
    segments: Sequence[Segment],
    redaction: Redaction,
) -> tuple[list[Segment], list[str]]:
    """Cut the same passages out of the speaker-labelled segments.

    The segments are not a copy of the transcript text that can be ignored: when the engine
    returned them, ``outputs.render_transcript`` builds the published body out of them and
    the flat text is never written. A redaction applied to the text alone would look
    complete and publish the held words line by line.

    Offsets do not carry across — a segment's text is its own string — so each held passage
    is matched by its words, whole first, then as a prefix or suffix run for the case where
    it straddles two segments.
    """
    out: list[Segment] = []
    problems: list[str] = []
    if not redaction.armed:
        return list(segments), problems
    for segment in segments:
        text = segment.text or ""
        masked, _ = redaction.mask(text, continuations=True)
        out.append(Segment(segment.start, segment.end, segment.speaker, masked))
    joined = "\n".join(s.text for s in out)
    for span, words, why in held_words_in(joined, redaction.cut_spans):
        problems.append(
            f"held passage {getattr(span, 'ref', '?')} is still readable in the "
            f"speaker-labelled body — {why}"
        )
    return out, problems


def redact_transcript(
    transcript: Transcript,
    spans: Sequence[HeldSpan | HeldRecord],
    *,
    mode: str = MODE_ON,
    held_on: str = "",
) -> tuple[Transcript, Redaction, list[str]]:
    """The whole transcript — text and segments — with every held passage cut out.

    This is what the pipeline calls, and it calls it *before* any of the three files is
    rendered. Returns the transcript to render from, the redaction to derive everything else
    with, and the problems a person must be told about. A non-empty problem list means the
    recording does not publish: neither withheld silently nor published silently.
    """
    redaction = redact_text(transcript.text or "", spans, mode=mode, held_on=held_on)
    problems = list(redaction.problems())
    segments, segment_problems = redact_segments(list(transcript.segments or ()), redaction)
    problems.extend(segment_problems)
    if redaction.armed:
        problems.extend(redaction.check_publishable(redaction.text))
    cut = Transcript(
        text=redaction.text,
        segments=segments,
        language=transcript.language,
        engine_metadata=dict(transcript.engine_metadata),
        engine=transcript.engine,
        duration_s=transcript.duration_s,
    )
    return cut, redaction, problems


# -- everything derived from the transcript ---------------------------------------------


def redact_extraction(extraction: Any, redaction: Redaction) -> tuple[Any, list[ItemOutcome]]:
    """The whole analysis, with every held passage taken out of everything derived from it.

    The summary, the participants' quotes, the site's quote, the rejected items' nearest
    passages: all of it is a reading of the *unredacted* transcript, because that is the
    only text a quote can honestly be verified against. Any of it can therefore carry held
    words into a file, and all of it is masked here in one pass.

    Proposals whose quote is wholly inside a held passage are taken out of the proposal
    list and named in the notes, which every one of the three files prints. Held, visible,
    and requestable — never silently absent.
    """
    if not redaction.armed:
        return extraction, []

    outcomes: list[ItemOutcome] = []
    proposals = tuple(getattr(extraction, "proposals", ()) or ())
    kept: list[Any] = []
    held_refs: list[str] = []
    for proposal in proposals:
        item = getattr(proposal, "item", proposal)
        quote = str(getattr(item, "quote", "") or "")
        swallowed = redaction.wholly_held(quote)
        if swallowed is not None:
            outcomes.append(
                ItemOutcome(
                    item=proposal,
                    action="held",
                    reason=(
                        f"every word it quotes is inside held passage {swallowed.ref} "
                        f"({swallowed.phrase})"
                    ),
                    refs=(swallowed.ref,),
                )
            )
            held_refs.append(swallowed.ref)
            continue
        masked, changed = _mask_value(proposal, redaction)
        kept.append(masked)
        if changed:
            outcomes.append(
                ItemOutcome(item=masked, action="masked", reason="held words were masked", refs=changed)
            )
        else:
            outcomes.append(ItemOutcome(item=masked, action="unchanged"))

    updated: dict[str, Any] = {"proposals": tuple(kept)}
    for attribute in ("summary", "site_quote", "site", "participants", "review", "unclear", "routing"):
        if not hasattr(extraction, attribute):
            continue
        masked, changed = _mask_value(getattr(extraction, attribute), redaction)
        if changed or masked is not getattr(extraction, attribute):
            updated[attribute] = masked

    notes = [str(n) for n in (getattr(extraction, "notes", ()) or ())]
    if held_refs:
        count = len(held_refs)
        notes.append(
            f"{count} proposal{'s' if count > 1 else ''} from this recording "
            f"{'are' if count > 1 else 'is'} held pending review as "
            f"{', '.join(sorted(set(held_refs)))} and {'are' if count > 1 else 'is'} not "
            "listed in the actions file"
        )
    if redaction.applied:
        many = len(redaction.applied) > 1
        notes.append(
            f"{len(redaction.applied)} passage{'s' if many else ''} in this recording "
            f"{'were' if many else 'was'} held for review before this file was written, and "
            f"{'are' if many else 'is'} marked in place where {'they were' if many else 'it was'} said"
        )
    updated["notes"] = tuple(notes)
    if hasattr(extraction, "redacted"):
        updated["redacted"] = True

    return _rebuild(extraction, updated), outcomes


#: Fields whose value is verbatim from the transcript and must be cut at the offsets the
#: transcript was cut at, rather than merely searched for. Everything else a model wrote is
#: its own prose and is masked by search.
_QUOTE_FIELDS = frozenset({"quote", "offered_quote", "site_quote"})


def _mask_value(value: Any, redaction: Redaction, field_name: str = "") -> tuple[Any, tuple[str, ...]]:
    """Mask every string anywhere inside ``value``, rebuilding as it goes.

    Generic rather than field-by-field on purpose. The analysis record grows a field
    whenever the AI pass learns to notice something new, and a masker that names its fields
    would keep publishing the one that was added last. It walks dataclasses, tuples, lists,
    dicts and plain objects alike, because the renderers duck-type their input and so must
    anything that has to reach every string they will print.
    """
    if isinstance(value, str):
        if field_name in _QUOTE_FIELDS:
            masked = redaction.apply_to_quote(value)
            return masked, (refs_in(masked) if masked != value else ())
        masked, refs = redaction.mask(value)
        return masked, refs
    if isinstance(value, tuple):
        out = []
        refs: list[str] = []
        for entry in value:
            masked, touched = _mask_value(entry, redaction)
            out.append(masked)
            refs.extend(touched)
        return tuple(out), tuple(dict.fromkeys(refs))
    if isinstance(value, list):
        out_list = []
        refs = []
        for entry in value:
            masked, touched = _mask_value(entry, redaction)
            out_list.append(masked)
            refs.extend(touched)
        return out_list, tuple(dict.fromkeys(refs))
    if isinstance(value, dict):
        out_dict = {}
        refs = []
        for key, entry in value.items():
            masked, touched = _mask_value(entry, redaction, str(key))
            out_dict[key] = masked
            refs.extend(touched)
        return out_dict, tuple(dict.fromkeys(refs))
    if is_dataclass(value) and not isinstance(value, type):
        updated: dict[str, Any] = {}
        refs = []
        for f in dataclass_fields(value):
            if not f.init:
                continue
            masked, touched = _mask_value(getattr(value, f.name), redaction, f.name)
            if touched or masked is not getattr(value, f.name):
                updated[f.name] = masked
            refs.extend(touched)
        if not refs and not updated:
            return value, ()
        if refs and "redacted" in {f.name for f in dataclass_fields(value)}:
            updated["redacted"] = True
        return _rebuild(value, updated), tuple(dict.fromkeys(refs))
    if hasattr(value, "__dict__") and not isinstance(value, type) and not callable(value):
        # A plain object — a proposal from somewhere that is not this codebase's dataclass,
        # a stand-in in a test, whatever the analysis pass grows next. Masked in place: it
        # is not this module's business to know how to rebuild somebody else's class, and a
        # walker that skipped what it did not recognise would publish exactly the field
        # nobody thought about.
        refs = []
        for key, entry in list(vars(value).items()):
            if key.startswith("_"):
                continue
            masked, touched = _mask_value(entry, redaction, key)
            if touched:
                try:
                    setattr(value, key, masked)
                except Exception:  # noqa: BLE001 - read-only attribute; the leak is reported
                    continue
                refs.extend(touched)
        return value, tuple(dict.fromkeys(refs))
    return value, ()


def _rebuild(record: Any, updates: Mapping[str, Any]) -> Any:
    """``dataclasses.replace``, falling back to setting attributes on anything else."""
    if not updates:
        return record
    if is_dataclass(record) and not isinstance(record, type):
        allowed = {f.name for f in dataclass_fields(record) if f.init}
        try:
            return replace(record, **{k: v for k, v in updates.items() if k in allowed})
        except (TypeError, ValueError):
            pass
    for key, value in updates.items():
        try:
            setattr(record, key, value)
        except Exception:  # noqa: BLE001 - nothing here is worth losing a redaction over
            continue
    return record


def restore_released(text: str, released: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    """Put back the words of every passage a person has released, in place.

    He asked for a held passage to be released *in place*: the transcript in the record
    stops saying "something was held here" and starts saying what was said, in the sentence
    it was said in, rather than as an appendix nobody reads. ``released`` is
    ``{ref: the words}``, which is what :meth:`transcriber.withheld.WithheldStore.released_for`
    returns.

    A marker whose reference is not in ``released`` is left exactly as it is — including a
    refused one, which keeps saying that something is held, because it is.
    """
    if not text or not released:
        return text, ()
    put_back: list[str] = []

    def _swap(match: re.Match[str]) -> str:
        ref = match.group("ref")
        words = released.get(ref)
        if words is None:
            return match.group(0)
        put_back.append(ref)
        return words

    out = FULL_MARKER_RE.sub(_swap, text)

    # The tail of a passage that straddled two speaker segments. Its words have just gone
    # back where the passage began, so the continuation marker is removed rather than
    # replaced: putting the same words back twice would be a worse answer than a line that
    # starts a beat later than it did.
    def _drop(match: re.Match[str]) -> str:
        return "" if match.group("ref") in released else match.group(0)

    out = PART_MARKER_RE.sub(_drop, out)
    return out, tuple(dict.fromkeys(put_back))
