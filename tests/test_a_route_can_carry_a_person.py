"""Routes were built to carry KINDS of recording. They are now also carrying PEOPLE.

That is not a rename. Three of the route rules were written when a route meant "phone
calls" or "site meetings", where the worst case of getting it wrong is untidy filing:

  * two routes may pool into one output folder, deliberately, and said nothing about it;
  * a recording both routes claimed was published to whichever route saw it FIRST, and
    reported in the morning email afterwards;
  * the check that catches one watched folder sitting inside another ran in the setup
    wizard and nowhere else — never once the service was running.

When a route is a person, all three of those are the same event: one person's conversation
ending up where a colleague reads it. None of them is fixed by reporting it afterwards,
because nothing takes a transcript back out of somebody's folder once it is there.

What must NOT change, and is asserted here as loudly as the fixes: pooling is still legal.
He asked for it by name. A validation that forbade it on grounds of tidiness would be this
suite's fault, so the notice is a notice and never a refusal.
"""

from __future__ import annotations

import unittest
from unittest import mock

from transcriber.config import Config, ConfigError, nested_folder_problems
from transcriber.graph import ancestor_ids
from transcriber.models import DriveItem, Route, State
from transcriber.ledger import Ledger

from tests.test_routes_config import env, problems_from


# --------------------------------------------------------------------- pooled outputs


def notices_from(**overrides: str) -> tuple[str, ...]:
    return tuple(Config.from_env(env(**overrides)).notices)


class PoolingStaysLegalAndStopsBeingSilent(unittest.TestCase):
    def test_two_kinds_of_recording_may_still_pool_with_nothing_said(self) -> None:
        """The original allowance, unchanged. Same reviewer means one person's filing."""
        notices = notices_from(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="POOL",
            ROUTE_SITE_MEETINGS_SOURCE="S-SITE", ROUTE_SITE_MEETINGS_OUTPUT="POOL",
        )
        self.assertEqual([n for n in notices if "SAME output folder" in n], [])

    def test_two_people_pooling_is_still_allowed_but_no_longer_silent(self) -> None:
        overrides = dict(
            ROUTES="carel,danie",
            ROUTE_CAREL_SOURCE="S-CAREL", ROUTE_CAREL_OUTPUT="POOL",
            ROUTE_CAREL_REVIEWER="carel@kbc.invalid",
            ROUTE_DANIE_SOURCE="S-DANIE", ROUTE_DANIE_OUTPUT="POOL",
            ROUTE_DANIE_REVIEWER="danie@kbc.invalid",
        )
        # Allowed: it still starts.
        self.assertEqual(problems_from(**overrides), [])
        said = [n for n in notices_from(**overrides) if "SAME output folder" in n]
        self.assertEqual(len(said), 1, said)
        self.assertIn("carel", said[0])
        self.assertIn("danie", said[0])
        self.assertIn("separate output folders", said[0])

    def test_a_named_person_pooling_with_an_unnamed_route_is_two_people(self) -> None:
        """No reviewer named means the service owner reviews it — who is a person too."""
        said = [n for n in notices_from(
            ROUTES="carel,mine",
            ROUTE_CAREL_SOURCE="S-CAREL", ROUTE_CAREL_OUTPUT="POOL",
            ROUTE_CAREL_REVIEWER="carel@kbc.invalid",
            ROUTE_MINE_SOURCE="S-MINE", ROUTE_MINE_OUTPUT="POOL",
        ) if "SAME output folder" in n]
        self.assertEqual(len(said), 1, said)

    def test_separate_folders_say_nothing_however_many_people(self) -> None:
        self.assertEqual([n for n in notices_from(
            ROUTES="carel,danie",
            ROUTE_CAREL_SOURCE="S-CAREL", ROUTE_CAREL_OUTPUT="O-CAREL",
            ROUTE_CAREL_REVIEWER="carel@kbc.invalid",
            ROUTE_DANIE_SOURCE="S-DANIE", ROUTE_DANIE_OUTPUT="O-DANIE",
            ROUTE_DANIE_REVIEWER="danie@kbc.invalid",
        ) if "SAME output folder" in n], [])

    def test_the_notice_is_actually_logged_and_not_just_returned(self) -> None:
        """A notice nobody sees is worth nothing. Config logs every notice at WARNING on
        every start; this proves this one goes with them rather than sitting in a field."""
        import logging as _logging

        with self.assertLogs("transcriber.config", level=_logging.WARNING) as caught:
            Config.from_env(env(
                ROUTES="carel,danie",
                ROUTE_CAREL_SOURCE="S-CAREL", ROUTE_CAREL_OUTPUT="POOL",
                ROUTE_CAREL_REVIEWER="carel@kbc.invalid",
                ROUTE_DANIE_SOURCE="S-DANIE", ROUTE_DANIE_OUTPUT="POOL",
                ROUTE_DANIE_REVIEWER="danie@kbc.invalid",
            ))
        self.assertTrue(any("SAME output folder" in line for line in caught.output),
                        caught.output)

    def test_a_disabled_route_is_not_pooling_with_anybody(self) -> None:
        self.assertEqual([n for n in notices_from(
            ROUTES="carel,danie",
            ROUTE_CAREL_SOURCE="S-CAREL", ROUTE_CAREL_OUTPUT="POOL",
            ROUTE_CAREL_REVIEWER="carel@kbc.invalid",
            ROUTE_DANIE_SOURCE="S-DANIE", ROUTE_DANIE_OUTPUT="POOL",
            ROUTE_DANIE_REVIEWER="danie@kbc.invalid", ROUTE_DANIE_ENABLED="false",
        ) if "SAME output folder" in n], [])


# ------------------------------------------------------------- the disputed recording


class ARecordingTwoRoutesClaimedIsNotPublished(unittest.TestCase):
    """It used to be published to whichever route saw it first, and reported afterwards.

    Afterwards is too late: nothing takes a transcript back out of somebody's folder.
    """

    def _ledger(self, tmp: str) -> Ledger:
        led = Ledger(f"{tmp}/ledger.sqlite")
        led.migrate()
        return led

    def test_the_ledger_can_be_asked_about_one_recording(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self._ledger(tmp) as led:
                item = DriveItem(item_id="x", name="Call Carel.m4a", size=10,
                                 created_at="2026-09-03T09:00:00Z")
                led.upsert_discovered(item, route="carel")
                self.assertEqual(led.disagreement_about("x"), "")
                led.upsert_discovered(item, route="danie")
                because = led.disagreement_about("x")

        self.assertIn("carel", because)
        self.assertIn("danie", because)

    def test_an_undisputed_recording_is_not_asked_about_twice(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with self._ledger(tmp) as led:
                led.upsert_discovered(DriveItem(item_id="y", name="ok.m4a", size=10,
                                                created_at="2026-09-03T09:00:00Z"),
                                      route="carel")
                self.assertEqual(led.disagreement_about("y"), "")

    def test_the_pipeline_refuses_the_route_of_a_disputed_recording(self) -> None:
        from transcriber import pipeline as P

        route = Route(name="carel", label="Carel", source_folder_id="S", output_folder_id="O")
        row = mock.Mock(item_id="x", route="carel")
        holder = mock.Mock()
        holder.ledger.disagreement_about.return_value = (
            "discovered on route carel, seen again on route danie; it stays on carel"
        )
        with self.assertRaises(P._RouteFault) as caught:
            P.Pipeline._refuse_if_disputed(holder, row, route)

        said = str(caught.exception)
        self.assertIn("two routes have both claimed", said)
        self.assertIn("danie", said)
        # And it says what was NOT done, because that is the reassuring half.
        self.assertIn("Nothing has been written, moved or deleted", said)

    def test_an_undisputed_recording_passes_straight_through(self) -> None:
        from transcriber import pipeline as P

        route = Route(name="carel", source_folder_id="S", output_folder_id="O")
        holder = mock.Mock()
        holder.ledger.disagreement_about.return_value = ""
        self.assertIsNone(
            P.Pipeline._refuse_if_disputed(holder, mock.Mock(item_id="x"), route)
        )

    def test_a_ledger_that_cannot_answer_does_not_stop_the_route(self) -> None:
        """The check is a guard, not a new way for one recording to take a route down."""
        from transcriber import pipeline as P

        route = Route(name="carel", source_folder_id="S", output_folder_id="O")
        holder = mock.Mock()
        holder.ledger.disagreement_about.side_effect = RuntimeError("the ledger is locked")
        self.assertIsNone(
            P.Pipeline._refuse_if_disputed(holder, mock.Mock(item_id="x"), route)
        )

    def test_route_of_asks_before_handing_back_a_route(self) -> None:
        """The refusal has to be on the path everything publishes through, not beside it."""
        import inspect

        from transcriber import pipeline as P

        body = inspect.getsource(P.Pipeline.route_of)
        self.assertIn("_refuse_if_disputed", body)


# ------------------------------------------------------------------- nested folders


class _Tree:
    """A drive that knows only who each folder's parent is."""

    def __init__(self, parents: dict[str, str], *, breaks: bool = False) -> None:
        self.parents = parents
        self.breaks = breaks
        self.asked: list[str] = []

    def get_item(self, item_id: str):
        self.asked.append(item_id)
        if self.breaks:
            raise RuntimeError("the drive will not answer")
        return mock.Mock(parent_id=self.parents.get(item_id, ""))


class OneWatchedFolderInsideAnotherIsCaughtWhileRunning(unittest.TestCase):
    """The check existed and only the setup wizard ever called it.

    A `.env` edited by hand, a folder dragged in OneDrive, or a route added later could
    nest two watched folders and the service would start perfectly clean.
    """

    def test_the_walk_climbs_to_the_root_nearest_first(self) -> None:
        tree = _Tree({"INNER": "OUTER", "OUTER": "ROOT"})
        self.assertEqual(ancestor_ids(tree.get_item, "INNER"), ("OUTER", "ROOT"))

    def test_a_drive_that_will_not_answer_is_not_an_answer(self) -> None:
        """An invented refusal would be worse than the bug it was looking for."""
        tree = _Tree({"INNER": "OUTER"}, breaks=True)
        self.assertEqual(ancestor_ids(tree.get_item, "INNER"), ())

    def test_a_cycle_cannot_hang_the_service(self) -> None:
        tree = _Tree({"A": "B", "B": "A"})
        self.assertEqual(ancestor_ids(tree.get_item, "A"), ("B",))
        self.assertLess(len(tree.asked), 5)

    def test_nesting_is_reported_when_the_drive_says_so(self) -> None:
        tree = _Tree({"S-INNER": "S-OUTER", "S-OUTER": "ROOT"})
        found = nested_folder_problems(
            [Route(name="outer", source_folder_id="S-OUTER", output_folder_id="O1"),
             Route(name="inner", source_folder_id="S-INNER", output_folder_id="O2")],
            lambda f: ancestor_ids(tree.get_item, f),
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("inside", found[0])

    def test_the_commands_that_process_recordings_all_run_the_check(self) -> None:
        """Read the source rather than keep a list — a list is what misses the next one."""
        import inspect

        from transcriber import __main__ as M

        for name in ("cmd_once", "cmd_run", "cmd_sweep", "cmd_backfill"):
            body = inspect.getsource(getattr(M, name))
            self.assertIn("_refuse_nested_watched_folders", body,
                          f"{name} processes recordings without checking for nested folders")

    def test_the_diagnostic_commands_do_NOT_run_it(self) -> None:
        """Being able to look is how a person sorts out a broken configuration. A check
        that stopped `status` or `held` from running would take away the tool needed to
        fix the very thing it is complaining about."""
        import inspect

        from transcriber import __main__ as M

        for name in ("cmd_status", "cmd_held", "cmd_gate", "cmd_review", "cmd_requeue",
                     "cmd_forget"):
            body = inspect.getsource(getattr(M, name))
            self.assertNotIn("_refuse_nested_watched_folders", body,
                             f"{name} is a diagnostic and must keep working when the "
                             "configuration is wrong")

    def test_it_refuses_to_start_and_names_both_folders(self) -> None:
        from transcriber import __main__ as M

        config = Config.offline()
        config.routes = (
            Route(name="outer", label="Carel", source_folder_id="S-OUTER", output_folder_id="O1"),
            Route(name="inner", label="Danie", source_folder_id="S-INNER", output_folder_id="O2"),
        )
        tree = _Tree({"S-INNER": "S-OUTER", "S-OUTER": "ROOT"})
        with self.assertRaises(ConfigError) as caught:
            M._refuse_nested_watched_folders(config, tree)
        self.assertTrue(any("inside" in p for p in caught.exception.problems))

    def test_a_drive_that_will_not_answer_lets_the_service_start(self) -> None:
        from transcriber import __main__ as M

        config = Config.offline()
        config.routes = (
            Route(name="outer", source_folder_id="S-OUTER", output_folder_id="O1"),
            Route(name="inner", source_folder_id="S-INNER", output_folder_id="O2"),
        )
        self.assertIsNone(
            M._refuse_nested_watched_folders(config, _Tree({"S-INNER": "S-OUTER"}, breaks=True))
        )

    def test_one_route_is_never_nested_inside_itself(self) -> None:
        from transcriber import __main__ as M

        config = Config.offline()
        config.routes = (Route(name="only", source_folder_id="S", output_folder_id="O"),)
        tree = _Tree({"S": "ROOT"})
        self.assertIsNone(M._refuse_nested_watched_folders(config, tree))
        self.assertEqual(tree.asked, [], "a single route needs no drive call at all")

    def test_the_wizard_and_the_service_share_one_walk(self) -> None:
        """Two copies of a subtle walk is how they come to disagree about which folders
        overlap — and the wizard's answer is the one somebody trusts at setup time."""
        import inspect

        from transcriber import setup_wizard

        self.assertIn("ancestor_ids", inspect.getsource(setup_wizard._Folders.ancestors))


if __name__ == "__main__":
    unittest.main()
