"""The group view: eight copies, one email to whoever is watching them.

Each person runs their own copy against their own drive. That is the right shape — a
recording never leaves the drive of whoever made it — and it leaves exactly one hole: when
somebody's copy stops working, the only person told is the one person whose record does not
suffer for it. Sipho's transcriber dying is Sipho's email, and Sipho is on site.

So each copy drops one small file into a shared folder every morning, and whichever copy is
configured as the admin's reads all of them and sends one email: a line per person, and —
the line that matters — who has not checked in.

**COUNTS ONLY. NEVER A NAME, NEVER A WORD OF WHAT WAS SAID.**

This is the rule the rest of this file exists to keep, and it is not a preference. The
review page is built so that one person cannot read another's held passages, including the
principal, because a staff member who finds the boss reads their held words stops keeping a
folder — and then the recordings are gone, which is the loss this whole service exists to
cure. A status file naming recordings would walk around that from the side: "Sipho, 3
stopped: DISCIPLINARY HEARING NOTES.m4a" tells the reader everything the gate was built to
withhold.

What a status file carries is therefore numbers, a name for the copy, and a timestamp.
:func:`status_of` is the only thing that builds one, :data:`_ALLOWED_KEYS` is the only shape
it may have, and :func:`_check_no_prose` refuses to write a file carrying a string that is
not one of those. Three layers for one rule, because the failure is silent and permanent.

**Nothing here can stop a personal email.** Every call is wrapped: a shared folder that has
moved, a drive that refuses, a status file written by a newer version — each of those costs
the group section and nothing else. The personal digest is the load-bearing one; this is a
convenience laid on top, and a convenience that can break the thing it decorates is a bug.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import utc_now_iso
from .sweep import parse_stamp

log = logging.getLogger(__name__)

__all__ = [
    "GroupReport",
    "PeerStatus",
    "STATUS_PREFIX",
    "is_admin",
    "read_statuses",
    "render_group_email",
    "status_filename",
    "status_of",
    "write_status",
]

#: Every status file starts with this, so the folder can be listed without guessing and a
#: file somebody else drops in there is ignored rather than parsed.
STATUS_PREFIX = "status-"

#: The only keys a status file may carry. A key not on this list is dropped on read as well
#: as refused on write: a newer copy writing a field this version does not know must not
#: break the group email, and must not smuggle anything past the no-prose check either.
_ALLOWED_KEYS = frozenset({
    "instance", "day", "written_at", "version",
    "arrived", "done", "failed", "in_flight", "silence",
    "held_pending", "spend_day_usd", "spend_month_usd", "unpriced",
})

#: Bumped when the shape changes. Read but not enforced: a file from a newer copy is used
#: for the keys this version understands rather than discarded, because a half-reported
#: group is better than a group email that vanishes after one person upgrades.
VERSION = 1

#: A status value may be a number, or one of these short identifiers. Anything else is
#: prose, and prose is how a recording's name would get in here.
_MAX_NAME = 60


@dataclass
class PeerStatus:
    """One copy's morning, as it reported it. Numbers and a name, by design."""

    instance: str = ""
    day: str = ""
    written_at: str = ""
    arrived: int = 0
    done: int = 0
    failed: int = 0
    in_flight: int = 0
    silence: int = 0
    held_pending: int = 0
    spend_day_usd: float = 0.0
    spend_month_usd: float = 0.0
    unpriced: bool = False
    #: Filled in by the reader, not the writer: how stale this file is by the time the group
    #: email is built. A copy cannot report its own silence, which is the entire point.
    hours_old: float = 0.0

    @property
    def needs_a_person(self) -> bool:
        return bool(self.failed) or self.in_flight > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "day": self.day,
            "written_at": self.written_at,
            "version": VERSION,
            "arrived": self.arrived,
            "done": self.done,
            "failed": self.failed,
            "in_flight": self.in_flight,
            "silence": self.silence,
            "held_pending": self.held_pending,
            "spend_day_usd": round(self.spend_day_usd, 4),
            "spend_month_usd": round(self.spend_month_usd, 4),
            "unpriced": bool(self.unpriced),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PeerStatus | None":
        """A status file, read defensively. None when it is not one."""
        name = str(raw.get("instance") or "").strip()
        if not name or len(name) > _MAX_NAME:
            return None
        return cls(
            instance=name,
            day=str(raw.get("day") or "")[:10],
            written_at=str(raw.get("written_at") or "")[:32],
            arrived=_whole(raw.get("arrived")),
            done=_whole(raw.get("done")),
            failed=_whole(raw.get("failed")),
            in_flight=_whole(raw.get("in_flight")),
            silence=_whole(raw.get("silence")),
            held_pending=_whole(raw.get("held_pending")),
            spend_day_usd=_money(raw.get("spend_day_usd")),
            spend_month_usd=_money(raw.get("spend_month_usd")),
            unpriced=bool(raw.get("unpriced")),
        )


def _whole(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _money(value: Any) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if n > 0 else 0.0


def status_filename(instance: str) -> str:
    """The one name this copy ever writes. Stable, so it overwrites rather than accumulates.

    Derived from the instance name with everything but letters, digits, dash and underscore
    removed — a name with a slash in it would otherwise address a different folder, and a
    name with a space in it is a different file every time somebody edits the setting.
    """
    safe = "".join(c for c in (instance or "").lower() if c.isalnum() or c in "-_")
    return f"{STATUS_PREFIX}{safe or 'unnamed'}.json"


def status_of(config: Any, *, counts: Any, spend: Any = None, held: Any = None) -> PeerStatus:
    """This copy's status, built ONLY from counts.

    The single place a status is constructed, so the no-name rule has one place to hold. It
    takes whole report objects and reads numbers off them; it never reaches for a name, a
    reason, a filename or a passage, and the reports it is handed carry all four.
    """
    return PeerStatus(
        instance=str(getattr(config, "instance_name", "") or "").strip(),
        day=str(getattr(counts, "day", "") or ""),
        written_at=utc_now_iso(),
        arrived=_whole(getattr(counts, "discovered", 0)),
        done=_whole(getattr(counts, "done", 0)),
        failed=_whole(getattr(counts, "quarantined", 0)),
        in_flight=_whole(getattr(counts, "in_flight", 0)),
        silence=_whole(getattr(counts, "skipped_empty", 0)),
        held_pending=_whole(getattr(held, "pending", 0)) if held is not None else 0,
        spend_day_usd=_money(getattr(spend, "day_usd", 0.0)) if spend is not None else 0.0,
        spend_month_usd=_money(getattr(spend, "month_usd", 0.0)) if spend is not None else 0.0,
        unpriced=bool(getattr(spend, "unpriced", ()) if spend is not None else ()),
    )


def _check_no_prose(payload: Mapping[str, Any]) -> None:
    """Refuse to write anything but numbers, the copy's name, and stamps.

    The last of the three layers, and the only one that would catch a mistake made in a
    later edit of this file. It is a hard failure rather than a scrub: a status file that
    quietly dropped a field would be a group email that quietly stopped reporting one.
    """
    extra = set(payload) - _ALLOWED_KEYS
    if extra:
        raise ValueError(
            f"a status file may not carry {', '.join(sorted(extra))}. Only counts, the "
            "copy's own name and its timestamps go in the group folder — see the header of "
            "group.py for why a recording's name must never appear here."
        )
    for key in ("instance", "day", "written_at"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > _MAX_NAME:
            raise ValueError(f"{key} must be a short string, not {value!r}")
    for key, value in payload.items():
        if key in ("instance", "day", "written_at"):
            continue
        if isinstance(value, str):
            raise ValueError(
                f"{key} carries the text {value!r}. Every field but the copy's name and its "
                "stamps is a number: a string here is how a recording's name reaches a "
                "folder other people can read."
            )


def write_status(config: Any, status: PeerStatus, *, client: Any) -> bool:
    """Drop this copy's status into the shared folder. True when it landed.

    Returns rather than raises on every failure. This is called after the personal email has
    already been sent, and nothing about the group view is worth a person not being told
    what happened to their own recordings.
    """
    folder = str(getattr(config, "group_folder_id", "") or "").strip()
    if not folder or not status.instance:
        return False
    payload = status.to_dict()
    try:
        _check_no_prose(payload)
    except ValueError:
        # Never swallowed quietly: this is the rule the file exists to keep, and a version
        # of this code that broke it must be loud about it.
        log.exception("refusing to write a status file that carries more than counts")
        return False
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    try:
        client.upload_small(folder, status_filename(status.instance), body)
    except Exception:  # noqa: BLE001 - a shared folder is not worth a failed morning
        log.warning("could not write this copy's status to the group folder", exc_info=True)
        return False
    return True


def read_statuses(config: Any, *, client: Any, now: float | None = None) -> list[PeerStatus]:
    """Every copy's status file, oldest report first. Never raises."""
    folder = str(getattr(config, "group_folder_id", "") or "").strip()
    if not folder:
        return []
    clock = time.time() if now is None else now
    try:
        children = client.list_children(folder)
    except Exception:  # noqa: BLE001
        log.warning("could not list the group folder", exc_info=True)
        return []

    out: list[PeerStatus] = []
    for item in children:
        name = str(getattr(item, "name", "") or "")
        if not name.startswith(STATUS_PREFIX) or not name.endswith(".json"):
            continue
        try:
            raw = client.read_small(getattr(item, "item_id", ""))
            doc = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - one unreadable file must not lose the other seven
            log.warning("a status file in the group folder could not be read: %s", name)
            continue
        if not isinstance(doc, Mapping):
            continue
        status = PeerStatus.from_dict(doc)
        if status is None:
            continue
        stamp = parse_stamp(status.written_at)
        status.hours_old = max(0.0, (clock - stamp) / 3600.0) if stamp else 0.0
        out.append(status)
    out.sort(key=lambda s: s.instance.lower())
    return out


def is_admin(config: Any) -> bool:
    """Whether this copy is the one that sends the group email.

    A copy is the admin because it was given somebody to send it to, not because of who
    owns its drive. So the role is moved by editing one setting on one copy, and two copies
    both configured would send two group emails rather than none — the visible failure
    rather than the silent one.
    """
    return bool(tuple(getattr(config, "group_admin_to", ()) or ()))


@dataclass
class GroupReport:
    """Everyone's morning, in one place."""

    day: str = ""
    peers: tuple[PeerStatus, ...] = ()
    silent_after_hours: int = 36

    @property
    def silent(self) -> tuple[PeerStatus, ...]:
        """Copies whose last status is older than the threshold, or undated.

        The reason this whole file exists. A copy that has stopped running stops writing,
        and stops being able to tell anybody so.
        """
        return tuple(p for p in self.peers
                     if not p.written_at or p.hours_old > self.silent_after_hours)

    @property
    def total_arrived(self) -> int:
        return sum(p.arrived for p in self.peers)

    @property
    def total_done(self) -> int:
        return sum(p.done for p in self.peers)

    @property
    def total_failed(self) -> int:
        return sum(p.failed for p in self.peers)

    @property
    def month_usd(self) -> float:
        return sum(p.spend_month_usd for p in self.peers)

    @property
    def day_usd(self) -> float:
        return sum(p.spend_day_usd for p in self.peers)

    @property
    def any_unpriced(self) -> bool:
        return any(p.unpriced for p in self.peers)


def subject_for_group(report: GroupReport) -> str:
    """The whole message, in the subject line, the way the personal one works.

    Silence first and by name. A stopped copy outranks a failed recording because a failure
    is visible in somebody's own email and silence is visible in nobody's.
    """
    if not report.peers:
        return "Recordings, everyone: no copy has reported yet"
    silent = report.silent
    if silent:
        who = ", ".join(p.instance for p in silent[:3])
        more = f" and {len(silent) - 3} more" if len(silent) > 3 else ""
        return f"⚠ Recordings, everyone: NO WORD FROM {who}{more}"
    failed = report.total_failed
    if failed:
        return (f"Recordings, everyone: {report.total_done} done, "
                f"{failed} STOPPED across {len(report.peers)} people")
    return (f"Recordings, everyone: all {report.total_done} done, "
            f"{len(report.peers)} people reporting")


def render_group_email(report: GroupReport, *, priced_on: str = "") -> str:
    """One email about everybody. Reports; decides nothing; names no recording."""
    rule = "-" * 62
    lines = [subject_for_group(report), ""]
    lines.append(f"For {report.day}. One line per person, from the status each copy wrote.")
    lines.append("")

    silent = report.silent
    if silent:
        lines += ["NO WORD FROM", rule]
        for peer in silent:
            when = (f"last heard {peer.hours_old:,.0f} hours ago"
                    if peer.written_at else "never reported")
            lines.append(f"  ! {peer.instance:<20} {when}")
        lines.append("")
        for chunk in _wrap(
            "A copy that has stopped running also stops reporting, so this is the one thing "
            "in this email nobody else can see. Their own morning email will not arrive "
            "either. Check the machine that copy runs on before anything else here."
        ):
            lines.append(f"  {chunk}")
        lines.append("")

    lines += ["EVERYONE", rule]
    lines.append(f"  {'person':<16} {'arrived':>8} {'done':>6} {'stopped':>8} "
                 f"{'waiting':>8} {'to approve':>11}   {'as at':<10}")
    for peer in report.peers:
        stale = peer in silent
        mark = "!" if stale or peer.needs_a_person else " "
        # A silent copy's numbers are from whenever it last spoke, and saying so on the row
        # is the difference between "Sipho is fine" and "Sipho's last word was Monday".
        as_at = f"{peer.day} — STALE" if stale and peer.day else ("never" if stale else "")
        lines.append(f"{mark} {peer.instance:<16} {peer.arrived:>8} {peer.done:>6} "
                     f"{peer.failed:>8} {peer.in_flight:>8} {peer.held_pending:>11}   {as_at}")
    # The totals exclude nobody, but they are labelled so a reader knows a stale row is in
    # them. Quietly dropping the silent copies would make the group look smaller than it is.
    lines.append(f"  {'':<16} {report.total_arrived:>8} {report.total_done:>6} "
                 f"{report.total_failed:>8}"
                 + ("   (includes the stale rows above)" if silent else ""))
    lines.append("")

    if report.month_usd or report.day_usd:
        lines += ["WHAT THE AI PASS COST, EVERYONE", rule]
        lines.append(f"  yesterday          ${report.day_usd:,.2f}")
        lines.append(f"  this month so far  ${report.month_usd:,.2f}")
        if report.any_unpriced:
            lines.append("")
            for chunk in _wrap(
                "AT LEAST ONE COPY IS USING A MODEL THE PRICE LIST DOES NOT KNOW, so both "
                "figures are undercounts. The copy in question says which in its own email."
            ):
                lines.append(f"  {chunk}")
        elif priced_on:
            lines.append(f"  priced from the published rates as at {priced_on}")
        lines.append("")

    for chunk in _wrap(
        "This email carries counts and nothing else — no recording names and no words from "
        "any recording. Held passages are counted here and readable only by the person they "
        "belong to, which is what keeps people willing to keep recording."
    ):
        lines.append(f"  {chunk}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _wrap(text: str, width: int = 74) -> list[str]:
    out: list[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out
