"""The three markdown files, and getting all three of them into OneDrive or none of them.

This is the module the downstream record actually reads, so its format is a contract rather
than a preference. ``kbc-site-memory/tools/ingest.py:parse_texty`` reads a ``.md`` drop as a
block of ``^(from|to|cc|subject|date|sent)\\s*:\\s*(.*)$`` lines terminated by the first
blank line, then a body. Three things follow from that, and all three are enforced
mechanically below rather than left to whoever edits this file next:

* **A ``From:`` header reclassifies the file as an email.** It would stop being a transcript,
  stop being filed as a call, and start being attributed to a sender. We emit none.
* **Only those six keys are recognised, and a non-matching non-blank line inside the header
  block is silently swallowed** — it reaches neither the header nor the body. A stray
  ``Recording: x.m4a`` line above the blank line does not fail; it disappears. So the header
  is ``Subject:`` and ``Date:``, then exactly one blank line, and every other piece of
  metadata lives in the body.
* **The ``Date:`` value must carry its ISO date followed by a space.** The record's
  ``parse_date`` looks for ``\\b20\\d\\d-\\d\\d-\\d\\d\\b`` first and falls through to a
  year-month pattern that returns the **first of the month**. ``2026-08-27T14:30:05`` has no
  word boundary after the day, so it would quietly file as ``2026-08-01`` — and that date
  becomes the item's id and its month folder in the record.

:func:`check_contract` re-parses every file we render with a faithful copy of that scan and
compares the body it recovers against the body we meant to write. A rendering mistake fails
here, offline, before anything is uploaded — not silently, six weeks later, in the record.

The other half of the job is that the three files land together. The incumbent has at least
one recording with a summary and no transcript, which is the worst possible remainder: the
derived reading survives and the evidence for it does not. Rendering happens for all three
before any upload begins, the transcript goes up first, each one is read back from Graph
before success is returned, and nothing short of all three lets the ledger advance.

Held passages, and why all three files are gated equally
--------------------------------------------------------

When the sensitivity gate is armed, the pipeline hands this module a transcript whose held
passages have already been cut out and replaced by their markers, and — in
:attr:`OutputContext.held` — the passages themselves, so that the last thing standing
between held words and OneDrive is a check that does not trust any of the work above it.

**All three files are still written, on time, every time.** Only the *words* wait. That is
what makes "hold indefinitely" a safe default rather than a way to lose recordings: nothing
about a passage awaiting approval delays the transcript, the summary or the proposals, and
the marker left in place says a passage is held, what kind it is, when it was said and how
to ask for it. A hole that says nothing would be indistinguishable from a conversation in
which nothing was said, and that is the failure this whole service exists to cure.

**The summary and the actions file are gated exactly as tightly as the transcript**, and
they have to be, because they are the more likely leak. Both are written by a model reading
the *unredacted* transcript — it must be unredacted, or quote verification cannot tell an
invented quote from a masked one — so the model can and does restate in its own prose what
the transcript masks in its own words. :func:`transcriber.redact.redact_extraction` masks
them from the same redaction that cut the transcript; :func:`refuse_held_text` then re-reads
every rendered file, whole, with :func:`transcriber.redact.contains_any_held`, and refuses
**the entire publish** if any of the three still carries a held passage — not just the file
that carries it. A partially redacted set is worse than an unredacted one, because it looks
redacted. That check shares no reasoning with the masking it is checking: the masker is the
thing that might have a bug, and a guard that reasons the same way guards nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .models import (
    DICTATED_EMAIL_RE,
    EMAIL_PLACEHOLDER,
    EMAIL_RE,
    AudioInfo,
    Segment,
    Transcript,
    OWNER_PATH_RE,
    contains_dictated_email,
    contains_email,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
)
from .naming import OutputNames, ParsedName, output_names
from . import redact
from . import sensitivity
from .redact import contains_any_held, held_words_in

# extract.Extraction and its proposals are read by attribute rather than imported. The
# renderers stay pure functions over plain data, testable with a stand-in and with no
# import back into the analysis pass.

__all__ = [
    "OutputContractError",
    "HeldTextWouldLeak",
    "UploadIncompleteError",
    "refuse_held_text",
    "refuse_written_down_again",
    "spoken_body",
    "OutputContext",
    "RenderedFile",
    "UploadedFile",
    "UploadResult",
    "render_transcript",
    "render_summary",
    "render_actions",
    "render_all",
    "check_name",
    "upload_outputs",
    "publish",
    "check_contract",
    "parse_like_downstream",
]

log = logging.getLogger(__name__)

#: The record's own header pattern, character for character. Copied rather than imported
#: because the record is a different repository on a different machine; if it ever changes,
#: this copy is what the test suite compares against and what fails loudly.
_HEADER_RE = re.compile(r"^(from|to|cc|subject|date|sent)\s*:\s*(.*)$", re.I)

#: The two branches of the record's ``parse_date`` that our ``Date:`` line can reach.
_ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

_ALLOWED_HEADERS = ("subject", "date")

#: The record truncates a subject at 90 characters when it writes the correspondence row.
#: Staying inside that means our subject is never the thing that gets cut.
_MAX_SUBJECT = 90

#: Replaced with a redaction note, or removed, once the body is known. See :func:`_scrub`.
_REDACTION_SLOT = "\x00redaction-note\x00"

#: OneDrive for Business puts the owner's address in the path: ``/personal/james_kbc_co_za/``
#: is a UPN with ``@`` and ``.`` rewritten as ``_``, and it reverses by splitting on the
#: underscore. :data:`EMAIL_RE` cannot see it — there is no ``@`` — so it is the one encoding
#: of an address that neither our guard nor the record's own address check recognises. It is
#: refused by name here, and :func:`_safe_web_url` removes it before anything is rendered.
_UPN_PATH_RE = OWNER_PATH_RE

#: The line :func:`spoken_body` writes for each segment: ``[HH:MM:SS] Speaker: words``.
#: Removed before the backstop reads a file, and for one reason: an address does not know
#: where the segments were cut. Segments break on a speaker change or a pause over 0.9 s, so
#: a pause in the middle of an address dictated slowly puts "carel@" at the end of one line
#: and "example.co.za" at the start of the next, with a timestamp and a name in between. No
#: pattern can read across that, which is why :func:`spoken_body` masks it before the line
#: prefixes go on. This is the check that the masking actually happened.
_SEGMENT_PREFIX_RE = re.compile(r"(?m)^\[[0-9:]{4,9}\]\s*(?:[^\s:][^:\n]{0,60}:\s*)?")

#: The two encodings of ``@`` a web form leaves behind, and the ``at``/``dot`` a person
#: says. Used only by :func:`_normalise_for_address_scan`.
_PERCENT_OR_ENTITY_AT_RE = re.compile(r"%40|&#0*64;|&#x0*40;|&commat;", re.I)
_BRACKETED_AT_RE = re.compile(r"\s*[<\[({]\s*at\s*[>\])}]\s*", re.I)
_SPACED_AT_RE = re.compile(r"\s*[@\uFF20\uFE6B]\s*")
_SPOKEN_DOT_RE = re.compile(r"(?<=[A-Za-z0-9])\s*[<\[({]?\s*dot\s*[>\])}]?\s*(?=[A-Za-z0-9])", re.I)

#: What an address looks like once every spelling of it has become the same spelling. This
#: is the only address check in this module that does not call the masker's own detectors,
#: and that is the whole point of it: :func:`check_contract` used to ask
#: :func:`transcriber.models.contains_email` the same question :func:`_scrub` had just asked,
#: so a spelling the masker could not see was a spelling the backstop could not see either,
#: and the two failed together by construction. Seven ordinary spellings went through both.
#:
#: The independence is in WHAT IT READS — a normalised copy — rather than in being a wider
#: pattern. Deliberately: a backstop wider than the masker refuses a publish the masker can
#: never satisfy, and this module's refusals are not retried, so the recording quarantines
#: forever with none of its three files written. Wider than the masker is not "safer" here;
#: it is the other way of losing the recording.
_NORMALISED_ADDRESS_RE = re.compile(r"[\w.%+\-]*\w[\w.%+\-]*@(?:[\w\-]+\.)+[^\W\d_]{2,}")


def _normalise_for_address_scan(text: str) -> str:
    """Every spelling of an address, reduced to the one spelling, for a yes/no read.

    Not usable for masking — it moves every offset in the string, so nothing found here can
    be put back in the right place. It exists to answer one question after the masking is
    done: is there still an address in this file, however it is written?
    """
    flat = re.sub(r"\s+", " ", text or "")
    flat = _PERCENT_OR_ENTITY_AT_RE.sub("@", flat)
    flat = _BRACKETED_AT_RE.sub("@", flat)
    flat = _SPACED_AT_RE.sub("@", flat)
    return _SPOKEN_DOT_RE.sub(".", flat)


#: The pipeline writing ``decided_by`` on a line of its own is forbidden. A person *saying*
#: the words on a recording is not: the transcript is the evidence, and the guard aimed at
#: our own metadata must not be able to quarantine what was actually said. Anchored to a
#: line-start field emission, which is the only shape this service could ever produce.
_DECIDED_BY_RE = re.compile(r"(?mi)^\s*(?:[-*>]\s*)*(?:\*\*)?decided_by(?:\*\*)?\s*:")

_CATEGORY_HEADINGS = {
    "decisions": "Things that sounded like a decision",
    "commitments": "Things that sounded like a commitment",
    "money": "Money that came up",
    "materials": "Materials that came up",
    "defects": "Defects that came up",
    "safety": "Safety that came up",
    "programme": "Programme and dates that came up",
    "open_questions": "Questions raised",
    "follow_ups": "Follow-ups suggested",
}


class OutputContractError(ValueError):
    """A rendered file would not survive the downstream parser, or breaks a house rule.

    Raised at render time, before any upload, so the recording is quarantined with a
    readable reason instead of being filed as a success that the record mis-reads.
    """


class HeldTextWouldLeak(OutputContractError):
    """A rendered file still carries words a person has not agreed to write down.

    Raised before any upload, and it stops **all three** files, not the one that carries the
    passage. Two files redacted and one not is worse than three unredacted ones: it looks
    like the gate worked.

    An :class:`OutputContractError` deliberately, so it takes the path this service already
    has for a file it must not write — never retried, because the same transcript renders
    the same way, and quarantined loudly with the reason in words a person can act on. What
    a person needs to know is *which* passage survived and into *which* file, so both are
    named; the words themselves are not, because naming them here would put them in the
    ledger, the log and the morning email, which is the leak this stopped.
    """

    def __init__(self, message: str, *, refs: Sequence[str] = (), files: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.refs = tuple(refs)
        self.files = tuple(files)


class UploadIncompleteError(RuntimeError):
    """Fewer than three files are confirmed present. The ledger must not advance.

    Carries what landed, what did not, and what was left behind, because a person reading
    the quarantine reason needs to know whether OneDrive holds a partial set. Re-running is
    safe and is the fix: the three names are derived from the recording, and every upload
    replaces by name rather than adding a second copy.
    """

    def __init__(
        self,
        message: str,
        *,
        uploaded: Sequence[str] = (),
        missing: Sequence[str] = (),
        orphans: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.uploaded = tuple(uploaded)
        self.missing = tuple(missing)
        self.orphans = tuple(orphans)


@dataclass(frozen=True)
class OutputContext:
    """Everything the three files are rendered from. No network, no clock, no I/O.

    Holding it in one frozen object is what makes the renderers pure and the test suite
    able to assert on their exact bytes.
    """

    item_id: str
    source_name: str
    parsed: ParsedName
    recorded_at: datetime
    timestamp_source: str
    transcript: Transcript
    extraction: Any | None = None          # extract.Extraction, duck-typed
    audio: AudioInfo | None = None
    content_hash: str = ""                 # sha256 of the audio we downloaded
    graph_hash: str = ""                   # what Graph reported for the same item
    web_url: str = ""
    engine: str = ""
    notes: tuple[str, ...] = ()
    #: The passages cut out of ``transcript`` before it got here — the pipeline's
    #: :class:`transcriber.withheld.HeldSpan` objects, carrying the words themselves so that
    #: :func:`refuse_held_text` can search every rendered file for them.
    #:
    #: Empty in ``GATE_MODE=off`` and in ``shadow``, because those modes cut nothing and so
    #: nothing can have survived a cut. Empty is therefore not "unknown" and not "not
    #: checked": it is "there was nothing to leak", which is why the backstop can treat an
    #: empty tuple as a pass rather than as a reason to refuse.
    #:
    #: ``repr=False`` and out of every rendered line: these are the words the whole gate
    #: exists to keep out of a file.
    held: tuple[Any, ...] = field(default=(), repr=False)
    #: What to call this recording to a person, when the service worked it out rather than
    #: reading it off the filename. Empty means the filename, which is every recording he
    #: named himself and every one where :mod:`transcriber.autoname` refused.
    #:
    #: It reaches the Subject line and the heading and **nothing else**. In particular it
    #: never reaches :attr:`names`, which stays a pure function of the recorded moment, the
    #: source stem, the copy marker and the item id — because a failed publish is recovered
    #: by writing the same three names again, and a name that could change between attempts
    #: would leave three files nobody can delete and a second document in the record.
    display_name: str = ""
    #: :class:`transcriber.sitebook.SiteEvidence` for this recording — which jobs the site
    #: list says it names, with the scores and the words behind them. Computed in the
    #: pipeline, which holds the book; this module stays pure and only renders it.
    #:
    #: It appears ONLY in the summary and actions files, never in the transcript. The
    #: transcript is the one file the record ingests as a source and scores, and a name
    #: written into it becomes evidence for the very question it was trying to answer — a
    #: mis-transcription would then confirm itself and file the recording under the wrong
    #: job, looking well supported. The other two carry a leading underscore, which is how
    #: the record's intake knows to skip them, so nothing here can move a filing.
    site_evidence: Any = None

    @property
    def names(self) -> OutputNames:
        """The three names, unique to this recording rather than to its filename.

        ``item_id`` is threaded through deliberately: two OneDrive duplicates of one name are
        two different recordings, and a name derived from the filename alone would have the
        second overwrite the first with every check still passing.
        """
        return output_names(
            self.recorded_at,
            self.parsed.stem,
            copy_marker=self.parsed.copy_marker,
            item_id=self.item_id,
        )

    @property
    def engine_name(self) -> str:
        return self.engine or self.transcript.engine or "not recorded"

    @property
    def duration_s(self) -> float | None:
        if self.audio is not None and self.audio.duration_s > 0:
            return self.audio.duration_s
        if self.transcript.duration_s:
            return self.transcript.duration_s
        return None

    @property
    def label(self) -> str:
        """How this recording is named to a human: the party if the filename gave one."""
        if self.display_name:
            return self.display_name
        if self.parsed.party:
            return f"Call with {self.parsed.party}"
        return self.parsed.stem or self.source_name or "Voice note"


@dataclass(frozen=True)
class RenderedFile:
    """One finished file: its name, its exact text, and which of the three it is."""

    kind: str            # "transcript" | "summary" | "actions"
    name: str
    text: str

    @property
    def data(self) -> bytes:
        return self.text.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class UploadedFile:
    kind: str
    name: str
    item_id: str
    size: int
    web_url: str = ""
    verified_bytes: bool = False


@dataclass(frozen=True)
class UploadResult:
    """Three files, all confirmed present by a read-back. Anything less raises instead."""

    parent_id: str
    files: tuple[UploadedFile, ...] = ()

    @property
    def names(self) -> dict[str, str]:
        """Keyed by kind, ready for the ledger's transcript/summary/actions columns."""
        return {f.kind: f.name for f in self.files}

    @property
    def item_ids(self) -> dict[str, str]:
        return {f.kind: f.item_id for f in self.files}

    @property
    def complete(self) -> bool:
        return {f.kind for f in self.files} == {"transcript", "summary", "actions"}


# --------------------------------------------------------------------------- rendering


def spoken_body(transcript: Transcript) -> str:
    """The words of the recording exactly as they are published, and nothing else.

    The one definition, used by :func:`render_transcript` to write the file and by
    :mod:`transcriber.autoname` to decide what the recording says. They must be the same
    string, and the reason is not tidiness.

    ``Transcript.text`` is the engine's own continuous prose. The published body is not
    that: when the engine returned segments it is one line per segment, each prefixed
    ``[MM:SS] Speaker: ``, cut on a speaker change or a pause over 0.9 s. So a two-word site
    name spoken either side of a breath — "Beach ... Court" — is contiguous in ``text`` and
    **split across two lines in the file**. Deciding from ``text`` would propose a title the
    published bytes do not contain, and the record, which reads the file and not the prose,
    would score it differently. That is not hypothetical: it is how a walk at one site got
    filed to another in testing.
    """
    segments = list(transcript.segments or ())
    if segments:
        texts = _mask_across_segments([str(s.text or "") for s in segments])
        return "\n".join(_segment_line(s, text) for s, text in zip(segments, texts))
    return (transcript.text or "").strip()


def _mask_across_segments(texts: Sequence[str]) -> list[str]:
    """Remove an address that the cut between two segments split in half.

    :func:`_scrub` reads the finished file, and by then every segment is its own line with a
    timestamp and a speaker in front of it. No pattern reads across that, so an address the
    engine happened to cut in two — "send it to carel@" ... "example.co.za when you can" —
    went into the published file in both halves, one under the other, and the contract check
    that exists to catch exactly this saw nothing. The cut lands there whenever the speaker
    pauses in the middle of dictating an address, which is when people pause.

    So the address is looked for in the segments joined as one run of text, where it is
    whole, and the cuts are then mapped back onto the individual segments: the first segment
    the address touches keeps the marker, the rest of it goes. The words either side are
    untouched, and the reader sees a marker in the place the address was said rather than a
    hole.
    """
    joined = "\n".join(texts)
    if not (contains_email(joined) or contains_dictated_email(joined)):
        return list(texts)

    spans = [m.span() for m in EMAIL_RE.finditer(joined)]
    spans += [m.span() for m in DICTATED_EMAIL_RE.finditer(joined)]
    # Only the ones the cut split. Anything inside a single segment is already the case
    # :func:`_scrub` handles on the finished file, and it handles it better: it says in the
    # file that an address was removed, which this cannot do from here.
    spans = sorted({(a, b) for a, b in spans if "\n" in joined[a:b]})
    if not spans:
        return list(texts)

    # Two detectors reading the same words can return two spans that overlap without being
    # the same span, and replacing one of them first would move the other one's offsets off
    # its own words. Merged into one run before anything is cut, so every replacement below
    # is over a piece of text nothing else touches.
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    spans = merged

    out = list(texts)
    offset = 0
    bounds: list[tuple[int, int]] = []
    for text in texts:
        bounds.append((offset, offset + len(text)))
        offset += len(text) + 1  # the "\n" the join put between them

    # Right to left through the address and right to left through the segments, so that a
    # replacement already made cannot move the offsets of one still to come.
    for start, end in reversed(spans):
        for index in range(len(out) - 1, -1, -1):
            low, high = bounds[index]
            cut_from, cut_to = max(start, low), min(end, high)
            if cut_from >= cut_to:
                continue
            # ``start >= low`` is true of exactly one segment: the one the address begins
            # in. That is where the marker goes; the continuation of it simply ends.
            out[index] = (
                out[index][: cut_from - low]
                + (EMAIL_PLACEHOLDER if start >= low else "")
                + out[index][cut_to - low:]
            )
    return out


def render_transcript(ctx: OutputContext) -> str:
    """The transcript file — the one the record ingests, and the only copy of what was said.

    Speaker-labelled where the engine diarised, and not where it did not: inventing
    "Speaker 1" for an engine that returned no speakers would be the pipeline asserting
    something about the recording that the recording does not say.
    """
    text = (ctx.transcript.text or "").strip()
    segments = list(ctx.transcript.segments or ())
    if not text and not segments:
        raise OutputContractError(
            f"{ctx.source_name!r} produced no transcript text. An empty transcript is a "
            f"plausibility failure and belongs in quarantine, never in the record"
        )

    body = [
        f"# {_title(ctx, 'voice note transcript')}",
        "",
        "Machine transcription of a voice note, kept as the evidence of what was said and",
        "nothing more. It states no status, reaches no conclusion and decides nothing.",
        "`observed_by: agent`.",
        "",
    ]
    # Before the provenance and before a word of the call. The record files the FIRST
    # twenty questions it finds in a transcript, in the order they appear, and a hold
    # marker sits down in the body among forty questions from a site walk — so on exactly
    # the recordings most likely to carry a hold, every hold question fell off the end of
    # that cap and the site's live page said nothing. Up here it cannot be pushed off, and
    # because it is the marker's own sentence the record de-duplicates the two into one.
    body += redact.held_preamble(
        ctx.held,
        site=str(getattr(ctx.extraction, "site", "") or ""),
    )
    body += _provenance(ctx)
    body += ["", "## What was said", ""]

    if segments:
        body += spoken_body(ctx.transcript).split("\n")
    else:
        body += [
            "The engine returned no segment timings, so this is the transcript as one run",
            "of text. Nothing has been re-ordered or joined up.",
            "",
        ]
        body += text.split("\n")

    if segments and text and _word_count(segments) == 0:
        raise OutputContractError(
            f"{ctx.source_name!r} has segments but no words in any of them"
        )

    body += ["", "---", ""]
    body += [
        f"Summary of this recording: `{ctx.names.summary}`",
        f"Proposals from this recording: `{ctx.names.actions}`",
        "",
        "Neither of those two files is evidence. This one is.",
    ]
    return _finalise(_subject(ctx, "voice note transcript"), ctx, body)


def render_summary(ctx: OutputContext) -> str:
    """What was discussed — a machine's reading of the transcript, labelled as one."""
    extraction = ctx.extraction
    body = [
        f"# {_title(ctx, 'voice note summary')}",
        "",
        "A machine's reading of the transcript. Every line is `observed_by: agent`: none of",
        "it is a status, a decision, or a fact about the job, and none of it supersedes the",
        f"transcript, which is in `{ctx.names.transcript}`.",
        "",
    ]
    body += _provenance(ctx)
    body += ["", "## What was discussed", ""]

    summary_text = _text_of(extraction, "summary")
    if summary_text:
        body += summary_text.strip().split("\n")
    elif extraction is None:
        body += [
            "No summary was produced: the analysis pass did not run for this recording.",
            "The transcript is complete and is the record. This gap is deliberate and",
            "visible — it is not an empty summary standing in for a real one.",
        ]
    else:
        body += [
            "The analysis pass produced no summary for this recording.",
            _routing_sentence(extraction) or "No reason was recorded for that.",
        ]

    body += _participants_block(extraction)
    body += _site_block(extraction)
    body += _site_evidence_lines(ctx)
    body += _unclear_block(extraction)
    body += _routing_block(extraction)
    body += _review_block(extraction, ctx)

    body += [
        "",
        "---",
        "",
        f"Proposals from this recording: `{ctx.names.actions}`",
    ]
    return _finalise(_subject(ctx, "voice note summary"), ctx, body)


def render_actions(ctx: OutputContext) -> str:
    """Commitments and questions, as proposals for a person — never as things settled.

    Every item carries the words it came from and ``observed_by: agent``. An item whose
    quote was not verified is refused outright rather than written out with a caveat: the
    guard against a misheard word hardening into a task is only a guard if it cannot be
    talked past.
    """
    extraction = ctx.extraction
    proposals = _proposals(extraction)
    _refuse_unverified(proposals, ctx)

    body = [
        f"# {_title(ctx, 'proposals to confirm')}",
        "",
        "**Nothing here has been decided.** Each item is something a machine heard in a",
        "voice note and is putting to a person, with the words it heard. Confirm it, correct",
        "it or throw it away — until somebody does, none of it is on the record and none of",
        "it closes anything. Every item is `observed_by: agent`; no item carries an owner",
        "who decided it, because this pipeline cannot decide.",
        "",
    ]
    body += _provenance(ctx)
    body += [
        f"- Proposals put forward: {len(proposals)}",
        f"- Transcript: `{ctx.names.transcript}`",
    ]

    if not proposals:
        body += [
            "",
            "## Nothing was proposed",
            "",
            "Nothing in this recording read as a commitment, a question or anything else",
            "needing a person. That is an observation about the recording, not a finding",
            "about the job — the transcript is the record, and it is complete.",
        ]
        if extraction is None:
            body += [
                "",
                "The analysis pass did not run for this recording, so nothing was looked for.",
            ]
        else:
            sentence = _routing_sentence(extraction)
            if sentence:
                body += ["", sentence]
    else:
        number = 0
        for category, group in _grouped(extraction, proposals):
            body += ["", f"## {_CATEGORY_HEADINGS.get(category, _humanise(category))}", ""]
            for proposal in group:
                number += 1
                body += _proposal_block(number, proposal,
                                        getattr(ctx, "site_evidence", None))

    body += _review_block(extraction, ctx)
    body += [
        "",
        "---",
        "",
        "To act on any of this, a person confirms it. This file records that it was heard,",
        "not that it is so.",
    ]
    return _finalise(_subject(ctx, "proposals to confirm (nothing decided)"), ctx, body)


def render_all(ctx: OutputContext) -> tuple[RenderedFile, ...]:
    """All three files, rendered and contract-checked before a single byte is uploaded.

    The transcript is first in the tuple and first onto the wire. If a partial set ever did
    survive a failure, the evidence being the part that landed is the only remainder worth
    having — the incumbent's fault is the exact inverse of it.
    """
    names = ctx.names
    problems = [p for name in names.as_tuple() for p in check_name(name)]
    if problems:
        raise OutputContractError(
            f"{ctx.source_name!r} would be written under a name this service may not "
            "write: " + "; ".join(problems)
        )
    if len(set(names.as_tuple())) != 3:
        raise OutputContractError(
            f"{ctx.source_name!r} produced fewer than three distinct output names "
            f"({', '.join(names.as_tuple())}); two files would overwrite each other"
        )
    files = (
        RenderedFile("transcript", names.transcript, render_transcript(ctx)),
        RenderedFile("summary", names.summary, render_summary(ctx)),
        RenderedFile("actions", names.actions, render_actions(ctx)),
    )
    refuse_held_text(files, ctx.held, source_name=ctx.source_name)
    refuse_written_down_again(
        files, source_name=ctx.source_name, armed=bool(ctx.held)
    )
    return files


def refuse_held_text(
    files: Sequence[RenderedFile],
    held: Sequence[Any],
    *,
    source_name: str = "",
) -> None:
    """Refuse the whole publish if any rendered file still carries a held passage.

    The mechanical backstop, and deliberately a dumb one. It does not know how the masking
    was done, does not consult the redaction that did it, and does not trust that it worked:
    it takes the words that were cut out of the transcript and looks for them, whole or in
    runs, in the finished bytes of all three files. The thing most likely to have a bug is
    the masker, and a guard that shares the masker's reasoning guards nothing.

    **All three files are refused together, whichever one carries the passage.** A set with
    the transcript masked and the summary not is worse than a set with neither masked,
    because it presents itself as redacted and nobody looks twice at it.

    The three files are checked on equal terms, and the two derived ones are the ones this
    is really for. The transcript was cut at exact offsets and is the easy case; the summary
    and the proposals are a model's prose about the *unredacted* transcript — it has to be
    unredacted, or quote verification cannot tell an invented quote from a masked one — so
    they are free to restate in other words what the transcript no longer says. They are
    masked upstream by searching for the held words; this is what catches the occasion when
    that search missed.

    **What it does not catch, stated plainly because the docstring used to imply otherwise:
    a restatement.** This is a search for the held *words*. A model summarising a held staff
    matter in its own prose — "there is a hearing for Marius on Friday and he will probably
    lose his job", against a held "Marius has his disciplinary hearing on Friday" — shares
    neither the whole passage nor a run of five consecutive words, and passes here. The two
    things that address it are :data:`transcriber.prompts.SENSITIVITY_NOTE`, which requires
    the model to keep its own prose clear of what it flags, and
    :func:`refuse_written_down_again`, which re-reads the derived files with the mechanical
    rules and so catches the part of a rewriting that is mechanically decidable. Neither is
    sufficient and both are needed; a word search is not the last line and must not be read
    as one.

    Empty ``held`` is a pass, not a skip: nothing was cut, so there is nothing that could
    have survived a cut. That is the state of every recording in ``shadow`` and ``off``.
    """
    spans = tuple(held or ())
    if not spans:
        return
    problems: list[str] = []
    refs: list[str] = []
    leaking: list[str] = []
    for rendered in files:
        if not contains_any_held(rendered.text, spans):
            continue
        # Asked again, for the detail: which passage, and how much of it is showing. The
        # answer above is the one that decides; this only writes the sentence a person reads.
        for span, words, why in held_words_in(rendered.text, spans):
            ref = str(getattr(span, "ref", "") or "?")
            phrase = str(getattr(span, "phrase", "") or "a held passage")
            refs.append(ref)
            leaking.append(rendered.name)
            problems.append(
                f"the {rendered.kind} file ({rendered.name}) still contains held passage "
                f"{ref} ({phrase}) — {why}"
            )
    if not problems:
        return
    raise HeldTextWouldLeak(
        f"{source_name or 'this recording'} has passages a person has not agreed to write "
        f"down yet, and they survived into the files that were about to be uploaded, so "
        f"none of the three has been written: " + "; ".join(problems) + ". Nothing was "
        "uploaded, nothing was moved and nothing was deleted; the passages are in the held "
        "queue and the recording is where it was.",
        refs=tuple(dict.fromkeys(refs)),
        files=tuple(dict.fromkeys(leaking)),
    )


#: A sha256 as this service writes it into the provenance rows. Never a thing a person
#: said, so never a thing a held passage can be a paraphrase of.
_DIGEST_RE = re.compile(r"\b[0-9a-f]{32,64}\b")


def _our_own_identifiers(files: Sequence[RenderedFile]) -> tuple[str, ...]:
    """The strings this service generated and then wrote into its own files.

    The three output names, and their stems without the extension, because the summary and
    the proposals cross-reference each other by name. Longest first, so removing one cannot
    leave the tail of another behind.
    """
    names: set[str] = set()
    for rendered in files:
        name = (rendered.name or "").strip()
        if not name:
            continue
        names.add(name)
        stem = name.rsplit(".", 1)[0]
        if stem and stem != name:
            names.add(stem)
    return tuple(sorted(names, key=len, reverse=True))


def _without(text: str, ours: Sequence[str]) -> str:
    """``text`` with this service's own identifiers taken out, for the rules to read.

    Replaced with a marker of the same shape rather than deleted, so offsets stay roughly
    honest and two sentences either side of a filename do not run together into one.
    """
    cleaned = text
    for name in ours:
        if name:
            cleaned = cleaned.replace(name, "[our own filename]")
    return _DIGEST_RE.sub("[our own hash]", cleaned)


def refuse_written_down_again(
    files: Sequence[RenderedFile],
    *,
    source_name: str = "",
    armed: bool = False,
) -> None:
    """Re-read the finished files with the mechanical rules, not with a word search.

    The second backstop, and it exists because the first one cannot see the case that
    matters. :func:`refuse_held_text` searches for the held *words*; a model restating a
    held passage in its own prose shares none of them, satisfies neither the whole-passage
    match nor the five-consecutive-word run, and is written to OneDrive. The summary and the
    proposals are both written by a model reading the unredacted transcript, and both go
    into the route's output folder — which is where James looks — so decision 6 is broken
    for a staff member's passage by a paraphrase alone.

    What this catches is the part that is mechanically decidable in a rewriting: an explicit
    request that something not be written down, restated, and a bare identity or account
    number, which survives a paraphrase intact because a number cannot be paraphrased. It
    cannot catch a reworded staff matter — nothing here can, and the honest place that is
    solved is the prompt, which now tells the model to keep its prose clear of what it
    flagged. Both are needed and neither is sufficient; this one is here because it is the
    half that does not depend on a model doing as it was asked.

    Runs only when the gate is armed. In ``shadow`` and ``off`` nothing was withheld, and a
    check that stopped a publish would be a measurement changing what reaches the record.
    """
    if not armed:
        return
    # What this service itself wrote into the file is not what this check is looking for.
    # It is looking for the MODEL restating a held passage in prose of its own, and our own
    # filenames and hashes are neither prose nor the model's. Scanning them was not merely
    # pointless, it was actively destructive: the summary and the proposals each carry
    # backticked cross-references to the other two names, and a stem like
    # ``_20260827-143005-...`` strips to the fourteen digits ``20260827143005``, which sits
    # in the 13-to-19 range the identifier rule calls a card and passes Luhn about one time
    # in ten. Measured over five thousand plausible recording moments: 10.6% trip it. Once
    # the gate is armed that refuses the publish, and HeldTextWouldLeak is in
    # ``pipeline._NEVER_RETRY`` — so roughly one gated recording in nine would have been
    # quarantined for ever, re-rendering the identical bytes on every retry, and the morning
    # email would have reported a near-leak of a card number that does not exist. A gate
    # that eats a day's recordings for a number it invented is a gate that gets switched
    # off, which is the failure the whole design is built against.
    ours = _our_own_identifiers(files)
    problems: list[str] = []
    leaking: list[str] = []
    for rendered in files:
        if rendered.kind == "transcript":
            # The transcript is the recording's own words, cut on exact offsets. A rule
            # firing on it would be firing on the marker's neighbours or on something the
            # classifier deliberately let through, and refusing there would quarantine
            # ordinary recordings.
            continue
        for finding in sensitivity.rule_findings(_without(rendered.text, ours)):
            if not finding.held:
                continue
            problems.append(
                f"the {rendered.kind} file ({rendered.name}) states something that must not "
                f"be written down — {finding.subject} — even though it is not a quotation of "
                f"a held passage, so it was not caught by masking"
            )
            leaking.append(rendered.name)
    if not problems:
        return
    raise HeldTextWouldLeak(
        f"{source_name or 'this recording'} produced a summary or a proposal restating "
        f"something that must not be written down, in words of its own rather than the "
        f"recording's, so none of the three files has been written: " + "; ".join(problems)
        + ". Nothing was uploaded, nothing was moved and nothing was deleted.",
        refs=(),
        files=tuple(dict.fromkeys(leaking)),
    )


# --------------------------------------------------------------------------- body pieces


def _provenance(ctx: OutputContext) -> list[str]:
    """The metadata block — in the body, where the parser can actually see it.

    Every line is prefixed with ``- ``. That is not decoration: it guarantees no line here
    can ever be read as a header if this text is re-parsed on its own, which the record's
    base64 recovery path does.
    """
    parsed = ctx.parsed
    rows: list[tuple[str, str]] = [
        ("Recording", ctx.source_name),
        ("Recorded", f"{_human_dt(ctx.recorded_at)} — {ctx.timestamp_source}"),
    ]
    if parsed.party:
        rows.append(("Other party, from the filename", parsed.party))
    else:
        rows.append(
            ("Other party", "not stated in the filename — no party is claimed for this one")
        )
    if not parsed.matched_call_form:
        rows.append(
            (
                "Filename form",
                "the voice recorder's own default name — this is a note he did not get to "
                "naming before it uploaded"
                if parsed.timestamp_recovered
                else "hand-typed, not one of the phone's call-recording names — this is how "
                "a site meeting arrives",
            )
        )
    if ctx.display_name:
        # Said out loud, in the body, because the title above is no longer the filename and
        # a reader comparing the two would otherwise think one of them is wrong. Never a
        # header row: the record's parser silently DELETES a seventh header key, and this
        # file's own contract check refuses to render one.
        rows.append(
            (
                "Name",
                f"chosen by this service from the site named in the recording. The file in "
                f"OneDrive is still called {parsed.original_name!r}",
            )
        )
    duration = ctx.duration_s
    if duration:
        rows.append(("Length", _human_duration(duration)))
    words = ctx.transcript.word_count
    if words:
        rows.append(("Words transcribed", f"{words:,}"))
    rows.append(("Transcribed by", ctx.engine_name))
    language = ctx.transcript.language or ""
    if language:
        rows.append(("Language reported", language))
    if ctx.audio is not None:
        rows.append(("Audio checked", _audio_sentence(ctx.audio)))
    if ctx.content_hash:
        rows.append(("Audio sha256", ctx.content_hash))
    if ctx.graph_hash:
        rows.append(("Hash Graph reported", ctx.graph_hash))
    rows.append(("OneDrive item", ctx.item_id or "not recorded"))
    safe_url = _safe_web_url(ctx.web_url)
    if safe_url:
        rows.append(("Recording in OneDrive", safe_url))
    if ctx.held:
        # Stated, not implied. The markers say it where the words were, but a reader who
        # opens the summary or the proposals sees no marker at all unless one happened to be
        # quoted — and "this file is complete" and "this file is complete except for two
        # passages" are different claims. Counts and references only: the references are
        # already printed in the transcript beside each marker, and are how a person asks
        # for the words back. Nothing here is a word of what was held.
        count = len(ctx.held)
        rows.append(
            (
                "Passages held for review",
                f"{count} ({', '.join(str(getattr(s, 'ref', '?')) for s in ctx.held)}) — "
                f"marked in place in the transcript, nothing was deleted, and nothing is "
                f"released until a person says so",
            )
        )
    rows.append(("observed_by", "agent"))

    lines = [f"- {key}: {_inline(value)}" for key, value in rows if str(value).strip()]
    for note in _all_notes(ctx):
        lines.append(f"- Note for a person: {_inline(note)}")
    lines.append(_REDACTION_SLOT)
    return lines


def _all_notes(ctx: OutputContext) -> list[str]:
    """Everything the run wants a person to know, from the context and the analysis both.

    ``extraction.notes`` is where the analysis pass records what it could not stand behind —
    "the site was read as X but the words offered as evidence are not in the transcript", "an
    email address was removed from the summary". Rendering only ``ctx.notes`` dropped every
    one of them on the floor, which made an unsupported reading indistinguishable from a
    supported one in the file a person actually reads.
    """
    notes: list[str] = [str(n).strip() for n in ctx.notes if str(n).strip()]
    for note in getattr(ctx.extraction, "notes", ()) or ():
        text = str(note).strip()
        if text and text not in notes:
            notes.append(text)
    return notes


def _safe_web_url(url: str) -> str:
    """Graph's ``webUrl`` with the owner's address taken out of the path.

    On OneDrive for Business the path carries ``/personal/<upn-with-underscores>/``, which is
    the owner's email address in a spelling no address check recognises. The link is still
    worth having — it is how a person opens the recording — so the identifying segment is
    replaced rather than the whole row dropped, and the item id above it is the durable
    handle either way.
    """
    return strip_owner_paths(str(url or "").strip())


def _segment_line(segment: Segment, text: str | None = None) -> str:
    """One published line. ``text`` overrides the segment's own words when they were masked."""
    text = _inline(segment.text if text is None else text)
    speaker = _inline(segment.speaker or "")
    stamp = _clock(segment.start)
    if speaker:
        return f"[{stamp}] {speaker}: {text}"
    return f"[{stamp}] {text}"


def _participants_block(extraction: Any) -> list[str]:
    people = list(getattr(extraction, "participants", ()) or ())
    if not people:
        return []
    out = [
        "",
        "## People heard, or spoken about",
        "",
        "Named because the recording names them. Nothing is claimed about what any of them",
        "agreed to, and no contact details are recorded here or anywhere else by this",
        "service.",
        "",
    ]
    for person in people:
        name = _inline(getattr(person, "name_or_role", "") or "")
        quote = _inline(getattr(person, "quote", "") or "")
        if not name:
            continue
        out.append(f"- {name}" + (f' — heard in: "{quote}"' if quote else ""))
    return out


def _site_evidence_lines(ctx: "OutputContext") -> list[str]:
    """Which jobs this recording names, with the workings — for the record, not instead of it.

    A site walk covers the job somebody is standing on and the two bothering them. One real
    recording ran through HQ, Lonehill and Pick n Pay, and everything downstream had to
    work that out again from raw text every time.

    The scoring already happened: :meth:`transcriber.sitebook.SiteBook.bind` runs the
    record's own rules, vendored verbatim, over the exact bytes the record is handed, and
    scores every job. Only the winner was used, to propose a title; the rest was discarded
    and the hunt started over downstream.

    **Candidates and scores, never a filing.** The record binds by scoring the document, and
    a name asserted here that is wrong can move a filing that was previously right. So this
    hands over the workings and the record still decides — which is also why it is only ever
    written into the two files the record's intake skips.
    """
    evidence = getattr(ctx, "site_evidence", None)
    if evidence is None:
        return []
    candidates = tuple(getattr(evidence, "candidates", ()) or ())
    fault = str(getattr(evidence, "fault", "") or "")
    if not candidates and not fault:
        return []

    out = ["", "## Which jobs this recording names", ""]
    if fault:
        out += [f"- The job list could not be read ({_inline(fault)}), so nothing here was "
                "matched against it. That is not the same as a recording naming no job."]
        return out
    out += [
        "- Matched against the job list with the record's own rules, over the same words "
        "the record is given.",
        "- **This is evidence, not a filing.** Nothing has been filed against any of these.",
        "",
        "| Job | Score |",
        "| --- | --- |",
    ]
    for candidate in candidates[:8]:
        out.append(f"| {_inline(candidate.title)} | {int(candidate.score)} |")
    if len(candidates) > 8:
        out.append(f"| _and {len(candidates) - 8} more, scoring lower_ | |")
    if len(candidates) > 1:
        out += ["", "More than one job is named here. That is normal on a site walk and it "
                "is the reason this list is a list."]
    return out


def _site_block(extraction: Any) -> list[str]:
    site = _text_of(extraction, "site")
    if not site:
        return []
    quote = _text_of(extraction, "site_quote")
    out = [
        "",
        "## The site this appears to concern",
        "",
        f"- Appears to concern: {_inline(site)}",
        "- `observed_by: agent` — this is what the words suggest, not a filing decision.",
    ]
    if quote:
        out.append(f'- On the strength of: "{_inline(quote)}"')
    else:
        # An attribution with nothing behind it must not read like one with evidence. The
        # record scores site vocabulary out of this body and binds the recording to a real
        # site on the strength of it, and its own rule is that filing to the wrong site is
        # worse than filing to none.
        out.append(
            "- **No words in the transcript were found supporting this name.** It is the "
            "model's reading only, and nothing should be filed against that site on it."
        )
    return out


def _unclear_block(extraction: Any) -> list[str]:
    passages = list(getattr(extraction, "unclear", ()) or ())
    if not passages:
        return []
    out = [
        "",
        "## Passages that were not clear",
        "",
        "Kept as they were heard rather than smoothed into something readable. A tidied",
        "guess is indistinguishable from a fact once it is written down.",
        "",
    ]
    for passage in passages:
        words = _inline(getattr(passage, "passage", "") or "")
        why = _inline(getattr(passage, "why", "") or "")
        if not words:
            continue
        out.append(f'- "{words}"' + (f" — {why}" if why else ""))
    return out


def _routing_block(extraction: Any) -> list[str]:
    sentence = _routing_sentence(extraction)
    if not sentence:
        return []
    models = list(getattr(extraction, "models_used", ()) or ())
    out = ["", "## How this recording was read", "", f"- {sentence}"]
    if models:
        out.append(f"- Models used: {_inline(', '.join(str(m) for m in models))}")
    return out


def _review_block(extraction: Any, ctx: OutputContext) -> list[str]:
    """Say that items were dropped, and never say what they were.

    An item whose quote could not be found in the transcript must not reach an output — but
    the fact that a model produced one is itself something a person needs to see, so the
    count is stated here and the items themselves stay on the review list.
    """
    review = list(getattr(extraction, "review", ()) or ())
    if not review:
        return []
    count = len(review)
    subject = "one item" if count == 1 else f"{count} items"
    verb = "was" if count == 1 else "were"
    return [
        "",
        "## What was left out of this file",
        "",
        f"- The analysis produced {subject} that could not be matched to any words in the "
        f"transcript, so {'it was' if count == 1 else 'they were'} not written here. Quoting "
        f"something that was never said is the failure this check exists to catch.",
        f"- {subject.capitalize()} {verb} kept against this recording in the ledger, with the "
        f"words the model offered and how close they came. Run `transcriber status` to see "
        f"the count, and `transcriber status --json` for the items themselves.",
        f"- The transcript is unaffected and complete: `{ctx.names.transcript}`.",
    ]


def _proposal_block(number: int, proposal: Any, evidence: Any = None) -> list[str]:
    item = getattr(proposal, "item", proposal)
    check = getattr(proposal, "quote_check", None)
    statement = _inline(getattr(item, "text", "") or "")
    quote = _inline(getattr(item, "quote", "") or "")

    lines = [f"### {number}. {statement or 'An item with no wording — see the quote'}", ""]
    lines.append(f"- Kind: {_inline(getattr(item, 'kind', '') or 'not stated')}")
    lines.append("- Status: proposed, not decided. A person confirms this or it does not count.")
    speaker = _inline(getattr(item, "speaker", "") or "")
    if speaker:
        lines.append(f"- Heard from: {speaker}")
    site = _inline(getattr(item, "site", "") or "")
    if site:
        lines.append(f"- Site it appears to concern: {site}")
    # The line above is the model's free-text answer. This one is the job list's, reached by
    # the record's own rules over the words of THIS line — so a walk covering three jobs says
    # which line belongs to which, instead of the whole recording being flattened to one.
    # Silent when the line names none, which is most of them: "this line does not say" is a
    # real answer and a better one than a guess.
    named: tuple[str, ...] = ()
    if evidence is not None:
        try:
            named = tuple(evidence.slugs_for(getattr(item, "quote", "") or ""))
        except Exception:  # noqa: BLE001 - a label is never worth losing a file over
            named = ()
    if named:
        titles = ", ".join(_inline(evidence.title_of(slug)) for slug in named)
        if len(named) == 1:
            lines.append(f"- The job list matches these words to: {titles}")
        else:
            lines.append(f"- These words name more than one job — {titles} — so which one "
                         "this belongs to is not settled here.")
    due = _inline(getattr(item, "due", "") or "")
    if due:
        lines.append(f"- Date heard: {due} — as spoken, not a date this service has set")
    confidence = getattr(item, "confidence", None)
    if isinstance(confidence, (int, float)):
        lines.append(f"- How sure the model was: {float(confidence):.2f}")
    if check is not None:
        method = _inline(str(getattr(check, "method", "") or ""))
        ratio = getattr(check, "ratio", None)
        detail = f"- Quote checked against the transcript: {method or 'checked'}"
        if isinstance(ratio, (int, float)):
            detail += f" (match {float(ratio):.2f})"
        lines.append(detail)
    if getattr(item, "redacted", False):
        lines.append("- An address was removed from this item's wording before it was written.")
    lines.append("- observed_by: agent")
    lines += ["", "Said, verbatim:", "", f"> {quote}", ""]
    return lines


# --------------------------------------------------------------------------- the contract


def parse_like_downstream(text: str) -> tuple[dict[str, str], str]:
    """A faithful copy of ``parse_texty``'s header scan, including what it swallows.

    Copied deliberately, with its behaviour intact rather than corrected: the point is to
    find out what the record will actually do with our file, not what it ought to do.
    """
    head: dict[str, str] = {}
    body = text
    seen = False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        match = _HEADER_RE.match(line.strip())
        if match:
            head[match.group(1).lower()] = match.group(2).strip()
            seen = True
            continue
        if seen and not line.strip():
            body = "\n".join(lines[i + 1:])
            break
        if not seen and i > 6:
            break
    return head, body


def _address_scan_variants(text: str) -> tuple[str, ...]:
    """The copies of a file the backstop reads: flattened, and with the line prefixes off."""
    flat = _normalise_for_address_scan(text)
    unwrapped = _normalise_for_address_scan(_SEGMENT_PREFIX_RE.sub("", text))
    return (flat,) if unwrapped == flat else (flat, unwrapped)


def check_name(name: str) -> list[str]:
    """The same mechanical guard as :func:`check_contract`, applied to the *filename*.

    The name is the one surface that cannot be edited afterwards: it goes into OneDrive, into
    the ledger, into the URL the downstream flow PUTs to, and into a git commit in the record.
    A guard on file content alone left it uncovered.
    """
    problems: list[str] = []
    base = str(name or "")
    if not base.strip():
        problems.append("an output file has no name")
        return problems
    # The symbol in all three of its spellings, not just the ASCII one. A phone with a CJK
    # keyboard writes U+FF20 and sometimes U+FE6B, and ``"@" in base`` is blind to both: a
    # recording named "Call carel＠example.co.za" put that address into three filenames, into
    # the ledger, into OneDrive and into a commit in the record, and every one of those is a
    # place it can no longer be taken out of.
    if contains_email(base) or any(ch in base for ch in "@\uFF20\uFE6B"):
        problems.append(f"the filename {base!r} contains an email address")
    if contains_dictated_email(base):
        problems.append(f"the filename {base!r} contains a spoken-out-loud email address")
    # And the same normalised read the file contents get. This one refuses rather than
    # redacts on purpose: :func:`transcriber.naming.safe_stem` has already had its go, and
    # its own docstring says why the name is the surface that gets the loud answer — it is
    # the one that cannot be corrected afterwards. Refusing costs a recording a retry after
    # somebody renames it. Publishing costs an address that stays published.
    elif _NORMALISED_ADDRESS_RE.search(_normalise_for_address_scan(base)):
        problems.append(
            f"the filename {base!r} contains an email address written in a way the "
            f"redaction did not remove; a filename cannot be corrected once it is in "
            f"OneDrive and in the record"
        )
    if _UPN_PATH_RE.search(base):
        problems.append(f"the filename {base!r} carries an account owner's path segment")
    if not base.lower().endswith(".md"):
        problems.append(f"the filename {base!r} is not a .md file")
    return problems


def check_contract(
    text: str,
    *,
    expected_body: str | None = None,
    expected_subject: str | None = None,
    expected_date: str | None = None,
) -> list[str]:
    """Every way this file could be mis-read downstream, in plain words. Empty is good.

    Exported so ``worker.py selftest`` can prove the markdown contract offline, with no
    credential and no network, the way ``graph_pull.py --selftest`` does downstream.
    """
    problems: list[str] = []
    head, body = parse_like_downstream(text)

    if "from" in head:
        problems.append(
            "there is a From: header, which reclassifies this file as an email rather than "
            "a transcript"
        )
    for key in sorted(set(head) - set(_ALLOWED_HEADERS)):
        problems.append(
            f"the header block carries {key!r}, which is not one of the two keys we emit "
            f"(subject, date)"
        )
    if "subject" not in head:
        problems.append("there is no Subject: header")
    elif not head["subject"].strip():
        problems.append("the Subject: header is empty, so the record falls back to the filename")
    if "date" not in head:
        problems.append("there is no Date: header")

    if expected_subject is not None and head.get("subject") != expected_subject:
        problems.append(
            f"the Subject: read back as {head.get('subject')!r}, not {expected_subject!r}"
        )

    if expected_date is not None:
        recovered = _first_iso_date(head.get("date", ""))
        if recovered != expected_date:
            problems.append(
                f"the record would read the date as {recovered or 'nothing'}, not "
                f"{expected_date} — a Date: value needs its ISO date followed by a space, "
                f"or the year-month fallback files it on the first of the month"
            )

    if expected_body is not None and body != expected_body:
        lost = [
            line
            for line in expected_body.split("\n")
            if line.strip() and line not in body.split("\n")
        ]
        problems.append(
            "the body the record recovers is not the body we wrote"
            + (
                f" — {len(lost)} line(s) were swallowed by the header block, starting with "
                f"{lost[0][:60]!r}"
                if lost
                else " — check the blank line separating the header from the body"
            )
        )

    if contains_email(text):
        found = EMAIL_RE.findall(text)
        problems.append(
            f"the file contains {len(found)} email address(es); this service never writes one"
        )

    if _DECIDED_BY_RE.search(text):
        problems.append(
            "the file emits a 'decided_by:' field; this pipeline is an agent and cannot "
            "decide anything, so nothing it writes may carry that field"
        )

    if _UPN_PATH_RE.search(text):
        problems.append(
            "the file carries a OneDrive '/personal/<owner>/' path, which is the account "
            "owner's email address written with underscores; this service never writes one "
            "in any spelling"
        )
    if contains_dictated_email(text):
        found = DICTATED_EMAIL_RE.findall(text)
        problems.append(
            f"the file contains {len(found)} spoken-out-loud email address(es) "
            "('name at host dot co dot za'); this service never writes one in any spelling"
        )

    # The backstop's own reading, and the only address check here that does not ask the
    # masker's own detectors the question the masker has already asked itself. Both of the
    # copies it reads are ones the masker cannot: the file with its line breaks flattened,
    # so an address split across a line is whole again, and the same file with the segment
    # prefixes taken off, so an address split across a segment cut is whole again too. The
    # spellings are normalised first — spacing, brackets, the lookalike characters, %40 and
    # &#64;, and "dot" said out loud — so this pattern does not have to know any of them.
    for scanned in _address_scan_variants(text):
        found = _NORMALISED_ADDRESS_RE.findall(scanned)
        if found:
            problems.append(
                f"the file contains {len(found)} email address(es) written in a way the "
                f"redaction did not remove — reading the file with its spacing, brackets "
                f"and lookalike characters normalised recovers them; this service never "
                f"writes one in any spelling"
            )
            break
        if contains_dictated_email(scanned):
            problems.append(
                "the file contains a spoken-out-loud email address that is split across a "
                "line or a segment break, so it reads as an address only once the file is "
                "joined up; this service never writes one in any spelling"
            )
            break

    if not text.startswith("Subject:"):
        problems.append("the file does not begin with the Subject: header")

    # The shape itself, not just what survives it. A body comparison alone cannot see a
    # third header line: the record swallows it and still recovers the body intact, so the
    # loss is invisible downstream and invisible to that check. This is the one that bites
    # when somebody reasonably decides the recording's name belongs "in the header".
    lines = text.split("\n")
    if len(lines) < 4 or lines[2].strip():
        problems.append(
            "the header block is not exactly Subject:, Date:, one blank line — anything "
            "else above that blank line reaches neither the header nor the body, and "
            "disappears without erroring"
        )
    if not text.endswith("\n"):
        problems.append("the file does not end with a newline")

    return problems


def _finalise(subject: str, ctx: OutputContext, body_lines: Iterable[str]) -> str:
    """Assemble, scrub, and refuse to return anything the record would mis-read."""
    body = _scrub("\n".join(body_lines).rstrip() + "\n")
    header = f"Subject: {subject}\nDate: {_header_date(ctx.recorded_at)}\n"
    text = header + "\n" + body

    problems = check_contract(
        text,
        expected_body=body,
        expected_subject=subject,
        expected_date=ctx.recorded_at.strftime("%Y-%m-%d"),
    )
    if problems:
        raise OutputContractError(
            f"{ctx.source_name!r} rendered a file the record would mis-read: "
            + "; ".join(problems)
        )
    return text


def _scrub(body: str) -> str:
    """Remove any address, and say in the file that one was removed.

    Visible, not silent. A reader who can see that something was taken out can ask what it
    was; a reader looking at a quietly altered quote cannot.
    """
    found = EMAIL_RE.findall(body)
    notes: list[str] = []
    if found:
        count = len(found)
        notes.append(
            f"- Note: {count} email address{'es' if count > 1 else ''} "
            f"{'were' if count > 1 else 'was'} removed from this text. This service never "
            f"writes one down; everything else here is unaltered."
        )
        body = strip_emails(body)
    if contains_dictated_email(body):
        # An address said out loud — "carel at example dot co dot za" — is an address. It is
        # reconstructable, it ends up in a git commit in the record, and the rule against
        # writing one down carries no exception for the spelling. Treated exactly as the
        # "@" form is treated a few lines above, including in the transcript: the same trade
        # the "@" form has always made here, and made visible the same way rather than
        # quietly. What is lost is one address; what is kept is a rule with no holes in it.
        # The wording deliberately carries no example of the pattern: an illustration of the
        # spoken form inside the note would itself match, and the file would refuse itself.
        notes.append(
            "- Note: an email address spoken aloud rather than spelled out was removed from "
            "this text. This service never writes one down, in any spelling; everything "
            "else here is unaltered."
        )
        body = strip_dictated_emails(body)
    if notes:
        body = body.replace(_REDACTION_SLOT, "\n".join(notes))
    body = "\n".join(line for line in body.split("\n") if line != _REDACTION_SLOT)
    return body.rstrip() + "\n"


def _first_iso_date(value: str) -> str:
    """What the record's ``parse_date`` would take from our Date: line, first branch only."""
    match = _ISO_DATE_RE.search(value or "")
    return match.group(0) if match else ""


def _header_date(when: datetime) -> str:
    """``2026-08-27 14:30:05 +02:00``.

    The space after the day is load-bearing: it is the word boundary that lets the record's
    full-date pattern match. An ISO ``T`` there would fall through to the year-month
    pattern and file the recording on the first of the month.
    """
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    offset = when.strftime("%z")
    if offset:
        return f"{stamp} {offset[:3]}:{offset[3:]}"
    return stamp


# --------------------------------------------------------------------------- upload


def upload_outputs(
    client: Any,
    parent_id: str,
    files: Sequence[RenderedFile],
    *,
    held: Sequence[Any] = (),
    verify_bytes: bool = False,
    orphan_folder_id: str = "",
    work_dir: str = "",
) -> UploadResult:
    """Upload all three and confirm all three, or raise having tried to leave none.

    ``client`` is a :class:`transcriber.graph.GraphClient`: ``upload(parent_id, name, data)``
    then ``get_item(item_id)``. The read-back is the point — an upload that returned 200 and
    an item that is actually there are not the same claim, and this service exists because
    the difference went unnoticed for months.

    ``verify_bytes`` additionally downloads each file and compares its sha256. It is off by
    default because the size-and-name read-back already proves presence, and on is the right
    setting for a run that has to be beyond argument.
    """
    if not files:
        raise UploadIncompleteError("nothing was rendered, so nothing can be uploaded")
    if len(files) != 3:
        raise UploadIncompleteError(
            f"expected three files and was given {len(files)}: "
            + ", ".join(f.kind for f in files)
        )

    # The last thing before the wire, and the second time it is asked. ``render_all`` already
    # refused these bytes once; this is the check on the way *out* rather than on the way in,
    # in the same place and for the same reason ``_refuse_unverified`` is asked twice. It
    # costs one pass over three small strings and it is the only thing standing between a
    # masking bug and a held passage in the record, from which nothing can take it back.
    refuse_held_text(files, held)
    refuse_written_down_again(files, armed=bool(held))

    uploaded: list[UploadedFile] = []
    remaining = [f.name for f in files]
    try:
        for rendered in files:
            item = client.upload(parent_id, rendered.name, rendered.data)
            item_id = str(getattr(item, "id", "") or "")
            if not item_id:
                raise UploadIncompleteError(
                    f"Graph accepted {rendered.name!r} but returned no item id, so there is "
                    f"nothing to read back and nothing to prove it exists"
                )
            uploaded.append(
                UploadedFile(
                    kind=rendered.kind,
                    name=rendered.name,
                    item_id=item_id,
                    size=rendered.size,
                    web_url=str(getattr(item, "web_url", "") or ""),
                )
            )
            remaining.remove(rendered.name)

        confirmed = [
            _read_back(client, rendered, up, verify_bytes=verify_bytes, work_dir=work_dir)
            for rendered, up in zip(files, uploaded)
        ]
    except Exception as exc:
        orphans = _rollback(client, uploaded, orphan_folder_id)
        raise UploadIncompleteError(
            f"the three output files did not all land, so this recording is not done: {exc}",
            uploaded=[u.name for u in uploaded],
            missing=remaining,
            orphans=orphans,
        ) from exc

    result = UploadResult(parent_id=parent_id, files=tuple(confirmed))
    if not result.complete:
        raise UploadIncompleteError(
            "the upload finished without all three kinds present: "
            + ", ".join(sorted(result.names)),
            uploaded=[f.name for f in confirmed],
        )
    log.info(
        "uploaded and confirmed %d files into %s: %s",
        len(confirmed),
        parent_id,
        ", ".join(f.name for f in confirmed),
    )
    return result


def publish(
    client: Any,
    parent_id: str,
    ctx: OutputContext,
    *,
    verify_bytes: bool = False,
    orphan_folder_id: str = "",
    work_dir: str = "",
) -> UploadResult:
    """Render all three, then upload all three. The one call the worker needs.

    Rendering happens first and completely: a contract failure in the actions file stops the
    transcript from ever being uploaded, which is the cheapest possible place for all-or-none
    to be decided. A held passage surviving into any one of the three is the same kind of
    failure and takes the same path — it stops all three, before the first byte.
    """
    return upload_outputs(
        client,
        parent_id,
        render_all(ctx),
        held=ctx.held,
        verify_bytes=verify_bytes,
        orphan_folder_id=orphan_folder_id,
        work_dir=work_dir,
    )


def _read_back(
    client: Any,
    rendered: RenderedFile,
    uploaded: UploadedFile,
    *,
    verify_bytes: bool,
    work_dir: str,
) -> UploadedFile:
    """Fetch the item we just wrote and check it is the file we meant to write."""
    item = client.get_item(uploaded.item_id)
    name = str(getattr(item, "name", "") or "")
    size = int(getattr(item, "size", 0) or 0)
    if name != rendered.name:
        raise UploadIncompleteError(
            f"read back {uploaded.item_id} expecting {rendered.name!r} and got {name!r}"
        )
    if size != rendered.size:
        raise UploadIncompleteError(
            f"{rendered.name!r} is {size} bytes in OneDrive and {rendered.size} bytes here; "
            f"the upload did not land whole"
        )

    verified = False
    if verify_bytes:
        _verify_bytes(client, rendered, uploaded, work_dir)
        verified = True

    return UploadedFile(
        kind=uploaded.kind,
        name=rendered.name,
        item_id=uploaded.item_id,
        size=size,
        web_url=str(getattr(item, "web_url", "") or uploaded.web_url),
        verified_bytes=verified,
    )


def _verify_bytes(
    client: Any, rendered: RenderedFile, uploaded: UploadedFile, work_dir: str
) -> None:
    directory = work_dir or tempfile.gettempdir()
    os.makedirs(directory, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory, prefix="readback-") as scratch:
        dest = os.path.join(scratch, rendered.name)
        result = client.download(uploaded.item_id, dest)
        digest = str(getattr(result, "sha256", "") or "")
        if not digest:
            digest = hashlib.sha256(open(dest, "rb").read()).hexdigest()
        if digest != rendered.sha256:
            raise UploadIncompleteError(
                f"{rendered.name!r} read back with a different sha256 than it was written "
                f"with; what is in OneDrive is not what this run produced"
            )


def _rollback(client: Any, uploaded: Sequence[UploadedFile], orphan_folder_id: str) -> list[str]:
    """Best effort at leaving nothing behind. What survives is named in the error.

    The Graph client has no delete — deleting a person's files is not a power this service
    asks for — so the honest fallback is to move the strays somewhere visible if a folder was
    configured, and otherwise to report them. Either way the ledger does not advance and the
    next run replaces all three by name, so a stray is stale for one cycle, never forever.
    """
    orphans: list[str] = []
    for item in uploaded:
        remover = getattr(client, "delete_item", None) or getattr(client, "delete", None)
        try:
            if callable(remover):
                remover(item.item_id)
                continue
            if orphan_folder_id and callable(getattr(client, "move", None)):
                client.move(item.item_id, orphan_folder_id)
                orphans.append(f"{item.name} (moved aside)")
                continue
            orphans.append(item.name)
        except Exception as exc:  # a failed cleanup must not hide the failure it followed
            log.warning("could not clear %s after a failed upload: %s", item.name, exc)
            orphans.append(item.name)
    if orphans:
        log.warning(
            "a partial output set is still in OneDrive and will be replaced on the next "
            "attempt: %s",
            ", ".join(orphans),
        )
    return orphans


# --------------------------------------------------------------------------- small helpers


def _subject(ctx: OutputContext, suffix: str) -> str:
    """The one header the record reads for meaning. Kept inside its 90-character cut.

    Through :func:`strip_emails` because the label comes from the source filename, and a
    recording genuinely named ``Call carel@example.co.za_260827_120055.m4a`` would
    otherwise put an address in the header — where the body's scrub cannot reach it, so the
    contract check refuses the file and the whole recording is quarantined for a fault in
    somebody's filename. Redacting the subject keeps the house rule and keeps the
    recording; the removal is still stated in the body, which carries the same name.
    """
    # The suffix is reserved before the label is cut, not appended after. A long hand-typed
    # site-meeting name otherwise pushed " — voice note summary" off the end of all three
    # subjects, and the record writes that subject as the Substance column of the site's
    # correspondence log: three identical rows for one site walk, with no way to tell the
    # evidence from the machine's reading of it.
    tail = f" — {suffix}"
    label = _one_line(
        strip_dictated_emails(strip_emails(str(ctx.label))),
        max(20, _MAX_SUBJECT - len(tail)),
    )
    return _one_line(f"{label}{tail}", _MAX_SUBJECT)


def _title(ctx: OutputContext, suffix: str) -> str:
    return _one_line(f"{ctx.label} — {suffix}", 160)


def _one_line(value: str, limit: int) -> str:
    """One line, no pipes, no bold, cut back to a word boundary.

    A pipe would break the markdown table row the record writes this into; a cut mid-token
    would turn a name into a different name.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("|", "/").replace("**", "")
    text = "".join(ch for ch in text if ch >= " " or ch == "\t").strip()
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
        text = cut.rstrip(" -—,.")
    return text


def _inline(value: Any) -> str:
    """Anything interpolated into a line: whitespace collapsed, never truncated."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _human_dt(when: datetime) -> str:
    return _header_date(when)


def _human_duration(seconds: float) -> str:
    total = int(round(float(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes} min {secs} s"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _clock(seconds: float | None) -> str:
    total = max(0, int(float(seconds or 0.0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _audio_sentence(info: AudioInfo) -> str:
    state = "truncated — this recording is incomplete" if info.truncated else "complete"
    probe = f", checked by {info.probed_by}" if info.probed_by else ""
    reason = f" — {info.reason}" if info.reason else ""
    return f"{info.container or 'container not identified'}, {state}{probe}{reason}"


def _word_count(segments: Sequence[Segment]) -> int:
    return sum(len((s.text or "").split()) for s in segments)


def _text_of(extraction: Any, attribute: str) -> str:
    return str(getattr(extraction, attribute, "") or "").strip() if extraction is not None else ""


def _routing_sentence(extraction: Any) -> str:
    routing = getattr(extraction, "routing", None)
    if routing is None:
        return ""
    why = getattr(routing, "why", None)
    text = _inline(why() if callable(why) else why or "")
    return f"How it was read: {text}" if text else ""


def _proposals(extraction: Any) -> list[Any]:
    return list(getattr(extraction, "proposals", ()) or ()) if extraction is not None else []


def _grouped(extraction: Any, proposals: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    """Grouped the way the analysis groups them, with anything unrecognised kept visible."""
    by_category = getattr(extraction, "by_category", None)
    if callable(by_category):
        grouped = by_category()
        if grouped:
            listed = {id(p) for group in grouped.values() for p in group}
            leftovers = [p for p in proposals if id(p) not in listed]
            out = [(category, list(group)) for category, group in grouped.items()]
            if leftovers:
                out.append(("other", leftovers))
            return out
    ordered: dict[str, list[Any]] = {}
    for proposal in proposals:
        ordered.setdefault(str(getattr(proposal, "category", "") or "other"), []).append(proposal)
    return list(ordered.items())


def _refuse_unverified(proposals: Sequence[Any], ctx: OutputContext) -> None:
    """An unverified item never reaches an output — checked here as well as in extract.py.

    Two checks rather than one because this is the guard that stops a misheard word from
    hardening into a task, and a guard on the way in is not the same as a guard on the way
    out. The ``exact`` claim is re-tested against the transcript for the same reason.

    **The sensitivity gate could have turned this guard into a shredder, and does not.**
    ``extract.py`` verifies each quote against the transcript as transcribed and discards
    any item whose quote it cannot find; ``ctx.transcript`` here is the transcript *after*
    held passages were cut out of it. Redact the transcript and leave the items alone and
    every item quoting a held passage fails this check — which raises rather than dropping
    it, so the whole recording quarantines and no action item is lost, but that is a
    quarantine a day for a gate working correctly, which is a gate somebody switches off.

    So it is solved upstream instead, and the solution is an ordering rather than a
    weakening of anything here: verification runs first, on the unredacted text, because
    only that text can answer "did the model invent this?" — and the gate then rewrites each
    surviving quote through :meth:`transcriber.redact.Redaction.apply_to_quote`, replacing
    exactly the held part with exactly the marker that replaced it in the transcript. The
    rewritten quote is still a literal substring of the published transcript, so the check
    below passes because it is true, not because it was loosened. An item quoting *only*
    held words is withheld as an item — kept, counted, listed as held in every file's notes,
    never silently dropped and never written out.

    Nothing in this function may be relaxed to accommodate the gate. If a quote ever fails
    here on a redacted recording, the masking is wrong and the recording must not publish.
    """
    # Both sides redacted the same way. ``ExtractedItem`` rewrites a quote containing an
    # address into "[address removed]", and comparing that against the raw transcript failed
    # every time somebody read an address out on the recording — quarantining the whole thing
    # with a reason saying the words were never said, which is false and sends whoever reads
    # it hunting a hallucination that did not happen.
    raw = ctx.transcript.text or ""
    haystack = _loose(raw)
    redacted_haystack = _loose(strip_dictated_emails(strip_emails(raw)))
    for index, proposal in enumerate(proposals, start=1):
        item = getattr(proposal, "item", proposal)
        quote = str(getattr(item, "quote", "") or "").strip()
        where = _identify(proposal, item, index)
        if not quote:
            raise OutputContractError(
                f"{ctx.source_name!r}: {where} has no verbatim quote, which makes it an "
                f"assertion rather than an observation"
            )
        if getattr(item, "observed_by", "agent") != "agent":
            raise OutputContractError(
                f"{ctx.source_name!r}: an item claims observed_by="
                f"{getattr(item, 'observed_by', None)!r}; this pipeline can only observe"
            )
        if not getattr(item, "quote_verified", False):
            raise OutputContractError(
                f"{ctx.source_name!r}: {where} did not pass quote verification and must go "
                f"to the review list, never to an output"
            )
        check = getattr(proposal, "quote_check", None)
        if check is not None and not bool(getattr(check, "ok", True)):
            raise OutputContractError(
                f"{ctx.source_name!r}: {where} carries a failed quote check and must not be "
                f"written out"
            )
        method = str(getattr(check, "method", "") or "") if check is not None else ""
        needle = _loose(quote)
        found = (needle in haystack) or (needle in redacted_haystack)
        if method == "exact" and haystack and not found:
            raise OutputContractError(
                f"{ctx.source_name!r}: {where} claims an exact quote match, but those words "
                f"are not in the transcript"
                + (
                    ". This recording had passages held for review, so the likeliest cause "
                    "is that the item's quote was not rewritten with the same markers the "
                    "transcript was cut with — the words were said, and nothing about them "
                    "is invented. Nothing was written"
                    if ctx.held
                    else ""
                )
            )


def _identify(proposal: Any, item: Any, index: int) -> str:
    """Which item this is, without repeating a word of it.

    These messages are raised as :class:`OutputContractError`, which the pipeline never
    retries, so they go to the ledger's ``quarantine_reason``, out through the morning
    email's "Technical detail:" line, and into the log. On a redacted recording an item's
    quote still carries the held words *by definition* — the raise only fires when the
    quote failed to be rewritten — so interpolating sixty characters of it here would put a
    staff matter or an identity number in James's inbox, on the one path that exists to
    prevent exactly that. The operator's next step is to open the ledger row for the
    recording either way, and the category and position are what find it.

    No error message in this service should be able to carry transcript text, so this
    carries none, held recording or not. A rule with an exception is a rule somebody edits.
    """
    category = str(getattr(proposal, "category", "") or getattr(item, "kind", "") or "?")
    return f"item {index} in {category}"


def _loose(text: str) -> str:
    """Casefolded, punctuation-free, single-spaced — generous on purpose.

    It is used only to catch a quote that is genuinely absent, so it must not fail an
    honest match over a comma or a curly apostrophe.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").casefold()).strip()


def _humanise(category: str) -> str:
    return str(category or "other").replace("_", " ").strip().capitalize()
