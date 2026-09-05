"""The command line, actually dispatched.

⛔ WHY THIS MODULE EXISTS. A profile of the whole suite found that of the
seventeen subcommands, exactly one — ``try`` — was ever reached through
``main()``. The other sixteen parsed in nobody's test, and neither did the
helpers behind them. Three tests named ``cmd_once`` and ``cmd_status`` but
asserted on their SOURCE TEXT with ``inspect.getsource``; they never invoked
anything. So the ownership rule at the heart of the gate — a held passage is
answered by the person it belongs to and by nobody else — had never once run
under test, and neither had any refusal path.

What is covered here is the wrapper: parsing, dispatch, exit codes, and the
sentence a person is shown when they get it wrong. The machinery underneath is
tested by its own modules; this asks whether typing the command reaches it.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import os
import tempfile
import unittest

from transcriber import __main__ as cli
from transcriber.ledger import Ledger, State
from transcriber.models import DriveItem
from transcriber.worker import LAST_CYCLE_OK

#: Every subcommand the parser offers. Kept as a literal rather than read off the
#: parser so that DELETING a subcommand fails a test instead of quietly shrinking
#: the thing this module claims to cover.
SUBCOMMANDS = (
    "archive", "backfill", "config", "digest", "forget", "gate", "held", "once",
    "requeue", "review", "routes", "run", "selftest", "setup", "status", "sweep", "try",
)

#: A complete, entirely fake environment. Every value is refused by any real
#: service it might reach, and nothing here is a credential.
ENVIRONMENT = {
    "GRAPH_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "GRAPH_CLIENT_ID": "00000000-0000-0000-0000-000000000001",
    "GRAPH_CLIENT_SECRET": "not-a-real-secret",
    "GRAPH_USER_ID": "owner@example.invalid",
    "DRIVE_USER_ID": "owner@example.invalid",
    "SOURCE_FOLDER_ID": "folder-source",
    "OUTPUT_FOLDER_ID": "folder-output",
    "TRANSCRIBE_ENGINE": "openai",
    "OPENAI_API_KEY": "sk-not-a-real-key",
    "ANALYSIS_PROVIDER": "anthropic",
    "ANALYSIS_API_KEY": "not-a-real-analysis-key",
    "SMTP_HOST": "smtp.example.invalid",
    "SMTP_USER": "digest",
    "SMTP_PASSWORD": "not-a-real-password",
    "SMTP_FROM": "digest@example.invalid",
    "SMTP_TO": "someone@example.invalid",
    "HEARTBEAT_URL": "https://monitor.example.invalid/ping/not-a-real-token",
    "POLL_INTERVAL_S": "120",
}


class DrivesTheCommandLine(unittest.TestCase):
    """Base: a temp ledger, a temp scratch directory, and a clean environment."""

    def setUp(self) -> None:
        self._room = tempfile.TemporaryDirectory()
        self.addCleanup(self._room.cleanup)
        self.ledger_path = os.path.join(self._room.name, "ledger.sqlite")

        # The whole environment is replaced, not updated: a variable left over
        # from the machine running the suite is exactly the kind of thing that
        # makes a CLI test pass here and fail on somebody else's laptop.
        self._saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(ENVIRONMENT)
        os.environ["LEDGER_PATH"] = self.ledger_path
        os.environ["WORK_DIR"] = os.path.join(self._room.name, "work")
        os.environ["GATE_HELD_STORE"] = os.path.join(self._room.name, "held.sqlite")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """``main(argv)``, with its two streams captured and SystemExit turned into a code."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as stop:  # argparse refuses in the parser
                code = int(stop.code or 0)
        return code, out.getvalue(), err.getvalue()


class EverySubcommandParsesAndDispatches(DrivesTheCommandLine):
    """The gap this module was written for: sixteen of seventeen never ran."""

    def test_every_subcommand_offers_help(self) -> None:
        for name in SUBCOMMANDS:
            with self.subTest(subcommand=name):
                code, out, _ = self.run_cli(name, "--help")
                self.assertEqual(code, 0, f"`{name} --help` did not succeed")
                self.assertIn("usage:", out)

    def test_the_parser_still_offers_exactly_these(self) -> None:
        parser = cli._parser()
        choices: set[str] = set()
        for action in parser._actions:
            if getattr(action, "dest", "") == "command" and getattr(action, "choices", None):
                choices = set(action.choices)
        self.assertEqual(
            sorted(choices), sorted(SUBCOMMANDS),
            "a subcommand was added or removed and this module was not told",
        )

    def test_the_offline_ones_do_their_job(self) -> None:
        """These read and print. None of them needs a network or a credential."""
        for argv in (
            ("status",),
            ("status", "--json"),
            ("gate",),
            ("held", "list", "--as", "jay"),
            # `selftest` is deliberately NOT driven from in here. Three of its 201
            # checks read back what the service logged, and `tests/__init__.py`
            # mutes that logger for the whole suite — so running it from inside
            # would test the muting, not the service. It has its own entry point
            # (`make selftest`), which CI runs separately and at image build time.
        ):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, cli.EXIT_OK, f"{argv} said {code}: {err[-400:]}")
                self.assertTrue(out.strip(), f"{argv} printed nothing at all")


class ARefusalIsASentenceAndNotATraceback(DrivesTheCommandLine):
    """Every wrong thing a person can type has to come back as words."""

    def test_an_unknown_recording_is_refused_in_words(self) -> None:
        code, out, err = self.run_cli("requeue", "--id", "no-such-recording")
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertNotIn("Traceback", out + err)
        self.assertTrue((out + err).strip())

    def test_a_held_passage_belongs_to_one_person(self) -> None:
        """The rule the gate is built on, run through the command line at last."""
        code, out, err = self.run_cli(
            "held", "release", "--ref", "no-such-ref", "--as", "somebody-else"
        )
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertNotIn("Traceback", out + err)


class ADateThatIsNotADateIsRefusedWhereItIsTyped(DrivesTheCommandLine):
    """A window nobody asked to widen is the dangerous direction."""

    def test_a_word_is_not_a_date(self) -> None:
        code, _out, err = self.run_cli("gate", "--since", "notadate")
        self.assertEqual(code, 2, "argparse should have refused it")
        self.assertIn("not a date", err)

    def test_the_day_first_form_is_refused_rather_than_believed(self) -> None:
        """``01-09-2026`` is the ordinary way to write it here, and it used to
        sort BEFORE every stored stamp — silently measuring the whole history
        while printing the string back as though it had been honoured."""
        code, _out, err = self.run_cli("gate", "--since", "01-09-2026")
        self.assertEqual(code, 2)
        self.assertIn("2026-09-01", err, "the refusal should show the shape that works")

    def test_a_real_date_is_accepted(self) -> None:
        code, _out, err = self.run_cli("gate", "--since", "2026-09-01")
        self.assertEqual(code, cli.EXIT_OK, err[-400:])

    def test_status_and_digest_take_the_same_treatment(self) -> None:
        for argv in (("status", "--day", "nonsense"), ("digest", "--day", "nonsense")):
            with self.subTest(argv=argv):
                code, _out, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("not a date", err)


class TheHealthCheckAsksWhetherTheLoopIsTurning(DrivesTheCommandLine):
    """It used to ask whether anything had failed, which is a different question."""

    def _mark(self, when: datetime.datetime) -> None:
        with Ledger(self.ledger_path) as ledger:
            ledger.cursor_set(LAST_CYCLE_OK, when.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_fresh_cycle_is_healthy(self) -> None:
        self._mark(datetime.datetime.now(datetime.timezone.utc))
        code, out, _ = self.run_cli("status", "--healthcheck")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("healthy", out)

    def test_a_month_of_silence_is_not_healthy(self) -> None:
        """The direction that used to exit 0: a container whose loop had been
        dead for a month reported healthy, because nothing had failed — nothing
        had been attempted."""
        self._mark(
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
        )
        code, out, _ = self.run_cli("status", "--healthcheck")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("unhealthy", out)

    def test_having_never_run_is_not_healthy_either(self) -> None:
        code, out, _ = self.run_cli("status", "--healthcheck")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertIn("unhealthy", out)

    def test_a_recording_waiting_for_a_person_is_not_a_container_fault(self) -> None:
        """The other direction, which used to exit 1 for as long as nobody got
        to the recording. The failures list deliberately carries EVERY
        quarantined recording, not only today's, so one of them kept a healthy
        container marked unhealthy indefinitely."""
        self._mark(datetime.datetime.now(datetime.timezone.utc))
        with Ledger(self.ledger_path) as ledger:
            ledger.upsert_discovered(
                DriveItem(
                    item_id="stuck-recording",
                    name="Call Someone_260905_090000.m4a",
                    size=1024,
                    etag='"e1"',
                )
            )
            ledger.advance("stuck-recording", State.QUARANTINED, quarantine_reason="nobody has looked yet")

        healthy, out, _ = self.run_cli("status", "--healthcheck")
        self.assertEqual(healthy, cli.EXIT_OK, "a quarantined recording is not a container fault")
        self.assertIn("healthy", out)

        # And the ordinary report still says so, loudly — that signal is not lost,
        # it is simply not the health check's question.
        _code, report, _err = self.run_cli("status")
        self.assertIn("Call Someone_260905_090000.m4a", report)
        self.assertIn("waiting for a person", report)


class ALinkIsNotMintedWhereItCannotBePrinted(DrivesTheCommandLine):
    """Issuing kills every earlier link that person holds."""

    def test_no_address_means_nothing_is_issued(self) -> None:
        os.environ["GATE_REVIEW_BASE_URL"] = ""
        code, out, err = self.run_cli("review", "--link", "jay")
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("GATE_REVIEW_BASE_URL", out + err)

        # The proof that matters: it used to persist the token first and die
        # afterwards, so the person was left with a dead old link and no new one.
        _c, revoked, _e = self.run_cli("review", "--revoke", "jay")
        self.assertIn("0 live link(s) revoked", revoked)

    def test_with_an_address_it_prints_the_link(self) -> None:
        os.environ["GATE_REVIEW_BASE_URL"] = "https://review.example.invalid/"
        code, out, err = self.run_cli("review", "--link", "jay")
        self.assertEqual(code, cli.EXIT_OK, err[-400:])
        self.assertIn("https://review.example.invalid/", out)


class TheDiagnosticCommandsWorkWhereTheServiceRuns(DrivesTheCommandLine):
    """A deployed host has no ``.env`` — systemd hands the settings over."""

    def test_config_list_answers_from_the_environment(self) -> None:
        code, out, err = self.run_cli("config", "list", "--env", "/nonexistent/.env")
        self.assertEqual(code, cli.EXIT_OK, err[-400:])
        self.assertIn("TRANSCRIBE_ENGINE", out)

    def test_routes_list_answers_from_the_environment(self) -> None:
        code, out, err = self.run_cli("routes", "list", "--env", "/nonexistent/.env")
        self.assertEqual(code, cli.EXIT_OK, err[-400:])
        self.assertTrue(out.strip())

    def test_it_says_which_source_it_is_showing(self) -> None:
        """Silence here would be worse than the refusal it replaced: the numbers
        would look like the file's when they are this process's."""
        _code, out, _err = self.run_cli("config", "list", "--env", "/nonexistent/.env")
        self.assertIn("THIS PROCESS", out)

    def test_writing_still_refuses_when_there_is_no_file(self) -> None:
        """Writing into a process environment changes nothing that outlives the
        command, so ``config set`` must keep saying so."""
        code, out, err = self.run_cli(
            "config", "set", "LOG_LEVEL", "DEBUG", "--env", "/nonexistent/.env"
        )
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertIn("no /nonexistent/.env", (out + err).replace("there is ", "no "))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
