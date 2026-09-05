"""The durable home for a held passage, and the only place one can be decided.

A held passage is not a flag on a recording. It is *the words themselves*, cut out of the
transcript before any file is written, and after that cut they exist in exactly two places:
the audio, and this database. Nothing releases one on a timer, nothing expires one, and
nothing in this module can decide one — every decision carries the name of the person who
made it. That is the whole reason this file reads like :mod:`transcriber.ledger` rather than
like a cache: it inherits the same discipline, for the same reason, and a shortcut taken
here is an irreversible loss of something somebody said out loud.

What it holds, per span:

    which recording it came from and on which route · which reviewer owns the decision ·
    the exact words · the category that caught it · why the classifier caught it and how
    sure it was · enough surrounding text to decide without opening the transcript · when
    it was held · and the decision, with who made it and when.

Four properties are structural rather than conventional, so that they cannot rot:

* **Nothing is deleted.** There is no ``DELETE`` in this file and no method that removes a
  row. A refusal is stored as a refusal — ``decision='refused'`` with a name and a time —
  because a refused passage that had simply vanished is indistinguishable from a passage
  the classifier never caught, and those two need different answers.
* **Nothing is decided by a machine.** :meth:`WithheldStore.decide` requires
  ``answered_by`` — the name of the person who answered — and refuses the names a scheduler
  would use ("auto", "system", "timer", the service's own name). There is no deadline column
  to expire against and no sweep that touches decisions. The field is called ``answered_by``
  and not the record's own word for this because that word names a *person applying a
  decision to the record*, and no module of this service may be able to produce it in any
  shape; ``tests/test_decides_nothing.py`` enforces exactly that, and a held passage a person
  releases goes back into the transcript as evidence of what was said, never as a decision
  about the job.
* **The held band cannot widen by accident.** :class:`HeldSpan` refuses a category that is
  not one of the six settled ones (``docs/GATE-DECISIONS.md`` §5). There is deliberately no
  category for a price, a supplier rate, an invoice, a defect or a named person doing their
  job — prices flow, labelled — so no classifier change, however enthusiastic, can put one
  in this table without a ratified change to the list below.
* **A staff member reviews their own words.** Every row carries a ``reviewer``.
  :meth:`WithheldStore.queue_for` is the only way to read the text and it answers for one
  reviewer; :meth:`WithheldStore.overview` returns counts, sites and ages and never a word
  of what was said. James's morning view is built from the second one. Staff record
  voluntarily, and a staff member who works out that their held text is read by the
  principal stops keeping a folder — after which the recordings are gone entirely, which is
  the loss this whole service exists to prevent, arriving as a social effect rather than a
  technical one.

It also carries the measurement that has to come before the gate is ever armed.
:meth:`WithheldStore.record_pass` writes one row per classified recording — how many spans
were found, how many characters they covered, and whether the run was live or shadow — so
that "how much does this actually touch" is a number read off this database rather than an
estimate. The design passes disagreed about that number by a factor of twenty-five, and
arming a gate against an estimate is how the queue becomes a wall.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .logging_setup import get_logger
from .models import (
    DEFAULT_ROUTE,
    day_of,
    is_route_name,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
    utc_now_iso,
)

log = get_logger(__name__)

__all__ = [
    "GATE_MODES",
    "MODE_OFF",
    "MODE_SHADOW",
    "MODE_ON",
    "normalise_mode",
    "CATEGORIES",
    "CATEGORY_PHRASE",
    "CATEGORY_DESCRIPTION",
    "STAFF_MATTER",
    "PERSONAL_CIRCUMSTANCES",
    "LEGAL_EXPOSURE",
    "BARE_IDENTIFIER",
    "ASKED_NOT_RECORDED",
    "MARGIN_POSITION",
    "Decision",
    "HeldSpan",
    "HeldRecord",
    "WithheldStore",
    "WithheldError",
    "NotAPersonError",
    "AlreadyDecidedError",
    "UnknownHoldError",
    "reviewer_for",
    "held_spans_from",
    "ref_for",
    "normalise_category",
    "CATEGORY_ALIASES",
    "SCHEMA_VERSION",
]


# -- the three modes -----------------------------------------------------------------
#
# It ships dark. ``shadow`` is the default everywhere it is read, because the failure this
# ordering prevents — a wall of held passages on day one, a page he stops opening, a record
# quietly hollowing out — is worse and slower to notice than the leak it guards against.

MODE_OFF = "off"          # classify nothing, hold nothing
MODE_SHADOW = "shadow"    # classify everything, hold nothing, record what it *would* have held
MODE_ON = "on"            # classify everything, and actually withhold
GATE_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)


def normalise_mode(value: Any, default: str = MODE_SHADOW) -> str:
    """One spelling of the mode across the service; anything unrecognised is ``shadow``.

    Unrecognised does not mean ``on`` and does not mean ``off``: a typo in the environment
    must not silently arm the gate, and must not silently disarm the measurement either.
    """
    text = str(value or "").strip().lower()
    return text if text in GATE_MODES else default


# -- the settled band ----------------------------------------------------------------
#
# These six are the whole of it, from docs/GATE-DECISIONS.md §5, answered by James on
# 2026-08-28. Adding a seventh is a decision he takes, not one a classifier takes at
# runtime, which is why this is a closed list that HeldSpan validates against.

STAFF_MATTER = "staff_matter"
PERSONAL_CIRCUMSTANCES = "personal_circumstances"
LEGAL_EXPOSURE = "legal_exposure"
BARE_IDENTIFIER = "bare_identifier"
ASKED_NOT_RECORDED = "do_not_write_down"
MARGIN_POSITION = "own_margin"

CATEGORIES: tuple[str, ...] = (
    STAFF_MATTER,
    PERSONAL_CIRCUMSTANCES,
    LEGAL_EXPOSURE,
    BARE_IDENTIFIER,
    ASKED_NOT_RECORDED,
    MARGIN_POSITION,
)

#: How the category is said in the marker that replaces the words, and in the review page.
#: Plain English, no jargon, no internal names: it is read by whoever is standing on a site
#: with a client on the line, and by the record's own question list.
CATEGORY_PHRASE: dict[str, str] = {
    STAFF_MATTER: "a staff matter",
    PERSONAL_CIRCUMSTANCES: "a person's personal circumstances",
    LEGAL_EXPOSURE: "a legal matter",
    BARE_IDENTIFIER: "an identity or account number",
    ASKED_NOT_RECORDED: "something a person asked not be written down",
    MARGIN_POSITION: "our own cost against what we charged",
}

#: Other spellings of the same six, accepted on the way in and stored as the canonical name.
#: The names above are the ones ``prompts.SENSITIVITY_CATEGORIES`` offers the model and
#: ``sensitivity.DISPOSITIONS`` puts in the held band — those two and this list have to agree
#: or a real hold arrives here and is refused. Accepting the hyphenated spelling as well
#: costs nothing and removes one way for that to happen quietly.
CATEGORY_ALIASES: dict[str, str] = {
    "staff-matter": STAFF_MATTER,
    "personal-circumstances": PERSONAL_CIRCUMSTANCES,
    "health": PERSONAL_CIRCUMSTANCES,
    "legal-exposure": LEGAL_EXPOSURE,
    "liability": LEGAL_EXPOSURE,
    "bare-identifier": BARE_IDENTIFIER,
    "identifier": BARE_IDENTIFIER,
    "asked-not-recorded": ASKED_NOT_RECORDED,
    "do-not-write-down": ASKED_NOT_RECORDED,
    "margin-position": MARGIN_POSITION,
    "own-margin": MARGIN_POSITION,
    "margin": MARGIN_POSITION,
}


def normalise_category(value: Any) -> str:
    """The canonical name of one held category, or the value unchanged if it is not one.

    Unchanged rather than guessed: :class:`HeldSpan` then refuses it by name, which is a
    classifier bug somebody can read, rather than a passage filed under the nearest match.
    """
    text = str(value or "").strip().lower()
    if text in CATEGORIES:
        return text
    return CATEGORY_ALIASES.get(text, text)

#: The longer form, for the review page and for anybody reading this file to find out what
#: the gate is actually for. Kept next to the phrases so the two cannot drift.
CATEGORY_DESCRIPTION: dict[str, str] = {
    STAFF_MATTER: "a warning, a hearing, pay, performance or a dismissal",
    PERSONAL_CIRCUMSTANCES: "an identifiable person's health or personal circumstances",
    LEGAL_EXPOSURE: (
        "an admission of KBC's own liability, attorney or insurer strategy, or "
        "'this must not leave the firm'"
    ),
    BARE_IDENTIFIER: "an ID number, bank details or a home address",
    ASKED_NOT_RECORDED: "anybody asking that something not be written down, in any language",
    MARGIN_POSITION: (
        "KBC's cost and its charge in one breath — 'we raised R1.65m and we'll land at "
        "R1.604m'. A price on its own is not this and is never held"
    ),
}


class Decision:
    """What has been decided about one held passage, and nothing more.

    ``PENDING`` is the only state a machine can put a row into. ``RELEASED`` and ``REFUSED``
    both require a person's name. ``NOT_WITHHELD`` is a shadow-mode row: the classifier
    would have held these words and nothing was actually cut, so there is nothing to release
    and no decision to make — it exists to be counted, not to be answered.
    """

    PENDING = "pending"
    RELEASED = "released"
    REFUSED = "refused"
    NOT_WITHHELD = "not-withheld"

    ALL = (PENDING, RELEASED, REFUSED, NOT_WITHHELD)
    #: The two a person may choose between. Deliberately not "expired", "lapsed" or "auto".
    DECIDABLE = (RELEASED, REFUSED)

    @staticmethod
    def is_known(value: str) -> bool:
        return value in Decision.ALL


#: Names a decision may not be recorded under. Not a security control — it is a guard
#: against the one mistake this design fears most, which is a future scheduler quietly
#: clearing the queue and still looking like a gate. Anything automatic that wants to clear
#: a hold has to lie about being a person to do it, and lying is a thing a reviewer can see
#: in the events table afterwards.
_NOT_A_PERSON = frozenset(
    {
        "agent",
        "auto",
        "automatic",
        "bot",
        "cron",
        "daemon",
        "default",
        "expiry",
        "gate",
        "machine",
        "nobody",
        "none",
        "pipeline",
        "robot",
        "scheduler",
        "service",
        "system",
        "timeout",
        "timer",
        "transcriber",
        "unknown",
        "worker",
    }
)


class WithheldError(RuntimeError):
    """Anything this store refuses to do. Never raised quietly, never swallowed."""


class NotAPersonError(WithheldError):
    """A decision arrived without a person's name on it."""


class AlreadyDecidedError(WithheldError):
    """A second decision on a passage that already has one, without saying so."""


class UnknownHoldError(WithheldError):
    """A decision, or a read, aimed at a hold id this store has never seen."""


def held_spans_from(
    findings: Iterable[Any],
    *,
    item_id: str,
    route: str = DEFAULT_ROUTE,
    transcript: str = "",
    site: str = "",
    source_name: str = "",
    recorded_at: str = "",
    recorded_by: str = "",
    principal: str = "",
    context_chars: int = 160,
) -> tuple[HeldSpan, ...]:
    """The classifier's findings, as the spans this store and the redactor both work in.

    :mod:`transcriber.sensitivity` produces findings — offsets, a category, a confidence, a
    public subject — and knows nothing about recordings, reviewers or storage; this store
    and :mod:`transcriber.redact` work in spans, which know all three. One adapter rather
    than a conversion written out at each call site, because a caller that forgets to carry
    ``item_id`` gets a different reference for the same words, and the reference is what the
    marker in the transcript says.

    Only findings in the held band are converted: anything the classifier labelled and
    published is not this store's business. The surrounding context is cut from
    ``transcript`` here rather than in the classifier, because it is needed by whoever
    reviews the passage weeks later, when the transcript no longer contains it.

    **The context of one held passage never carries another one's words.** Two passages of
    the same recording can be a hundred characters apart and belong to two different
    people's queues — a staff member's own circumstances and, in the next sentence, the
    disciplinary matter that routes to the principal — and a context window cut raw from the
    transcript walks straight around every other protection here: the queue answers for one
    named person, and :class:`transcriber.review_page.Elsewhere` has nowhere to put words at
    all, and then the surround of a passage he *is* entitled to read hands him the passage he
    is not. So every other held passage inside a context window is replaced by its own
    reference before the span is built. It reads as ``[held 3F91C2]``, which is the same
    thing the transcript says in the same place, so a reviewer sees that something else was
    caught there rather than a gap that looks like ordinary talk.
    """
    caught: list[tuple[Any, str, int, int, str]] = []
    for finding in findings:
        held = getattr(finding, "held", None)
        if held is False:
            continue
        category = normalise_category(getattr(finding, "category", ""))
        if category not in CATEGORIES:
            continue
        start = int(getattr(finding, "start", 0))
        end = int(getattr(finding, "end", 0))
        text = str(getattr(finding, "text", "") or "")
        if not text and transcript:
            text = transcript[start:end]
        caught.append((finding, category, start, end, text))

    # The reference is content-addressed on the recording, the offsets and the words, so it
    # is known before the span exists and cannot drift from the one the span will carry.
    elsewhere = tuple(
        (start, end, ref_for(item_id, start, end, text))
        for _finding, _category, start, end, text in caught
    )

    out: list[HeldSpan] = []
    for finding, category, start, end, text in caught:
        before = _context(transcript, max(0, start - context_chars), start, elsewhere, start)
        after = _context(transcript, end, end + context_chars, elsewhere, start)
        span = HeldSpan(
            item_id=item_id,
            start=start,
            end=end,
            text=text,
            category=category,
            route=route,
            subject=str(getattr(finding, "subject", "") or ""),
            reason=str(getattr(finding, "reason", "") or ""),
            confidence=_as_float(getattr(finding, "confidence", None)),
            context_before=before,
            context_after=after,
            site=site,
            source_name=source_name,
            recorded_at=recorded_at,
            recorded_by=recorded_by,
        )
        out.append(span.with_reviewer(reviewer_for(category, recorded_by, principal)))
    return tuple(out)


def ref_for(item_id: str, start: int, end: int, text: str) -> str:
    """The short reference these exact words at this exact place will carry.

    The one implementation of it: :attr:`HeldSpan.ref` calls this, and so does the context
    builder above, which needs to name a passage before the span for it has been built.
    """
    digest = hashlib.sha256(
        "␟".join((item_id, str(int(start)), str(int(end)), text)).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def _context(
    transcript: str,
    window_start: int,
    window_end: int,
    elsewhere: Sequence[tuple[int, int, str]],
    mine: int,
) -> str:
    """A slice of the transcript with every *other* held passage replaced by its reference.

    ``mine`` is the start offset of the passage this context belongs to, which is what tells
    the passage apart from its neighbours; only offsets identify a passage here, because two
    holds in one recording can legitimately carry the same words.
    """
    if not transcript:
        return ""
    start = max(0, min(int(window_start), len(transcript)))
    end = max(start, min(int(window_end), len(transcript)))
    if start == end:
        return ""
    blocks = sorted(
        (max(start, s), min(end, e), ref)
        for s, e, ref in elsewhere
        if s != mine and s < end and start < e
    )
    if not blocks:
        return transcript[start:end]
    pieces: list[str] = []
    at = start
    for block_start, block_end, ref in blocks:
        if block_start > at:
            pieces.append(transcript[at:block_start])
        if block_end > at:
            pieces.append(f"[held {ref[:6].upper()}]")
            at = block_end
    if at < end:
        pieces.append(transcript[at:end])
    return "".join(pieces)


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _as_notes(row: Any) -> tuple[str, ...]:
    """The classifier's notes off a ``passes`` row, tolerant of a row written before v2."""
    try:
        raw = row["notes"]
    except (IndexError, KeyError):
        return ()
    try:
        loaded = json.loads(str(raw or "[]"))
    except ValueError:
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(note) for note in loaded if str(note).strip())


def reviewer_for(category: str, recorded_by: str, principal: str) -> str:
    """Who owns the decision on one held passage — decision 6, in one function.

    A staff member reviews their own held passages, so by default the reviewer is whoever
    recorded the call. Staff matters are the exception James named: a warning, a hearing,
    pay, performance or a dismissal is genuinely his to hold, so those route to him whoever
    recorded them. His own recordings are his throughout.

    One function rather than a rule repeated in the classifier, the review page and the
    morning email, because those three arriving at different answers is the failure that
    ends with a staff member reading their own words on somebody else's screen.
    """
    who = (recorded_by or "").strip()
    boss = (principal or "").strip()
    if not who:
        return boss
    if category == STAFF_MATTER:
        return boss or who
    return who


@dataclass(frozen=True)
class HeldSpan:
    """One stretch of the transcript the classifier says must not be written down.

    Offsets are character offsets into the transcript text the classifier read, ``end``
    exclusive, and ``text`` is exactly ``transcript[start:end]``. Both are carried because
    each catches a different mistake: the offsets make the cut exact, and the text makes it
    checkable — :mod:`transcriber.redact` refuses to cut at offsets whose text does not
    match, rather than trusting an index that may have been computed against a different
    version of the transcript.

    ``context_before`` / ``context_after`` are what a reviewer needs to decide in seconds
    without opening the recording. They are stored, not derived later, because by the time
    anybody reviews this the transcript in the record no longer contains the passage.
    """

    item_id: str
    start: int
    end: int
    text: str
    category: str
    route: str = DEFAULT_ROUTE
    #: A short public noun phrase for what was held — "a rate for the remedial" — checked by
    #: the classifier to carry no name, figure or address, because it is published in the
    #: marker in place of the words. Empty falls back to the category's own phrase.
    subject: str = ""
    reason: str = ""
    confidence: float | None = None
    context_before: str = ""
    context_after: str = ""
    speaker: str | None = None
    site: str = ""
    source_name: str = ""
    recorded_at: str = ""
    recorded_by: str = ""
    reviewer: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", normalise_category(self.category))
        if not (self.item_id or "").strip():
            raise WithheldError("a held span must say which recording it came from")
        if self.category not in CATEGORIES:
            raise WithheldError(
                f"{self.category!r} is not one of the categories James settled on "
                f"({', '.join(CATEGORIES)}). A price, a supplier rate, an invoice, a defect "
                "or a named person doing their job is let through labelled and is never held"
            )
        if int(self.end) <= int(self.start) or int(self.start) < 0:
            raise WithheldError(
                f"a held span runs from {self.start} to {self.end}, which is not a stretch "
                "of text; an empty or backwards span would cut the wrong words or none"
            )
        if not (self.text or "").strip():
            raise WithheldError(
                "a held span with no words is not a hold: the words are the only copy of "
                "this outside the audio"
            )
        if len(self.text) != int(self.end) - int(self.start):
            raise WithheldError(
                f"the held text is {len(self.text)} characters but the span covers "
                f"{int(self.end) - int(self.start)}; one of the two is wrong and cutting on "
                "either would take the wrong words"
            )
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            raise WithheldError(f"confidence {self.confidence!r} is not between 0 and 1")
        if self.route and not is_route_name(self.route):
            raise WithheldError(f"{self.route!r} is not a usable route name")

    @property
    def hold_id(self) -> str:
        """A stable id for these words in this recording at this place.

        Derived rather than allocated, so that classifying the same recording twice — a
        requeue, a sweep, a re-run after a crash — lands on the same row instead of a second
        copy of the same held passage in somebody's queue. It is content-addressed on the
        recording, the offsets and the words: change any of the three and it is a different
        hold, which is correct, because it is then a different cut of the transcript.
        """
        return ref_for(self.item_id, self.start, self.end, self.text)

    @property
    def ref(self) -> str:
        """The short reference a person quotes back: six characters, upper case.

        It goes into the marker that replaces the words, into the review page, and into the
        morning email, so it has to be short enough to read out over the phone and stable
        across runs. Taken from the front of :attr:`hold_id`, so the two cannot disagree.
        """
        return self.hold_id[:6].upper()

    @property
    def length(self) -> int:
        return int(self.end) - int(self.start)

    @property
    def phrase(self) -> str:
        """What this is, in the words the marker and the review page use."""
        return (self.subject or "").strip() or CATEGORY_PHRASE.get(
            self.category, "something held for review"
        )

    def with_reviewer(self, reviewer: str) -> "HeldSpan":
        """The same span, owned by a named reviewer. Offsets and words are untouched."""
        return HeldSpan(
            item_id=self.item_id,
            start=self.start,
            end=self.end,
            text=self.text,
            category=self.category,
            route=self.route,
            subject=self.subject,
            reason=self.reason,
            confidence=self.confidence,
            context_before=self.context_before,
            context_after=self.context_after,
            speaker=self.speaker,
            site=self.site,
            source_name=self.source_name,
            recorded_at=self.recorded_at,
            recorded_by=self.recorded_by,
            reviewer=reviewer,
            meta=dict(self.meta),
        )


@dataclass(frozen=True)
class HeldRecord:
    """One row of the store: a held span, plus what has happened to it since.

    ``text``, ``context_before`` and ``context_after`` are the reviewer's copy of what was
    said. :meth:`without_words` is the same record with all three blanked, which is the only
    shape that reaches anybody who does not own the decision.
    """

    hold_id: str
    ref: str
    item_id: str
    route: str
    source_name: str
    site: str
    reviewer: str
    recorded_by: str
    category: str
    subject: str
    text: str
    reason: str
    confidence: float | None
    start: int
    end: int
    context_before: str
    context_after: str
    speaker: str | None
    recorded_at: str
    held_at: str
    mode: str
    decision: str
    answered_by: str
    decided_at: str
    decision_note: str
    times_seen: int = 1
    decisions_made: int = 0
    words_visible: bool = True
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        """Waiting for a person. Shadow rows are not pending — nothing was withheld."""
        return self.decision == Decision.PENDING and self.mode == MODE_ON

    @property
    def withheld(self) -> bool:
        """True when these words were actually cut out of a transcript."""
        return self.mode == MODE_ON

    @property
    def phrase(self) -> str:
        """The public noun phrase for what was held. Carries no name, figure or address."""
        return (self.subject or "").strip() or CATEGORY_PHRASE.get(
            self.category, "something held for review"
        )

    def age_days(self, now: str | None = None) -> int:
        """Whole days since it was held, for the escalating morning email."""
        return _days_between(self.held_at, now or utc_now_iso())

    def without_words(self) -> "HeldRecord":
        """The same row with every word of what was said removed.

        This is what James sees for a passage he does not own: the count, the site, the
        recording, the category and the age — never the text, never the context, never the
        classifier's quotation of it in ``reason``.
        """
        return HeldRecord(
            hold_id=self.hold_id,
            ref=self.ref,
            item_id=self.item_id,
            route=self.route,
            source_name=self.source_name,
            site=self.site,
            reviewer=self.reviewer,
            recorded_by=self.recorded_by,
            category=self.category,
            subject=self.subject,
            text="",
            reason="",
            confidence=self.confidence,
            start=self.start,
            end=self.end,
            context_before="",
            context_after="",
            speaker=None,
            recorded_at=self.recorded_at,
            held_at=self.held_at,
            mode=self.mode,
            decision=self.decision,
            answered_by=self.answered_by,
            decided_at=self.decided_at,
            decision_note="",
            times_seen=self.times_seen,
            decisions_made=self.decisions_made,
            words_visible=False,
            meta={},
        )

    def as_span(self) -> HeldSpan:
        """Back to the span shape, for :mod:`transcriber.redact` to cut or restore with."""
        return HeldSpan(
            item_id=self.item_id,
            start=self.start,
            end=self.end,
            text=self.text,
            category=self.category,
            route=self.route or DEFAULT_ROUTE,
            subject=self.subject,
            reason=self.reason,
            confidence=self.confidence,
            context_before=self.context_before,
            context_after=self.context_after,
            speaker=self.speaker,
            site=self.site,
            source_name=self.source_name,
            recorded_at=self.recorded_at,
            recorded_by=self.recorded_by,
            reviewer=self.reviewer,
            meta=dict(self.meta),
        )

    def to_dict(self, *, include_words: bool = False) -> dict[str, Any]:
        """A plain dict. The words are left out unless a caller asks for them by name.

        ``subject`` is the classifier's own noun phrase for the passage, and it is only ever
        emitted for a record whose words this caller may read. It used to be emitted always,
        including from :meth:`without_words` — which is the projection whose whole job is
        that James sees the count and the site and never what was said. ``held list --json``
        prints ``overview()`` verbatim, so ``transcriber held list --json`` with no ``--as``
        put a staff member's classified subject on the screen and into whatever it was piped
        into, while the human-readable branch of the very same command was careful to print
        "deliberately not even the classifier's own summary of them".

        The check on that subject is mechanical and shallow — digits, ``@``, mid-string
        capitals, a length cap — so "the foreman's drinking problem" passes it. It is safe
        in the marker, where it stands in place of words that are gone, and it is not safe
        as a description of somebody else's passage handed to a third party. When the words
        are hidden, ``phrase`` falls back to the category's own fixed sentence, which is one
        of six and cannot carry detail.
        """
        out: dict[str, Any] = {
            "hold_id": self.hold_id,
            "ref": self.ref,
            "item_id": self.item_id,
            "route": self.route,
            "source_name": self.source_name,
            "site": self.site,
            "reviewer": self.reviewer,
            "category": self.category,
            "subject": self.subject if self.words_visible else "",
            "phrase": (
                self.phrase
                if self.words_visible
                else CATEGORY_PHRASE.get(self.category, "something held for review")
            ),
            "confidence": self.confidence,
            "held_at": self.held_at,
            "recorded_at": self.recorded_at,
            "mode": self.mode,
            "decision": self.decision,
            "answered_by": self.answered_by,
            "decided_at": self.decided_at,
            "characters": max(0, self.end - self.start),
            "times_seen": self.times_seen,
        }
        if include_words and self.words_visible:
            out["text"] = self.text
            out["context_before"] = self.context_before
            out["context_after"] = self.context_after
            out["reason"] = self.reason
            out["speaker"] = self.speaker
            out["decision_note"] = self.decision_note
        return out


SCHEMA_VERSION = 2

_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "held passages, their decisions, and one row per classified recording",
        (
            """
            CREATE TABLE IF NOT EXISTS holds (
                hold_id        TEXT PRIMARY KEY,
                ref            TEXT NOT NULL,
                item_id        TEXT NOT NULL,
                route          TEXT NOT NULL DEFAULT 'default',
                source_name    TEXT NOT NULL DEFAULT '',
                site           TEXT NOT NULL DEFAULT '',
                reviewer       TEXT NOT NULL DEFAULT '',
                recorded_by    TEXT NOT NULL DEFAULT '',
                category       TEXT NOT NULL,
                subject        TEXT NOT NULL DEFAULT '',
                text           TEXT NOT NULL,
                reason         TEXT NOT NULL DEFAULT '',
                confidence     REAL,
                start_offset   INTEGER NOT NULL,
                end_offset     INTEGER NOT NULL,
                context_before TEXT NOT NULL DEFAULT '',
                context_after  TEXT NOT NULL DEFAULT '',
                speaker        TEXT,
                recorded_at    TEXT NOT NULL DEFAULT '',
                held_at        TEXT NOT NULL,
                mode           TEXT NOT NULL,
                decision       TEXT NOT NULL DEFAULT 'pending',
                answered_by     TEXT NOT NULL DEFAULT '',
                decided_at     TEXT NOT NULL DEFAULT '',
                decision_note  TEXT NOT NULL DEFAULT '',
                times_seen     INTEGER NOT NULL DEFAULT 1,
                decisions_made INTEGER NOT NULL DEFAULT 0,
                meta           TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_holds_item ON holds(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_holds_reviewer ON holds(reviewer, decision)",
            "CREATE INDEX IF NOT EXISTS idx_holds_decision ON holds(decision, held_at)",
            "CREATE INDEX IF NOT EXISTS idx_holds_ref ON holds(ref)",
            """
            CREATE TABLE IF NOT EXISTS hold_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                hold_id  TEXT,
                item_id  TEXT,
                at       TEXT NOT NULL,
                kind     TEXT NOT NULL,
                actor    TEXT NOT NULL DEFAULT '',
                was      TEXT,
                became   TEXT,
                detail   TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hold_events_hold ON hold_events(hold_id)",
            "CREATE INDEX IF NOT EXISTS idx_hold_events_at ON hold_events(at)",
            # One row per recording the classifier read, held or not. This is the
            # denominator: without it "3 passages held today" is a number with nothing to
            # divide by, and the whole point of shipping dark is to learn the fraction.
            """
            CREATE TABLE IF NOT EXISTS passes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id           TEXT NOT NULL,
                route             TEXT NOT NULL DEFAULT 'default',
                at                TEXT NOT NULL,
                mode              TEXT NOT NULL,
                spans_found       INTEGER NOT NULL DEFAULT 0,
                characters_held   INTEGER NOT NULL DEFAULT 0,
                transcript_chars  INTEGER NOT NULL DEFAULT 0,
                classifier        TEXT NOT NULL DEFAULT '',
                categories        TEXT NOT NULL DEFAULT '[]'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_passes_item ON passes(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_passes_at ON passes(at)",
        ),
    ),
    (
        2,
        "what the classifier could not stand behind, per recording",
        (
            # The gate's own notes about a recording — "the model did not answer the
            # sensitivity question", "the words it quoted are not in the transcript, a
            # person should look at this recording". They used to reach a file only when
            # the gate was armed, which meant that in ``shadow`` — the mode that ships —
            # they reached nobody at all. They lived in a ledger row's meta that nobody
            # opens at 06:00, while the morning email told him the measurement was ready.
            #
            # A note carries no held words by construction: ``sensitivity.assess`` writes
            # them from the category's public phrase and the classifier's checked subject,
            # never from the passage. That is why they can be stored and printed here.
            "ALTER TABLE passes ADD COLUMN notes TEXT NOT NULL DEFAULT '[]'",
        ),
    ),
)


class WithheldStore:
    """The held passages of every recording, and every decision ever made about one."""

    def __init__(self, path: str, *, busy_timeout_ms: int = 30_000, scrub: Any = None) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._scrub = scrub if callable(scrub) else None
        self._memory = path == ":memory:" or path.startswith("file::memory:")
        self._permission_warning_given = False
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if not self._memory:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.migrate()

    # -- where it lives ------------------------------------------------------------

    @staticmethod
    def path_beside(ledger_path: str) -> str:
        """Where this database goes, given the ledger's path.

        Beside the ledger and not inside it: the ledger is read by ``transcriber status``,
        copied when somebody debugs a lost recording, and its rows are printed. Held text
        does not belong in a file with those habits. One function so the worker, the review
        page and the digest all open the same file.
        """
        if ledger_path in (":memory:", "") or str(ledger_path).startswith("file::memory:"):
            return ":memory:"
        base, ext = os.path.splitext(ledger_path)
        return f"{base}-withheld{ext or '.db'}"

    @classmethod
    def from_config(cls, config: Any) -> "WithheldStore":
        """Open the store this configuration describes, beside its ledger."""
        explicit = str(getattr(config, "withheld_path", "") or "")
        path = explicit or cls.path_beside(str(getattr(config, "ledger_path", ":memory:")))
        return cls(path, scrub=getattr(config, "scrub", None))

    # -- connections ---------------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        if not self._memory:
            conn.execute("PRAGMA journal_mode=WAL")
        # A held passage lost in a power cut is a passage that exists only in the audio,
        # with nothing anywhere saying it was ever taken out. Durability wins.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._restrict_permissions()
        return conn

    def _restrict_permissions(self) -> None:
        """0600 on the database and on the WAL files beside it, every time it is opened.

        The ledger does this because it carries fragments of what was said. This file *is*
        what was said — whole passages, cut out of the record precisely because they must
        not be read by everybody — so it is the single most revealing file the service
        writes. SQLite creates ``-wal`` and ``-shm`` beside it holding the same content,
        which is why all three are set and why it is redone on every connection.

        Best effort, like the ledger's: a filesystem that will not take a chmod must not
        stop the service, but it must say so, once, loudly enough to act on.
        """
        if self._memory:
            return
        for suffix in ("", "-wal", "-shm"):
            target = self.path + suffix
            try:
                if os.path.exists(target):
                    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                if not self._permission_warning_given:
                    self._permission_warning_given = True
                    log.warning(
                        "withheld-permissions",
                        "could not restrict the withheld-passage store to this account "
                        "only; it holds the full text of everything the gate has held and "
                        "may be readable by other users on this machine",
                        file=os.path.basename(target),
                        error=str(exc),
                    )

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._new_connection()
                return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One all-or-nothing write, on the same pattern the ledger uses.

        The COMMIT is inside the guard deliberately: SQLite does not roll back a failed
        COMMIT, so without this a full disk would leave an open transaction on this
        thread's connection and every later write would fail until a restart.
        """
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    self._discard(conn)
                raise

    def _discard(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if self._shared is conn:
            self._shared = None
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None

    def close(self) -> None:
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "WithheldStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- schema --------------------------------------------------------------------

    def migrate(self) -> int:
        conn = self._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT)"
        )
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise WithheldError(
                f"the withheld store at {self.path} is schema v{current}, but this build "
                f"only knows v{SCHEMA_VERSION}. A newer version of the service wrote it; run "
                "that one rather than downgrading a database that holds the only copy of "
                "what was said"
            )
        for version, note, statements in _MIGRATIONS:
            if version <= current:
                continue
            with self._tx() as tx:
                for sql in statements:
                    tx.execute(sql)
                tx.execute(
                    "INSERT INTO schema_version (version, applied_at, note) VALUES (?,?,?)",
                    (version, utc_now_iso(), note),
                )
        return self.schema_version()

    def schema_version(self) -> int:
        row = self._conn().execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    # -- writing -------------------------------------------------------------------

    def hold(self, span: HeldSpan, *, mode: str = MODE_ON, at: str | None = None) -> HeldRecord:
        """Record one held passage. Returns the row, new or already there."""
        return self.hold_many((span,), mode=mode, at=at)[0]

    def hold_many(
        self,
        spans: Sequence[HeldSpan],
        *,
        mode: str = MODE_ON,
        at: str | None = None,
    ) -> tuple[HeldRecord, ...]:
        """Record every held passage of one recording, in a single transaction.

        All of them or none of them, for the same reason :mod:`transcriber.outputs` uploads
        three files or none: the transcript is about to be cut at these exact spans, and a
        store that holds four of five cuts is a service that has removed words from the
        record with no way for anybody to ask for them back.

        Re-holding a span already stored is not an error and not a duplicate: the same words
        at the same offsets of the same recording produce the same
        :attr:`HeldSpan.hold_id`, so a requeue or a re-run finds the existing row, records
        that it was seen again, and leaves any decision on it exactly as it was.
        """
        mode = normalise_mode(mode, MODE_ON)
        if mode == MODE_OFF and spans:
            raise WithheldError(
                "the gate is off, so nothing classified it and nothing may be held; this is "
                "a wiring mistake rather than a decision"
            )
        stamp = at or utc_now_iso()
        decision = Decision.PENDING if mode == MODE_ON else Decision.NOT_WITHHELD
        out: list[HeldRecord] = []
        with self._tx() as tx:
            for span in spans:
                hold_id = span.hold_id
                existing = tx.execute(
                    "SELECT * FROM holds WHERE hold_id=?", (hold_id,)
                ).fetchone()
                if existing is not None:
                    # A shadow row says, in its own words, that "nothing was actually cut"
                    # — that is what NOT_WITHHELD means. Once the gate is armed and this
                    # same passage is classified again, that stops being true: the masker
                    # cuts it, because masking works off the spans and the mode and never
                    # consults this row. The row was then a hold that had happened and that
                    # nobody could answer — absent from every queue, absent from the review
                    # page, absent from the morning email, with the words gone from the
                    # transcript and no way through the product to release them. That is the
                    # gate emptying itself quietly, which is the one failure this design
                    # exists to make impossible, arriving from the other direction.
                    #
                    # So an armed run promotes a shadow row to PENDING. This is the only
                    # state a machine may set (see Decision), and it is the conservative
                    # direction — it puts the passage IN FRONT OF a person, never past one.
                    # A row a person has already answered is never touched: RELEASED and
                    # REFUSED carry somebody's name and nothing here may overrule them.
                    became = existing["decision"]
                    promoted = mode == MODE_ON and existing["decision"] == Decision.NOT_WITHHELD
                    if promoted:
                        became = Decision.PENDING
                        # held_at moves to now because now is when it was actually held. The
                        # age on the review page is how long a person has been sitting on
                        # it, and nobody could have been sitting on something that was not
                        # in their queue.
                        tx.execute(
                            "UPDATE holds SET times_seen=times_seen+1, decision=?, mode=?, "
                            "held_at=? WHERE hold_id=?",
                            (Decision.PENDING, mode, stamp, hold_id),
                        )
                    else:
                        tx.execute(
                            "UPDATE holds SET times_seen=times_seen+1 WHERE hold_id=?", (hold_id,)
                        )
                    self._event(
                        tx,
                        hold_id,
                        span.item_id,
                        "armed" if promoted else "seen-again",
                        stamp,
                        actor="",
                        was=existing["decision"],
                        became=became,
                        detail=(
                            "the gate was armed after this passage was first seen in shadow, "
                            "so the words are now actually cut and somebody has to answer for "
                            "them; moved into the review queue and nothing was decided"
                            if promoted else
                            f"classified again in mode {mode}; the decision is untouched"
                        ),
                    )
                    refreshed = tx.execute(
                        "SELECT * FROM holds WHERE hold_id=?", (hold_id,)
                    ).fetchone()
                    out.append(_record(refreshed))
                    continue
                tx.execute(
                    """
                    INSERT INTO holds (
                        hold_id, ref, item_id, route, source_name, site, reviewer,
                        recorded_by, category, subject, text, reason, confidence,
                        start_offset, end_offset, context_before, context_after, speaker,
                        recorded_at, held_at, mode, decision, meta
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        hold_id,
                        span.ref,
                        span.item_id,
                        span.route or DEFAULT_ROUTE,
                        span.source_name or "",
                        span.site or "",
                        span.reviewer or "",
                        span.recorded_by or "",
                        span.category,
                        span.subject or "",
                        # The words themselves are stored exactly as they were said. This is
                        # the one place in the service that does not filter what it stores:
                        # everywhere else the text is on its way to a file somebody reads,
                        # and here it is the evidence a person needs in order to decide, and
                        # the only copy of it outside the audio.
                        span.text,
                        self._clean(span.reason),
                        None if span.confidence is None else float(span.confidence),
                        int(span.start),
                        int(span.end),
                        span.context_before or "",
                        span.context_after or "",
                        span.speaker,
                        span.recorded_at or "",
                        stamp,
                        mode,
                        decision,
                        json.dumps(dict(span.meta or {}), sort_keys=True),
                    ),
                )
                self._event(
                    tx,
                    hold_id,
                    span.item_id,
                    "held" if mode == MODE_ON else "would-have-held",
                    stamp,
                    actor="",
                    was=None,
                    became=decision,
                    detail=(
                        f"{span.length} characters, {span.category}, "
                        f"reviewer {span.reviewer or 'unassigned'}"
                    ),
                )
                row = tx.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
                out.append(_record(row))
        return tuple(out)

    def record_pass(
        self,
        item_id: str,
        *,
        route: str = DEFAULT_ROUTE,
        mode: str = MODE_SHADOW,
        spans: Sequence[HeldSpan] = (),
        transcript_chars: int = 0,
        classifier: str = "",
        notes: Sequence[str] = (),
        at: str | None = None,
    ) -> None:
        """One row per recording the classifier read, whether or not it held anything.

        The denominator of every number this gate is judged on. "Eleven passages held this
        week" answers nothing; "eleven passages across 214 recordings, 0.4% of the text,
        eight of them one category" is the number that decides whether the gate can be
        armed and whether the queue is survivable.

        ``classifier`` is the literal ``"rules"`` when the model did not answer the
        sensitivity question, and the model names when it did. That difference is not
        bookkeeping: a gate whose classifier is not running looks identical, in a count of
        holds, to a record with nothing sensitive in it — and both read as "ready to arm".

        ``notes`` is what the classifier could not stand behind on this recording. Stored
        in every mode, because in ``shadow`` there is no file for them to reach and the
        morning email is the only place anybody would see them.
        """
        stamp = at or utc_now_iso()
        categories = sorted({s.category for s in spans})
        with self._tx() as tx:
            tx.execute(
                """
                INSERT INTO passes (
                    item_id, route, at, mode, spans_found, characters_held,
                    transcript_chars, classifier, categories, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    route or DEFAULT_ROUTE,
                    stamp,
                    normalise_mode(mode),
                    len(spans),
                    sum(s.length for s in spans),
                    int(transcript_chars),
                    classifier or "",
                    json.dumps(categories),
                    json.dumps([str(n) for n in notes if str(n).strip()]),
                ),
            )

    def decide(
        self,
        hold_id: str,
        decision: str,
        *,
        answered_by: str,
        note: str = "",
        at: str | None = None,
        supersede: bool = False,
    ) -> HeldRecord:
        """Release or refuse one held passage, in the name of the person who said so.

        There is no third option and no automatic one. ``answered_by`` is required and is
        refused if it looks like a machine, because the single failure this gate cannot
        survive is something that empties the queue on its own while still presenting itself
        as a gate.

        A passage that already has a decision is not quietly overwritten: changing an answer
        needs ``supersede=True``, and both the old answer and the new one stay in the events
        table. Nothing is deleted, including a mind that was changed.
        """
        if decision not in Decision.DECIDABLE:
            raise WithheldError(
                f"{decision!r} is not a decision a person makes about a held passage; it is "
                f"{' or '.join(Decision.DECIDABLE)}"
            )
        who = (answered_by or "").strip()
        if not who:
            raise NotAPersonError(
                "a held passage can only be released or refused by a named person, and no "
                "name was given"
            )
        if who.casefold() in _NOT_A_PERSON:
            raise NotAPersonError(
                f"{who!r} is not a person. Nothing is decided for him on a timer, ever — a "
                "held passage is released because somebody read it and said so"
            )
        stamp = at or utc_now_iso()
        with self._tx() as tx:
            row = tx.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
            if row is None:
                raise UnknownHoldError(f"there is no held passage with id {hold_id!r}")
            was = str(row["decision"])
            if was == Decision.NOT_WITHHELD:
                raise WithheldError(
                    f"{hold_id!r} was recorded while the gate was in shadow, so nothing was "
                    "ever withheld and there is nothing to release or refuse"
                )
            if was in Decision.DECIDABLE and not supersede:
                raise AlreadyDecidedError(
                    f"{hold_id!r} was already {was} by {row['answered_by'] or 'somebody'} on "
                    f"{row['decided_at'] or 'an unrecorded date'}; pass supersede=True to "
                    "record a change of mind, which keeps both answers"
                )
            tx.execute(
                """
                UPDATE holds
                   SET decision=?, answered_by=?, decided_at=?, decision_note=?,
                       decisions_made=decisions_made+1
                 WHERE hold_id=?
                """,
                (decision, who, stamp, self._clean(note), hold_id),
            )
            self._event(
                tx,
                hold_id,
                str(row["item_id"]),
                "superseded" if was in Decision.DECIDABLE else decision,
                stamp,
                actor=who,
                was=was,
                became=decision,
                detail=self._clean(note) or None,
            )
            fresh = tx.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return _record(fresh)

    def release(self, hold_id: str, *, answered_by: str, note: str = "", **kw: Any) -> HeldRecord:
        """A person has read these words and said they may go into the record."""
        return self.decide(hold_id, Decision.RELEASED, answered_by=answered_by, note=note, **kw)

    def refuse(self, hold_id: str, *, answered_by: str, note: str = "", **kw: Any) -> HeldRecord:
        """A person has read these words and said they stay out.

        Recorded as a refusal and kept forever. A refused passage that had been deleted
        would read, six months later, exactly like a passage the classifier never caught —
        and those two need opposite responses.
        """
        return self.decide(hold_id, Decision.REFUSED, answered_by=answered_by, note=note, **kw)

    # -- reading -------------------------------------------------------------------

    def get(self, hold_id: str) -> HeldRecord | None:
        row = self._conn().execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        return _record(row) if row is not None else None

    def by_ref(self, ref: str) -> tuple[HeldRecord, ...]:
        """Every hold carrying this short reference — normally exactly one.

        A tuple rather than a single record because the reference is six characters and a
        person reads it off a page: on the day two collide, the caller must see both rather
        than be handed whichever the database returned first.
        """
        rows = self._conn().execute(
            "SELECT * FROM holds WHERE ref=? ORDER BY held_at", ((ref or "").strip().upper(),)
        ).fetchall()
        return tuple(_record(r) for r in rows)

    def for_recording(self, item_id: str, *, include_shadow: bool = True) -> tuple[HeldRecord, ...]:
        """Every hold from one recording, in the order the words were said."""
        sql = "SELECT * FROM holds WHERE item_id=?"
        params: list[Any] = [item_id]
        if not include_shadow:
            sql += " AND mode=?"
            params.append(MODE_ON)
        sql += " ORDER BY start_offset"
        rows = self._conn().execute(sql, tuple(params)).fetchall()
        return tuple(_record(r) for r in rows)

    def queue_for(
        self,
        reviewer: str,
        *,
        decision: str = Decision.PENDING,
        route: str | None = None,
        limit: int | None = None,
    ) -> tuple[HeldRecord, ...]:
        """One reviewer's own held passages, with the words, oldest first.

        The only method that returns text, and it answers for one named reviewer. Decision 6
        is not a convention here: a caller cannot ask this for "everybody", and a caller
        asking for somebody else's queue gets that person's rows only if it can name them —
        which is what the review page's sign-in decides, not this file.
        """
        who = (reviewer or "").strip()
        if not who:
            raise WithheldError(
                "a review queue belongs to a named person; asking for everybody's held text "
                "at once is the thing decision 6 rules out"
            )
        sql = "SELECT * FROM holds WHERE reviewer=? AND decision=?"
        params: list[Any] = [who, decision]
        if decision == Decision.PENDING:
            sql += " AND mode=?"
            params.append(MODE_ON)
        if route:
            sql += " AND route=?"
            params.append(route)
        sql += " ORDER BY held_at, start_offset"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn().execute(sql, tuple(params)).fetchall()
        return tuple(_record(r) for r in rows)

    def grouped_for(self, reviewer: str, **kw: Any) -> tuple[dict[str, Any], ...]:
        """The same queue, grouped by recording — which is how it is reviewed.

        Seconds per item means never making somebody rebuild the context of a call in their
        head five times because five passages of it are on a flat list.
        """
        groups: dict[str, dict[str, Any]] = {}
        for record in self.queue_for(reviewer, **kw):
            group = groups.setdefault(
                record.item_id,
                {
                    "item_id": record.item_id,
                    "source_name": record.source_name,
                    "site": record.site,
                    "route": record.route,
                    "recorded_at": record.recorded_at,
                    "held_at": record.held_at,
                    "records": [],
                },
            )
            group["records"].append(record)
        return tuple(groups.values())

    def forget(self, item_id: str) -> int:
        """Erase the held passages of one recording. Returns how many were forgotten.

        Called only by :mod:`transcriber.erase`, and it is the most consequential thing that
        module does. A held passage is the most sensitive text this service holds — a staff
        matter, somebody's health, an admission of liability — and it is exactly what a
        person asking to be forgotten is asking about.

        The row is kept and EMPTIED, the same way the ledger's is. What goes: the text, the
        context either side, the reason, the subject, the site, the speaker, the source
        name, the metadata. What stays: that a passage of some category was held on some
        date and was erased on this one. So a count of what the gate held over a period does
        not silently change when somebody exercises their rights — the measurement stays
        honest, and no words remain.

        The decision is NOT cleared, and that is deliberate: a passage a person refused is a
        decision that person made, and erasing the fact of it would rewrite their answer.
        """
        now = utc_now_iso()
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT hold_id FROM holds WHERE item_id=? AND text<>''", (item_id,)
            ).fetchall()
            if not rows:
                return 0
            conn.execute(
                "UPDATE holds SET text='', context_before='', context_after='', reason='',"
                " subject='', site='', speaker=NULL, source_name='', recorded_by='',"
                " decision_note='', meta='{}' WHERE item_id=?",
                (item_id,),
            )
            for row in rows:
                self._event(conn, row["hold_id"], item_id, "erased", now,
                            detail="the recording was erased at a person's request")
        return len(rows)

    def overview(
        self,
        *,
        decision: str = Decision.PENDING,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Counts, sites and ages — and not one word of what was said.

        This is what the morning email is built from, and what James sees for a passage that
        is not his to read: how many are waiting, on which sites, for how long, and whose
        queue they are in. It escalates the way he asked — the count, then the age, then the
        oldest one by name — and the name is the recording's, never the passage's contents.
        """
        stamp = now or utc_now_iso()
        sql = "SELECT * FROM holds WHERE decision=?"
        params: list[Any] = [decision]
        if decision == Decision.PENDING:
            sql += " AND mode=?"
            params.append(MODE_ON)
        rows = [_record(r) for r in self._conn().execute(sql + " ORDER BY held_at", tuple(params))]
        by_reviewer: dict[str, int] = {}
        by_site: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_route: dict[str, int] = {}
        for row in rows:
            by_reviewer[row.reviewer or "unassigned"] = by_reviewer.get(row.reviewer or "unassigned", 0) + 1
            by_site[row.site or "no site named"] = by_site.get(row.site or "no site named", 0) + 1
            by_category[row.category] = by_category.get(row.category, 0) + 1
            by_route[row.route] = by_route.get(row.route, 0) + 1
        oldest = rows[0].without_words() if rows else None
        return {
            "decision": decision,
            "count": len(rows),
            "recordings": len({r.item_id for r in rows}),
            "by_reviewer": by_reviewer,
            "by_site": by_site,
            "by_category": by_category,
            "by_route": by_route,
            "oldest": oldest.to_dict() if oldest is not None else None,
            "oldest_age_days": rows[0].age_days(stamp) if rows else 0,
            "ages_days": [r.age_days(stamp) for r in rows],
        }

    def counts_for_day(self, day: str) -> dict[str, int]:
        """What the gate did on one day: held, released, refused, and shadow sightings."""
        conn = self._conn()
        held = conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE substr(held_at,1,10)=? AND mode=?",
            (day, MODE_ON),
        ).fetchone()["n"]
        observed = conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE substr(held_at,1,10)=? AND mode=?",
            (day, MODE_SHADOW),
        ).fetchone()["n"]
        released = conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE substr(decided_at,1,10)=? AND decision=?",
            (day, Decision.RELEASED),
        ).fetchone()["n"]
        refused = conn.execute(
            "SELECT COUNT(*) AS n FROM holds WHERE substr(decided_at,1,10)=? AND decision=?",
            (day, Decision.REFUSED),
        ).fetchone()["n"]
        recordings = conn.execute(
            "SELECT COUNT(DISTINCT item_id) AS n FROM passes WHERE substr(at,1,10)=?", (day,)
        ).fetchone()["n"]
        return {
            "day": day,
            "held": int(held),
            "would_have_held": int(observed),
            "released": int(released),
            "refused": int(refused),
            "recordings_classified": int(recordings),
        }

    def measurement(self, *, since: str = "", until: str = "") -> dict[str, Any]:
        """The number that has to be real before the gate is armed.

        How many recordings were classified, how many carried anything at all, how many
        passages that came to, what fraction of the transcript they covered, and which
        categories they fell into. Read straight off the shadow run rather than estimated,
        because the five design passes estimated it and disagreed by a factor of
        twenty-five.

        And — because the number is worthless without it — **how many of those recordings
        the model actually answered the sensitivity question on.** Four of the six held
        categories cannot be seen by the mechanical rules at all: a staff matter, an
        identifiable person's health, KBC's attorney strategy, and its own cost against its
        own charge. A gate whose classifier is not running produces a low held fraction that
        is indistinguishable, in this dictionary, from a fortnight of clean recordings — and
        both would read as "ready". So the denominator is reported twice: recordings read,
        and recordings the model actually read. The digest refuses to call the measurement
        ready when those two numbers are far apart.
        """
        conn = self._conn()
        where, params = _range_clause("at", since, until)
        passes = conn.execute(f"SELECT * FROM passes {where}", params).fetchall()
        recordings = len({p["item_id"] for p in passes})
        touched = len({p["item_id"] for p in passes if int(p["spans_found"] or 0) > 0})
        held_chars = sum(int(p["characters_held"] or 0) for p in passes)
        total_chars = sum(int(p["transcript_chars"] or 0) for p in passes)
        hold_where, hold_params = _range_clause("held_at", since, until)
        by_category: dict[str, int] = {}
        for row in conn.execute(f"SELECT category, COUNT(*) AS n FROM holds {hold_where} GROUP BY category", hold_params):
            by_category[str(row["category"])] = int(row["n"])
        spans = sum(int(p["spans_found"] or 0) for p in passes)
        days = len({str(p["at"])[:10] for p in passes}) or 1

        # Which recordings the model actually read for sensitive passages, and which fell
        # back to the mechanical rules. ``record_pass`` writes the literal "rules" for the
        # second case and the model names for the first.
        read_by_model = {
            str(p["item_id"]) for p in passes
            if str(p["classifier"] or "").strip() not in ("", "rules")
        }
        rules_only = {str(p["item_id"]) for p in passes} - read_by_model

        notes: dict[str, int] = {}
        for row in passes:
            for note in _as_notes(row):
                notes[note] = notes.get(note, 0) + 1

        return {
            "since": since,
            "until": until,
            "recordings_classified": recordings,
            "recordings_with_a_hold": touched,
            "fraction_of_recordings": round(touched / recordings, 4) if recordings else 0.0,
            "recordings_the_model_read": len(read_by_model),
            "recordings_rules_only": len(rules_only),
            "fraction_the_model_read": (
                round(len(read_by_model) / recordings, 4) if recordings else 0.0
            ),
            "spans": spans,
            "spans_per_day": round(spans / days, 2),
            "characters_held": held_chars,
            "characters_read": total_chars,
            "fraction_of_text": round(held_chars / total_chars, 6) if total_chars else 0.0,
            "by_category": by_category,
            "days_measured": days,
            # What the classifier could not stand behind, and how often. Counts of a fixed
            # set of sentences, none of which carries a word of any recording.
            "notes": notes,
        }

    def released_for(self, item_id: str) -> dict[str, str]:
        """``{ref: the words}`` for every passage of one recording a person has released.

        What the republish path puts back. He asked for a held passage to be *released in
        place*, so the marker in the published transcript is replaced by what was actually
        said, in the same position, rather than appended somewhere as an afterthought.
        """
        rows = self._conn().execute(
            "SELECT ref, text FROM holds WHERE item_id=? AND decision=? ORDER BY start_offset",
            (item_id, Decision.RELEASED),
        ).fetchall()
        return {str(r["ref"]): str(r["text"]) for r in rows}

    def history(self, hold_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Everything that has ever happened to one held passage, oldest first."""
        rows = self._conn().execute(
            "SELECT * FROM hold_events WHERE hold_id=? ORDER BY id LIMIT ?",
            (hold_id, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """A whole-store summary, for ``transcriber status``. No text in it, ever."""
        conn = self._conn()
        by_decision: dict[str, int] = {}
        for row in conn.execute("SELECT decision, COUNT(*) AS n FROM holds GROUP BY decision"):
            by_decision[str(row["decision"])] = int(row["n"])
        return {
            "path": self.path,
            "schema_version": self.schema_version(),
            "holds": int(conn.execute("SELECT COUNT(*) AS n FROM holds").fetchone()["n"]),
            "by_decision": by_decision,
            "pending": by_decision.get(Decision.PENDING, 0),
            "recordings_classified": int(
                conn.execute("SELECT COUNT(DISTINCT item_id) AS n FROM passes").fetchone()["n"]
            ),
        }

    # -- internals -----------------------------------------------------------------

    def _clean(self, text: str | None) -> str:
        """Anything a model or a caller wrote, through the same filter as a log line.

        Applied to the classifier's reason and to a decision note — machine-authored or
        typed-in prose — and never to the held text itself, which is the recording's own
        words and the only copy of them outside the audio.
        """
        value = "" if text is None else str(text)
        if self._scrub is not None:
            try:
                value = str(self._scrub(value))
            except Exception:  # noqa: BLE001 - a broken scrubber must not lose the reason
                pass
        return strip_owner_paths(strip_dictated_emails(strip_emails(value)))

    def _event(
        self,
        conn: sqlite3.Connection,
        hold_id: str | None,
        item_id: str | None,
        kind: str,
        at: str,
        *,
        actor: str = "",
        was: str | None = None,
        became: str | None = None,
        detail: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO hold_events (hold_id, item_id, at, kind, actor, was, became, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (hold_id, item_id, at, kind, actor, was, became, (detail or "")[:2000] or None),
        )


def _record(row: Mapping[str, Any]) -> HeldRecord:
    return HeldRecord(
        hold_id=str(row["hold_id"]),
        ref=str(row["ref"]),
        item_id=str(row["item_id"]),
        route=str(row["route"] or DEFAULT_ROUTE),
        source_name=str(row["source_name"] or ""),
        site=str(row["site"] or ""),
        reviewer=str(row["reviewer"] or ""),
        recorded_by=str(row["recorded_by"] or ""),
        category=str(row["category"]),
        subject=str(row["subject"] or ""),
        text=str(row["text"]),
        reason=str(row["reason"] or ""),
        confidence=None if row["confidence"] is None else float(row["confidence"]),
        start=int(row["start_offset"]),
        end=int(row["end_offset"]),
        context_before=str(row["context_before"] or ""),
        context_after=str(row["context_after"] or ""),
        speaker=row["speaker"],
        recorded_at=str(row["recorded_at"] or ""),
        held_at=str(row["held_at"]),
        mode=str(row["mode"]),
        decision=str(row["decision"]),
        answered_by=str(row["answered_by"] or ""),
        decided_at=str(row["decided_at"] or ""),
        decision_note=str(row["decision_note"] or ""),
        times_seen=int(row["times_seen"] or 1),
        decisions_made=int(row["decisions_made"] or 0),
        meta=_decode_meta(row["meta"]),
    )


def _decode_meta(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _range_clause(column: str, since: str, until: str) -> tuple[str, tuple[Any, ...]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} <= ?")
        params.append(until)
    if not clauses:
        return "", ()
    return "WHERE " + " AND ".join(clauses), tuple(params)


_DAY_SECONDS = 86_400


def _days_between(earlier: str, later: str) -> int:
    """Whole days between two of our timestamps; 0 if either is unreadable.

    Deliberately arithmetic on the date part only. Nothing in this service acts on an age —
    the morning email says it, and a person decides — so an off-by-one here is a sentence
    reading slightly wrong, never a passage released or discarded.
    """
    try:
        from datetime import datetime, timezone

        first = datetime.strptime(day_of(earlier), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        second = datetime.strptime(day_of(later), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    return max(0, int((second - first).total_seconds() // _DAY_SECONDS))
