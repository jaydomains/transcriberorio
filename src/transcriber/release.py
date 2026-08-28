"""Getting released words into the record — as a fourth file, because nothing here may edit one.

When a person reads a held passage and says it may be written down, the words have to reach
``kbc-site-memory``. There is no component in this service, and none downstream of it, that
can edit a site file the record has already built: the record's pages are assembled from its
own sources, and the only way in is to *be* a source. So a release does not go back and
change anything. It writes a **fourth markdown file** into the same OneDrive output folder as
the recording's three, named after the recording and the held passage it answers, and that
file is delivered, ingested and filed exactly like every other source document. The transcript
already in the record keeps its marker, and this file is what the marker was waiting for.

Five things about that are load-bearing, and each one is a way this could quietly fail:

**1. The release file is evidence, so it must be ingested as one.** The summary and the
proposals are named with a leading ``_`` precisely so the record's intake skips them; this
file is not, because its whole purpose is to be read. It carries a ``Subject:`` and a
``Date:`` and nothing else above the blank line, it is checked with the same
:func:`transcriber.outputs.check_contract` the three files are checked with, and it is
refused rather than uploaded if that check finds anything. A released passage filed as a
machine's commentary would be a release that released nothing.

**2. It answers a question without asking a new one.** The marker in the transcript is
phrased as a stated unknown so the record's question harvester carries it onto the site's
live page — *"what was said in held passage 4F2A11 on 24 Aug 2026, and may it be released
into the record?"*. If this file repeated that sentence, the harvester would lift it again
and the page would carry the same open question twice, one of them already answered. So the
release file names the reference and never repeats the question, and
:func:`transcriber.redact.harvestable` is asserted to find nothing in it before it is
written. A question genuinely asked *on the recording* is a different thing and is left
alone — that is the transcript doing its job.

**3. A refusal writes nothing, and is still recorded.** No file, no note in the record, no
change to the transcript: the marker stays exactly where it is and keeps saying that
something was said and is not written here. That is deliberate. The record's own rule is
that absence is itself a record, and a refused passage whose marker had been tidied away
would read, six months later, exactly like a recording where nothing was said. What *is*
written is the fact of the refusal — in the held-passage store, which keeps it forever, and
in the recording's ledger row, so ``transcriber status --item`` shows the whole history.

**4. Releasing one passage must not release another.** A recording can carry several holds,
and the stored context either side of one can overlap the words of another that is still
pending — or that was refused outright. So the release file carries the released words and
no surrounding context at all, and before it is written every other still-held passage of
that recording is searched for in it with :func:`transcriber.redact.contains_any_held`. If
one is found the file is not written and a person is told, which is the same trade
``outputs.refuse_held_text`` makes: never publish on a doubt, never withhold on one either.

**5. A decision that has been made is not lost because a drive was busy.** The person's
answer is written to the held-passage store first and the file is uploaded second, so the
two can never disagree about what was decided. An upload that fails leaves a released
passage with no file, which :func:`outstanding` finds by comparing the store against the
ledger — so the morning email can say so, and ``transcriber held deliver`` can finish the
job. Nothing here retries silently and nothing here decides anything: the only thing that
turns a held passage into a released one is a person, elsewhere, saying so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from . import naming, outputs, redact
from .logging_setup import get_logger
from .models import (
    DEFAULT_ROUTE,
    contains_email,
    day_of,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
    utc_now_iso,
)

# Imported rather than copied. The space between the date and the time in a ``Date:`` line is
# the word boundary the record's own date parser needs; an ISO ``T`` there files the recording
# on the first of the month instead. One copy of that rule, in the module that discovered it.
from .outputs import _header_date as header_date
from .review_page import display_name
from .withheld import (
    CATEGORY_PHRASE,
    MODE_ON,
    Decision,
    HeldRecord,
    WithheldStore,
)

log = get_logger(__name__)

__all__ = [
    "RELEASE_MARKER",
    "RELEASE_SUFFIX",
    "META_KEY",
    "ReleaseError",
    "NotReleased",
    "NowhereToWrite",
    "WouldLeakAnotherHold",
    "Rendered",
    "Delivery",
    "Outstanding",
    "release_name",
    "render_release",
    "answered_by_name",
    "recorded_answers",
    "outstanding",
    "Releaser",
]


#: What goes between the recording's own output stem and the reference, in the fourth file's
#: name. Readable in a OneDrive folder listing without opening anything, and unmistakable.
RELEASE_SUFFIX = "-released-"

#: The word the file's own body uses for itself, so a person grepping the record can find
#: every one of them.
RELEASE_MARKER = "Released into the record"

#: Where the recording's ledger row remembers what has been delivered and what was refused.
#: Counts, references, filenames and dates — never a word of what was held.
META_KEY = "gate_answers"


class ReleaseError(RuntimeError):
    """Anything that stops a released passage reaching the record. Never swallowed."""


class NotReleased(ReleaseError):
    """Asked to deliver a passage nobody has released."""


class NowhereToWrite(ReleaseError):
    """The recording's route no longer says where its files go."""


class WouldLeakAnotherHold(ReleaseError):
    """The file for one released passage still carries the words of another that is held.

    Carries the references so a person can see which two passages overlap, and never the
    words, because naming them here would put them in the log and in the ledger — which is
    the leak this refusal just prevented.
    """

    def __init__(self, message: str, *, released: str = "", refs: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.released = released
        self.refs = tuple(refs)


# --------------------------------------------------------------------------- the file


def release_name(transcript_name: str, ref: str) -> str:
    """The fourth file's name, derived from the transcript's own.

    Derived rather than rebuilt: the transcript's name already carries the timestamp prefix
    that sorts the folder, the safe stem, and the digest of the item id that makes two
    recordings with one filename impossible. Reusing it means the release file sorts next to
    the recording it belongs to and inherits every one of those guarantees, and the reference
    on the end is what keeps two releases from the same recording apart.

    It deliberately does **not** take the ``_`` prefix the summary and the proposals carry.
    That prefix is what makes the record's intake skip a file, and this one has to be read.
    """
    base = os.path.basename(str(transcript_name or "").strip())
    if not base:
        raise ReleaseError(
            "there is no transcript name to derive a release file's name from, so there is "
            "no way to say which recording these words belong to"
        )
    stem = base[:-3] if base.lower().endswith(".md") else base
    stem = stem.lstrip("_")
    tag = str(ref or "").strip().upper()
    if not tag:
        raise ReleaseError("a release file has to name the held passage it answers")
    return f"{stem}{RELEASE_SUFFIX}{tag}.md"


def answered_by_name(record: HeldRecord) -> str:
    """The person who answered, as this service is allowed to write them down.

    Reviewers are configured as addresses, and the house rule has no exceptions: this
    service never types an email address into anything. The local part is who they are to
    anybody reading the file in the first place.
    """
    return display_name(record.answered_by) or "a person"


def _when(record: HeldRecord) -> tuple[datetime, str]:
    """When to date the file, and in plain words where that came from.

    The recording's own moment where we have one, because that is where these words belong
    in the record — the same reasoning ``naming.resolve_timestamp`` uses. Falling back to
    the day it was held, and then to the day it was released, and saying which.
    """
    for stamp, why in (
        (record.recorded_at, "when the recording was made"),
        (record.held_at, "when the passage was held; the recording's own time was not recorded"),
        (record.decided_at, "when the passage was released; nothing earlier was recorded"),
    ):
        moment = naming.parse_graph_datetime(stamp)
        if moment is not None:
            return moment.astimezone(naming.SAST), why
    raise ReleaseError(
        f"held passage {record.ref} carries no usable date of any kind, so a release file "
        "for it would be filed under a day it did not happen on"
    )


def _label(record: HeldRecord) -> str:
    """How this recording is named to a person: the party from the filename, or the site."""
    parsed = naming.parse_source_name(record.source_name or "")
    if parsed.party:
        return f"Call with {parsed.party}"
    site = (record.site or "").strip()
    if site:
        return site
    return parsed.stem or record.source_name or "Voice note"


def _one_line(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _scrub(text: str) -> tuple[str, list[str]]:
    """Every spelling of an address out, and a note saying one was taken out.

    Visible, never silent: a reader who can see that something was removed can ask what it
    was. The held words are the recording's own and are not otherwise touched — this is the
    one rule that outranks verbatim, and it is the same trade ``outputs._scrub`` makes in
    the transcript itself.
    """
    notes: list[str] = []
    out = str(text or "")
    if contains_email(out):
        notes.append(
            "an email address was removed from the words below. This service never writes "
            "one down; everything else here is exactly as it was said"
        )
        out = strip_emails(out)
    stripped = strip_dictated_emails(out)
    if stripped != out:
        notes.append(
            "an email address said out loud rather than spelled out was removed from the "
            "words below. This service never writes one down in any spelling; everything "
            "else here is exactly as it was said"
        )
        out = stripped
    cleaned = strip_owner_paths(out)
    if cleaned != out:
        notes.append(
            "a OneDrive path carrying an account owner's address was removed from the words "
            "below"
        )
        out = cleaned
    return out, notes


@dataclass(frozen=True)
class Rendered:
    """One release file, finished and checked, before anything touches the network."""

    name: str
    text: str
    ref: str
    hold_id: str
    item_id: str
    transcript_name: str

    @property
    def data(self) -> bytes:
        return self.text.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.data)


def render_release(
    record: HeldRecord,
    *,
    transcript_name: str,
    still_held: Sequence[Any] = (),
    source_name: str = "",
) -> Rendered:
    """The fourth file for one released passage: its name and its exact bytes.

    Pure — no clock, no network, no store — so the whole shape of it is provable offline,
    which is what ``transcriber selftest`` does with it. Everything that could stop it being
    written is decided here, before an upload can leave a half-answered recording behind.

    ``still_held`` is every other passage of the same recording that is *not* released:
    pending ones and refused ones alike. They are searched for in the finished text, and one
    being found refuses the file rather than shrinking it, because a release file that had
    quietly dropped part of what it was releasing would be the worst of both.
    """
    if record.decision != Decision.RELEASED:
        raise NotReleased(
            f"held passage {record.ref} is {record.decision}, not released. Nothing writes a "
            "release file for a passage a person has not released — that is the whole gate"
        )
    if record.mode != MODE_ON:
        raise NotReleased(
            f"held passage {record.ref} was recorded while the gate was in shadow, so nothing "
            "was ever withheld and there is nothing to put back"
        )
    words = str(record.text or "").strip()
    if not words:
        raise ReleaseError(
            f"held passage {record.ref} has no words stored against it, so there is nothing "
            "to release; the store is the only copy of them outside the audio"
        )

    name = release_name(transcript_name, record.ref)
    problems = outputs.check_name(name)
    if problems:
        raise ReleaseError(
            f"a release file for {record.ref} would be written under a name this service may "
            "not write: " + "; ".join(problems)
        )

    when, date_note = _when(record)
    label = _label(record)
    recording = source_name or record.source_name or record.item_id
    who = answered_by_name(record)
    body_words, notes = _scrub(words)

    subject = _one_line(f"{RELEASE_MARKER}: held passage {record.ref} — {label}", 90)

    lines: list[str] = [
        f"# {RELEASE_MARKER}: {label}",
        "",
        "A passage of this recording was held back when it was transcribed, and a person has",
        "since read it and said it may be written down. The words are below, exactly as they",
        "were said. Nothing else about the recording has changed: the transcript keeps the",
        "marker where this was said, because nothing in this service edits a file the record",
        "has already taken. This file is how the words arrive.",
        "",
        "It states no status, closes nothing and settles nothing about the job. A person",
        "agreed that these words may be written down; that is a permission, not a finding.",
        "`observed_by: agent`.",
        "",
    ]

    rows: list[tuple[str, str]] = [
        ("Recording", recording),
        ("Transcript this belongs to", f"`{transcript_name}`"),
        ("Held passage", record.ref),
        ("What was held", record.phrase),
        # The category's short phrase, never its long description. The long one carries a
        # worked example with rand figures in it — "we raised R1.65m and we'll land at
        # R1.604m" — which is fine in a review page and wrong here: this file goes into the
        # record as evidence, and an illustration sitting in it reads like something that
        # was said on the call.
        ("Why it was held", CATEGORY_PHRASE.get(record.category, record.category)),
        ("Held on", day_of(record.held_at) or "not recorded"),
        ("Released by", f"{who} on {day_of(record.decided_at) or 'an unrecorded date'}"),
        ("Dated", f"{when.strftime('%Y-%m-%d')} — {date_note}"),
    ]
    if record.site:
        rows.insert(1, ("Site", record.site))
    if record.route and record.route != DEFAULT_ROUTE:
        rows.append(("Route", record.route))
    rows.append(("OneDrive item", record.item_id or "not recorded"))
    rows.append(("observed_by", "agent"))
    lines += [f"- {key}: {' '.join(str(value).split())}" for key, value in rows if str(value).strip()]
    for note in notes:
        lines.append(f"- Note for a person: {note}")

    lines += ["", "## What was said", ""]
    speaker = (record.speaker or "").strip()
    prefix = f"{speaker}: " if speaker else ""
    for chunk in body_words.split("\n"):
        lines.append(f"> {prefix}{chunk}" if chunk.strip() else ">")
        prefix = ""

    lines += [
        "",
        "---",
        "",
        f"This is the answer to the passage marked `[held {record.ref}]` in",
        f"`{transcript_name}`.",
        "",
        "The marker stays where it is. It is the record of when these words were said and",
        "of the fact that they were held for a person to look at, and nothing in this",
        "service edits a file the record has already taken.",
    ]

    body = "\n".join(lines).rstrip() + "\n"
    text = f"Subject: {subject}\nDate: {header_date(when)}\n\n{body}"

    contract = outputs.check_contract(
        text,
        expected_body=body,
        expected_subject=subject,
        expected_date=when.strftime("%Y-%m-%d"),
    )
    if contract:
        raise ReleaseError(
            f"a release file for {record.ref} would be mis-read by the record: "
            + "; ".join(contract)
        )

    # The marker's question, repeated here, would be harvested a second time and the site's
    # live page would carry the same open question twice — one of them already answered.
    # Asserted rather than assumed, because the wording above is a sentence somebody will
    # reasonably want to improve one day.
    echo = redact.harvestable(text)
    if echo:
        raise ReleaseError(
            f"a release file for {record.ref} repeats the marker's own question "
            f"({echo[:60]!r}), so the record would file it as a second open question about "
            "words that have just been released"
        )

    leaks = redact.held_words_in(text, redact.spans_of(still_held))
    if leaks:
        refs = [str(getattr(span, "ref", "?")) for span, _, _ in leaks]
        raise WouldLeakAnotherHold(
            f"the release file for {record.ref} still carries words from "
            f"{'passages' if len(refs) > 1 else 'passage'} {', '.join(sorted(set(refs)))}, "
            "which nobody has released. Nothing was written. The two passages overlap in the "
            "transcript, so releasing this one needs the other answered as well",
            released=record.ref,
            refs=sorted(set(refs)),
        )

    return Rendered(
        name=name,
        text=text,
        ref=record.ref,
        hold_id=record.hold_id,
        item_id=record.item_id,
        transcript_name=transcript_name,
    )


# --------------------------------------------------------------------------- what happened


@dataclass(frozen=True)
class Delivery:
    """What happened to one answer. Never a word of what was said, in any field."""

    ok: bool
    ref: str
    hold_id: str
    item_id: str
    decision: str
    #: ``written`` | ``nothing-to-write`` | ``already-there`` | ``failed`` | ``refused-to-write``
    state: str
    detail: str = ""
    name: str = ""
    drive_item_id: str = ""
    at: str = ""

    @property
    def wrote_a_file(self) -> bool:
        return self.state in ("written", "already-there")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ref": self.ref,
            "item_id": self.item_id,
            "decision": self.decision,
            "state": self.state,
            "detail": self.detail,
            "file": self.name,
            "at": self.at,
        }

    def line(self) -> str:
        """One sentence a person can read, for the terminal and for the morning email."""
        if self.state == "written":
            return f"{self.ref}: released, and written into the record as {self.name}"
        if self.state == "already-there":
            return f"{self.ref}: released, and already written into the record as {self.name}"
        if self.state == "nothing-to-write":
            if self.decision != Decision.REFUSED:
                return f"{self.ref}: {self.detail}"
            return (
                f"{self.ref}: refused. Nothing was written, and the marker stays in the "
                f"transcript — the record keeps saying that something was said here"
            )
        if self.state == "refused-to-write":
            return f"{self.ref}: NOT written — {self.detail}"
        return f"{self.ref}: released, but NOT yet written into the record — {self.detail}"


@dataclass(frozen=True)
class Outstanding:
    """Released passages whose words have not reached the record yet.

    Counts, references and sites. No words: this is built so the morning email and
    ``transcriber gate --status`` can say that something is unfinished without either of
    them holding a held passage in order to say it.
    """

    count: int = 0
    refs: tuple[str, ...] = ()
    sites: tuple[str, ...] = ()
    recordings: int = 0
    oldest_at: str = ""
    problems: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return self.count > 0 or bool(self.problems)

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.count:
            out.append(
                f"{self.count} released passage(s) across {self.recordings} recording(s) have "
                f"been approved but their words have not been written into the record yet: "
                f"{', '.join(self.refs[:8])}"
                + (" and more" if len(self.refs) > 8 else "")
            )
            if self.sites:
                out.append("Sites: " + ", ".join(self.sites))
            out.append("Run `transcriber held deliver` to finish it. Nothing is lost meanwhile.")
        out.extend(self.problems)
        return out


# --------------------------------------------------------------------------- the ledger note


def recorded_answers(ledger: Any, item_id: str) -> dict[str, Any]:
    """What this recording's row already says about answered passages. Never raises."""
    try:
        row = ledger.get(item_id)
    except Exception as exc:  # noqa: BLE001 - a sick ledger must not lose a decision
        log.warning("release-ledger-unreadable", "could not read the recording's row",
                    item=item_id, error=str(exc))
        return {}
    if row is None:
        return {}
    stored = (getattr(row, "meta", None) or {}).get(META_KEY)
    return dict(stored) if isinstance(stored, Mapping) else {}


def _remember(ledger: Any, item_id: str, ref: str, entry: Mapping[str, Any]) -> None:
    """Write one answer into the recording's row, keeping everything already there.

    Read, merge, write: :meth:`Ledger.set_fields` replaces the whole ``meta`` blob, and the
    pipeline keeps its own notes in there. This only ever runs long after the pipeline has
    finished with the recording — a passage cannot be answered before it has been
    published — so the merge is not racing anything, and it is written that way rather than
    assumed.
    """
    try:
        row = ledger.get(item_id)
        if row is None:
            log.warning(
                "release-no-row",
                "a passage was answered for a recording the ledger has no row for",
                item=item_id, ref=ref,
            )
            return
        meta = dict(getattr(row, "meta", None) or {})
        answers = dict(meta.get(META_KEY) or {}) if isinstance(meta.get(META_KEY), Mapping) else {}
        answers[str(ref)] = dict(entry)
        meta[META_KEY] = answers
        ledger.set_fields(item_id, meta=meta)
    except Exception as exc:  # noqa: BLE001 - the decision is already durable in the store
        log.error(
            "release-note-not-written",
            "the answer is recorded in the held-passage store, but the recording's own row "
            "could not be updated; the delivery will look outstanding until it is",
            item=item_id, ref=ref, error=str(exc),
        )


# --------------------------------------------------------------------------- the work


def outstanding(store: WithheldStore, ledger: Any) -> Outstanding:
    """Every released passage whose words are not in the record yet, without the words.

    Built from the store's own overview — counts and reviewers, no text — and then one
    reviewer's queue at a time, each record reduced to :meth:`HeldRecord.without_words`
    before anything leaves this function. That is decision 6 kept structural in the one
    place that has to walk every released passage: reading them in order to count them is
    unavoidable, showing them is not.

    A released passage with no reviewer against it cannot be listed this way — the store
    refuses to answer a queue for "everybody", deliberately — so it is reported as a problem
    with the reference a person can pass to ``transcriber held deliver --ref``, rather than
    quietly left out of the count.
    """
    problems: list[str] = []
    try:
        overview = store.overview(decision=Decision.RELEASED)
    except Exception as exc:  # noqa: BLE001 - never take the morning email down
        return Outstanding(problems=(
            f"the held-passage store could not be read, so it is not known whether any "
            f"released words are still waiting to be written into the record: {exc}",
        ))

    refs: list[str] = []
    sites: list[str] = []
    items: set[str] = set()
    oldest = ""
    for reviewer, count in sorted((overview.get("by_reviewer") or {}).items()):
        if not count:
            continue
        if not reviewer or reviewer == "unassigned":
            problems.append(
                f"{count} released passage(s) have no reviewer recorded against them and "
                f"cannot be listed here. Find them with `transcriber gate --status` and "
                f"deliver each with `transcriber held deliver --ref <reference>`."
            )
            continue
        try:
            records = store.queue_for(reviewer, decision=Decision.RELEASED)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"one reviewer's released passages could not be read: {exc}")
            continue
        for full in records:
            record = full.without_words()
            if str(record.ref) in recorded_answers(ledger, record.item_id):
                continue
            refs.append(record.ref)
            items.add(record.item_id)
            if record.site and record.site not in sites:
                sites.append(record.site)
            if not oldest or (record.decided_at and record.decided_at < oldest):
                oldest = record.decided_at
    return Outstanding(
        count=len(refs),
        refs=tuple(sorted(refs)),
        sites=tuple(sorted(sites)),
        recordings=len(items),
        oldest_at=oldest,
        problems=tuple(problems),
    )


class Releaser:
    """Writes the fourth file, and knows which ones are still owed.

    Deliberately takes the drive client as an argument that may be ``None``: everything this
    class knows about *what is owed* is answerable from the store and the ledger alone, so
    the morning email and ``gate --status`` can ask it without a credential, and only an
    actual delivery needs the network.
    """

    def __init__(
        self,
        config: Any,
        ledger: Any,
        store: WithheldStore,
        graph: Any = None,
        *,
        now: Any = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.store = store
        self.graph = graph
        self._now = now if callable(now) else utc_now_iso
        self._outputs = {
            str(getattr(route, "name", "")): str(getattr(route, "output_folder_id", "") or "")
            for route in (getattr(config, "routes", ()) or ())
        }

    # -- where it goes -------------------------------------------------------------

    def output_folder_for(self, route: str) -> str:
        """The folder this recording's files went to, and only that one.

        The route's own folder, never a service-wide default and never the first route's: a
        release file landing in another route's folder would be filed against the wrong site
        by the record, which is a worse answer than not filing it at all.
        """
        folder = self._outputs.get(str(route or DEFAULT_ROUTE), "")
        if folder:
            return folder
        if len(self._outputs) == 1 and (route or DEFAULT_ROUTE) == DEFAULT_ROUTE:
            return next(iter(self._outputs.values()))
        raise NowhereToWrite(
            f"the route {route!r} is not in the configuration any more, so there is nowhere "
            f"to write this recording's released words. Put the route back with "
            f"`transcriber routes`, or run `transcriber held deliver` once it is. Nothing "
            f"was written, the words are still in the held-passage store, and the decision "
            f"stands."
        )

    # -- one answer ----------------------------------------------------------------

    def on_decision(self, record: HeldRecord) -> Delivery:
        """What the review page calls the moment an answer is written. Never raises.

        The page's job ends when the decision is stored; this is what happens next, and a
        failure here must not look like a failure to record the answer — the answer *is*
        recorded, and :func:`outstanding` will find the file that is still owed.
        """
        return self.deliver_quietly(record)

    def deliver(self, record: HeldRecord, *, force: bool = False) -> Delivery:
        """Write the fourth file for one released passage, or say plainly why not.

        A refusal takes the other branch: nothing is written, the marker stays, and the fact
        of it is recorded against the recording.
        """
        stamp = self._now()
        if record.decision == Decision.REFUSED:
            return self._refusal(record, stamp)
        if record.decision == Decision.NOT_WITHHELD:
            return Delivery(
                ok=True, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
                decision=record.decision, state="nothing-to-write",
                detail=(
                    f"{record.ref} was recorded while the gate was in shadow, so nothing was "
                    f"withheld and the transcript already carries these words"
                ),
                at=stamp,
            )
        if record.decision != Decision.RELEASED:
            return Delivery(
                ok=False, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
                decision=record.decision, state="refused-to-write",
                detail=(
                    f"nobody has answered {record.ref}, so there is nothing to write. A held "
                    f"passage is released because a person read it and said so"
                ),
                at=stamp,
            )

        known = recorded_answers(self.ledger, record.item_id).get(record.ref)
        if known and not force and str(known.get("file") or ""):
            return Delivery(
                ok=True, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
                decision=record.decision, state="already-there",
                name=str(known.get("file") or ""), drive_item_id=str(known.get("item") or ""),
                at=str(known.get("at") or stamp),
            )

        row = self.ledger.get(record.item_id)
        if row is None:
            raise ReleaseError(
                f"there is no ledger row for the recording {record.item_id!r} that held "
                f"{record.ref}, so it is not known which transcript these words belong to "
                "or where it was written"
            )
        transcript_name = str(getattr(row, "transcript_name", "") or "")
        if not transcript_name:
            raise ReleaseError(
                f"the recording that held {record.ref} has no transcript filed against it "
                f"yet, so there is nothing for a release file to answer. Nothing was written."
            )
        parent = self.output_folder_for(str(getattr(row, "route", "") or DEFAULT_ROUTE))

        still_held = tuple(
            other.as_span()
            for other in self.store.for_recording(record.item_id, include_shadow=False)
            if other.hold_id != record.hold_id and other.decision != Decision.RELEASED
        )
        rendered = render_release(
            record,
            transcript_name=transcript_name,
            still_held=still_held,
            source_name=str(getattr(row, "name", "") or record.source_name),
        )

        if self.graph is None:
            return Delivery(
                ok=False, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
                decision=record.decision, state="failed", name=rendered.name,
                detail=(
                    "this command has no connection to OneDrive, so the file was prepared "
                    "and not sent. Run `transcriber held deliver` on the service host."
                ),
                at=stamp,
            )

        item = self.graph.upload(parent, rendered.name, rendered.data)
        drive_id = str(getattr(item, "id", "") or "")
        if not drive_id:
            raise ReleaseError(
                f"OneDrive accepted {rendered.name!r} but returned no item id, so there is "
                "nothing to read back and nothing to prove the words are there"
            )
        # The same read-back the three files get, and for the same reason: an upload that
        # returned 200 and a file that is actually in the folder are two different claims,
        # and this service exists because the difference went unnoticed for months.
        back = self.graph.get_item(drive_id)
        name_back = str(getattr(back, "name", "") or "")
        size_back = int(getattr(back, "size", 0) or 0)
        if name_back != rendered.name or size_back != rendered.size:
            raise ReleaseError(
                f"the release file for {record.ref} was uploaded as {rendered.name!r} "
                f"({rendered.size} bytes) and read back as {name_back!r} ({size_back} "
                f"bytes); it is not confirmed in the folder, so it is not recorded as done"
            )

        _remember(
            self.ledger,
            record.item_id,
            record.ref,
            {
                "decision": Decision.RELEASED,
                "file": rendered.name,
                "item": drive_id,
                "at": stamp,
                "by": answered_by_name(record),
            },
        )
        log.info(
            "release-written",
            "released words were written into the record as their own file",
            ref=record.ref, item=record.item_id, file=rendered.name,
        )
        return Delivery(
            ok=True, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
            decision=record.decision, state="written", name=rendered.name,
            drive_item_id=drive_id, at=stamp,
        )

    def _refusal(self, record: HeldRecord, stamp: str) -> Delivery:
        """A refusal: nothing written anywhere, and the fact of it kept forever.

        The marker in the transcript is left exactly as it is. The record's rule is that
        absence is itself a record, and a marker tidied away on a refusal would read like a
        recording where nothing was said — which is the opposite of the truth.
        """
        _remember(
            self.ledger,
            record.item_id,
            record.ref,
            {
                "decision": Decision.REFUSED,
                "file": "",
                "at": stamp,
                "by": answered_by_name(record),
                "note": "nothing was written; the marker stays in the transcript",
            },
        )
        log.info(
            "release-refused",
            "a held passage was refused, so nothing was written and the marker stays",
            ref=record.ref, item=record.item_id,
        )
        return Delivery(
            ok=True, ref=record.ref, hold_id=record.hold_id, item_id=record.item_id,
            decision=Decision.REFUSED, state="nothing-to-write", at=stamp,
        )

    # -- the ones still owed --------------------------------------------------------

    def outstanding(self) -> Outstanding:
        """Released passages whose words are not in the record yet. No words in the answer."""
        return outstanding(self.store, self.ledger)

    def deliver_ref(self, ref: str, *, force: bool = False) -> tuple[Delivery, ...]:
        """Deliver by the six-character reference a person reads off a page or an email.

        A reference nobody has ever heard of raises — that is a person's typing mistake and
        it must be said out loud. A drive that would not take the file does **not**: it comes
        back as a :class:`Delivery` saying so, because the decision is already recorded and
        what a person needs at that point is a sentence and a retry, not a traceback.
        """
        records = self.store.by_ref(ref)
        if not records:
            raise ReleaseError(
                f"there is no held passage with the reference {str(ref).strip().upper()!r}. "
                "References are six characters and are printed beside the marker in the "
                "transcript, on the review page and in the morning email."
            )
        return tuple(self.deliver_quietly(record, force=force) for record in records)

    def deliver_quietly(self, record: HeldRecord, *, force: bool = False) -> Delivery:
        """:meth:`deliver`, with every failure turned into a :class:`Delivery` that says so.

        This is what everything outside this module calls. A person's answer is durable
        before this runs and stays durable whatever happens in it, so the only question left
        is how the failure is reported — and reporting it as an exception in the middle of a
        terminal session, or through the review page's own error path, would make a recorded
        decision look like a lost one.
        """
        try:
            return self.deliver(record, force=force)
        except Exception as exc:  # noqa: BLE001 - the decision stands whatever this does
            log.error(
                "release-delivery-failed",
                "a person's answer is recorded, but the words have not reached the record",
                ref=record.ref, item=record.item_id, error=str(exc),
            )
            return Delivery(
                ok=False,
                ref=record.ref,
                hold_id=record.hold_id,
                item_id=record.item_id,
                decision=record.decision,
                state="failed",
                detail=f"{type(exc).__name__}: {exc}",
                at=self._now(),
            )

    def deliver_outstanding(self, *, limit: int | None = None) -> tuple[Delivery, ...]:
        """Finish every release whose file is still owed, oldest reference first.

        Retryable by construction and safe to run twice: a delivery already recorded is
        answered ``already-there`` without touching the drive, and an upload replaces by
        name rather than adding a second copy.
        """
        owed = self.outstanding()
        out: list[Delivery] = []
        for ref in owed.refs:
            if limit is not None and len(out) >= limit:
                break
            try:
                out.extend(self.deliver_ref(ref))
            except Exception as exc:  # noqa: BLE001 - one bad reference must not stop the rest
                log.error("release-delivery-failed", "one owed release could not be written",
                          ref=ref, error=str(exc))
                out.append(
                    Delivery(
                        ok=False, ref=ref, hold_id="", item_id="", decision=Decision.RELEASED,
                        state="failed", detail=f"{type(exc).__name__}: {exc}", at=self._now(),
                    )
                )
        return tuple(out)
