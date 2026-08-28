"""The load-bearing invariant, once per route — and the upgrade of a database in the field.

``ARCHITECTURE.md`` puts it plainly: the delta cursor is committed in the same transaction
as the rows from its page, and cannot advance past a file that was not recorded. Routes did
not weaken that; they multiplied it. Each route polls its own folder with its own cursor,
so the property has to hold **per route**, and one route's bad page must not move another
route's mark by a single byte.

Two failures are tested for here, and both of them are silent in the field:

  * a route's page that fails to commit taking another route's cursor with it — a
    recording skipped on a folder that was working fine;
  * an upgraded ledger whose existing rows come back belonging to no route, or to a route
    the configuration cannot name, which is a whole history of recordings that the archive
    pass, the digest and ``status`` can no longer account for.

The migration fixture is built from the shipped v1 statements rather than from a
hand-written copy of them, so what is upgraded here is the schema that is actually in the
field and not this test's memory of it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from transcriber.ledger import (
    DEFAULT_ROUTE,
    Ledger,
    LedgerInvariantError,
    _MIGRATIONS,
    delta_cursor_name,
    sweep_cursor_name,
)
from transcriber.models import DriveItem, State


def _item(item_id: str, name: str) -> DriveItem:
    return DriveItem(item_id=item_id, name=name, size=2048, etag=f'"{item_id}"')


def _v1_database(path: str, *, rows: list[tuple[str, str]], cursors: dict[str, str]) -> None:
    """A ledger exactly as the version before routes wrote it: no ``route`` column.

    The schema comes from the shipped migration, so this is the real old database and not a
    plausible imitation of one. Written with plain sqlite3 because :class:`Ledger` would
    upgrade it on the way in, which is the thing under test.
    """
    _version, _note, statements = _MIGRATIONS[0]
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, note TEXT)"
        )
        for sql in statements:
            conn.execute(sql)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at, note) VALUES (1,?,?)",
            ("2026-08-01T00:00:00Z", "initial schema"),
        )
        for item_id, name in rows:
            conn.execute(
                "INSERT INTO items (item_id, name, state, size, discovered_at, updated_at,"
                " transcript_name, summary_name, actions_name, done_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id, name, State.DONE, 4096,
                    "2026-08-01T09:00:00Z", "2026-08-01T09:30:00Z",
                    f"{item_id}-transcript.md", f"{item_id}-summary.md", f"{item_id}-actions.md",
                    "2026-08-01T09:30:00Z",
                ),
            )
        for name, value in cursors.items():
            conn.execute(
                "INSERT INTO cursors (name, value, updated_at) VALUES (?,?,?)",
                (name, value, "2026-08-01T09:30:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


class ADatabaseWrittenBeforeRoutesUpgradesInPlace(unittest.TestCase):
    """The one in the field. It is opened, not rebuilt, and nothing in it is lost."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ledger.sqlite3")

    def test_the_old_rows_read_back_as_the_default_route(self) -> None:
        _v1_database(
            self.path,
            rows=[("A", "Call Carel_260801_120055.m4a"), ("B", "BEACH COURT SITE WALK.m4a")],
            cursors={"delta:source": "old-delta-link"},
        )

        with Ledger(self.path) as ledger:
            self.assertEqual(ledger.get("A").route, DEFAULT_ROUTE)
            self.assertEqual(ledger.get("B").route, "default")
            self.assertEqual(ledger.routes_seen(), ("default",))

    def test_the_column_exists_and_is_not_null(self) -> None:
        """Defaulting to NULL would make the rows belong to a route nobody can name."""
        _v1_database(self.path, rows=[("A", "one.m4a")], cursors={})

        with Ledger(self.path):
            pass
        conn = sqlite3.connect(self.path)
        try:
            columns = {r[1]: r for r in conn.execute("PRAGMA table_info(items)").fetchall()}
            self.assertIn("route", columns)
            self.assertEqual(columns["route"][3], 1, "route must be NOT NULL")
            self.assertEqual(columns["route"][4], "'default'")
            nulls = conn.execute("SELECT COUNT(*) FROM items WHERE route IS NULL").fetchone()[0]
            self.assertEqual(nulls, 0)
        finally:
            conn.close()

    def test_nothing_is_lost_in_the_upgrade(self) -> None:
        _v1_database(
            self.path,
            rows=[("A", "one.m4a"), ("B", "two.m4a"), ("C", "three.m4a")],
            cursors={"delta:source": "old-delta-link"},
        )

        with Ledger(self.path) as ledger:
            for item_id in ("A", "B", "C"):
                row = ledger.get(item_id)
                self.assertIsNotNone(row, f"{item_id} did not survive the upgrade")
                self.assertEqual(row.state, State.DONE)
                self.assertEqual(row.transcript_name, f"{item_id}-transcript.md")

    def test_the_old_cursor_keeps_its_value_under_the_new_name(self) -> None:
        """Dropping it would be safe and would make every upgraded install re-enumerate its
        whole folder on the first poll, which looks exactly like a fault."""
        _v1_database(
            self.path,
            rows=[("A", "one.m4a")],
            cursors={"delta:source": "old-delta-link", "delta:sweep": "old-sweep-link"},
        )

        with Ledger(self.path) as ledger:
            self.assertEqual(ledger.cursor_get(delta_cursor_name("default")), "old-delta-link")
            self.assertEqual(ledger.cursor_get(sweep_cursor_name("default")), "old-sweep-link")

    def test_the_upgrade_runs_once_and_is_recorded(self) -> None:
        _v1_database(self.path, rows=[("A", "one.m4a")], cursors={})

        with Ledger(self.path) as ledger:
            first = ledger.schema_version()
        with Ledger(self.path) as reopened:
            self.assertEqual(reopened.schema_version(), first)
            self.assertGreaterEqual(first, 2)

        conn = sqlite3.connect(self.path)
        try:
            applied = conn.execute(
                "SELECT COUNT(*) FROM schema_version WHERE version=2"
            ).fetchone()[0]
            self.assertEqual(applied, 1, "the route migration ran more than once")
        finally:
            conn.close()

    def test_an_upgraded_ledger_still_records_new_work_on_the_default_route(self) -> None:
        """The upgrade is not a museum piece: the service carries on into the same database."""
        _v1_database(self.path, rows=[("A", "one.m4a")], cursors={"delta:source": "old-link"})

        with Ledger(self.path) as ledger:
            ledger.record_page([_item("D", "four.m4a")], "new-link")
            self.assertEqual(ledger.get("D").route, "default")
            self.assertEqual(ledger.cursor_get(delta_cursor_name("default")), "new-link")
            self.assertEqual(sorted(r.item_id for r in ledger.unfinished("default")), ["D"])


class TwoRoutesAreTwoCursors(unittest.TestCase):
    """Discovering on one route must not move the other's mark, ever, by any path."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def test_the_cursor_names_are_per_route(self) -> None:
        self.assertEqual(delta_cursor_name("calls"), "delta:calls")
        self.assertEqual(sweep_cursor_name("site-meetings"), "sweep:site-meetings")
        self.assertNotEqual(delta_cursor_name("calls"), delta_cursor_name("site-meetings"))

    def test_recording_on_one_route_leaves_the_other_untouched(self) -> None:
        self.ledger.record_page([_item("A", "one.m4a")], "calls-1", route="calls")

        self.assertEqual(self.ledger.cursor_get("delta:calls"), "calls-1")
        self.assertIsNone(
            self.ledger.cursor_get("delta:site-meetings"),
            "a route that has never polled must not acquire a cursor from another route",
        )
        self.assertEqual(self.ledger.get("A").route, "calls")

    def test_each_route_advances_only_its_own(self) -> None:
        self.ledger.record_page([_item("A", "one.m4a")], "calls-1", route="calls")
        self.ledger.record_page([_item("B", "two.m4a")], "site-1", route="site-meetings")
        self.ledger.record_page([_item("C", "three.m4a")], "calls-2", route="calls")

        self.assertEqual(self.ledger.cursor_get("delta:calls"), "calls-2")
        self.assertEqual(self.ledger.cursor_get("delta:site-meetings"), "site-1")
        self.assertEqual(
            sorted(r.item_id for r in self.ledger.unfinished("calls")), ["A", "C"]
        )
        self.assertEqual([r.item_id for r in self.ledger.unfinished("site-meetings")], ["B"])
        self.assertEqual(len(self.ledger.unfinished()), 3, "no route filter is every route")

    def test_a_failure_between_one_route_s_rows_and_its_cursor_takes_only_that_route_down(self) -> None:
        """The crash the invariant exists for, with a second route standing beside it.

        ``_write_cursor`` is the last statement inside ``record_page``'s transaction, so
        raising there puts the process death precisely between "rows written" and "cursor
        written". Route A must replay; route B must not notice anything happened.
        """
        self.ledger.record_page([_item("A1", "a-one.m4a")], "calls-1", route="calls")
        self.ledger.record_page([_item("B1", "b-one.m4a")], "site-1", route="site-meetings")

        original = self.ledger._write_cursor
        attempted: list[str] = []

        def die_before_writing_the_cursor(conn, name, value, now):  # type: ignore[no-untyped-def]
            attempted.append(name)
            raise RuntimeError("the process died here")

        self.ledger._write_cursor = die_before_writing_the_cursor  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.ledger.record_page(
                    [_item("A2", "a-two.m4a"), _item("A3", "a-three.m4a")],
                    "calls-2",
                    route="calls",
                )
        finally:
            self.ledger._write_cursor = original  # type: ignore[method-assign]

        self.assertEqual(attempted, ["delta:calls"], "the failure was injected at the wrong point")

        # Route A: the rows from the failed page are gone and its cursor did not move, so
        # the next poll re-reads them.
        self.assertIsNone(self.ledger.get("A2"))
        self.assertIsNone(self.ledger.get("A3"))
        self.assertEqual(self.ledger.cursor_get("delta:calls"), "calls-1")
        self.assertIsNotNone(self.ledger.get("A1"), "the page that committed is untouched")

        # Route B: not one thing about it changed.
        self.assertEqual(self.ledger.cursor_get("delta:site-meetings"), "site-1")
        self.assertIsNotNone(self.ledger.get("B1"))

        # And route A really does replay: the same page again lands both rows.
        replayed = self.ledger.record_page(
            [_item("A2", "a-two.m4a"), _item("A3", "a-three.m4a")], "calls-2", route="calls"
        )
        self.assertEqual(sorted(replayed), ["A2", "A3"], "the lost recordings must come back")
        self.assertEqual(self.ledger.cursor_get("delta:calls"), "calls-2")
        self.assertEqual(self.ledger.cursor_get("delta:site-meetings"), "site-1")

    def test_the_rollback_reaches_the_disk_and_the_other_route_is_still_there(self) -> None:
        """Re-opening the database proves it, rather than trusting one connection's view."""
        path = os.path.join(self.dir.name, "durable.sqlite3")
        with Ledger(path) as ledger:
            ledger.record_page([_item("A1", "a-one.m4a")], "calls-1", route="calls")
            ledger.record_page([_item("B1", "b-one.m4a")], "site-1", route="site-meetings")
            ledger._write_cursor = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError):
                ledger.record_page([_item("A2", "a-two.m4a")], "calls-2", route="calls")

        with Ledger(path) as reopened:
            self.assertEqual(reopened.cursor_get("delta:calls"), "calls-1")
            self.assertIsNone(reopened.get("A2"))
            self.assertEqual(reopened.cursor_get("delta:site-meetings"), "site-1")
            self.assertIsNotNone(reopened.get("B1"))

    def test_no_route_s_delta_cursor_can_be_set_on_its_own(self) -> None:
        """There is no back door, and adding routes did not open one per route."""
        for name in ("delta:calls", "sweep:calls", "delta:site-meetings", "delta:default"):
            with self.assertRaises(LedgerInvariantError, msg=f"{name} was settable"):
                self.ledger.cursor_set(name, "cursor-99")
            self.assertIsNone(self.ledger.cursor_get(name))

    def test_a_route_s_page_with_no_cursor_is_refused_outright(self) -> None:
        with self.assertRaises(LedgerInvariantError):
            self.ledger.record_page([_item("A", "one.m4a")], "", route="calls")
        self.assertIsNone(self.ledger.get("A"))

    def test_rewinding_one_route_leaves_the_others_where_they_are(self) -> None:
        self.ledger.record_page([_item("A", "one.m4a")], "calls-1", route="calls")
        self.ledger.record_page([_item("B", "two.m4a")], "site-1", route="site-meetings")

        self.ledger.rewind_cursor("delta:calls", "Graph rejected the cursor with 410")

        self.assertIsNone(self.ledger.cursor_get("delta:calls"))
        self.assertEqual(self.ledger.cursor_get("delta:site-meetings"), "site-1")
        self.assertIsNotNone(self.ledger.get("A"), "a rewind must not delete what was recorded")

    def test_a_route_name_a_cursor_key_cannot_be_built_from_is_refused(self) -> None:
        """The name is half of a cursor key. Anything that would need escaping stops here."""
        for bad in ("Calls", "site meetings", "", "calls/../default"):
            with self.assertRaises(Exception, msg=f"{bad!r} was accepted as a route"):
                self.ledger.record_page([_item("X", "x.m4a")], "cursor", route=bad)

    def test_a_recording_keeps_the_route_it_arrived_on(self) -> None:
        """Seeing it again on another route is a configuration fault, not a re-filing."""
        self.ledger.record_page([_item("A", "one.m4a")], "calls-1", route="calls")
        self.ledger.record_page([_item("A", "one.m4a")], "site-1", route="site-meetings")

        self.assertEqual(self.ledger.get("A").route, "calls")
        kinds = [e["kind"] for e in self.ledger.history("A")]
        self.assertIn("route-disagreement", kinds, "it must be visible, not resolved quietly")

    def test_the_per_route_counts_do_not_bleed_into_each_other(self) -> None:
        self.ledger.record_page(
            [_item("A1", "a-one.m4a"), _item("A2", "a-two.m4a")], "calls-1", route="calls"
        )
        self.ledger.record_page([_item("B1", "b-one.m4a")], "site-1", route="site-meetings")
        # Read each cohort's own day off the rows, so a run that happens to straddle
        # midnight UTC does not turn a real assertion into a coin toss.
        calls_day = self.ledger.get("A1").discovered_at[:10]
        site_day = self.ledger.get("B1").discovered_at[:10]

        self.assertEqual(self.ledger.counts_for_day(calls_day, "calls")["discovered"], 2)
        self.assertEqual(self.ledger.counts_for_day(site_day, "site-meetings")["discovered"], 1)
        self.assertEqual(
            self.ledger.counts_for_day(calls_day, "site-meetings")["discovered"],
            1 if site_day == calls_day else 0,
            "one route's recordings were counted against another's",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
