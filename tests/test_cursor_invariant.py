"""The cursor invariant: the delta cursor cannot advance past an unrecorded recording.

This is the property the whole service is built on. If the cursor can move ahead of the
rows from its page, then a crash in the wrong microsecond loses a recording permanently and
nothing anywhere will ever say so — no error, no gap, no row. Every other guarantee in this
codebase is worth less than this one.

Three ways it is asserted here:

  * a failure *between* writing the rows and writing the cursor takes both down together —
    the rows are gone and the cursor did not move;
  * the next poll therefore returns the same items;
  * there is no polite API for advancing a delta cursor on its own.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.ledger import DELTA_CURSOR, Ledger, LedgerInvariantError
from transcriber.models import DriveItem, State
from transcriber.worker import Worker

from . import support


def _item(item_id: str, name: str) -> DriveItem:
    return DriveItem(item_id=item_id, name=name, size=2048, etag=f'"{item_id}"')


class CursorAndRowsCommitTogether(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def test_a_page_records_its_rows_and_its_cursor(self) -> None:
        new = self.ledger.record_page([_item("A", "one.m4a"), _item("B", "two.m4a")], "cursor-1")

        self.assertEqual(sorted(new), ["A", "B"])
        self.assertEqual(self.ledger.cursor_get(DELTA_CURSOR), "cursor-1")
        self.assertIsNotNone(self.ledger.get("A"))
        self.assertIsNotNone(self.ledger.get("B"))

    def test_a_failure_after_the_rows_and_before_the_cursor_loses_both(self) -> None:
        """The exact crash the invariant exists for, simulated at the one dangerous moment.

        ``_write_cursor`` is the last statement inside ``record_page``'s transaction, so
        blowing it up puts the process death precisely between "rows written" and "cursor
        written". If the two were not one transaction, the rows would survive here — or,
        far worse, in the other order, the cursor would.
        """
        self.ledger.record_page([_item("A", "one.m4a")], "cursor-1")

        original = self.ledger._write_cursor
        calls: list[str] = []

        def die_before_writing_the_cursor(conn, name, value, now):  # type: ignore[no-untyped-def]
            calls.append(name)
            raise RuntimeError("the process died here")

        self.ledger._write_cursor = die_before_writing_the_cursor  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.ledger.record_page([_item("B", "two.m4a"), _item("C", "three.m4a")], "cursor-2")
        finally:
            self.ledger._write_cursor = original  # type: ignore[method-assign]

        self.assertEqual(calls, [DELTA_CURSOR], "the failure was injected at the wrong point")
        # The rows from the failed page are gone...
        self.assertIsNone(self.ledger.get("B"))
        self.assertIsNone(self.ledger.get("C"))
        # ...and the cursor did not move past them.
        self.assertEqual(self.ledger.cursor_get(DELTA_CURSOR), "cursor-1")
        # The page committed before it is untouched.
        self.assertIsNotNone(self.ledger.get("A"))

    def test_the_rows_survive_only_if_the_cursor_did(self) -> None:
        """Re-opening the database proves the rollback reached the disk, not just memory."""
        path = os.path.join(self.dir.name, "durable.sqlite3")
        with Ledger(path) as ledger:
            ledger.record_page([_item("A", "one.m4a")], "cursor-1")
            ledger._write_cursor = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError):
                ledger.record_page([_item("B", "two.m4a")], "cursor-2")

        with Ledger(path) as reopened:
            self.assertEqual(reopened.cursor_get(DELTA_CURSOR), "cursor-1")
            self.assertIsNone(reopened.get("B"))

    def test_a_delta_cursor_cannot_be_set_on_its_own(self) -> None:
        """There is no back door. Advancing a delta cursor without rows is refused."""
        with self.assertRaises(LedgerInvariantError):
            self.ledger.cursor_set(DELTA_CURSOR, "cursor-99")
        self.assertIsNone(self.ledger.cursor_get(DELTA_CURSOR))

    def test_record_page_refuses_a_page_with_no_cursor(self) -> None:
        with self.assertRaises(LedgerInvariantError):
            self.ledger.record_page([_item("A", "one.m4a")], "")
        self.assertIsNone(self.ledger.get("A"))

    def test_rewinding_is_allowed_because_it_loses_nothing(self) -> None:
        self.ledger.record_page([_item("A", "one.m4a")], "cursor-1")
        self.ledger.rewind_cursor(DELTA_CURSOR, "Graph rejected the cursor with 410")
        self.assertIsNone(self.ledger.cursor_get(DELTA_CURSOR))
        self.assertIsNotNone(self.ledger.get("A"), "a rewind must not delete what was recorded")


class TheNextPollReturnsTheSameItems(unittest.TestCase):
    """End to end through ``Worker.poll``: a lost page is re-read, not skipped."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.graph = support.FakeGraph(
            [
                ([support.FakeGraph.item("A", "Call Carel_260827_120055.m4a")], "cursor-1"),
                ([support.FakeGraph.item("B", "BEACH COURT SITE WALK 270826.m4a")], "cursor-2"),
            ]
        )

    def _worker(self) -> Worker:
        return Worker(self.config, self.ledger, self.graph, pipeline=_NoPipeline(), heartbeat=_NoHeartbeat())

    def test_a_poll_that_fails_mid_page_re_reads_that_page(self) -> None:
        worker = self._worker()

        # First poll: both pages land, cursor at the end.
        first = worker.poll()
        self.assertEqual(first.error, "")
        self.assertEqual(sorted(first.new), ["A", "B"])
        self.assertEqual(self.ledger.cursor_get(DELTA_CURSOR), "cursor-2")

        # A third page arrives and the worker dies while committing it.
        self.graph.pages.append(([support.FakeGraph.item("C", "Call Sipho_260827_161049.m4a")], "cursor-3"))
        broken = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(broken.close)
        broken._write_cursor = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))  # type: ignore[method-assign]
        crashed = Worker(self.config, broken, self.graph, pipeline=_NoPipeline(), heartbeat=_NoHeartbeat())
        result = crashed.poll()
        self.assertIn("crash", result.error, "the poll must report the failure, not swallow it")
        self.assertIsNone(self.ledger.get("C"))
        self.assertEqual(self.ledger.cursor_get(DELTA_CURSOR), "cursor-2")

        # The next poll asks Graph from the cursor that did survive, and gets C again.
        again = self._worker().poll()
        self.assertEqual(again.error, "")
        self.assertEqual(again.new, ["C"], "the recording lost to the crash must come back")
        self.assertEqual(self.ledger.cursor_get(DELTA_CURSOR), "cursor-3")
        self.assertEqual(self.ledger.get("C").state, State.DISCOVERED)

    def test_a_page_with_no_cursor_records_its_rows_and_holds_the_cursor_back(self) -> None:
        """Graph returned neither nextLink nor deltaLink. Re-reading is free; skipping is not."""
        self.graph = support.FakeGraph([([support.FakeGraph.item("A", "one.m4a")], None)])
        worker = self._worker()

        result = worker.poll()

        self.assertEqual(result.new, ["A"])
        self.assertEqual(result.cursor_held_back, 1)
        self.assertIsNone(self.ledger.cursor_get(DELTA_CURSOR))
        self.assertIsNotNone(self.ledger.get("A"))


class _NoPipeline:
    """The poll tests never process anything; a real Pipeline would want an engine key."""

    def process_one(self, row):  # pragma: no cover - never reached in these tests
        raise AssertionError("polling must not process anything")


class _NoHeartbeat:
    configured = False

    def success(self, note: str = ""): return None
    def start(self, note: str = ""): return None
    def fail(self, note: str = ""): return None
    def log(self, note: str = ""): return None


if __name__ == "__main__":
    unittest.main()
