"""What he says in the first minute, which is him telling us what the recording is.

    *"I normally record what site it is for and what we meeting for at the beginning of the
    recording. So for example, I get to site A, beginning of the recording I say this is a
    site walk of site A, general inspection, or meeting with trustees etc. — or at least I
    try to remember to say that."*

This changes what counts as evidence, and it corrects a rule that had it backwards.

The first rule written here asked for a site to be named **repeatedly and spread across the
recording**, on the reasoning that a site mentioned throughout is where he was standing and
a site mentioned in one patch is a phone call that came up. That reasoning is sound for the
phone call and wrong for everything else, because it rejects exactly the recordings he takes
most care over: he states the site once, on purpose, in the first sentence, and then never
says it again — nobody standing in a building keeps saying its name. Measured against a
transcript of that shape, the sustained rule refused it with *"only mentioned once, so it
looks like something that came up rather than where he was."* It was reading his clearest
possible declaration as his weakest possible evidence.

So there are two ways to earn a name now, and a recording needs only one:

**Declared.** The site is named inside the opening window, and no *other* site is named
there. This is him saying what the recording is, and it is the strongest evidence available
— stronger than any amount of repetition, because it is deliberate.

**Sustained.** The old rule, kept for the recording where he forgot: named twice or more,
early, and spread across the recording.

The opening window keeps the phone-call protection intact rather than weakening it. A call
about another site taken at minute twelve is nowhere near the first minute, so it can never
be *declared*; and two sites named in the opening — *"just finishing a call about
Canterbury… right, this is a walk of Beach Court"* — is ambiguity, and ambiguity is a
refusal.

**The activity comes from here and from nowhere else.** His own filenames carry it —
``BEACH COURT SITE WALK 270826``, ``CANTERBURY SNAG WALK 14 AUGUST``, ``22 CHEPSTOW SITE
INSPECTION 2408`` — so it is part of the structure he asked to have stuck to. But scanning
the *whole* recording for an activity word attributes events the recording contradicts:
*"it has opened up since the last inspection"* is a past event somewhere else, and *"the
site meeting for Canterbury moves to next Wednesday"* is a meeting that has not happened
yet. Both would have produced a confident, wrong title. Taken only from the opening
declaration, the word is a statement about *this* recording, which is the only place it can
be trusted. When the opening names none, the title is the site alone, which is a shape he
writes too (``CANTERBURY 6 AUGUST``); when it names more than one, the first wins, because
that is how the sentence is built — see :func:`activity_in`, which also says why this one
field is allowed to decide where everything else here refuses.

Also worth stating plainly: **none of this is required.** *"Or at least I try to remember to
say that"* is the operative half of what he said. A recording with no declaration falls
straight through to the sustained rule, and a recording that satisfies neither gets no name
and is published on time under the name it arrived with, exactly as before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DEFAULT_WINDOW_S",
    "ACTIVITIES",
    "Opening",
    "opening",
    "activity_in",
]

#: How much of the recording counts as "the beginning". Sixty seconds is long enough for
#: him to find the words — *"right… okay… so this is a site walk of Beach Court"* — and
#: short enough that a call taken a few minutes in is nowhere near it.
DEFAULT_WINDOW_S = 60.0

#: When the engine returned no timings there is no clock to window by, so a character
#: budget stands in. Roughly a minute of speech at a normal pace.
_WINDOW_CHARS = 900

#: The ``[MM:SS]`` or ``[HH:MM:SS]`` a rendered segment line opens with.
_STAMP_RE = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]")

#: The activity phrases he actually uses, grouped so that one thing said two ways is not
#: read as two things. Longest phrase in a family wins, and the text emitted is the phrase
#: **as it was matched**, never a canonical spelling of it — a word that was not said must
#: never appear in a title, and a title must not introduce a term the transcript lacks.
ACTIVITIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("walk", ("snag walk", "site walk", "walk around", "walkabout", "walk")),
    ("inspection", ("site inspection", "inspection")),
    ("meeting", ("progress meeting", "site meeting", "meeting")),
    ("visit", ("site visit",)),
    ("handover", ("handover",)),
    ("interview", ("interview",)),
    # NOT "snag list": that is the document he is walking through, not what the recording
    # is. "CANTERBURY SNAG LIST" reads as though the file IS a snag list, and it is a
    # recording of a walk. He writes "CANTERBURY SNAG WALK", which the walk family catches.
    ("snag", ("snagging",)),
)


@dataclass(frozen=True)
class Opening:
    """The first minute of the recording, and what it says."""

    #: The published text of the opening window. Empty when there is nothing to read.
    text: str = ""
    #: Where the window ends in the body, so a caller can say whether a hit is inside it.
    end: int = 0
    #: Whether the window was cut by the clock rather than by a character budget. Only
    #: informational — both are honest, one is just more precise.
    timed: bool = False

    def __bool__(self) -> bool:
        return bool(self.text)


def opening(spoken: str, *, window_s: float = DEFAULT_WINDOW_S) -> Opening:
    """The opening of the published body, by the clock where there is one.

    ``spoken`` is :func:`transcriber.outputs.spoken_body` — the words exactly as they are
    written to the file. Windowed from the ``[MM:SS]`` stamps the rendered segment lines
    already carry, so this reads the published bytes like everything else here and needs no
    second source of truth.

    A recording with no segment timings falls back to a character budget. That is less
    precise and it is stated rather than hidden: an engine that returns no timings gets a
    rougher window, not a wrong one.
    """
    text = spoken or ""
    if not text.strip():
        return Opening()

    lines = text.split("\n")
    cut = 0            # characters consumed so far, including the newlines between lines
    kept: list[str] = []
    timed = False

    for line in lines:
        stamp = _STAMP_RE.match(line)
        if stamp is not None:
            timed = True
            hours = int(stamp.group(1) or 0)
            seconds = hours * 3600 + int(stamp.group(2)) * 60 + int(stamp.group(3))
            if kept and seconds > window_s:
                # Past the window. The line that crosses it is left out whole rather than
                # cut mid-sentence: half a sentence is a good way to read half a site name.
                break
        elif kept and cut >= _WINDOW_CHARS:
            break
        kept.append(line)
        cut += len(line) + 1

    if not timed:
        # No clock anywhere. Take the character budget from the front, ending on a line
        # boundary for the same reason.
        kept, cut = [], 0
        for line in lines:
            if kept and cut >= _WINDOW_CHARS:
                break
            kept.append(line)
            cut += len(line) + 1

    window = "\n".join(kept)
    return Opening(text=window, end=len(window), timed=timed)


def activity_in(window: str) -> str:
    """What he says the recording is *for*, from the opening, or ``""``.

    Returns the phrase as it appears in the recording, upper-cased — never a canonical
    spelling. *"a general inspection of the roof"* yields ``INSPECTION``, because that is
    the phrase that was matched and it is genuinely in the transcript; it does not yield
    ``SITE INSPECTION``, which would put two words in a title when only one was said.

    **When the opening names more than one, the first one wins**, and his own example is
    why: *"this is a site walk of site A, general inspection"* names two, and the two are
    not competing — the first says what the visit is and the rest qualifies it. That is
    simply how the sentence is built, and a rule that refused it would drop the activity
    from his most typical opening of all. So position decides, and the answer for that
    sentence is ``SITE WALK``, which is exactly what he types.

    **The stakes here are deliberately low, and that is what justifies deciding at all.**
    Everywhere else in naming, ambiguity is a refusal, because the thing at risk is which
    site a note is filed under and a wrong answer is a misfile nobody ever notices. This
    field cannot do that: the site is chosen before this is asked, the record's filing does
    not depend on it, and the worst case is a note that says ``INSPECTION`` where he would
    have written ``SITE WALK``. That is a cosmetic difference on a correctly filed note,
    and it is worth taking a fair guess at. A wrong site never is.

    Empty when the opening names no kind of activity at all — most of them — and the title
    is then the site alone, which is also a shape he writes: ``CANTERBURY 6 AUGUST``.
    """
    lowered = (window or "").lower()
    if not lowered.strip():
        return ""

    best: tuple[int, str] | None = None
    for _family, phrases in ACTIVITIES:
        for phrase in phrases:            # longest phrase in a family first, by construction
            match = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", lowered)
            if match is not None:
                # The matched text, not the phrase: the title must never carry a word the
                # transcript does not, and this is the one place a canonical spelling could
                # sneak one in.
                said = window[match.start():match.end()]
                if best is None or match.start() < best[0]:
                    best = (match.start(), said)
                break

    if best is None:
        return ""
    return " ".join(best[1].split()).upper()
