"""The two commands that change the configuration without a text editor.

Both of them can do damage in a way that is invisible until the next morning, and each is
tested for the moment it must refuse rather than for the happy path:

  * ``config set`` **validates before it writes.** A misspelt setting name, or a model id
    nobody documented, is not refused by anything else until the first recording of the day
    is analysed — which is 06:00 on a Tuesday. So it is refused here, with the list of what
    would have worked, and the ``.env`` is left byte for byte as it was;
  * ``routes remove`` **takes a route out of the watching and deletes nothing.** The
    promise printed on the screen — "the history is kept, the recordings are untouched" —
    is a claim, and a claim in a service about not losing things has to be a tested one.
    Every ledger row that route ever wrote is still there afterwards, still readable, still
    counted.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
import io
import os
import stat
import tempfile
import unittest

from transcriber import config_cmd, routes_cmd
from transcriber.config_cmd import ANALYSIS_MODELS
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, State
from transcriber.setup_wizard import load_env_file, write_env_file


ENV = {
    "GRAPH_TENANT_ID": "tenant-for-tests",
    "GRAPH_CLIENT_ID": "client-for-tests",
    "GRAPH_CLIENT_SECRET": "not-a-real-secret",
    "GRAPH_USER_ID": "drive-owner",
    "TRANSCRIBE_ENGINE": "openai",
    "OPENAI_API_KEY": "not-a-real-engine-key",
    "ANALYSIS_API_KEY": "not-a-real-analysis-key",
    "ANALYSIS_MODEL_CHEAP": "claude-haiku-4-5",
    "ANALYSIS_MODEL_STRONG": "claude-haiku-4-5",
    "SMTP_HOST": "smtp.invalid",
    "SMTP_USER": "digest",
    "SMTP_PASSWORD": "not-a-real-password",
    "SMTP_FROM": "digest@invalid",
    "SMTP_TO": "someone@invalid",
    "HEARTBEAT_URL": "https://example.invalid/beat",
    "ROUTES": "calls,site-meetings",
    "ROUTE_CALLS_LABEL": "Phone calls",
    "ROUTE_CALLS_SOURCE": "S-CALLS",
    "ROUTE_CALLS_OUTPUT": "O-CALLS",
    "ROUTE_SITE_MEETINGS_LABEL": "Site meetings",
    "ROUTE_SITE_MEETINGS_SOURCE": "S-SITE",
    "ROUTE_SITE_MEETINGS_OUTPUT": "O-SITE",
}


class _EnvFile:
    """A real ``.env`` on disk, written the way the wizard writes one."""

    def __init__(self, directory: str, **overrides: str) -> None:
        self.path = os.path.join(directory, ".env")
        values = dict(ENV)
        values["LEDGER_PATH"] = os.path.join(directory, "ledger.sqlite3")
        values.update(overrides)
        write_env_file(self.path, values)
        self.before = self.read_bytes()

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()

    def values(self) -> dict[str, str]:
        return load_env_file(self.path)

    @property
    def unchanged(self) -> bool:
        return self.read_bytes() == self.before


class _Answers:
    """Scripted keyboard answers, so a confirmation can be given without a terminal."""

    def __init__(self, answers) -> None:
        self.answers = list(answers)

    def __call__(self, _prompt: str = "") -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def set_args(**kw) -> argparse.Namespace:
    """The namespace ``config set`` gets from argparse, with every alias defaulted off."""
    fields = {"action": "set", "key": None, "value": None, "env": ""}
    fields.update({alias.replace("-", "_"): None for alias in config_cmd.ALIASES})
    fields.update(kw)
    return argparse.Namespace(**fields)


class ConfigSetRefusesBeforeItWrites(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = _EnvFile(self.dir.name)
        self.out = io.StringIO()

    def _set(self, key: str | None, value: str | None, **kw) -> int:
        return config_cmd.cmd_set(
            set_args(env=self.env.path, key=key, value=value, **kw), self.out
        )

    def test_an_unknown_key_is_refused_and_nothing_is_written(self) -> None:
        code = self._set("ANALYSIS_MODEL_STRONGEST", "claude-opus-5")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged, "the .env was rewritten for a setting that does not exist")
        printed = self.out.getvalue()
        self.assertIn("Nothing was written", printed)
        self.assertIn("ANALYSIS_MODEL_STRONGEST", printed)
        self.assertIn("config list", printed, "it must say how to find the real names")

    def test_an_unknown_key_close_to_a_real_one_suggests_it(self) -> None:
        self._set("DIGEST_HOURS", "7")

        printed = self.out.getvalue()
        self.assertIn("Did you mean", printed)
        self.assertIn("DIGEST_HOUR", printed)
        self.assertTrue(self.env.unchanged)

    def test_an_undocumented_model_id_is_refused_with_the_documented_ones(self) -> None:
        """A model id nobody has is not refused by anything until 06:00 on a Tuesday."""
        code = self._set("ANALYSIS_MODEL_STRONG", "claude-opus-9-ultra")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        printed = self.out.getvalue()
        self.assertIn("Nothing was written", printed)
        for model in ANALYSIS_MODELS:
            self.assertIn(model, printed, "the refusal must list what would have worked")
        self.assertEqual(
            self.env.values()["ANALYSIS_MODEL_STRONG"], "claude-haiku-4-5",
            "the old value must survive a refused change",
        )

    def test_a_near_miss_model_id_suggests_the_real_one(self) -> None:
        self._set("ANALYSIS_MODEL_STRONG", "claude-opus-5.1")

        self.assertIn("Did you mean claude-opus-5", self.out.getvalue())
        self.assertTrue(self.env.unchanged)

    def test_the_shorthand_is_validated_the_same_way(self) -> None:
        """`config set --model X` must not be a way around the check."""
        code = self._set(None, None, model="gpt-9")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("Nothing was written", self.out.getvalue())

    def test_a_documented_model_is_accepted_and_written(self) -> None:
        """The refusals are only worth anything if the command still does its job."""
        code = self._set("ANALYSIS_MODEL_STRONG", "claude-opus-5")

        self.assertEqual(code, config_cmd.EXIT_OK, self.out.getvalue())
        self.assertEqual(self.env.values()["ANALYSIS_MODEL_STRONG"], "claude-opus-5")
        self.assertIn("claude-opus-5", self.out.getvalue())

    def test_a_number_out_of_range_is_refused(self) -> None:
        code = self._set("DIGEST_HOUR", "25")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("Nothing was written", self.out.getvalue())

    def test_a_route_s_folder_is_not_settable_here(self) -> None:
        """Folders are picked from the drive and checked against each other, or not changed."""
        code = self._set("ROUTE_CALLS_SOURCE", "SOMEWHERE-ELSE")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("transcriber routes", self.out.getvalue())

    def test_a_change_that_would_stop_the_service_starting_is_refused(self) -> None:
        """Valid on its own, wrong beside its neighbours: the second gate exists for this."""
        code = self._set("TRANSCRIBE_ENGINE", "elevenlabs")   # no ELEVENLABS_API_KEY in the file

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("Nothing was written", self.out.getvalue())
        self.assertIn("ELEVENLABS_API_KEY", self.out.getvalue())

    def test_the_single_folder_settings_are_refused_once_routes_exist(self) -> None:
        code = self._set("SOURCE_FOLDER_ID", "SOMEWHERE-ELSE")

        self.assertEqual(code, config_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("ROUTES", self.out.getvalue())

    def test_a_refusal_never_prints_a_secret(self) -> None:
        self._set("GRAPH_CLIENT_SECRET", "")

        self.assertNotIn("not-a-real-secret", self.out.getvalue())
        self.assertTrue(self.env.unchanged)

    def test_a_written_file_is_still_readable_only_by_its_owner(self) -> None:
        self._set("DIGEST_HOUR", "7")

        mode = stat.S_IMODE(os.stat(self.env.path).st_mode)
        self.assertEqual(mode, 0o600, f"a file holding a client secret was mode {mode:o}")

    def test_a_write_keeps_every_other_setting(self) -> None:
        before = self.env.values()

        self._set("DIGEST_HOUR", "7")

        after = self.env.values()
        for key, value in before.items():
            if key == "DIGEST_HOUR":
                continue
            self.assertEqual(after.get(key), value, f"{key} was lost by an unrelated change")


class RoutesRemoveKeepsEveryLedgerRow(unittest.TestCase):
    """"The ledger history is NEVER deleted" is a promise, so it is a test."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = _EnvFile(self.dir.name)
        self.ledger_path = self.env.values()["LEDGER_PATH"]
        self.out = io.StringIO()

        with Ledger(self.ledger_path) as ledger:
            ledger.record_page(
                [self._item("C1", "Call Carel_260827_120055.m4a"),
                 self._item("C2", "Call Sipho_260827_161049.m4a")],
                "calls-1",
                route="calls",
            )
            ledger.record_page(
                [self._item("M1", "BEACH COURT SITE WALK 270826.m4a")], "site-1",
                route="site-meetings",
            )
            for state in (State.CLAIMED, State.FETCHED, State.TRANSCRIBED, State.ANALYSED,
                          State.DONE):
                ledger.advance("C1", state)
            self.before = self._snapshot(ledger)

    @staticmethod
    def _item(item_id: str, name: str) -> DriveItem:
        return DriveItem(item_id=item_id, name=name, size=2048, etag=f'"{item_id}"',
                         created_at="2026-08-27T09:00:00Z")

    @staticmethod
    def _snapshot(ledger: Ledger) -> dict[str, tuple[str, str, str]]:
        rows = {}
        for route in ledger.routes_seen():
            for row in ledger.rows_in_state(State.DONE, route) + ledger.unfinished(route):
                rows[row.item_id] = (row.route, row.state, row.name)
        return rows

    def _routes(self, action: str, slug: str | None, answers: list[str]) -> int:
        """Run one ``routes`` sub-command with its questions answered.

        ``--yes`` is deliberately *not* used: it means "take the default answer", and the
        default answer to "remove this route?" is no — which is the right default and the
        wrong thing to test a removal with. The answers are typed instead, which is also
        what a person does.
        """
        args = argparse.Namespace(
            action=action, slug=slug, env=self.env.path, offline=True, yes=False
        )
        original = builtins.input
        builtins.input = _Answers(answers)
        try:
            with contextlib.redirect_stdout(self.out):
                return routes_cmd.run(args, self.out)
        finally:
            builtins.input = original

    def _remove(self, slug: str, answers: list[str] | None = None) -> int:
        return self._routes("remove", slug, answers if answers is not None else ["y"])

    def test_the_route_is_taken_out_of_the_file(self) -> None:
        code = self._remove("calls")

        self.assertEqual(code, routes_cmd.EXIT_OK, self.out.getvalue())
        values = self.env.values()
        self.assertEqual(values["ROUTES"], "site-meetings")
        self.assertNotIn("ROUTE_CALLS_SOURCE", values)

    def test_every_ledger_row_is_still_there(self) -> None:
        self._remove("calls")

        with Ledger(self.ledger_path) as ledger:
            after = self._snapshot(ledger)
        self.assertEqual(after, self.before, "removing a route changed the ledger")

    def test_the_removed_route_s_rows_keep_their_route_name(self) -> None:
        """They are not re-filed under whatever is left: that would be a quiet rewrite."""
        self._remove("calls")

        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(ledger.get("C1").route, "calls")
            self.assertEqual(ledger.get("C2").route, "calls")
            self.assertEqual(ledger.get("C1").state, State.DONE)
            self.assertIn("calls", ledger.routes_seen())
            self.assertEqual(len(ledger.unfinished("calls")), 1)

    def test_it_says_plainly_that_the_history_and_the_recordings_are_kept(self) -> None:
        self._remove("calls")

        printed = self.out.getvalue()
        self.assertIn("Nothing in OneDrive is touched", printed)
        self.assertIn("kept", printed)
        self.assertIn("moved, renamed or", printed)
        self.assertIn("2 ledger row(s)", printed, "the promise should be a number, not an adjective")

    def test_saying_no_leaves_the_file_and_the_ledger_exactly_as_they_were(self) -> None:
        code = self._remove("calls", answers=["n"])

        self.assertEqual(code, routes_cmd.EXIT_OK)
        self.assertTrue(self.env.unchanged)
        self.assertIn("Nothing was written", self.out.getvalue())
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(self._snapshot(ledger), self.before)

    def test_removing_the_last_route_is_refused_and_nothing_is_written(self) -> None:
        """A file with no enabled route is a service that will not start."""
        self._remove("calls")
        before = self.env.read_bytes()

        code = self._remove("site-meetings")

        self.assertEqual(code, routes_cmd.EXIT_FAILED)
        self.assertEqual(self.env.read_bytes(), before)
        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(self._snapshot(ledger), self.before)

    def test_a_route_that_does_not_exist_changes_nothing(self) -> None:
        code = self._remove("whatsapp", answers=[])

        self.assertEqual(code, routes_cmd.EXIT_FAILED)
        self.assertTrue(self.env.unchanged)
        self.assertIn("no route called 'whatsapp'", self.out.getvalue())

    def test_disable_keeps_the_route_and_its_folders_in_the_file(self) -> None:
        """The gentler option the removal message points at has to actually exist."""
        code = self._routes("disable", "calls", [])

        self.assertEqual(code, routes_cmd.EXIT_OK, self.out.getvalue())
        values = self.env.values()
        self.assertIn("calls", values["ROUTES"])
        self.assertEqual(values["ROUTE_CALLS_SOURCE"], "S-CALLS")
        self.assertEqual(values["ROUTE_CALLS_ENABLED"].lower(), "false")

        with Ledger(self.ledger_path) as ledger:
            self.assertEqual(self._snapshot(ledger), self.before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class YesMeansYes(unittest.TestCase):
    """`routes remove --yes` used to DECLINE the removal.

    `--yes` was routed through `confirm()`, which returns the *default* when assuming, and
    the default on a destructive prompt is deliberately No. So the flag that reads as
    "confirm this" did the opposite, silently, and said nothing was written. A flag that
    inverts its own meaning is worse than not having one.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.env = _EnvFile(self.dir.name)
        self.out = io.StringIO()

    def _remove(self, *, yes: bool, typed: list[str]) -> int:
        args = argparse.Namespace(
            action="remove", slug="calls", env=self.env.path, offline=True, yes=yes
        )
        original = builtins.input
        builtins.input = _Answers(typed)
        try:
            with contextlib.redirect_stdout(self.out):
                return routes_cmd.run(args, self.out)
        finally:
            builtins.input = original

    def test_yes_actually_removes(self) -> None:
        # Nothing is typed: --yes must carry the confirmation on its own.
        code = self._remove(yes=True, typed=[])

        self.assertEqual(code, routes_cmd.EXIT_OK, self.out.getvalue())
        self.assertNotIn(
            "calls", self.env.values().get("ROUTES", ""),
            "--yes declined the removal it was meant to confirm",
        )

    def test_answering_no_still_keeps_the_route(self) -> None:
        code = self._remove(yes=False, typed=["n"])

        self.assertEqual(code, routes_cmd.EXIT_OK, self.out.getvalue())
        self.assertIn("calls", self.env.values().get("ROUTES", ""))
        self.assertTrue(self.env.unchanged, "declining still rewrote the file")
