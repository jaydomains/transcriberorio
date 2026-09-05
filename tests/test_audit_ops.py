"""``.env.example`` is a deployment artifact, and CI has to read it the way a deploy does.

Two separate claims are made about that file, and both of them used to be maintained by
hand — which is to say, not maintained at all.

**It has to be readable by the things that actually read it.** Neither of the documented
deployments hands the file to Python. systemd's ``EnvironmentFile=`` and docker's
``--env-file`` both treat a ``#`` as the start of a comment only at the *start of a line*:
inside a value there is no word boundary, so everything after the first ``=`` is kept
verbatim. A line written to be read by a person::

    POLL_INTERVAL_S=120               # Seconds between delta polls.

therefore sets the poll interval to the whole string ``120               # Seconds between
delta polls.``, and the service refuses to start. This survived for as long as it did
because the two readers in this repository disagree: the wizard's ``load_env_file`` strips a
trailing comment, and a deploy does not — so anybody who ran ``transcriber setup`` never saw
the fault, and only a by-the-book first deploy hit it, on its first day, before anybody knew
what a normal start looked like.

**And it has to be the whole list.** Its first line says it is "every environment variable
the service reads". ``config.py`` already keeps that list — ``_SPEC`` plus the per-engine key
names plus ``_ALSO_READ``, the ones read elsewhere in the service — so whether the claim is
true is a set comparison, and there is no reason for a person to be the one making it. The
variable that proved the point was ``GRAPH_SECRET_EXPIRES_ON``: ops/AZURE.md calls it the
single most likely way this service dies, and it appeared in no example file anywhere.

These tests read the shipped file; they do not read a fixture. That is deliberate. A fixture
would test the parser, and the parser is not the thing that keeps breaking.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

from transcriber.config import (
    ENGINE_KEY_VARS,
    Config,
    ConfigError,
    _ALSO_READ,
    _SPEC,
)
from transcriber.setup_wizard import load_env_file

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLE = os.path.join(_REPO_ROOT, ".env.example")

#: ``NAME=`` and then everything else, to the end of the line. No stripping, no comment
#: handling, no quote handling — the point of this expression is that it is as dumb as the
#: things that read the file in production.
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _read_it_the_way_a_deploy_reads_it(path: str) -> dict[str, str]:
    """Parse a ``.env`` under the rule systemd and docker actually apply.

    A line is a comment only when its first non-blank character is ``#``. Everything after
    the first ``=`` is the value, spaces and ``#`` and all. Nothing is stripped, because
    stripping here would quietly repair exactly the fault this file has to be free of.
    """
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = _ASSIGNMENT.match(line)
            if match:
                values[match.group(1)] = match.group(2)
    return values


#: The values a first deploy types in by hand. Every one of these ships blank on purpose —
#: they are the credentials, the folder ids and the addresses, and there are no real values
#: in that file and there must never be any. Supplying them here is what leaves everything
#: else — the numbers, the booleans, the paths, the defaults — coming from the file itself,
#: which is the half these tests are about.
_TYPED_IN_ON_A_FIRST_DEPLOY = {
    "GRAPH_TENANT_ID": "a-tenant-id",
    "GRAPH_CLIENT_ID": "a-client-id",
    "GRAPH_CLIENT_SECRET": "a-client-secret",
    "GRAPH_USER_ID": "somebody@example.co",
    "SOURCE_FOLDER_ID": "01SOURCEFOLDERID",
    "OUTPUT_FOLDER_ID": "01OUTPUTFOLDERID",
    "TRANSCRIBE_ENGINE": "openai",
    "OPENAI_API_KEY": "not-a-real-engine-key",
    "ANALYSIS_API_KEY": "not-a-real-analysis-key",
    "SMTP_HOST": "smtp.example.co",
    "SMTP_USER": "a-user",
    "SMTP_PASSWORD": "a-password",
    "SMTP_FROM": "transcriber@example.co",
    "SMTP_TO": "somebody@example.co",
    "HEARTBEAT_URL": "https://monitor.example.co/ping",
    "LEDGER_PATH": ":memory:",
}


class TheExampleFileReadsTheSameToEveryReader(unittest.TestCase):
    """The shape half: what a deploy gets when it reads the shipped file."""

    def test_the_service_starts_on_the_file_a_deploy_would_hand_it(self) -> None:
        """Copy the file, fill in the credentials, start the service. That must work.

        This is the finding end to end, rather than a search for ``#`` characters, because
        the fault is not really about ``#``: it is that the file is handed to something with
        no idea what a comment is, and the only reader whose opinion counts about the result
        is ``Config.from_env``.
        """
        from_the_file = _read_it_the_way_a_deploy_reads_it(_EXAMPLE)

        environment = dict(from_the_file)
        environment.update(_TYPED_IN_ON_A_FIRST_DEPLOY)
        # from_env creates the work directory as it goes, and the shipped value is a system
        # path (/var/cache/transcriber) that only root can create. Whether that path is the
        # right one is a separate question, asked separately; here it is redirected so this
        # test says the same thing on a developer's laptop, in CI and on the server.
        with tempfile.TemporaryDirectory() as scratch:
            environment["WORK_DIR"] = os.path.join(scratch, "transcriber")
            try:
                Config.from_env(environment)
            except ConfigError as refusal:
                self.fail(
                    "A deploy that copied .env.example, filled in the credentials and "
                    "started the service would be refused. Every problem below comes from a "
                    "value the example file itself supplies:\n" + str(refusal)
                )

        # And, once the file is known to be startable: none of the values this test
        # supplies may already be in the file. If one ever is, the dictionary above is
        # quietly overriding something shipped, and the test is no longer examining what an
        # operator would actually get. Asked of the tolerant reader, because the question
        # here is what a person wrote down, not what a deploy makes of it.
        intended = load_env_file(_EXAMPLE)
        for name in _TYPED_IN_ON_A_FIRST_DEPLOY:
            self.assertEqual(
                "", intended.get(name, ""),
                f"{name} now ships a value in .env.example, but this test overrides it with "
                f"a made-up one, so the shipped value is not being tested. Either that value "
                f"does not belong in an example file, or this test should stop filling it in.",
            )

    def test_the_wizard_and_the_deploy_agree_about_every_value(self) -> None:
        """The two readers in this repository must get the same file out of the same file.

        ``transcriber setup`` writes a .env and reads it back with ``load_env_file``, which
        is tolerant on purpose: it strips a trailing ``  # comment`` so that a hand-edited
        file still loads. A deploy strips nothing. While those two disagree about even one
        line, the file means two different things depending on who opens it, and the person
        who finds out is whoever did the install by hand rather than with the wizard.
        """
        strict = _read_it_the_way_a_deploy_reads_it(_EXAMPLE)
        tolerant = load_env_file(_EXAMPLE)

        self.assertEqual(
            sorted(tolerant), sorted(strict),
            "The wizard's reader and a deploy's reader do not even find the same variables "
            "in .env.example.",
        )
        for name in sorted(strict):
            with self.subTest(variable=name):
                self.assertEqual(
                    tolerant[name], strict[name],
                    f"{name} means one thing to `transcriber setup` and another to systemd "
                    f"and docker. The wizard reads {tolerant[name]!r}; a deploy reads "
                    f"{strict[name]!r}, because it keeps everything after the '=' exactly as "
                    f"written. Put the explanation on its own '#' line above the variable.",
                )


class TheExampleFileIsTheWholeList(unittest.TestCase):
    """The completeness half: the file's own first line, checked instead of trusted."""

    def test_its_variables_are_exactly_the_ones_the_service_reads(self) -> None:
        """Set equality, both directions, because both directions cost somebody a morning.

        A variable the service reads and the file does not mention is a setting nobody
        knows to set — that is how a client secret expires with no warning. A variable the
        file lists and the service does not read is worse in a quieter way: it is a setting
        an operator carefully fills in that does nothing at all, and there is no error
        anywhere, because a service cannot report a variable it never looks at.

        The per-route variables (ROUTE_<NAME>_SOURCE and friends) are not in either set:
        their names depend on the route names, so the file shows them as a commented example
        block instead, and a commented block is not an assignment to any reader.
        """
        listed = set(_read_it_the_way_a_deploy_reads_it(_EXAMPLE))
        read_by_the_service = (
            {var.env for var in _SPEC} | set(ENGINE_KEY_VARS.values()) | set(_ALSO_READ)
        )

        missing = sorted(read_by_the_service - listed)
        self.assertEqual(
            [], missing,
            "The service reads these and .env.example does not mention them, though its "
            "first line says it lists every one: " + ", ".join(missing) + ". Add a line for "
            "each, with its explanation above it. If one of them is deliberately not offered "
            "to operators, it does not belong in config.py's list of names the service "
            "reads either — the two statements have to agree somewhere.",
        )

        does_nothing = sorted(listed - read_by_the_service)
        self.assertEqual(
            [], does_nothing,
            ".env.example offers these and nothing in the service ever reads them: "
            + ", ".join(does_nothing) + ". Either they were renamed and the example file was "
            "not, or they were removed. Setting one of them has no effect and produces no "
            "error, which is the hardest kind of configuration problem to find.",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
