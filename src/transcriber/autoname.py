"""A name for a recording that arrived without one, or no name at all.

He names his site notes by hand — ``BEACH COURT SITE WALK 270826.m4a`` — and his phone
names his calls — ``Call +27817957457_260420_133533.m4a``. The gap is the site note he did
not get to before it uploaded, which lands as the voice recorder's own default,
``Voice 260806_162219.m4a``, and stays that way forever. In his words: *"the system should
be able to infer the potential name from the recording and suggest it... if it is unsure or
there is ambiguity, it doesnt try and fail it surfaces for human input."*

**There are two outcomes and no third.** A name, or no name. No name is exactly today's
behaviour — the file keeps the recorder's name, the transcript is written on time, and the
morning email says one line about it. Nothing is ever held, delayed, retried or queued for
an answer, and there is nothing for him to approve: an unnamed recording is not a problem
to be resolved, it is a recording with a plain title.

**Nothing is ever renamed in OneDrive.** Not the audio, not the three output files. The name
reaches the transcript's subject line and its heading, which is what the record reads and
what it shows a person. See :mod:`transcriber.outputs`.

The rule is deliberately hard to satisfy, because a wrong name is worse than no name: a
site note filed under the wrong site pollutes that site's record and nobody ever notices.
Nine conditions, all mechanical, all assertable, every one of them failing towards no name:

    N1  the model named a site at all
    N2  it is not a placeholder — "the site", "the office", "here"
    N3  those exact words appear in the PUBLISHED body of the transcript
    N4  the words look like a name — letters, digits and single spaces
    N6  the span names exactly one site the record knows about
    N7  it is what the recording is ABOUT — he announced it, or it wins the conversation
    N8  the name survives being made into a name
    N9  **adding the name does not change what the record binds the file to**

N9 needs explaining, because the hazard it addresses is real and it is currently
unreachable. The title becomes part of the bytes the record scores, so a title naming the
wrong site does not merely mislabel the note — it *ties* a binding that was previously
clean and sends the note nowhere. Measured, on the real record: a body that binds cleanly
to Milton Court binds to **nothing at all** once ``CANTERBURY`` is put in its subject line.

N9 renders the file twice, with the name and without, and refuses unless the record's own
answer is identical. **As the rules stand it can never fire**, and the honest reason is
worth writing down: the record scores a term by whether it appears *at all*, not how often,
and N3 has already established that the span appears in the body — so adding it to the
subject line introduces no term the body did not have. N9 is kept anyway, and it is not
decoration. It is the only check here that depends on nothing except the record's actual
answer, so it stays correct if the record ever counts occurrences, if the transcript's
layout changes, or if a later hand loosens N3 or N6 without re-deriving why they were
tight. ``test_naming_never_misfiles.py`` asserts both halves: that the hazard is real, and
that N9 rejects it when the earlier rules are removed.

This module never imports :mod:`transcriber.outputs`. The renderer arrives as a callable,
which is what keeps the import graph acyclic and what lets every rule be tested without a
Graph client, a network or a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import declaration, naming, sitebook

__all__ = [
    "STOP_SITES",
    "NameDecision",
    "NO_NAME",
    "is_recorder_default",
    "eligible",
    "decide",
]

#: The voice recorder's own default name, and nothing else. Anchored at both ends and
#: **case-sensitive**, so ``voice 260806_162219`` and ``VOICE NOTE FOR CAREL`` are not it.
#: Deliberately narrow: the cost of a device this does not recognise is that its recordings
#: are never named, and the cost of one it recognises wrongly is renaming something he
#: chose. Those are not the same size of mistake.
_RECORDER_DEFAULT = re.compile(r"^Voice \d{6}_\d{6}$")

#: A name has to look like a name: letters and digits in single-spaced words. Rules out a
#: span carrying punctuation, a line break, a timestamp prefix or a speaker label.
_NAME_SHAPE = re.compile(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*")

#: What a model says when it has not identified a site but has been asked for one. Every
#: one of these is a real thing to say on a site walk and none of them names anywhere.
STOP_SITES = frozenset({
    "site", "the site", "this site", "site office", "office", "head office",
    "the building", "the house", "the block", "the yard", "the unit", "the job",
    "here", "there", "on site", "unknown", "n/a", "none", "various", "multiple",
})

#: Shortest and longest a proposed name may be. Sixty leaves room inside the subject line's
#: ninety for the longest suffix the renderers append; his longest real name is 49.
_MIN_NAME = 3
_MAX_NAME = 60

#: How early the first mention must fall, and how far apart the first and last must be, as
#: fractions of the transcript. Together they say "this recording is ABOUT this site"
#: rather than "this site came up". A phone call taken during a walk is local: it is a
#: cluster somewhere in the middle, and it fails the spread.
_FIRST_WITHIN = 0.25
_MIN_SPREAD = 0.40

#: The fewest mentions that can propose a name.
_MIN_MENTIONS = 2


def is_recorder_default(stem: str) -> bool:
    """True only for the voice recorder's own untouched default name."""
    return _RECORDER_DEFAULT.fullmatch(stem or "") is not None


@dataclass(frozen=True)
class NameDecision:
    """What was decided about one recording's name, and why, in plain English.

    ``decided`` means *a decision was reached and stored*, *not* "a name was found". The
    difference is load-bearing: a refusal is stored too, and a later attempt reuses it
    rather than deciding again. Without that, a publish that failed halfway and retried the
    next morning — with a newer site list, or a book that failed to load that night — could
    reach the opposite answer, write a different subject line, and leave the record holding
    two documents for one recording that it has no way to reconcile.
    """

    decided: bool = False
    name: str = ""              # "" when refused
    applied: bool = False       # written into the subject line and the heading
    site: str = ""              # the slug the record bound, "" when none
    span: str = ""              # the words in the transcript the name was taken from
    mentions: int = 0           # how often the site is named across the recording
    code: str = ""              # "ok" | "off" | "E1".."E4" | "N1".."N9"
    why: str = ""               # plain English, printed in the morning email
    book: str = ""              # which site list decided it
    #: Whether he announced the site at the top of the recording, rather than it being
    #: worked out from the rest of the conversation. The stronger of the two.
    declared: bool = False
    #: What he said the recording was for — SITE WALK, INSPECTION — from the opening only.
    activity: str = ""
    #: Where the record will actually file the note, which is not always where the
    #: recording says it belongs. Reported, never obeyed.
    filed: str = ""
    disagrees: bool = False

    def as_meta(self) -> dict[str, Any]:
        """The form stored on the ledger row. Small, flat and JSON-safe."""
        return {
            "decided": bool(self.decided),
            "name": self.name,
            "applied": bool(self.applied),
            "site": self.site,
            "span": self.span,
            "mentions": int(self.mentions),
            "code": self.code,
            "why": self.why,
            "book": self.book,
            "declared": bool(self.declared),
            "activity": self.activity,
            "filed": self.filed,
            "disagrees": bool(self.disagrees),
        }

    @classmethod
    def from_meta(cls, raw: Any) -> "NameDecision | None":
        """Read one back, or ``None`` when there is nothing usable stored.

        Tolerant on purpose: a row written by an older version, or by hand, must not stop a
        recording being published. Anything unreadable means "not decided yet", which
        decides again — the safe direction.
        """
        if not isinstance(raw, Mapping) or not raw.get("decided"):
            return None
        try:
            return cls(
                decided=True,
                name=str(raw.get("name") or ""),
                applied=bool(raw.get("applied")),
                site=str(raw.get("site") or ""),
                span=str(raw.get("span") or ""),
                mentions=int(raw.get("mentions") or 0),
                code=str(raw.get("code") or ""),
                why=str(raw.get("why") or ""),
                book=str(raw.get("book") or ""),
                declared=bool(raw.get("declared")),
                activity=str(raw.get("activity") or ""),
                filed=str(raw.get("filed") or ""),
                disagrees=bool(raw.get("disagrees")),
            )
        except (TypeError, ValueError):
            return None


#: What every failure returns, and what the pipeline uses when anything at all goes wrong.
#: Note ``decided=True``: a recording that could not be named is decided, so a retry does
#: not try again and reach a different answer.
NO_NAME = NameDecision(decided=True, code="off", why="naming is switched off")


def eligible(
    parsed: naming.ParsedName,
    extraction: Any,
    duration_s: float | None,
    *,
    min_seconds: int,
) -> tuple[bool, str, str]:
    """Whether this recording may be named at all. ``(ok, code, why)``.

    Four conditions, and the first is the important one: **the file still carries the voice
    recorder's own default name.** Anything else he named himself, and a service that
    renames what a person chose is a service he turns off. ``CJ.m4a``, ``Q.m4a``,
    ``JORDS.m4a``, ``Morne Interview.m4a`` all look nameless to a machine and are not: they
    are what he calls those people. The tempting rule — "there is no recognisable site in
    the name, so suggest one" — is wrong on every one of them.
    """
    if not is_recorder_default(parsed.stem):
        return False, "E1", "he named this one himself"

    if not parsed.timestamp_recovered:
        # The shape matched but the digits are not a real moment -- "Voice 260832_250000".
        # The recorder writes its own clock and cannot produce that, so this is something
        # else wearing the shape, and something else is a name a person chose.
        return False, "E1", "he named this one himself"

    if parsed.form != naming.FORM_FREE_TEXT or parsed.party is not None:
        # True by construction today. Asserted so that a future change to the parser cannot
        # widen what may be renamed without this line failing first.
        return False, "E2", "the filename names a party, so it is not an unnamed note"

    routing = getattr(extraction, "routing", None)
    if not bool(getattr(routing, "substantive", False)):
        return False, "E3", "there is not enough in it to say what it is about"

    if duration_s is None or duration_s < float(min_seconds):
        # The engine's own repetitions are indistinguishable from a site being named twice.
        # A short recording of wind noise comes back as "Canterbury Square. Thank you for
        # watching. Canterbury Square, thank you for watching" — which passes every
        # plausibility floor this service has, satisfies "mentioned twice", and satisfies
        # "mentioned early". The two conditions that look like evidence ARE the signature.
        # Length is the only cheap thing that separates them.
        shown = "no duration" if duration_s is None else f"{int(duration_s)}s"
        return False, "E4", f"too short to name from ({shown})"

    return True, "", ""


def decide(
    *,
    parsed: naming.ParsedName,
    extraction: Any,
    spoken: str,
    duration_s: float | None,
    book: sitebook.SiteBook,
    render: Callable[[str], str],
    apply: bool,
    min_seconds: int,
    opening_seconds: float = declaration.DEFAULT_WINDOW_S,
) -> NameDecision:
    """Work out what to call this recording, or refuse. **Never raises.**

    ``spoken`` is :func:`transcriber.outputs.spoken_body` — the published words, not the
    engine's prose. ``render`` takes a candidate name and returns the exact bytes the record
    will be handed, so N5 and N9 run against reality rather than against a model of it.
    """
    book_name = book.generated_at or ("none" if not book else "unknown")

    ok, code, why = eligible(parsed, extraction, duration_s, min_seconds=min_seconds)
    if not ok:
        return NameDecision(decided=True, code=code, why=why, book=book_name)

    if not book:
        return NameDecision(
            decided=True, code="N0", book=book_name,
            why=book.fault or "there is no site list, so nothing can be named",
        )

    # N1 — the model named a site at all.
    site = str(getattr(extraction, "site", "") or "").strip()
    if not site or len(site) < _MIN_NAME or len(site) > _MAX_NAME or not any(c.isalpha() for c in site):
        return NameDecision(decided=True, code="N1", book=book_name,
                            why="nothing in it says which site it is about")

    # N2 — not a placeholder.
    if site.strip().lower() in STOP_SITES:
        return NameDecision(decided=True, code="N2", book=book_name,
                            why=f"it only says {site!r}, which does not name anywhere")

    # N3 — those words appear in the PUBLISHED body, and the span is the body's own
    # characters. Not the model's spelling: it canonicalises "22 CHEPSTOW" to "22 Chepstow,
    # Sea Point", which is the record's title and not what he writes or what was said.
    match = _find_span(site, spoken)
    if match is None:
        return NameDecision(decided=True, code="N3", site="", book=book_name,
                            why=f"it says {site!r}, but not in those words, so there is "
                                f"nothing in the recording to take the name from")
    span = _normalise(spoken[match[0]:match[1]])

    # N4 — the span looks like a name.
    if not _NAME_SHAPE.fullmatch(span) or not (_MIN_NAME <= len(span) <= _MAX_NAME):
        return NameDecision(decided=True, code="N4", span=span, book=book_name,
                            why="the words it is named by do not read as a name")

    # N6 — the span names exactly one site the record knows about.
    #
    # This is the guard that stops an ordinary English word becoming a title. "House",
    # "North", "Green", "Forest" and "Beach" all appear in real site titles, and the record
    # discards any term it uses of more than two sites, so none of them discriminates
    # anything and none of them can name a recording.
    #
    # It is NOT a check that the record binds this document anywhere. That check used to be
    # here and it was wrong, in a way only an adversary found: the record scores a site by
    # how many DISTINCT vocabulary terms appear in a document, once each, never by how
    # often. A two-minute call about "Ashton Steelworks" carries three of its terms; an
    # hour standing in Eagle House carries one. So the record answered Ashton Steelworks,
    # this rule agreed with it, and titled an Eagle House walk after a phone call — while
    # refusing a model that answered Eagle House, which was the truth. Deferring to the
    # record made a misfile look deliberate. See :meth:`SiteBook.mentions_of_each`.
    named = book.sites_named_by(span)
    if not named:
        return NameDecision(decided=True, code="N6", span=span, book=book_name,
                            why=f"{span!r} is not a site the record knows about")
    if len(named) > 1:
        return NameDecision(decided=True, code="N6", span=span, book=book_name,
                            why=f"{span!r} could be more than one site")
    site_slug = next(iter(named))

    # N7 — and it has to be what the recording is ABOUT, not merely something said in it.
    # Two ways to earn that, and a recording needs only one.
    counts = book.mentions_of_each(spoken)
    mine = counts.get(site_slug, 0)
    rivals = {slug: n for slug, n in counts.items() if slug != site_slug}
    runner_up = max(rivals.values()) if rivals else 0

    window = declaration.opening(spoken, window_s=float(opening_seconds))
    declared_here = book.sites_named_by(window.text) if window else frozenset()
    declared = declared_here == {site_slug}

    if len(declared_here) == 1 and not declared:
        # He announced ONE site at the top and it was not this one. That is the recording
        # itself contradicting the answer, and it outranks any amount of counting.
        other = next(iter(declared_here))
        return NameDecision(decided=True, code="N7", site=site_slug, span=span,
                            mentions=mine, book=book_name,
                            why=f"the recording opens by saying it is "
                                f"{book.title_of(other)}, not {span}")

    # An opening naming two sites is not a contradiction, it is a busy first minute — he
    # finishes a call and then announces where he is. That falls through to the count
    # rather than refusing, which is the whole point of having a second path.
    if not declared:
        # Nothing was announced, so fall back to what the conversation is mostly about.
        # This is the second half of what he asked for: "if the name is not announced in the
        # beginning then it should try and infer from the conversation."
        if mine < _MIN_MENTIONS:
            return NameDecision(decided=True, code="N7", site=site_slug, span=span,
                                mentions=mine, book=book_name,
                                why=f"{span} is only mentioned once and is not announced at "
                                    f"the start, so it looks like something that came up "
                                    f"rather than where he was")
        if mine <= runner_up:
            other = max(rivals, key=lambda k: rivals[k])
            return NameDecision(decided=True, code="N7", site=site_slug, span=span,
                                mentions=mine, book=book_name,
                                why=f"the recording talks about {book.title_of(other)} as "
                                    f"much as {span}, so it does not say plainly enough "
                                    f"which one it is about")

    # N8 — the name survives being made into a name, with the activity he announced.
    activity = declaration.activity_in(window.text) if declared else ""
    candidate = " ".join(part for part in (span.upper(), activity) if part).strip()
    if (naming.safe_stem(candidate) != candidate or is_recorder_default(candidate)
            or len(candidate) > _MAX_NAME):
        candidate = span.upper()
        activity = ""
    if naming.safe_stem(candidate) != candidate or is_recorder_default(candidate):
        return NameDecision(decided=True, code="N8", site=site_slug, span=span,
                            mentions=mine, book=book_name,
                            why="the name would have to be changed to be usable, so it is "
                                "not what was said")

    # N9 — what the record will do with it, reported rather than obeyed.
    #
    # This used to refuse a name that changed the record's answer. That is now backwards:
    # the record's answer is the frequency-blind one, so a title that moves a filing toward
    # the site he announced is the title CORRECTING the record, which is the best thing
    # that can happen here. What is worth knowing is when the two disagree, because that
    # means this note is about to be filed somewhere he will not look for it — and a title
    # that visibly disagrees with the filing is far better than one that quietly
    # corroborates a wrong one.
    filed = ""
    try:
        filed, _scores = book.bind(render(candidate))
    except Exception:
        filed = ""
    disagrees = bool(filed) and filed != site_slug

    how = "you say so at the start" if declared else f"most of the recording is about it"
    why = f"{span} is what this one is about — {how}"
    if mine:
        why += f", named {mine} time{'s' if mine != 1 else ''}"
    if disagrees:
        why += (f". Worth knowing: the record will file it under "
                f"{book.title_of(filed)} rather than {book.title_of(site_slug)}")
    elif filed == site_slug:
        why += f", and the record files it there too"

    return NameDecision(
        decided=True, name=candidate, applied=bool(apply), site=site_slug, span=span,
        mentions=mine, code="ok", book=book_name,
        declared=declared, activity=activity, filed=filed or "", disagrees=disagrees,
        why=why,
    )


# --------------------------------------------------------------------------- span search


def _pattern(phrase: str) -> re.Pattern[str] | None:
    """A phrase as it would be SAID: its words, any whitespace between them, whole words.

    Whitespace-flexible because the published body breaks on segment boundaries, so a
    two-word name can have a newline in the middle of it. Word-bounded so that "Canterbury"
    does not match inside "Canterburys" and a digit-led name like "22 Chepstow" does not
    match inside "122 Chepstow".
    """
    tokens = [re.escape(t) for t in re.findall(r"[A-Za-z0-9]+", phrase or "")]
    if not tokens:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(tokens) + r"(?!\w)", re.IGNORECASE)


def _find_span(phrase: str, text: str) -> tuple[int, int] | None:
    pattern = _pattern(phrase)
    if pattern is None:
        return None
    found = pattern.search(text or "")
    return (found.start(), found.end()) if found else None


def _normalise(raw: str) -> str:
    """The span's own characters with its whitespace flattened — including a line break."""
    return " ".join((raw or "").split())
