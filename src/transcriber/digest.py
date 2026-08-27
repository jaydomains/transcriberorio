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
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Callable, Mapping, Sequence

from .heartbeat import Heartbeat, PingResult
from .ledger import Ledger
from .models import (
    DigestCounts,
    State,
    contains_email,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
    utc_now_iso,
)
from .sweep import local_now, parse_stamp

log = logging.getLogger("transcriber.digest")

__all__ = [
    "Digest",
    "credential_warnings",
    "SendResult",
    "DigestResult",
    "build",
    "send",
    "run",
    "should_run",
    "mark_run",
    "subject_for",
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


def subject_for(counts: DigestCounts, open_failures: int) -> str:
    """The whole message, in the line he sees on his phone before opening anything."""
    if counts.discovered == 0:
        if open_failures:
            return f"⚠ Recordings: nothing arrived yesterday, {open_failures} still FAILED"
        return "⚠ Recordings: nothing arrived yesterday"
    if open_failures == 0:
        if counts.skipped_empty:
            return f"Recordings: all {counts.discovered} done ({counts.skipped_empty} silent)"
        return f"Recordings: all {counts.discovered} done"
    return f"Recordings: {counts.done} done, {open_failures} FAILED"


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

    @property
    def needs_a_person(self) -> bool:
        return self.open_failures > 0 or self.counts.nothing_arrived

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
    if sweep_report is None:
        sweep_report = _stored_report(ledger, "sweep")
    if archive_report is None:
        archive_report = _stored_report(ledger, "archive")

    subject = subject_for(counts, len(failures))
    body = _render(
        config,
        counts,
        subject=subject,
        moment=moment,
        today_failures=today_failures,
        older_failures=older_failures,
        stats=ledger.stats(),
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
        index = 0
        for failure in list(today_failures) + list(older_failures):
            index += 1
            lines += _failure_block(index, failure, counts.day)
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


def _failure_block(index: int, failure: Mapping[str, Any], day: str) -> list[str]:
    name = str(failure.get("name") or failure.get("item_id") or "an unnamed recording")
    attempts = int(failure.get("attempts") or 0)
    discovered = str(failure.get("discovered_at") or "")
    raw = str(failure.get("reason") or "").strip()
    block = [f"{index}. {name}"]
    for chunk in _wrap(plain_reason(failure)):
        block.append(f"     {chunk}")
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
