"""The 06:00 email. It goes out every single day, including the days when nothing is wrong.

A report that only arrives when something breaks is indistinguishable from a service that
has died. That is not a theory: it is how four days of recordings went missing without
anybody noticing, and it is the specific failure this email exists to remove. So there is
no "quiet success" path in this module. Every morning there is a message, and the subject
line carries the whole story so it can be read on a phone from the notification alone::

    Recordings: all 23 done
    Recordings: 20 done, 3 FAILED
    ⚠ Recordings: nothing arrived yesterday

The zero-arrival alert is **armed at weekends too**. A Saturday site walk is entirely
normal, and more to the point a Friday-evening failure that suppressed the weekend's
digests would not surface until Monday morning — three days of silence, which is the exact
shape of the original problem.

Failures come first, above the counts, in plain English, each with a link to the file. The
raw error is kept underneath as a technical detail rather than dropped: the plain sentence
is for reading, the technical line is for fixing.

**The subject line stays one line about the whole service; the body counts every route on
its own.** Averaged into a total, "site meetings all fine, WhatsApp broken" reads as "23
arrived, 20 done" and looks like an ordinary morning. So under the counts there is a line
per route — including a line for a route that processed nothing yesterday, because that is
the signal that used to be missing: a route quietly receiving nothing looks exactly like a
route nobody mentioned.

Three things this email never contains:

  * **A secret.** Everything rendered goes through ``Config.scrub`` before it is sent.
  * **An email address taken from anything but the configuration.** The recipient is the
    configured recipient and nothing else; every rendered line is then passed through
    ``strip_emails`` as a mechanical backstop, so an address that somehow reached a filename
    or an error message cannot ride out in the body.
  * **HTML.** Plain text, one part, no tracking, nothing to render.

And if the email cannot be sent, the heartbeat is *not* pinged. The external monitor then
sees silence and raises the alarm — which is the design working. A digest that failed
quietly would be the worst outcome available.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Mapping, Sequence

from . import logging_setup
from . import release as release_module
from . import sitebook
from .heartbeat import Heartbeat, PingResult
from .ledger import Ledger
from . import prices
from .extract import Spend
from .models import (
    DEFAULT_ROUTE,
    DigestCounts,
    Row,
    State,
    contains_email,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
    utc_now_iso,
)
from .review_page import display_name
from .sweep import local_now, parse_stamp, routes_of
from .withheld import (
    CATEGORY_PHRASE,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    Decision,
    WithheldStore,
    normalise_mode,
)

log = logging.getLogger("transcriber.digest")

__all__ = [
    "Digest",
    "RouteDigest",
    "RouteQueue",
    "QueueReport",
    "HeldSite",
    "HeldReport",
    "held_report",
    "held_store_for",
    "HeldStoreUnavailable",
    "HELD_AGE_LINE",
    "HELD_AGE_NAMED",
    "HELD_AGE_SUBJECT",
    "queue_report",
    "queue_history",
    "record_queue_depth",
    "human_duration",
    "QUEUE_DEPTH_MARK",
    "QUEUE_STALE_AFTER_S",
    "STUCK_AFTER_S",
    "WORK_DIR_MARK",
    "WORK_DIR_REFUSED_MARK",
    "credential_warnings",
    "SendResult",
    "DigestResult",
    "build",
    "send",
    "run",
    "should_run",
    "mark_run",
    "subject_for",
    "split_stopped_from_queued",
    "plain_reason",
    "DIGEST_DAY_MARK",
    "DIGEST_ATTEMPT_MARK",
]

DIGEST_DAY_MARK = "digest:last_sent_day"
DIGEST_ATTEMPT_MARK = "digest:last_attempt_at"
DIGEST_ERROR_MARK = "digest:last_error"

#: Do not retry a failed send more often than this. The digest must be persistent, not a
#: mail loop: a wrong password should not become 720 authentication failures a day.
RETRY_AFTER_S = 900.0

_RULE = "-" * 62

#: Where the queue depth from each morning is kept, so "is it growing?" can be answered
#: rather than guessed. A small JSON map of day -> how many were queued that morning.
QUEUE_DEPTH_MARK = "queue:depth_by_day"
#: Enough history to see a week-long trend and no more; this is a hint, not a metrics store.
QUEUE_HISTORY_DAYS = 14
#: A recording still queued a day after it arrived is not a busy morning any more. Under
#: ``QUEUE_STALE_HOURS`` if a deployment sets one.
QUEUE_STALE_AFTER_S = 24 * 3600.0

#: How long an unfinished recording may sit before the subject line calls it FAILED rather
#: than queued. The distinction is age, not state: eighty recordings landing at 17:00 are
#: still legitimately in hand at 06:00, while one that arrived before lunch and has not
#: moved has stopped in all but name. Six hours is comfortably longer than any real drain
#: at this volume and comfortably shorter than a working day, so a genuinely stuck file is
#: named the same morning rather than the next one.
STUCK_AFTER_S = 6 * 3600.0

#: What the worker wrote about the work directory on its last drain, and the recordings it
#: cannot start at all until a size is raised. Written by ``worker.WORK_DIR_NOTE`` and
#: ``worker.WORK_DIR_REFUSED``; read here by name rather than by import, because the worker
#: pulls in the pipeline and every engine behind it and the digest is built from a ledger
#: and nothing else. ``test_capacity_reporting`` asserts the two names have not drifted.
WORK_DIR_MARK = "worker:work_dir"
WORK_DIR_REFUSED_MARK = "worker:work_dir_refused"


# --------------------------------------------------------------------------- the queue


def human_duration(seconds: float) -> str:
    """A wait a person can read. Never "0:00:00", never a float with six decimal places."""
    total = max(0.0, float(seconds))
    if total < 90:
        return f"{int(total)} second{'' if int(total) == 1 else 's'}"
    if total < 5400:
        minutes = int(round(total / 60.0))
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    if total < 172800:
        return f"{total / 3600.0:.1f} hours"
    return f"{total / 86400.0:.1f} days"


@dataclass(frozen=True)
class RouteQueue:
    """One route's share of the work in hand."""

    name: str
    label: str = ""
    queued: int = 0
    #: Of those, the ones a worker is holding right now. The rest are waiting their turn.
    started: int = 0
    oldest_at: str = ""
    oldest_name: str = ""
    oldest_age_s: float = 0.0

    @property
    def display(self) -> str:
        label = (self.label or "").strip()
        return f"{label} ({self.name})" if label and label != self.name else self.name

    def line(self) -> str:
        if not self.queued:
            return f"{self.display}: nothing queued"
        being = f", {self.started} being worked on now" if self.started else ""
        return (
            f"{self.display}: {self.queued} queued{being}, "
            f"oldest waiting {human_duration(self.oldest_age_s)}"
        )


@dataclass(frozen=True)
class QueueReport:
    """How much work is in hand, per route, and whether it is piling up.

    This exists because a backlog and a loss look identical from outside, and that
    confusion is the whole disease this service was built to cure. "42 queued, working
    through them" and "42 missing" are the same forty-two recordings to anyone reading a
    total, and they are completely different mornings.

    Nothing here is a failure. Every recording counted below is in the ledger, with a row
    of its own, and will be transcribed. The two things that *are* worth saying out loud
    are the age of the oldest one and whether the number is bigger than it was yesterday —
    together they are the difference between "busy" and "not keeping up".
    """

    day: str = ""
    queued: int = 0
    started: int = 0
    routes: tuple[RouteQueue, ...] = ()
    oldest_at: str = ""
    oldest_name: str = ""
    oldest_route: str = ""
    oldest_age_s: float = 0.0
    previous_day: str = ""
    previous_queued: int | None = None
    #: The last few mornings' depths, oldest first, as (day, queued). Two rises in a row is
    #: a trend; one morning bigger than the last is a Tuesday.
    history: tuple[tuple[str, int], ...] = ()
    stale_after_s: float = QUEUE_STALE_AFTER_S
    #: Why the drain is pacing itself, when it is: the work directory is at its budget, or
    #: this cycle's recordings did not all fit in what is left. Empty on an ordinary day.
    #: Without this the queue section says how much is waiting and never says why nothing
    #: is moving, which reads as a service that has died rather than one that is full.
    work_dir: str = ""
    #: Recordings that cannot be started at any time, because one of them alone needs more
    #: scratch space than the whole budget. The one thing here that needs a person.
    work_dir_refused: str = ""
    #: Set when the ledger could not be counted. The digest still goes out; it says this
    #: instead of quietly printing a zero, which would read as "nothing is waiting".
    unavailable: str = ""

    @property
    def empty(self) -> bool:
        return self.queued == 0 and not self.unavailable

    @property
    def stale(self) -> bool:
        """The oldest has been waiting longer than a queue that is moving ever should."""
        return self.queued > 0 and self.oldest_age_s > self.stale_after_s

    @property
    def growing(self) -> bool:
        """Deeper than it was when the last digest went out."""
        return (
            self.queued > 0
            and self.previous_queued is not None
            and self.queued > self.previous_queued
        )

    @property
    def growing_across_days(self) -> bool:
        """Deeper every morning for three mornings running, this one included.

        One morning bigger than the last is an ordinary Tuesday — eight people record more
        on some days than others. Three in a row that each grew is arithmetic: less is
        going out than is coming in, and that gap does not close by itself.
        """
        points = [count for _, count in self.history[-2:]] + [self.queued]
        if len(points) < 3:
            return False
        return all(later > earlier for earlier, later in zip(points, points[1:]))

    @property
    def short_of_throughput(self) -> bool:
        """Not "busy" — genuinely not keeping up, and worth a person's attention."""
        return self.queued > 0 and (self.stale or self.growing_across_days)

    def headline(self) -> str:
        if self.unavailable:
            return "The queue could not be counted this morning."
        if not self.queued:
            return "Nothing is queued: everything that has arrived has been dealt with."
        being = f" ({self.started} being worked on right now)" if self.started else ""
        return (
            f"{self.queued} recording(s) queued and being worked through{being}. "
            f"Nothing here is lost or missing: each one has a row in the ledger and will "
            f"be transcribed."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "queued": self.queued,
            "started": self.started,
            "oldest_at": self.oldest_at,
            "oldest_name": self.oldest_name,
            "oldest_route": self.oldest_route,
            "oldest_age_s": round(self.oldest_age_s, 1),
            "oldest_age": human_duration(self.oldest_age_s) if self.queued else "",
            "previous_day": self.previous_day,
            "previous_queued": self.previous_queued,
            "stale": self.stale,
            "work_dir": self.work_dir,
            "work_dir_refused": self.work_dir_refused,
            "growing": self.growing,
            "growing_across_days": self.growing_across_days,
            "history": [list(point) for point in self.history],
            "unavailable": self.unavailable,
            "routes": [
                {
                    "route": r.name,
                    "label": r.label,
                    "queued": r.queued,
                    "started": r.started,
                    "oldest_at": r.oldest_at,
                    "oldest_age_s": round(r.oldest_age_s, 1),
                }
                for r in self.routes
            ],
        }

    def lines(self) -> list[str]:
        """The section as it is read: the count first, then the routes, then the warning."""
        out: list[str] = []
        if self.unavailable:
            for chunk in _wrap(
                f"The queue could not be counted this morning: {self.unavailable}. That is a "
                f"fault in this report, not in the recordings — run `transcriber status`."
            ):
                out.append(f"  {chunk}")
            return out

        for chunk in _wrap(self.headline()):
            out.append(f"  {chunk}")

        # Before the per-route breakdown, because it is the answer to the question the
        # breakdown provokes: "why is that number not going down?". Both of these are the
        # worker's own words from its last drain, so the email says what the log says.
        if self.work_dir:
            out.append("")
            for chunk in _wrap(f"Why the queue is moving slowly: {self.work_dir}"):
                out.append(f"  {chunk}")
        if self.work_dir_refused:
            out.append("")
            for chunk in _wrap(
                "One or more recordings cannot be started at all until a size is raised, "
                "however long they wait: " + self.work_dir_refused
            ):
                out.append(f"  {chunk}")

        if not self.queued:
            return out

        out.append("")
        for entry in self.routes:
            if entry.queued:
                out.append(f"    {entry.line()}")
        quiet = [entry.display for entry in self.routes if not entry.queued]
        if quiet:
            out.append(f"    nothing queued on: {', '.join(quiet)}")
        out.append("")

        if self.oldest_name:
            out.append(
                f"  Longest in the queue: {self.oldest_name}, first seen "
                f"{human_duration(self.oldest_age_s)} ago."
            )
        if self.stale:
            for chunk in _wrap(
                f"That is longer than anything should sit in this queue "
                f"(over {human_duration(self.stale_after_s)}), so the queue is not moving "
                f"as fast as recordings are arriving."
            ):
                out.append(f"  {chunk}")
        if self.previous_queued is not None:
            direction = (
                "longer than" if self.queued > self.previous_queued
                else "shorter than" if self.queued < self.previous_queued
                else "the same length as"
            )
            out.append(
                f"  The queue was {self.previous_queued} when the {self.previous_day} email "
                f"went out, so it is {direction} it was."
            )
        if self.growing_across_days:
            out.append(
                "  It has grown every morning for three mornings running."
            )
        if self.short_of_throughput:
            out.append("")
            for chunk in _wrap(
                "This is the one thing in this section worth acting on: the queue is not "
                "just busy, it is not keeping up. Recordings are arriving faster than they "
                "are being transcribed, so the wait gets longer every day until either the "
                "number of recordings drops or the service is given more capacity — more "
                "workers, or a higher engine limit. Nothing is being lost while that is "
                "true: every recording here is in the ledger and will be transcribed. It "
                "is only getting slower."
            ):
                out.append(f"  {chunk}")
        return out


def queue_history(ledger: Ledger) -> dict[str, int]:
    """How deep the queue was on each of the last few mornings. Never raises."""
    try:
        raw = ledger.cursor_get(QUEUE_DEPTH_MARK) or ""
    except Exception as exc:  # noqa: BLE001 - the digest must be sendable from a sick ledger
        log.warning("could not read the queue history: %s", exc)
        return {}
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        log.warning("the stored queue history is not readable JSON; starting a new one")
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, int] = {}
    for day, value in loaded.items():
        try:
            out[str(day)] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def record_queue_depth(ledger: Ledger, day: str, queued: int) -> None:
    """Write this morning's depth down, so tomorrow's email can say whether it grew.

    Called by :func:`run` and not by :func:`build`: building a digest is a read, and a
    ``status`` or a dry run that quietly rewrote the history would make "it is growing"
    depend on who looked.
    """
    if not day:
        return
    history = queue_history(ledger)
    history[str(day)] = int(queued)
    for stale in sorted(history)[:-QUEUE_HISTORY_DAYS]:
        history.pop(stale, None)
    try:
        ledger.cursor_set(QUEUE_DEPTH_MARK, json.dumps(history, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - nothing about the digest depends on this write
        log.warning("could not record the queue depth for %s: %s", day, exc)


def queue_report(
    config: Any,
    ledger: Ledger,
    *,
    day: str = "",
    now: float | None = None,
    routes: Sequence[Any] | None = None,
) -> QueueReport:
    """Count the work in hand, per route, from the ledger as it stands right now.

    Unfinished means exactly what the ledger means by it: a row that is not DONE, not
    QUARANTINED and not verified silence. Those rows are the queue — the recordings this
    service has written down and not yet finished — and counting them is the only honest
    answer to "where are my recordings?".
    """
    clock = time.time() if now is None else now
    stale_after = _stale_after(config)
    try:
        rows: Sequence[Row] = ledger.unfinished()
    except Exception as exc:  # noqa: BLE001 - the digest must be sendable from a sick ledger
        log.warning("could not count the queue: %s", exc)
        return QueueReport(day=day, stale_after_s=stale_after, unavailable=f"{type(exc).__name__}: {exc}")

    known: list[Any] = list(routes) if routes is not None else (
        list(routes_of(config)) if config is not None else []
    )
    labels = {
        str(getattr(r, "name", "")): str(getattr(r, "label", "") or "") for r in known
    }
    order = [str(getattr(r, "name", "")) for r in known if getattr(r, "name", "")]

    queued: dict[str, int] = {name: 0 for name in order}
    started: dict[str, int] = {name: 0 for name in order}
    oldest: dict[str, Row] = {}
    for row in rows:
        name = str(getattr(row, "route", "") or DEFAULT_ROUTE)
        queued[name] = queued.get(name, 0) + 1
        started.setdefault(name, 0)
        if not row.lease_expired(clock):
            started[name] += 1
        # unfinished() is ordered oldest first, so the first row seen for a route is its
        # oldest and nothing here has to sort or compare timestamps to find it.
        oldest.setdefault(name, row)
        if name not in order:
            order.append(name)

    def age_of(row: Row | None) -> float:
        stamp = parse_stamp(getattr(row, "discovered_at", "") if row else "")
        return max(0.0, clock - stamp) if stamp is not None else 0.0

    entries = tuple(
        RouteQueue(
            name=name,
            label=labels.get(name, ""),
            queued=queued.get(name, 0),
            started=started.get(name, 0),
            oldest_at=str(getattr(oldest.get(name), "discovered_at", "") or ""),
            oldest_name=str(getattr(oldest.get(name), "name", "") or ""),
            oldest_age_s=age_of(oldest.get(name)),
        )
        for name in order
    )

    first = rows[0] if rows else None
    history = queue_history(ledger)
    work_dir = _mark(ledger, WORK_DIR_MARK)
    refused = _mark(ledger, WORK_DIR_REFUSED_MARK)
    earlier = [d for d in sorted(history) if not day or d < day]
    previous_day = earlier[-1] if earlier else ""
    trend = tuple((d, history[d]) for d in earlier[-QUEUE_HISTORY_DAYS:])
    return QueueReport(
        day=day,
        queued=len(rows),
        started=sum(started.values()),
        routes=entries,
        oldest_at=str(getattr(first, "discovered_at", "") or ""),
        oldest_name=str(getattr(first, "name", "") or ""),
        oldest_route=str(getattr(first, "route", "") or DEFAULT_ROUTE) if first else "",
        oldest_age_s=age_of(first),
        previous_day=previous_day,
        previous_queued=history.get(previous_day) if previous_day else None,
        history=trend,
        stale_after_s=stale_after,
        work_dir=work_dir,
        work_dir_refused=refused,
    )


def _mark(ledger: Ledger, name: str) -> str:
    """One of the worker's notes, or '' — never an exception, and never a stale guess.

    The worker rewrites both of these on every drain and clears them when there is nothing
    to say, so what is read here is always about the last cycle rather than about the worst
    afternoon this month.
    """
    try:
        return str(ledger.cursor_get(name) or "").strip()
    except Exception as exc:  # noqa: BLE001 - the digest must be sendable from a sick ledger
        log.warning("could not read %s: %s", name, exc)
        return ""


def _stale_after(config: Any) -> float:
    """How long a queued recording may wait before the wait itself is the news."""
    try:
        hours = float(getattr(config, "queue_stale_hours", 0) or 0)
    except (TypeError, ValueError):
        hours = 0.0
    return hours * 3600.0 if hours > 0 else QUEUE_STALE_AFTER_S


# ---------------------------------------------------------------- the held-passage queue

#: How the held queue escalates, in days. The rule he gave is that it must get **more
#: specific, not just louder** — a warning that says the same thing more loudly every
#: morning becomes wallpaper by the third one, and then the queue is invisible again while
#: still technically being reported. So each step adds a *fact*: first the age, then the
#: oldest one by name, then the subject line — which is the only escalation left that
#: reaches him before he opens anything.
HELD_AGE_LINE = 1        # the age of the oldest is stated
HELD_AGE_NAMED = 3       # the oldest is named: its site, whose list it is, its reference
HELD_AGE_SUBJECT = 7     # it reaches the subject line of the email
#
# All three are sentences and nothing else. There is no age at which a passage is released,
# refused, discarded or hidden from this report, and no threshold in this module produces
# anything but words — which is what makes "nothing is decided for him on a timer, ever"
# a property of the code rather than a promise about it.


class HeldStoreUnavailable(Exception):
    """The held-passage store exists but could not be opened.

    Distinct from "there is no store", which is an ordinary and honest state on a service
    that has never held anything. This one means the queue is unreadable, and the morning
    email must say so rather than print a zero.
    """


def held_store_for(config: Any) -> WithheldStore | None:
    """Open the store of held passages, or ``None`` when this deployment has never used one.

    ``None`` is only ever returned for a service that has genuinely never classified a
    recording — the gate is off *and* there is no database on disk. A gate switched off
    after it has held something must still report what is waiting, because switching the
    classifier off does not answer anybody's held passage, and a queue that vanished from
    the morning email the day the mode changed would be the exact silent-emptying failure
    this whole design is built against.
    """
    path = str(getattr(config, "held_store_path", "") or "")
    if not path:
        path = WithheldStore.path_beside(str(getattr(config, "ledger_path", ":memory:")))
    mode = normalise_mode(getattr(config, "gate_mode", MODE_SHADOW))
    if mode == MODE_OFF and path not in (":memory:", "") and not os.path.exists(path):
        return None
    try:
        return WithheldStore(path, scrub=getattr(config, "scrub", None))
    except Exception as exc:  # noqa: BLE001 - the morning email goes out regardless
        log.warning("the held-passage store could not be opened: %s", exc)
        # The caller has to be able to tell this apart from "there is no store because
        # nothing was ever held". Both used to arrive as None, so a store that could not be
        # opened — a corrupt file, a permission that changed, a disk fault — rendered in the
        # morning email as "nothing has been held", which is the one sentence this design
        # exists to prevent anybody reading when it is not true. The words are still safe
        # (nothing was read), but the queue's existence stopped being reported, and Jay does
        # not read the log this warning goes to.
        raise HeldStoreUnavailable(f"{type(exc).__name__}: {exc}") from exc


@dataclass(frozen=True)
class HeldSite:
    """One site's share of the held queue. A count and an age — never a word."""

    site: str
    count: int = 0
    recordings: int = 0
    oldest_age_days: int = 0

    def line(self) -> str:
        name = self.site or "no site named"
        waiting = (
            f", oldest waiting {self.oldest_age_days} day{'' if self.oldest_age_days == 1 else 's'}"
            if self.oldest_age_days
            else ", all of them from today"
        )
        return f"{name}: {self.count} waiting{waiting}"


#: The share of classified recordings the model must actually have read before the shadow
#: measurement is allowed to describe itself as ready to arm. Not a threshold that decides
#: anything — nothing in this module can release, discard or arm — only the line between
#: printing "it is ready" and printing "this number is not yet real". Set high because the
#: failure it guards is silent: a gate whose classifier stopped running reports a small
#: held fraction, which is exactly what a well-tuned gate reports.
_MEASUREMENT_IS_REAL_ABOVE = 0.8


@dataclass(frozen=True)
class HeldReport:
    """The held queue as the morning email says it — and as it must never say it.

    Two rules shape every field on this object:

    **Counts and sites, never words.** A staff member reviews their own held passages, and
    James sees how many and where. That is not politeness: staff record voluntarily and can
    stop keeping a folder at all, and one who works out that the boss reads the held text
    from their calls has an obvious and rational response. The recordings would then be gone
    entirely, which is the original loss arriving as a social effect rather than a technical
    one, and it cannot be fixed in code afterwards. So the only text on this object is a
    category's own phrase — "a staff matter", "a legal matter" — chosen from a fixed list of
    six, plus the classifier's public subject for passages that are *his own* to read.

    **Nothing here is a deadline.** Age is reported and never acted on. There is no field
    that expires, no threshold that releases, and no count that commits an overflow. Under
    fatigue the thing that must never quietly empty is the gate, and the way that is
    guaranteed is that this module can only ever produce sentences.
    """

    mode: str = MODE_SHADOW
    #: True when the gate is actually withholding. False in shadow, which is what ships.
    pending: int = 0
    recordings: int = 0
    oldest_age_days: int = 0
    oldest_site: str = ""
    oldest_ref: str = ""
    oldest_reviewer: str = ""
    oldest_phrase: str = ""
    oldest_recording: str = ""
    oldest_is_his: bool = False
    by_site: tuple[HeldSite, ...] = ()
    by_reviewer: tuple[tuple[str, int], ...] = ()
    by_category: tuple[tuple[str, int], ...] = ()
    #: Pending passages with no reviewer recorded, which cannot be grouped. Said out loud
    #: rather than left out of the totals.
    unassigned: int = 0
    #: The shadow-mode measurement — what the classifier *would* have held, over how many
    #: recordings, and what fraction of the text that came to.
    would_have_held: int = 0
    classified: int = 0
    with_a_hold: int = 0
    fraction_of_text: float = 0.0
    fraction_of_recordings: float = 0.0
    spans_per_day: float = 0.0
    days_measured: int = 0
    shadow_by_category: tuple[tuple[str, int], ...] = ()
    #: How many of those recordings the model actually answered the sensitivity question
    #: on, and how many fell back to the mechanical rules. Without this pair, a gate whose
    #: classifier is not running reads in this email exactly like a fortnight of clean
    #: recordings — and both read as "ready to arm". Four of the six held categories are
    #: invisible to the rules, so the difference is most of the gate.
    model_read: int = 0
    rules_only: int = 0
    #: What the classifier could not stand behind, and how many recordings each applies to.
    #: Counts of a fixed set of sentences; never a word of any recording.
    classifier_notes: tuple[tuple[str, int], ...] = ()
    #: Every passage this store has ever recorded, answered or not, in any mode. It is the
    #: difference between "the gate has found nothing" and "the gate has never run", which
    #: read identically from a count of what is pending and need opposite responses.
    held_ever: int = 0
    #: Answers a person gave on the reported day. Released and refused are both progress.
    released_today: int = 0
    refused_today: int = 0
    #: Released passages whose words have not been written into the record yet.
    outstanding: release_module.Outstanding = field(default_factory=release_module.Outstanding)
    review_url: str = ""
    unavailable: str = ""

    @property
    def armed(self) -> bool:
        return self.mode == MODE_ON

    @property
    def empty(self) -> bool:
        """Nothing to say at all: a service that has never classified a recording."""
        return (
            not self.unavailable
            and self.mode != MODE_ON
            and self.pending == 0
            and self.classified == 0
            and self.would_have_held == 0
            and not self.outstanding.any
        )

    @property
    def needs_a_person(self) -> bool:
        return self.pending > 0 or self.outstanding.any or bool(self.unavailable)

    @property
    def escalation(self) -> str:
        """How specific this morning's warning has to be: none | age | named | subject."""
        if not self.pending:
            return "none"
        if self.oldest_age_days >= HELD_AGE_SUBJECT:
            return "subject"
        if self.oldest_age_days >= HELD_AGE_NAMED:
            return "named"
        if self.oldest_age_days >= HELD_AGE_LINE:
            return "age"
        return "none"

    def subject_warning(self) -> str:
        """The subject-line warning, after a week. Empty before that.

        Specific rather than shouted: how many, and how long the oldest has waited. That is
        the smallest sentence that tells him something he did not know yesterday.
        """
        if self.escalation != "subject":
            return ""
        return (
            f"⚠ {self.pending} passage(s) held, oldest {self.oldest_age_days} days"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "pending": self.pending,
            "recordings": self.recordings,
            "oldest_age_days": self.oldest_age_days,
            "escalation": self.escalation,
            "by_site": [
                {"site": s.site, "count": s.count, "oldest_age_days": s.oldest_age_days}
                for s in self.by_site
            ],
            "by_category": [list(pair) for pair in self.by_category],
            "unassigned": self.unassigned,
            "would_have_held": self.would_have_held,
            "classified": self.classified,
            "with_a_hold": self.with_a_hold,
            "fraction_of_text": self.fraction_of_text,
            "fraction_of_recordings": self.fraction_of_recordings,
            "spans_per_day": self.spans_per_day,
            "days_measured": self.days_measured,
            "model_read": self.model_read,
            "rules_only": self.rules_only,
            "fraction_the_model_read": round(self.fraction_the_model_read, 4),
            "measurement_is_real": self.fraction_the_model_read >= _MEASUREMENT_IS_REAL_ABOVE,
            "classifier_notes": [list(pair) for pair in self.classifier_notes],
            "released_today": self.released_today,
            "refused_today": self.refused_today,
            "held_ever": self.held_ever,
            "outstanding": self.outstanding.count,
            "unavailable": self.unavailable,
        }

    # -- the section --------------------------------------------------------------

    def heading(self) -> str:
        if self.unavailable:
            return "HELD PASSAGES — the queue could not be read"
        if self.mode == MODE_SHADOW:
            return "THE GATE IS WATCHING AND HOLDING NOTHING"
        if self.mode == MODE_OFF:
            return "HELD PASSAGES — the gate is switched off"
        if not self.pending:
            return "HELD PASSAGES — nothing is waiting"
        return f"HELD PASSAGES — {self.pending} waiting for a person"

    def lines(self) -> list[str]:
        if self.unavailable:
            return [
                f"  {chunk}"
                for chunk in _wrap(
                    f"The held-passage queue could not be read this morning: "
                    f"{self.unavailable}. Nothing has been released and nothing has been "
                    f"discarded — this is a fault in this report. Run "
                    f"`transcriber gate --status`."
                )
            ]
        out: list[str] = []
        if self.mode == MODE_SHADOW:
            out += self._shadow_lines()
        elif self.mode == MODE_OFF:
            out += [
                f"  {chunk}"
                for chunk in _wrap(
                    "The gate is switched off: no recording is read for sensitive passages "
                    "and nothing is being held. Everything said on a recording is written "
                    "into the record as it was said."
                )
            ]
        if self.pending or self.mode == MODE_ON:
            out += self._pending_lines()
        if self.released_today or self.refused_today:
            out.append("")
            out.append(
                f"  Answered on the day reported: {self.released_today} released, "
                f"{self.refused_today} refused."
            )
        if self.outstanding.any:
            out.append("")
            for line in self.outstanding.lines():
                for chunk in _wrap(line):
                    out.append(f"  {chunk}")
        return out

    def _shadow_lines(self) -> list[str]:
        """What it *would* have held. Plainly marked as not withheld — the whole measurement.

        This is what ships. The five design passes disagreed about how much this touches by
        a factor of twenty-five, and arming a gate against an estimate is how the queue
        becomes a wall he bounces off. So the number is read off a real run before anything
        is ever withheld, and the sentence has to be unmistakable: nothing below was held
        back, and every one of these transcripts went into the record complete.
        """
        out = [
            f"  {chunk}"
            for chunk in _wrap(
                "NOTHING WAS WITHHELD. The gate is in shadow: it reads every recording and "
                "writes down what it would have held, and then holds nothing. Every "
                "transcript below went into the record complete, with nothing taken out and "
                "nothing waiting for you. This section is the measurement that has to be "
                "real before it is ever switched on."
            )
        ]
        out.append("")
        if not self.classified:
            out.append("  No recording has been read for sensitive passages yet.")
            return out
        out += [
            f"    recordings read                 {self.classified}",
            f"    of those, read by the model     {self.model_read}"
            f"  ({self.fraction_the_model_read * 100:.0f}%)",
            f"    read by the rules alone         {self.rules_only}",
            f"    of those, carrying something    {self.with_a_hold}"
            f"  ({self.fraction_of_recordings * 100:.1f}%)",
            f"    passages it would have held     {self.would_have_held}",
            f"    that is, per day                {self.spans_per_day:.1f}",
            f"    share of the words              {self.fraction_of_text * 100:.3f}%",
            f"    days measured                   {self.days_measured}",
        ]
        if self.shadow_by_category:
            out.append("")
            out.append("    what it would have held, by kind:")
            for name, count in self.shadow_by_category:
                out.append(f"      {_category_words(name)}: {count}")
        if self.classifier_notes:
            out.append("")
            out.append("  What it could not stand behind:")
            for note, count in self.classifier_notes:
                for index, chunk in enumerate(_wrap(f"{note} — on {count} recording(s)", 68)):
                    out.append(f"    {'- ' if index == 0 else '  '}{chunk}")
        out.append("")
        out += self._is_it_ready()
        return out

    @property
    def fraction_the_model_read(self) -> float:
        return (self.model_read / self.classified) if self.classified else 0.0

    def _is_it_ready(self) -> list[str]:
        """The sentence that tells him what to do with the number — or refuses to.

        "If it is small and the categories look right, it is ready" is only true when the
        classifier that produced the number was actually running. Four of the six held
        categories can only be seen by the model reading the transcript; when it did not,
        the per-day figure is small for the one reason that must never be read as reassuring.
        A gate armed against that number would let staff matters, a person's health and
        KBC's own cost-against-charge go straight into the record while the email that
        approved it said the measurement looked good.
        """
        if self.fraction_the_model_read < _MEASUREMENT_IS_REAL_ABOVE:
            return [
                f"  {chunk}"
                for chunk in _wrap(
                    f"THIS NUMBER IS NOT YET REAL, so do not switch the gate on against it. "
                    f"The model answered the sensitivity question on {self.model_read} of "
                    f"{self.classified} recordings. The rest were read by the mechanical "
                    f"rules alone, which can only see an explicit request that something not "
                    f"be written down and a bare identity or account number — not a staff "
                    f"matter, not a person's health, not what our attorney is planning, and "
                    f"not our own cost set against what we charged. A low figure here means "
                    f"the question was not asked, not that there was nothing to find. This "
                    f"needs fixing before the measurement means anything."
                )
            ]
        return [
            f"  {chunk}"
            for chunk in _wrap(
                "Read the per-day figure as the number of approvals a day switching this on "
                "would cost. If it is small and the categories look right, it is ready. If "
                "it is not, it is the classifier that needs changing, not the queue."
            )
        ]

    def _pending_lines(self) -> list[str]:
        out: list[str] = []
        if not self.pending:
            out.append("")
            if self.classified == 0 and self.held_ever == 0:
                # Armed and never used. Worth one line rather than a cheerful sentence about
                # a history that does not exist: a gate switched on against a classifier that
                # is not running looks exactly like a gate that has found nothing.
                out.append(
                    "  The gate is on and has not read a recording yet. Nothing has been "
                    "held and nothing has been kept out of the record."
                )
            else:
                out.append(
                    "  Nothing is being held: every passage the gate has held has been "
                    "answered."
                )
            return out
        out.append("")
        for chunk in _wrap(
            f"{self.pending} passage(s) from {self.recordings} recording(s) were taken out "
            f"of a transcript and are waiting for a person. They are marked in place in the "
            f"record where they were said, so nothing reads as missing — but until somebody "
            f"answers each one, the words are only in the recording and in the store."
        ):
            out.append(f"  {chunk}")

        out.append("")
        for site in self.by_site:
            out.append(f"    {site.line()}")
        if self.unassigned:
            out.append(
                f"    {self.unassigned} more with no reviewer recorded — see "
                f"`transcriber gate --status`"
            )

        if self.by_reviewer:
            out.append("")
            out.append("  Whose list each is on:")
            for who, count in self.by_reviewer:
                out.append(f"    {who}: {count}")

        # The escalation. Each step adds a fact rather than an adjective.
        if self.escalation in ("age", "named", "subject"):
            out.append("")
            out.append(
                f"  The oldest has been waiting {self.oldest_age_days} "
                f"day{'' if self.oldest_age_days == 1 else 's'}."
            )
        if self.escalation in ("named", "subject"):
            out.append(f"  {self._name_the_oldest()}")
        if self.escalation == "subject":
            out.append("")
            for chunk in _wrap(
                "This has now been waiting over a week, so it is in the subject line of "
                "this email as well. Nothing will happen to it on its own: it will not be "
                "released, it will not be discarded, and it will not stop being reported. "
                "It needs one tap of yes or no from the person whose list it is on."
            ):
                out.append(f"  {chunk}")

        out.append("")
        if self.review_url:
            out.append(f"  Answer them here: {self.review_url}")
        out.append(
            "  Or from the service host: `transcriber held list`, then "
            "`transcriber held show <reference>`."
        )
        return out

    def _name_the_oldest(self) -> str:
        """The oldest one, named as far as the person reading this may be told.

        His own passage is named fully — the recording, the reference, and the classifier's
        own short subject for it. Somebody else's is named by its site, whose list it is on
        and the *category's* phrase, never the classifier's subject: that phrase is one of
        six fixed sentences, so a staff member's held words cannot reach his screen through
        a field that was supposed to be a summary of them.
        """
        where = self.oldest_site or "no site named"
        if self.oldest_is_his:
            what = self.oldest_phrase or "something held for review"
            recording = self.oldest_recording or "a recording"
            return (
                f"It is {self.oldest_ref} on your own list — {what}, from {recording} at "
                f"{where}."
            )
        return (
            f"It is on {self.oldest_reviewer or 'somebody'}'s list, at {where}: "
            f"{self.oldest_phrase or 'something held for review'}. You see that it is "
            f"waiting, not what it says."
        )


def _category_words(name: str) -> str:
    """One of the six categories in the words the review page uses. Never an internal name."""
    return CATEGORY_PHRASE.get(name, name.replace("_", " "))


def held_report(
    config: Any,
    ledger: Ledger,
    *,
    store: WithheldStore | None = None,
    day: str = "",
    now: str = "",
    principal: str = "",
) -> HeldReport:
    """The held queue and the shadow measurement, counted without reading anybody's words.

    One walk over the pending passages is unavoidable — per-site ages and per-category
    counts do not exist anywhere else — and every record is reduced through
    :meth:`transcriber.withheld.HeldRecord.without_words` on the same line it is read, so
    nothing carrying text ever escapes this function. That is the boundary decision 6 lives
    on, and it is one line rather than a convention because a convention gets edited.
    """
    mode = normalise_mode(getattr(config, "gate_mode", MODE_SHADOW))
    owner = (principal or _principal_of(config)).strip()
    url = str(getattr(config, "gate_review_base_url", "") or "").strip()
    try:
        held = store if store is not None else held_store_for(config)
    except HeldStoreUnavailable as exc:
        return HeldReport(mode=mode, review_url=url, unavailable=str(exc))
    if held is None:
        return HeldReport(mode=mode, review_url=url)

    stamp = now or utc_now_iso()
    try:
        overview = held.overview(decision=Decision.PENDING, now=stamp)
    except Exception as exc:  # noqa: BLE001 - the morning email goes out regardless
        log.warning("the held-passage queue could not be counted: %s", exc)
        return HeldReport(mode=mode, review_url=url, unavailable=f"{type(exc).__name__}: {exc}")

    sites: dict[str, list[int]] = {}
    site_items: dict[str, set[str]] = {}
    categories: dict[str, int] = {}
    listed = 0
    oldest: Any = None
    for reviewer, count in sorted((overview.get("by_reviewer") or {}).items()):
        if not count or not reviewer or reviewer == "unassigned":
            continue
        try:
            queue = held.queue_for(reviewer, decision=Decision.PENDING)
        except Exception as exc:  # noqa: BLE001
            log.warning("one reviewer's held queue could not be read: %s", exc)
            continue
        for full in queue:
            record = full.without_words()   # the words stop here, on this line
            listed += 1
            site = record.site or "no site named"
            sites.setdefault(site, []).append(record.age_days(stamp))
            site_items.setdefault(site, set()).add(record.item_id)
            categories[record.category] = categories.get(record.category, 0) + 1
            if oldest is None or record.held_at < oldest.held_at:
                oldest = record

    by_site = tuple(
        sorted(
            (
                HeldSite(
                    site=site,
                    count=len(ages),
                    recordings=len(site_items.get(site, ())),
                    oldest_age_days=max(ages) if ages else 0,
                )
                for site, ages in sites.items()
            ),
            key=lambda s: (-s.oldest_age_days, -s.count, s.site),
        )
    )
    reviewers = tuple(
        sorted(
            (
                (display_name(who), int(count))
                for who, count in (overview.get("by_reviewer") or {}).items()
                if count and who and who != "unassigned"
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )

    measurement: Mapping[str, Any] = {}
    day_counts: Mapping[str, Any] = {}
    try:
        measurement = held.measurement()
    except Exception as exc:  # noqa: BLE001
        log.warning("the gate's measurement could not be read: %s", exc)
    try:
        stats = held.stats()
    except Exception as exc:  # noqa: BLE001
        log.warning("the held-passage store could not be summarised: %s", exc)
        stats = {}
    try:
        day_counts = held.counts_for_day(day) if day else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("the gate's counts for %s could not be read: %s", day, exc)

    owed = release_module.outstanding(held, ledger)

    pending = int(overview.get("count") or 0)
    oldest_reviewer = str(getattr(oldest, "reviewer", "") or "")
    return HeldReport(
        mode=mode,
        pending=pending,
        recordings=int(overview.get("recordings") or 0),
        oldest_age_days=int(overview.get("oldest_age_days") or 0),
        oldest_site=str(getattr(oldest, "site", "") or ""),
        oldest_ref=str(getattr(oldest, "ref", "") or ""),
        oldest_reviewer=display_name(oldest_reviewer),
        oldest_phrase=(
            str(getattr(oldest, "phrase", "") or "")
            if oldest is not None and oldest_reviewer == owner
            else _category_words(str(getattr(oldest, "category", "") or ""))
        ),
        oldest_recording=str(getattr(oldest, "source_name", "") or ""),
        oldest_is_his=bool(owner) and oldest_reviewer == owner,
        by_site=by_site,
        by_reviewer=reviewers,
        by_category=tuple(sorted(categories.items(), key=lambda pair: (-pair[1], pair[0]))),
        unassigned=max(0, pending - listed),
        would_have_held=int(measurement.get("spans") or 0),
        classified=int(measurement.get("recordings_classified") or 0),
        with_a_hold=int(measurement.get("recordings_with_a_hold") or 0),
        fraction_of_text=float(measurement.get("fraction_of_text") or 0.0),
        fraction_of_recordings=float(measurement.get("fraction_of_recordings") or 0.0),
        spans_per_day=float(measurement.get("spans_per_day") or 0.0),
        days_measured=int(measurement.get("days_measured") or 0),
        shadow_by_category=tuple(
            sorted((measurement.get("by_category") or {}).items(), key=lambda p: (-p[1], p[0]))
        ),
        model_read=int(measurement.get("recordings_the_model_read") or 0),
        rules_only=int(measurement.get("recordings_rules_only") or 0),
        classifier_notes=tuple(
            sorted((measurement.get("notes") or {}).items(), key=lambda p: (-p[1], p[0]))
        )[:6],
        held_ever=int(stats.get("holds") or 0),
        released_today=int(day_counts.get("released") or 0),
        refused_today=int(day_counts.get("refused") or 0),
        outstanding=owed,
        review_url=url,
    )


def _principal_of(config: Any) -> str:
    """Who this email is written for. The same answer the review page reaches.

    Named explicitly where the configuration says so, otherwise the first digest recipient —
    which is the address this email is already going to. Getting a different answer here
    from the one the review page gets would put a staff member's own subject line on his
    screen, so the rule is written once and read the same way in both places.
    """
    for attribute in ("gate_principal", "principal", "review_principal"):
        value = str(getattr(config, attribute, "") or "").strip()
        if value:
            return value
    recipients = tuple(getattr(config, "smtp_to", ()) or ())
    return str(recipients[0]).strip() if recipients else ""


# --------------------------------------------------------------------------- wording

#: Plain-English translations of the failure reasons this pipeline actually produces. The
#: first match wins, and the raw text is printed underneath regardless — this replaces
#: nothing, it explains.
_REASON_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("truncat", "moov", "mdat", "incomplete container", "cut off", "not complete"),
        "the recording stops part-way through. The file itself is incomplete, which normally "
        "means the phone ran out of battery or storage while it was still recording. It was "
        "deliberately not transcribed: a fragment filed as if it were the whole recording is "
        "worse than no recording at all.",
    ),
    (
        ("implausible", "words per minute", "too few words", "plausib"),
        "the transcript that came back was far too short for how long the audio runs, so it was "
        "not filed. Something went wrong in transcription rather than on site.",
    ),
    (
        ("silence", "silent"),
        "the audio contains no speech that could be found. It has been kept and marked as "
        "verified silence rather than deleted.",
    ),
    (
        ("quote", "verbatim"),
        "the analysis produced a note whose quote could not be found in the transcript, so that "
        "note was withheld. Nothing was filed that cannot be traced to something actually said.",
    ),
    (
        ("split", "duration", "reassemb"),
        "the audio had to be split for transcription and the pieces did not add back up to the "
        "full length, so it was stopped. This is the guard against a silently shortened transcript.",
    ),
    (
        ("429", "throttl", "too many requests"),
        "Microsoft OneDrive was rate-limiting us and would not hand the file over in time.",
    ),
    (
        ("401", "403", "unauthor", "forbidden", "token", "credential", "invalid_client"),
        "OneDrive refused the connection. The app's credentials have most likely expired and "
        "need renewing — nothing will process until they are.",
    ),
    (
        ("404", "not found", "notfound"),
        "the file was no longer there when we went back for it. It was moved or deleted between "
        "being noticed and being fetched.",
    ),
    (
        ("timeout", "timed out", "connection reset", "temporarily unavailable", "urlerror"),
        "the connection failed part-way through and did not recover within the retry budget.",
    ),
    (
        ("upload", "output", "write back"),
        "the transcript and its summary could not be written back to OneDrive, so nothing was "
        "filed. The recording itself is untouched.",
    ),
    (
        ("hash", "size mismatch", "incomplete download", "verify"),
        "the downloaded copy did not match what OneDrive said the file was, so it was rejected "
        "rather than transcribed from possibly damaged audio.",
    ),
    (
        ("engine", "api", "model", "rate limit"),
        "the transcription service returned an error and did not produce a transcript.",
    ),
)

_STUCK_BY_STATE: Mapping[str, str] = {
    State.DISCOVERED: "it was found in OneDrive but never picked up for processing.",
    State.CLAIMED: "a worker started on it and never finished; its claim has since lapsed.",
    State.FETCHED: "the audio was downloaded but never transcribed.",
    State.TRANSCRIBED: "it was transcribed, but the summary and the actions were never produced.",
    State.ANALYSED: "everything was produced, but the files were never written back to OneDrive.",
}


def plain_reason(failure: Mapping[str, Any]) -> str:
    """One sentence a person can act on, from whatever the ledger recorded."""
    raw = str(failure.get("reason") or "").strip()
    lowered = raw.lower()
    for needles, sentence in _REASON_RULES:
        if any(needle in lowered for needle in needles):
            return sentence
    state = str(failure.get("state") or "")
    if state in _STUCK_BY_STATE:
        return _STUCK_BY_STATE[state]
    if raw:
        return raw
    return "it did not finish, and nothing was recorded about why. That is itself worth looking at."


def stuck_after_s(config: Any = None) -> float:
    """Seconds before an unfinished recording is called stuck rather than queued."""
    raw = getattr(config, "stuck_after_hours", None) if config is not None else None
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return STUCK_AFTER_S
    return hours * 3600.0 if hours > 0 else STUCK_AFTER_S


def split_stopped_from_queued(
    failures: Sequence[Mapping[str, Any]],
    *,
    now: float | None = None,
    config: Any = None,
) -> tuple[int, int]:
    """How many have stopped, and how many are still going.

    ``failures`` carries two different things: every quarantined recording, and the day's
    unfinished ones. Reporting them as one number is what made a healthy backlog read as a
    catastrophe on a phone screen.

    Quarantined has stopped, always. An unfinished one is judged on **age**: past
    :func:`stuck_after_s` it has stopped in all but name and is named; younger than that it
    is work in hand. Age, not state, because "arrived yesterday and not finished" describes
    both a file that died at 09:00 and one of eighty that landed at 17:00.
    """
    limit = stuck_after_s(config)
    moment = time.time() if now is None else float(now)
    stopped = queued = 0
    for failure in failures:
        if str(failure.get("state")) == State.QUARANTINED:
            stopped += 1
            continue
        found = parse_stamp(str(failure.get("discovered_at") or ""))
        if found is not None and (moment - found) < limit:
            queued += 1
        else:
            # No usable timestamp means no evidence it is moving, so it is named rather
            # than quietly counted as fine.
            stopped += 1
    return stopped, queued


def subject_for(counts: DigestCounts, open_failures: int, queued: int = 0) -> str:
    """The whole message, in the line he sees on his phone before opening anything.

    **Queued is not failed, and the subject line must not say it is.** Eighty recordings
    landing at 17:00 are still in hand at 06:00; calling them FAILED on a phone screen is
    the exact confusion this service exists to remove, and it would teach him to distrust
    the one line that is supposed to be trustworthy. A recording that has stopped
    (quarantined) is a failure; one still moving through the queue is work in hand. The
    body says how old the queue is and whether it grew, which is what distinguishes a busy
    morning from a service that has fallen behind for good.

    ``open_failures`` is the stopped ones. ``queued`` is the ones still going.
    """
    if counts.discovered == 0:
        parts = []
        if open_failures:
            parts.append(f"{open_failures} still FAILED")
        if queued:
            parts.append(f"{queued} still queued")
        if parts:
            return "⚠ Recordings: nothing arrived yesterday, " + ", ".join(parts)
        return "⚠ Recordings: nothing arrived yesterday"
    if open_failures == 0 and queued == 0:
        if counts.skipped_empty:
            return f"Recordings: all {counts.discovered} done ({counts.skipped_empty} silent)"
        return f"Recordings: all {counts.discovered} done"
    tail = []
    if open_failures:
        tail.append(f"{open_failures} FAILED")
    if queued:
        tail.append(f"{queued} queued")
    return f"Recordings: {counts.done} done, " + ", ".join(tail)


# ------------------------------------------------------------------ the hand-over onwards

#: The half of the journey this service cannot see, and the slot a receipt would fill.
#:
#: "Done" is settled here by this service's own eyes: the transcript was written to OneDrive
#: and read back from OneDrive to check the bytes arrived whole. Carrying the file on into
#: the record is a separate flow that runs outside this service and holds its own access,
#: which expires like every other credential here. When it does, the record stops receiving
#: transcripts while every number in this email stays perfect — because from here everything
#: genuinely did work. That sentence has been in the deployment notes since the flow existed,
#: where it is read once; the line under the counts puts it in front of the person reading
#: this every morning.
#:
#: The honest fix is a receipt: something outside this service saying the record actually
#: holds the file. Nothing emits one yet, and where the signal should come from is a decision
#: for Jay rather than for this module, so the slot below reads an optional file and stays
#: silent when there is none. The cheapest source is ``ops/build-site-book.py`` — it already
#: reads the record's nightly build every morning, so alongside the site list it already
#: writes it could write out the transcript filenames the record currently holds and the
#: newest stamp among them. Until something does, this returns "" and the email says nothing
#: it cannot stand behind.
RECEIPT_FILE_SETTING = "record_receipts_file"


def newest_confirmed_arrival(config: Any, now: float | None = None) -> str:
    """One line about the newest transcript the record confirms it holds, or ``''``.

    Empty whenever no receipt source is configured, which today is always. **Never raises**
    and never guesses: a receipt file that is missing, unreadable or says nothing about any
    transcript reports itself as such, because a silent slot and a slot whose source broke a
    fortnight ago must not look the same — that is the identical mistake as reporting a
    perfect morning on a hand-over nobody checked.
    """
    path = str(getattr(config, RECEIPT_FILE_SETTING, "") or "").strip()
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return (
            f"the record has not reported back: {os.path.basename(path)} is not there yet, "
            f"so none of the counts above are confirmed as having reached it"
        )
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        # Only the reason, never the path. An OSError stringifies as
        # "[Errno 21] Is a directory: '/srv/…'", and this sentence goes into an
        # email — the branch above is careful for the same reason.
        return (
            f"the record's receipt could not be read "
            f"({type(exc).__name__}: {os.path.basename(path)}), so nothing is confirmed"
        )
    stamp = parse_stamp(str(raw.get("newest_at") or "")) if isinstance(raw, dict) else None
    if stamp is None:
        return (
            f"the record's receipt names no transcript at all, so nothing has been confirmed "
            f"as arriving there"
        )
    moment = time.time() if now is None else float(now)
    return (
        f"the record confirms it holds a transcript from "
        f"{human_duration(moment - stamp)} ago — the newest one it has"
    )


# --------------------------------------------------------------------------- routes


@dataclass(frozen=True)
class RouteDigest:
    """One route's day, counted on its own.

    ``configured`` false is a route the ledger has history for that is no longer in
    ``ROUTES``. Taking a route out stops it being watched and deletes nothing, so its
    recordings are still here to be reported on, and a quarantined one on it is still
    somebody's job this morning.
    """

    name: str
    label: str
    counts: DigestCounts
    enabled: bool = True
    configured: bool = True

    @property
    def display(self) -> str:
        label = (self.label or "").strip()
        return f"{label} ({self.name})" if label and label != self.name else self.name

    @property
    def open_failures(self) -> int:
        return self.counts.quarantined + self.counts.in_flight

    @property
    def needs_a_person(self) -> bool:
        return self.open_failures > 0

    def line(self) -> str:
        """One plain sentence about this route.

        A route that processed nothing says so. It is not omitted: a missing line reads as
        "fine" and means nothing of the sort, and "no recordings arrived on the WhatsApp
        route yesterday" is exactly the sentence that was missing when a folder stopped
        syncing.
        """
        counts = self.counts
        if not self.configured:
            state = " (not in the configuration any more; its history is kept)"
        elif not self.enabled:
            state = " (switched off)"
        else:
            state = ""
        if counts.quarantined or counts.in_flight:
            parts = [f"{counts.discovered} arrived", f"{counts.done} done"]
            if counts.quarantined:
                parts.append(f"{counts.quarantined} STOPPED FOR YOU")
            if counts.in_flight:
                parts.append(f"{counts.in_flight} still in progress")
            body = ", ".join(parts)
        elif counts.discovered == 0:
            body = "nothing arrived"
        elif counts.skipped_empty:
            body = f"all {counts.discovered} done ({counts.skipped_empty} silent)"
        else:
            body = f"all {counts.discovered} done"
        return f"{self.display}{state}: {body}"


def _counts_for(ledger: Ledger, day: str, route: str) -> DigestCounts:
    """One route's counts, or an empty day. A route the ledger cannot answer for is not
    allowed to stop the morning email going out at all."""
    try:
        return DigestCounts.from_counts(day, ledger.counts_for_day(day, route=route))
    except Exception as exc:  # noqa: BLE001 - the digest must be sendable from a sick ledger
        log.warning("could not count the day %s for the route %r: %s", day, route, exc)
        return DigestCounts(day=day)


def route_digests(config: Any, ledger: Ledger, day: str) -> tuple[RouteDigest, ...]:
    """Every route worth a line this morning, configured ones first and in order.

    Routes the ledger knows and the configuration does not are appended, but only when they
    actually have something to say — a route removed months ago with nothing outstanding is
    history, not news.
    """
    out: list[RouteDigest] = []
    named: set[str] = set()
    for route in routes_of(config):
        name = str(getattr(route, "name", "") or "").strip()
        if not name or name in named:
            continue
        named.add(name)
        out.append(
            RouteDigest(
                name=name,
                label=str(getattr(route, "label", "") or ""),
                counts=_counts_for(ledger, day, name),
                enabled=bool(getattr(route, "enabled", True)),
            )
        )
    try:
        seen = ledger.routes_seen()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list the routes in the ledger: %s", exc)
        seen = ()
    for name in seen:
        if name in named:
            continue
        counts = _counts_for(ledger, day, name)
        if counts.discovered == 0 and not counts.failures:
            continue
        out.append(RouteDigest(name=name, label="", counts=counts, enabled=False, configured=False))
    return tuple(out)


# --------------------------------------------------------------------------- records


@dataclass
class Digest:
    day: str
    subject: str
    body: str
    counts: DigestCounts
    open_failures: int = 0
    new_failures: int = 0
    service_error: str = ""
    credential_warning: str = ""
    #: The same morning, counted one route at a time. The subject line is still the whole
    #: service; this is what the body breaks it down into.
    routes: tuple[RouteDigest, ...] = ()
    #: Recordings two routes have both claimed. Not a failure — the recording is being
    #: processed — but the transcript may be landing in the wrong folder, and that is only
    #: ever fixed by a person looking at the folders.
    route_disagreements: tuple[Mapping[str, Any], ...] = ()
    #: The work in hand right now, per route. Not part of the day being reported: it is the
    #: answer to "where are the rest of them?", which is asked about this minute.
    queue: QueueReport = field(default_factory=QueueReport)
    #: The held-passage queue and, while the gate ships dark, the measurement. Counts, sites
    #: and ages — never a word of what anybody said.
    held: HeldReport = field(default_factory=HeldReport)
    #: What the analysis pass cost. Carried on the digest rather than only rendered into the
    #: body, because the group email needs the figure and must not parse it back out of
    #: somebody's prose.
    spend: "SpendReport" = field(default_factory=lambda: SpendReport())

    @property
    def needs_a_person(self) -> bool:
        return (
            self.open_failures > 0
            or self.counts.nothing_arrived
            or bool(self.route_disagreements)
            or self.held.needs_a_person
        )

    @property
    def alarm(self) -> bool:
        """Whether the *external* monitor should be told this morning was not fine.

        Pinging ``success`` off the SMTP result made the monitor a check on the mail server:
        a credential could expire on a Friday, the loop fail every two minutes all weekend,
        and each morning a digest saying "nothing arrived" would reset the timer. The monitor
        then never alerts, and the whole thing rests on somebody reading a weekend email on a
        phone — which is the assumption that lost four days of recordings.

        Deliberately *not* ``needs_a_person``. An old quarantine nobody has got to yet is a
        person's task, and holding the monitor red on it forever would make the alarm mean
        nothing by the second week. This is "something went wrong recently, or the service
        itself is faulted", which recovers on its own when it is no longer true.
        """
        return bool(
            self.counts.nothing_arrived
            or self.new_failures > 0
            or self.service_error
            or self.credential_warning
        )


@dataclass
class SendResult:
    ok: bool
    detail: str = ""
    recipients: int = 0
    host: str = ""
    #: Reviewers who are not digest recipients and were sent their own queue's link. A
    #: staff member reviewing their own held passages is the ordinary case and is never on
    #: SMTP_TO, so without this number the queue's only drain is invisible from here.
    reviewers: int = 0


@dataclass
class DigestResult:
    digest: Digest
    sent: SendResult
    ping: PingResult | None = None

    @property
    def ok(self) -> bool:
        return self.sent.ok


# --------------------------------------------------------------------------- building


@dataclass
class SpendReport:
    """What the analysis pass cost, for the day and for the month it sits in.

    A METER, NOT A BRAKE. Nothing here stops anything: it was asked for as a number to
    look at, and a ceiling that paused the reading was considered and not chosen. So this
    reports and returns, and there is no code path from this dataclass to a decision.

    Both a day and a month, because either alone misleads. One day says nothing about the
    trend and every day looks small; a month-to-date figure on the 2nd looks like nothing
    and on the 28th looks alarming with no way to tell which. Together they answer "is
    today normal" and "where will the month land".
    """

    day: str = ""
    day_usd: float = 0.0
    day_recordings: int = 0
    month: str = ""
    month_usd: float = 0.0
    month_recordings: int = 0
    month_calls: int = 0
    #: Tokens for the month, split the way they are billed. The reader's output is normally
    #: three quarters of the bill, and that is only visible if output is kept apart.
    month_input: int = 0
    month_output: int = 0
    month_cache_read: int = 0
    month_cache_write: int = 0
    #: Models this deployment used that the price list has no entry for. Their tokens are
    #: counted and their money is NOT, so a non-empty list means every figure above is an
    #: undercount, and the section says so rather than printing a total that looks whole.
    unpriced: tuple[str, ...] = ()
    priced_on: str = ""

    @property
    def projected_month_usd(self) -> float:
        """Where the month lands if the rest of it looks like the part measured.

        Straight-line from the days elapsed. Crude on purpose and labelled as such in the
        email - a builder's month is not uniform, and a cleverer projection would be a
        guess with better presentation.
        """
        try:
            elapsed = int(self.day.split("-")[2])
        except (IndexError, ValueError):
            return 0.0
        if elapsed < 1:
            return 0.0
        return self.month_usd / elapsed * 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "day_usd": round(self.day_usd, 4),
            "day_recordings": self.day_recordings,
            "month": self.month,
            "month_usd": round(self.month_usd, 4),
            "month_recordings": self.month_recordings,
            "month_calls": self.month_calls,
            "month_tokens": {
                "input": self.month_input,
                "output": self.month_output,
                "cache_read": self.month_cache_read,
                "cache_write": self.month_cache_write,
            },
            "projected_month_usd": round(self.projected_month_usd, 2),
            "unpriced": list(self.unpriced),
            "priced_on": self.priced_on,
        }


def spend_report(config: Any, ledger: Ledger, *, day: str) -> SpendReport:
    """Read the recorded token counts and price them. Reads only."""
    month = day[:7] if len(day) >= 7 else ""
    since = f"{month}-01" if month else day
    try:
        rows = ledger.spend_since(since)
    except Exception:  # noqa: BLE001 - a meter must never be the reason an email fails
        log.warning("the spend meter could not read the ledger; the email omits it")
        return SpendReport(day=day, month=month, priced_on=prices.CHECKED_ON)

    report = SpendReport(day=day, month=month, priced_on=prices.CHECKED_ON)
    unpriced: list[str] = []
    for row in rows:
        calls = [Spend.from_dict(c) for c in row.get("calls") or ()]
        if not calls:
            continue
        amount, missing = prices.cost_of_all(calls)
        for name in missing:
            if name not in unpriced:
                unpriced.append(name)
        report.month_usd += amount
        report.month_recordings += 1
        report.month_calls += len(calls)
        for call in calls:
            report.month_input += call.input_tokens
            report.month_output += call.output_tokens
            report.month_cache_read += call.cache_read_tokens
            report.month_cache_write += call.cache_write_tokens
        if str(row.get("at") or "").startswith(day):
            report.day_usd += amount
            report.day_recordings += 1
    report.unpriced = tuple(unpriced)
    return report


def build(
    config: Any,
    ledger: Ledger,
    *,
    day: str | None = None,
    now: float | None = None,
    sweep_report: Any = None,
    archive_report: Any = None,
) -> Digest:
    """Assemble yesterday's digest. Reads the ledger; writes nothing; decides nothing."""
    clock = time.time() if now is None else now
    moment = local_now(config, clock)
    target = day or (moment.date() - datetime.timedelta(days=1)).isoformat()

    raw = ledger.counts_for_day(target)
    counts = DigestCounts.from_counts(target, raw)
    failures = list(counts.failures)
    today_failures = [f for f in failures if str(f.get("discovered_at") or "").startswith(target)]
    older_failures = [f for f in failures if not str(f.get("discovered_at") or "").startswith(target)]

    # The service's own last words. An expired Graph secret surfaces in the poll, not in any
    # recording, so it never becomes a failure row: without this the email said "nothing
    # arrived yesterday" while the ledger already knew the credential had expired, and the
    # one person who can fix it was told the phone might not have synced.
    service_error = _service_error(ledger)
    attention = _attention(ledger, target)
    routes = route_digests(config, ledger, target)
    queue = queue_report(config, ledger, day=target, now=clock)
    # Read, never written to. Nothing in the morning email decides anything about a held
    # passage: it counts them, says how old the oldest is, and gets more specific about it
    # every few days until a person answers it.
    held = held_report(config, ledger, day=target, now=utc_now_iso(clock))
    disagreements = route_disagreements(ledger, target)
    naming = naming_report(config, ledger, day=target)
    spend = spend_report(config, ledger, day=target)
    # Empty unless a receipt source is configured, which today it never is. See
    # newest_confirmed_arrival for what the slot is for and where it would be filled from.
    receipt = newest_confirmed_arrival(config, clock)
    if sweep_report is None:
        sweep_report = _stored_report(ledger, "sweep")
    if archive_report is None:
        archive_report = _stored_report(ledger, "archive")

    _stopped, _queued = split_stopped_from_queued(failures, now=clock, config=config)
    subject = subject_for(counts, _stopped, _queued)
    body = _render(
        config,
        counts,
        spend=spend,
        subject=subject,
        moment=moment,
        today_failures=today_failures,
        older_failures=older_failures,
        stats=ledger.stats(),
        routes=routes,
        queue=queue,
        held=held,
        disagreements=disagreements,
        sweep_report=sweep_report,
        archive_report=archive_report,
        service_error=service_error,
        attention=attention,
        naming=naming,
        receipt=receipt,
        expiries=credential_warnings(config, clock),
    )

    scrub = getattr(config, "scrub", None)
    if callable(scrub):
        subject = scrub(subject)
        body = scrub(body)
    # Three spellings, three filters. The "@" form, the spoken form, and the OneDrive path
    # segment that is a UPN with underscores — the last of which reaches here in every
    # failure's "Open it:" link and is invisible to an address check.
    subject = strip_owner_paths(strip_dictated_emails(strip_emails(subject)))
    body = strip_owner_paths(strip_dictated_emails(strip_emails(body)))

    if contains_email(body) or contains_email(subject):
        # strip_emails should have made this impossible. If it did not, the safe act is to
        # send a digest that says so rather than one that carries an address.
        log.error("the rendered digest still matched an email address after redaction; body withheld")
        body = (
            "The morning digest could not be sent in full: after redaction it still contained "
            "something matching an email address, and this service never emits one.\n\n"
            f"Counts for {target}: {counts.discovered} arrived, {counts.done} done, "
            f"{len(failures)} needing attention.\n\n"
            "Look at the ledger directly (transcriber status) and report this — it is a bug."
        )
        subject = strip_emails(subject)

    # A held passage that has waited a week reaches the subject line — the last escalation
    # there is, and the only one that reaches him before he opens anything. It goes on
    # after the counts and before any credential warning, because a credential that has
    # expired means nothing is processing at all and outranks everything.
    held_warning = held.subject_warning()
    if held_warning:
        subject = f"{held_warning} — {subject}"

    warnings = credential_warnings(config, clock)
    if warnings and warnings[0][0] <= _EXPIRY_SUBJECT_DAYS:
        subject = f"⚠ {warnings[0][1]} — {subject}"
    return Digest(
        day=target,
        subject=subject,
        body=body,
        counts=counts,
        open_failures=len(failures),
        new_failures=len(today_failures),
        service_error=service_error,
        credential_warning=warnings[0][1] if warnings else "",
        routes=routes,
        route_disagreements=disagreements,
        queue=queue,
        held=held,
        spend=spend,
    )


#: Credential expiry: mention it from here, and put it in the subject line from below.
_EXPIRY_NOTICE_DAYS = 45
_EXPIRY_SUBJECT_DAYS = 14

#: Where the worker and the scheduled jobs leave their last words.
_SERVICE_MARKS = (
    ("worker:last_cycle_error_detail", "the processing loop"),
    ("sweep:last_error", "the nightly sweep"),
    ("digest:last_error", "the morning email"),
)


def credential_warnings(config: Any, now: float | None = None) -> list[tuple[int, str]]:
    """Credentials near or past their stated expiry, soonest first.

    The investigation named an expired Entra client secret as the single most likely way
    this service dies: it runs perfectly for a year and then stops dead on a Tuesday with no
    prior notice of any kind. One optional date in the environment turns that cliff into a
    countdown in the email that is already read every morning.
    """
    clock = time.time() if now is None else now
    today = datetime.date.fromtimestamp(clock)
    out: list[tuple[int, str]] = []
    for attribute, what in (
        ("graph_secret_expires_on", "the OneDrive app secret"),
        ("engine_key_expires_on", "the transcription engine key"),
        ("analysis_key_expires_on", "the analysis model key"),
    ):
        raw = str(getattr(config, attribute, "") or "").strip()
        if not raw:
            continue
        try:
            when = datetime.date.fromisoformat(raw[:10])
        except ValueError:
            out.append((0, f"{what} has an unreadable expiry date ({raw!r}) in the configuration"))
            continue
        days = (when - today).days
        if days < 0:
            out.append((days, f"{what} EXPIRED {abs(days)} day(s) ago ({when.isoformat()})"))
        elif days <= _EXPIRY_NOTICE_DAYS:
            out.append((
                days,
                f"{what} expires in {days} day(s) ({when.isoformat()}); nothing will "
                f"process after that",
            ))
    return sorted(out, key=lambda pair: pair[0])


def _service_error(ledger: Ledger) -> str:
    """The worker's and the jobs' last recorded failure, in one line, or ''."""
    parts: list[str] = []
    for mark, what in _SERVICE_MARKS:
        try:
            value = (ledger.cursor_get(mark) or "").strip()
        except Exception:  # noqa: BLE001 - the digest must be sendable from a sick ledger
            continue
        if value:
            parts.append(f"{what}: {value}")
    return " | ".join(parts)


#: How many naming rows the email prints before it says "and N more". Five, with the count,
#: because the existing review list five lines above prints five and then STOPS — no
#: overflow line at all — so on a burst day fifty-five became five and silence. Copying that
#: half of the precedent would be copying the bug.
_NAMING_ROWS = 5


def _naming_lines(naming: Mapping[str, Any] | None) -> list[str]:
    """The naming part of WORTH A LOOK. Nothing here is a question.

    Every recording named below was transcribed and filed on time. The only thing being
    reported is what it ended up called, and the only action is optional: rename the audio
    in OneDrive if he wants the two to match.
    """
    facts = dict(naming or {})
    if not facts:
        return []

    unreadable = str(facts.get("unreadable") or "")
    lines = ["", f"  {facts.get('book') or 'site list: unknown'}"]

    if unreadable:
        # A fault and a quiet day are the same empty list and must never be the same
        # sentence. Saying "nothing came in" here would report a broken read as good news,
        # for as long as it stayed broken.
        lines.append(
            "  Could not read what was named yesterday, so this part of the email is "
            "missing rather"
        )
        lines.append(f"  than empty: {unreadable}")
        return lines

    if facts.get("book_fault"):
        # Not a quiet day: the site list is missing or unreadable, so nothing can be named
        # and nothing will be until somebody looks. Said every morning until it is fixed.
        lines.append("  Nothing is being named until that is sorted out. Nothing else is")
        lines.append("  affected — every recording is still transcribed and filed as usual.")
        return lines

    eligible = int(facts.get("eligible") or 0)
    if not eligible:
        # Nothing to say, so say nothing — this sits under "WORTH A LOOK (nothing failed)",
        # and a section that prints every morning on every install to report that nothing
        # happened trains him to skip the part of the email that matters.
        #
        # The exception is a site list that is missing or unreadable, handled above: that is
        # not a quiet day, it is a feature that has silently stopped, and it prints.
        return []

    lines.append("")
    for chunk in _wrap(
        f"{eligible} recording{'s' if eligible != 1 else ''} came in with the voice "
        f"recorder's own name. Every one of them was transcribed and filed on time — this "
        f"is only about what they are called.",
        width=72,
    ):
        lines.append(f"  {chunk}")
    if not any(r.get("applied") for r in (facts.get("rows") or ())):
        lines.append(
            "  Nothing has been renamed: it is only saying what it would have called them."
        )
    lines.append("")

    rows = list(facts.get("rows") or ())
    for row in rows[:_NAMING_ROWS]:
        # Never the item id as a fallback. It is 34 characters of OneDrive bookkeeping,
        # it identifies nothing he can look up, and printing it where a filename goes makes
        # the email read like a machine talking to itself.
        source = str(row.get("source_name") or "").strip() or "a recording with no name of its own"
        name = str(row.get("name") or "")
        # From the decision, never from today's setting. He may have switched it on this
        # morning; yesterday's recordings were still only being watched, and telling him
        # they were named would send him looking in the record for a document that is not
        # there. The reverse is worse: a rename reported as a suggestion is a change he
        # does not know he made.
        verb = "named" if row.get("applied") else "would call"
        head = f"{source}  ->  {verb} it {name}" if name else f"{source}  ->  left as it is"
        lines.append(f"    {head}")
        for chunk in _wrap(str(row.get("why") or ""), width=68):
            lines.append(f"      {chunk}")
        if name:
            lines.append(f"      If you want the audio to match: {name}")
        lines.append("")
    if len(rows) > _NAMING_ROWS:
        lines.append(f"    ...and {len(rows) - _NAMING_ROWS} more")
    return lines


def naming_report(config: Any, ledger: Ledger, *, day: str) -> Mapping[str, Any]:
    """What the service worked out to call yesterday's unnamed recordings.

    Read-only, and it decides nothing. There is nothing here for him to answer: every one
    of these recordings was transcribed and filed on time, and the only question is what it
    is called. Returns an empty mapping when naming is off, so the section never prints.
    """
    if not bool(getattr(config, "naming", False)):
        return {}
    # Read fresh, and deliberately not shared with the worker's cached copy. The digest is
    # built once a day, often in a different process from the one that published yesterday's
    # recordings, and what it reports is what the site list looks like NOW — a book the
    # worker loaded a week ago and has held ever since is exactly the thing this line exists
    # to expose. The cost is one 80 KB read a day.
    book = sitebook.EMPTY
    try:
        book = sitebook.load(str(getattr(config, "naming_sites_file", "") or ""))
    except Exception as exc:  # noqa: BLE001 - the email must send from a sick everything
        log.warning("could not read the site list: %s", exc)
        # sitebook.load is documented never to raise, so this branch should be unreachable.
        # If it ever fires, the empty book's own line reads "site list: empty, so nothing is
        # being named" — which is what a service with no book configured says, and would
        # report a fault as a settled choice. Say what actually happened instead.
        book = sitebook.SiteBook(fault=f"it could not be read ({exc})")
    unreadable = ""
    try:
        decisions = ledger.naming_for_day(day)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the naming decisions for %s: %s", day, exc)
        decisions, unreadable = [], str(exc) or exc.__class__.__name__
    # E1 is "he named this one himself", which is most recordings and is not news.
    eligible = [d for d in decisions if str(d.get("code") or "") not in ("E1", "E2", "off")]
    return {
        "book": book.line(),
        "applying": bool(getattr(config, "naming_apply", False)),
        "eligible": len(eligible),
        "named": sum(1 for d in eligible if d.get("name")),
        "rows": eligible,
        #: The site list could not be loaded. Distinct from "nothing came in": one is a
        #: quiet day and the other is the feature having silently stopped, and they must
        #: never render the same way — which for a quiet day is not at all.
        "book_fault": bool(book.fault or not book),
        #: Set when the ledger could not be asked. Carried rather than swallowed, because
        #: an empty list from a failed read and an empty list from a quiet day are the same
        #: value and must never be the same sentence.
        "unreadable": unreadable,
    }


def _attention(ledger: Ledger, day: str) -> Mapping[str, Any]:
    try:
        return ledger.attention_for_day(day)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the attention counts for %s: %s", day, exc)
        return {}


def route_disagreements(ledger: Ledger, day: str) -> tuple[Mapping[str, Any], ...]:
    """Recordings two routes have both claimed, from the reported day onwards.

    A disagreement is either a recording he moved between two watched folders or two routes
    watching folders one of which is inside the other — OneDrive reports a folder and
    everything under it. The second sends a transcript to the wrong folder and would, at
    sixty days, move the original into the wrong archive, so it belongs in the one thing he
    reads every morning rather than only in the event log.
    """
    try:
        return tuple(ledger.route_disagreements(since=day))
    except Exception as exc:  # noqa: BLE001 - the digest must be sendable from a sick ledger
        log.warning("could not read the route disagreements: %s", exc)
        return ()


def _stored_report(ledger: Ledger, name: str) -> str:
    """Last night's rendered report, read back after a restart lost the in-memory one."""
    try:
        return (ledger.cursor_get(f"{name}:last_report") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _render(
    config: Any,
    counts: DigestCounts,
    *,
    subject: str,
    moment: Any,
    today_failures: Sequence[Mapping[str, Any]],
    older_failures: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    sweep_report: Any,
    naming: Mapping[str, Any] | None = None,
    routes: Sequence["RouteDigest"] = (),
    queue: "QueueReport | None" = None,
    held: "HeldReport | None" = None,
    disagreements: Sequence[Mapping[str, Any]] = (),
    archive_report: Any,
    service_error: str = "",
    attention: Mapping[str, Any] | None = None,
    expiries: Sequence[tuple[int, str]] = (),
    spend: "SpendReport | None" = None,
    receipt: str = "",
) -> str:
    lines: list[str] = [subject, ""]

    day_stamp = parse_stamp(counts.day + "T12:00:00Z")
    pretty = time.strftime("%A %d %B %Y", time.gmtime(day_stamp)) if day_stamp else counts.day
    lines.append(f"For {pretty} ({counts.day}).")
    offset = moment.utcoffset()
    if offset and offset.total_seconds():
        hours = offset.total_seconds() / 3600.0
        lines.append(
            f"Days are counted on UTC dates; local time here is UTC{hours:+.0f}, so this covers "
            f"roughly {abs(hours):.0f}:00 to {abs(hours):.0f}:00 local."
        )
    lines.append("")

    if service_error:
        lines += [
            "THE SERVICE ITSELF REPORTED A FAULT",
            _RULE,
            "This is not about any one recording. Until it is fixed, nothing will be",
            "processed at all.",
            "",
        ]
        for chunk in _wrap(plain_reason({"reason": service_error})):
            lines.append(f"  {chunk}")
        lines.append("")
        for chunk in _wrap(f"Technical detail: {service_error}"):
            lines.append(f"  {chunk}")
        lines.append("")

    for days, sentence in expiries:
        lines += ["A CREDENTIAL IS RUNNING OUT", _RULE]
        for chunk in _wrap(sentence[0].upper() + sentence[1:] + "."):
            lines.append(f"  {chunk}")
        lines += [
            "",
            "  Renew it in the Azure portal under the app registration, put the new value",
            "  in the service environment, restart, and update the expiry date.",
            "",
        ]
        break

    if counts.nothing_arrived:
        lines += [
            "NOTHING ARRIVED YESTERDAY.",
            _RULE,
            "No recording reached the folder at all. If you did not record anything, this is",
            "nothing — ignore it. If you did, then either the phone did not sync or this service",
            "is not seeing the folder, and both need looking at today.",
            "",
            "This alert is armed at weekends as well. A Saturday site walk is normal, and a",
            "Friday-evening failure that stayed quiet over a weekend would not surface until",
            "Monday.",
            "",
        ]

    if today_failures or older_failures:
        total = len(today_failures) + len(older_failures)
        lines += [f"NEEDS YOU — {total} recording(s) did not finish", _RULE]
        # Some of these are not stuck at all: they are yesterday's recordings still in the
        # queue this morning. Saying so here, next to them, is the difference between a
        # person believing a recording is lost and a person seeing that it is next.
        waiting = [
            f for f in list(today_failures) + list(older_failures)
            if not State.is_terminal(str(f.get("state") or ""))
        ]
        if waiting:
            for chunk in _wrap(
                f"{len(waiting)} of these had not finished by the end of the day rather than "
                f"failed — they are in the queue, counted again under THE QUEUE below, and "
                f"nothing about them is lost."
            ):
                lines.append(f"  {chunk}")
            lines.append("")
        # Which route a failure arrived on, but only when there is more than one: on a
        # single-route service it would be the same word under every failure.
        route_labels = {r.name: r.display for r in routes} if len(routes) > 1 else {}
        index = 0
        for failure in list(today_failures) + list(older_failures):
            index += 1
            lines += _failure_block(index, failure, counts.day, route_labels)
        lines.append("")
    else:
        lines += ["Nothing needs you this morning.", ""]

    # Held passages sit here, above the counts, only when somebody is actually waiting on
    # him. They are not a failure — nothing is lost and nothing is late — but they are the
    # one thing in this email that no amount of time will resolve on its own, so when there
    # is a queue it goes where a queue that needs a person goes. When there is nothing
    # waiting, the same section drops below the routes and reports the measurement instead.
    if held is not None and held.pending:
        lines += _held_section(held)

    lines += [
        "WHAT ARRIVED",
        _RULE,
        f"  arrived                {counts.discovered}",
        f"  transcribed and filed  {counts.done}",
        f"  verified silence       {counts.skipped_empty}",
        f"  still in progress      {counts.in_flight}",
        f"  stopped for you        {counts.quarantined}",
        "",
        f"  finished yesterday (whenever they arrived): {counts.done_on_day}",
        "",
    ]

    # What "done" is actually evidence of, said where the counts are read rather than only
    # in the deployment notes. Everything above is settled by this service watching itself:
    # it wrote the file to OneDrive and read it back. The hand-over into the record is a
    # separate flow with its own expiring access, and when that lapses the record quietly
    # stops receiving transcripts while this email goes on reporting perfect mornings. One
    # sentence is the whole of the fix available from inside this file; the real one is a
    # receipt, and the slot for it is the line underneath.
    for chunk in _wrap(
        '"Transcribed and filed" means the transcript was written to OneDrive and read back '
        "from OneDrive to check it arrived whole. Carrying it on from there into the record "
        "is a separate flow outside this service, and nothing here can see whether it got "
        "there — so if that flow's access has expired, this email will still report a "
        "perfect morning. A transcript missing from the record is that flow, not these "
        "counts."
    ):
        lines.append(f"  {chunk}")
    lines.append("")
    if receipt:
        for chunk in _wrap(receipt[0].upper() + receipt[1:] + "."):
            lines.append(f"  {chunk}")
        lines.append("")

    # Directly under the counts, because it is the same cohort measured in money. Nothing
    # in this section stops anything - it is a meter, and it was asked for as one.
    if spend is not None and (spend.month_calls or spend.unpriced):
        lines += _spend_section(spend)

    # Directly under the counts, because it is the sentence that stops "still in progress"
    # above from being read as "lost". A queue is work in hand; the failures are above and
    # are the only thing in this email that is a loss.
    if queue is not None:
        lines += ["THE QUEUE — what is waiting to be transcribed", _RULE]
        lines += queue.lines()
        lines.append("")

    if routes:
        # One line per route, every route, every morning. The totals above are the whole
        # service; these are what they are made of, and a route with nothing on it is listed
        # saying so rather than left out to be read as "fine".
        lines += ["BY ROUTE", _RULE]
        for entry in routes:
            marker = "!" if entry.needs_a_person else " "
            lines.append(f"  {marker} {entry.line()}")
        lines.append("")

    if held is not None and not held.pending:
        lines += _held_section(held)

    if disagreements:
        # Next to the per-route breakdown, because it is a fact about two routes rather than
        # about one recording. Nothing here is decided: which of the two routes a recording
        # belongs to is exactly the question, and only a person can answer it.
        lines += [f"TWO ROUTES CLAIMED THE SAME RECORDING — {len(disagreements)}", _RULE]
        for chunk in _wrap(
            "Each of these was seen on one route and then seen again on another. Either you "
            "moved it between two watched folders, or one route's folder is inside another "
            "route's folder — OneDrive reports a folder and everything underneath it, so "
            "both routes see the same recording."
        ):
            lines.append(f"  {chunk}")
        lines.append("")
        for event in list(disagreements)[:10]:
            what = str(event.get("item_name") or event.get("item_id") or "a recording")
            lines.append(f"  - {what}: {event.get('detail') or 'seen on two routes'}")
        if len(disagreements) > 10:
            lines.append(f"  - and {len(disagreements) - 10} more")
        lines.append("")
        for chunk in _wrap(
            "Its transcript went to the folder of the route it stayed on, which may not be "
            "the right one, and it will not be archived until this is sorted out. Run "
            "`transcriber routes` to see which folders each route watches."
        ):
            lines.append(f"  {chunk}")
        lines.append("")

    facts = dict(attention or {})
    naming_lines = _naming_lines(naming)
    if (facts.get("review") or facts.get("unverified_duration_guard")
            or facts.get("degraded_transcripts") or naming_lines):
        lines += ["WORTH A LOOK (nothing failed)", _RULE]
        if facts.get("review"):
            lines.append(
                f"  {facts['review']} proposed item(s) were withheld because the words offered"
            )
            lines.append(
                "  as evidence are not in the transcript. They are kept against the recording;"
            )
            lines.append("  run: transcriber status --item <id>")
            for row in list(facts.get("review_rows") or ())[:5]:
                lines.append(f"    - {row.get('name') or row.get('item_id')}: {row.get('count')}")
        if facts.get("unverified_duration_guard"):
            lines.append(
                f"  {facts['unverified_duration_guard']} recording(s) were too large for the "
                "engine and"
            )
            lines.append(
                "  had to be split. The engine returned no timestamps, so the assembled"
            )
            lines.append(
                "  transcript could not be measured against the clock — each piece was checked"
            )
            lines.append("  for words instead. Worth an eye if one reads short.")
        if facts.get("degraded_transcripts"):
            lines.append(
                f"  {facts['degraded_transcripts']} transcript(s) were produced with some engine"
            )
            lines.append("  settings stripped, so they may be less accurate than usual.")
        lines += naming_lines
        lines.append("")

    if sweep_report is not None:
        lines += ["LAST NIGHT'S SWEEP", _RULE, _indent(_render_of(sweep_report)), ""]
    if archive_report is not None:
        lines += ["THE ARCHIVE PASS", _RULE, _indent(_render_of(archive_report)), ""]

    lines += ["THE LEDGER", _RULE]
    by_state = dict(stats.get("by_state") or {})
    lines.append(f"  recordings on record   {stats.get('total', 0)}")
    for state in (State.DONE, State.QUARANTINED, State.SKIPPED_EMPTY):
        if by_state.get(state):
            lines.append(f"  {state.lower():<22} {by_state[state]}")
    unfinished = sum(count for state, count in by_state.items() if not State.is_terminal(state))
    lines.append(f"  {'unfinished':<22} {unfinished}")
    oldest = stats.get("oldest_unfinished")
    if oldest:
        lines.append(
            f"  oldest unfinished      {oldest.get('name', '')} (first seen {oldest.get('discovered_at', '')})"
        )
    for name, mark in sorted((stats.get("cursors") or {}).items()):
        state = "set" if mark.get("value_present") else "NOT SET"
        lines.append(f"  {name:<22} {state}, last touched {mark.get('updated_at', 'never')}")
    lines.append("")

    lines += [
        _RULE,
        "This email is sent every morning, including the mornings when everything worked.",
        "If it stops arriving, the service has stopped — that is the whole point of it.",
        "Nothing in this pipeline decides anything: it transcribes what was said and files",
        "commitments and questions as proposals for you to confirm.",
    ]
    return "\n".join(lines)


def _spend_section(spend: "SpendReport") -> list[str]:
    """What the AI pass cost. Reports; decides nothing; stops nothing.

    The provenance line is not decoration. The setup guide told him this cost "a few
    dollars a month" when it was nearer eighty, and the reason nobody caught it is that the
    figure carried no date and no workings. Every figure here says which price list it came
    from and when that list was read, so a stale number is visible rather than trusted.
    """
    lines = ["WHAT THE AI PASS COST", _RULE]
    lines.append(f"  yesterday                {_usd(spend.day_usd)}   "
                 f"({spend.day_recordings} recording{'' if spend.day_recordings == 1 else 's'})")
    lines.append(f"  this month so far        {_usd(spend.month_usd)}   "
                 f"({spend.month_recordings} recording{'' if spend.month_recordings == 1 else 's'}, "
                 f"{spend.month_calls} model call{'' if spend.month_calls == 1 else 's'})")
    projected = spend.projected_month_usd
    if projected:
        lines.append(f"  the month at this rate   {_usd(projected)}   "
                     "(straight-line, and a month of building is not straight)")
    lines.append("")

    total_tokens = (spend.month_input + spend.month_output
                    + spend.month_cache_read + spend.month_cache_write)
    if total_tokens:
        # Tokens, and named as such. Calling them words would overstate the reading by
        # about a third, and it is exactly the sort of figure that gets repeated.
        lines.append("  what was read and written this month, in tokens")
        lines.append("  (a token is roughly three quarters of a word)")
        lines.append(f"    sent to the model            {spend.month_input:>12,}")
        lines.append(f"    it wrote back                {spend.month_output:>12,}")
        if spend.month_cache_read or spend.month_cache_write:
            lines.append(f"    re-read from cache, cheaply  {spend.month_cache_read:>12,}")
            if spend.month_cache_write:
                lines.append(f"    put into the cache           {spend.month_cache_write:>12,}")
        # The reader's answers are normally most of the bill, and that is the one line here
        # that suggests what to change if the figure ever matters.
        if spend.month_output:
            lines.append("")
            for chunk in _wrap(
                "What it writes back costs five times what it reads, so the middle line is "
                "usually most of the bill. If this ever needs to come down, that is the "
                "number to aim at - not the number of recordings."
            ):
                lines.append(f"  {chunk}")
        lines.append("")

    if spend.unpriced:
        # Said loudly, because every figure above is an undercount when this fires and a
        # total that quietly omits a model reads exactly like a total that includes it.
        for chunk in _wrap(
            "EVERY FIGURE ABOVE IS AN UNDERCOUNT. This deployment used "
            + ", ".join(spend.unpriced)
            + ", which the price list has no entry for, so those calls are counted and not "
              "costed. Tell me and I will add them; until then the real figure is higher "
              "than the one above by however much they came to."
        ):
            lines.append(f"  {chunk}")
        lines.append("")

    for chunk in _wrap(
        f"What is recorded against each recording is the tokens it used; the money is "
        f"worked out when this email is written, from the published rates as they stood on "
        f"{spend.priced_on}. If those rates have moved, this figure has moved with them and "
        f"the token counts are still right. Nothing here stops anything: it is a meter."
    ):
        lines.append(f"  {chunk}")
    lines.append("")
    return lines


def _usd(amount: float) -> str:
    """Money, at a precision that does not pretend to more than it knows."""
    if amount and amount < 0.01:
        return "under $0.01"
    return f"${amount:,.2f}"


def _held_section(held: "HeldReport") -> list[str]:
    """The held-passage section, or nothing at all on a service that has never used one.

    Nothing at all is the deliberate case, and it is narrow: the gate has classified no
    recording, is holding nothing, and owes the record nothing. Every other state produces a
    section, including a gate that has been switched off with passages still waiting — a
    queue that disappeared from the morning email the day somebody changed a setting is
    exactly the silent emptying this design exists to make impossible.
    """
    if held.empty:
        return []
    return [held.heading(), _RULE] + held.lines() + [""]


def _failure_block(
    index: int,
    failure: Mapping[str, Any],
    day: str,
    route_labels: Mapping[str, str] | None = None,
) -> list[str]:
    name = str(failure.get("name") or failure.get("item_id") or "an unnamed recording")
    attempts = int(failure.get("attempts") or 0)
    discovered = str(failure.get("discovered_at") or "")
    raw = str(failure.get("reason") or "").strip()
    block = [f"{index}. {name}"]
    for chunk in _wrap(plain_reason(failure)):
        block.append(f"     {chunk}")
    route = str(failure.get("route") or "").strip()
    if route and route_labels:
        block.append(f"     Which route: {route_labels.get(route, route)}.")
    if discovered and not discovered.startswith(day):
        block.append(f"     Still open from {discovered[:10]}.")
    if attempts:
        block.append(f"     Tried {attempts} time{'' if attempts == 1 else 's'}.")
    link = failure.get("web_url")
    block.append(f"     Open it: {link}" if link else "     No link was recorded for it.")
    if raw:
        block.append(f"     Technical detail: {raw}")
    block.append("")
    return block


def _wrap(text: str, width: int = 74) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _render_of(report: Any) -> str:
    render = getattr(report, "render", None)
    if callable(render):
        try:
            return str(render())
        except Exception as exc:  # noqa: BLE001 - a bad report must not stop the digest
            return f"(this report could not be rendered: {type(exc).__name__}: {exc})"
    return str(report)


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else "" for line in text.split("\n"))


# --------------------------------------------------------------------------- sending


def review_links(config: Any) -> dict[str, str]:
    """One working review link per person who has something waiting, plus the principal.

    The queue has exactly one drain, and this is it. The morning email used to print the
    bare ``GATE_REVIEW_BASE_URL``, which without a token renders "This link has expired.
    Open the link in this morning's email" — pointing at the email the dead link came from.
    There is no login form and no other issuance path, so the only working link came from an
    operator running ``transcriber review --link`` on the service host, by hand, per person,
    every thirty-six hours. Nothing releases or discards on a timer, by design, so a queue
    nobody can open is a queue that fills forever and a record that quietly hollows out.

    Never raises. A digest that failed to send because the token store was busy would be the
    same silent morning this service exists to remove; the email goes out either way, and
    with no link it still names the command that works from the host.
    """
    if normalise_mode(getattr(config, "gate_mode", MODE_SHADOW)) != MODE_ON:
        # Nothing is being withheld, so there is nothing to approve and no reason to mint a
        # capability. Shadow is measured, not answered.
        return {}
    if not str(getattr(config, "gate_review_base_url", "") or "").strip():
        return {}
    try:
        from . import review_server

        service = review_server.service_from_config(config)
        links = review_server.links_for_pending(config, service)
    except Exception as exc:  # noqa: BLE001 - the morning email goes out regardless
        log.warning("review links could not be minted for this morning's email: %s", exc)
        return {}
    # A token is a capability. It must never reach a log line, and it reaches one the moment
    # anything interpolates this dictionary into a message.
    if links:
        secrets: list[str] = []
        for url in links.values():
            secrets.append(url)
            _base, _sep, token = url.partition("?k=")
            if token:
                secrets.append(token)
        logging_setup.add_secrets(secrets)
    return links


def _personalised(body: str, base_url: str, link: str) -> str:
    """The digest body with the review link this one reader can actually open.

    One substitution of one whole line, matched on the sentence
    :meth:`HeldReport._pending_lines` writes. Deliberately not a substitution of the bare
    URL: the tokenised link *starts with* the base URL, so replacing the URL and then the
    sentence containing it appends the token twice and produces a dead link, which is the
    bug this whole function exists to fix.
    """
    marker = f"Answer them here: {base_url}"
    if not base_url or not link or marker not in body:
        return body
    return body.replace(marker, f"Answer them here: {link}")


def _deliver(
    config: Any,
    outgoing: Sequence[EmailMessage],
    *,
    host: str,
    port: int,
    smtp_factory: Callable[..., Any] | None,
    primary_count: int,
    log_subject: str,
    what: str = "the morning digest",
) -> SendResult:
    """Open one connection, send each message on its own, and report honestly.

    Shared by the personal digest and the group email so the lessons in here are learned
    once. They were expensive: see the long comment below about the morning this loop sent
    seventy-two copies of the same email.

    ``primary_count`` is how many of ``outgoing`` are the main recipients; anything after
    that index is counted as a reviewer in the result. ``what`` names the mail in the log
    lines, because "the morning digest could not be sent" about the group email would send
    somebody looking at the wrong thing.
    """
    scrub = getattr(config, "scrub", None)

    def _said(exc: Exception) -> str:
        detail = f"{type(exc).__name__}: {exc}"
        return scrub(detail) if callable(scrub) else detail

    def _temporary(exc: Exception) -> bool:
        """Whether the relay said "not now" rather than "not ever".

        A 4xx is greylisting or a full mailbox: the address is fine and tomorrow will
        probably work. A 5xx is a mailbox that is gone. Both are reported; they are worth
        telling apart because one is somebody's job to fix and the other is the mail system
        doing what it does.
        """
        codes: list[int] = []
        answers = getattr(exc, "recipients", None)
        if isinstance(answers, dict):
            for answer in answers.values():
                try:
                    codes.append(int(answer[0]))
                except (TypeError, ValueError, IndexError):
                    pass
        code = getattr(exc, "smtp_code", None)
        if isinstance(code, int):
            codes.append(code)
        return bool(codes) and all(400 <= c < 500 for c in codes)

    # One address the relay will not take must not stop the others. It used to: every
    # message went inside one try, so a mailbox deleted when somebody left aborted the loop
    # at that address. Everyone earlier in the list had already received the email, everyone
    # later got nothing, and the whole send reported ok=False — which leaves DIGEST_DAY_MARK
    # unwritten, so `should_run` fires again fifteen minutes later and sends the same email
    # to the same people. Roughly seventy-two copies before midnight, every day, while the
    # log and the monitor both said it could not be sent. That is the mail loop RETRY_AFTER_S
    # was written to prevent, arriving as delivered mail rather than as retries.
    #
    # With the gate armed it also silently skipped every reviewer sorted after the bad
    # address, so people never got the link to passages waiting on them, and nothing recorded
    # who was missed.
    refused: list[str] = []
    delivered_recipients = 0
    delivered_reviewers = 0
    try:
        with _connect(config, host, port, smtp_factory) as server:
            for index, message in enumerate(outgoing):
                try:
                    server.send_message(message)
                except Exception as exc:  # noqa: BLE001 - one address, not the whole morning
                    when = "temporarily" if _temporary(exc) else "permanently"
                    refused.append(f"{message['To']} ({when} — {_said(exc)})")
                    continue
                if index < primary_count:
                    delivered_recipients += 1
                else:
                    delivered_reviewers += 1
    except Exception as exc:  # noqa: BLE001 - the connection itself; nothing went out
        detail = _said(exc)
        # ERROR, not WARNING: an unsendable digest is the failure mode this service exists
        # to remove, and it must be loud even though nothing raises.
        log.error("%s could NOT be sent via %s:%s — %s", what, host, port, detail)
        return SendResult(ok=False, detail=detail, recipients=primary_count, host=host)

    if refused:
        # Loud, and named — but NOT a failed send, because the people who were reachable have
        # the email in their hands and must not be sent it again every quarter of an hour.
        #
        # The cost of that choice, stated rather than hidden: an address refused TEMPORARILY
        # — greylisting, a full mailbox — misses this one morning rather than being retried,
        # because retrying means re-sending to everyone who already has it. The alternative
        # was the mail loop this replaced, which sent Jay seventy-two copies. The word
        # "temporarily" in the line below is what tells a person which of the two happened,
        # and tomorrow's send is the retry.
        log.error(
            "%s was refused for %d address(es) via %s:%s — %s",
            what, len(refused), host, port, "; ".join(refused),
        )
    if not delivered_recipients and not delivered_reviewers:
        # Nobody at all got it. That is a failed morning however the relay phrased it.
        return SendResult(
            ok=False, detail="; ".join(refused) or "no message was accepted",
            recipients=primary_count, host=host,
        )

    log.info(
        "%s sent to %d recipient(s) and %d reviewer(s) via %s:%s — %s",
        what, delivered_recipients, delivered_reviewers, host, port, log_subject,
    )
    return SendResult(
        ok=True,
        detail="; ".join(refused),
        recipients=delivered_recipients,
        host=host,
        reviewers=delivered_reviewers,
    )


def send_group(
    config: Any,
    subject: str,
    body: str,
    *,
    smtp_factory: Callable[..., Any] | None = None,
) -> SendResult:
    """Send the one consolidated email to whoever is configured as the admin.

    Separate from :func:`send` and not a mode of it, because the two have different
    recipients and a different rule about what may be in the body. The personal digest
    carries a review link, which is a capability belonging to one person; the group email
    carries counts about everybody and so must never carry a link at all. Sharing one
    function would mean one edit away from mailing everyone's capability to the admin.
    """
    recipients = tuple(getattr(config, "group_admin_to", ()) or ())
    host = str(getattr(config, "smtp_host", "") or "")
    port = int(getattr(config, "smtp_port", 587) or 587)
    if not recipients or not host:
        return SendResult(ok=False, detail="no group admin recipient is configured",
                          recipients=0, host=host)

    sender = str(getattr(config, "smtp_from", "") or "")
    outgoing = []
    for who in recipients:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = who
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(body, subtype="plain", charset="utf-8")
        outgoing.append(message)
    return _deliver(
        config, outgoing, host=host, port=port, smtp_factory=smtp_factory,
        primary_count=len(recipients), log_subject=subject, what="the group email",
    )


def send(
    config: Any,
    digest: Digest,
    *,
    smtp_factory: Callable[..., Any] | None = None,
    links: Mapping[str, str] | None = None,
) -> SendResult:
    """Send the digest as one plain-text part. Never raises; the caller must see failure.

    One message per recipient rather than one message to all of them, because the review
    link is a capability belonging to one person: putting everybody's in one body would let
    any recipient open anybody's queue, and putting one person's in a body everybody gets
    would do it more quietly. The bodies are otherwise identical.
    """
    recipients = tuple(getattr(config, "smtp_to", ()) or ())
    host = str(getattr(config, "smtp_host", "") or "")
    port = int(getattr(config, "smtp_port", 587) or 587)
    if not recipients or not host:
        detail = "no SMTP host or recipient is configured, so the morning digest cannot be sent"
        log.error("%s", detail)
        return SendResult(ok=False, detail=detail, recipients=len(recipients), host=host)

    issued = dict(links or {})
    base_url = str(getattr(config, "gate_review_base_url", "") or "").strip()
    sender = str(getattr(config, "smtp_from", "") or "")

    def build(to: str, body: str, subject: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(body, subtype="plain", charset="utf-8")
        return message

    outgoing = [
        build(who, _personalised(digest.body, base_url, issued.get(who, "")), digest.subject)
        for who in recipients
    ]
    # Everybody who has passages waiting and does not get the morning email — a staff member
    # reviewing their own held passages is the ordinary case, and they are not on SMTP_TO.
    # Without this the only person who can ever drain the queue is the principal, and
    # decision 6 says most of it is not his to read.
    others = [who for who in sorted(issued) if who and who not in recipients]
    for who in others:
        outgoing.append(
            build(who, _own_queue_body(config, digest, issued[who]),
                  _own_queue_subject(digest, who))
        )

    return _deliver(
        config, outgoing, host=host, port=port, smtp_factory=smtp_factory,
        primary_count=len(recipients), log_subject=digest.subject,
    )


def _own_queue_subject(digest: Digest, who: str) -> str:
    return f"Your held passages — {digest.day}"


def _own_queue_body(config: Any, digest: Digest, link: str) -> str:
    """What a reviewer who is not the principal is sent: their own queue, and the way in.

    Deliberately not the morning digest. It carries the whole service's failures, its queue
    depth and its per-route breakdown — none of which is a staff member's business — and it
    names the oldest held passage's site and reviewer. This says one thing: you have some
    waiting, here is where to answer them.

    It carries no count and no site either. Those are on the page behind the link, which
    checks who is asking; an email is forwarded, screenshotted and read over a shoulder.
    """
    return "\n".join(
        [
            f"For {digest.day}.",
            "",
            "You have passages from your own recordings waiting for a yes or a no.",
            "",
            "They were taken out of the transcript and are not in the record until you say",
            "so. Nothing will happen to them on their own: they will not be released, they",
            "will not be discarded, and they will not stop being reported. Only you see the",
            "words — the count and the site are all anybody else is shown.",
            "",
            f"  Answer them here: {link}",
            "",
            "That link is yours, it expires, and a new one comes with tomorrow's email.",
            "Do not forward it: anybody holding it can answer your queue.",
            "",
            "-- ",
            "Sent by the transcriber. Nothing in this path decides anything.",
        ]
    ) + "\n"


def _connect(config: Any, host: str, port: int, smtp_factory: Callable[..., Any] | None) -> Any:
    timeout = float(getattr(config, "http_timeout_s", 60) or 60)
    if smtp_factory is not None:
        return smtp_factory(host, port, timeout=timeout)
    user = str(getattr(config, "smtp_user", "") or "")
    password = str(getattr(config, "smtp_password", "") or "")
    context = ssl.create_default_context()
    if port == 465:
        server: Any = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        server.ehlo()
        if getattr(config, "smtp_starttls", True):
            server.starttls(context=context)
            server.ehlo()
    if user and password:
        server.login(user, password)
    return server


# --------------------------------------------------------------------------- the job


def run(
    config: Any,
    ledger: Ledger,
    *,
    day: str | None = None,
    now: float | None = None,
    sweep_report: Any = None,
    archive_report: Any = None,
    heartbeat: Heartbeat | None = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> DigestResult:
    """Build it, send it, and only then tell the outside world we are alive.

    The ordering is the point. A ping sent before the digest went out would tell the monitor
    the morning was fine on exactly the mornings it was not.
    """
    clock = time.time() if now is None else now
    ledger.cursor_set(DIGEST_ATTEMPT_MARK, utc_now_iso(clock))

    digest = build(
        config, ledger, day=day, now=clock, sweep_report=sweep_report, archive_report=archive_report
    )
    sent = send(config, digest, smtp_factory=smtp_factory, links=review_links(config))
    # Written after the build, never during it: tomorrow's "is it growing?" is answered
    # against what this morning's email actually reported, and a dry run or a `status` that
    # rewrote the history would make the answer depend on who looked.
    record_queue_depth(ledger, digest.queue.day or digest.day, digest.queue.queued)

    # Is this THIS MORNING's email, or somebody looking at an older day? Only the first may
    # touch the marks, and the difference is not cosmetic: `mark_run` stamps TODAY whatever
    # day was asked for, so `transcriber digest --day 2026-08-27` — reading back an old
    # morning, which is what the option is for — marked today as already sent and the real
    # 06:00 email never went out. The heartbeat is deliberately left alone: pinging is the
    # established contract for any run that actually sent an email, and narrowing it here
    # would be a second change riding along with this one.
    for_today = not day or day == local_now(config, clock).date().isoformat()

    monitor = heartbeat if heartbeat is not None else Heartbeat.from_config(config)
    if sent.ok and not digest.alarm:
        if for_today:
            mark_run(config, ledger, now=clock)
            ledger.cursor_set(DIGEST_ERROR_MARK, "")
        ping = monitor.success(digest.subject)
    elif sent.ok:
        # The email went out and says something is wrong. The day is still marked sent — this
        # is not a send failure and must not become a mail loop — but the monitor is told, so
        # a weekend of "nothing arrived" cannot pass with the alarm sitting green.
        if for_today:
            mark_run(config, ledger, now=clock)
            ledger.cursor_set(DIGEST_ERROR_MARK, "")
        ping = monitor.fail(digest.subject)
    else:
        ledger.cursor_set(DIGEST_ERROR_MARK, sent.detail[:500])
        # Actively alert rather than wait for the monitor's grace period to lapse: the one
        # thing worse than a broken morning is a broken morning nobody hears about.
        ping = monitor.fail(f"the morning digest could not be sent: {sent.detail}")

    # --- the group view, strictly last ---------------------------------------------
    # Everything above has happened: the email is sent, the day is marked, the monitor is
    # told. Nothing below may change any of that. One person running this alone does none
    # of it, because INSTANCE_NAME and GROUP_FOLDER_ID are unset and `_group_step` returns
    # on the first line.
    _group_step(config, digest, smtp_factory=smtp_factory, now=clock)

    return DigestResult(digest=digest, sent=sent, ping=ping)


def _group_step(
    config: Any,
    digest: Digest,
    *,
    smtp_factory: Callable[..., Any] | None = None,
    now: float | None = None,
    client: Any = None,
) -> None:
    """Write this copy's status, and send the group email if this copy is the admin's.

    Swallows everything. The one thing this function must never do is turn a morning where
    somebody got their email into a morning where they did not, and it is called after that
    email has already gone out precisely so that it cannot.
    """
    if not str(getattr(config, "group_folder_id", "") or "").strip():
        return
    try:
        from . import group as group_module

        drive = client
        if drive is None:
            # A second client, because the shared folder is normally in somebody else's
            # drive: this copy's own client is pinned to this person's drive, which is the
            # whole reason a shared folder is needed.
            owner = str(getattr(config, "group_drive_user_id", "") or "").strip()
            drive = _graph_for(config, user_id=owner) if owner else _graph_for(config)

        status = group_module.status_of(
            config, counts=digest.counts, spend=getattr(digest, "spend", None),
            held=getattr(digest, "held", None),
        )
        group_module.write_status(config, status, client=drive)

        if not group_module.is_admin(config):
            return
        peers = group_module.read_statuses(config, client=drive, now=now)
        report = group_module.GroupReport(
            day=digest.day,
            peers=tuple(peers),
            silent_after_hours=int(getattr(config, "group_silent_after_hours", 36) or 36),
        )
        body = group_module.render_group_email(report, priced_on=prices.CHECKED_ON)
        result = send_group(config, group_module.subject_for_group(report), body,
                            smtp_factory=smtp_factory)
        if not result.ok:
            log.warning("the group email could not be sent: %s", result.detail)
    except Exception:  # noqa: BLE001 - see the docstring; this may never break a morning
        log.warning("the group view could not be updated this morning", exc_info=True)


def _graph_for(config: Any, *, user_id: str = "") -> Any:
    """A Graph client for the shared folder's drive, or this copy's own.

    Imported here rather than at module scope: the digest is built and rendered in tests
    that have no credentials, and a module-level import of the Graph client would make
    reading an email require a tenant.
    """
    from .graph import GraphClient

    if user_id:
        return GraphClient.from_config(config, user_id=user_id)
    return GraphClient.from_config(config)


def should_run(config: Any, ledger: Ledger, *, now: float | None = None) -> bool:
    """True once a local day from the configured hour, with a throttle on failed retries."""
    clock = time.time() if now is None else now
    moment = local_now(config, clock)
    if moment.hour < int(getattr(config, "digest_hour", 6) or 0):
        return False
    if ledger.cursor_get(DIGEST_DAY_MARK) == moment.date().isoformat():
        return False
    last_attempt = parse_stamp(ledger.cursor_get(DIGEST_ATTEMPT_MARK))
    if last_attempt is not None and (clock - last_attempt) < RETRY_AFTER_S:
        return False
    return True


def mark_run(config: Any, ledger: Ledger, *, now: float | None = None) -> None:
    ledger.cursor_set(DIGEST_DAY_MARK, local_now(config, now).date().isoformat())
