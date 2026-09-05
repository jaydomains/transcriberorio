"""The durable state: one SQLite row per recording, and nothing is ever deleted.

The load-bearing property of this file, and the reason the service exists:

    **A route's delta cursor moves only in the same transaction as that route's rows.**

If the process dies between recording a page and saving the cursor, both are lost together
and the next poll re-reads the same page. If it dies after, both survived together. There is
no ordering in which the cursor is ahead of the ledger, which is precisely how a recording
goes missing forever in the incumbent.

That is not left to a caller remembering to do two calls in the right order:
:meth:`Ledger.record_page` is the only way in, and :meth:`Ledger.cursor_set` refuses any
name that looks like a delta link. Moving a cursor *backwards* is allowed
(:meth:`Ledger.rewind_cursor`) because re-discovering a recording is harmless; only
advancing past unrecorded rows is dangerous.

Every route has its own pair of cursors — ``delta:<route>`` for the live poll and
``sweep:<route>`` for the nightly zero-cursor re-enumeration — and every row records the
route it arrived on. Generalising the invariant did not weaken it: :meth:`Ledger.record_page`
still writes rows and cursor in one transaction, and it now writes *that route's* rows and
*that route's* cursor, so a page lost by one route cannot move another route's mark. Build
the cursor names with :func:`delta_cursor_name` / :func:`sweep_cursor_name` rather than by
hand: a caller that formats the string itself is a caller that can format it differently.

Claiming is a single conditional UPDATE with a lease, so two workers cannot hold the same
recording and a worker that dies does not strand one: the lease expires and the file is
claimable again.

Every state change also appends to ``events``. A ledger that only holds the current state
can tell you a file is quarantined but not that it had been done twice before, and the
questions asked of this service after an incident are all questions about history.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .logging_setup import get_logger
from .models import (
    DEFAULT_ROUTE,
    DriveItem,
    Row,
    State,
    day_of,
    is_route_name,
    strip_dictated_emails,
    strip_emails,
    strip_owner_paths,
    utc_now_iso,
)

log = get_logger(__name__)

__all__ = [
    "Ledger",
    "LedgerError",
    "LedgerInvariantError",
    "LedgerStateError",
    "DEFAULT_ROUTE",
    "DELTA_CURSOR",
    "SWEEP_CURSOR",
    "delta_cursor_name",
    "sweep_cursor_name",
    "route_cursor_names",
    "SCHEMA_VERSION",
]


def delta_cursor_name(route: str = DEFAULT_ROUTE) -> str:
    """The live poll's cursor for one route: ``delta:calls``.

    A function rather than an f-string at each call site because five modules need this
    name and they must all produce the *same* one — a worker polling ``delta:calls`` while
    the sweep rewinds ``delta:Calls`` is a route that re-reads its whole folder nightly and
    a bug nothing would report.
    """
    return f"delta:{_cursor_route(route)}"


def sweep_cursor_name(route: str = DEFAULT_ROUTE) -> str:
    """The nightly zero-cursor re-enumeration's own mark for one route: ``sweep:calls``."""
    return f"sweep:{_cursor_route(route)}"


def route_cursor_names(route: str = DEFAULT_ROUTE) -> tuple[str, str]:
    """Both of a route's delta cursors, live first — for ``status`` and for rewinding a route."""
    return delta_cursor_name(route), sweep_cursor_name(route)


def _cursor_route(route: str) -> str:
    name = (route or "").strip()
    if not is_route_name(name):
        raise LedgerError(
            f"{route!r} is not a usable route name, and a cursor key cannot be built from "
            "it — route names are lowercase letters, digits and hyphens"
        )
    return name


DELTA_CURSOR = "delta:default"   # the live poll's cursor for the one route a legacy .env has
SWEEP_CURSOR = "sweep:default"   # and that route's nightly re-enumeration mark

#: A cursor holding a delta link may only be written by record_page. Two shapes qualify:
#: anything under ``delta`` (which is where they have always lived, including the backfill's
#: own ``delta:backfill:<route>``) and a route's ``sweep:<route>``. The sweep pass keeps
#: other marks under the same ``sweep:`` prefix — ``sweep:last_attempt_at``, ``sweep:last_report`` — and
#: those are ordinary bookkeeping; a route name cannot contain an underscore, which is what
#: keeps the two apart.
_GUARDED_PREFIX = "delta"
_ROUTE_DELTA_CURSOR_RE = re.compile(r"^(?:delta|sweep):[a-z0-9][a-z0-9-]*$")


def is_delta_cursor(name: str) -> bool:
    """True when this cursor holds a delta link and may only move alongside its rows."""
    return str(name).startswith(_GUARDED_PREFIX) or bool(_ROUTE_DELTA_CURSOR_RE.match(str(name)))


class LedgerError(RuntimeError):
    """Anything the ledger refuses to do. Never raised quietly, never swallowed."""


class LedgerInvariantError(LedgerError):
    """An attempt to move a delta cursor other than alongside its rows."""


class LedgerStateError(LedgerError):
    """An attempt to move a row somewhere the state machine does not allow."""


SCHEMA_VERSION = 4

# Migrations run in order, each in its own transaction, each recorded. To add one: append
# (2, "note", (sql, sql, ...)) below and raise SCHEMA_VERSION. Never edit an entry that has
# shipped — a database in the field has already applied it.
_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (
        1,
        "initial schema",
        (
            """
            CREATE TABLE IF NOT EXISTS items (
                item_id           TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                state             TEXT NOT NULL,
                size              INTEGER NOT NULL DEFAULT 0,
                etag              TEXT,
                parent_id         TEXT,
                web_url           TEXT,
                created_at        TEXT,
                modified_at       TEXT,
                discovered_at     TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                claimed_by        TEXT,
                lease_until       REAL,
                attempts          INTEGER NOT NULL DEFAULT 0,
                seen_count        INTEGER NOT NULL DEFAULT 1,
                last_error        TEXT,
                content_hash      TEXT,
                graph_hash        TEXT,
                duration_s        REAL,
                container         TEXT,
                truncated         INTEGER NOT NULL DEFAULT 0,
                engine            TEXT,
                language          TEXT,
                word_count        INTEGER,
                transcript_name   TEXT,
                summary_name      TEXT,
                actions_name      TEXT,
                output_item_ids   TEXT NOT NULL DEFAULT '{}',
                quarantine_reason TEXT,
                quarantined_at    TEXT,
                skipped_reason    TEXT,
                done_at           TEXT,
                archived_at       TEXT,
                source_deleted_at TEXT,
                meta              TEXT NOT NULL DEFAULT '{}'
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_items_state ON items(state)",
            "CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at)",
            "CREATE INDEX IF NOT EXISTS idx_items_lease ON items(lease_until)",
            "CREATE INDEX IF NOT EXISTS idx_items_done ON items(done_at)",
            """
            CREATE TABLE IF NOT EXISTS cursors (
                name       TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    TEXT,
                at         TEXT NOT NULL,
                kind       TEXT NOT NULL,
                from_state TEXT,
                to_state   TEXT,
                detail     TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_events_item ON events(item_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_at ON events(at)",
        ),
    ),
    (
        2,
        "routes: one column on every row, one pair of cursors per route",
        (
            # Defaulting to 'default' rather than to NULL is the whole point of this step: a
            # database written before routes existed holds the rows of the one route a
            # single-folder .env describes, and after the upgrade they have to read back as
            # exactly that route — not as a route nobody can name.
            "ALTER TABLE items ADD COLUMN route TEXT NOT NULL DEFAULT 'default'",
            "CREATE INDEX IF NOT EXISTS idx_items_route ON items(route)",
            # The two cursors move to their per-route names carrying their values with them.
            # Dropping them instead would be safe — a rewound cursor re-reads pages we
            # already hold — but it would make every upgraded installation re-enumerate its
            # whole source folder on the first poll, which looks exactly like a fault.
            # OR REPLACE covers the case where the new name somehow already exists; the
            # value being moved is the older one either way, and re-reading loses nothing.
            "UPDATE OR REPLACE cursors SET name='delta:default' WHERE name='delta:source'",
            "UPDATE OR REPLACE cursors SET name='sweep:default' WHERE name='delta:sweep'",
        ),
    ),
    (
        3,
        "the backfill lane gets its own namespace, one cursor per route",
        (
            # ``delta:backfill`` was the backfill's cursor for the one route a pre-routes
            # .env has, and it is *also* the live delta cursor of a route somebody calls
            # ``backfill`` — a name nothing refused. That route would then read the backfill
            # lane's token, walk a different folder's change feed, and advance its cursor as
            # though its own new recordings had been seen. Moving the backfill lane under
            # ``delta:backfill:<route>`` for every route, ``default`` included, closes the
            # namespace instead of fencing off a word. The value moves with the name so an
            # installation half way through backfilling its history does not start again.
            "UPDATE OR REPLACE cursors SET name='delta:backfill:default' WHERE name='delta:backfill'",
        ),
    ),
    (
        4,
        "somebody may ask to be forgotten",
        (
            # Three columns rather than one JSON blob in meta, because these are the fields
            # that must survive when meta itself is emptied — erasing a recording clears
            # meta, and a tombstone that erased its own explanation would be worse than no
            # tombstone at all.
            "ALTER TABLE items ADD COLUMN erased_at TEXT",
            "ALTER TABLE items ADD COLUMN erased_by TEXT",
            "ALTER TABLE items ADD COLUMN erased_because TEXT",
            "CREATE INDEX IF NOT EXISTS idx_items_erased ON items(erased_at)",
        ),
    ),
)

# Columns advance()/set_fields() may never write directly: identity, the state machine's own
# column, and the discovery stamp, which is history.
#: ``route`` is protected for the same reason ``item_id`` is: it decides which folder this
#: recording's transcript is written to and which archive it ages into, so a stray
#: ``set_fields(route=...)`` would silently redirect a finished recording's outputs.
#: :meth:`Ledger.reassign_route` is the deliberate way, and it records why.
_PROTECTED_COLUMNS = frozenset({"item_id", "state", "discovered_at", "route"})
_JSON_COLUMNS = frozenset({"output_item_ids", "meta"})


def default_owner() -> str:
    """Who holds a claim: host, process, thread. Enough to find it when one goes stale."""
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


class Ledger:
    """One row per Graph ``item_id``, stable across a move within the drive."""

    def __init__(
        self,
        path: str,
        *,
        busy_timeout_ms: int = 30_000,
        scrub: Any = None,
    ) -> None:
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms
        # Every other sink in the service filters mechanically — the log formatter, the
        # digest, the heartbeat body, the rendered file. The ledger did not, and it is the
        # one sink that is never pruned and is printed verbatim by ``transcriber status``.
        # Pass ``config.scrub`` and a secret in an unanticipated exception message stops
        # being permanent.
        self._scrub = scrub if callable(scrub) else None
        self._memory = path == ":memory:" or path.startswith("file::memory:")
        #: Said once per ledger, not per connection — see _restrict_permissions.
        self._permission_warning_given = False
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if not self._memory:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        self.migrate()
        self._columns = self._item_columns()

    # -- connections ---------------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,      # transactions are explicit; see _tx()
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        if not self._memory:
            conn.execute("PRAGMA journal_mode=WAL")
        # A recording that reached DONE and then vanished in a power cut is exactly the
        # failure this service removes, so durability wins over write speed at this volume.
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._restrict_permissions()
        return conn

    def _restrict_permissions(self) -> None:
        """Make the ledger readable only by the account that runs the service.

        It is not just state. Quarantine reasons, last errors and the disagreement log all
        carry fragments of what was said, and the sensitivity gate will hold whole withheld
        passages here — so on a shared host this file is one of the most revealing things on
        the machine. The work directory was already locked down and this was not, which is
        the kind of gap that survives precisely because the file looks like bookkeeping.

        SQLite writes ``-wal`` and ``-shm`` beside it, and they carry the same content, so
        all three are set. Best effort by design: a volume that will not take a chmod (a
        Windows bind mount, some container filesystems) must not stop the service starting —
        losing recordings is the worse failure. It is done on every connection because WAL
        files come and go.
        """
        if self._memory:
            return
        for suffix in ("", "-wal", "-shm"):
            target = self.path + suffix
            try:
                if os.path.exists(target):
                    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            except OSError as exc:
                # Said once, not per connection: a warning on every database open is noise
                # nobody reads, and noise nobody reads is how a real warning gets missed.
                # But it MUST be said — an earlier version claimed config reported this,
                # and config only warns about the work directory, so the one gap this
                # method exists to close was itself closing silently.
                if not self._permission_warning_given:
                    self._permission_warning_given = True
                    log.warning(
                        "ledger-permissions",
                        "could not restrict the ledger to this account only; it holds "
                        "fragments of what was said and may be readable by other users "
                        "on this machine",
                        file=os.path.basename(target),
                        error=str(exc),
                    )

    def _conn(self) -> sqlite3.Connection:
        # An in-memory database is per-connection, so threads must share one; a file
        # database gets a connection per thread, which is what WAL is for.
        if self._memory:
            with self._lock:
                if self._shared is None:
                    self._shared = self._new_connection()
                return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One all-or-nothing write. BEGIN IMMEDIATE takes the writer lock up front."""
        with self._lock:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                # The COMMIT is inside the guard on purpose. SQLite does not roll back a
                # failed COMMIT — SQLITE_FULL on a full disk, SQLITE_BUSY past the busy
                # timeout — so the transaction would stay open on this thread's cached
                # connection and every later write from it would fail with "cannot start a
                # transaction within a transaction". A transient full disk would become a
                # dead service needing a restart.
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    # Rolling back failed too, so this connection cannot be trusted. Drop it
                    # rather than hand it to the next caller; a new one is made on demand.
                    self._discard(conn)
                raise

    def _discard(self, conn: sqlite3.Connection) -> None:
        """Throw away a connection whose transaction state is no longer knowable."""
        try:
            conn.close()
        except sqlite3.Error:
            pass
        if self._shared is conn:
            self._shared = None
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None

    def close(self) -> None:
        with self._lock:
            if self._shared is not None:
                self._shared.close()
                self._shared = None
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _clean(self, text: str | None) -> str:
        """Anything a caller wants stored as free text, through the same filter as a log line."""
        value = "" if text is None else str(text)
        if self._scrub is not None:
            try:
                value = str(self._scrub(value))
            except Exception:  # noqa: BLE001 - a broken scrubber must not lose the reason
                pass
        return strip_owner_paths(strip_dictated_emails(strip_emails(value)))

    # -- schema --------------------------------------------------------------------

    def migrate(self) -> int:
        """Bring the database up to :data:`SCHEMA_VERSION`, one recorded step at a time."""
        conn = self._conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT)"
        )
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise LedgerError(
                f"ledger at {self.path} is schema v{current}, but this build only knows "
                f"v{SCHEMA_VERSION}. A newer version of the service has written it; run that "
                "one rather than downgrading the database."
            )
        for version, note, statements in _MIGRATIONS:
            if version <= current:
                continue
            with self._tx() as tx:
                for sql in statements:
                    tx.execute(sql)
                tx.execute(
                    "INSERT INTO schema_version (version, applied_at, note) VALUES (?,?,?)",
                    (version, utc_now_iso(), note),
                )
        return self.schema_version()

    def schema_version(self) -> int:
        row = self._conn().execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        return int(row["v"] or 0)

    def _item_columns(self) -> frozenset[str]:
        rows = self._conn().execute("PRAGMA table_info(items)").fetchall()
        return frozenset(r["name"] for r in rows)

    # -- the invariant -------------------------------------------------------------

    def record_page(
        self,
        rows: Sequence[DriveItem],
        new_cursor: str,
        *,
        route: str = DEFAULT_ROUTE,
        cursor_name: str | None = None,
    ) -> list[str]:
        """Commit one route's delta page — **that route's rows and that route's cursor**, or neither.

        This is the only way a delta cursor advances. Returns the ids that were new, so a
        caller can log what arrived without a second query.

        Routes did not dilute this. Each route polls its own folder with its own cursor, and
        one transaction covers exactly one route's page: a page that fails to commit takes
        down only its own route's rows and leaves only its own route's cursor where it was.
        No route's mark can move because another route wrote something, and no route's mark
        can move past a recording that was not recorded.

        ``cursor_name`` is for the one caller that keeps a delta cursor which is not a
        route's — the backfill's ``delta:backfill:<route>``. Leave it alone and the route's own live
        cursor is what moves.
        """
        if not isinstance(new_cursor, str) or not new_cursor.strip():
            raise LedgerInvariantError(
                "record_page needs the cursor Graph returned for this page; without it the "
                "next poll would re-read from the old one forever"
            )
        route_name = _cursor_route(route)
        name = cursor_name or delta_cursor_name(route_name)
        now = utc_now_iso()
        inserted: list[str] = []
        with self._tx() as conn:
            for item in rows:
                if self._upsert(conn, item, now, route_name):
                    inserted.append(item.item_id)
            self._write_cursor(conn, name, new_cursor, now)
        return inserted

    def upsert_discovered(self, item: DriveItem, route: str = DEFAULT_ROUTE) -> bool:
        """Record one item outside a delta page (backfill, a direct GET, a re-check).

        True when the row is new. Never resets an existing row's state: a file seen again is
        the same file, whatever we have since done with it.
        """
        with self._tx() as conn:
            return self._upsert(conn, item, utc_now_iso(), _cursor_route(route))

    def _upsert(
        self, conn: sqlite3.Connection, item: DriveItem, now: str, route: str = DEFAULT_ROUTE
    ) -> bool:
        if not item.item_id:
            raise LedgerError(f"a driveItem with no id cannot be recorded: {item.name!r}")
        existing = conn.execute(
            "SELECT state, name, size, etag, route FROM items WHERE item_id=?", (item.item_id,)
        ).fetchone()

        if existing is not None and existing["state"] == State.ERASED:
            # THE AUTOMATIC PATH, and the one that made this guard necessary. Deleting a
            # file in OneDrive moves it to the recycle bin; an administrator restores it
            # three weeks later; the next poll finds it and — without this — writes its name
            # straight back onto the tombstone of a recording somebody asked to be rid of.
            # Nobody types that. The person who asked would never know it had happened.
            #
            # So a file that comes back is a file to delete THERE, not a row to fill in
            # here. It is logged every time it is seen, because a recording that keeps
            # reappearing is something a person has to go and deal with.
            log.warning(
                f"{item.item_id} was erased at somebody's request and has turned up in the "
                "source folder again — probably restored from the recycle bin. It has NOT "
                "been re-ingested. Delete it where it is."
            )
            return False

        if item.deleted:
            # Deletion at the source is recorded, never mirrored. Our row is the only proof
            # the recording ever existed.
            if existing is not None:
                conn.execute(
                    "UPDATE items SET source_deleted_at=COALESCE(source_deleted_at,?), updated_at=? "
                    "WHERE item_id=?",
                    (now, now, item.item_id),
                )
                self._event(conn, item.item_id, "source-deleted", now, existing["state"], existing["state"],
                            "the item was deleted or moved out of the source folder")
            return False

        if existing is None:
            conn.execute(
                "INSERT INTO items ("
                " item_id, name, state, route, size, etag, parent_id, web_url, created_at,"
                " modified_at, discovered_at, updated_at, graph_hash"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.item_id, item.name, State.DISCOVERED, route, int(item.size or 0), item.etag,
                    item.parent_id, item.web_url, item.created_at, item.modified_at,
                    now, now, item.best_hash,
                ),
            )
            self._event(conn, item.item_id, "discovered", now, None, State.DISCOVERED,
                        f"{item.name} (route {route})")
            return True

        conn.execute(
            "UPDATE items SET name=?, size=?, etag=?, parent_id=COALESCE(?,parent_id),"
            " web_url=COALESCE(?,web_url), created_at=COALESCE(?,created_at),"
            " modified_at=COALESCE(?,modified_at), graph_hash=COALESCE(?,graph_hash),"
            " seen_count=seen_count+1, updated_at=? WHERE item_id=?",
            (
                item.name, int(item.size or 0), item.etag, item.parent_id, item.web_url,
                item.created_at, item.modified_at, item.best_hash, now, item.item_id,
            ),
        )
        if existing["route"] != route:
            # The same recording turning up on a second route is one of two things, and both
            # need a person: he moved it between watched folders, or one route's source
            # folder sits inside another's and Graph's subtree delta feed is handing the same
            # recording to both. The second is the dangerous one — the transcript goes to the
            # wrong folder and, sixty days later, the only copy of the audio is moved into
            # the wrong archive — so this is said out loud at error level, on the spot, as
            # well as written into the history. The row keeps the route it was discovered on,
            # because that is where its outputs went; nothing is decided here.
            self._event(
                conn, item.item_id, "route-disagreement", now, existing["state"], existing["state"],
                f"discovered on route {existing['route']}, seen again on route {route}; "
                f"it stays on {existing['route']}",
            )
            log.error(
                "route-disagreement",
                f"{item.name!r} was discovered on the route {existing['route']!r} and has now "
                f"been seen again on the route {route!r}. It stays on {existing['route']!r}, so "
                f"its transcript goes to that route's output folder and it would age into that "
                f"route's archive. Either it was moved between watched folders, or those two "
                f"routes are watching folders one of which is inside the other — check that "
                f"before the archive pass moves it",
                item=item.item_id,
                route=route,
            )

        changed = int(existing["size"] or 0) != int(item.size or 0) or existing["etag"] != item.etag
        if changed:
            # Somebody replaced the recording. Nobody is going to notice that from the state
            # column — the UPDATE above has just quietly taken the new size and etag — so it
            # goes in the history where a person can find it. Two names, because they are
            # two different situations and a person reading the history wants to tell them
            # apart. Replaced after we finished: the transcript and summary already written
            # describe bytes that are no longer in the drive, and nothing in the pipeline
            # will revisit them. Replaced while it is still being worked on: this is the one
            # somebody can still act on, and the row now carries the new recording's size
            # and hash, so the download step will see that the file in the work directory is
            # the old recording and fetch the new one before anything is published.
            finished = existing["state"] in (State.DONE, State.SKIPPED_EMPTY)
            self._event(
                conn, item.item_id,
                "changed-after-finish" if finished else "changed-while-working",
                now, existing["state"], existing["state"],
                f"size {existing['size']}->{item.size}, etag {existing['etag']}->{item.etag}",
            )
        return False

    # -- cursors -------------------------------------------------------------------

    def cursor_get(self, name: str) -> str | None:
        row = self._conn().execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        return None if row is None else row["value"]

    def cursor_set(self, name: str, value: str) -> None:
        """Set a bookkeeping mark (last digest, last sweep, last archive).

        A delta cursor is refused here on purpose. Advancing one without its rows in the
        same transaction is the exact way a recording is lost, so there is no polite path
        to it: use :meth:`record_page`.
        """
        if is_delta_cursor(name):
            raise LedgerInvariantError(
                f"{name!r} is a delta cursor and cannot be set on its own — it advances only "
                "in the same transaction as the rows from its page, via record_page()"
            )
        now = utc_now_iso()
        with self._tx() as conn:
            self._write_cursor(conn, name, self._clean(value), now)

    def rewind_cursor(self, name: str, reason: str) -> None:
        """Clear a delta cursor so the next poll re-enumerates from zero.

        Allowed where advancing is not: re-reading pages we have already recorded costs a
        little work and loses nothing, while skipping one loses a recording.
        """
        if not reason.strip():
            raise LedgerError("rewinding a cursor is a deliberate act and needs a stated reason")
        now = utc_now_iso()
        with self._tx() as conn:
            self._write_cursor(conn, name, None, now)
            self._event(conn, None, "cursor-rewound", now, None, None, f"{name}: {reason}")

    def _write_cursor(self, conn: sqlite3.Connection, name: str, value: str | None, now: str) -> None:
        conn.execute(
            "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, value, now),
        )

    # -- claiming ------------------------------------------------------------------

    def claim(
        self,
        item_id: str,
        lease_seconds: int,
        *,
        owner: str | None = None,
        states: Sequence[str] | None = None,
        now: float | None = None,
    ) -> bool:
        """Take an expiring claim on one recording. False if somebody else holds it.

        One conditional UPDATE: the row moves only if it is in a claimable state *and* no
        live lease is on it, so two workers racing cannot both win. The lease is why a
        worker that dies mid-job releases the file instead of stranding it — nobody has to
        notice the death for the recording to be picked up again.

        A DISCOVERED row becomes CLAIMED; a row further along keeps the progress it has, so
        resuming a half-finished recording does not throw away its download.
        """
        if lease_seconds <= 0:
            raise LedgerError("a claim with no lease never expires, which is the bug the lease exists to prevent")
        clock = time.time() if now is None else now
        claimable = tuple(states) if states else tuple(sorted(State.ACTIVE))
        holder = owner or default_owner()
        placeholders = ",".join("?" for _ in claimable)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE items SET claimed_by=?, lease_until=?, updated_at=?,"
                f" state=CASE WHEN state=? THEN ? ELSE state END"
                f" WHERE item_id=? AND state IN ({placeholders})"
                f" AND (lease_until IS NULL OR lease_until < ?)",
                (
                    holder, clock + lease_seconds, utc_now_iso(clock),
                    State.DISCOVERED, State.CLAIMED,
                    item_id, *claimable, clock,
                ),
            )
            if cur.rowcount != 1:
                return False
            self._event(conn, item_id, "claimed", utc_now_iso(clock), None, None,
                        f"{holder} for {lease_seconds}s")
            return True

    def renew(self, item_id: str, lease_seconds: int, owner: str, now: float | None = None) -> bool:
        """Extend a claim we still hold. False if it expired and somebody else took it."""
        clock = time.time() if now is None else now
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE items SET lease_until=?, updated_at=? WHERE item_id=? AND claimed_by=?"
                " AND lease_until IS NOT NULL AND lease_until >= ?",
                (clock + lease_seconds, utc_now_iso(clock), item_id, owner, clock),
            )
            return cur.rowcount == 1

    #: Clear the claim only when it is ours to clear. ``owner`` is optional so an operator
    #: acting by hand can still let go of anything; when it is given, a worker can never
    #: strip a claim another worker is actively holding. The row is still updated either way,
    #: so the caller's rowcount check — and therefore its error path — is unchanged.
    _CLAIM_RELEASE = (
        " claimed_by=CASE WHEN ? IS NULL OR claimed_by IS NULL OR claimed_by=? THEN NULL ELSE claimed_by END,"
        " lease_until=CASE WHEN ? IS NULL OR claimed_by IS NULL OR claimed_by=? THEN NULL ELSE lease_until END"
    )

    @staticmethod
    def _claim_params(owner: str | None) -> tuple[Any, Any, Any, Any]:
        return (owner, owner, owner, owner)

    def release(self, item_id: str, reason: str = "", *, owner: str | None = None) -> None:
        """Give up a claim without failing the item, so it is claimable immediately.

        With ``owner`` given this only lets go of *our* claim. Two workers on one ledger is
        the ordinary shape of a redeploy on a shared volume, and an unconditional release
        let worker A strip worker B's live lease on a recording B was still transcribing.
        """
        now = utc_now_iso()
        with self._tx() as conn:
            conn.execute(
                "UPDATE items SET updated_at=?," + self._CLAIM_RELEASE + " WHERE item_id=?",
                (now, *self._claim_params(owner), item_id),
            )
            self._event(conn, item_id, "released", now, None, None, self._clean(reason) or None)

    # -- state ---------------------------------------------------------------------

    def _refuse_if_erased(self, conn: sqlite3.Connection, item_id: str, what: str) -> bool:
        """True when this row has been erased, and therefore may not be written to.

        ERASED IS FINAL, and this is the guard that makes that true rather than aspirational.
        Emptying the row is not enough on its own, because half a dozen paths write to a row
        and every one of them would happily fill it back in:

          * ``advance`` moved it back into the pipeline;
          * ``set_fields`` and ``upsert_discovered`` wrote the NAME straight back;
          * ``record_attempt`` wrote an error message that quoted the recording;
          * ``requeue`` and ``quarantine`` gave it a live state again.

        The worst is ``upsert_discovered``, because nobody types it. A file deleted from
        OneDrive goes to the recycle bin, an administrator restores it three weeks later,
        the next poll finds it, and the tombstone of a recording somebody asked to be rid of
        quietly becomes a recording again with its name on it. The person who asked would
        never know, and neither would anybody here.

        So the answer is no, at the boundary, for everything except :meth:`erase` itself —
        which is idempotent and whose whole job this is.
        """
        row = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row is None or row["state"] != State.ERASED:
            return False
        # Pre-formatted: this module's ``log`` is an EventLogger, not the stdlib logger, and
        # does not take %-style arguments.
        log.warning(
            f"refusing to {what} {item_id}: it was erased at somebody's request, and ERASED "
            "is final. If this is a file that came back from the recycle bin, it needs "
            "deleting there rather than re-ingesting here."
        )
        return True

    def advance(self, item_id: str, state: str, **fields: Any) -> None:
        """Move a row to ``state``, writing any accompanying fields in the same statement.

        Refuses an unknown state and an unknown field name: a typo that silently dropped a
        transcript's word count, or wrote a state nothing else recognises, is a failure with
        no symptom until somebody goes looking months later.
        """
        if not State.is_known(state):
            raise LedgerStateError(f"{state!r} is not a state — one of: " + ", ".join(sorted(State.ALL)))
        now = utc_now_iso()
        assignments, values = self._assignments(fields)
        with self._tx() as conn:
            current = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
            if current is None:
                raise LedgerError(f"no ledger row for {item_id!r}: it must be discovered before it can advance")
            was = current["state"]
            if was == State.ERASED:
                raise LedgerStateError(
                    f"{item_id} was erased at somebody's request; ERASED is final. Nothing "
                    "moves it back into the pipeline, because a recording that came back is "
                    "a file to delete rather than a row to fill in."
                )
            if was == State.DONE and state != State.DONE:
                raise LedgerStateError(
                    f"{item_id} is DONE; moving it back to {state} would erase a finished "
                    "recording's outcome. Use requeue() if that is really what is meant."
                )
            if state == State.DONE and "done_at" not in fields:
                assignments.append("done_at=?")
                values.append(now)
            if state == State.QUARANTINED and not fields.get("quarantine_reason"):
                raise LedgerStateError("quarantine() carries the reason; advance() to QUARANTINED without one does not")
            if State.is_terminal(state):
                assignments.extend(["claimed_by=NULL", "lease_until=NULL"])
            clause = "".join(", " + a for a in assignments)
            conn.execute(
                f"UPDATE items SET state=?, updated_at=?{clause} WHERE item_id=?",
                (state, now, *values, item_id),
            )
            self._event(conn, item_id, "advanced", now, was, state, _summarise(fields))

    #: Everything on a row that describes the recording rather than the fact of it. Emptied
    #: by :meth:`erase`. Listed here rather than inline so that a column added later is
    #: noticed: :func:`transcriber.erase.columns_not_covered` walks the table against this
    #: list and fails a test if something new holds content nobody thought to clear.
    CONTENT_COLUMNS = (
        "name", "etag", "web_url", "content_hash", "graph_hash", "last_error",
        "quarantine_reason", "skipped_reason", "transcript_name", "summary_name",
        "actions_name", "output_item_ids", "container", "language", "meta",
    )

    def erase(self, item_id: str, *, by: str, because: str) -> None:
        """Empty a row of everything that described the recording, and say who asked.

        The one write in this class that deliberately goes backwards. :meth:`advance`
        refuses to move a DONE row anywhere else, for good reasons that do not apply here:
        somebody has asked to be forgotten, and a finished recording is exactly the kind
        that needs forgetting.

        What is kept: the item id, the route, the dates it arrived and finished, its size
        and duration, and the three erasure fields. What is gone: its name, the names of
        its three output files, its hashes, its error text and its metadata. A person
        reading the ledger afterwards can see that a recording existed and was removed on a
        stated date at a stated person's request; they cannot see what it was.

        ``by`` is a PERSON. Not a hostname, not "the service", not the empty string — the
        same rule the sensitivity gate applies to releasing a held passage, and for the same
        reason: an erasure nobody's name is on is an erasure nobody decided.
        """
        who = (by or "").strip()
        why = (because or "").strip()
        if not who:
            raise LedgerError(
                "erase() needs the name of the person who decided it. A recording removed "
                "at nobody's request is indistinguishable from one lost to a bug."
            )
        if not why:
            raise LedgerError("erase() needs the reason it was asked for")
        now = utc_now_iso()
        with self._tx() as conn:
            current = conn.execute(
                "SELECT state, name FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if current is None:
                raise LedgerError(f"no ledger row for {item_id!r}")
            was = current["state"]
            if was == State.ERASED:
                return          # idempotent: erasing twice is not an error, it is a re-run
            blanks = ", ".join(f"{c}=?" for c in self.CONTENT_COLUMNS)
            # NULL where the column allows it, and the column's own empty value where it
            # does not. Read off the table rather than listed here, so a NOT NULL added
            # later cannot make an erasure fail at the moment somebody needs it to work.
            notnull = {
                r["name"]: (r["notnull"], r["dflt_value"])
                for r in conn.execute("PRAGMA table_info(items)").fetchall()
            }
            empties: list[Any] = []
            for column in self.CONTENT_COLUMNS:
                required, default = notnull.get(column, (0, None))
                if not required:
                    empties.append(None)
                elif default is not None:
                    empties.append(str(default).strip("'"))
                else:
                    empties.append("")
            conn.execute(
                f"UPDATE items SET state=?, updated_at=?, {blanks},"
                " erased_at=?, erased_by=?, erased_because=?,"
                " claimed_by=NULL, lease_until=NULL WHERE item_id=?",
                (State.ERASED, now, *empties, now, who, why, item_id),
            )
            # EVERY EARLIER EVENT'S DETAIL GOES TOO, and this is the half that is easy to
            # miss. The row is not the only place the recording's name was written: the
            # events table carries "discovered: Carel dismissal call.m4a" and, on the DONE
            # event, all three output filenames. Emptying the row and leaving those would
            # be an erasure that removed the record and kept the description of it — and
            # the names here are often the most telling part, because a person names a
            # recording after what it is about.
            #
            # The events themselves stay. What happened and when is the audit trail this
            # tombstone exists to preserve; the detail is what described the recording.
            conn.execute(
                "UPDATE events SET detail=NULL WHERE item_id=? AND detail IS NOT NULL",
                (item_id,),
            )
            # Written after the clearing, so this one keeps its detail: it carries WHO and
            # WHY and never the name of what was removed.
            self._event(conn, item_id, "erased", now, was, State.ERASED,
                        f"at {who}'s request: {why}"[:400])

    def set_fields(self, item_id: str, **fields: Any) -> None:
        """Update fields without touching the state (archival stamps, hashes, metadata)."""
        if not fields:
            return
        now = utc_now_iso()
        assignments, values = self._assignments(fields)
        with self._tx() as conn:
            if self._refuse_if_erased(conn, item_id, "write fields to"):
                return
            cur = conn.execute(
                f"UPDATE items SET updated_at=?, {', '.join(assignments)} WHERE item_id=?",
                (now, *values, item_id),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"no ledger row for {item_id!r}")
            self._event(conn, item_id, "updated", now, None, None, _summarise(fields))

    def quarantine(self, item_id: str, reason: str, *, owner: str | None = None) -> None:
        """Stop, loudly, and leave it for a person. Never silently retried into oblivion.

        The state change is unconditional — a quarantine that could be vetoed would not be a
        quarantine — but the *claim* is only released when it is ours, so this cannot pull a
        recording out from under another worker that is still working on it.
        """
        if not (reason or "").strip():
            raise LedgerError("a quarantine with no reason is not visible, which defeats the point of it")
        clean = self._clean(reason)
        now = utc_now_iso()
        with self._tx() as conn:
            current = conn.execute("SELECT state FROM items WHERE item_id=?", (item_id,)).fetchone()
            if current is None:
                raise LedgerError(f"no ledger row for {item_id!r}")
            if self._refuse_if_erased(conn, item_id, "quarantine"):
                return
            conn.execute(
                "UPDATE items SET state=?, quarantine_reason=?, quarantined_at=?, updated_at=?,"
                + self._CLAIM_RELEASE + " WHERE item_id=?",
                (State.QUARANTINED, clean, now, now, *self._claim_params(owner), item_id),
            )
            self._event(conn, item_id, "quarantined", now, current["state"], State.QUARANTINED, clean)

    def requeue(
        self,
        item_id: str,
        reason: str,
        state: str = State.DISCOVERED,
        *,
        reset_attempts: bool = False,
    ) -> None:
        """Deliberately put a row back in the queue — after a person fixed what was wrong.

        Separate from advance() because moving work backwards should be something somebody
        chose, with a reason recorded, rather than something a caller did by accident.

        ``reset_attempts`` is for the manual path and is what makes it mean anything. A
        recording quarantined after three engine failures still carries ``attempts=3``, so
        putting it back in the queue with the count intact bought it nothing: the first
        transient failure took it straight past ``max_attempts`` again, and if the worker
        did not reach it first the nightly sweep re-quarantined it on the count alone —
        naming, as the reason, the errors from before the person fixed the cause. Either
        way the requeue was undone within hours and the recording had never actually been
        retried once. ``cmd_requeue`` passes it; the sweep's own automatic requeue does not,
        so ``max_attempts`` still bounds the machine. Nothing is destroyed either way —
        every attempt that ever happened is still in the ``events`` table.
        """
        if not State.is_known(state):
            raise LedgerStateError(f"{state!r} is not a state")
        if not (reason or "").strip():
            raise LedgerError("requeueing needs a stated reason; it is a manual act")
        clean = self._clean(reason)
        now = utc_now_iso()
        with self._tx() as conn:
            current = conn.execute(
                "SELECT state, meta FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if current is None:
                raise LedgerError(f"no ledger row for {item_id!r}")
            if self._refuse_if_erased(conn, item_id, "requeue"):
                return
            # A requeue is an explicit decision that this should be tried *now*. Leaving the
            # previous attempt's backoff in place made the sweep's "re-queued from the start"
            # a half-truth — the row sat out up to another hour — and made a person's manual
            # requeue look as though it had done nothing, which invites a second one.
            meta = _decode_meta(current["meta"])
            for key in ("retry_at", "retry_reason", "gate_first_seen"):
                meta.pop(key, None)
            if reset_attempts:
                conn.execute(
                    "UPDATE items SET state=?, claimed_by=NULL, lease_until=NULL,"
                    " updated_at=?, meta=?, attempts=0, quarantine_reason=NULL,"
                    " quarantined_at=NULL, last_error=NULL WHERE item_id=?",
                    (state, now, json.dumps(meta, sort_keys=True), item_id),
                )
            else:
                conn.execute(
                    "UPDATE items SET state=?, claimed_by=NULL, lease_until=NULL,"
                    " updated_at=?, meta=? WHERE item_id=?",
                    (state, now, json.dumps(meta, sort_keys=True), item_id),
                )
            self._event(conn, item_id, "requeued", now, current["state"], state, clean)

    def reassign_route(self, item_id: str, route: str, reason: str) -> None:
        """Move one recording to a different route, deliberately and with a reason recorded.

        The only way the ``route`` column changes after discovery. It is not an ordinary
        field write because the route decides where this recording's transcript is written
        and which archive it ages into: doing it by accident sends a finished recording's
        outputs to a folder nobody chose, and nothing would say so.
        """
        target = _cursor_route(route)
        if not (reason or "").strip():
            raise LedgerError("moving a recording to another route is a manual act and needs a stated reason")
        clean = self._clean(reason)
        now = utc_now_iso()
        with self._tx() as conn:
            current = conn.execute(
                "SELECT state, route FROM items WHERE item_id=?", (item_id,)
            ).fetchone()
            if current is None:
                raise LedgerError(f"no ledger row for {item_id!r}")
            if self._refuse_if_erased(conn, item_id, "reassign the route of"):
                return
            conn.execute(
                "UPDATE items SET route=?, updated_at=? WHERE item_id=?", (target, now, item_id)
            )
            self._event(
                conn, item_id, "route-changed", now, current["state"], current["state"],
                f"{current['route']} -> {target}: {clean}",
            )

    def record_attempt(self, item_id: str, error: str, *, owner: str | None = None) -> int:
        """Count a failure, keep its message, and let go of the claim.

        Releasing the lease matters: the attempt failed, so whatever this worker was doing
        it is not doing any more, and another worker (or the next poll) should be free to
        try. The count is what the caller compares against max_attempts before quarantining.
        """
        clean = self._clean(error)
        now = utc_now_iso()
        with self._tx() as conn:
            # The error text quotes the recording — "the engine failed on Carel's call" —
            # so this path puts a name back on a tombstone as surely as set_fields does.
            if self._refuse_if_erased(conn, item_id, "record a failed attempt against"):
                return 0
            cur = conn.execute(
                "UPDATE items SET attempts=attempts+1, last_error=?, updated_at=?,"
                + self._CLAIM_RELEASE + " WHERE item_id=?",
                (clean, now, *self._claim_params(owner), item_id),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"no ledger row for {item_id!r}")
            row = conn.execute("SELECT attempts FROM items WHERE item_id=?", (item_id,)).fetchone()
            attempts = int(row["attempts"])
            self._event(conn, item_id, "attempt-failed", now, None, None, f"attempt {attempts}: {clean}")
            return attempts

    # -- reading -------------------------------------------------------------------

    def get(self, item_id: str) -> Row | None:
        record = self._conn().execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        return None if record is None else Row.from_db(record)

    def find_by_name(self, needle: str, limit: int | None = 20) -> list[Row]:
        """Recordings whose filename contains ``needle``, newest first.

        For the person at a terminal who has a filename and not a OneDrive item id. The
        morning email prints both, a few lines apart, and nobody retypes an id by hand.

        ``limit=None`` means every match. That exists for ``transcriber forget``, where a
        cap is not a convenience but a hazard: matching two hundred recordings, removing the
        newest twenty and reporting success would tell somebody who asked to be forgotten
        that they had been.
        """
        # Every LIKE metacharacter in the needle is escaped before the wildcards that make
        # this a "contains" search are put around it — backslash first, or it would escape
        # the escapes added after it. Searching for "100%" used to match every recording,
        # because the % in the filename was read as "anything at all".
        raw = (needle or "").strip()
        escaped = raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        records = self._conn().execute(
            "SELECT * FROM items WHERE name LIKE ? ESCAPE '\\' "
            "ORDER BY COALESCE(discovered_at, '') DESC"
            + ("" if limit is None else " LIMIT ?"),
            (f"%{escaped}%",) if limit is None else (f"%{escaped}%", int(limit)),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def unfinished(self, route: str | None = None) -> list[Row]:
        """Everything not in a terminal state, oldest first — what the sweep re-queues.

        ``route`` narrows it to one route, so a sweep of one route re-queues that route's
        work and reports on that route, and a route that is failing cannot be hidden inside
        a whole-service total.
        """
        terminal = tuple(sorted(State.TERMINAL))
        placeholders = ",".join("?" for _ in terminal)
        clause, params = self._route_filter(route)
        records = self._conn().execute(
            f"SELECT * FROM items WHERE state NOT IN ({placeholders}){clause}"
            " ORDER BY discovered_at ASC",
            (*terminal, *params),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def claimable(
        self, limit: int = 100, now: float | None = None, route: str | None = None
    ) -> list[Row]:
        """Unfinished rows nobody holds a live lease on, oldest first.

        Left unfiltered on purpose by the ordinary worker loop: every route's work is one
        queue, and each row carries the route that decides where its outputs go.
        """
        clock = time.time() if now is None else now
        active = tuple(sorted(State.ACTIVE))
        placeholders = ",".join("?" for _ in active)
        clause, params = self._route_filter(route)
        records = self._conn().execute(
            f"SELECT * FROM items WHERE state IN ({placeholders})"
            f" AND (lease_until IS NULL OR lease_until < ?){clause}"
            " ORDER BY discovered_at ASC LIMIT ?",
            (*active, clock, *params, int(limit)),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def last_advanced_at(self) -> dict[str, str]:
        """When each row last made genuine forward progress, from the event log.

        Not ``updated_at``: claiming, releasing and deferring all write that column, so a row
        the worker picks up and fails on every two-minute cycle looks freshly touched forever
        and the sweep's re-queue and quarantine arms can never fire on the rows that are
        actually stuck. An ``advanced`` event is written only by :meth:`advance`, which is
        the state machine moving and nothing else.
        """
        return {
            str(r["item_id"]): str(r["at"])
            for r in self._conn().execute(
                "SELECT item_id, MAX(at) AS at FROM events"
                " WHERE kind='advanced' AND item_id IS NOT NULL GROUP BY item_id"
            ).fetchall()
        }

    def owner_of_output_name(self, name: str) -> str | None:
        """Which recording already owns this output filename, if any.

        The mechanical backstop under the naming fix: before three files are written, ask
        whether some other row already holds one of those names. Uploading replaces by name
        and returns the same driveItem id, so without this a collision destroys a transcript
        while every read-back, the sweep and the archive all pass.
        """
        if not (name or "").strip():
            return None
        record = self._conn().execute(
            "SELECT item_id FROM items WHERE transcript_name=? OR summary_name=? OR actions_name=?"
            " LIMIT 1",
            (name, name, name),
        ).fetchone()
        return None if record is None else str(record["item_id"])

    def rows_in_route(self, route: str) -> list[Row]:
        """Every row on one route, whatever state it is in, newest first.

        For ``transcriber forget``, which is asked about a person rather than about a stage:
        somebody who has asked to be forgotten does not care which of their recordings
        happened to fail, and a selection that quietly skipped the quarantined ones would
        leave behind exactly the recordings a person had already been told about.
        """
        records = self._conn().execute(
            "SELECT * FROM items WHERE route=? ORDER BY COALESCE(discovered_at, '') DESC",
            (route,),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def rows_in_state(self, state: str, route: str | None = None) -> list[Row]:
        clause, params = self._route_filter(route)
        records = self._conn().execute(
            f"SELECT * FROM items WHERE state=?{clause} ORDER BY discovered_at ASC",
            (state, *params),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def routes_seen(self) -> tuple[str, ...]:
        """Every route that has ever recorded a row, in name order.

        Including routes no longer in the configuration: taking a route out of ``ROUTES``
        stops it being watched and deletes nothing, so its history is still here to be
        reported and still has to be findable.
        """
        return tuple(
            str(r["route"])
            for r in self._conn().execute(
                "SELECT DISTINCT route FROM items ORDER BY route ASC"
            ).fetchall()
        )

    def due_for_archive(
        self, older_than_days: int, now: float | None = None, route: str | None = None
    ) -> list[Row]:
        """Done recordings past the age, whose three outputs are recorded as present.

        Never a failure, never one whose outputs we cannot name: an original is only moved
        on evidence, never on the system's own belief that it finished.

        ``route`` matters here more than anywhere else. Each route archives into its own
        folder, and a recording must never be moved into a folder belonging to a route it
        did not arrive on, so the archive pass asks one route at a time.
        """
        clock = time.time() if now is None else now
        cutoff = utc_now_iso(clock - older_than_days * 86400)
        clause, params = self._route_filter(route)
        records = self._conn().execute(
            "SELECT * FROM items WHERE state=? AND archived_at IS NULL"
            " AND transcript_name IS NOT NULL AND summary_name IS NOT NULL AND actions_name IS NOT NULL"
            f" AND COALESCE(created_at, discovered_at) < ?{clause}"
            " ORDER BY COALESCE(created_at, discovered_at) ASC",
            (State.DONE, cutoff, *params),
        ).fetchall()
        return [Row.from_db(r) for r in records]

    def naming_for_day(self, day: str, route: str | None = None) -> list[dict[str, Any]]:
        """Every naming decision on the recordings that FINISHED on ``day``.

        Keyed on ``done_at``, not ``discovered_at``, and the difference is not a detail.
        The digest is built for yesterday and every other cohort in it is selected by
        discovery — but a naming decision is written when the recording publishes, so a
        recording discovered at 21:31 and published at 09:12 the next morning would be
        selected by a digest built before its decision existed, and by no later one. Its
        name would appear in no email, ever. That is exactly the deferred, backed-off,
        burst-day population where a name is most likely to be wrong, so it is exactly the
        population that must not be invisible.

        (``done_at`` is UTC and the digest targets a local day, so a decision can be
        reported up to two hours early. Every row is still reported exactly once. Same skew
        the rest of the digest already carries; noted rather than papered over.)
        """
        conn = self._conn()
        like = f"{day}%"
        clause, params = self._route_filter(route)
        out: list[dict[str, Any]] = []
        rows = conn.execute(
            f"SELECT item_id, name, route, meta FROM items "
            f"WHERE done_at LIKE ?{clause} ORDER BY done_at ASC",
            (like, *params),
        ).fetchall()
        for row in rows:
            decision = _decode_meta(row["meta"]).get("naming")
            if not isinstance(decision, dict) or not decision.get("decided"):
                continue
            entry = dict(decision)
            entry["item_id"] = row["item_id"]
            entry["source_name"] = row["name"]
            entry["route"] = row["route"]
            out.append(entry)
        return out

    def spend_since(self, since: str, route: str | None = None) -> list[dict]:
        """Every recorded analysis spend stamped on or after ``since`` (an ISO date).

        Narrowed in SQL and decided in Python, on purpose. The date that matters is the one
        stamped inside the spend record, and reaching into JSON from SQL would tie this
        query to whether the SQLite the deployment happens to have was built with the JSON
        extension. The SQL clause is a cheap over-fetch on the columns that do exist; the
        filter that counts is the stamp.

        Rows are small and there are hundreds a month, not millions.
        """
        conn = self._conn()
        clause, params = self._route_filter(route)
        out: list[dict] = []
        for record in conn.execute(
            "SELECT meta, route FROM items"
            " WHERE COALESCE(done_at, updated_at, discovered_at) >= ?"
            f"{clause}",
            (since, *params),
        ).fetchall():
            try:
                meta = json.loads(record["meta"] or "{}")
            except ValueError:
                continue
            entry = meta.get("spend")
            if not isinstance(entry, Mapping):
                continue
            at = str(entry.get("at") or "")
            if at < since:
                continue
            calls = entry.get("calls")
            if not isinstance(calls, (list, tuple)):
                continue
            out.append({
                "at": at,
                "route": record["route"],
                "calls": [dict(c) for c in calls if isinstance(c, Mapping)],
            })
        return out

    def counts_for_day(self, day: str, route: str | None = None) -> dict:
        """What the digest reports: the cohort discovered on ``day`` and how it ended up.

        ``in_flight`` is a count of recordings from that day that are still unfinished —
        on a digest for yesterday that is a stuck file, which is why it is reported next to
        the failures rather than buried in by_state.

        ``failures`` deliberately carries **every** quarantined recording, not only that
        day's: one nobody has dealt with is still a failure this morning, and a list that
        forgets it is how it stops being anybody's job.

        ``route=None`` is the whole service, which is what the subject line counts. Asked
        one route at a time it gives the digest its per-route breakdown, so "site meetings
        all fine, WhatsApp broken" is visible instead of averaged away in a total.
        """
        conn = self._conn()
        like = f"{day}%"
        clause, params = self._route_filter(route)
        by_state: dict[str, int] = {}
        for record in conn.execute(
            f"SELECT state, COUNT(*) AS n FROM items WHERE discovered_at LIKE ?{clause} GROUP BY state",
            (like, *params),
        ).fetchall():
            by_state[record["state"]] = int(record["n"])
        discovered = sum(by_state.values())
        done = by_state.get(State.DONE, 0)
        quarantined = by_state.get(State.QUARANTINED, 0)
        skipped = by_state.get(State.SKIPPED_EMPTY, 0)
        in_flight = discovered - done - quarantined - skipped

        failures = [
            {
                "item_id": r["item_id"],
                "name": r["name"],
                "state": r["state"],
                "reason": (
                    r["quarantine_reason"]
                    or r["last_error"]
                    or f"still {r['state']} — it never finished, and no error was recorded"
                ),
                "attempts": int(r["attempts"] or 0),
                "web_url": r["web_url"],
                "discovered_at": r["discovered_at"],
                "route": r["route"],
            }
            for r in conn.execute(
                "SELECT * FROM items WHERE (state=? OR (discovered_at LIKE ? AND state NOT IN (?,?,?)))"
                f"{clause} ORDER BY discovered_at ASC",
                (State.QUARANTINED, like, State.DONE, State.QUARANTINED, State.SKIPPED_EMPTY, *params),
            ).fetchall()
        ]
        done_on_day = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM items WHERE done_at LIKE ?{clause}", (like, *params)
            ).fetchone()["n"]
        )
        archived = int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM items WHERE archived_at LIKE ?{clause}", (like, *params)
            ).fetchone()["n"]
        )
        return {
            "day": day,
            "route": route,
            "discovered": discovered,
            "done": done,
            "quarantined": quarantined,
            "skipped_empty": skipped,
            "in_flight": in_flight,
            "archived": archived,
            "done_on_day": done_on_day,
            "by_state": by_state,
            "failures": failures,
        }

    def attention_for_day(self, day: str, route: str | None = None) -> dict[str, Any]:
        """What a person should look at that is not a state change, for the morning email.

        None of these move a row, so none of them appears in a count of states — and until
        they were reported here, a withheld item and a duration guard that could not run were
        recorded in the row's meta and read by nobody.
        """
        like = f"{day}%"
        review = 0
        review_rows: list[dict[str, Any]] = []
        unverified_guard = 0
        degraded = 0
        clause, params = self._route_filter(route)
        for record in self._conn().execute(
            f"SELECT item_id, name, route, meta FROM items WHERE discovered_at LIKE ?{clause}"
            " ORDER BY discovered_at ASC",
            (like, *params),
        ).fetchall():
            meta = _decode_meta(record["meta"])
            analysis = meta.get("analysis") if isinstance(meta.get("analysis"), dict) else {}
            count = int(analysis.get("review") or 0)
            if count:
                review += count
                review_rows.append({
                    "item_id": record["item_id"],
                    "name": record["name"],
                    "route": record["route"],
                    "count": count,
                    # The items themselves, so the promise the summary and actions files make
                    # — "kept against this recording" — is one somebody can actually collect on.
                    "items": list(analysis.get("review_items") or ()),
                })
            engine = meta.get("engine") if isinstance(meta.get("engine"), dict) else {}
            split = engine.get("split") if isinstance(engine.get("split"), dict) else {}
            if str(split.get("duration_guard") or "").strip():
                unverified_guard += 1
            if engine.get("degraded"):
                degraded += 1
        return {
            "review": review,
            "review_rows": review_rows,
            "unverified_duration_guard": unverified_guard,
            "degraded_transcripts": degraded,
        }

    def stats(self) -> dict[str, Any]:
        """Whole-ledger totals for the ``status`` command."""
        conn = self._conn()
        by_state = {
            r["state"]: int(r["n"])
            for r in conn.execute("SELECT state, COUNT(*) AS n FROM items GROUP BY state").fetchall()
        }
        cursors = {
            r["name"]: {"value_present": bool(r["value"]), "updated_at": r["updated_at"]}
            for r in conn.execute("SELECT * FROM cursors").fetchall()
        }
        by_route: dict[str, dict[str, int]] = {}
        for r in conn.execute(
            "SELECT route, state, COUNT(*) AS n FROM items GROUP BY route, state"
        ).fetchall():
            by_route.setdefault(str(r["route"]), {})[str(r["state"])] = int(r["n"])
        oldest = conn.execute(
            "SELECT item_id, name, route, discovered_at FROM items WHERE state NOT IN (?,?,?)"
            " ORDER BY discovered_at ASC LIMIT 1",
            (State.DONE, State.QUARANTINED, State.SKIPPED_EMPTY),
        ).fetchone()
        return {
            "path": self.path,
            "schema_version": self.schema_version(),
            "total": sum(by_state.values()),
            "by_state": by_state,
            # Per route as well as in total, because "23 done, 3 failed" across the whole
            # service does not say that all three failures are one folder that stopped working.
            "by_route": by_route,
            "routes": tuple(sorted(by_route)),
            "cursors": cursors,
            "oldest_unfinished": dict(oldest) if oldest is not None else None,
        }

    def history(self, item_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Everything that has ever happened to one recording, oldest first."""
        records = self._conn().execute(
            "SELECT * FROM events WHERE item_id=? ORDER BY id ASC LIMIT ?", (item_id, int(limit))
        ).fetchall()
        return [dict(r) for r in records]

    def route_disagreements(
        self, since: str | None = None, limit: int | None = 200
    ) -> list[dict[str, Any]]:
        """Recordings two routes have both claimed, newest first — read by somebody.

        ``since`` is a timestamp or a bare ``YYYY-MM-DD``; events at or after it are
        returned. Omitted, every disagreement since that point comes back.

        ``limit`` caps the rows for the places that are *displaying* them — the status
        table and the morning email, where two hundred is already more than anybody reads.
        Pass ``None`` where the answer is used as a SET rather than a list: the archive
        excludes disputed recordings from being moved, and a set that silently stops at two
        hundred is not an exclusion, it is a guard that quietly lets the oldest ones
        through — into the wrong route's archive folder, which is the exact outcome this
        query exists to prevent.

        This exists because writing the event was not the same as reporting it. A
        disagreement means either a recording moved between watched folders or one route's
        source folder is nested inside another's, and the second sends a transcript to the
        wrong folder and, at sixty days, the original into the wrong archive. It is read by
        ``transcriber status`` and by the morning email, so nobody has to open SQLite to
        find out.
        """
        clause = " AND at >= ?" if (since or "").strip() else ""
        params: tuple[Any, ...] = ((since,) if clause else ())
        records = self._conn().execute(
            "SELECT events.*, items.name AS item_name, items.route AS item_route,"
            " items.state AS item_state"
            " FROM events LEFT JOIN items ON items.item_id = events.item_id"
            f" WHERE events.kind='route-disagreement'{clause}"
            " ORDER BY events.id DESC" + (" LIMIT ?" if limit is not None else ""),
            (*params, *((int(limit),) if limit is not None else ())),
        ).fetchall()
        return [dict(r) for r in records]

    def disagreement_about(self, item_id: str) -> str:
        """Why this one recording's route is in doubt, or "" when it is not.

        The per-item question, asked on the processing path. :meth:`route_disagreements`
        answers the same thing for a whole day and is right for the status table and the
        morning email; asking it per recording would scan every disagreement ever recorded
        to answer a question about one row, on every recording, forever.

        The answer is the newest disagreement's own words, because that is the sentence a
        person needs: which two routes, and which one it stayed on.

        **Only disputes NEWER than the last thing a person did to this recording count.**
        Events are append-only and a disagreement is never deleted, so without this the
        answer would be "disputed" forever — and the refusal it feeds tells the operator to
        sort the folders out and requeue, which would then re-quarantine on every attempt
        with no way out but hand-editing SQLite. A recovery path that does not recover is
        worse than no refusal at all.

        ``requeued`` and ``route-changed`` are the two operator actions, and both already
        demand a stated reason, so neither happens by accident. Doing either is a person
        saying they have dealt with it; if they have not, the next poll finds both routes
        claiming the recording again and records a fresh disagreement, which is newer than
        their action and stops it again. Nothing is lost — the whole history stays in
        ``events`` and :meth:`route_disagreements` still reports every one of them.
        """
        row = self._conn().execute(
            "SELECT detail FROM events WHERE item_id=? AND kind='route-disagreement'"
            " AND id > COALESCE("
            "   (SELECT MAX(id) FROM events WHERE item_id=?"
            "     AND kind IN ('requeued','route-changed')), 0)"
            " ORDER BY id DESC LIMIT 1",
            (item_id, item_id),
        ).fetchone()
        return str((row["detail"] if row else "") or "")

    def route_disagreement_counts(self, since: str | None = None) -> dict[str, int]:
        """How many disagreements each route is named in, for the ``status`` table.

        Both routes are counted: the one the recording stays on and the one that saw it
        again. Which of the two is misconfigured is not something this can know, and it is
        not going to guess.
        """
        counts: dict[str, int] = {}
        for event in self.route_disagreements(since):
            for name in _routes_in_disagreement(event):
                counts[name] = counts.get(name, 0) + 1
        return counts

    def recent_events(self, limit: int = 200) -> list[dict[str, Any]]:
        records = self._conn().execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(r) for r in records]

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _route_filter(route: str | None) -> tuple[str, tuple[Any, ...]]:
        """``route=None`` means every route — the whole-service view the digest still needs.

        Returned as a fragment plus its parameters so the read methods stay one query each
        rather than two spellings of the same query kept in step by hand.
        """
        if route is None:
            return "", ()
        return " AND route=?", (_cursor_route(route),)

    def _assignments(self, fields: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
        assignments: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in self._columns:
                raise LedgerError(
                    f"{key!r} is not a ledger column — one of: " + ", ".join(sorted(self._columns))
                )
            if key in _PROTECTED_COLUMNS:
                raise LedgerError(f"{key!r} is not writable this way (identity, state and discovery are not fields)")
            assignments.append(f"{key}=?")
            values.append(_adapt(key, value))
        return assignments, values

    def _event(
        self,
        conn: sqlite3.Connection,
        item_id: str | None,
        kind: str,
        at: str,
        from_state: str | None,
        to_state: str | None,
        detail: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO events (item_id, at, kind, from_state, to_state, detail) VALUES (?,?,?,?,?,?)",
            (item_id, at, kind, from_state, to_state, (detail or "")[:2000] or None),
        )


def _decode_meta(raw: Any) -> dict[str, Any]:
    """The row's meta blob as a dict, whatever state it is in. Never raises."""
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


_DISAGREEMENT_RE = re.compile(
    r"discovered on route (?P<first>\S+), seen again on route (?P<second>\S+);"
)


def _routes_in_disagreement(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Both route names out of a ``route-disagreement`` event's detail line.

    Parsed from the detail because that is where both names are: the row itself only ever
    carries the one it stayed on, and reporting only that route would hide the other half
    of the pair — which is the half a person has to look at.
    """
    detail = str(event.get("detail") or "")
    match = _DISAGREEMENT_RE.search(detail)
    names: list[str] = []
    if match:
        names = [match.group("first"), match.group("second")]
    else:
        current = str(event.get("item_route") or "").strip()
        if current:
            names = [current]
    return tuple(dict.fromkeys(n for n in names if n))


def _adapt(column: str, value: Any) -> Any:
    if column in _JSON_COLUMNS:
        return json.dumps(value if value is not None else {}, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return value


def _summarise(fields: Mapping[str, Any]) -> str | None:
    if not fields:
        return None
    parts = []
    for key, value in fields.items():
        text = str(value)
        parts.append(f"{key}={text[:120]}" if len(text) > 120 else f"{key}={text}")
    return ", ".join(parts)
