"""What a second pass over routes found, and what now stops it happening again.

Every class here is one way the multi-folder change could file a recording under the wrong
kind, or hide the fact that it had. Two of them are the ones that cost audio:

  * **one route's watched folder inside another's.** ``/Recordings`` for phone calls with
    ``/Recordings/SiteMeetings`` inside it is a natural way to organise a drive, both
    folders are pickable, and their ids are simply two different strings — nothing in an id
    says one contains the other. But OneDrive's change feed reports a folder *and everything
    under it*, so the outer route sees the site meetings too, claims them first, writes their
    transcripts into the wrong folder, and sixty days later moves the only copy of the audio
    into the wrong archive. Containment is therefore checked where the ids are chosen, at the
    keyboard, against the live tree;

  * **the detector that fires when it happens anyway.** The ledger has always written a
    ``route-disagreement`` event when two routes claim one recording — and nothing read it.
    No command printed it, no digest mentioned it, not even a log line. It is now shouted at
    the point it is written, counted in ``status``, carried in the morning email, and it
    holds the recording back from being archived, because which archive folder it belongs in
    is exactly what is not settled.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from typing import Any, Sequence

from transcriber import archive as archive_module
from transcriber import digest as digest_module
from transcriber import graph as graph_module
from transcriber import routes_cmd
from transcriber import sweep as sweep_module
from transcriber.__main__ import BACKFILL_CURSOR, backfill_cursor_name, _route_status
from transcriber.config import nested_folder_problems
from transcriber.ledger import SCHEMA_VERSION, Ledger
from transcriber.models import DriveItem, Route, State
from transcriber.setup_wizard import _Folders, route_problems

from . import support


CALLS = Route(
    name="calls", label="Phone calls",
    source_folder_id="S-CALLS", output_folder_id="O-CALLS", archive_folder_id="A-CALLS",
)
SITE = Route(
    name="site-meetings", label="Site meetings",
    source_folder_id="S-SITE", output_folder_id="O-SITE", archive_folder_id="A-SITE",
)


def tree(**parents: str) -> Any:
    """``ancestors_of`` for a drive whose shape is written out as child -> parent."""
    def ancestors_of(folder_id: str) -> tuple[str, ...]:
        chain: list[str] = []
        current = folder_id
        while current in parents:
            current = parents[current]
            chain.append(current)
        return tuple(chain)
    return ancestors_of


# --------------------------------------------------------------------------------------
# one route's folder inside another's
# --------------------------------------------------------------------------------------


class ASourceFolderInsideAnotherSourceFolderIsRefused(unittest.TestCase):
    """The transfer of ownership no comparison of ids can see."""

    def test_the_nested_pair_is_refused_and_both_routes_are_named(self) -> None:
        found = nested_folder_problems([CALLS, SITE], tree(**{"S-SITE": "S-CALLS"}))

        self.assertEqual(len(found), 1, found)
        self.assertIn("calls", found[0])
        self.assertIn("site-meetings", found[0])
        self.assertIn("inside", found[0])

    def test_it_reads_the_same_whichever_way_round_the_routes_are_listed(self) -> None:
        first = nested_folder_problems([CALLS, SITE], tree(**{"S-SITE": "S-CALLS"}))
        second = nested_folder_problems([SITE, CALLS], tree(**{"S-SITE": "S-CALLS"}))

        self.assertEqual(len(second), 1, second)
        self.assertEqual(first[0], second[0])

    def test_a_folder_several_levels_down_is_caught_too(self) -> None:
        """Graph's delta is a subtree feed, so depth changes nothing about who sees what."""
        found = nested_folder_problems(
            [CALLS, SITE], tree(**{"S-SITE": "2026", "2026": "S-CALLS"})
        )

        self.assertEqual(len(found), 1, found)

    def test_two_folders_side_by_side_are_fine(self) -> None:
        found = nested_folder_problems(
            [CALLS, SITE], tree(**{"S-CALLS": "ROOT", "S-SITE": "ROOT"})
        )

        self.assertEqual(found, [])

    def test_a_paused_route_is_not_dragged_into_it(self) -> None:
        """A paused route's folder is not enumerated, so it cannot claim anything."""
        found = nested_folder_problems(
            [CALLS, replace(SITE, enabled=False)], tree(**{"S-SITE": "S-CALLS"})
        )

        self.assertEqual(found, [])

    def test_an_archive_folder_inside_a_watched_folder_is_refused(self) -> None:
        found = nested_folder_problems([CALLS], tree(**{"A-CALLS": "S-CALLS"}))

        self.assertEqual(len(found), 1, found)
        self.assertIn("archive", found[0].lower())

    def test_a_drive_that_cannot_be_asked_reports_nothing(self) -> None:
        """A refusal invented out of a Graph call that failed is worse than the bug."""
        def unanswerable(_folder_id: str) -> tuple[str, ...]:
            raise TimeoutError("the drive could not be read")

        self.assertEqual(nested_folder_problems([CALLS, SITE], unanswerable), [])
        self.assertEqual(nested_folder_problems([CALLS, SITE], lambda _f: ()), [])

    def test_pooling_two_routes_into_one_output_folder_survives_the_new_check(self) -> None:
        """He asked for pooling by name. A containment rule must not take it away."""
        pooled = replace(SITE, output_folder_id="O-CALLS")

        found = nested_folder_problems(
            [CALLS, pooled], tree(**{"S-CALLS": "ROOT", "S-SITE": "ROOT", "O-CALLS": "ROOT"})
        )

        self.assertEqual(found, [])

    def test_the_wizard_asks_the_same_question_when_a_drive_is_at_hand(self) -> None:
        found = route_problems([CALLS, SITE], tree(**{"S-SITE": "S-CALLS"}))

        self.assertTrue(any("inside" in p for p in found), found)
        # And without a drive it is simply not asked, rather than guessed at.
        self.assertEqual(route_problems([CALLS, SITE]), [])


class _FakeTree:
    """A drive that answers ``get_item`` with a parent, or refuses to."""

    def __init__(self, parents: dict[str, str], broken: bool = False) -> None:
        self.parents = dict(parents)
        self.broken = broken
        self.asked: list[str] = []

    def get_item(self, item_id: str) -> Any:
        self.asked.append(item_id)
        if self.broken:
            raise TimeoutError("the drive could not be read")
        return type("Item", (), {
            "id": item_id, "name": item_id, "parent_id": self.parents.get(item_id, ""),
        })()


class TheFolderPickersCanWalkUpTheTree(unittest.TestCase):
    """Both places a folder id is chosen have to be able to answer "what contains this?"."""

    def test_the_wizard_walks_the_whole_chain_nearest_first(self) -> None:
        folders = _Folders(_FakeTree({"S-SITE": "S-CALLS", "S-CALLS": "ROOT"}))

        self.assertEqual(folders.ancestors("S-SITE"), ("S-CALLS", "ROOT"))

    def test_the_routes_command_walks_it_too(self) -> None:
        drive = routes_cmd._Drive(_FakeTree({"S-SITE": "S-CALLS", "S-CALLS": "ROOT"}))

        self.assertEqual(drive.ancestors("S-SITE"), ("S-CALLS", "ROOT"))

    def test_a_drive_that_will_not_answer_gives_an_empty_chain(self) -> None:
        folders = _Folders(_FakeTree({}, broken=True))
        drive = routes_cmd._Drive(_FakeTree({}, broken=True))

        self.assertEqual(folders.ancestors("S-SITE"), ())
        self.assertEqual(drive.ancestors("S-SITE"), ())

    def test_it_is_asked_once_per_folder(self) -> None:
        client = _FakeTree({"S-SITE": "S-CALLS", "S-CALLS": "ROOT"})
        folders = _Folders(client)

        folders.ancestors("S-SITE")
        folders.ancestors("S-SITE")

        self.assertEqual(client.asked, ["S-SITE", "S-CALLS", "ROOT"])

    def test_offline_the_answer_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(_Folders(None).ancestors("S-SITE"), ())
        self.assertEqual(routes_cmd._Drive(None).ancestors("S-SITE"), ())


# --------------------------------------------------------------------------------------
# the detector that nothing read
# --------------------------------------------------------------------------------------


class _Ledgered(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "ledger.sqlite3")
        self.ledger = Ledger(self.path)
        self.addCleanup(self.ledger.close)

    def _seen_by_both(self, item_id: str = "SITE-1") -> None:
        """One recording enumerated by ``calls`` first and then by ``site-meetings``."""
        item = DriveItem(item_id=item_id, name="BEACH COURT SITE WALK 270826.m4a",
                         size=4096, parent_id="S-SITE", created_at="2026-08-27T09:00:00Z")
        self.ledger.record_page([item], "calls-1", route="calls")
        self.ledger.record_page([item], "site-1", route="site-meetings")


class ARecordingTwoRoutesClaimedIsReadableByAPerson(_Ledgered):
    def test_the_ledger_can_be_asked_for_them(self) -> None:
        self._seen_by_both()

        found = self.ledger.route_disagreements()

        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["item_id"], "SITE-1")
        self.assertIn("calls", found[0]["detail"])
        self.assertIn("site-meetings", found[0]["detail"])
        self.assertEqual(found[0]["item_name"], "BEACH COURT SITE WALK 270826.m4a")

    def test_both_routes_are_counted_not_only_the_one_it_stayed_on(self) -> None:
        """Which of the two is misconfigured is exactly what this cannot know."""
        self._seen_by_both()

        counts = self.ledger.route_disagreement_counts()

        self.assertEqual(counts, {"calls": 1, "site-meetings": 1})

    def test_a_service_with_nothing_wrong_reports_nothing(self) -> None:
        self.ledger.record_page(
            [DriveItem(item_id="C1", name="Call Carel_260827_120055.m4a")],
            "calls-1", route="calls",
        )

        self.assertEqual(self.ledger.route_disagreements(), [])
        self.assertEqual(self.ledger.route_disagreement_counts(), {})

    def test_status_shows_the_count_against_both_routes(self) -> None:
        self._seen_by_both()
        config = support.make_config(routes=(CALLS, SITE), work_dir=self.dir.name)

        rows = {r["route"]: r for r in _route_status(config, self.ledger, self.ledger.stats())}

        self.assertEqual(rows["calls"]["route_disagreements"], 1)
        self.assertEqual(rows["site-meetings"]["route_disagreements"], 1)

    def test_the_morning_email_says_it_and_asks_for_a_person(self) -> None:
        self._seen_by_both()
        day = self.ledger.get("SITE-1").discovered_at[:10]
        config = support.make_config(routes=(CALLS, SITE), work_dir=self.dir.name)

        built = digest_module.build(config, self.ledger, day=day)

        self.assertTrue(built.route_disagreements)
        self.assertIn("TWO ROUTES CLAIMED THE SAME RECORDING", built.body)
        self.assertIn("BEACH COURT SITE WALK 270826.m4a", built.body)
        self.assertTrue(built.needs_a_person)

    def test_it_is_shouted_at_the_moment_it_is_written(self) -> None:
        """The log file is the one place a running service can say something immediately."""
        import logging

        logger = logging.getLogger("transcriber.ledger")
        records: list[logging.LogRecord] = []

        class _Catch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Catch()
        logger.addHandler(handler)
        was_disabled, logger.disabled = logger.disabled, False
        old_level = logger.level
        logger.setLevel(logging.ERROR)
        try:
            self._seen_by_both()
        finally:
            logger.removeHandler(handler)
            logger.disabled = was_disabled
            logger.setLevel(old_level)

        shouted = [r for r in records if r.levelno >= logging.ERROR]
        self.assertTrue(shouted, "the disagreement reached no log line at all")
        text = " ".join(r.getMessage() for r in shouted)
        self.assertIn("calls", text)
        self.assertIn("site-meetings", text)


class ADisputedRecordingIsNotArchived(_Ledgered):
    """Moving the only copy of the audio on the strength of a route that is in doubt."""

    class _Drive:
        def __init__(self) -> None:
            self.moves: list[tuple[str, str]] = []

        def get_item(self, item_id: str) -> Any:
            return type("Item", (), {
                "id": item_id, "name": f"{item_id}.m4a", "size": 4096,
                "parent_id": "S-SITE", "is_deleted": False, "is_folder": False,
            })()

        def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
            self.moves.append((item_id, parent_id))
            return self.get_item(item_id)

    def _finish(self, item_id: str) -> None:
        for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
            self.ledger.advance(item_id, state)
        self.ledger.advance(
            item_id, State.DONE,
            transcript_name=f"{item_id}-transcript.md",
            summary_name=f"{item_id}-summary.md",
            actions_name=f"{item_id}-actions.md",
            output_item_ids={"transcript": "t", "summary": "s", "actions": "a"},
            done_at="2020-01-01T09:30:00Z",
        )

    def test_it_is_held_back_and_the_reason_names_the_two_routes(self) -> None:
        item = DriveItem(item_id="SITE-1", name="BEACH COURT SITE WALK.m4a", size=4096,
                         parent_id="S-SITE", created_at="2020-01-01T09:00:00Z")
        self.ledger.record_page([item], "calls-1", route="calls")
        self.ledger.record_page([item], "site-1", route="site-meetings")
        self._finish("SITE-1")
        config = support.make_config(routes=(CALLS, SITE), work_dir=self.dir.name,
                                     archive_age_days=60)
        drive = self._Drive()

        run = archive_module.archive(config, self.ledger, drive, now=1_800_000_000.0)

        self.assertEqual(drive.moves, [], "a disputed recording was moved anyway")
        held = [o for o in run.outcomes if o.result == archive_module.HELD_BACK]
        self.assertTrue(held, run.render())
        self.assertIn("two routes", held[0].detail.lower())
        self.assertIsNone(self.ledger.get("SITE-1").archived_at)


# --------------------------------------------------------------------------------------
# paused is not the same as gone
# --------------------------------------------------------------------------------------


class APausedRouteIsNotAnOrphan(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.ledger.record_page(
            [DriveItem(item_id="W1", name="PTT-20260827-WA0003.opus", size=2048)],
            "wa-1", route="whatsapp",
        )
        self.run = sweep_module.SweepRun(started_at="2026-08-28T00:00:00Z")

    def _report(self, *routes: Route) -> list[sweep_module.SweepFinding]:
        config = support.make_config(routes=tuple(routes), work_dir=self.dir.name)
        sweep_module._report_unswept_routes(self.ledger, self.run, config=config, swept=set())
        return list(self.run.findings)

    def test_a_paused_route_is_reported_as_paused_and_not_as_missing(self) -> None:
        paused = Route(name="whatsapp", label="WhatsApp voice notes",
                       source_folder_id="S-WA", output_folder_id="O-WA", enabled=False)

        findings = self._report(CALLS, paused)

        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].kind, "paused-route")
        self.assertIn("switched off", findings[0].detail)
        self.assertNotIn("not in the configuration any more", findings[0].detail)
        self.assertIn("routes enable whatsapp", findings[0].detail)

    def test_it_does_not_claim_the_recordings_are_going_nowhere(self) -> None:
        """The drain is not filtered by route, so those rows are still being worked."""
        paused = Route(name="whatsapp", source_folder_id="S-WA", output_folder_id="O-WA",
                       enabled=False)

        findings = self._report(CALLS, paused)

        self.assertNotIn("nothing will pick them up", findings[0].detail)
        self.assertIn("still being worked", findings[0].detail)

    def test_a_route_that_really_is_gone_is_still_reported_and_needs_a_person(self) -> None:
        findings = self._report(CALLS)

        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].kind, "unwatched-route")
        self.assertIn("not in the configuration any more", findings[0].detail)
        self.assertTrue(findings[0].needs_a_person)

    def test_and_it_says_those_rows_are_stopped_rather_than_left_alone(self) -> None:
        """They are picked up and quarantined, which is the opposite of untouched."""
        findings = self._report(CALLS)

        self.assertIn("stopped for you", findings[0].detail)


class TheArchivePassSaysWhyAPausedRouteWasNotArchived(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.ledger.record_page(
            [DriveItem(item_id="W1", name="PTT-20260827-WA0003.opus", size=2048,
                       created_at="2020-01-01T09:00:00Z")],
            "wa-1", route="whatsapp",
        )
        for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
            self.ledger.advance("W1", state)
        self.ledger.advance(
            "W1", State.DONE, transcript_name="t.md", summary_name="s.md",
            actions_name="a.md", output_item_ids={"transcript": "t"},
            done_at="2020-01-01T09:30:00Z",
        )
        self.run = archive_module.ArchiveRun(started_at="2026-08-28T00:00:00Z", age_days=60)

    def _note(self, *routes: Route) -> str:
        config = support.make_config(routes=tuple(routes), work_dir=self.dir.name)
        archive_module._note_unarchived_routes(
            self.ledger, self.run, days=60, clock=1_800_000_000.0, config=config, passed=set(),
        )
        return "\n".join(self.run.notes)

    def test_a_paused_route_with_an_archive_folder_is_not_told_it_has_none(self) -> None:
        paused = Route(name="whatsapp", source_folder_id="S-WA", output_folder_id="O-WA",
                       archive_folder_id="A-WA", enabled=False)

        note = self._note(CALLS, paused)

        self.assertIn("switched off", note)
        self.assertIn("It has an archive folder", note)
        self.assertNotIn("not in the configuration any more", note)
        self.assertIn("routes enable whatsapp", note)

    def test_a_route_that_is_gone_still_reads_as_gone(self) -> None:
        note = self._note(CALLS)

        self.assertIn("not in the configuration any more", note)
        self.assertIn("no archive folder to move them to", note)


# --------------------------------------------------------------------------------------
# the move that touches the only copy
# --------------------------------------------------------------------------------------


class TheMoveStatesWhatToDoAboutACollision(unittest.TestCase):
    class _Client(graph_module.GraphClient):
        def __init__(self) -> None:
            super().__init__(tenant_id="t", client_id="c", client_secret="s", user_id="u")
            self.bodies: list[dict[str, Any]] = []

        def _request(self, method: str, url: str, *, body: bytes | None = None, **kw: Any) -> Any:
            self.bodies.append(json.loads((body or b"{}").decode("utf-8")))
            return type("R", (), {"json": staticmethod(lambda: {
                "id": "C1", "name": "Call Carel.m4a", "parentReference": {"id": "A-CALLS"},
            })})()

    def test_the_move_never_leaves_the_collision_rule_to_a_default(self) -> None:
        client = self._Client()

        client.move("C1", "A-CALLS")

        self.assertEqual(client.bodies[0]["parentReference"], {"id": "A-CALLS"})
        self.assertEqual(client.bodies[0]["@microsoft.graph.conflictBehavior"], "fail")


class ANameAlreadyInTheArchiveIsReportedAsItself(unittest.TestCase):
    class _Drive:
        """Every move is refused the way Graph refuses one: 409, name already there."""

        def __init__(self) -> None:
            self.attempts = 0

        def get_item(self, item_id: str) -> Any:
            return type("Item", (), {
                "id": item_id, "name": f"{item_id}.m4a", "size": 4096,
                "parent_id": "S-CALLS", "is_deleted": False, "is_folder": False,
            })()

        def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
            self.attempts += 1
            raise graph_module.GraphHTTPError(
                409, method="PATCH", url="https://graph.invalid/items/x",
                code="nameAlreadyExists",
            )

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        for index in range(7):
            item_id = f"C{index}"
            self.ledger.record_page(
                [DriveItem(item_id=item_id, name="Call Carel_260827_143005.m4a", size=4096,
                           parent_id="S-CALLS", created_at="2020-01-01T09:00:00Z")],
                f"calls-{index}", route="calls",
            )
            for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
                self.ledger.advance(item_id, state)
            self.ledger.advance(
                item_id, State.DONE, transcript_name="t.md", summary_name="s.md",
                actions_name="a.md",
                output_item_ids={"transcript": "t", "summary": "s", "actions": "a"},
                done_at="2020-01-01T09:30:00Z",
            )
        self.config = support.make_config(routes=(CALLS,), work_dir=self.dir.name,
                                          archive_age_days=60)

    def test_it_is_reported_in_his_words_and_nothing_is_recorded_as_archived(self) -> None:
        drive = self._Drive()

        run = archive_module.archive(self.config, self.ledger, drive, now=1_800_000_000.0)

        failed = [o for o in run.outcomes if o.result == archive_module.FAILED]
        self.assertEqual(len(failed), 7, run.render())
        self.assertIn("already in the archive folder", failed[0].detail)
        self.assertIsNone(self.ledger.get("C0").archived_at)

    def test_it_is_not_folded_into_the_stop_about_hammering_onedrive(self) -> None:
        """Seven name clashes are seven filing problems, not a drive that is falling over."""
        drive = self._Drive()

        run = archive_module.archive(self.config, self.ledger, drive, now=1_800_000_000.0)

        self.assertEqual(drive.attempts, 7, "the pass stopped early on a per-file problem")
        for report in run.reports:
            self.assertEqual(report.stopped_early, "")


# --------------------------------------------------------------------------------------
# the cursor namespace, and the row read twice
# --------------------------------------------------------------------------------------


class TheBackfillLaneCannotShareARoutesCursor(unittest.TestCase):
    def test_no_route_name_can_collide_with_the_backfill_cursor(self) -> None:
        from transcriber.ledger import delta_cursor_name

        self.assertNotEqual(backfill_cursor_name("default"), delta_cursor_name("backfill"))
        self.assertEqual(backfill_cursor_name("default"), "delta:backfill:default")
        self.assertEqual(delta_cursor_name("backfill"), "delta:backfill")

    def test_every_route_gets_its_own_backfill_cursor(self) -> None:
        names = {backfill_cursor_name(r) for r in ("default", "calls", "backfill")}

        self.assertEqual(len(names), 3, names)

    def test_a_half_finished_backfill_is_carried_across_by_the_migration(self) -> None:
        """Dropping it would make an upgraded install walk its whole history again."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "ledger.sqlite3")
        with Ledger(path) as ledger:
            ledger.record_page(
                [DriveItem(item_id="C1", name="Call Carel_260827_120055.m4a")],
                "half-way-through", cursor_name=BACKFILL_CURSOR,
            )
            conn = ledger._conn()
            # Wind the database back to v2 properly, rather than relabelling a current one.
            #
            # Deleting only the v3 row worked while v3 was the newest migration, and stopped
            # working the moment a v4 existed: migrate() compares against MAX(version), so a
            # gap in the middle leaves the maximum untouched and the migration under test
            # never re-runs. Deleting the rows from 3 up fixes that half — and then the
            # columns v4 added are still there, so re-running it fails on a duplicate column.
            # A v2 database does not have them, so neither does this one.
            conn.execute("DELETE FROM schema_version WHERE version>=3")
            conn.execute("DROP INDEX IF EXISTS idx_items_erased")
            for column in ("erased_at", "erased_by", "erased_because"):
                conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
            conn.execute(
                "UPDATE OR REPLACE cursors SET name=? WHERE name=?",
                (BACKFILL_CURSOR, f"{BACKFILL_CURSOR}:default"),
            )

        with Ledger(path) as upgraded:
            # The constant, not a literal: this test is about a half-finished backfill
            # surviving the upgrade, and pinning the number here made it fail every
            # time a later migration was added, for a reason it does not care about.
            self.assertEqual(upgraded.schema_version(), SCHEMA_VERSION)
            self.assertEqual(
                upgraded.cursor_get(backfill_cursor_name("default")), "half-way-through"
            )
            self.assertIsNone(upgraded.cursor_get(BACKFILL_CURSOR))


class TheArchivePassChecksTheRowItIsAboutToMove(unittest.TestCase):
    """A candidate list is a snapshot; a move is not."""

    class _Drive:
        def __init__(self, on_confirm) -> None:
            self.on_confirm = on_confirm
            self.moves: list[tuple[str, str]] = []

        def get_item(self, item_id: str) -> Any:
            self.on_confirm(item_id)
            return type("Item", (), {
                "id": item_id, "name": f"{item_id}.m4a", "size": 4096,
                "parent_id": "S-CALLS", "is_deleted": False, "is_folder": False,
            })()

        def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> Any:
            self.moves.append((item_id, parent_id))
            return self.get_item(item_id)

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        for item_id in ("C1", "C2"):
            self.ledger.record_page(
                [DriveItem(item_id=item_id, name=f"{item_id}.m4a", size=4096,
                           parent_id="S-CALLS", created_at="2020-01-01T09:00:00Z")],
                f"calls-{item_id}", route="calls",
            )
            for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
                self.ledger.advance(item_id, state)
            self.ledger.advance(
                item_id, State.DONE, transcript_name="t.md", summary_name="s.md",
                actions_name="a.md",
                output_item_ids={"transcript": "t", "summary": "s", "actions": "a"},
                done_at="2020-01-01T09:30:00Z",
            )
        self.config = support.make_config(routes=(CALLS,), work_dir=self.dir.name,
                                          archive_age_days=60)

    def test_a_recording_requeued_mid_pass_is_not_moved_out_from_under_the_worker(self) -> None:
        def requeue_c2_once(item_id: str) -> None:
            row = self.ledger.get("C2")
            if row is not None and row.state == State.DONE:
                self.ledger.requeue("C2", "somebody asked for it again")

        drive = self._Drive(requeue_c2_once)

        run = archive_module.archive(self.config, self.ledger, drive, now=1_800_000_000.0)

        moved = [item for item, _folder in drive.moves]
        self.assertIn("C1", moved)
        self.assertNotIn("C2", moved, "a re-queued recording was archived mid-download")
        self.assertIsNone(self.ledger.get("C2").archived_at)
        held = [o for o in run.outcomes if o.result == archive_module.HELD_BACK]
        self.assertTrue(any("not finished" in o.detail for o in held), run.render())


# --------------------------------------------------------------------------------------
# removing a route, and what it costs
# --------------------------------------------------------------------------------------


class RemovingARouteSaysWhatHappensToUnfinishedWork(unittest.TestCase):
    """"The history is kept" is true and was being read as "nothing is lost"."""

    def setUp(self) -> None:
        from .test_routes_commands import ENV, _Answers
        from transcriber.setup_wizard import write_env_file

        self.answers = _Answers
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env_path = os.path.join(self.dir.name, ".env")
        values = dict(ENV)
        self.ledger_path = os.path.join(self.dir.name, "ledger.sqlite3")
        values["LEDGER_PATH"] = self.ledger_path
        write_env_file(self.env_path, values)
        with Ledger(self.ledger_path) as ledger:
            ledger.record_page(
                [DriveItem(item_id="M1", name="BEACH COURT SITE WALK.m4a", size=2048),
                 DriveItem(item_id="M2", name="ERF 91 WALKTHROUGH.m4a", size=2048)],
                "site-1", route="site-meetings",
            )
            ledger.advance("M2", State.CLAIMED)
        self.out = io.StringIO()

    def _remove(self, slug: str, answers: list[str]) -> int:
        args = argparse.Namespace(action="remove", slug=slug, env=self.env_path,
                                  offline=True, yes=False)
        original = builtins.input
        builtins.input = self.answers(answers)
        try:
            with contextlib.redirect_stdout(self.out):
                return routes_cmd.run(args, self.out)
        finally:
            builtins.input = original

    def test_the_count_still_in_flight_is_printed_before_the_prompt(self) -> None:
        self._remove("site-meetings", ["n"])

        printed = self.out.getvalue()
        self.assertIn("2 of those recordings have not finished", printed)
        self.assertIn("stopped for you", printed)
        self.assertIn("routes disable site-meetings", printed)

    def test_it_comes_before_the_question_not_after_the_answer(self) -> None:
        self._remove("site-meetings", ["n"])

        printed = self.out.getvalue()
        self.assertLess(
            printed.index("have not finished"), printed.index("Nothing was written"),
            "the warning must be readable while the answer is still open",
        )

    def test_a_route_whose_work_is_all_finished_says_nothing_alarming(self) -> None:
        with Ledger(self.ledger_path) as ledger:
            for item_id in ("M1", "M2"):
                row = ledger.get(item_id)
                for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED,
                              State.ANALYSED, State.DONE):
                    if row is not None and state == row.state:
                        continue
                    ledger.advance(item_id, state)

        self._remove("site-meetings", ["n"])

        self.assertNotIn("have not finished", self.out.getvalue())
