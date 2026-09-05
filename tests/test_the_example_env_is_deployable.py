"""``.env.example`` has to survive being read the way a deploy actually reads it.

Both documented deployments hand the file to something that is not Python. systemd's
``EnvironmentFile=`` and docker's ``--env-file`` each keep **everything after the first
``=`` verbatim**: neither of them knows what a trailing ``# comment`` is, because in a shell
a ``#`` only starts a comment at the start of a word, and there is no word boundary inside a
value. So a line written for a human to read::

    POLL_INTERVAL_S=120               # Seconds between delta polls.

sets the poll interval to the string ``120               # Seconds between delta polls.``,
and the service refuses to start. The file once shipped with an inline comment on all
sixty-two of its value lines, which is thirty-four separate refusals on a by-the-book first
deploy — and the blank ones were worse than the numeric ones, because ``ROUTES=`` picked up
``# Short names, comma separated`` as a route *name* rather than as an obvious error.

It survived as long as it did because the wizard's own reader (``load_env_file``) strips a
trailing comment and the service's reader does not, so anybody who ran ``transcriber setup``
never saw it. ``write_env_file`` puts every explanation on its own line above its variable;
these two tests are what keep the example file in the same shape as the file the tool
writes.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from transcriber.config import Config, ConfigError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLE = os.path.join(_REPO_ROOT, ".env.example")

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _read_the_way_systemd_reads_it(path: str) -> dict[str, str]:
    """Parse a ``.env`` under the rule systemd and docker actually apply.

    A ``#`` starts a comment only at the beginning of a line. Everything after the first
    ``=`` is the value, trailing spaces and all — deliberately not stripped of anything,
    because stripping here would hide exactly the fault this file exists to catch.
    """
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _ASSIGNMENT.match(line)
            if match:
                values[match.group(1)] = match.group(2)
    return values


#: What a first deploy fills in by hand. Every one of these ships blank in the example on
#: purpose — they are the credentials and the folder ids, and there are no real values in
#: that file and there must never be any. Supplying them here is what leaves the *defaults*
#: — the numbers, the booleans, the paths — coming from the file itself, which is the half
#: under test.
_FILLED_IN_BY_THE_OPERATOR = dict(
    GRAPH_TENANT_ID="a-tenant-id",
    GRAPH_CLIENT_ID="a-client-id",
    GRAPH_CLIENT_SECRET="a-client-secret",
    GRAPH_USER_ID="somebody@example.co",
    SOURCE_FOLDER_ID="01SOURCE",
    OUTPUT_FOLDER_ID="01OUTPUT",
    TRANSCRIBE_ENGINE="openai",
    OPENAI_API_KEY="not-a-real-engine-key",
    ANALYSIS_API_KEY="not-a-real-analysis-key",
    SMTP_HOST="smtp.example.co",
    SMTP_USER="a-user",
    SMTP_PASSWORD="a-password",
    SMTP_FROM="transcriber@example.co",
    SMTP_TO="somebody@example.co",
    HEARTBEAT_URL="https://monitor.example.co/ping",
    LEDGER_PATH=":memory:",
)


class TheExampleFileIsDeployable(unittest.TestCase):

    def test_no_value_carries_a_trailing_comment(self) -> None:
        """The shape check, which is the one that names the fault plainly.

        Kept separate from the test below because it points at the line rather than at the
        symptom: a value with a ``#`` in it is wrong whether or not that particular setting
        happens to be one the service validates.
        """
        for name, value in _read_the_way_systemd_reads_it(_EXAMPLE).items():
            with self.subTest(variable=name):
                self.assertNotIn(
                    "#", value,
                    f"{name} has an explanation after the value on the same line. systemd and "
                    f"docker keep it as part of the value. Put it on its own '#' line above "
                    f"the variable, the way `transcriber setup` writes one.",
                )
                self.assertEqual(
                    value, value.strip(),
                    f"{name} has padding around its value, which is kept verbatim too.",
                )

    def test_the_service_starts_on_the_example_file(self) -> None:
        """The end-to-end check: fill in the blanks, and the service must accept the rest."""
        environment = dict(_read_the_way_systemd_reads_it(_EXAMPLE))
        environment.update(_FILLED_IN_BY_THE_OPERATOR)
        # `from_env` MAKES the directories it is given, so the example's real
        # WORK_DIR would be created on whatever machine runs the suite — as root
        # under CI, and not at all for an ordinary user, who would see this test
        # error rather than fail. The paths themselves are checked by
        # `test_the_scratch_directory_is_not_in_tmp`; here only the rest matters.
        with tempfile.TemporaryDirectory() as scratch:
            environment["WORK_DIR"] = os.path.join(scratch, "work")
            environment["LEDGER_PATH"] = os.path.join(scratch, "ledger.sqlite")
            try:
                Config.from_env(environment)
            except ConfigError as refusal:
                self.fail(
                    "A deploy that copied .env.example, filled in the credentials and started "
                    "the service would be refused:\n" + str(refusal)
                )

    def test_the_scratch_directory_is_not_in_tmp(self) -> None:
        """/tmp is emptied on reboot and is a memory filesystem on many distributions.

        The unit sets ``PrivateTmp=yes`` and provisions ``/var/cache/transcriber``, and the
        container image sets the same path. An example that disagreed with both sent the
        audio of a failed recording — kept for two days so a person can listen to what went
        wrong — into a directory wiped by the next restart.
        """
        work_dir = _read_the_way_systemd_reads_it(_EXAMPLE).get("WORK_DIR", "")
        self.assertTrue(work_dir, "WORK_DIR should show its value in .env.example")
        self.assertFalse(
            work_dir.startswith("/tmp"),
            "WORK_DIR must not be under /tmp — see ops/transcriber.service and ops/Dockerfile.",
        )

    def test_every_variable_the_service_reads_is_in_the_file(self) -> None:
        """Line 1 claims the file is the whole list, so it has to be the whole list.

        ``GRAPH_SECRET_EXPIRES_ON`` is the one that mattered: ops/AZURE.md calls it the
        single most likely way this service dies, and it was in no example file anywhere.
        """
        from transcriber.config import _SPEC  # the list of every variable, one place only

        listed = set(_read_the_way_systemd_reads_it(_EXAMPLE))
        # The per-route names depend on the route names, so they are shown as a commented
        # example block rather than as settable lines, and are not expected here.
        missing = sorted(var.env for var in _SPEC if var.env not in listed)
        self.assertEqual(
            [], missing,
            "These are read by the service and are not in .env.example, which says on its "
            "first line that it lists every one: " + ", ".join(missing),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
