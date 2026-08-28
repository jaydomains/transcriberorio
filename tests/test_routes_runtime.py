"""What the running service does with routes: polls them, writes them, archives them.

Three properties, and each one is a way a recording is lost or misfiled without anything
raising:

  * **one broken folder is not a dead service.** A route that cannot be polled is written
    down, named in the report and stepped over. The routes after it are still polled and
    their recordings still reach the ledger. "WhatsApp is broken" and "the transcriber is
    down" have to be distinguishable, and this loop is the only place that distinction can
    be made;
  * **outputs go to the item's own route's folder, and to no other.** Not a service-wide
    default, not the first route's, not the folder the recording came from. A site
    meeting's transcript in the phone-calls folder is not an error anything downstream
    would ever report;
  * **a route archives into its own archive folder, and a route with none is skipped.** An
    empty archive folder means "this kind of recording stays where it is" — a decision, not
    a misconfiguration — so it must not read as a failure, and it must not fall back to
    somebody else's folder.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from typing import Any, Sequence

from transcriber import archive as archive_module
from transcriber import naming
from transcriber.config import Config
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, Segment, State, Transcript
from transcriber.pipeline import Pipeline
from transcriber.worker import Worker, route_poll_error_mark, route_poll_ok_mark

from . import support


CALLS = Route(
    name="calls", label="Phone calls",
    source_folder_id="S-CALLS", output_folder_id="O-CALLS", archive_folder_id="A-CALLS",
)
SITE = Route(
    name="site-meetings", label="Site meetings",
    source_folder_id="S-SITE", output_folder_id="O-SITE", archive_folder_id="A-SITE",
)
WHATSAPP = Route(
    name="whatsapp", label="WhatsApp voice notes",
    source_folder_id="S-WA", output_folder_id="O-WA", archive_folder_id="",
)


def config_with(*routes: Route, **overrides: Any) -> Config:
    return support.make_config(routes=tuple(routes), **overrides)


class _RoutedGraph:
    """A change feed per folder, so two routes really do read two different folders.

    ``breaks`` names folder ids whose delta raises, which is what a folder whose permissions
    were revoked or whose id was mistyped actually looks like from here.
    """

    def __init__(
        self,
        pages: dict[str, Sequence[tuple[Sequence[Any], str | None]]],
        breaks: dict[str, Exception] | None = None,
    ) -> None:
        self.pages = {k: [(list(i), c) for i, c in v] for k, v in pages.items()}
        self.breaks = dict(breaks or {})
        self.asked: list[tuple[str | None, str | None]] = []

    @staticmethod
    def item(item_id: str, name: str, parent: str) -> Any:
        return support.FakeGraph.item(item_id, name, parentReference={"id": parent})

    def delta_with_resync(self, folder_id=None, cursor=None, on_resync=None):
        self.asked.append((folder_id, cursor))
        if folder_id in self.breaks:
            raise self.breaks[folder_id]
        script = self.pages.get(folder_id or "", [])
        start = 0
        if cursor is not None:
            for index, (_items, page_cursor) in enumerate(script):
                if page_cursor == cursor:
                    start = index + 1
                    break
        for items, page_cursor in script[start:]:
            yield support.FakeDeltaPage(items, page_cursor, is_final=page_cursor is not None)

    def delta(self, folder_id=None, cursor=None):
        return self.delta_with_resync(folder_id, cursor, None)


class _NoPipeline:
    def process_one(self, row):  # pragma: no cover - polling must not process anything
        raise AssertionError("polling must not process anything")


class _NoHeartbeat:
    configured = False

    def success(self, note: str = ""): return None
    def start(self, note: str = ""): return None
    def fail(self, note: str = ""): return None
    def log(self, note: str = ""): return None


class OneBrokenRouteIsNotADeadService(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = config_with(CALLS, SITE, WHATSAPP, work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.graph = _RoutedGraph(
            pages={
                "S-CALLS": [([_RoutedGraph.item("C1", "Call Carel_260827_120055.m4a", "S-CALLS")], "calls-1")],
                "S-SITE": [([_RoutedGraph.item("M1", "BEACH COURT SITE WALK 270826.m4a", "S-SITE")], "site-1")],
                "S-WA": [([_RoutedGraph.item("W1", "PTT-20260827-WA0003.opus", "S-WA")], "wa-1")],
            },
            breaks={"S-WA": TimeoutError("the folder could not be read")},
        )

    def _worker(self) -> Worker:
        return Worker(
            self.config, self.ledger, self.graph, pipeline=_NoPipeline(), heartbeat=_NoHeartbeat()
        )

    def test_the_working_routes_still_record_their_recordings(self) -> None:
        result = self._worker().poll()

        self.assertEqual(sorted(result.new), ["C1", "M1"])
        self.assertEqual(self.ledger.get("C1").route, "calls")
        self.assertEqual(self.ledger.get("M1").route, "site-meetings")
        self.assertEqual(self.ledger.cursor_get("delta:calls"), "calls-1")
        self.assertEqual(self.ledger.cursor_get("delta:site-meetings"), "site-1")

    def test_the_broken_route_is_named_in_the_report(self) -> None:
        result = self._worker().poll()

        self.assertFalse(result.ok)
        self.assertEqual(result.failed_routes, ("whatsapp",))
        self.assertIn("whatsapp", result.error)
        self.assertIn("TimeoutError", result.error)
        # And the good ones are named too: "site meetings fine, WhatsApp broken" has to be
        # readable off one line, not inferred from a total that got smaller.
        line = result.line()
        self.assertIn("whatsapp", line)
        self.assertIn("calls", line)
        self.assertIn("site-meetings", line)

    def test_the_broken_route_s_cursor_did_not_move(self) -> None:
        """So nothing in that folder has been skipped: it is re-read when it recovers."""
        self._worker().poll()

        self.assertIsNone(self.ledger.cursor_get("delta:whatsapp"))
        self.assertIsNone(self.ledger.get("W1"))

    def test_a_route_that_fails_first_does_not_stop_the_ones_after_it(self) -> None:
        """Order must not decide who gets polled. The failing folder is watched first here."""
        self.config = config_with(WHATSAPP, CALLS, SITE, work_dir=self.dir.name)

        result = self._worker().poll()

        self.assertEqual(result.polled_routes, ("whatsapp", "calls", "site-meetings"))
        self.assertEqual(sorted(result.new), ["C1", "M1"])

    def test_the_failure_is_written_down_where_a_restart_cannot_lose_it(self) -> None:
        self._worker().poll()

        self.assertIn(
            "TimeoutError", self.ledger.cursor_get(route_poll_error_mark("whatsapp")) or ""
        )
        self.assertTrue(self.ledger.cursor_get(route_poll_ok_mark("calls")))
        self.assertFalse(self.ledger.cursor_get(route_poll_error_mark("calls")))

    def test_a_route_that_recovers_stops_looking_broken(self) -> None:
        """A mark that could only ever be set leaves a route broken forever after one bad hour."""
        self._worker().poll()
        self.graph.breaks.clear()

        again = self._worker().poll()

        self.assertTrue(again.ok, again.error)
        self.assertEqual(again.new, ["W1"], "the recording that was waiting must arrive")
        self.assertFalse(self.ledger.cursor_get(route_poll_error_mark("whatsapp")))
        self.assertTrue(self.ledger.cursor_get(route_poll_ok_mark("whatsapp")))

    def test_a_paused_route_is_not_polled_at_all(self) -> None:
        self.config = config_with(CALLS, replace(SITE, enabled=False),
                                  work_dir=self.dir.name)

        result = self._worker().poll()

        self.assertEqual(result.polled_routes, ("calls",))
        self.assertIsNone(self.ledger.get("M1"))
        self.assertNotIn("S-SITE", [folder for folder, _cursor in self.graph.asked])

    def test_every_route_switched_off_is_a_loud_error_not_a_quiet_success(self) -> None:
        self.config = config_with(
            replace(CALLS, enabled=False), work_dir=self.dir.name
        )

        result = self._worker().poll()

        self.assertFalse(result.ok)
        self.assertIn("paused", result.error)


class AWholeCycleCarriesOnAroundABrokenRoute(unittest.TestCase):
    """Not just the poll: the work already discovered on every route is still processed.

    A cycle that abandoned the drain because one folder could not be read would leave every
    other route's recordings sitting in the ledger until the folder was fixed — which is a
    service that is down, reported as a service with one broken route.
    """

    class _CountingPipeline:
        def __init__(self, ledger: Ledger) -> None:
            self.ledger = ledger
            self.seen: list[str] = []

        def process_one(self, row):
            self.seen.append(row.item_id)
            for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED,
                          State.DONE):
                self.ledger.advance(row.item_id, state)
            return type("Outcome", (), {
                "item_id": row.item_id, "result": "done", "detail": "", "route": row.route,
                "ok": True, "needs_a_person": False, "line": lambda self=None: "done",
            })()

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = config_with(CALLS, SITE, WHATSAPP, work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        support.quiesce_scheduled_jobs(self.config, self.ledger)
        self.graph = _RoutedGraph(
            pages={
                "S-CALLS": [([_RoutedGraph.item("C1", "Call Carel_260827_120055.m4a", "S-CALLS")], "calls-1")],
                "S-SITE": [([_RoutedGraph.item("M1", "BEACH COURT SITE WALK 270826.m4a", "S-SITE")], "site-1")],
            },
            breaks={"S-WA": TimeoutError("the folder could not be read")},
        )
        self.pipeline = self._CountingPipeline(self.ledger)

    def test_the_other_routes_recordings_are_still_processed(self) -> None:
        worker = Worker(self.config, self.ledger, self.graph,
                        pipeline=self.pipeline, heartbeat=_NoHeartbeat())

        report = worker.run_once()

        self.assertEqual(sorted(self.pipeline.seen), ["C1", "M1"],
                         "a broken folder stopped the work that had nothing to do with it")
        self.assertEqual(self.ledger.get("C1").state, State.DONE)
        self.assertEqual(self.ledger.get("M1").state, State.DONE)

    def test_the_cycle_is_not_ok_and_names_the_route_that_broke(self) -> None:
        worker = Worker(self.config, self.ledger, self.graph,
                        pipeline=self.pipeline, heartbeat=_NoHeartbeat())

        report = worker.run_once()

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_routes, ("whatsapp",))
        self.assertTrue(any(e.startswith("whatsapp:") for e in report.errors), report.errors)
        # One error per failed route, not one line saying "the poll failed" while two of the
        # three folders are perfectly fine.
        self.assertEqual(len([e for e in report.errors if "TimeoutError" in e]), 1)

    def test_the_failure_reaches_the_mark_status_reads(self) -> None:
        worker = Worker(self.config, self.ledger, self.graph,
                        pipeline=self.pipeline, heartbeat=_NoHeartbeat())

        worker.run_once()

        detail = self.ledger.cursor_get("worker:last_cycle_error_detail") or ""
        self.assertIn("whatsapp", detail, "a failure nobody can see is a failure nobody fixes")


class OutputsGoToTheItemsOwnRoute(unittest.TestCase):
    """Two routes, and the second one's transcript must not land in the first one's folder."""

    class _Uploads:
        """A drive that records which folder each file was written into."""

        def __init__(self) -> None:
            self.written: list[tuple[str, str]] = []   # (parent_id, name)
            self.items: dict[str, Any] = {}

        def upload(self, parent_id: str, name: str, data: bytes) -> Any:
            self.written.append((parent_id, name))
            item = type("Item", (), {
                "id": f"out-{len(self.items)}", "name": name, "size": len(data),
                "web_url": f"https://example.invalid/{name}",
            })()
            self.items[item.id] = item
            return item

        def get_item(self, item_id: str) -> Any:
            return self.items[item_id]

        def folders_used(self) -> set[str]:
            return {parent for parent, _name in self.written}

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = config_with(CALLS, SITE, work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.drive = self._Uploads()
        self.pipeline = Pipeline(self.config, self.ledger, self.drive)

    def _row(self, item_id: str, name: str, route: str):
        self.ledger.record_page(
            [DriveItem(item_id=item_id, name=name, size=4096, etag=f'"{item_id}"',
                       created_at="2026-08-27T09:00:00Z")],
            f"{route}-1",
            route=route,
        )
        return self.ledger.get(item_id)

    def _publish(self, row):
        text = (
            "Right, I'm at Beach Court now. Spoke to Carel about the roof leak at unit four. "
            "I told him we'd get a price for the remedial before the end of the month."
        )
        return self.pipeline._publish(
            row,
            naming.parse_source_name(row.name),
            Transcript(
                text=text,
                segments=[Segment(0.0, 12.0, "James", text)],
                language="en-ZA",
                engine="test-engine",
            ),
            support.StubExtraction(summary="He walked the site and spoke about a roof leak."),
            support.audio_info(120.0),
            self.pipeline.route_of(row),
        )

    def test_the_second_route_s_files_do_not_land_in_the_first_route_s_folder(self) -> None:
        first = self._row("C1", "Call Carel_260827_120055.m4a", "calls")
        second = self._row("M1", "BEACH COURT SITE WALK 270826.m4a", "site-meetings")

        self._publish(first)
        written_after_calls = list(self.drive.written)
        self._publish(second)
        written_for_site = self.drive.written[len(written_after_calls):]

        self.assertEqual({p for p, _n in written_after_calls}, {"O-CALLS"})
        self.assertEqual(len(written_for_site), 3, "all three files, or none")
        self.assertEqual(
            {p for p, _n in written_for_site}, {"O-SITE"},
            "a site meeting's transcript was written into the phone-calls folder",
        )
        self.assertNotIn(
            "O-CALLS", {p for p, _n in written_for_site},
            "the first route's folder is not a fallback",
        )

    def test_the_route_is_read_from_the_row_not_from_the_first_route(self) -> None:
        row = self._row("M1", "BEACH COURT SITE WALK 270826.m4a", "site-meetings")

        self.assertEqual(self.pipeline.route_of(row).name, "site-meetings")
        self.assertEqual(self.pipeline.route_of(row).output_folder_id, "O-SITE")
        self.assertNotEqual(
            self.pipeline.route_of(row).output_folder_id, self.config.output_folder_id
        )

    def test_pooled_routes_both_write_into_the_one_shared_folder(self) -> None:
        """He asked to be able to pool inputs. Doing so must actually pool them."""
        pooled_calls = replace(CALLS, output_folder_id="POOL")
        pooled_site = replace(SITE, output_folder_id="POOL")
        self.config.routes = (pooled_calls, pooled_site)

        self._publish(self._row("C1", "Call Carel_260827_120055.m4a", "calls"))
        self._publish(self._row("M1", "BEACH COURT SITE WALK 270826.m4a", "site-meetings"))

        self.assertEqual(self.drive.folders_used(), {"POOL"})
        self.assertEqual(len(self.drive.written), 6)

    def test_a_row_naming_a_route_that_no_longer_exists_stops_loudly(self) -> None:
        """The alternative is writing it into whichever folder happened to be first."""
        row = self._row("W1", "PTT-20260827-WA0003.opus", "whatsapp")

        with self.assertRaises(Exception) as caught:
            self.pipeline.route_of(row)

        message = str(caught.exception)
        self.assertIn("whatsapp", message)
        self.assertIn("calls", message, "it must say which routes do exist")
        self.assertIn("Nothing has been written", message)
        self.assertEqual(self.drive.written, [], "nothing may be written on this path")

    def test_a_route_with_no_output_folder_writes_nothing_and_says_why(self) -> None:
        self.config.routes = (Route(name="calls", label="Phone calls",
                                    source_folder_id="S-CALLS", output_folder_id=""),)
        row = self._row("C1", "Call Carel_260827_120055.m4a", "calls")

        with self.assertRaises(Exception) as caught:
            self._publish(row)

        self.assertIn("ROUTE_CALLS_OUTPUT", str(caught.exception))
        self.assertEqual(self.drive.written, [], "nothing may be uploaded with nowhere to put it")


class ArchiveUsesTheRoutesOwnFolder(unittest.TestCase):
    class _Drive:
        """Every output is real; every move is recorded with the folder it went to."""

        def __init__(self, sources: dict[str, str]) -> None:
            self.sources = dict(sources)       # item_id -> current parent folder id
            self.moves: list[tuple[str, str]] = []

        def get_item(self, item_id: str) -> Any:
            if item_id in self.sources:
                return type("Item", (), {
                    "id": item_id, "name": f"{item_id}.m4a", "size": 4096,
                    "parent_id": self.sources[item_id], "is_deleted": False, "is_folder": False,
                })()
            # An output file, confirmed present with bytes in it.
            return type("Item", (), {
                "id": item_id, "name": f"{item_id}.md", "size": 1024,
                "parent_id": "O", "is_deleted": False, "is_folder": False,
            })()

        def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
            self.moves.append((item_id, parent_id))
            self.sources[item_id] = parent_id
            return self.get_item(item_id)

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.config = config_with(CALLS, SITE, WHATSAPP, work_dir=self.dir.name,
                                  archive_age_days=60)
        self.now = 1_800_000_000.0
        self.old = "2020-01-01T09:00:00Z"

    def _finished(self, item_id: str, route: str, source_folder: str) -> None:
        self.ledger.record_page(
            [DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=4096,
                       parent_id=source_folder, created_at=self.old)],
            f"{route}-1",
            route=route,
        )
        for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
            self.ledger.advance(item_id, state)
        self.ledger.advance(
            item_id, State.DONE,
            transcript_name=f"{item_id}-transcript.md",
            summary_name=f"{item_id}-summary.md",
            actions_name=f"{item_id}-actions.md",
            output_item_ids={"transcript": f"{item_id}-t", "summary": f"{item_id}-s",
                             "actions": f"{item_id}-a"},
            done_at="2020-01-01T09:30:00Z",
        )

    def test_each_route_s_recording_goes_into_that_route_s_archive_folder(self) -> None:
        self._finished("C1", "calls", "S-CALLS")
        self._finished("M1", "site-meetings", "S-SITE")
        drive = self._Drive({"C1": "S-CALLS", "M1": "S-SITE"})

        run = archive_module.archive(self.config, self.ledger, drive, now=self.now)

        self.assertTrue(run.ok, run.errors)
        self.assertEqual(dict(drive.moves), {"C1": "A-CALLS", "M1": "A-SITE"})
        self.assertEqual(self.ledger.get("C1").parent_id, "A-CALLS")
        self.assertEqual(self.ledger.get("M1").parent_id, "A-SITE")

    def test_a_route_with_no_archive_folder_is_skipped_not_failed(self) -> None:
        """An empty archive folder is a decision: this kind of recording stays where it is."""
        self._finished("W1", "whatsapp", "S-WA")
        drive = self._Drive({"W1": "S-WA"})

        run = archive_module.archive(self.config, self.ledger, drive, now=self.now)

        self.assertTrue(run.ok, f"a skipped route is not a failure: {run.errors}")
        self.assertEqual(drive.moves, [], "nothing may be moved for a route with no archive")
        self.assertIsNone(self.ledger.get("W1").archived_at)

        report = run.report_for("whatsapp")
        self.assertIsNotNone(report, "a skipped route must still be listed")
        self.assertTrue(report.skipped)
        self.assertTrue(report.ok)
        self.assertIn("stay where they are", report.skipped)
        self.assertIn("whatsapp", run.render())

    def test_a_recording_is_never_moved_into_another_route_s_archive(self) -> None:
        """Checked again immediately before the move, not taken on trust from the query."""
        self._finished("M1", "site-meetings", "S-SITE")
        drive = self._Drive({"M1": "S-SITE"})

        report = archive_module.archive_route(
            self.config, self.ledger, drive, CALLS, now=self.now
        )

        self.assertEqual(drive.moves, [])
        self.assertEqual(report.considered, 0, "the ledger's own filter is per route")

        held = archive_module._may_move(
            self.ledger.get("M1"), 60, self.now, route_name="calls"
        )
        self.assertFalse(held[0])
        self.assertIn("site-meetings", held[1])

    def test_one_route_s_bad_month_does_not_stop_the_others(self) -> None:
        self._finished("C1", "calls", "S-CALLS")
        self._finished("M1", "site-meetings", "S-SITE")

        class _HalfBroken(self._Drive):
            def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
                if parent_id == "A-CALLS":
                    raise RuntimeError("OneDrive said no")
                return super().move(item_id, parent_id, new_name)

        drive = _HalfBroken({"C1": "S-CALLS", "M1": "S-SITE"})
        run = archive_module.archive(self.config, self.ledger, drive, now=self.now)

        self.assertEqual(dict(drive.moves), {"M1": "A-SITE"}, "site meetings still archived")
        self.assertIsNone(self.ledger.get("C1").archived_at, "a failed move is never recorded")

        # Not ok, and the route that was not ok is named — in the headline the worker logs,
        # in the report it stores, and on the outcome itself.
        self.assertFalse(run.ok)
        self.assertEqual(run.failed, 1)
        self.assertIn("Phone calls", run.headline())
        self.assertIn("PROBLEMS", run.headline())
        self.assertTrue(run.report_for("site-meetings").ok)
        self.assertFalse(run.report_for("calls").ok)
        failed = [o for o in run.outcomes if o.result == archive_module.FAILED]
        self.assertEqual([o.route for o in failed], ["calls"])
        self.assertIn("OneDrive said no", run.render())

    def test_a_recording_whose_outputs_cannot_be_confirmed_is_held_back(self) -> None:
        """The original is only ever moved on evidence, never on our belief that we finished."""
        self._finished("C1", "calls", "S-CALLS")

        class _NoOutputs(self._Drive):
            def get_item(self, item_id: str) -> Any:
                if item_id not in self.sources:
                    raise RuntimeError("404 not found")
                return super().get_item(item_id)

        drive = _NoOutputs({"C1": "S-CALLS"})
        run = archive_module.archive(self.config, self.ledger, drive, now=self.now)

        self.assertEqual(drive.moves, [])
        self.assertEqual(run.held_back, 1)
        self.assertIsNone(self.ledger.get("C1").archived_at)

    def test_finished_recordings_on_a_route_nobody_watches_are_said_out_loud(self) -> None:
        """Taking a route out of ROUTES deletes nothing, so its history is still ageing."""
        self._finished("X1", "old-device", "S-OLD")
        drive = self._Drive({"X1": "S-OLD"})

        run = archive_module.archive(self.config, self.ledger, drive, now=self.now)

        self.assertEqual(drive.moves, [])
        self.assertTrue(any("old-device" in note for note in run.notes), run.notes)
        self.assertIn("untouched", " ".join(run.notes))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
