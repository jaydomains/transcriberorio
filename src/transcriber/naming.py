"""Filenames: reading his, and writing ours.

Two jobs, and the first one is where recordings get lost.

**Reading his.** His phone writes ``Call <Party>_<YYMMDD>_<HHMMSS>.m4a``, and an older
handset wrote ``Call recording <Party>_...``. But a site meeting is named by hand — *BEACH
COURT SITE WALK 270826.m4a* — and carries no timestamp at all. Site meetings are an eighth
of what he loses, so any parser that assumes the ``Call`` shape is blind to exactly the
files that need it most. Every name parses here: the two call forms yield a party and a
timestamp, and everything else is a first-class result with ``timestamp=None``, for the
caller to fall back to the item's created time.

There is a third machine-written form, and missing it cost every unnamed recording its
real date: the recorder's own default, ``Voice 260806_162219.m4a``. The digits are the same
``YYMMDD_HHMMSS`` the call form writes, but a space stands where the call form has an
underscore, so it fell through to the hand-typed branch and was dated by when OneDrive
finished receiving it — hours late for a walk uploaded on the drive home, and across
midnight often enough to file it on a day it did not happen. It is read now, and only for
that exact anchored shape.

The trailing digits on a hand-typed name are *not* read as a timestamp. ``270826`` on that
site walk is 27 August written the way a person writes it; read as ``YYMMDD`` it would be
2027, and the recording would file itself a year into the future where nobody would look
for it. A timestamp is taken only from the structured ``_YYMMDD_HHMMSS`` tail, which is
machine-written and unambiguous.

**Writing ours.** ``<YYYYMMDD-HHMMSS>-<stem>.md``. The stamp prefix is what stops two
files landing in the same second from colliding, and it sorts the output folder into the
order the recordings were made.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .models import (
    FILENAME_EMAIL_PLACEHOLDER,
    strip_dictated_emails,
    strip_emails,
)

__all__ = [
    "SAST",
    "FORM_CALL",
    "FORM_CALL_RECORDING",
    "FORM_FREE_TEXT",
    "FORMS",
    "DERIVED_PREFIX",
    "TRANSCRIPT_SUFFIX",
    "SUMMARY_SUFFIX",
    "ACTIONS_SUFFIX",
    "TimestampUnavailable",
    "ParsedName",
    "OutputNames",
    "parse_source_name",
    "resolve_timestamp",
    "parse_graph_datetime",
    "stamp",
    "safe_stem",
    "output_stem",
    "output_names",
    "is_output_name",
]

#: South Africa keeps one offset all year and has not observed daylight saving since 1944,
#: so a fixed offset is not an approximation here — it is the whole rule. It is also the
#: only form that cannot break: :mod:`zoneinfo` needs a system tz database, and a container
#: shipped without one would otherwise take this service down years from now. A caller that
#: wants the IANA zone from config can pass its own ``tzinfo`` to every function below.
SAST = timezone(timedelta(hours=2), "SAST")

FORM_CALL = "call"                       # "Call <Party>_<YYMMDD>_<HHMMSS>"
FORM_CALL_RECORDING = "call-recording"   # the older handset: "Call recording <Party>_..."
FORM_FREE_TEXT = "free-text"             # named by hand — site meetings live here
FORMS = (FORM_CALL, FORM_CALL_RECORDING, FORM_FREE_TEXT)

#: The record's intake skips any file whose name starts with ``.`` or ``_``. The two derived
#: files carry it so that only the evidence is ever ingested as evidence.
DERIVED_PREFIX = "_"

TRANSCRIPT_SUFFIX = ".md"
SUMMARY_SUFFIX = "-summary.md"
ACTIONS_SUFFIX = "-actions.md"

#: The machine-written tail, and the only thing read as a timestamp. Anchored at the end of
#: the stem so the party may itself contain underscores and digits.
_STAMPED_TAIL_RE = re.compile(r"^(?P<lead>.*)_(?P<date>\d{6})_(?P<time>\d{6})$")

#: The voice recorder's own default name, for a recording he did not get to naming:
#: ``Voice 260806_162219``. The same handset writes ``Call +27…_260420_133533``, so the
#: digits are the same machine-written ``YYMMDD_HHMMSS`` — but a SPACE stands where the
#: call form has an underscore, and :data:`_STAMPED_TAIL_RE` needs two underscores. So
#: every recording he did not name was dated by when OneDrive finished RECEIVING it, which
#: for a site walk uploaded on the drive home is hours late and can cross midnight into a
#: day the recording did not happen on. The record derives its month folder and its item id
#: from that date.
#:
#: Read here, and only here: this shape is machine-written and unambiguous, unlike the
#: trailing digits of a hand-typed name, which :data:`_STAMPED_TAIL_RE`'s comment explains
#: are never read. ``241121`` as YYMMDD is 2024-11-21; day-first it would be November 2021,
#: before any of this existed.
_RECORDER_DEFAULT_RE = re.compile(r"^Voice (?P<date>\d{6})_(?P<time>\d{6})$")

#: "Call", optionally "Call recording", then the party. The party group is optional so that
#: a nameless "Call recording_260827_143005" does not come back with a party of "recording",
#: and ``recording`` is a named group so a name like "Call recordings Ltd" — where the word
#: is the start of the party, not the form — falls through to the party on backtracking.
_CALL_PREFIX_RE = re.compile(
    r"^call(?:[\s_]+(?P<recording>recording))?(?:[\s_]+(?P<party>.+))?$", re.IGNORECASE
)

#: OneDrive's own de-duplication suffix, appended when a name is re-used: "... (1).m4a".
#: Left on the stem it would push the timestamp tail off the end of the string and silently
#: demote a call recording to a hand-named one.
_COPY_MARKER_RE = re.compile(r"^(?P<stem>.*?)\s*\((?P<n>\d{1,3})\)$")

#: Illegal in a OneDrive / SharePoint file name. Control characters go with them, and so
#: does ``@`` — in all three of its spellings. An address is never allowed to survive into a
#: name, and treating the symbol as illegal is the belt to :func:`strip_emails`' braces; but
#: the belt only held for the ASCII symbol. A recording named "Call carel＠example.co.za"
#: (U+FF20, which is what a phone with a CJK keyboard writes, and U+FE6B, which some of them
#: write instead) was accepted whole and used for all three output files. U+FF20 and U+FE6B
#: are here for the same reason ``@`` is, and :func:`transcriber.outputs.check_name` refuses
#: a name carrying any of them outright, because a filename is the one thing this service
#: writes that cannot be corrected after the fact.
_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|#%@\uFF20\uFE6B\x00-\x1f]+')
_STAMP_PREFIX_RE = re.compile(r"^_?\d{8}-\d{6}-")

#: How many hex characters of the item id's digest go into every output name. Eight is
#: 4 billion values against a folder that sees a few thousand recordings a year, and it is
#: what makes two output names for two *different* recordings impossible rather than
#: unlikely — see :func:`output_stem`.
_ID_SLICE = 8

#: Long enough for any name he types, short enough that stamp + stem + "-summary.md" stays
#: well inside the path limits of both OneDrive and the git repository downstream.
_MAX_STEM = 120


class TimestampUnavailable(ValueError):
    """Neither the filename nor Graph could say when a recording was made.

    Raised rather than defaulted to "now": a fabricated timestamp files the recording under
    a day it did not happen on, and the downstream record derives its month folder and its
    item id from that date. A loud failure here becomes a quarantine, which is a person
    looking at one file; a quiet ``now`` becomes a recording nobody can find again.
    """


@dataclass(frozen=True)
class ParsedName:
    """What a source filename says about the recording, and what it does not.

    ``timestamp`` is naive on purpose: it is the wall clock the phone wrote, with no zone
    of its own. :func:`resolve_timestamp` is the one place it is given one.
    """

    original_name: str
    stem: str                        # the name without its extension, copy marker removed
    extension: str
    form: str                        # one of FORMS
    party: str | None = None         # the counterparty, when the name states one
    timestamp: datetime | None = None
    copy_marker: int | None = None   # the n from a OneDrive "(n)" duplicate suffix
    timestamp_note: str = ""         # plain words for the output body
    #: True when the moment came from the voice recorder's default name rather than from
    #: the call form. :func:`resolve_timestamp` checks it against the item's created time,
    #: because this is the one timestamp source that was inferred rather than agreed.
    timestamp_recovered: bool = False

    @property
    def matched_call_form(self) -> bool:
        """True when the name matched either machine-written call shape.

        False is the site-meeting case, and the caller must handle it: no party, no
        timestamp, fall back to the item's created time.
        """
        return self.form in (FORM_CALL, FORM_CALL_RECORDING)

    @property
    def is_free_text(self) -> bool:
        return self.form == FORM_FREE_TEXT

    @property
    def has_timestamp(self) -> bool:
        return self.timestamp is not None


@dataclass(frozen=True)
class OutputNames:
    """The three names this recording writes, all sharing one stamp and one stem."""

    stem: str            # "<YYYYMMDD-HHMMSS>-<source stem>"
    transcript: str
    summary: str
    actions: str

    def as_tuple(self) -> tuple[str, str, str]:
        return (self.transcript, self.summary, self.actions)

    def as_dict(self) -> dict[str, str]:
        return {"transcript": self.transcript, "summary": self.summary, "actions": self.actions}


def parse_source_name(name: str) -> ParsedName:
    """Read one source filename. Never raises: every name is a recording worth keeping."""
    base = os.path.basename(name or "").strip()
    stem, extension = os.path.splitext(base)

    copy_marker: int | None = None
    marker = _COPY_MARKER_RE.match(stem)
    if marker:
        copy_marker = int(marker.group("n"))
        stem = marker.group("stem")

    tail = _STAMPED_TAIL_RE.match(stem)
    timestamp: datetime | None = None
    bad_stamp = ""
    if tail:
        timestamp = _to_datetime(tail.group("date"), tail.group("time"))
        if timestamp is None:
            # Digits in the right shape that are not a real moment — 260832_250000. Read as
            # nothing at all rather than nudged into the nearest valid date.
            bad_stamp = f"{tail.group('date')}_{tail.group('time')}"

    # The recorder's own default, read only when the call form did not match. A file he
    # named himself never reaches this: the shape is anchored at both ends and is
    # case-sensitive, so "VOICE NOTE FOR CAREL" and "Voice 260806_162219 CANTERBURY" both
    # fall through to the hand-typed branch and keep their timestamp of None.
    recovered = False
    if timestamp is None and not bad_stamp:
        default = _RECORDER_DEFAULT_RE.match(stem)
        if default:
            timestamp = _to_datetime(default.group("date"), default.group("time"))
            recovered = timestamp is not None

    lead = tail.group("lead") if tail else stem
    call = _CALL_PREFIX_RE.match(lead.strip())

    if call and timestamp is not None:
        form = FORM_CALL_RECORDING if call.group("recording") else FORM_CALL
        party = _clean_party(call.group("party"))
        note = "read from the filename"
    elif recovered:
        # The voice recorder's default name. Machine-written and unambiguous, but nobody
        # agreed it: the note says where it came from so a reader can weigh it.
        form = FORM_FREE_TEXT
        party = None
        note = (
            "read from the voice recorder's own default name, which is when the recording "
            "was made rather than when it finished uploading"
        )
    elif timestamp is not None:
        # The machine-written tail without the "Call" prefix. The stamp is still
        # unambiguous; the party is not stated, so none is claimed.
        form = FORM_FREE_TEXT
        party = None
        note = "read from the timestamp in the filename; the filename does not name a party"
    else:
        form = FORM_FREE_TEXT
        party = None
        note = (
            f"the filename carries no timestamp (its digits {bad_stamp!r} are not a real "
            f"date and time)"
            if bad_stamp
            else "the filename carries no timestamp (it is a hand-typed name)"
        )

    return ParsedName(
        original_name=base,
        stem=stem,
        extension=extension.lower(),
        form=form,
        party=party,
        timestamp=timestamp,
        timestamp_recovered=recovered,
        copy_marker=copy_marker,
        timestamp_note=note,
    )


def resolve_timestamp(
    parsed: ParsedName,
    created_at: str | datetime | None = None,
    *,
    tz: timezone | None = None,
) -> tuple[datetime, str]:
    """When the recording was made, and in plain words where that came from.

    The filename wins when it has a timestamp, because it is the phone's own clock at the
    moment of recording; the item's created time is when OneDrive finished receiving it,
    which for a site walk uploaded on the drive home can be hours later. Both are recorded
    in the output either way, so a reader can see which one was used.
    """
    zone = tz or SAST
    created = parse_graph_datetime(created_at)

    if parsed.timestamp is not None:
        local = parsed.timestamp.replace(tzinfo=zone)
        # A moment recovered from the recorder's default name is the one timestamp source
        # nobody agreed to, so it is checked against the one fact that cannot be argued
        # with: a recording cannot have been made after it was uploaded. Later than that by
        # more than a day means the digits are not what they look like — a differently
        # configured handset, a name coincidentally in the shape — and the created time,
        # late as it is, is the safer answer. Being EARLIER is normal and is the whole
        # point: that is the drive home.
        if parsed.timestamp_recovered and created is not None:
            if local > created + timedelta(hours=24):
                return created.astimezone(zone), (
                    f"the name looks like the voice recorder's default but reads as "
                    f"{local.strftime('%Y-%m-%d %H:%M:%S')}, which is after OneDrive "
                    f"received the file, so it is not a moment this recording could have "
                    f"been made; this is when OneDrive recorded the file as created "
                    f"({created.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
                )
        return local, parsed.timestamp_note

    if created is None:
        raise TimestampUnavailable(
            f"{parsed.original_name!r} has no timestamp in its name and Graph reported no "
            f"usable created time ({created_at!r}), so there is nothing to date it by"
        )
    local = created.astimezone(zone)
    return local, (
        f"{parsed.timestamp_note}, so this is when OneDrive recorded the file as created "
        f"({created.strftime('%Y-%m-%d %H:%M:%S')} UTC)"
    )


def parse_graph_datetime(value: str | datetime | None) -> datetime | None:
    """Graph's ``createdDateTime`` to an aware datetime. None when it cannot be read."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = (value or "").strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        # Graph occasionally returns more sub-second digits than fromisoformat accepts.
        match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if not match:
            return None
        try:
            parsed = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}+00:00")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stamp(when: datetime) -> str:
    """``YYYYMMDD-HHMMSS`` — the prefix that keeps two files in one second apart."""
    return when.strftime("%Y%m%d-%H%M%S")


def safe_stem(stem: str) -> str:
    """A source stem reduced to something OneDrive and git will both accept.

    Truncation cuts back to a word boundary. A name sliced mid-token reads as a whole one
    and stops being the name of anything — the same reason the record's own ``clean()``
    backtracks instead of slicing.

    The redaction comes first and is not optional. A recording genuinely named
    ``Call carel@example.co.za_260827_120055.m4a`` would otherwise put an address into three
    filenames, into the ledger, into OneDrive and — through the downstream flow — into a git
    commit in the record, permanently. The body and the header are both scrubbed; this is
    the one remaining surface, and it is the only one that cannot be edited afterwards.
    """
    cleaned = strip_emails(stem or "", FILENAME_EMAIL_PLACEHOLDER)
    cleaned = strip_dictated_emails(cleaned, FILENAME_EMAIL_PLACEHOLDER)
    cleaned = _ILLEGAL_RE.sub("-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = cleaned.lstrip("~$").strip(" .-_")
    if len(cleaned) > _MAX_STEM:
        cut = cleaned[:_MAX_STEM]
        space = cut.rfind(" ")
        if space > _MAX_STEM // 2:
            cut = cut[:space]
        cleaned = cut.rstrip(" .-_")
    return cleaned or "recording"


def output_stem(
    when: datetime,
    stem: str,
    *,
    copy_marker: int | None = None,
    item_id: str = "",
) -> str:
    """``<YYYYMMDD-HHMMSS>-<stem>[-copy<n>]-<id>`` — unique per *recording*, not per name.

    The tail is the whole point. His phone re-uploads after an interrupted sync, so ``/CALLS``
    holds ``Call Carel_260827_143005.m4a`` **and** ``Call Carel_260827_143005 (1).m4a``: two
    Graph items, two ledger rows, two different recordings, and — before this — one output
    name. The second upload replaces the first in place, both rows go DONE, every read-back
    passes, and one recording's transcript no longer exists anywhere while the ledger says it
    was written. That is the exact failure this service was built to remove, committed by the
    service itself.

    ``copy_marker`` keeps OneDrive's own ``(n)`` visible because it is meaningful to a person.
    The digest of the item id is what makes the guarantee: it is unique by construction, so it
    also covers two hand-typed names that happen to match.
    """
    parts = [stamp(when), safe_stem(stem)]
    base = "-".join(parts)
    if copy_marker is not None:
        base = f"{base}-copy{int(copy_marker)}"
    tag = _id_tag(item_id)
    return f"{base}-{tag}" if tag else base


def _id_tag(item_id: str) -> str:
    """A short, filename-safe digest of the Graph item id. Empty when there is no id."""
    text = str(item_id or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_ID_SLICE]


def output_names(
    when: datetime,
    stem: str,
    *,
    copy_marker: int | None = None,
    item_id: str = "",
) -> OutputNames:
    """The three output names for one recording.

    Only the transcript is named so the record will ingest it. The summary and the actions
    file are the machine's *reading* of the recording, and the record's intake
    (``tools/transcripts.py:waiting``) skips any name beginning with ``_`` — so they travel
    with the transcript, are readable by a person in OneDrive and in the repository, and are
    never filed as source evidence. Filing a machine's summary as a verbatim source file is
    how unquoted prose ends up satisfying a proof that every claim traces to something
    actually said.
    """
    base = output_stem(when, stem, copy_marker=copy_marker, item_id=item_id)
    return OutputNames(
        stem=base,
        transcript=base + TRANSCRIPT_SUFFIX,
        summary=DERIVED_PREFIX + base + SUMMARY_SUFFIX,
        actions=DERIVED_PREFIX + base + ACTIONS_SUFFIX,
    )


def is_output_name(name: str) -> bool:
    """Whether a name is one this service wrote.

    Used where our own output folder is enumerated — a sweep that re-queued its own
    markdown would loop forever. It answers "ours or not", not which of the three: the
    ledger stores all three names against the recording, so nothing needs to read them
    back out of a filename.
    """
    base = os.path.basename(name or "")
    return bool(_STAMP_PREFIX_RE.match(base)) and base.lower().endswith(".md")


def _to_datetime(date_digits: str, time_digits: str) -> datetime | None:
    """``YYMMDD`` + ``HHMMSS`` to a naive local datetime. None if it is not a real moment.

    Year is 2000 + YY: 260827 is 2026-08-27. Year, then month, then day — reading it as
    day-first would put every August recording in a month that does not exist, or worse, in
    a plausible wrong one.
    """
    try:
        year = 2000 + int(date_digits[0:2])
        month = int(date_digits[2:4])
        day = int(date_digits[4:6])
        hour = int(time_digits[0:2])
        minute = int(time_digits[2:4])
        second = int(time_digits[4:6])
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _clean_party(value: str | None) -> str | None:
    """The counterparty as the filename states it, tidied but never rewritten."""
    party = re.sub(r"\s+", " ", (value or "")).strip(" -_.")
    return party or None
