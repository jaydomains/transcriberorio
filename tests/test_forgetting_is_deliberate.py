"""Forgetting a recording: dry by default, named always, honest about its own reach.

Everywhere else this service never deletes — ``archive.py`` says so in its first rule and
there is no delete call in that file. This is the deliberate exception, and the tests below
are the ones that keep it from becoming a hazard.

The dangerous shapes, all four tested:

  * a `forget` that quietly matches everything;
  * a `forget` that removes some of what was asked and reports that it removed all of it;
  * a `forget` that empties the row before the files, losing the only thing that knew what
    was still out there;
  * a `forget` that leaves a person's words somewhere nobody thought to look.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from transcriber import erase as erase_module
from transcriber.ledger import Ledger, LedgerError
from transcriber.models import DriveItem, Row, State


class _Drive:
    """A drive that deletes, and can be told to refuse one particular id."""

    def __init__(self, refuse: str = "", missing: tuple = ()):
        self.deleted: list[str] = []
        self.refuse = refuse
        self.missing = set(missing)

    def delete(self, item_id: str) -> bool:
        if item_id == self.refuse:
            raise RuntimeError("the drive said no")
        if item_id in self.missing:
            return False
        self.deleted.append(item_id)
        return True


class _Holds:
    def __init__(self):
        self.forgotten: list[str] = []

    def forget(self, item_id: str) -> int:
        self.forgotten.append(item_id)
        return 2


def _ledger(tmp: str) -> Ledger:
    led = Ledger(os.path.join(tmp, "ledger.sqlite"))
    led.migrate()
    return led


def _finished(led: Ledger, item_id: str, name: str, route: str = "james") -> None:
    led.upsert_discovered(DriveItem(item_id=item_id, name=name, size=10,
                                    created_at="2026-08-27T09:00:00Z"), route=route)
    for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
        led.advance(item_id, state)
    led.advance(item_id, State.DONE,
                transcript_name=f"{name}.transcript.md",
                summary_name=f"_{name}.summary.md",
                actions_name=f"_{name}.actions.md",
                # A dict, the way the pipeline passes it: the ledger JSON-encodes this
                # column itself, so handing it a pre-encoded string stores nothing.
                output_item_ids={"transcript": f"{item_id}-t",
                                 "summary": f"{item_id}-s",
                                 "actions": f"{item_id}-a"})


class AnErasureNeedsAPersonAndAReason(unittest.TestCase):
    def test_the_ledger_refuses_an_erasure_with_no_name_on_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                for by, because in (("", "they asked"), ("  ", "they asked"),
                                    ("James", ""), ("James", "   ")):
                    with self.assertRaises(LedgerError):
                        led.erase("a", by=by, because=because)
                self.assertEqual(led.get("a").state, State.DONE)

    def test_the_module_refuses_the_same(self) -> None:
        plan = erase_module.ErasePlan(candidates=())
        with self.assertRaises(ValueError):
            erase_module.erase(None, plan, by="", because="they asked")


class TheRowBecomesATombstone(unittest.TestCase):
    def test_what_described_the_recording_is_gone_and_the_fact_of_it_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Carel dismissal call.m4a")
                led.erase("a", by="James Janeke", because="Carel asked us to remove it")
                row = led.get("a")

        self.assertEqual(row.state, State.ERASED)
        # Gone.
        self.assertFalse(row.name)
        self.assertFalse(row.transcript_name)
        self.assertFalse(row.summary_name)
        self.assertFalse(row.actions_name)
        self.assertIn(row.output_item_ids, ({}, "{}", None))
        # Kept — the record of the thing, not the thing.
        self.assertEqual(row.item_id, "a")
        self.assertEqual(row.route, "james")
        self.assertEqual(row.erased_by, "James Janeke")
        self.assertIn("Carel asked", row.erased_because)
        self.assertTrue(row.erased_at)
        self.assertTrue(row.created_at)

    def test_the_history_says_who_and_why_and_not_what(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Carel dismissal call.m4a")
                led.erase("a", by="James", because="Carel asked")
                blob = json.dumps(led.history("a"))
        self.assertIn("James", blob)
        self.assertNotIn("dismissal", blob)
        self.assertNotIn("Carel dismissal call.m4a", blob)

    def test_erasing_twice_is_a_re_run_and_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                led.erase("a", by="James", because="asked")
                led.erase("a", by="Somebody Else", because="asked again")
                # The first erasure's attribution stands: the second changed nothing.
                self.assertEqual(led.get("a").erased_by, "James")

    def test_a_done_row_can_be_erased_even_though_advance_refuses_to_move_it(self) -> None:
        """The one deliberate way backwards, and only through erase()."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                with self.assertRaises(Exception):
                    led.advance("a", State.DISCOVERED)
                led.erase("a", by="James", because="asked")
                self.assertEqual(led.get("a").state, State.ERASED)


class NoColumnEscapesTheErasure(unittest.TestCase):
    def test_every_column_is_either_cleared_or_deliberately_kept(self) -> None:
        """The test that makes a column added next year get thought about at the time."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                missed = erase_module.columns_not_covered(led)
        self.assertEqual(missed, (), f"these columns hold content nothing clears: {missed}")


class TheFilesGoBeforeTheRow(unittest.TestCase):
    def test_a_refused_delete_leaves_the_row_alone_so_it_can_be_re_run(self) -> None:
        """The row is the only thing that knows which files to delete."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                plan = erase_module.plan(led, rows=[led.get("a")])
                result = erase_module.erase(led, plan, by="James", because="asked",
                                            client=_Drive(refuse="a-s"))
                row = led.get("a")

        self.assertFalse(result.ok)
        self.assertTrue(result.files_refused)
        self.assertEqual(result.recordings, 0)
        # Still DONE, still naming its files. Nothing is half-removed.
        self.assertEqual(row.state, State.DONE)
        self.assertTrue(row.transcript_name)

    def test_a_file_already_gone_is_the_requested_state_not_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                plan = erase_module.plan(led, rows=[led.get("a")])
                result = erase_module.erase(led, plan, by="James", because="asked",
                                            client=_Drive(missing=("a-a",)))
                self.assertTrue(result.ok)
                self.assertEqual(result.files_already_gone, 1)
                self.assertEqual(led.get("a").state, State.ERASED)

    def test_the_source_and_all_three_outputs_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                drive = _Drive()
                plan = erase_module.plan(led, rows=[led.get("a")])
                erase_module.erase(led, plan, by="James", because="asked", client=drive)
        self.assertEqual(sorted(drive.deleted), ["a", "a-a", "a-s", "a-t"])

    def test_held_passages_are_forgotten_too(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                holds = _Holds()
                plan = erase_module.plan(led, rows=[led.get("a")])
                result = erase_module.erase(led, plan, by="James", because="asked",
                                            client=_Drive(), held_store=holds)
        self.assertEqual(holds.forgotten, ["a"])
        self.assertEqual(result.held_forgotten, 2)


class ThePlanTouchesNothing(unittest.TestCase):
    def test_building_a_plan_changes_no_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                before = led.get("a")
                erase_module.plan(led, rows=[before])
                after = led.get("a")
        self.assertEqual(after.state, before.state)
        self.assertEqual(after.name, before.name)

    def test_an_already_erased_recording_is_not_a_candidate_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "a", "Beach Court.m4a")
                led.erase("a", by="James", because="asked")
                plan = erase_module.plan(led, rows=[led.get("a")])
        self.assertEqual(plan.recordings, 0)

    def test_an_output_with_no_id_is_named_rather_than_counted_as_erased(self) -> None:
        """A published file this service cannot delete must not read as deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                led.upsert_discovered(DriveItem(item_id="a", name="Beach.m4a", size=1))
                for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED):
                    led.advance("a", state)
                led.advance("a", State.DONE, transcript_name="Beach.transcript.md",
                            summary_name="_Beach.summary.md",
                            output_item_ids={"transcript": "a-t"})
                plan = erase_module.plan(led, rows=[led.get("a")])
        self.assertIn("_Beach.summary.md", plan.unreachable_outputs)


class TheSearchIsNotCapped(unittest.TestCase):
    def test_a_name_search_for_forgetting_returns_every_match(self) -> None:
        """Removing the newest twenty of somebody's two hundred and reporting success is
        the way this feature would lie."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                for i in range(45):
                    led.upsert_discovered(
                        DriveItem(item_id=f"i{i}", name=f"Beach Court {i}.m4a", size=1))
                self.assertEqual(len(led.find_by_name("Beach")), 20)
                self.assertEqual(len(led.find_by_name("Beach", limit=None)), 45)

    def test_a_route_selection_includes_the_failed_ones(self) -> None:
        """Somebody who asked to be forgotten does not care which of theirs failed."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _finished(led, "done", "Beach Court.m4a", route="james")
                led.upsert_discovered(DriveItem(item_id="bad", name="Broken.m4a", size=1),
                                      route="james")
                led.quarantine("bad", "three engine failures")
                led.upsert_discovered(DriveItem(item_id="other", name="Someone else.m4a",
                                                size=1), route="nomsa")
                rows = led.rows_in_route("james")
        self.assertEqual(sorted(r.item_id for r in rows), ["bad", "done"])


if __name__ == "__main__":
    unittest.main()
