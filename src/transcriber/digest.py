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
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Mapping, Sequence

from .heartbeat import Heartbeat, PingResult
from .ledger import Ledger
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
from .sweep import local_now, parse_stamp, routes_of

log = logging.getLogger("transcriber.digest")

__all__ = [
    "Digest",
    "RouteDigest",
    "RouteQueue",
    "QueueReport",
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

    @property
    def needs_a_person(self) -> bool:
        return (
            self.open_failures > 0
            or self.counts.nothing_arrived
            or bool(self.route_disagreements)
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


@dataclass
class DigestResult:
    digest: Digest
    sent: SendResult
    ping: PingResult | None = None

    @property
    def ok(self) -> bool:
        return self.sent.ok


# --------------------------------------------------------------------------- building


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
    disagreements = route_disagreements(ledger, target)
    if sweep_report is None:
        sweep_report = _stored_report(ledger, "sweep")
    if archive_report is None:
        archive_report = _stored_report(ledger, "archive")

    _stopped, _queued = split_stopped_from_queued(failures, now=clock, config=config)
    subject = subject_for(counts, _stopped, _queued)
    body = _render(
        config,
        counts,
        subject=subject,
        moment=moment,
        today_failures=today_failures,
        older_failures=older_failures,
        stats=ledger.stats(),
        routes=routes,
        queue=queue,
        disagreements=disagreements,
        sweep_report=sweep_report,
        archive_report=archive_report,
        service_error=service_error,
        attention=attention,
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
    routes: Sequence["RouteDigest"] = (),
    queue: "QueueReport | None" = None,
    disagreements: Sequence[Mapping[str, Any]] = (),
    archive_report: Any,
    service_error: str = "",
    attention: Mapping[str, Any] | None = None,
    expiries: Sequence[tuple[int, str]] = (),
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
    if facts.get("review") or facts.get("unverified_duration_guard") or facts.get("degraded_transcripts"):
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


def send(
    config: Any,
    digest: Digest,
    *,
    smtp_factory: Callable[..., Any] | None = None,
) -> SendResult:
    """Send the digest as one plain-text part. Never raises; the caller must see failure."""
    recipients = tuple(getattr(config, "smtp_to", ()) or ())
    host = str(getattr(config, "smtp_host", "") or "")
    port = int(getattr(config, "smtp_port", 587) or 587)
    if not recipients or not host:
        detail = "no SMTP host or recipient is configured, so the morning digest cannot be sent"
        log.error("%s", detail)
        return SendResult(ok=False, detail=detail, recipients=len(recipients), host=host)

    message = EmailMessage()
    message["Subject"] = digest.subject
    message["From"] = str(getattr(config, "smtp_from", "") or "")
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(digest.body, subtype="plain", charset="utf-8")

    try:
        with _connect(config, host, port, smtp_factory) as server:
            server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - every send failure is reported, none is raised
        detail = f"{type(exc).__name__}: {exc}"
        scrub = getattr(config, "scrub", None)
        if callable(scrub):
            detail = scrub(detail)
        # ERROR, not WARNING: an unsendable digest is the failure mode this service exists
        # to remove, and it must be loud even though nothing raises.
        log.error("the morning digest could NOT be sent via %s:%s — %s", host, port, detail)
        return SendResult(ok=False, detail=detail, recipients=len(recipients), host=host)

    log.info(
        "morning digest sent to %d recipient(s) via %s:%s — %s",
        len(recipients), host, port, digest.subject,
    )
    return SendResult(ok=True, recipients=len(recipients), host=host)


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
    sent = send(config, digest, smtp_factory=smtp_factory)
    # Written after the build, never during it: tomorrow's "is it growing?" is answered
    # against what this morning's email actually reported, and a dry run or a `status` that
    # rewrote the history would make the answer depend on who looked.
    record_queue_depth(ledger, digest.queue.day or digest.day, digest.queue.queued)

    monitor = heartbeat if heartbeat is not None else Heartbeat.from_config(config)
    if sent.ok and not digest.alarm:
        mark_run(config, ledger, now=clock)
        ledger.cursor_set(DIGEST_ERROR_MARK, "")
        ping = monitor.success(digest.subject)
    elif sent.ok:
        # The email went out and says something is wrong. The day is still marked sent — this
        # is not a send failure and must not become a mail loop — but the monitor is told, so
        # a weekend of "nothing arrived" cannot pass with the alarm sitting green.
        mark_run(config, ledger, now=clock)
        ledger.cursor_set(DIGEST_ERROR_MARK, "")
        ping = monitor.fail(digest.subject)
    else:
        ledger.cursor_set(DIGEST_ERROR_MARK, sent.detail[:500])
        # Actively alert rather than wait for the monitor's grace period to lapse: the one
        # thing worse than a broken morning is a broken morning nobody hears about.
        ping = monitor.fail(f"the morning digest could not be sent: {sent.detail}")

    return DigestResult(digest=digest, sent=sent, ping=ping)


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
