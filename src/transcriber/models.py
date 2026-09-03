"""The record types every module in the service shares.

Nothing here imports the rest of the service, so these definitions cannot drift module to
module: there is one DriveItem, one Row, one Transcript.

Two of the house rules are enforced *here* rather than left to each caller's diligence,
because a rule that lives in nine places is a rule that is eventually broken in one of
them:

  * an extracted item can only ever be ``observed_by: agent`` — this pipeline is not a
    person, it cannot decide anything, and there is no field in which it could say it did;
  * no record type carries an email address onward. Anything that could reach an output
    passes through :func:`strip_emails`, and the fact that a redaction happened is recorded
    on the item rather than applied quietly.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, fields as dataclass_fields
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "EMAIL_RE",
    "DICTATED_EMAIL_RE",
    "EMAIL_PLACEHOLDER",
    "FILENAME_EMAIL_PLACEHOLDER",
    "contains_email",
    "contains_dictated_email",
    "strip_emails",
    "strip_dictated_emails",
    "OWNER_PATH_RE",
    "strip_owner_paths",
    "AUDIO_EXTENSIONS",
    "DEFAULT_ROUTE",
    "ROUTE_NAME_RE",
    "is_route_name",
    "route_env_stem",
    "route_env_var",
    "Route",
    "State",
    "DriveItem",
    "Row",
    "Segment",
    "Transcript",
    "Hints",
    "AudioInfo",
    "ExtractedItem",
    "DigestCounts",
    "ITEM_KINDS",
    "utc_now_iso",
    "day_of",
]

# Shaped on the downstream record's own address check (tools/ingest.py: ADDR_RE) but
# deliberately WIDER than it, in the one way that matters here: letters outside ASCII count
# as letters. The record's pattern is ASCII-only, and this is the Cape — Müller, José,
# Voëlklip. Measured against the ASCII version: "muller@site.co.za" was removed and
# "müller@site.co.za" was published untouched, and "joão.silva@kbc.co.za" came out as
# "joão[address removed]", which is worse than leaving it alone because it looks handled.
# Being wider than the record is safe in a way that being narrower is not: everything we
# remove, it never sees, and the only thing that could go wrong the other way is an address
# reaching the record because our pattern was the stricter of the two.
EMAIL_RE = re.compile(r"\b[\w.%+\-]+@[\w.\-]+\.[^\W\d_]{2,}\b")
EMAIL_PLACEHOLDER = "[address removed]"

#: The same rule, spelled the way somebody says it out loud: "carel at example dot co dot
#: za". :data:`EMAIL_RE` cannot see it — it needs an ``@`` — so a model that copies a
#: dictated address into a summary would put a reconstructable address into the record with
#: nothing to catch it. Deliberately tight: it needs a word before the ``at``, at least one
#: ``dot``, and an alphabetic ending, so "at 3 dot 5 metres" and "look at the roof" do not
#: match. It is applied to what this service *writes*, never to the verbatim transcript,
#: where the words are the evidence.
DICTATED_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]{2,}\s*(?:\(at\)|\[at\]|\bat\b)"
    r"(?:\s*[A-Za-z0-9\-]{2,})"
    r"(?:\s*(?:\(dot\)|\[dot\]|\bdot\b)\s*[A-Za-z0-9\-]{2,})*"
    r"\s*(?:\(dot\)|\[dot\]|\bdot\b)\s*[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

#: OneDrive for Business writes the drive owner's address into every ``webUrl`` as
#: ``/personal/james_kbc_co_za/`` — a UPN with ``@`` and ``.`` rewritten as ``_``, which
#: reverses by splitting on the underscore. Neither :data:`EMAIL_RE` nor the record's own
#: address check can see it, so it is the one encoding that would otherwise ride out in a
#: link in the morning email, in the ledger's stored URL, and in a rendered file.
OWNER_PATH_RE = re.compile(r"/personal/[^/\s]+/", re.I)


def strip_owner_paths(text: str | None) -> str:
    """Replace the identifying segment of a OneDrive path, keeping the link usable."""
    if not text:
        return text or ""
    return OWNER_PATH_RE.sub("/personal/[owner removed]/", text)


#: What a redacted address becomes inside a *filename*. ``[address removed]`` carries a
#: space and brackets, which OneDrive tolerates and a URL path does not; this one is safe
#: everywhere a name of ours travels.
FILENAME_EMAIL_PLACEHOLDER = "address-removed"

AUDIO_EXTENSIONS = frozenset(
    {".m4a", ".mp3", ".mp4", ".wav", ".aac", ".amr", ".ogg", ".opus", ".flac", ".wma", ".3gp", ".caf"}
)


def contains_email(text: str | None) -> bool:
    return bool(text) and bool(EMAIL_RE.search(text or ""))


def strip_emails(text: str | None, placeholder: str = EMAIL_PLACEHOLDER) -> str:
    """Replace any address with a visible marker.

    Visible, not silent: a reader of the output can see that something was removed and ask,
    which is the whole difference between a redaction and a quiet corruption of a quote.
    """
    if not text:
        return text or ""
    return EMAIL_RE.sub(placeholder, text)


def contains_dictated_email(text: str | None) -> bool:
    return bool(text) and bool(DICTATED_EMAIL_RE.search(text or ""))


def strip_dictated_emails(text: str | None, placeholder: str = EMAIL_PLACEHOLDER) -> str:
    """Remove a spoken-out-loud address from text this service authored.

    Never applied to the transcript body: there the words are what was said, and altering
    evidence is worse than carrying it. Applied to everything a model wrote, because a
    machine-authored line containing ``carel at example dot co dot za`` puts a working
    address into the record just as surely as the ``@`` spelling would.
    """
    if not text:
        return text or ""
    return DICTATED_EMAIL_RE.sub(placeholder, text)


def utc_now_iso(now: float | None = None) -> str:
    """One timestamp format across the whole service: UTC, second precision, sortable."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def day_of(stamp: str | None) -> str:
    """The ``YYYY-MM-DD`` part of one of our timestamps, or '' if there isn't one."""
    return (stamp or "")[:10]


#: The name of the one route a `.env` written before routes existed describes. A ledger row
#: with no route recorded is that route's, which is why the column defaults to it rather
#: than to NULL: an upgraded database has to read back as correct, not as unknown.
DEFAULT_ROUTE = "default"

#: What a route may be called. Lowercase, digits and hyphens, starting with a letter or a
#: digit — because the name is not decoration: it is half of a cursor key
#: (``delta:site-meetings``), a column value in the ledger, and the middle of an environment
#: variable name. Anything that would need quoting, case-folding or escaping in one of those
#: three places is refused here instead of going wrong in one of them later.
ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def is_route_name(name: str | None) -> bool:
    return bool(name) and bool(ROUTE_NAME_RE.match(name or ""))


def route_env_stem(name: str) -> str:
    """``site-meetings`` -> ``SITE_MEETINGS``: the middle of that route's variables.

    One function rather than three copies of ``name.upper().replace("-", "_")``, because
    config, the wizard and the ``routes`` command all have to arrive at the *same* variable
    name or a route reads its folders from settings nobody ever wrote.
    """
    return (name or "").strip().upper().replace("-", "_")


def route_env_var(name: str, suffix: str) -> str:
    """The full variable: ``route_env_var("site-meetings", "source")`` -> ``ROUTE_SITE_MEETINGS_SOURCE``."""
    return f"ROUTE_{route_env_stem(name)}_{(suffix or '').strip().upper()}"


@dataclass(frozen=True)
class Route:
    """One watched folder and where its results go.

    A kind of recording, in his words: phone calls, site meetings, WhatsApp voice notes,
    whatever the next device drops somewhere else. The service runs N of these, each with
    its own delta cursor, and the route a recording arrived on travels with it all the way
    to the folder its transcript lands in.

    Frozen because a route is read by the worker, the pipeline, the archive pass and the
    digest, on different threads, throughout a run: a route that could be edited underneath
    them is a recording written to a folder nobody chose. Changing one means writing the
    ``.env`` and restarting, which is also the only way the change is durable.

    ``archive_folder_id`` empty means this route never archives — a deliberate value, not a
    missing one. ``engine`` empty means the service default. ``enabled`` false means the
    folder is not watched, and nothing else: the ledger history of everything it ever
    processed is untouched, which is what makes pausing a route safe.
    """

    name: str
    label: str = ""
    source_folder_id: str = ""
    output_folder_id: str = ""
    archive_folder_id: str = ""
    engine: str = ""
    enabled: bool = True

    @property
    def display(self) -> str:
        """What to call it in a sentence a person reads. Never empty."""
        return (self.label or "").strip() or self.name

    @property
    def archives(self) -> bool:
        """False means aged recordings stay where they are — skipped, not an error."""
        return bool((self.archive_folder_id or "").strip())

    @property
    def env_stem(self) -> str:
        return route_env_stem(self.name)

    def env_var(self, suffix: str) -> str:
        return route_env_var(self.name, suffix)

    def describe(self) -> str:
        """One plain line for a log, a report or the ``routes`` listing."""
        parts = [f"{self.display} ({self.name})"]
        if not self.enabled:
            parts.append("paused")
        if self.engine:
            parts.append(f"engine {self.engine}")
        if not self.archives:
            parts.append("never archives")
        return ", ".join(parts)


class State:
    """The ledger's state machine.

    ``QUARANTINED`` and ``SKIPPED_EMPTY`` are terminal but are *not* success: both mean a
    person has something to look at, which is why neither is ever reached silently.
    """

    DISCOVERED = "DISCOVERED"
    CLAIMED = "CLAIMED"
    FETCHED = "FETCHED"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYSED = "ANALYSED"
    DONE = "DONE"
    QUARANTINED = "QUARANTINED"
    SKIPPED_EMPTY = "SKIPPED_EMPTY"
    #: Somebody asked us to forget this recording and a person carried it out. The row is
    #: kept and everything on it that described the recording is gone: the name, the three
    #: output names, the hashes, the error text, the metadata. What remains is that a
    #: recording existed, when it arrived, that it was erased, by whom, and on what request.
    #:
    #: A tombstone rather than a deleted row, and both halves of that are deliberate. The
    #: content is gone because that is what was asked for. The row stays because a record
    #: with a hole in it where a thing used to be is worse than one that says "there was
    #: something here and it was removed on this date at this person's request" — and
    #: because a deleted row would be rediscovered as new the next time anything enumerated
    #: the folder.
    #:
    #: NOT reachable through ``advance()``. Only ``Ledger.erase()`` sets it, and that
    #: requires a person's name and a reason, the same way a released hold does.
    ERASED = "ERASED"

    PIPELINE = (DISCOVERED, CLAIMED, FETCHED, TRANSCRIBED, ANALYSED, DONE)
    TERMINAL = frozenset((DONE, QUARANTINED, SKIPPED_EMPTY, ERASED))
    ALL = frozenset(PIPELINE) | TERMINAL
    ACTIVE = frozenset(PIPELINE) - frozenset((DONE,))

    @staticmethod
    def is_terminal(state: str) -> bool:
        return state in State.TERMINAL

    @staticmethod
    def is_known(state: str) -> bool:
        return state in State.ALL

    @staticmethod
    def rank(state: str) -> int:
        """Position along the happy path; -1 for the two states that leave it."""
        try:
            return State.PIPELINE.index(state)
        except ValueError:
            return -1


@dataclass
class DriveItem:
    """One Microsoft Graph driveItem, from ``/delta`` or from a direct ``GET``.

    ``download_url`` and the hash fields are ``None`` on anything that came out of delta on
    a business account — Graph does not return them there. That is not a bug to work
    around; it is why completeness re-``GET``s the item.
    """

    item_id: str
    name: str
    size: int = 0
    etag: str | None = None
    ctag: str | None = None
    parent_id: str | None = None
    web_url: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    mime_type: str | None = None
    quick_xor_hash: str | None = None
    sha256_hash: str | None = None
    sha1_hash: str | None = None
    download_url: str | None = None
    deleted: bool = False
    is_folder: bool = False
    pending: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_graph(cls, payload: Mapping[str, Any]) -> "DriveItem":
        file_facet = payload.get("file") or {}
        hashes = file_facet.get("hashes") or {}
        parent = payload.get("parentReference") or {}
        return cls(
            item_id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            size=int(payload.get("size") or 0),
            etag=payload.get("eTag"),
            ctag=payload.get("cTag"),
            parent_id=parent.get("id"),
            web_url=payload.get("webUrl"),
            created_at=payload.get("createdDateTime"),
            modified_at=payload.get("lastModifiedDateTime"),
            mime_type=file_facet.get("mimeType"),
            quick_xor_hash=hashes.get("quickXorHash"),
            sha256_hash=hashes.get("sha256Hash"),
            sha1_hash=hashes.get("sha1Hash"),
            download_url=payload.get("@microsoft.graph.downloadUrl"),
            deleted="deleted" in payload,
            is_folder="folder" in payload,
            pending=bool(payload.get("pendingOperations")),
            raw=dict(payload),
        )

    @classmethod
    def from_graph_item(cls, item: Any) -> "DriveItem":
        """One :class:`transcriber.graph.DriveItem` as the record type the ledger stores.

        The wire shape and the stored shape are deliberately different objects — the wire
        one carries things only Graph cares about (``is_package``, ``pendingOperations``),
        the stored one carries the ledger's columns. What must not be different is the
        conversion between them, and it was: three callers each wrote their own, and two of
        them had no answer for an item that arrives without its payload. The same recording
        was therefore recordable by one caller and not by another, which is the shape of a
        lost file.

        Duck-typed rather than imported, so ``models`` still depends on nothing.
        """
        raw = getattr(item, "raw", None)
        if isinstance(raw, Mapping) and raw.get("id"):
            return cls.from_graph(raw)
        hashes = dict(getattr(item, "hashes", {}) or {})
        return cls(
            item_id=str(getattr(item, "id", "") or ""),
            name=str(getattr(item, "name", "") or ""),
            size=int(getattr(item, "size", 0) or 0),
            etag=getattr(item, "etag", None) or None,
            ctag=getattr(item, "ctag", None) or None,
            parent_id=getattr(item, "parent_id", None) or None,
            web_url=getattr(item, "web_url", None) or None,
            created_at=getattr(item, "created_datetime", None) or None,
            modified_at=getattr(item, "last_modified_datetime", None) or None,
            mime_type=getattr(item, "mime_type", None) or None,
            quick_xor_hash=hashes.get("quickXorHash"),
            sha256_hash=hashes.get("sha256Hash"),
            sha1_hash=hashes.get("sha1Hash"),
            download_url=getattr(item, "download_url", None) or None,
            deleted=bool(getattr(item, "is_deleted", False)),
            is_folder=bool(getattr(item, "is_folder", False)),
            pending=bool(getattr(item, "pending_operations", ())),
        )

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def extension(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def looks_like_audio(self) -> bool:
        return self.extension in AUDIO_EXTENSIONS

    @property
    def has_hash(self) -> bool:
        return bool(self.quick_xor_hash or self.sha256_hash or self.sha1_hash)

    @property
    def best_hash(self) -> str | None:
        return self.sha256_hash or self.quick_xor_hash or self.sha1_hash


@dataclass
class Row:
    """One ledger row: everything known about one recording, at any point in its life.

    Field names match the ledger's columns one for one, so a row read back is the same
    shape as a row written.
    """

    item_id: str
    name: str = ""
    state: str = State.DISCOVERED
    #: Which route this recording arrived on. It decides where its transcript is written and
    #: which archive folder, if any, it ages into, so it travels with the row rather than
    #: being re-derived later from a parent folder that may since have changed.
    route: str = DEFAULT_ROUTE
    size: int = 0
    etag: str | None = None
    parent_id: str | None = None
    web_url: str | None = None
    created_at: str | None = None       # createdDateTime in Graph — when it was recorded
    modified_at: str | None = None
    discovered_at: str | None = None    # when we first saw it
    updated_at: str | None = None
    claimed_by: str | None = None
    lease_until: float | None = None    # epoch seconds; compared against time.time()
    attempts: int = 0
    seen_count: int = 1
    last_error: str | None = None
    content_hash: str | None = None     # sha256 of the bytes we actually downloaded
    graph_hash: str | None = None       # what Graph reported for the same item
    duration_s: float | None = None
    container: str | None = None
    truncated: bool = False
    engine: str | None = None
    language: str | None = None
    word_count: int | None = None
    transcript_name: str | None = None
    summary_name: str | None = None
    actions_name: str | None = None
    output_item_ids: dict[str, str] = field(default_factory=dict)
    quarantine_reason: str | None = None
    #: Set only by ``Ledger.erase``. ``erased_by`` is a PERSON's name, never a hostname and
    #: never a process: the whole point of an erasure is that somebody decided it.
    erased_at: str | None = None
    erased_by: str | None = None
    erased_because: str | None = None
    quarantined_at: str | None = None
    skipped_reason: str | None = None
    done_at: str | None = None
    archived_at: str | None = None
    source_deleted_at: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_db(cls, record: Mapping[str, Any]) -> "Row":
        known = {f.name for f in dataclass_fields(cls)}
        kwargs: dict[str, Any] = {}
        for key in known:
            if key not in record.keys():
                continue
            value = record[key]
            if key in ("output_item_ids", "meta"):
                kwargs[key] = _json_dict(value)
            elif key == "truncated":
                kwargs[key] = bool(value)
            else:
                kwargs[key] = value
        kwargs.setdefault("item_id", record["item_id"])
        return cls(**kwargs)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.name)[0]

    @property
    def is_terminal(self) -> bool:
        return State.is_terminal(self.state)

    @property
    def outputs_present(self) -> bool:
        return bool(self.transcript_name and self.summary_name and self.actions_name)

    def lease_expired(self, now: float | None = None) -> bool:
        return self.lease_until is None or self.lease_until < (time.time() if now is None else now)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


@dataclass
class Segment:
    """One stretch of speech. ``speaker`` is None when the engine did not diarise."""

    start: float
    end: float
    speaker: str | None = None
    text: str = ""

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end - self.start)

    def shifted(self, offset_s: float) -> "Segment":
        """Used when reassembling split audio, where each piece's clock starts at zero."""
        return Segment(self.start + offset_s, self.end + offset_s, self.speaker, self.text)


@dataclass
class Transcript:
    """What an engine returned. ``text`` is authoritative; segments may be empty."""

    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    engine_metadata: dict[str, Any] = field(default_factory=dict)
    engine: str = ""
    duration_s: float | None = None

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def covered_duration_s(self) -> float:
        """How much wall-clock the segments actually account for.

        The split-and-reassemble guard compares this against the probed audio duration: a
        splitting bug shortens a transcript without raising anything, so the arithmetic has
        to be checked rather than trusted.
        """
        return max((s.end for s in self.segments), default=0.0)


@dataclass
class Hints:
    """What we can tell an engine before it listens, none of it a conclusion.

    The counterparty name comes from the filename, the vocabulary from config. Neither is
    ever treated as a fact about the recording — they only make a misheard word less
    likely.
    """

    vocabulary: tuple[str, ...] = ()
    counterparty: str | None = None
    language: str | None = None
    languages: tuple[str, ...] = ()
    recorded_at: str | None = None
    source_name: str = ""
    duration_s: float | None = None

    def prompt_text(self) -> str:
        """A single hint string for engines that take one. Never carries an address."""
        parts: list[str] = []
        if self.counterparty:
            parts.append(f"Speaking about or with: {self.counterparty}.")
        if self.vocabulary:
            parts.append("Names and terms likely to occur: " + ", ".join(self.vocabulary) + ".")
        if self.language:
            parts.append(f"Primary language: {self.language}.")
        elif self.languages:
            parts.append("Languages: " + ", ".join(self.languages) + ".")
        return strip_emails(" ".join(parts))


@dataclass
class AudioInfo:
    """The result of probing the file itself.

    ``truncated`` is the field this whole check exists for: a recording cut off by a dying
    battery uploads perfectly and hashes perfectly, and only the container says so.
    """

    duration_s: float
    container: str
    truncated: bool
    reason: str = ""
    size_bytes: int = 0
    probed_by: str = ""          # "ffprobe" or "walk"
    bitrate: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_silent(self) -> bool:
        return self.duration_s <= 0.0


ITEM_KINDS = ("commitment", "question", "observation", "risk", "followup")


@dataclass
class ExtractedItem:
    """One thing the AI pass noticed, as a proposal for a person — never as a fact.

    ``quote`` is verbatim from the transcript and is verified mechanically before this item
    is allowed anywhere near an output; ``quote_verified`` is that verification's answer,
    and an item whose quote could not be found goes to the review list instead.

    There is deliberately no ``decided_by`` field. ``observed_by`` is fixed to ``agent`` and
    the constructor refuses any other value, because the one thing this pipeline must never
    be able to express is that it concluded something.
    """

    kind: str
    text: str
    quote: str
    speaker: str | None = None
    site: str | None = None
    due: str | None = None
    confidence: float | None = None
    quote_verified: bool = False
    observed_by: str = "agent"
    redacted: bool = False
    source_item_id: str | None = None

    def __post_init__(self) -> None:
        if self.observed_by != "agent":
            raise ValueError(
                "observed_by is 'agent' and nothing else: this pipeline cannot decide, "
                f"so it cannot claim {self.observed_by!r} observed anything"
            )
        self.kind = (self.kind or "").strip().lower()
        if not self.kind:
            raise ValueError("an extracted item needs a kind, one of: " + ", ".join(ITEM_KINDS))
        if not (self.quote or "").strip():
            raise ValueError("an extracted item without a verbatim quote is an assertion, not an observation")
        for attr in ("text", "quote", "speaker", "site"):
            value = getattr(self, attr)
            if contains_email(value):
                setattr(self, attr, strip_emails(value))
                self.redacted = True
        # The spoken spelling, in what the model *wrote* only. ``quote`` is deliberately not
        # in this list: it is verbatim from the transcript, and an address said out loud is
        # evidence of what was said. Everything else here is the machine's own words.
        for attr in ("text", "speaker", "site"):
            value = getattr(self, attr)
            if contains_dictated_email(value):
                setattr(self, attr, strip_dictated_emails(value))
                self.redacted = True

    def to_dict(self) -> dict[str, Any]:
        """The shape written into the actions file. ``decided_by`` is not producible."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "text": self.text,
            "quote": self.quote,
            "observed_by": "agent",
        }
        for key in ("speaker", "site", "due", "confidence", "source_item_id"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        if self.redacted:
            out["redacted"] = True
        return out


@dataclass
class DigestCounts:
    """The 06:00 email's whole content, counted.

    Sent on good days too, which is why ``all_done`` and ``nothing_arrived`` are both
    first-class: a report that only arrives when something breaks is indistinguishable
    from a service that has died.
    """

    day: str
    discovered: int = 0
    done: int = 0
    quarantined: int = 0
    skipped_empty: int = 0
    in_flight: int = 0
    archived: int = 0
    done_on_day: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    failures: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_counts(cls, day: str, counts: Mapping[str, Any]) -> "DigestCounts":
        return cls(
            day=str(counts.get("day") or day),
            discovered=int(counts.get("discovered") or 0),
            done=int(counts.get("done") or 0),
            quarantined=int(counts.get("quarantined") or 0),
            skipped_empty=int(counts.get("skipped_empty") or 0),
            in_flight=int(counts.get("in_flight") or 0),
            archived=int(counts.get("archived") or 0),
            done_on_day=int(counts.get("done_on_day") or 0),
            by_state=dict(counts.get("by_state") or {}),
            failures=tuple(counts.get("failures") or ()),
        )

    @property
    def failed(self) -> int:
        return self.quarantined + self.in_flight

    @property
    def nothing_arrived(self) -> bool:
        return self.discovered == 0

    @property
    def all_done(self) -> bool:
        return self.discovered > 0 and self.done == self.discovered

    @property
    def needs_a_person(self) -> bool:
        return self.quarantined > 0 or self.in_flight > 0 or self.nothing_arrived
