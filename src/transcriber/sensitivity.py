"""The sensitivity gate: which passages of a transcript must not be written down yet.

This module answers one question about every passage of a recording — **who is harmed if
this is repeated?** — and nothing else. It does not mask anything, it does not store
anything and it does not email anybody. It produces a :class:`Report` of exact,
non-overlapping character spans, each one either **held** (cut out of the transcript until a
person approves it) or **labelled** (published, with the label riding along so a later
client-facing check can see what it is).

Where it sits, which is the whole design
----------------------------------------

::

    transcribe → extract (quotes verified against the ORIGINAL text)
               → assess (this module: spans into the ORIGINAL text)
               → mask the transcript text
               → render the three .md outputs

Three orderings in that diagram are load-bearing and none of them is a preference.

**The mask lands on the transcript text, before any of the three files is generated.** The
actions file is named so the record never ingests it; only the transcript reaches the
record. A redaction applied to the actions file is not a redaction.

**Quote verification runs before masking, against the original text.** ``extract.py``
mechanically checks that every extracted item's quote appears in the transcript and discards
any item whose quote it cannot find. Mask the transcript first and every item quoting a
masked passage fails that check and is silently destroyed — a redaction that deletes
somebody's action items. So the order above is not negotiable, and the *residue* of it is
handled here: :meth:`Report.covers` and :func:`locate_spans` let the module that renders the
outputs ask "does this verified quote sit inside a held span?" and mask or withhold the item
on that answer, rather than discovering it by failing verification.

**A held passage is marked as a stated unknown, not deleted.** The record's read path is
built from six sources and this service's inbox is not one of them; a marker that only exists
in the transcript is invisible to the assistant answering questions on site. So every
finding carries :attr:`Finding.subject` — a short public noun phrase, guaranteed to contain
none of the detail — from which the marker reads *"a rate was recorded 24 Aug and is held
pending James"*. A confident answer built on a quietly partial record is worse than the leak
it prevents.

Precision over recall, deliberately
-----------------------------------

The gate is tuned to hold **rarely and obviously rightly**, and this is a decision, not an
accident of thresholds. Nothing here drains itself: no automatic release, no deadline, no
daily cap that commits the overflow unasked, no rule that writes itself. Only a person
clears the queue. That makes a false positive far more expensive than it looks — it is a
withdrawal from the only budget that matters, his willingness to keep opening the page — and
a gate he stops opening does not fail safely. It silently swallows the record, which is the
failure this whole service was built to cure.

So, concretely:

* **Prices flow.** A rand figure is ordinary business here — 6.3% of the record's content
  lines carry one — and is never held on its own. Only KBC's own cost set against its own
  charge, in one breath, is held. There is deliberately no mechanical rule that reacts to a
  currency symbol.
* **A held finding needs the model to be sure** (:data:`HOLD_CONFIDENCE`). Below that the
  passage is published with a label and a note a person reads, rather than held. Under doubt
  this module neither withholds silently nor publishes silently: :attr:`Report.notes` is the
  surface, and it is populated on every ambiguous outcome.
* **A quote that cannot be located exactly is not held.** No fuzzy span matching: cutting
  text on an approximate span either leaves the sensitive words in or removes the wrong ones.
  The single relaxation is a whole-sentence fallback (:data:`SENTENCE_COVERAGE`), which can
  only ever cut *more* than was asked for, never a partial phrase.
* **Two mechanical rules can hold on their own**, and only two: an explicit instruction not
  to write something down, in any language, and a bare identifier that validates as one (an
  identity number that passes its own checksum, bank or card details). Both are near-certain
  by construction. Everything else needs the model.

It ships dark. ``GATE_MODE`` defaults to ``shadow``: every recording is classified, what
*would* have been held is recorded, and **nothing is withheld**. The estimates of how much
this touches differ by a factor of twenty-five, and arming it before that number is real is
how the queue becomes a wall he bounces off. :meth:`Report.would_hold` is populated in every
mode, which is what shadow mode is for.

**What actually gates on the mode is** :func:`transcriber.redact.redact_text`, which returns
the text unchanged unless ``GATE_MODE=on``. Nothing in this module removes a character from
anything: it classifies, and :mod:`transcriber.redact` cuts. :meth:`Report.spans_to_mask`
expresses the same rule for a reader — held findings when armed, nothing otherwise — but it
is a description of the decision, not the enforcement of it, and it should not be read as
the thing standing between a shadow run and a redacted transcript.

Determinism
-----------

Given the same transcript and the same model answer, the output is byte-identical: rules are
regexes over the original text, spans are located by exact match or by sentence boundary,
and merging sorts on ``(start, end, category)``. Nothing here iterates a set or a dict whose
order could vary.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import prompts
from .withheld import CATEGORIES as STORE_CATEGORIES, CATEGORY_PHRASE, GATE_MODES

__all__ = [
    "GATE_MODES",
    "STORE_CATEGORIES",
    "HELD",
    "LABELLED",
    "DISPOSITIONS",
    "HELD_CATEGORIES",
    "LABELLED_CATEGORIES",
    "PUBLIC_SUBJECTS",
    "HOLD_CONFIDENCE",
    "SENTENCE_COVERAGE",
    "IMPLAUSIBLE_HELD_FRACTION",
    "GateSettings",
    "Span",
    "Finding",
    "Report",
    "locate_spans",
    "rule_findings",
    "assess",
]

log = logging.getLogger("transcriber.sensitivity")


# --------------------------------------------------------------------------------- policy

# ``GATE_MODES`` — off | shadow | on — is imported from :mod:`transcriber.withheld` rather
# than spelled again here. ``off`` means the gate is not merely inactive but **not in the
# way**: nothing extra is asked of the model, and nothing about the analysis pass differs
# from the day before the gate existed.

HELD = "held"
LABELLED = "labelled"

#: The band each category falls in — his answer of 2026-08-28, in code. **This table, not
#: the model, decides what is withheld.** The model names a category from a fixed list; the
#: consequence of that name is settled here, so a model having a strong day cannot widen the
#: held band, and widening it is an amendment to this line rather than a change of wording
#: in a prompt.
DISPOSITIONS: dict[str, str] = {
    "do_not_write_down": HELD,
    "staff_matter": HELD,
    "personal_circumstances": HELD,
    "legal_exposure": HELD,
    "bare_identifier": HELD,
    "own_margin": HELD,
    "commercial_figure": LABELLED,
    "conduct_or_quality": LABELLED,
}

HELD_CATEGORIES: tuple[str, ...] = tuple(
    c for c in prompts.SENSITIVITY_CATEGORIES if DISPOSITIONS.get(c) == HELD
)
LABELLED_CATEGORIES: tuple[str, ...] = tuple(
    c for c in prompts.SENSITIVITY_CATEGORIES if DISPOSITIONS.get(c) == LABELLED
)

# Checked at import rather than asserted: a category the prompt offers the model but this
# table has no disposition for would be dropped on every recording, silently, and `python -O`
# strips an assert.
if set(DISPOSITIONS) != set(prompts.SENSITIVITY_CATEGORIES):
    raise RuntimeError(
        "the sensitivity prompt and the disposition table disagree about the categories: "
        f"prompt-only={sorted(set(prompts.SENSITIVITY_CATEGORIES) - set(DISPOSITIONS))} "
        f"table-only={sorted(set(DISPOSITIONS) - set(prompts.SENSITIVITY_CATEGORIES))}"
    )
# The held band is the store's own closed list, spelled its way. A category this module
# holds that ``withheld.HeldSpan`` will not accept is a passage cut out of the transcript
# and then refused by the queue — the words gone from the record and nowhere to approve
# them from — so the two lists are compared here rather than at the first hold.
if set(HELD_CATEGORIES) != set(STORE_CATEGORIES):
    raise RuntimeError(
        "the categories this gate holds and the categories the store accepts disagree: "
        f"gate-only={sorted(set(HELD_CATEGORIES) - set(STORE_CATEGORIES))} "
        f"store-only={sorted(set(STORE_CATEGORIES) - set(HELD_CATEGORIES))}"
    )

#: What is published in place of the words when the model's own phrase cannot be trusted to
#: be free of detail. Deliberately dull: it appears on a page a client may read, and it has
#: to say what kind of thing is missing without saying anything about it.
#:
#: The six held phrases are :data:`transcriber.withheld.CATEGORY_PHRASE` itself, not a copy
#: of it. The marker written into the transcript and the line on the review page are built
#: from that table, and a passage described one way in the record and another way on the
#: page he approves it from is two descriptions of one thing.
PUBLIC_SUBJECTS: dict[str, str] = dict(CATEGORY_PHRASE) | {
    "commercial_figure": "a commercial figure",
    "conduct_or_quality": "a comment on somebody's work",
}

if not set(PUBLIC_SUBJECTS) >= set(prompts.SENSITIVITY_CATEGORIES):
    raise RuntimeError(
        "these categories have no plain phrase to publish in place of the words: "
        f"{sorted(set(prompts.SENSITIVITY_CATEGORIES) - set(PUBLIC_SUBJECTS))}"
    )

#: How sure the model must be before a held-band passage is actually withheld. Below it the
#: passage is published, labelled, and named in :attr:`Report.notes` for a person to see —
#: because an uncertain hold spends the review budget that the certain ones need, and the
#: whole gate lives or dies on that budget. Tuned against real numbers in shadow mode; it is
#: a constant rather than a setting because it is a judgement about this record, not a knob
#: for an operator to turn at 06:30.
HOLD_CONFIDENCE = 0.75

#: An explicit instruction not to write something down is not judged at all — it is a
#: person's own instruction and it outranks everything else here, including this module's
#: preference for precision. It is never downgraded for want of confidence.
NEVER_DOWNGRADED: frozenset[str] = frozenset({"do_not_write_down"})

#: When a quoted passage cannot be found character for character, the fallback is the whole
#: sentence carrying this much of the quote's own vocabulary. High, because the fallback
#: cuts a superset of what was asked for and a wrong sentence is a wrong hold.
SENTENCE_COVERAGE = 0.9

#: Below this many words a quote is matched exactly or not at all — coverage across a
#: handful of common words means nothing.
MIN_QUOTE_WORDS = 3

#: The same quoted words can genuinely occur more than once in one recording, and holding
#: only the first occurrence would leave the sensitive words in the record. Every occurrence
#: is held, up to this many; beyond it something is wrong with the quote rather than with
#: the recording, and it is said in a note instead.
MAX_OCCURRENCES = 8

#: A gate that would hold a third of a recording is not a gate, it is a shredder, and the
#: fault is in the classifier rather than in the conversation. Nothing is released on
#: account of this — releasing on a threshold is exactly the silent-emptying failure this
#: design refuses — it is said out loud in :attr:`Report.notes` and everything stands.
IMPLAUSIBLE_HELD_FRACTION = 0.35

#: The sentences either side of an explicit "don't write this down" that are held with it.
#: The instruction is about the words around it, so holding the phrase alone would withhold
#: nothing at all and still cost an approval.
DO_NOT_WRITE_CONTEXT_SENTENCES = 1


# ------------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class GateSettings:
    """How the gate is configured for this deployment, resolved once.

    Built directly (tests, ``selftest``) or with :meth:`from_config`. ``reviewers`` and
    ``review_base_url`` are carried here so the queue and the morning email can be built
    from one object; this module itself uses only ``mode`` and ``hold_confidence``.
    """

    mode: str = "shadow"
    hold_confidence: float = HOLD_CONFIDENCE
    held_store: str = ""
    review_base_url: str = ""
    #: route name -> the address that reviews that route's held passages; empty means the
    #: service owner. ``repr=False`` because these are addresses, and the house rule is that
    #: this service never prints one anywhere, for any reason.
    reviewers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        mode = (self.mode or "shadow").strip().lower()
        if mode not in GATE_MODES:
            raise ValueError(
                f"GATE_MODE={self.mode!r} is not one of: " + ", ".join(GATE_MODES)
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "hold_confidence", max(0.0, min(1.0, float(self.hold_confidence))))

    @property
    def classifies(self) -> bool:
        """Whether recordings are read for sensitive passages at all."""
        return self.mode != "off"

    @property
    def withholds(self) -> bool:
        """Whether anything is actually cut out of a transcript. ``on`` only."""
        return self.mode == "on"

    def reviewer_for(self, route: str) -> str:
        """Who reviews this route's held passages. Empty means the service owner.

        A staff member reviews their own held passages: he sees the count and the site,
        never the words. That is not politeness — staff record voluntarily and can stop
        keeping a folder, and if they work out that he reads the held text from their calls
        the rational answer is to stop recording, which loses the recordings entirely.
        """
        return str(self.reviewers.get((route or "").strip(), "") or "").strip()

    @classmethod
    def from_config(cls, config: Any) -> "GateSettings":
        return cls(
            mode=str(getattr(config, "gate_mode", "shadow") or "shadow"),
            held_store=str(getattr(config, "held_store_path", "") or ""),
            review_base_url=str(getattr(config, "gate_review_base_url", "") or ""),
            reviewers=dict(getattr(config, "route_reviewers", {}) or {}),
        )


# -------------------------------------------------------------------------- normalisation


# The same normalisation ``extract.verify_quote`` applies, kept here rather than imported
# because that function answers a boolean and this module needs OFFSETS, and because
# ``extract`` will import this module rather than the other way round. The two are held to
# the same behaviour by a test (``tests/test_sensitivity_gate.py``) that compares this
# against ``extract.normalise_for_match`` over the awkward characters.
_ZERO_WIDTH = {0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD}
_PUNCT_EQUIVALENTS = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", " ": " ",
}
_EDGE_PUNCT = " \t\r\n\"'`.,;:!?-–—…()[]{}<>"
_TOKEN_RE = re.compile(r"[0-9a-zà-ɏ]+")
_WORDISH = re.compile(r"[0-9A-Za-zÀ-ÿ]")


@dataclass(frozen=True)
class _Normalised:
    """Normalised text alongside the map back to where each character came from."""

    text: str
    source: tuple[int, ...]

    def bounds(self, start: int, end: int) -> tuple[int, int]:
        """The original offsets that produced ``text[start:end]``."""
        if start >= end or not self.source:
            return (0, 0)
        first = self.source[start]
        last = self.source[min(end, len(self.source)) - 1]
        return (first, last + 1)


def _normalise(text: str) -> _Normalised:
    out: list[str] = []
    source: list[int] = []
    pending_space = False
    started = False
    for index, raw in enumerate(text or ""):
        if ord(raw) in _ZERO_WIDTH:
            continue
        char = _PUNCT_EQUIVALENTS.get(raw, raw)
        if char.isspace():
            pending_space = started
            continue
        if pending_space:
            out.append(" ")
            source.append(index)
            pending_space = False
        folded = unicodedata.normalize("NFKC", char).casefold()
        for piece in folded:
            out.append(piece)
            source.append(index)
        started = True
    return _Normalised("".join(out), tuple(source))


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _coverage(wanted: Sequence[str], window: str) -> float:
    if not wanted:
        return 0.0
    have = set(_tokens(window))
    return sum(1 for token in wanted if token in have) / len(wanted)


def _wordish(text: str, index: int) -> bool:
    return 0 <= index < len(text) and bool(_WORDISH.match(text[index]))


def _snap(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span so it never cuts a word or a number in half.

    Another module cuts text on these offsets. A span ending inside ``R1,650,000`` leaves
    ``,000`` behind in the record, which is both wrong and worse than leaving the whole
    figure: it reads as a complete number.
    """
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    while True:
        moved = False
        while _wordish(text, start - 1) and _wordish(text, start):
            start -= 1
            moved = True
        while _wordish(text, end) and _wordish(text, end - 1):
            end += 1
            moved = True
        # A digit group separator sits between two digits and belongs to the number.
        if start >= 2 and text[start - 1] in ",." and text[start - 2].isdigit() and text[start].isdigit():
            start -= 2
            moved = True
        if end + 1 < len(text) and text[end] in ",." and text[end - 1].isdigit() and text[end + 1].isdigit():
            end += 2
            moved = True
        if not moved:
            return (start, end)


# ------------------------------------------------------------------------------ sentences


_SENTENCE_END = re.compile(r"[.!?…]+[\s\"'’)\]]*|\n+")


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Every sentence in the transcript, as ``(start, end)`` offsets, in order.

    A newline ends a sentence: these transcripts are speaker-labelled, one turn per line,
    and running two speakers' words together would make a sentence-level hold cut somebody
    else's sentence.
    """
    spans: list[tuple[int, int]] = []
    at = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        if text[at:end].strip():
            spans.append((at, end))
        at = end
    if text[at:].strip():
        spans.append((at, len(text)))
    return tuple(spans)


def _sentence_index(spans: Sequence[tuple[int, int]], position: int) -> int:
    for index, (start, end) in enumerate(spans):
        if start <= position < end:
            return index
    return -1


# ------------------------------------------------------------------------- locating spans


@dataclass(frozen=True)
class Span:
    """One resolved passage: exact offsets into the transcript, and how they were found."""

    start: int
    end: int
    method: str  # "exact" | "sentence" | "rule"

    @property
    def length(self) -> int:
        return self.end - self.start


def locate_spans(quote: str, transcript: str) -> tuple[Span, ...]:
    """Where ``quote`` genuinely appears in ``transcript``, as exact offsets.

    Case, whitespace and the shapes of quote marks and dashes are normalised — a model that
    types a straight apostrophe where the transcript has a curly one has not invented
    anything — and nothing else is relaxed. No word is dropped, reordered or stemmed:
    "unit 4" and "unit 14" must not be the same passage when text is about to be cut on the
    answer.

    Every occurrence is returned, because holding only the first would leave the same words
    standing later in the same recording.

    When the quote is not found character for character, one fallback applies: the sentence
    carrying :data:`SENTENCE_COVERAGE` of the quote's own words is returned whole. It can
    only ever cut more than was asked for, never a fragment of a phrase. Anything less
    certain than that returns nothing at all, and the caller says so out loud rather than
    holding a guessed span.
    """
    if not (quote or "").strip() or not (transcript or "").strip():
        return ()

    hay = _normalise(transcript)
    needle = _normalise(quote).text.strip(_EDGE_PUNCT).strip()
    if not needle:
        return ()

    found: list[Span] = []
    at = 0
    while len(found) < MAX_OCCURRENCES:
        hit = hay.text.find(needle, at)
        if hit < 0:
            break
        start, end = hay.bounds(hit, hit + len(needle))
        start, end = _snap(transcript, start, end)
        found.append(Span(start, end, "exact"))
        at = hit + max(1, len(needle))
    if found:
        return tuple(found)

    words = _tokens(needle)
    if len(words) < MIN_QUOTE_WORDS:
        return ()
    best: tuple[float, int, int] | None = None
    for start, end in _sentence_spans(transcript):
        score = _coverage(words, _normalise(transcript[start:end]).text)
        if score >= SENTENCE_COVERAGE and (best is None or score > best[0]):
            best = (score, start, end)
    if best is None:
        return ()
    start, end = _snap(transcript, best[1], best[2])
    return (Span(start, end, "sentence"),)


# ----------------------------------------------------------------------- mechanical rules


@dataclass(frozen=True)
class _Rule:
    """One mechanical trigger: near-certain, so it may hold without a model agreeing."""

    id: str
    category: str
    pattern: re.Pattern[str]
    reason: str
    scope: str  # "match" | "sentence-context"


_I = re.IGNORECASE


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _I)


#: "Do not write this down", in any language — the one instruction that outranks every
#: other judgement in this module, because it is a person saying what they want done with
#: their own words.
#:
#: Every pattern here is anchored on the RECORD, never on writing as such. "Don't write to
#: the trustees yet" and "moenie vir hulle sê wat ons betaal het nie" are instructions about
#: correspondence and about what to say to a client; neither is a request that something not
#: be written down, and a rule that could not tell them apart would hold a large part of an
#: ordinary week.
_DO_NOT_WRITE_PATTERNS: tuple[str, ...] = (
    # English: don't write this down / never write that down
    r"\b(?:do\s*n['’]?t|dont|do\s+not|never|please\s+do\s*n['’]?t)\b[^.!?\n]{0,30}?\bwrit\w*\b[^.!?\n]{0,20}?\bdown\b",
    # English: don't put this in the report / minutes / notes
    r"\b(?:do\s*n['’]?t|dont|do\s+not|never)\b[^.!?\n]{0,20}?\b(?:put|include|type|log)\b[^.!?\n]{0,25}?\bin\s+(?:the\s+)?(?:report|minutes|record|notes?|write[\s-]?up|file|system|email)\b",
    # English: don't minute that / don't record this
    r"\b(?:do\s*n['’]?t|dont|do\s+not|never)\s+(?:minute|record)\s+(?:this|that|it)\b",
    # English: off the record / not for the record
    r"\b(?:off|not\s+for)\s+the\s+record\b",
    # English: this doesn't go in the report
    r"\b(?:this|that|it)\s+(?:does\s*n['’]?t|doesn['’]?t|must\s*n['’]?t|mustn['’]?t|is\s+not\s+to)\s+go\s+(?:in|into|on)\b",
    # English: keep this between us / to yourself / off the record
    r"\bkeep\s+(?:this|that|it)\s+(?:off\s+the\s+record|between\s+us|to\s+yourself|quiet)\b",
    r"\b(?:just\s+)?between\s+(?:you\s+and\s+(?:me|i)|us\s+two|ourselves|the\s+two\s+of\s+us)\b",
    # English: strike that from the record
    r"\b(?:strike|leave)\s+(?:this|that|it)\s+(?:from|out\s+of)\s+the\s+(?:record|minutes|notes?|report)\b",
    # Afrikaans: moenie dit neerskryf nie — the verb must be the writing-down one, never
    # the bare "skryf", which is what you do to a client.
    r"\bmoenie\b[^.!?\n]{0,40}?\b(?:neerskryf|neergeskryf|opskryf|opgeskryf|aanteken|aangeteken|notuleer)\b",
    r"\bmoenie\b[^.!?\n]{0,40}?\bskryf\b[^.!?\n]{0,15}?\bneer\b",
    # Afrikaans: keep it between us / not for the record
    r"\b(?:hou|bly)\s+(?:dit|hierdie|hierso)?\s*tussen\s+ons\b",
    r"\b(?:nie\s+vir\s+die\s+rekord|buite\s+die\s+rekord)\b",
    # isiXhosa / isiZulu: the negative imperative of -bhala / -loba, "do not write".
    # These forms mean nothing else, which is why they can stand alone.
    r"\b(?:ungakubhali|ungabhali|ungayibhali|ungazibhali|ungakulobi|ungalobi|"
    r"sukubhala|sukukubhala|ningabhali|ningakubhali|musa\s+uku(?:ku)?bhala|"
    r"musani\s+uku(?:ku)?bhala)\b",
)

#: "This must not leave the firm" — his own words for the legal_exposure band. Held for the
#: same reason and reported under its own category, so the person reviewing it sees what
#: kind of thing it is.
_MUST_NOT_LEAVE = (
    r"\b(?:must\s*n['’]?t|must\s+not|cannot|can\s*not|shouldn['’]?t|should\s+not|mag\s+nie)\b"
    r"[^.!?\n]{0,20}?\bleave\b\s+(?:this\s+|the\s+|our\s+)?(?:firm|office|room|building|company)\b"
)

RULES: tuple[_Rule, ...] = tuple(
    _Rule(
        id=f"do-not-write-down/{index}",
        category="do_not_write_down",
        pattern=_p(pattern),
        reason="somebody asked that this not be written down",
        scope="sentence-context",
    )
    for index, pattern in enumerate(_DO_NOT_WRITE_PATTERNS)
) + (
    _Rule(
        id="must-not-leave-the-firm",
        category="legal_exposure",
        pattern=_p(_MUST_NOT_LEAVE),
        reason="it was said that this must not leave the firm",
        scope="sentence-context",
    ),
)


#: Digits that could be an account or a card, with the words that say so nearby. Deliberately
#: keyword-anchored: an unanchored run of digits is a unit number, an erf number, an invoice
#: number or a phone number far more often than it is a bank detail.
_ACCOUNT_WORDS = _p(
    r"\b(?:account|acc|acct|a/c|bank|banking|branch\s*code|iban|swift|card|cvv|"
    r"rekening|rekeningnommer|bankrekening|takkode|kaartnommer|kredietkaart|"
    r"i-?akhawunti|inombolo\s+ye-?akhawunti)\b"
)
#: Words that make a long run of digits something else entirely. Checked before the account
#: words, because "invoice 4501234567890" beside the word "account" is still an invoice.
_NOT_AN_ACCOUNT = _p(
    r"\b(?:invoice|inv|order|po|certificate|cert|erf|stand|unit|plot|portion|"
    r"reference|ref|quote|quotation|job|ticket|serial|vat|meter)\b"
)
_DIGIT_RUN = re.compile(r"(?<![\d,.])\d[\d\s-]{4,22}\d(?![\d])")
_ID_CANDIDATE = re.compile(r"(?<![\d,.])(\d[\d\s-]{11,17}\d)(?![\d])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_sa_id(digits: str) -> bool:
    """A South African identity number, checked rather than guessed.

    Thirteen digits, a real date of birth in the first six, a citizenship digit of 0 or 1,
    and a Luhn checksum over the whole thing. Three independent constraints is what makes
    this safe to hold on mechanically: an invoice number does not pass them by accident.
    """
    if len(digits) != 13 or not digits.isdigit():
        return False
    month, day = int(digits[2:4]), int(digits[4:6])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return False
    if digits[10] not in "01":
        return False
    return _luhn_ok(digits)


def _identifier_spans(transcript: str) -> list[tuple[Span, str, str]]:
    """Identity numbers and bank details, as ``(span, rule id, reason)``.

    Only the identifier itself is held: "his ID number is ⟨held⟩" still reads as a sentence,
    and cutting the sentence around it would hold the fact that a site register exists.
    """
    out: list[tuple[Span, str, str]] = []
    seen: set[tuple[int, int]] = set()

    for match in _ID_CANDIDATE.finditer(transcript):
        digits = re.sub(r"[\s-]", "", match.group(1))
        if not _is_sa_id(digits):
            continue
        span = Span(*_snap(transcript, match.start(1), match.end(1)), "rule")
        if (span.start, span.end) in seen:
            continue
        seen.add((span.start, span.end))
        out.append((span, "identifier/sa-id", "it is an identity number"))

    for match in _DIGIT_RUN.finditer(transcript):
        digits = re.sub(r"[\s-]", "", match.group(0))
        if len(digits) < 6:
            continue
        before = transcript[max(0, match.start() - 40): match.start()]
        if _NOT_AN_ACCOUNT.search(before):
            continue
        card = 13 <= len(digits) <= 19 and _luhn_ok(digits)
        if not card and not _ACCOUNT_WORDS.search(before):
            continue
        span = Span(*_snap(transcript, match.start(), match.end()), "rule")
        if (span.start, span.end) in seen:
            continue
        seen.add((span.start, span.end))
        out.append((
            span,
            "identifier/card" if card else "identifier/account",
            "it is a card or account number" if card else "it is a bank account number",
        ))
    return out


def rule_findings(transcript: str) -> tuple["Finding", ...]:
    """Everything the mechanical rules hold, with no model involved.

    These run whether or not the model answered, and whether or not it agreed. An explicit
    instruction not to write something down is not a judgement call, and an identity number
    that passes its own checksum is not an opinion.
    """
    text = transcript or ""
    if not text.strip():
        return ()
    sentences = _sentence_spans(text)
    findings: list[Finding] = []

    for rule in RULES:
        for match in rule.pattern.finditer(text):
            if rule.scope == "sentence-context":
                index = _sentence_index(sentences, match.start())
                if index < 0:
                    start, end = match.start(), match.end()
                else:
                    first = max(0, index - DO_NOT_WRITE_CONTEXT_SENTENCES)
                    last = min(len(sentences) - 1, index + DO_NOT_WRITE_CONTEXT_SENTENCES)
                    start, end = sentences[first][0], sentences[last][1]
            else:
                start, end = match.start(), match.end()
            start, end = _snap(text, start, end)
            findings.append(
                Finding(
                    start=start,
                    end=end,
                    category=rule.category,
                    disposition=HELD,
                    confidence=1.0,
                    reason=rule.reason,
                    subject=PUBLIC_SUBJECTS[rule.category],
                    harmed="",
                    source="rule",
                    rule=rule.id,
                    method="rule",
                    text=text[start:end],
                )
            )

    for span, rule_id, reason in _identifier_spans(text):
        findings.append(
            Finding(
                start=span.start,
                end=span.end,
                category="bare_identifier",
                disposition=HELD,
                confidence=1.0,
                reason=reason,
                subject=PUBLIC_SUBJECTS["bare_identifier"],
                harmed="the person it belongs to",
                source="rule",
                rule=rule_id,
                method="rule",
                text=text[span.start:span.end],
            )
        )
    return tuple(findings)


# ------------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class Finding:
    """One passage, its exact offsets, and what happens to it.

    ``text`` is the transcript's own words and is the sensitive part: it is excluded from
    :meth:`to_dict` unless asked for, so that a finding can be logged, counted, put in the
    morning email or written to the ledger without carrying what it was hiding.

    ``subject`` is the opposite — a short public noun phrase, checked mechanically to carry
    no name, figure or address — and it is what the record shows in place of the words.
    """

    start: int
    end: int
    category: str
    disposition: str
    confidence: float
    reason: str
    subject: str
    harmed: str = ""
    source: str = "model"        # "model" | "rule"
    rule: str = ""
    method: str = "exact"        # "exact" | "sentence" | "rule"
    text: str = ""
    #: True when this sat in the held band but the model was not sure enough to withhold it.
    #: It is published with its label, and :attr:`Report.notes` says so.
    downgraded: bool = False

    @property
    def held(self) -> bool:
        return self.disposition == HELD

    @property
    def length(self) -> int:
        return self.end - self.start

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "category": self.category,
            "disposition": self.disposition,
            "confidence": round(float(self.confidence), 4),
            "reason": self.reason,
            "subject": self.subject,
            "harmed": self.harmed,
            "source": self.source,
            "rule": self.rule,
            "method": self.method,
            "downgraded": self.downgraded,
            "chars": self.length,
        }
        if include_text:
            out["text"] = self.text
        return out


@dataclass(frozen=True)
class Report:
    """What the gate found in one recording, and what — if anything — is withheld.

    :attr:`findings` are sorted by position and **do not overlap**, because another module
    cuts text on them. :meth:`would_hold` is populated in every mode and is what shadow mode
    measures; :meth:`spans_to_mask` is empty unless the gate is switched on.
    """

    mode: str
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()
    model_answered: bool = False
    transcript_chars: int = 0

    @property
    def active(self) -> bool:
        return self.mode != "off"

    def would_hold(self) -> tuple[Finding, ...]:
        """Everything the gate judged held — whatever mode it is in. Shadow mode's whole point."""
        return tuple(f for f in self.findings if f.held)

    def labelled(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.held)

    def spans_to_mask(self) -> tuple[Finding, ...]:
        """What *would* be cut out of the transcript. Empty unless ``GATE_MODE=on``.

        The one method here that reads the mode, and deliberately the only one: everything
        else measures, so that switching the gate on changes what is withheld and nothing
        else about what is recorded.

        It is a statement of the rule, not the enforcement of it. The thing that actually
        keeps a shadow run from cutting anything is
        :func:`transcriber.redact.redact_text`, which returns the text untouched in any mode
        but ``on``, and it does not consult this method. That is not duplication to be
        tidied away: the module that classifies and the module that cuts arrive at the
        answer independently, and a redactor that took its instruction from the classifier's
        own view of the mode would have one place to get the mode wrong instead of two that
        must agree.
        """
        return self.would_hold() if self.mode == "on" else ()

    def covers(self, start: int, end: int) -> Finding | None:
        """The held finding overlapping ``[start, end)``, if any.

        A query, for a caller that already holds offsets into the same transcript this
        report was built from. It is not on the publishing path: by the time
        :mod:`transcriber.outputs` sees anything, the transcript has been cut and the
        offsets no longer mean what they meant here, so the equivalent question there is
        :meth:`transcriber.redact.Redaction.wholly_held`, which asks it of the redaction
        that did the cutting. Said plainly because this docstring used to claim outputs
        called it, and a comment describing a call that does not exist is worse than no
        comment: it reads as a guarantee.

        What it is genuinely for is the property the whole ordering rests on — that a held
        span and a labelled one never overlap, so cutting on one cannot take the other with
        it — which is asserted directly against this method in the test suite.
        """
        for finding in self.findings:
            if finding.held and finding.start < end and start < finding.end:
                return finding
        return None

    def counts(self) -> dict[str, int]:
        """How many of each category, in the taxonomy's own order. No words, ever.

        This is what the service owner is shown for a staff member's recording: the count
        and the site, never the text.
        """
        out: dict[str, int] = {}
        for category in prompts.SENSITIVITY_CATEGORIES:
            found = sum(1 for f in self.findings if f.category == category)
            if found:
                out[category] = found
        return out

    @property
    def held_chars(self) -> int:
        return sum(f.length for f in self.would_hold())

    def describe(self) -> str:
        """One plain line for a log or the morning email. Carries no held words."""
        if not self.active:
            return "the sensitivity gate is switched off"
        held = len(self.would_hold())
        labelled = len(self.labelled())
        if not held and not labelled:
            return "nothing in this recording needs holding or labelling"
        parts: list[str] = []
        if held:
            verb = "held" if self.mode == "on" else "would have been held"
            parts.append(f"{held} passage{'s' if held != 1 else ''} {verb}")
        if labelled:
            parts.append(f"{labelled} labelled and published")
        return ", ".join(parts)

    def to_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "model_answered": self.model_answered,
            "findings": [f.to_dict(include_text=include_text) for f in self.findings],
            "counts": self.counts(),
            "held": len(self.would_hold()),
            "labelled": len(self.labelled()),
            "held_chars": self.held_chars,
            "transcript_chars": self.transcript_chars,
            "withheld": bool(self.spans_to_mask()),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- the subject


_SUBJECT_MAX = 60
#: Digits and ``@`` because the phrase stands in place of words that were withheld and must
#: not smuggle a figure or an address back into the marker. ``?`` and ``!`` because the
#: marker is written to be picked up by the record's question scan, which captures a run
#: ending in ``?`` and starting after a full stop or a line break: a subject carrying either
#: splits the marker's one sentence in two and files a second, meaningless open question on
#: the site's live page — "a rate for the remedial?" — which somebody then has to close.
_SUBJECT_BANNED = re.compile(r"[0-9@?!]")
_MID_CAPITAL = re.compile(r"(?<=\S\s)[A-Z]")


def _public_subject(offered: str, category: str) -> tuple[str, bool]:
    """The noun phrase published in place of the words, and whether the model's was used.

    The model is asked for one, and it is checked rather than trusted: this string goes onto
    a page a client may read, in place of text that was withheld because it must not be
    read. A phrase carrying a digit, an address or a capitalised name has smuggled the
    detail into the marker, so it is replaced by the dull one for its category.
    """
    phrase = " ".join(str(offered or "").split())
    if not phrase:
        return PUBLIC_SUBJECTS[category], False
    if len(phrase) > _SUBJECT_MAX:
        return PUBLIC_SUBJECTS[category], False
    if _SUBJECT_BANNED.search(phrase):
        return PUBLIC_SUBJECTS[category], False
    if _MID_CAPITAL.search(phrase):
        # A capital that is not the first letter is a name or a company, nine times in ten.
        return PUBLIC_SUBJECTS[category], False
    return phrase, True


# ------------------------------------------------------------------------------- merging


def _merge_same_band(findings: Sequence[Finding], transcript: str) -> list[Finding]:
    """Overlapping findings of one band become one span. Deterministic, order-independent."""
    ordered = sorted(findings, key=lambda f: (f.start, f.end, f.category))
    merged: list[Finding] = []
    for finding in ordered:
        if merged and finding.start <= merged[-1].end:
            merged[-1] = _join(merged[-1], finding, transcript)
        else:
            merged.append(finding)
    return merged


def _join(first: Finding, second: Finding, transcript: str) -> Finding:
    """One finding from two that overlap. The stronger claim decides how it is described."""
    start = min(first.start, second.start)
    end = max(first.end, second.end)
    # Whichever is more certain names the passage; a rule beats a model at equal confidence,
    # because a rule is a person's own instruction rather than a judgement.
    lead, other = first, second
    if (second.confidence, second.source == "rule") > (first.confidence, first.source == "rule"):
        lead, other = second, first
    reasons = [lead.reason] + ([other.reason] if other.reason != lead.reason else [])
    return Finding(
        start=start,
        end=end,
        category=lead.category,
        disposition=lead.disposition,
        confidence=max(first.confidence, second.confidence),
        reason="; ".join(r for r in reasons if r)[:300],
        subject=lead.subject,
        harmed=lead.harmed or other.harmed,
        source="rule" if "rule" in (first.source, second.source) else lead.source,
        rule=lead.rule or other.rule,
        method=lead.method,
        text=transcript[start:end],
        downgraded=lead.downgraded and other.downgraded,
    )


def _subtract(labelled: Sequence[Finding], held: Sequence[Finding], transcript: str) -> list[Finding]:
    """Trim labelled spans out of held ones, so the result never overlaps.

    A labelled passage inside a held one is simply held — but the held span must not GROW to
    swallow the labelled one, because over-holding is the failure this module is tuned
    against. So the labelled span is cut back to whatever lies outside, and dropped when
    nothing does.
    """
    out: list[Finding] = []
    for finding in labelled:
        pieces: list[tuple[int, int]] = [(finding.start, finding.end)]
        for block in held:
            nxt: list[tuple[int, int]] = []
            for start, end in pieces:
                if block.end <= start or block.start >= end:
                    nxt.append((start, end))
                    continue
                if start < block.start:
                    nxt.append((start, block.start))
                if block.end < end:
                    nxt.append((block.end, end))
            pieces = nxt
        for start, end in pieces:
            start, end = _snap(transcript, start, end)
            if end - start < 3:
                continue
            out.append(
                Finding(
                    start=start,
                    end=end,
                    category=finding.category,
                    disposition=finding.disposition,
                    confidence=finding.confidence,
                    reason=finding.reason,
                    subject=finding.subject,
                    harmed=finding.harmed,
                    source=finding.source,
                    rule=finding.rule,
                    method=finding.method,
                    text=transcript[start:end],
                    downgraded=finding.downgraded,
                )
            )
    return out


# --------------------------------------------------------------------------- the assessor


def _as_entries(data: Any) -> tuple[list[Mapping[str, Any]], bool]:
    """The model's ``sensitive_passages``, and whether it answered the question at all."""
    if data is None:
        return [], False
    if isinstance(data, Mapping):
        if "sensitive_passages" not in data:
            return [], False
        raw = data.get("sensitive_passages")
    else:
        raw = data
    if not isinstance(raw, (list, tuple)):
        return [], False
    return [entry for entry in raw if isinstance(entry, Mapping)], True


def _as_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(1.0, number))


def _one_line(text: str, limit: int = 200) -> str:
    line = " ".join(str(text or "").split())
    return line[:limit]


def assess(
    transcript: str,
    data: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    *,
    settings: GateSettings | None = None,
) -> Report:
    """Classify one transcript. The whole of this module's public behaviour.

    ``data`` is the extraction call's own answer — either the whole decoded object or just
    its ``sensitive_passages`` list. Passing ``None`` runs the mechanical rules alone, which
    is what happens when the gate is running and the model did not answer: an instruction
    not to write something down is still honoured.

    Offsets are into ``transcript`` exactly as given. The caller must not have masked or
    otherwise rewritten it first — see this module's docstring for why that ordering is not
    negotiable.
    """
    settings = settings or GateSettings()
    text = transcript or ""
    if not settings.classifies:
        return Report(mode=settings.mode, transcript_chars=len(text))

    entries, answered = _as_entries(data)
    notes: list[str] = []
    if not answered:
        notes.append(
            "the model did not answer the sensitivity question for this recording, so only "
            "the mechanical rules were applied to it"
        )

    collected: list[Finding] = list(rule_findings(text))

    for entry in entries:
        category = str(entry.get("category") or "").strip().lower()
        if category not in DISPOSITIONS:
            notes.append(
                f"a passage was returned under {category or '(no category)'!r}, which is not "
                "one of the kinds this service knows, so nothing was done with it"
            )
            continue
        quote = str(entry.get("quote") or "")
        spans = locate_spans(quote, text)
        subject, model_subject = _public_subject(str(entry.get("what_it_is") or ""), category)
        disposition = DISPOSITIONS[category]
        confidence = _as_confidence(entry.get("confidence"))
        reason = _one_line(entry.get("reason"))

        if not spans:
            notes.append(
                f"the model reported {subject} in this recording, but the words it quoted "
                "are not in the transcript, so nothing was "
                + ("held" if disposition == HELD else "labelled")
                + " on it — a person should look at this recording"
            )
            continue

        downgraded = False
        if (
            disposition == HELD
            and category not in NEVER_DOWNGRADED
            and confidence < settings.hold_confidence
        ):
            # Precision, spent on purpose. An uncertain hold costs an approval he has to
            # clear, and ten of those a day is a page he stops opening. It is published
            # with its label instead — and said out loud here, so it is neither withheld
            # silently nor published silently.
            disposition = LABELLED
            downgraded = True
            notes.append(
                f"{subject} was close to the line but the reading was only "
                f"{confidence:.0%} sure, so it was published with a label rather than held"
            )
        if not model_subject and disposition == HELD:
            notes.append(
                "the description offered for a held passage carried detail of its own, so "
                "the record shows the plain phrase for its kind instead"
            )

        for span in spans:
            collected.append(
                Finding(
                    start=span.start,
                    end=span.end,
                    category=category,
                    disposition=disposition,
                    confidence=confidence,
                    reason=reason,
                    subject=subject,
                    harmed=_one_line(entry.get("who_is_harmed"), 80),
                    source="model",
                    method=span.method,
                    text=text[span.start:span.end],
                    downgraded=downgraded,
                )
            )
        if len(spans) >= MAX_OCCURRENCES:
            notes.append(
                f"the words quoted for {subject} occur at least {MAX_OCCURRENCES} times in "
                "this recording, which usually means the quote is a common phrase rather "
                "than the passage — a person should look at it"
            )

    held = _merge_same_band([f for f in collected if f.held], text)
    labelled = _merge_same_band([f for f in collected if not f.held], text)
    findings = tuple(sorted(held + _subtract(labelled, held, text), key=lambda f: (f.start, f.end)))

    held_chars = sum(f.length for f in findings if f.held)
    if text and held_chars > len(text) * IMPLAUSIBLE_HELD_FRACTION:
        notes.append(
            f"the gate marked {held_chars / len(text):.0%} of this recording as needing to "
            "be held, which is far more than a recording normally contains. Nothing has "
            "been released on account of that — it stands as it is — but the classifier "
            "needs looking at"
        )

    report = Report(
        mode=settings.mode,
        findings=findings,
        notes=tuple(notes),
        model_answered=answered,
        transcript_chars=len(text),
    )
    if findings or notes:
        # Counts and categories only. Nothing in this line is the recording's own words.
        log.info(
            "sensitivity gate (%s): %s [%s]",
            report.mode,
            report.describe(),
            ", ".join(f"{k}={v}" for k, v in sorted(report.counts().items())) or "no categories",
        )
    return report
