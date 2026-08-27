"""The nightly reconciliation — the backstop that turns "unlikely to miss one" into "cannot".

The live path is a change feed. Change feeds are excellent and they are not infallible: a
cursor can be rejected, a page can be dropped by a proxy, a process can die between reading
a page and committing it, an operator can restore a file from a backup so that it never
appears as a change at all. Every one of those failures is silent. So once a night this
walks the whole source folder from scratch, compares it against the ledger, and re-queues
anything the live path does not have finished.

Three decisions here are load-bearing, and all three come from ARCHITECTURE.md.

**Enumeration is delta from a zero cursor, never ``/children``.** A folder listing is not
guaranteed complete while writes are in flight, and his phone writes continuously; a short
listing is exactly how the original measurement stuck at 200 items. A backstop built on a
listing that can quietly come back short is not a backstop.

**The rows and the cursor are committed together**, through ``Ledger.record_page``. The
sweep can crash on page nine and lose nothing, because the cursor never got ahead of the
rows.

**Nothing here matches on a filename.** This is the whole point of the sweep sharing no
classification logic with the live path: if both decided what a recording is by looking at
the end of its name, then a file named in a way neither expects would be invisible to both
at once, and a backstop that shares the primary's blind spot is decoration. So this module
classifies on Graph facets instead — folder/package/deleted flags, the ``file`` facet's MIME
type, the item's parent, and the ledger's own record of which items are our outputs. It
imports no filename constant and inspects no extension. What it cannot classify it queues
**and** reports, so an oddly-named recording gets processed and a person still hears about
it: the one outcome that is never available here is "quietly ignored".

What it does with what it finds:

===========================  ============================================================
at source, no ledger row     recorded as DISCOVERED — the live path missed it
unfinished, lease expired    re-queued to DISCOVERED with a reason in the event log
unfinished, too many tries   quarantined, loudly, for a person
unfinished, lease live       left alone; a worker has it right now
unfinished, gone at source   reported — the file vanished mid-flight, nobody can fix that
                             silently
DONE but outputs incomplete  reported — the "summary with no transcript" bug, made visible
unclassifiable file          queued anyway *and* reported
===========================  ============================================================

It decides nothing about content, and it never marks anything done.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .ledger import SWEEP_CURSOR, Ledger, LedgerError
from .models import DriveItem as LedgerDriveItem
from .models import Row, State, utc_now_iso

log = logging.getLogger("transcriber.sweep")

__all__ = [
    "SweepFinding",
    "SweepReport",
    "sweep",
    "should_run",
    "mark_run",
    "classify",
    "RETRY_AFTER_S",
    "SWEEP_ATTEMPT_MARK",
    "local_now",
    "parse_stamp",
    "zone_of",
    "SWEEP_DAY_MARK",
]

#: Bookkeeping mark, not a delta cursor: "the sweep completed for this local day".
#: Guarded cursor names (anything starting ``delta``) are refused by the ledger on purpose.
SWEEP_DAY_MARK = "sweep:last_completed_day"

#: When the sweep last *started*, successful or not. A sweep that fails must retry, but a
#: full re-enumeration every two minutes because Graph is having a bad afternoon would be a
#: self-inflicted throttling incident on top of whatever the original fault was.
SWEEP_ATTEMPT_MARK = "sweep:last_attempt_at"

#: How long a failed sweep waits before trying the whole folder again.
RETRY_AFTER_S = 3600.0

#: What counts as audio, decided from the MIME type Graph reports rather than from the
#: name. A voice memo arrives as ``audio/*``; a phone that wrapped it in an MP4 container
#: arrives as ``video/mp4`` with no video stream, which is why both are here.
_RECORDING_MIME_PREFIXES = ("audio/", "video/")

#: Classifications this module produces. Nothing falls outside them.
RECORDING = "recording"
OUR_OUTPUT = "our-output"
STRUCTURE = "structure"
UNRECOGNISED = "unrecognised"

_FRACTION = re.compile(r"\.(\d{7,})")


# --------------------------------------------------------------------------- time

_ZONE_CACHE: dict[str, Any] = {}


def zone_of(name: str) -> datetime.tzinfo:
    """The configured IANA zone, or UTC with a complaint.

    A missing tzdata would otherwise shift every scheduled job by two hours without
    anything saying so; being an hour early is survivable, not knowing about it is not.
    """
    key = name or "UTC"
    cached = _ZONE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        from zoneinfo import ZoneInfo

        zone: datetime.tzinfo = ZoneInfo(key)
    except Exception as exc:  # noqa: BLE001 - any zoneinfo failure means the same thing
        log.error(
            "timezone %r is not available (%s) — the scheduled jobs will run on UTC, which "
            "will shift the digest and the sweep away from the hours they were set for",
            key, exc,
        )
        zone = datetime.timezone.utc
    _ZONE_CACHE[key] = zone
    return zone


def local_now(config: Any, now: float | None = None) -> datetime.datetime:
    """Wall-clock time in the configured zone. The scheduled jobs are set in local hours."""
    clock = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(clock, zone_of(getattr(config, "timezone", "UTC")))


def parse_stamp(value: str | None) -> float | None:
    """Epoch seconds from any timestamp this service or Graph produces, or None.

    Graph emits seven fractional digits, which ``fromisoformat`` rejects; a comparison that
    silently failed here would make every row look ageless and stop the sweep re-queueing
    anything. Truncating is safe — nothing in this service is decided on a microsecond.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6], text)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        log.warning("could not read the timestamp %r; treating it as unknown", value)
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


# --------------------------------------------------------------------------- findings


@dataclass(frozen=True)
class SweepFinding:
    """One thing the sweep did or one thing it wants a person to look at."""

    kind: str
    item_id: str
    name: str
    detail: str
    needs_a_person: bool = False

    def line(self) -> str:
        mark = "!" if self.needs_a_person else "-"
        return f"  {mark} {self.kind}: {self.name or self.item_id} — {self.detail}"


@dataclass
class SweepReport:
    """What the sweep found, in a shape the digest and the logs can both read."""

    started_at: str
    finished_at: str = ""
    pages: int = 0
    items_seen: int = 0
    recordings_seen: int = 0
    outputs_seen: int = 0
    new_rows: int = 0
    requeued: int = 0
    quarantined: int = 0
    unrecognised: int = 0
    vanished: int = 0
    incomplete_outputs: int = 0
    in_hand: int = 0
    left_alone: int = 0
    cursor_advanced: bool = False
    dry_run: bool = False
    findings: list[SweepFinding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A sweep that could not finish is a failed sweep, whatever it managed on the way."""
        return not self.errors

    @property
    def needs_a_person(self) -> int:
        return sum(1 for f in self.findings if f.needs_a_person)

    def add(self, kind: str, item_id: str, name: str, detail: str, *, needs_a_person: bool = False) -> None:
        self.findings.append(SweepFinding(kind, item_id, name, detail, needs_a_person))

    def headline(self) -> str:
        if self.errors:
            return f"nightly sweep FAILED after {self.pages} page(s): {self.errors[0]}"
        parts = [f"{self.items_seen} item(s) at source"]
        for count, word in (
            (self.new_rows, "newly recorded"),
            (self.requeued, "re-queued"),
            (self.quarantined, "quarantined"),
            (self.unrecognised, "unrecognised"),
            (self.vanished, "vanished mid-flight"),
            (self.incomplete_outputs, "done with outputs missing"),
        ):
            if count:
                parts.append(f"{count} {word}")
        if len(parts) == 1:
            parts.append("nothing needed doing")
        return "nightly sweep: " + ", ".join(parts)

    def render(self) -> str:
        lines = [self.headline()]
        if self.dry_run:
            lines.append("  (dry run — nothing was written)")
        lines.append(
            f"  pages {self.pages}, recordings {self.recordings_seen}, our outputs {self.outputs_seen}, "
            f"in hand {self.in_hand}, healthy and recent {self.left_alone}"
        )
        if not self.cursor_advanced and not self.dry_run:
            lines.append("  ! the enumeration mark was not advanced — see the errors below")
        for finding in self.findings:
            lines.append(finding.line())
        for error in self.errors:
            lines.append(f"  ! error: {error}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- classify


def classify(item: Any, *, output_folder_id: str = "", own_output_ids: frozenset[str] = frozenset()) -> str:
    """What kind of thing this is, decided without ever looking at the filename.

    The order matters. Structure first (a folder is not a recording however it is named),
    then our own outputs by *identity* — the ledger recorded the item ids we wrote — then
    the MIME type Graph reports. Anything left is unrecognised, which is a report, not a
    dismissal.
    """
    if getattr(item, "is_folder", False) or getattr(item, "is_package", False):
        return STRUCTURE
    if getattr(item, "is_deleted", False):
        return STRUCTURE
    item_id = str(getattr(item, "id", "") or "")
    if item_id and item_id in own_output_ids:
        return OUR_OUTPUT
    parent_id = str(getattr(item, "parent_id", "") or "")
    mime = str(getattr(item, "mime_type", "") or "").lower()
    if output_folder_id and parent_id == output_folder_id and not mime.startswith(_RECORDING_MIME_PREFIXES):
        # Written by us into the output folder. Identified by where it lives, not by what
        # it is called.
        return OUR_OUTPUT
    if mime.startswith(_RECORDING_MIME_PREFIXES):
        return RECORDING
    return UNRECOGNISED


def _own_output_ids(ledger: Ledger) -> frozenset[str]:
    """Every driveItem id this service has written. The only name-free way to know our own."""
    ids: set[str] = set()
    for row in ledger.rows_in_state(State.DONE):
        for value in (row.output_item_ids or {}).values():
            if value:
                ids.add(str(value))
    return frozenset(ids)


def _as_ledger_item(item: Any) -> LedgerDriveItem:
    """Graph's DriveItem to the ledger's. The conversion itself lives in ``models``.

    Kept as a name here because the sweep reads better for it, but it is deliberately not a
    second implementation: the live poll and the backfill convert the same way, so a file
    the sweep can record is a file they can record too.
    """
    return LedgerDriveItem.from_graph_item(item)


# --------------------------------------------------------------------------- the sweep


def sweep(
    config: Any,
    ledger: Ledger,
    graph: Any,
    *,
    now: float | None = None,
    stale_after_s: float | None = None,
    dry_run: bool = False,
    resolve_unrecognised: int = 50,
) -> SweepReport:
    """Re-enumerate the source folder from zero, reconcile it with the ledger, report.

    ``stale_after_s`` is how long an unfinished row may sit untouched before the sweep
    concludes the live path has dropped it. It defaults to four lease periods, so a worker
    that is simply slow is never interrupted, and it never falls below an hour.
    """
    clock = time.time() if now is None else now
    report = SweepReport(started_at=utc_now_iso(clock), dry_run=dry_run)

    lease_seconds = int(getattr(config, "lease_seconds", 900) or 900)
    stale_after = float(stale_after_s if stale_after_s is not None else max(3600.0, lease_seconds * 4.0))
    max_attempts = int(getattr(config, "max_attempts", 3) or 3)
    source_folder_id = getattr(config, "source_folder_id", "") or None
    output_folder_id = str(getattr(config, "output_folder_id", "") or "")

    # How long a row has been stuck, measured from the last time it genuinely moved — the
    # event log's ``advanced`` entries, written only by ``Ledger.advance``. NOT from
    # ``updated_at``: claiming writes it, releasing writes it, and every deferral writes it,
    # so a row the worker picks up and fails on every two-minute cycle showed an idle time of
    # about two minutes forever, landed in ``left_alone``, and could never be re-queued or
    # quarantined by the backstop. Combined with a live path that could not count the
    # attempt, that was a closed loop with no ending.
    progress = ledger.last_advanced_at()
    idle_baseline = {
        row.item_id: progress.get(row.item_id) or row.discovered_at
        for row in ledger.unfinished()
    }

    if not dry_run:
        ledger.cursor_set(SWEEP_ATTEMPT_MARK, utc_now_iso(clock))

    own_outputs = _own_output_ids(ledger)
    seen_ids: set[str] = set()
    unrecognised_items: list[Any] = []

    # --- 1. enumerate, from a zero cursor, committing rows and cursor together --------
    try:
        for page in graph.delta(source_folder_id, None):
            report.pages += 1
            to_record: list[LedgerDriveItem] = []
            for item in page.items:
                report.items_seen += 1
                item_id = str(getattr(item, "id", "") or "")
                kind = classify(item, output_folder_id=output_folder_id, own_output_ids=own_outputs)
                if kind == STRUCTURE:
                    continue
                if kind == OUR_OUTPUT:
                    report.outputs_seen += 1
                    continue
                if not item_id:
                    report.errors.append(
                        f"Graph returned a file with no id (name given as {getattr(item, 'name', '')!r}); "
                        "it cannot be recorded or tracked"
                    )
                    continue
                seen_ids.add(item_id)
                if kind == RECORDING:
                    report.recordings_seen += 1
                else:
                    unrecognised_items.append(item)
                to_record.append(_as_ledger_item(item))

            if dry_run:
                report.new_rows += sum(1 for i in to_record if ledger.get(i.item_id) is None)
                continue

            if page.cursor:
                # The invariant: these rows and this cursor commit or neither does.
                inserted = ledger.record_page(to_record, page.cursor, cursor_name=SWEEP_CURSOR)
                report.cursor_advanced = True
            else:
                # Graph gave a final page with no deltaLink. The rows still matter more than
                # the mark, so they are written individually and the gap is reported rather
                # than papered over.
                inserted = [i.item_id for i in to_record if ledger.upsert_discovered(i)]
                report.errors.append(
                    "the final delta page carried no deltaLink, so the enumeration mark could "
                    "not be advanced; every row from that page was still recorded"
                )
            report.new_rows += len(inserted)
            for item_id in inserted:
                row = ledger.get(item_id)
                report.add(
                    "recorded",
                    item_id,
                    row.name if row else "",
                    "at source but not in the ledger — the live change feed did not have it",
                    needs_a_person=True,
                )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        report.errors.append(f"{type(exc).__name__}: {exc}")
        log.exception("nightly sweep could not finish enumerating the source folder")
        report.finished_at = utc_now_iso()
        return report

    # --- 2. anything we could not classify: ask Graph directly, then say so -----------
    for item in unrecognised_items[: max(0, resolve_unrecognised)]:
        report.unrecognised += 1
        item_id = str(getattr(item, "id", "") or "")
        name = str(getattr(item, "name", "") or "")
        mime = str(getattr(item, "mime_type", "") or "")
        if not mime:
            # Delta withholds parts of the file facet; a direct GET is the same discipline
            # completeness.py uses, and for the same reason.
            try:
                full = graph.get_item(item_id)
                mime = str(getattr(full, "mime_type", "") or "")
                if mime.lower().startswith(_RECORDING_MIME_PREFIXES):
                    report.unrecognised -= 1
                    report.recordings_seen += 1
                    continue
            except Exception as exc:  # noqa: BLE001
                mime = f"could not be read ({type(exc).__name__})"
        report.add(
            "unrecognised",
            item_id,
            name,
            f"a file in the recordings folder that does not look like audio (type: {mime or 'none given'}). "
            "It has been queued anyway so it cannot be lost; if it is not a recording it will "
            "be quarantined with a reason rather than ignored",
            needs_a_person=True,
        )
    if len(unrecognised_items) > max(0, resolve_unrecognised):
        extra = len(unrecognised_items) - max(0, resolve_unrecognised)
        report.unrecognised += extra
        report.add(
            "unrecognised",
            "",
            f"{extra} further file(s)",
            "not listed individually; all of them were recorded in the ledger",
            needs_a_person=True,
        )

    # --- 3. diff the ledger against what is actually there ---------------------------
    try:
        _reconcile(
            ledger,
            report,
            seen_ids=seen_ids,
            clock=clock,
            stale_after=stale_after,
            max_attempts=max_attempts,
            dry_run=dry_run,
            idle_baseline=idle_baseline,
        )
        _check_finished_rows(ledger, report)
    except LedgerError as exc:
        report.errors.append(f"ledger refused a reconciliation step: {exc}")
        log.exception("nightly sweep could not reconcile the ledger")

    if not dry_run and report.ok:
        mark_run(config, ledger, now=clock)

    report.finished_at = utc_now_iso()
    log.info("%s", report.headline())
    return report


def _reconcile(
    ledger: Ledger,
    report: SweepReport,
    *,
    seen_ids: set[str],
    clock: float,
    stale_after: float,
    max_attempts: int,
    dry_run: bool,
    idle_baseline: dict[str, str | None],
) -> None:
    for row in ledger.unfinished():
        if row.item_id not in seen_ids:
            # It was in the ledger and it is not in the folder any more, and it never
            # finished. Re-queueing would be pointless and moving it on would be a lie.
            report.vanished += 1
            report.add(
                "vanished",
                row.item_id,
                row.name,
                f"unfinished ({row.state}) and no longer in the recordings folder — it was moved "
                "or deleted before it could be transcribed. The audio may be the only copy",
                needs_a_person=True,
            )
            continue

        if not row.lease_expired(clock):
            report.in_hand += 1
            continue

        if row.item_id not in idle_baseline:
            # First seen by this very sweep, so it has had no chance to be worked yet.
            report.left_alone += 1
            continue

        idle_for = _idle_seconds(row, idle_baseline[row.item_id], clock)
        if idle_for is not None and idle_for < stale_after:
            report.left_alone += 1
            continue

        idle_text = "for an unknown length of time" if idle_for is None else f"for {_duration(idle_for)}"
        if row.attempts >= max_attempts:
            report.quarantined += 1
            reason = (
                f"stuck in {row.state} after {row.attempts} attempt(s) and found again by the "
                f"nightly sweep {idle_text} later. Last error: {row.last_error or 'none recorded'}"
            )
            report.add("quarantined", row.item_id, row.name, reason, needs_a_person=True)
            if not dry_run:
                ledger.quarantine(row.item_id, reason)
            continue

        report.requeued += 1
        reason = (
            f"the nightly sweep found it still in {row.state} with no live claim, untouched "
            f"{idle_text}; re-queued from the start"
        )
        report.add("re-queued", row.item_id, row.name, reason, needs_a_person=True)
        if not dry_run:
            ledger.requeue(row.item_id, reason, state=State.DISCOVERED)


def _check_finished_rows(ledger: Ledger, report: SweepReport) -> None:
    """Catch the failure the incumbent has: a recording marked done with outputs missing."""
    for row in ledger.rows_in_state(State.DONE):
        if row.outputs_present:
            continue
        report.incomplete_outputs += 1
        missing = [
            label
            for label, value in (
                ("transcript", row.transcript_name),
                ("summary", row.summary_name),
                ("actions", row.actions_name),
            )
            if not value
        ]
        report.add(
            "outputs-missing",
            row.item_id,
            row.name,
            "marked finished, but the ledger has no " + ", ".join(missing)
            + " for it. It will not be archived, and it needs checking by hand",
            needs_a_person=True,
        )


def _idle_seconds(row: Row, last_touched: str | None, clock: float) -> float | None:
    """How long the row has gone without moving a state, before this sweep began looking."""
    stamp = parse_stamp(last_touched) or parse_stamp(row.discovered_at)
    return None if stamp is None else max(0.0, clock - stamp)


def _duration(seconds: float) -> str:
    if seconds < 5400:
        return f"{int(round(seconds / 60))} minutes"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


# --------------------------------------------------------------------------- schedule


def should_run(config: Any, ledger: Ledger, *, now: float | None = None) -> bool:
    """True once per local day, from the configured hour onwards.

    Anchored on a mark in the ledger rather than on a timer, so a restart at 01:30 does not
    skip the night's sweep and a restart at 01:00 does not run it twice.
    """
    clock = time.time() if now is None else now
    moment = local_now(config, clock)
    if moment.hour < int(getattr(config, "sweep_hour", 1) or 0):
        return False
    if ledger.cursor_get(SWEEP_DAY_MARK) == moment.date().isoformat():
        return False
    last_attempt = parse_stamp(ledger.cursor_get(SWEEP_ATTEMPT_MARK))
    if last_attempt is not None and (clock - last_attempt) < RETRY_AFTER_S:
        return False
    return True


def mark_run(config: Any, ledger: Ledger, *, now: float | None = None) -> None:
    ledger.cursor_set(SWEEP_DAY_MARK, local_now(config, now).date().isoformat())
