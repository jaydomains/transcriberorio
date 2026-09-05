"""The little HTTPS server the morning email links to, and the only place a hold is answered.

This page holds the most sensitive text in the whole service. Everywhere else, a held
passage is a row in a database on a server; here it is a sentence on a phone screen, on a
site, with somebody standing next to him. So the rules here are stricter than anywhere else
in the codebase, and each one exists because of a specific way this could go wrong:

**A capability, per reviewer, per day.** There is no password and no login form. The morning
email carries a link containing a token — long, random, ``secrets.token_urlsafe`` — that is
good for that person, for that day, and expires. What is stored is not the token: it is a
selector and the SHA-256 of a verifier, so a copy of the token database grants nobody
anything, and the verifier is compared with :func:`hmac.compare_digest` rather than ``==``.
Issuing today's link revokes yesterday's, so exactly one live link exists per person, and
:meth:`TokenStore.revoke_for` kills it immediately when a phone goes missing.

**Scoped in the query, not in the template.** A session names exactly one reviewer, and the
only call in this file that returns held text is ``WithheldStore.queue_for(<that name>)``,
which answers for one named person and refuses to answer for everybody. No request
parameter names a reviewer; there is nothing to tamper with. James's own link runs the same
query for his own name, and everybody else's queue reaches him through
``WithheldStore.overview()``, which returns counts, sites and ages and has no text in it at
all. That is decision 6, and it is the decision that keeps staff willing to record: staff
record voluntarily, and one discovering that the boss reads their held words stops keeping a
folder — after which the recordings are gone, which is the loss this service exists to cure.

**Held words never leave in a place that gets written down.** Not in a URL, not in a query
string, not in a redirect, not in a log line. The default
:meth:`BaseHTTPRequestHandler.log_message` writes the raw request line — token and all — to
stderr, so it is overridden here to write nothing; this file logs a route name it chose
itself, a status and a duration, and never a path from the wire. ``Referrer-Policy:
no-referrer`` and a ``default-src 'none'`` CSP mean the page makes no request to anywhere
and leaks nothing through one. The token does have to ride in the emailed link once: the
first request trades it for a ``__Host-`` cookie and redirects to a clean path, so it leaves
the address bar immediately.

**An answer is idempotent, and undoable for a few seconds.** Answers are POSTs carrying both
the capability token and a CSRF token derived from it. The same answer twice is the same
answer, not an error; a *different* answer from a second device is a conflict shown plainly
rather than an overwrite. And an answer does not reach the store at once: it waits out an
undo window in this process, so a mis-tap is taken back with a tap rather than with a
support call. If the process dies inside that window the answer is lost and the passage is
still held — which is the safe direction, and visible in tomorrow's email.

**Nothing here decides anything.** There is no deadline that releases, no cap that commits
the overflow, and no rule that writes itself. The only thing that turns a held passage into
a released one is a person tapping a button, and the name that reaches
:meth:`WithheldStore.decide` is that person's.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, Sequence

from . import review_page as page_mod
from .logging_setup import get_logger
from .models import day_of, utc_now_iso
from .naming import parse_source_name
from .review_page import (
    Elsewhere,
    Flash,
    Item,
    Page,
    Recording,
    display_name,
)
from .withheld import (
    CATEGORY_DESCRIPTION,
    CATEGORY_PHRASE,
    MODE_ON,
    MODE_SHADOW,
    AlreadyDecidedError,
    Decision,
    HeldRecord,
    WithheldStore,
    normalise_mode,
)

log = get_logger(__name__)

__all__ = [
    "ReviewError",
    "IssuedToken",
    "Session",
    "TokenStore",
    "Outcome",
    "ReviewService",
    "ReviewHandler",
    "build_server",
    "serve",
    "link_for",
    "principal_of",
    "store_for",
    "store_path_for",
    "service_from_config",
    "links_for_pending",
    "main",
    "COOKIE_NAME",
    "DEFAULT_TOKEN_HOURS",
    "DEFAULT_UNDO_SECONDS",
]

#: ``__Host-`` is not decoration: a browser only accepts it when the cookie is ``Secure``,
#: path ``/`` and carries no ``Domain``, which is exactly the cookie we want and rules out a
#: sibling host setting one on our behalf.
COOKIE_NAME = "__Host-kbc_review"

#: How long an emailed link is good for. Long enough that a 06:00 email still opens at
#: lunch the next day, short enough that a forwarded screenshot of a link goes stale.
DEFAULT_TOKEN_HOURS = 36

#: The undo window. Long enough to catch the thumb, short enough that nobody waits for it.
DEFAULT_UNDO_SECONDS = 8

#: The biggest POST body this server will read. An answer is a few hundred bytes.
MAX_BODY_BYTES = 16 * 1024

#: Bad tokens allowed from one address before it is asked to stop, and for how long.
BAD_TOKEN_LIMIT = 10
BAD_TOKEN_WINDOW_S = 300

_ANSWERS = {"release": Decision.RELEASED, "refuse": Decision.REFUSED}


class ReviewError(RuntimeError):
    """A refusal this module makes loudly, never quietly."""


# --------------------------------------------------------------------------- the tokens


@dataclass(frozen=True)
class IssuedToken:
    """A freshly minted link. The only moment the whole token exists in this process."""

    token: str
    selector: str
    reviewer: str
    day: str
    expires_at: str

    def url(self, base_url: str) -> str:
        return link_for(base_url, self.token)


@dataclass(frozen=True)
class Session:
    """Who is signed in, for this request. Built only by :meth:`TokenStore.verify`.

    ``reviewer`` is the only name any query in this file is ever given. It comes off the
    token row and never off the request, so there is no parameter anybody could edit to read
    somebody else's held passages.
    """

    reviewer: str
    selector: str
    day: str
    expires_at: str
    csrf: str
    is_principal: bool = False


class TokenStore:
    """Per-reviewer, per-day capability tokens, stored as hashes and never as tokens.

    The row holds a *selector* (a public handle, used for the lookup) and the SHA-256 of a
    *verifier* (the secret half). The lookup is by selector so that the secret comparison is
    a single :func:`hmac.compare_digest` on a fixed-length digest rather than a database
    match on a secret, and a stolen copy of this file is worth nothing: the tokens it
    describes cannot be reconstructed from it.
    """

    def __init__(self, path: str, *, busy_timeout_ms: int = 30_000) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        self._memory = path == ":memory:" or str(path).startswith("file::memory:")
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if not self._memory:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._migrate()
        self._restrict_permissions()

    # -- where it lives ------------------------------------------------------------

    @staticmethod
    def path_beside(held_store_path: str) -> str:
        """Beside the held passages, because it is the key to them and shares their fate."""
        if held_store_path in (":memory:", "") or str(held_store_path).startswith("file::memory:"):
            return ":memory:"
        base, ext = os.path.splitext(held_store_path)
        return f"{base}-tokens{ext or '.db'}"

    @classmethod
    def from_config(cls, config: Any) -> "TokenStore":
        return cls(cls.path_beside(store_path_for(config)))

    # -- connections ---------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        self._restrict_permissions()
        return conn

    def _conn(self) -> sqlite3.Connection:
        if self._memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._connect()
                return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _restrict_permissions(self) -> None:
        """0600 on the database and on the WAL files beside it, on every connection.

        Like the ledger and like the held-passage store, and for the same reason as both:
        SQLite writes ``-wal`` and ``-shm`` beside the database carrying the same rows, so a
        database at 0600 with its write-ahead log at 0644 is a database at 0644. This file
        is the key to the held text — it names every reviewer and holds the digest of every
        live link — and it is redone on every connection because a WAL removed by a
        checkpoint comes back with the process umask when the next write recreates it.
        """
        if self._memory:
            return
        for suffix in ("", "-wal", "-shm"):
            target = self.path + suffix
            try:
                if os.path.exists(target):
                    os.chmod(target, 0o600)
            except OSError as exc:  # noqa: PERF203 - a filesystem that cannot, said out loud
                log.warning(
                    "token-store-permissions",
                    "could not restrict the review token database to its owner",
                    file=os.path.basename(target),
                    error=str(exc),
                )

    def _migrate(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS review_tokens (
                selector      TEXT PRIMARY KEY,
                verifier_hash TEXT NOT NULL,
                reviewer      TEXT NOT NULL,
                day           TEXT NOT NULL,
                issued_at     TEXT NOT NULL,
                expires_at    TEXT NOT NULL,
                revoked_at    TEXT NOT NULL DEFAULT '',
                revoked_why   TEXT NOT NULL DEFAULT '',
                last_used_at  TEXT NOT NULL DEFAULT '',
                uses          INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS review_tokens_reviewer
                ON review_tokens (reviewer, day);
            """
        )

    def close(self) -> None:
        with self._lock:
            for conn in (self._shared, getattr(self._local, "conn", None)):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._shared = None
            self._local.conn = None

    # -- minting -------------------------------------------------------------------

    def issue(
        self,
        reviewer: str,
        *,
        hours: int = DEFAULT_TOKEN_HOURS,
        now: float | None = None,
        supersede: bool = True,
    ) -> IssuedToken:
        """Mint one link for one person.

        ``supersede`` — on by default — revokes that person's other live tokens, so there is
        exactly one working link at a time. Yesterday's email stops working when today's
        arrives, which is what somebody would expect and is one fewer live capability.
        """
        who = (reviewer or "").strip()
        if not who:
            raise ReviewError("a review link belongs to a named person, and no name was given")
        stamp = time.time() if now is None else float(now)
        selector = secrets.token_urlsafe(9)
        verifier = secrets.token_urlsafe(32)
        issued = utc_now_iso(stamp)
        expires = utc_now_iso(stamp + max(1, int(hours)) * 3600)
        conn = self._conn()
        with self._lock:
            if supersede:
                conn.execute(
                    "UPDATE review_tokens SET revoked_at=?, revoked_why=? "
                    "WHERE reviewer=? AND revoked_at='' AND expires_at>?",
                    (issued, "a newer link was issued", who, issued),
                )
            conn.execute(
                "INSERT INTO review_tokens (selector, verifier_hash, reviewer, day, issued_at,"
                " expires_at) VALUES (?,?,?,?,?,?)",
                (selector, _hash(verifier), who, day_of(issued), issued, expires),
            )
        log.info(
            "review-link-issued",
            "a review link was minted",
            day=day_of(issued),
            expires_at=expires,
            hours=int(hours),
        )
        return IssuedToken(
            token=f"{selector}.{verifier}",
            selector=selector,
            reviewer=who,
            day=day_of(issued),
            expires_at=expires,
        )

    # -- checking ------------------------------------------------------------------

    def verify(self, token: str, *, now: float | None = None, principal: str = "") -> Session | None:
        """The signed-in person, or ``None``. Constant-time on the secret half.

        Every failure returns the same ``None`` and takes the same shape of work: an unknown
        selector still runs a comparison against a dummy digest, so "no such token" and
        "wrong token" do not answer at measurably different speeds.
        """
        raw = (token or "").strip()
        selector, _, verifier = raw.partition(".")
        stamp = utc_now_iso(time.time() if now is None else float(now))
        row = None
        if selector and verifier:
            row = self._conn().execute(
                "SELECT * FROM review_tokens WHERE selector=?", (selector,)
            ).fetchone()
        stored = str(row["verifier_hash"]) if row is not None else _hash("")
        if not hmac.compare_digest(stored, _hash(verifier)):
            return None
        if row is None:  # pragma: no cover - only reachable if the dummy digest ever matched
            return None
        if str(row["revoked_at"]):
            return None
        if str(row["expires_at"]) <= stamp:
            return None
        try:
            self._conn().execute(
                "UPDATE review_tokens SET uses=uses+1, last_used_at=? WHERE selector=?",
                (stamp, selector),
            )
        except sqlite3.Error as exc:  # noqa: PERF203 - a read-only disk must not lock him out
            log.warning("token-use-not-recorded", "could not record a link being used", error=str(exc))
        reviewer = str(row["reviewer"])
        return Session(
            reviewer=reviewer,
            selector=selector,
            day=str(row["day"]),
            expires_at=str(row["expires_at"]),
            csrf=_csrf_for(stored, selector),
            is_principal=bool(principal) and reviewer.casefold() == principal.strip().casefold(),
        )

    # -- revoking ------------------------------------------------------------------

    def revoke(self, selector: str, *, why: str = "revoked", now: float | None = None) -> bool:
        stamp = utc_now_iso(time.time() if now is None else float(now))
        with self._lock:
            cur = self._conn().execute(
                "UPDATE review_tokens SET revoked_at=?, revoked_why=? WHERE selector=? AND revoked_at=''",
                (stamp, why, (selector or "").strip()),
            )
        return bool(cur.rowcount)

    def revoke_for(self, reviewer: str, *, why: str = "revoked", now: float | None = None) -> int:
        """Kill every live link one person holds. The answer to a lost phone."""
        stamp = utc_now_iso(time.time() if now is None else float(now))
        with self._lock:
            cur = self._conn().execute(
                "UPDATE review_tokens SET revoked_at=?, revoked_why=? WHERE reviewer=? AND revoked_at=''",
                (stamp, why, (reviewer or "").strip()),
            )
        count = int(cur.rowcount or 0)
        log.info("review-links-revoked", "review links were revoked", count=count, why=why)
        return count

    def live_for(self, reviewer: str, *, now: float | None = None) -> int:
        stamp = utc_now_iso(time.time() if now is None else float(now))
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM review_tokens WHERE reviewer=? AND revoked_at='' AND expires_at>?",
            ((reviewer or "").strip(), stamp),
        ).fetchone()
        return int(row["n"])


def _hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _csrf_for(verifier_hash: str, selector: str) -> str:
    """A CSRF token derived from the stored secret, so it survives a restart.

    Belt and braces: a cross-site form already cannot forge one of these submissions,
    because the submission carries the capability token itself and no other site knows it.
    This is the second lock, and it costs one HMAC.
    """
    try:
        key = bytes.fromhex(verifier_hash)
    except ValueError:  # pragma: no cover - only if the column were corrupted
        key = verifier_hash.encode("utf-8")
    return hmac.new(key, b"kbc-review-csrf|" + selector.encode("utf-8"), hashlib.sha256).hexdigest()


def link_for(base_url: str, token: str) -> str:
    """The address that goes in the morning email.

    The token rides in the query string exactly once, because an emailed link has nowhere
    else to carry it. The first request trades it for a cookie and redirects to a clean
    path, the page sends ``Referrer-Policy: no-referrer``, and this server never writes a
    request path to a log. Held words are never in a URL under any circumstances; this is a
    capability, and it is short-lived, single-holder and revocable for that reason.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ReviewError(
            "GATE_REVIEW_BASE_URL is not set, so there is no address to send anybody to"
        )
    return f"{base}/?k={urllib.parse.quote(token, safe='')}"


# --------------------------------------------------------------------------- the service


@dataclass(frozen=True)
class Outcome:
    """What happened to one answer, in a shape both the page and the JSON reply can use."""

    ok: bool
    state: str          # queued | recorded | already | conflict | undone | too-late
                        # | refused-scope | shadow | bad | stale
    message: str
    hold_id: str = ""
    ref: str = ""
    answer: str = ""    # released | refused
    undo_until_ms: int = 0

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "message": self.message,
            "ref": self.ref,
            "answer": self.answer,
            "undo_until_ms": self.undo_until_ms,
        }


@dataclass
class _Pending:
    """An answer a person has given, waiting out its undo window in this process."""

    hold_id: str
    ref: str
    reviewer: str
    answer: str
    note: str
    answered_at: str
    due: float
    undo_until_ms: int
    attempts: int = 0


class ReviewService:
    """Everything the page does, with no HTTP in it — which is what makes it testable.

    The HTTP layer below turns a request into a :class:`Session` and calls one of three
    methods here. Nothing in this class reads a request, and nothing in it takes a reviewer
    name from anywhere but a verified session.
    """

    def __init__(
        self,
        store: WithheldStore,
        tokens: TokenStore,
        *,
        principal: str = "",
        mode: str = MODE_ON,
        timezone_name: str = page_mod.DEFAULT_TZ,
        undo_seconds: int = DEFAULT_UNDO_SECONDS,
        route_labels: Mapping[str, str] | None = None,
        on_decision: Callable[[HeldRecord], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.tokens = tokens
        self.principal = (principal or "").strip()
        self.mode = normalise_mode(mode, MODE_ON)
        self.timezone_name = timezone_name
        self.undo_seconds = max(0, int(undo_seconds))
        self.route_labels = dict(route_labels or {})
        self._on_decision = on_decision
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._flash: dict[str, list[Flash]] = {}

    # -- answering -----------------------------------------------------------------

    def answer(self, session: Session, hold_id: str, answer: str, *, note: str = "") -> Outcome:
        """Record one person's answer to one held passage. Idempotent, and undoable briefly."""
        self.commit_due()
        decision = _ANSWERS.get((answer or "").strip().lower(), "")
        if not decision:
            return Outcome(False, "bad", "That is not an answer this page knows.")
        # The whole decision — reading the row, checking who owns it, and queueing the
        # answer — happens under one lock. Reading first and locking afterwards leaves a
        # gap in which another device's answer is written between the read and the queue,
        # and the second person is told their answer was taken when it was not.
        with self._lock:
            record = self.store.get((hold_id or "").strip())
            # One reply for "no such passage" and for "not yours". Telling them apart tells
            # somebody guessing hold ids which half of the guess was right.
            if record is None or record.reviewer != session.reviewer:
                if record is not None:
                    log.warning(
                        "review-scope-refused",
                        "an answer named a passage that belongs to somebody else's queue",
                        ref=record.ref,
                    )
                return Outcome(False, "refused-scope", "That passage is not on your list.")
            if record.mode != MODE_ON:
                return Outcome(
                    False,
                    "shadow",
                    "Nothing was held back for that one, so there is nothing to put back.",
                    ref=record.ref,
                )
            waiting = self._pending.get(record.hold_id)
            if waiting is not None:
                if waiting.answer == decision:
                    # The same answer arriving twice — a flaky signal, a second tap, a second
                    # device. It is the same answer, so it is not an error and the undo
                    # window is not extended by it.
                    return self._queued(waiting, "queued")
                waiting = self._replace_pending(record, decision, note)
                return self._queued(waiting, "queued")
            if record.decision in Decision.DECIDABLE:
                if record.decision == decision:
                    return Outcome(
                        True,
                        "already",
                        f"Already recorded: {_said(decision)}.",
                        hold_id=record.hold_id,
                        ref=record.ref,
                        answer=decision,
                    )
                return Outcome(
                    False,
                    "conflict",
                    f"{display_name(record.answered_by) or 'Somebody'} answered this already "
                    f"— {_said(record.decision)}. Nothing was changed.",
                    hold_id=record.hold_id,
                    ref=record.ref,
                    answer=record.decision,
                )
            if self.undo_seconds <= 0:
                self._commit(
                    _Pending(
                        hold_id=record.hold_id,
                        ref=record.ref,
                        reviewer=session.reviewer,
                        answer=decision,
                        note=note,
                        answered_at=utc_now_iso(self._wall()),
                        due=self._clock(),
                        undo_until_ms=0,
                    )
                )
                return Outcome(
                    True, "recorded", f"Recorded: {_said(decision)}.",
                    hold_id=record.hold_id, ref=record.ref, answer=decision,
                )
            fresh = self._replace_pending(record, decision, note)
            return self._queued(fresh, "queued")

    def _replace_pending(self, record: HeldRecord, decision: str, note: str) -> _Pending:
        entry = _Pending(
            hold_id=record.hold_id,
            ref=record.ref,
            reviewer=record.reviewer,
            answer=decision,
            note=note,
            answered_at=utc_now_iso(self._wall()),
            due=self._clock() + self.undo_seconds,
            undo_until_ms=int((self._wall() + self.undo_seconds) * 1000),
        )
        self._pending[record.hold_id] = entry
        return entry

    def _queued(self, entry: _Pending, state: str) -> Outcome:
        return Outcome(
            True,
            state,
            f"{_said(entry.answer)}. Undo within {self.undo_seconds} seconds.",
            hold_id=entry.hold_id,
            ref=entry.ref,
            answer=entry.answer,
            undo_until_ms=entry.undo_until_ms,
        )

    def undo(self, session: Session, hold_id: str) -> Outcome:
        """Take an answer back, if it has not been written yet."""
        self.commit_due()
        with self._lock:
            entry = self._pending.get((hold_id or "").strip())
            if entry is not None and entry.reviewer == session.reviewer:
                del self._pending[entry.hold_id]
                log.info("review-undone", "an answer was taken back inside the undo window", ref=entry.ref)
                return Outcome(True, "undone", "Undone. It is waiting again.", hold_id=entry.hold_id, ref=entry.ref)
        return Outcome(
            False,
            "too-late",
            "That has been recorded already. Nothing was changed.",
            hold_id=(hold_id or "").strip(),
        )

    # -- the undo window -----------------------------------------------------------

    def commit_due(self, *, force: bool = False) -> int:
        """Write every answer whose undo window has closed. Called on every request.

        Called on the way in to every request rather than only from a timer, so that a
        service with no background thread — a test, a single-worker deployment — still
        behaves identically. The background thread exists so that an answer given by
        somebody who then puts their phone in their pocket lands anyway.
        """
        now = self._clock()
        with self._lock:
            ready = [p for p in self._pending.values() if force or p.due <= now]
            for entry in ready:
                self._pending.pop(entry.hold_id, None)
        written = 0
        for entry in ready:
            if self._commit(entry):
                written += 1
        return written

    def _commit(self, entry: _Pending) -> bool:
        try:
            with self._lock:
                record = self.store.decide(
                    entry.hold_id,
                    entry.answer,
                    answered_by=entry.reviewer,
                    note=entry.note,
                    at=entry.answered_at,
                )
        except AlreadyDecidedError as exc:
            # Another device got there first. Both answers are in the events table; nothing
            # is overwritten and nothing is invented.
            log.warning("review-already-decided", str(exc), ref=entry.ref)
            return False
        except Exception as exc:  # noqa: BLE001 - a failed write must not lose the answer
            entry.attempts += 1
            if entry.attempts <= 5:
                entry.due = self._clock() + 30.0
                with self._lock:
                    self._pending.setdefault(entry.hold_id, entry)
                log.error(
                    "review-decision-not-written",
                    "an answer could not be written and will be retried",
                    ref=entry.ref, attempt=entry.attempts, error=str(exc),
                )
            else:
                log.error(
                    "review-decision-lost",
                    "an answer could not be written after five attempts; the passage is "
                    "still held and still in tomorrow's email",
                    ref=entry.ref, error=str(exc),
                )
            return False
        log.info(
            "review-decision-recorded",
            "a person answered a held passage",
            ref=record.ref, decision=record.decision, item=record.item_id,
        )
        if self._on_decision is not None:
            try:
                self._on_decision(record)
            except Exception as exc:  # noqa: BLE001 - republishing is not this page's job to guarantee
                log.error(
                    "review-followup-failed",
                    "the decision is recorded, but what happens next did not run",
                    ref=record.ref, error=str(exc), exc_info=True,
                )
        return True

    def flush(self) -> int:
        """Write every waiting answer now. Called when the server stops.

        A person gave these answers; dropping them on a restart would lose a decision a
        person made, which is a different failure from the one the undo window exists for.
        """
        return self.commit_due(force=True)

    # -- messages ------------------------------------------------------------------

    def add_flash(self, session: Session, text: str, tone: str = "warn") -> None:
        with self._lock:
            self._flash.setdefault(session.selector, []).append(Flash(text, tone))

    def _take_flashes(self, session: Session) -> tuple[Flash, ...]:
        with self._lock:
            return tuple(self._flash.pop(session.selector, ()))

    # -- the page ------------------------------------------------------------------

    def page_for(self, session: Session) -> Page:
        """One reviewer's page. The only text-bearing query in this file is in here."""
        self.commit_due()
        recordings = self._recordings_for(session)
        elsewhere, summary = self._elsewhere_for(session)
        return Page(
            reviewer=display_name(session.reviewer),
            csrf=session.csrf,
            token="",  # filled in by the handler, which is the only place the token exists
            recordings=recordings,
            elsewhere=elsewhere,
            elsewhere_summary=summary,
            flashes=self._take_flashes(session),
            mode=self.mode,
            shadow_note=self._shadow_note() if self.mode == MODE_SHADOW else "",
            timezone_name=self.timezone_name,
            undo_seconds=self.undo_seconds,
            is_principal=session.is_principal,
            generated_at=utc_now_iso(self._wall()),
        )

    def _recordings_for(self, session: Session) -> tuple[Recording, ...]:
        groups = self.store.grouped_for(session.reviewer)
        with self._lock:
            waiting = dict(self._pending)
        out: list[Recording] = []
        for group in groups:
            items: list[Item] = []
            for record in group["records"]:
                if record.reviewer != session.reviewer:
                    # Unreachable: queue_for filters on the reviewer in SQL. Kept because a
                    # future edit to that query must fail loudly here rather than quietly
                    # put a staff member's words on somebody else's screen.
                    log.error(
                        "review-scope-leak",
                        "a queue returned a passage belonging to another reviewer; it was dropped",
                        ref=record.ref,
                    )
                    continue
                items.append(self._item(record, waiting.get(record.hold_id)))
            if not items:
                continue
            out.append(
                Recording(
                    item_id=str(group["item_id"]),
                    title=_title_of(str(group.get("source_name") or ""), str(group["item_id"])),
                    site=str(group.get("site") or ""),
                    who=_who_of(str(group.get("source_name") or ""), group["records"]),
                    recorded_at=str(group.get("recorded_at") or group.get("held_at") or ""),
                    route_label=self.route_labels.get(str(group.get("route") or ""), ""),
                    items=tuple(items),
                )
            )
        return tuple(out)

    def _item(self, record: HeldRecord, waiting: _Pending | None) -> Item:
        what = _sentence(CATEGORY_PHRASE.get(record.category, "something held for review"))
        subject = (record.subject or "").strip()
        if subject and subject.casefold() != CATEGORY_PHRASE.get(record.category, "").casefold():
            what = f"{what} — {subject}"
        reason = (record.reason or "").strip() or CATEGORY_DESCRIPTION.get(record.category, "")
        return Item(
            hold_id=record.hold_id,
            ref=record.ref,
            what=what,
            subject=subject,
            reason=reason,
            before=record.context_before,
            words=record.text,
            after=record.context_after,
            speaker=(record.speaker or "").strip(),
            held_at=record.held_at,
            age_days=record.age_days(utc_now_iso(self._wall())),
            unsure=record.confidence is not None and float(record.confidence) < 0.55,
            answered="" if waiting is None else waiting.answer,
            undo_until_ms=0 if waiting is None else waiting.undo_until_ms,
        )

    def _elsewhere_for(self, session: Session) -> tuple[tuple[Elsewhere, ...], str]:
        """What he is allowed to know about queues that are not his: how many, where, how old.

        Built from ``overview()``, which returns counts and sites and carries no text. There
        is deliberately no per-person site list here, because the only query that could
        produce one is the one that also returns the words.
        """
        if not session.is_principal:
            return (), ""
        try:
            overview = self.store.overview()
        except Exception as exc:  # noqa: BLE001 - a summary must not take the page down
            log.error("review-overview-failed", "could not summarise the other queues", error=str(exc))
            return (), ""
        rows: list[Elsewhere] = []
        for who, count in sorted(overview.get("by_reviewer", {}).items(), key=lambda kv: -kv[1]):
            if not who or who == session.reviewer or who == "unassigned":
                continue
            rows.append(Elsewhere(who=display_name(who), count=int(count)))
        if not rows:
            return (), ""
        sites = [name for name in overview.get("by_site", {}) if name and name != "no site named"]
        parts = []
        if sites:
            parts.append("Sites with something waiting: " + ", ".join(sorted(sites)[:6]) + ".")
        oldest = int(overview.get("oldest_age_days") or 0)
        if oldest:
            parts.append(f"The oldest anywhere has been waiting {oldest} day{'' if oldest == 1 else 's'}.")
        return tuple(rows), " ".join(parts)

    def _shadow_note(self) -> str:
        try:
            measured = self.store.measurement()
        except Exception as exc:  # noqa: BLE001
            log.error("review-measurement-failed", "could not read the shadow measurement", error=str(exc))
            return ""
        recordings = int(measured.get("recordings_classified") or 0)
        if not recordings:
            return "Nothing has been read for it yet."
        touched = int(measured.get("recordings_with_a_hold") or 0)
        spans = int(measured.get("spans") or 0)
        per_day = measured.get("spans_per_day") or 0
        return (
            f"So far it has read {recordings} recording{'' if recordings == 1 else 's'} and would "
            f"have held {spans} passage{'' if spans == 1 else 's'} in {touched} of them — about "
            f"{per_day} a day. That is the number to look at before this is switched on."
        )


def _said(decision: str) -> str:
    return page_mod.RELEASED_SAID if decision == Decision.RELEASED else page_mod.REFUSED_SAID


def _sentence(phrase: str) -> str:
    text = (phrase or "").strip()
    return text[:1].upper() + text[1:] if text else text


def _title_of(source_name: str, item_id: str) -> str:
    name = (source_name or "").strip()
    if not name:
        return f"Recording {item_id[:8]}"
    return os.path.splitext(name)[0]


def _who_of(source_name: str, records: Sequence[HeldRecord]) -> str:
    """Who was on the call, as far as anything actually knows.

    The filename names the counterparty when the phone wrote it; a hand-typed site-meeting
    name does not, and then the speakers on the held passages are the honest answer. Nothing
    is guessed: with neither, the line is simply not printed.
    """
    party = ""
    if source_name:
        try:
            party = (parse_source_name(source_name).party or "").strip()
        except Exception:  # noqa: BLE001 - a filename must never break the page
            party = ""
    speakers = []
    for record in records:
        name = (record.speaker or "").strip()
        if name and name not in speakers:
            speakers.append(name)
    if party and speakers:
        return f"{party} ({', '.join(speakers[:3])})"
    return party or ", ".join(speakers[:3])


# --------------------------------------------------------------------------- throttling


class _Throttle:
    """A failure counter per address. Not a firewall: a speed limit on guessing."""

    def __init__(self, limit: int = BAD_TOKEN_LIMIT, window_s: int = BAD_TOKEN_WINDOW_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window_s]
            self._hits[key] = hits
            return len(hits) >= self.limit

    def failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            hits = [t for t in self._hits.get(key, ()) if now - t < self.window_s]
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 4096:  # a bounded map, whatever the internet does
                for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window_s]:
                    self._hits.pop(stale, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# --------------------------------------------------------------------------- the handler

#: The paths this server answers, and the name each is logged under. Nothing from the wire
#: is ever logged: what goes in a log line is one of these fixed strings.
_ROUTES = {
    "/": "entry",
    "/review": "page",
    "/review/answer": "answer",
    "/review/undo": "undo",
    "/healthz": "health",
}


class ReviewHandler(BaseHTTPRequestHandler):
    """One request. Reads a session, calls the service, writes a hardened response."""

    protocol_version = "HTTP/1.1"
    server_version = "kbc-review"
    sys_version = ""

    # -- the two things the base class does that we must not ------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - the base class's name
        """Write nothing.

        The default implementation writes the raw request line to stderr — which on the
        first request of the day is ``GET /?k=<the token that opens the queue> HTTP/1.1``.
        Every log line this server writes is emitted by :meth:`_respond` from a route name
        it chose itself.
        """

    def log_error(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.warning("review-http-error", "a request could not be parsed")

    def version_string(self) -> str:
        return "kbc-review"

    # -- plumbing -------------------------------------------------------------------

    @property
    def service(self) -> ReviewService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def tokens(self) -> TokenStore:
        return self.server.service.tokens  # type: ignore[attr-defined]

    def _client(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded and getattr(self.server, "trust_forwarded", False):
            return forwarded.split(",")[0].strip()[:64]
        return str(self.client_address[0]) if self.client_address else "?"

    def _wants_json(self) -> bool:
        return "application/json" in (self.headers.get("Accept", "") or "")

    def _read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            # The body is not read, so whatever is still on the socket would be parsed as
            # the next request. Close instead of leaving a connection half-consumed.
            self.close_connection = True
            return {}
        raw = self.rfile.read(length)
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items() if values}

    def _token(self, form: Mapping[str, str] | None = None) -> str:
        """The capability: from the body, then the emailed link, then the cookie.

        The link beats the cookie deliberately. Issuing this morning's link revokes
        yesterday's, so a phone still carrying yesterday's cookie would otherwise be told
        its link had expired at the exact moment it opened a fresh one — the person having
        done everything right.
        """
        if form and form.get("k"):
            return form["k"]
        from_link = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("k", [""])[0]
        if from_link:
            return from_link
        cookie = SimpleCookie(self.headers.get("Cookie", "") or "")
        if COOKIE_NAME in cookie:
            return cookie[COOKIE_NAME].value
        return ""

    def _session(self, form: Mapping[str, str] | None = None) -> Session | None:
        principal = self.service.principal
        return self.tokens.verify(self._token(form), principal=principal)

    # -- responses -------------------------------------------------------------------

    def _respond(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str = "text/html; charset=utf-8",
        nonce: str = "",
        route: str = "other",
        headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing about this page may be stored, framed, sniffed, or referred onward.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-cohort=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            + (f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; " if nonce else "")
            + "img-src data:; connect-src 'self'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        if getattr(self.server, "https", False):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        log.info("review-request", "", route=route, method=self.command, status=int(status))

    def _notice(self, status: int, title: str, message: str, *, detail: str = "", route: str = "other") -> None:
        nonce = secrets.token_urlsafe(16)
        body = page_mod.render_notice(title, message, nonce=nonce, detail=detail).encode("utf-8")
        self._respond(status, body, nonce=nonce, route=route)

    def _json(self, status: int, payload: Mapping[str, Any], *, route: str) -> None:
        self._respond(
            status,
            json.dumps(dict(payload)).encode("utf-8"),
            content_type="application/json; charset=utf-8",
            route=route,
        )

    def _no_session(self, route: str) -> None:
        """The same answer for expired, revoked, wrong and never-issued."""
        self.tokens_failed()
        if self._wants_json():
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"ok": False, "state": "expired", "message": "This link has expired. Open today's email."},
                route=route,
            )
            return
        self._notice(
            HTTPStatus.UNAUTHORIZED,
            "This link has expired",
            "Open the link in this morning's email instead. Nothing has been changed, and "
            "nothing has been released.",
            detail="Links are good for one person and about a day, on purpose.",
            route=route,
        )

    def tokens_failed(self) -> None:
        throttle = getattr(self.server, "throttle", None)
        if throttle is not None:
            throttle.failure(self._client())

    # -- GET ---------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        path = urllib.parse.urlsplit(self.path).path
        route = _ROUTES.get(path.rstrip("/") or "/", "other")
        if route == "health":
            self._respond(HTTPStatus.OK, b"ok\n", content_type="text/plain; charset=utf-8", route=route)
            return
        if route not in ("entry", "page"):
            self._notice(HTTPStatus.NOT_FOUND, "Nothing here", "There is no page at this address.")
            return
        throttle = getattr(self.server, "throttle", None)
        if throttle is not None and throttle.blocked(self._client()):
            self._notice(
                HTTPStatus.TOO_MANY_REQUESTS,
                "Too many tries",
                "Too many links have been tried from here. Wait a few minutes and open the "
                "link in this morning's email.",
                route=route,
            )
            return
        session = self._session()
        if session is None:
            self._no_session(route)
            return
        if throttle is not None:
            throttle.clear(self._client())
        token = self._token()
        # The token arrived in the emailed link. Trade it for a cookie and send the browser
        # to a clean path, so it leaves the address bar, the history and any screenshot.
        if "k" in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query or ""):
            remaining = max(60, int(_seconds_until(session.expires_at)))
            self._respond(
                HTTPStatus.SEE_OTHER,
                b"",
                content_type="text/plain; charset=utf-8",
                route=route,
                headers=(
                    ("Location", "/review"),
                    (
                        "Set-Cookie",
                        f"{COOKIE_NAME}={token}; Path=/; Max-Age={remaining}; Secure; HttpOnly; "
                        "SameSite=Strict",
                    ),
                ),
            )
            return
        self._render_page(session, token, route)

    def _render_page(self, session: Session, token: str, route: str) -> None:
        model = self.service.page_for(session)
        # The token is put on the page model here and nowhere else: the service never holds
        # it, so nothing that logs or summarises a page model can carry a capability.
        model = _with_token(model, token)
        nonce = secrets.token_urlsafe(16)
        self._respond(HTTPStatus.OK, page_mod.render(model, nonce=nonce).encode("utf-8"),
                      nonce=nonce, route=route)

    # -- POST --------------------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        route = _ROUTES.get(path.rstrip("/") or "/", "other")
        if route not in ("answer", "undo"):
            self._notice(HTTPStatus.NOT_FOUND, "Nothing here", "There is no page at this address.")
            return
        throttle = getattr(self.server, "throttle", None)
        if throttle is not None and throttle.blocked(self._client()):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"ok": False, "state": "throttled",
                       "message": "Too many tries. Wait a few minutes."}, route=route)
            return
        form = self._read_form()
        session = self._session(form)
        if session is None:
            self._no_session(route)
            return
        if not hmac.compare_digest(str(form.get("csrf", "")), session.csrf):
            log.warning("review-csrf-refused", "an answer arrived without a matching form token")
            self._finish_post(session, form, route, Outcome(
                False, "stale",
                "This page has been open a while. Pull down to reload it and answer again.",
            ))
            return
        hold_id = str(form.get("hold", ""))
        if route == "answer":
            outcome = self.service.answer(session, hold_id, str(form.get("answer", "")),
                                          note=str(form.get("note", ""))[:500])
        else:
            outcome = self.service.undo(session, hold_id)
        self._finish_post(session, form, route, outcome)

    def _finish_post(self, session: Session, form: Mapping[str, str], route: str, outcome: Outcome) -> None:
        """A JSON reply to the page's own script, a redirect to anything else.

        The redirect target is a fixed path with no query on it. An answer is idempotent by
        hold id, so a browser that re-sends the POST on a refresh changes nothing.
        """
        if self._wants_json():
            status = HTTPStatus.OK if outcome.ok else HTTPStatus.CONFLICT
            self._json(status, outcome.as_json(), route=route)
            return
        if not outcome.ok:
            self.service.add_flash(session, outcome.message, "warn")
        self._respond(
            HTTPStatus.SEE_OTHER,
            b"",
            content_type="text/plain; charset=utf-8",
            route=route,
            headers=(("Location", "/review" + (f"#h-{outcome.ref}" if outcome.ref else "")),),
        )


def _with_token(model: Page, token: str) -> Page:
    return replace(model, token=token)


def _seconds_until(stamp: str) -> float:
    from datetime import datetime, timezone as _tz

    text = (stamp or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return 3600.0
    if not when.tzinfo:
        when = when.replace(tzinfo=_tz.utc)
    return max(0.0, when.timestamp() - time.time())


# --------------------------------------------------------------------------- the server


class _ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: ReviewService, *, https: bool,
                 trust_forwarded: bool) -> None:
        self.service = service
        self.https = https
        self.trust_forwarded = trust_forwarded
        self.throttle = _Throttle()
        super().__init__(address, ReviewHandler)


def build_server(
    service: ReviewService,
    *,
    host: str = "127.0.0.1",
    port: int = 8443,
    certfile: str = "",
    keyfile: str = "",
    allow_plaintext: bool = False,
    trust_forwarded: bool = False,
) -> _ReviewHTTPServer:
    """The server, refusing the one configuration that would put held text on the wire.

    TLS is either terminated here (a certificate) or in front of us (bind to loopback and
    let a proxy do it). Binding a plaintext socket to anything else would serve the most
    sensitive text in the service over the open network, so it is refused by name rather
    than warned about.
    """
    # "" is every interface, which is the opposite of loopback. Naming it here rather
    # than letting it fall through is the difference between a proxy in front and the
    # held text of eight people on the open internet.
    loopback = host in ("127.0.0.1", "::1", "localhost")
    tls = bool(certfile)
    if not tls and not loopback and not allow_plaintext:
        raise ReviewError(
            f"refusing to serve held passages in the clear on {host}:{port}. This page shows "
            "the most sensitive text in the service, so either give it a certificate "
            "(--cert/--key), or bind it to 127.0.0.1 and let a proxy terminate HTTPS in "
            "front of it."
        )
    server = _ReviewHTTPServer((host, port), service, https=tls, trust_forwarded=trust_forwarded)
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile or certfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def serve(
    service: ReviewService,
    *,
    host: str = "127.0.0.1",
    port: int = 8443,
    certfile: str = "",
    keyfile: str = "",
    allow_plaintext: bool = False,
    trust_forwarded: bool = False,
) -> None:
    """Run until interrupted. Every answer still inside its undo window is written on the way out."""
    server = build_server(
        service, host=host, port=port, certfile=certfile, keyfile=keyfile,
        allow_plaintext=allow_plaintext, trust_forwarded=trust_forwarded,
    )
    stop = threading.Event()

    def _sweep() -> None:
        # The undo window closes on its own, even if nobody makes another request.
        while not stop.wait(0.5):
            try:
                service.commit_due()
            except Exception as exc:  # noqa: BLE001 - the loop outlives one bad write
                log.error("review-commit-failed", "an answer could not be written", error=str(exc))

    thread = threading.Thread(target=_sweep, name="review-undo", daemon=True)
    thread.start()
    log.info("review-server-started", "the review page is listening",
             port=int(port), tls=bool(certfile), mode=service.mode)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        pass
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        written = service.flush()
        log.info("review-server-stopped", "the review page stopped",
                 answers_written_on_the_way_out=written)


# --------------------------------------------------------------------------- wiring


def principal_of(config: Any) -> str:
    """Who James is, as far as this service is concerned.

    Named explicitly if the configuration says so; otherwise the first digest recipient,
    which is the address the morning email already goes to. One function, because the
    classifier, the store and this page arriving at different answers about who the
    principal is would put a staff member's words on his screen.
    """
    for attr in ("gate_principal", "principal", "review_principal"):
        value = str(getattr(config, attr, "") or "").strip()
        if value:
            return value
    recipients = getattr(config, "smtp_to", ()) or ()
    return str(recipients[0]).strip() if recipients else ""


def store_path_for(config: Any) -> str:
    """Where the held passages are, preferring what the configuration actually says.

    ``Config.held_store_path`` honours ``GATE_HELD_STORE``; ``WithheldStore.from_config``
    does not read that attribute at all. Opening the wrong file here would show an empty
    queue while passages were being held in another one, so this asks the configuration
    first and falls back to the store's own default.
    """
    configured = str(getattr(config, "held_store_path", "") or "")
    if configured:
        return configured
    return WithheldStore.path_beside(str(getattr(config, "ledger_path", ":memory:")))


def store_for(config: Any) -> WithheldStore:
    return WithheldStore(store_path_for(config), scrub=getattr(config, "scrub", None))


def service_from_config(
    config: Any,
    *,
    store: WithheldStore | None = None,
    tokens: TokenStore | None = None,
    on_decision: Callable[[HeldRecord], None] | None = None,
) -> ReviewService:
    held = store or store_for(config)
    labels = {route.name: route.label for route in getattr(config, "routes", ()) or ()}
    return ReviewService(
        held,
        tokens or TokenStore(TokenStore.path_beside(store_path_for(config))),
        principal=principal_of(config),
        mode=str(getattr(config, "gate_mode", MODE_ON) or MODE_ON),
        timezone_name=str(getattr(config, "timezone", page_mod.DEFAULT_TZ) or page_mod.DEFAULT_TZ),
        undo_seconds=_undo_seconds(),
        route_labels=labels,
        on_decision=on_decision,
    )


def _undo_seconds() -> int:
    """How long undo stays available. A bad value is said out loud, not obeyed."""
    raw = str(os.environ.get("REVIEW_UNDO_SECONDS", "") or "").strip()
    if not raw:
        return DEFAULT_UNDO_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning(
            "review-undo-seconds-ignored",
            "REVIEW_UNDO_SECONDS is not a number, so the usual undo window is used",
            value=raw, using=DEFAULT_UNDO_SECONDS,
        )
        return DEFAULT_UNDO_SECONDS


def links_for_pending(config: Any, service: ReviewService, *, hours: int = DEFAULT_TOKEN_HOURS) -> dict[str, str]:
    """One link per person who has something waiting, for the morning email to carry.

    Built from ``overview()``, which counts without reading, so building the email never
    touches a held word. The principal always gets a link, because his page is also where
    he sees how much is waiting with everybody else.
    """
    overview = service.store.overview()
    people = {who for who, count in overview.get("by_reviewer", {}).items() if who and count and who != "unassigned"}
    principal = service.principal
    if principal:
        people.add(principal)
    base = str(getattr(config, "gate_review_base_url", "") or "")
    out: dict[str, str] = {}
    for who in sorted(people):
        issued = service.tokens.issue(who, hours=hours)
        out[who] = issued.url(base)
    return out


# There is deliberately no second entry point here. `python3 -m transcriber review`
# is the only supported way to serve this page, because it is the only one that
# builds the releaser and passes `on_decision` — without which a reviewer's approval
# is recorded and the approved words are never written back into the record.


if __name__ == "__main__":  # pragma: no cover - it only says where to go
    # This module used to serve the page itself, built WITHOUT the callback that
    # writes approved words back into the record — so approvals made through it
    # were recorded and never delivered. Rather than exit silently for anyone
    # with the old command in their notes, say what replaced it.
    import sys as _sys

    print(
        "This is not the way to serve the review page. Use:\n"
        "    python3 -m transcriber review\n"
        "which is the only entry point that also writes approved passages back "
        "into the record.",
        file=_sys.stderr,
    )
    raise SystemExit(2)
