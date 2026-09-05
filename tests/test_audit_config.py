"""An independent pass over the four configuration faults the audit found.

Each one had the same shape: the file said one thing and the service did another, and
nothing anywhere said so. ``GATE_MODEE=on`` left the gate in shadow. ``ROUTE_CALLS_ENABLD``
left a route watching a folder somebody had switched off. An ``http://`` analysis endpoint
put the whole unredacted transcript, and the key, on the wire in clear. A credential with no
expiry date looked exactly like a countdown that had not started yet. And
``ROUTE_DEFAULT_ARCHIVE=""``, written by the wizard to mean "never archive", arrived through
``docker --env-file`` as the two-character string ``""`` and was taken for a folder id.

These tests are written from the outside: they set an environment the way an operator's
``.env`` and the machine around it would set it, and assert on what a person is told. They
are deliberately a second opinion on the module that shipped with the fix rather than a copy
of it, so two independent readings of "is this fault actually closed" have to agree.

**The control matters as much as the checks.** Under systemd the ``.env`` is loaded with
``EnvironmentFile=``, so what the parse sees is the file's names plus systemd's own plus the
shell's plus the container's. A check that refused what it did not recognise would refuse to
start on an ordinary host — a worse fault than the one it fixes, and one that arrives at the
worst moment, on a restart nobody chose. So a full realistic host environment is loaded here
and asserted to be entirely uneventful: no refusal, and not one word about anybody else's
variables in the notices either.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.config import Config, ConfigError

#: The workspace the ledger and the work directory live in for the whole module. A real path
#: rather than ``:memory:`` so that the held-passage store lands beside the ledger and
#: outside the work directory, which keeps the unrelated "held passages would be swept"
#: notice out of every assertion here.
_WORKSPACE: tempfile.TemporaryDirectory | None = None


def setUpModule() -> None:
    global _WORKSPACE
    _WORKSPACE = tempfile.TemporaryDirectory(prefix="transcriber-config-audit-")


def tearDownModule() -> None:
    if _WORKSPACE is not None:
        _WORKSPACE.cleanup()


def _base(**overrides: str) -> dict[str, str]:
    """A ``.env`` that starts, in the single-folder shape most installations still use."""
    root = _WORKSPACE.name
    env = {
        "GRAPH_TENANT_ID": "tenant-for-tests",
        "GRAPH_CLIENT_ID": "client-for-tests",
        "GRAPH_CLIENT_SECRET": "not-a-real-secret",
        "GRAPH_USER_ID": "drive-owner",
        "TRANSCRIBE_ENGINE": "openai",
        "OPENAI_API_KEY": "not-a-real-engine-key",
        "ANALYSIS_API_KEY": "not-a-real-analysis-key",
        "SMTP_HOST": "smtp.invalid",
        "SMTP_USER": "digest",
        "SMTP_PASSWORD": "not-a-real-password",
        "SMTP_FROM": "digest@example.invalid",
        "SMTP_TO": "james@example.invalid",
        "HEARTBEAT_URL": "https://hc.example.invalid/aaaa-bbbb",
        "SOURCE_FOLDER_ID": "FOLDER-SOURCE",
        "OUTPUT_FOLDER_ID": "FOLDER-OUTPUT",
        "LEDGER_PATH": os.path.join(root, "ledger.sqlite3"),
        "WORK_DIR": os.path.join(root, "work"),
    }
    env.update(overrides)
    return env


def _routed(**overrides: str) -> dict[str, str]:
    """The same file, written the way a two-route installation writes it."""
    env = _base()
    for name in ("SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID"):
        env.pop(name)
    env.update(
        ROUTES="calls,site",
        ROUTE_CALLS_LABEL="Phone calls",
        ROUTE_CALLS_SOURCE="FOLDER-CALLS-IN",
        ROUTE_CALLS_OUTPUT="FOLDER-CALLS-OUT",
        ROUTE_SITE_LABEL="Site meetings",
        ROUTE_SITE_SOURCE="FOLDER-SITE-IN",
        ROUTE_SITE_OUTPUT="FOLDER-SITE-OUT",
    )
    env.update(overrides)
    return env


#: Everything a real machine puts in the environment before the ``.env`` is read at all:
#: systemd's own variables, the shell's, a container's, a proxy's. Several of these begin
#: with the same word as one of this service's settings on purpose — ``HTTP_PROXY`` beside
#: ``HTTP_TIMEOUT_S``, ``MAX_JOBS`` beside ``MAX_RETRIES``, ``LOG_DIR`` beside ``LOG_LEVEL``,
#: ``GROUPS`` beside ``GROUP_FOLDER_ID``, ``SOURCE_DATE_EPOCH`` beside ``SOURCE_FOLDER_ID``,
#: ``LANGUAGE`` one letter from ``LANGUAGES`` — because that is exactly the shape a check
#: like this gets wrong.
_HOST = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/var/lib/transcriber",
    "PWD": "/opt/transcriber",
    "OLDPWD": "/root",
    "SHELL": "/usr/sbin/nologin",
    "SHLVL": "1",
    "TERM": "xterm-256color",
    "USER": "transcriber",
    "LOGNAME": "transcriber",
    "GROUP": "transcriber",
    "GROUPS": "transcriber adm",
    "MAIL": "/var/mail/transcriber",
    "EDITOR": "vi",
    "LANG": "en_ZA.UTF-8",
    "LANGUAGE": "en_ZA:en",
    "LC_ALL": "C.UTF-8",
    "TZ": "Africa/Johannesburg",
    "TMPDIR": "/tmp",
    "HOSTNAME": "record-vm-1",
    "INVOCATION_ID": "9f2c4c1a7e2b4d0f9b3a",
    "JOURNAL_STREAM": "8:214748",
    "SYSTEMD_EXEC_PID": "812",
    "MAINPID": "812",
    "MANAGERPID": "1",
    "LISTEN_FDS": "0",
    "NOTIFY_SOCKET": "/run/systemd/notify",
    "WATCHDOG_PID": "812",
    "WATCHDOG_USEC": "30000000",
    "STATE_DIRECTORY": "/var/lib/transcriber",
    "CACHE_DIRECTORY": "/var/cache/transcriber",
    "RUNTIME_DIRECTORY": "/run/transcriber",
    "CONFIGURATION_DIRECTORY": "/etc/transcriber",
    "LOGS_DIRECTORY": "/var/log/transcriber",
    "XDG_RUNTIME_DIR": "/run/user/0",
    "SSH_CONNECTION": "10.0.0.4 51234 10.0.0.9 22",
    "VIRTUAL_ENV": "/opt/transcriber/venv",
    "PYTHONPATH": "/opt/transcriber/src",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "SOURCE_DATE_EPOCH": "1700000000",
    "HTTP_PROXY": "http://proxy.internal:3128",
    "HTTPS_PROXY": "http://proxy.internal:3128",
    "NO_PROXY": "localhost,127.0.0.1",
    "MAX_JOBS": "2",
    "LOG_DIR": "/var/log",
    "DEBIAN_FRONTEND": "noninteractive",
    "CONTAINER_ID": "b91f2c",
    "AWS_REGION": "af-south-1",
}


def _refusals(env: dict[str, str]) -> list[str]:
    """Every problem the parse would refuse to start on, or an empty list."""
    try:
        Config.from_env(env)
    except ConfigError as exc:
        return list(exc.problems)
    return []


def _said(env: dict[str, str]) -> str:
    """Everything the parse tells a person without refusing to start, as one string."""
    return "\n".join(Config.from_env(env).notices)


class ASettingNobodyReadsIsRefusedRatherThanIgnored(unittest.TestCase):
    """The five spellings from the audit. Every one of them used to start perfectly.

    Refused rather than mentioned, and refused whether or not the real setting is also
    present: with the real one missing the service is quietly running the default, and with
    the real one there the file offers two answers and the reader believes the wrong one.
    """

    def _refused_naming(self, wrong: str, value: str, right: str) -> str:
        found = _refusals(_base(**{wrong: value}))
        self.assertTrue(
            any(wrong in problem for problem in found),
            f"{wrong}={value} started clean, exactly as it did before the fix: {found}",
        )
        said = next(problem for problem in found if wrong in problem)
        self.assertIn(
            right, said,
            "a person has to be shown the name they meant, not told to go and find it",
        )
        return said

    def test_the_gate_mode_typo_that_left_the_gate_in_shadow(self) -> None:
        self._refused_naming("GATE_MODEE", "on", "GATE_MODE")

    def test_the_engine_typo_that_left_the_other_engine_transcribing(self) -> None:
        self._refused_naming("TRANSCRIBE_ENGIN", "azure", "TRANSCRIBE_ENGINE")

    def test_the_naming_typo_that_left_the_name_out_of_the_transcript(self) -> None:
        self._refused_naming("NAMING_APPY", "true", "NAMING_APPLY")

    def test_the_starttls_typo_that_left_the_password_unprotected(self) -> None:
        self._refused_naming("SMTP_STARTLS", "false", "SMTP_STARTTLS")

    def test_the_archive_age_typo(self) -> None:
        self._refused_naming("ARCHIVE_AGE_DAY", "5", "ARCHIVE_AGE_DAYS")

    def test_the_sentence_says_what_the_service_is_actually_doing(self) -> None:
        """Not "unknown variable" — what is running instead, which is the actionable part."""
        said = self._refused_naming("GATE_MODEE", "on", "GATE_MODE")

        self.assertIn("nothing uses it", said)
        self.assertIn("default", said)

    def test_it_is_refused_even_when_the_real_setting_is_set_too(self) -> None:
        """Two lines, two answers. The one the reader believes is the one nothing reads."""
        said = self._refused_naming("GATE_MODEE", "on", "GATE_MODE")  # real one unset
        self.assertIn("default", said)

        both = _refusals(_base(GATE_MODE="off", GATE_MODEE="on"))
        with_real = next((p for p in both if "GATE_MODEE" in p), "")
        self.assertTrue(with_real, f"a typo beside the real setting was ignored: {both}")
        self.assertIn("that is what the service is doing", with_real)

    def test_the_file_that_starts_is_the_control_for_all_of_the_above(self) -> None:
        self.assertEqual(_refusals(_base()), [])


class AnOrdinaryHostStillStarts(unittest.TestCase):
    """The check that stops a service booting is worse than the bug it was written for.

    Nothing here fails against the old code — there was no check to fire — and that is the
    point: these are the tests that have to keep passing for the fix to be worth having.
    """

    def test_a_full_machine_environment_starts_without_a_word(self) -> None:
        env = dict(_HOST)
        env.update(_base())

        self.assertEqual(_refusals(env), [])

    def test_a_full_machine_environment_starts_with_routes_too(self) -> None:
        env = dict(_HOST)
        env.update(_routed())

        self.assertEqual(_refusals(env), [])

    def test_not_one_of_them_is_even_mentioned_in_the_notices(self) -> None:
        """A morning warning about somebody else's variable teaches a person to stop reading."""
        env = dict(_HOST)
        env.update(_base())
        said = _said(env)

        for name in sorted(_HOST):
            self.assertNotIn(name, said, f"{name} is not this service's business")

    def test_the_names_that_begin_the_way_ours_do(self) -> None:
        """These pass the family gate and must still be left entirely alone."""
        for name, value in (
            ("LANGUAGE", "en_ZA:en"),          # the locale; LANGUAGES is ours
            ("HTTP_PROXY", "http://proxy:3128"),  # beside HTTP_TIMEOUT_S
            ("MAX_JOBS", "2"),                 # beside MAX_RETRIES and MAX_ATTEMPTS
            ("LOG_DIR", "/var/log"),           # beside LOG_LEVEL
            ("GROUPS", "transcriber adm"),     # beside GROUP_FOLDER_ID
            ("SOURCE_DATE_EPOCH", "1700000000"),  # beside SOURCE_FOLDER_ID
            ("ENGINE_ROOM", "3"),              # beside every ENGINE_* setting we have
        ):
            with self.subTest(name=name):
                env = _base(**{name: value})

                self.assertEqual(_refusals(env), [])
                self.assertNotIn(name, _said(env))

    def test_an_empty_variable_named_like_ours_is_not_a_misspelling(self) -> None:
        """An unset line in a .env configures nothing, so there is nothing to correct."""
        self.assertEqual(_refusals(_base(GATE_MODEE="")), [])


class ARoutesOwnSettingsAreReadTheSameWay(unittest.TestCase):
    """A route is *pulled*: it asks for its seven names, and nothing looked at the rest.

    So a route's own settings are where this fault bites hardest — ``ROUTE_CALLS_ENABLD``
    said the route was off and the route went on watching, with nothing said anywhere.
    """

    def _refused_naming(self, wrong: str, value: str, right: str) -> str:
        found = _refusals(_routed(**{wrong: value}))
        self.assertTrue(
            any(wrong in problem for problem in found),
            f"{wrong}={value} started clean, exactly as it did before the fix: {found}",
        )
        said = next(problem for problem in found if wrong in problem)
        self.assertIn(right, said, f"the corrected name has to be shown: {said}")
        return said

    def test_the_route_that_went_on_watching_a_folder_marked_off(self) -> None:
        self._refused_naming("ROUTE_CALLS_ENABLD", "false", "ROUTE_CALLS_ENABLED")

    def test_the_route_left_with_no_archive_folder_at_all(self) -> None:
        self._refused_naming("ROUTE_CALLS_ARCHIV", "FOLDER-CALLS-OLD", "ROUTE_CALLS_ARCHIVE")

    def test_the_reviewer_that_was_never_assigned(self) -> None:
        """Held passages went to the service owner while the file named somebody else."""
        self._refused_naming(
            "ROUTE_CALLS_REVIEWR", "sipho@example.invalid", "ROUTE_CALLS_REVIEWER"
        )

    def test_the_suffix_one_letter_short(self) -> None:
        self._refused_naming("ROUTE_SITE_ENABLE", "false", "ROUTE_SITE_ENABLED")

    def test_the_route_name_itself_misspelt(self) -> None:
        said = self._refused_naming("ROUTE_SITES_ENABLED", "false", "ROUTE_SITE_ENABLED")

        self.assertIn("sites", said, "the name the line carries should be shown as well")

    def test_the_refusal_names_the_route_it_was_meant_for(self) -> None:
        said = self._refused_naming("ROUTE_CALLS_ENABLD", "false", "ROUTE_CALLS_ENABLED")

        self.assertIn("calls", said)

    def test_the_seven_real_settings_are_left_alone(self) -> None:
        """The control for the five above: a fully specified route is uneventful."""
        env = _routed(
            ROUTE_CALLS_ARCHIVE="FOLDER-CALLS-OLD",
            ROUTE_CALLS_ENGINE="openai",
            ROUTE_CALLS_ENABLED="true",
            ROUTE_CALLS_REVIEWER="sipho@example.invalid",
            ROUTE_SITE_REVIEWER="james@example.invalid",
        )

        self.assertEqual(_refusals(env), [])

    def test_a_leftover_from_a_deleted_route_is_said_but_not_refused(self) -> None:
        """Renaming a route leaves these behind. They do nothing, and nothing is at risk."""
        env = _routed(ROUTE_WHATSAPP_SOURCE="FOLDER-WA-IN")

        self.assertEqual(_refusals(env), [])
        self.assertIn("ROUTE_WHATSAPP_SOURCE", _said(env))

    def test_route_settings_in_a_file_with_no_routes_line_are_said_to_do_nothing(self) -> None:
        """Without ROUTES the single-folder variables are the whole configuration."""
        env = _base(ROUTE_DEFAULT_SOURCE="FOLDER-SOMEWHERE-ELSE")

        self.assertEqual(_refusals(env), [])
        said = _said(env)
        self.assertIn("ROUTE_DEFAULT_SOURCE", said)
        self.assertEqual(
            Config.from_env(env).source_folder_id, "FOLDER-SOURCE",
            "the notice has to describe what actually happens, which is that it is ignored",
        )


class NothingOfOursTravelsInClear(unittest.TestCase):
    """Three addresses this service sends something of its own to, and none was checked.

    An ``http://`` endpoint puts what travels over it in clear at every hop, and it is where
    an on-path attacker puts the redirect that sends the next request — key and all — to a
    host nobody chose. ``GATE_REVIEW_BASE_URL`` was refused without ``https://`` and these
    three, a few lines away, were not.
    """

    def _both_ways(self, name: str, insecure: str, secure: str) -> str:
        """Refused in plain http, accepted over https — both, so neither can pass alone."""
        found = _refusals(_base(**{name: insecure}))
        self.assertTrue(
            any(name in problem for problem in found),
            f"{name}={insecure} was accepted, exactly as it was before the fix: {found}",
        )
        self.assertEqual(
            _refusals(_base(**{name: secure})), [],
            f"{name} over https:// is the ordinary case and must still start",
        )
        return next(problem for problem in found if name in problem)

    def test_the_transcription_endpoint(self) -> None:
        said = self._both_ways(
            "ENGINE_BASE_URL",
            "http://transcribe.example.invalid/v1",
            "https://transcribe.example.invalid/v1",
        )

        self.assertIn("https://", said)
        self.assertIn("key", said, "it should say what is on the wire, not just refuse")

    def test_the_analysis_endpoint_carries_the_whole_unredacted_transcript(self) -> None:
        said = self._both_ways(
            "ANALYSIS_BASE_URL",
            "http://analysis.example.invalid",
            "https://analysis.example.invalid",
        )

        self.assertIn("transcript", said)

    def test_the_heartbeat_address_is_named_and_never_printed(self) -> None:
        """It carries an account identifier in its path, so it is a secret like any other."""
        said = self._both_ways(
            "HEARTBEAT_URL",
            "http://hc.example.invalid/9f2c-account-token",
            "https://hc.example.invalid/9f2c-account-token",
        )

        self.assertNotIn("9f2c-account-token", said)

    def test_a_mock_on_this_machine_can_be_allowed_deliberately(self) -> None:
        env = _base(
            ALLOW_PLAINTEXT_ENDPOINTS="true",
            ENGINE_BASE_URL="http://127.0.0.1:1234/v1",
        )

        self.assertEqual(_refusals(env), [])
        said = _said(env)
        self.assertIn("ENGINE_BASE_URL", said)
        self.assertIn("in the open", said)

    def test_the_opt_in_is_not_a_way_to_turn_the_check_off(self) -> None:
        """It allows plain http and nothing else — a scheme nobody meant is still refused."""
        for address in ("ftp://analysis.example.invalid", "analysis.example.invalid"):
            with self.subTest(address=address):
                found = _refusals(
                    _base(ALLOW_PLAINTEXT_ENDPOINTS="true", ANALYSIS_BASE_URL=address)
                )

                self.assertTrue(any("ANALYSIS_BASE_URL" in p for p in found), found)

    def test_the_opt_in_off_is_the_same_as_never_setting_it(self) -> None:
        found = _refusals(
            _base(ALLOW_PLAINTEXT_ENDPOINTS="false", ENGINE_BASE_URL="http://127.0.0.1:1234/v1")
        )

        self.assertTrue(any("ENGINE_BASE_URL" in p for p in found), found)

    def test_the_shipped_defaults_are_all_https(self) -> None:
        """Nothing about the ordinary file changes: the check has no default of its own."""
        config = Config.from_env(_base())

        self.assertTrue(config.analysis_base_url.startswith("https://"))
        self.assertEqual(config.engine_base_url, "")


class ACredentialWithNoExpiryDateSaysSo(unittest.TestCase):
    """A date nobody set read exactly like a countdown that had not started.

    An expired Entra client secret is the most likely way this service dies: a year of
    working perfectly, then nothing, on a morning nobody was warned about. Never a refusal —
    a service that will not start because a date is missing is a worse outcome than the
    silence.
    """

    def test_the_credentials_in_use_are_named(self) -> None:
        said = _said(_base())

        self.assertIn("No expiry date is set", said)
        for name in ("GRAPH_SECRET_EXPIRES_ON", "ENGINE_KEY_EXPIRES_ON",
                     "ANALYSIS_KEY_EXPIRES_ON"):
            self.assertIn(name, said)

    def test_a_date_that_is_filled_in_drops_out_of_the_sentence(self) -> None:
        """Otherwise it is a notice nobody can ever act their way out of."""
        said = _said(_base(GRAPH_SECRET_EXPIRES_ON="2099-01-01"))

        self.assertIn("No expiry date is set", said)
        self.assertNotIn("GRAPH_SECRET_EXPIRES_ON", said)
        self.assertIn("ENGINE_KEY_EXPIRES_ON", said)

    def test_all_three_filled_in_says_nothing_at_all(self) -> None:
        said = _said(
            _base(
                GRAPH_SECRET_EXPIRES_ON="2099-01-01",
                ENGINE_KEY_EXPIRES_ON="2099-02-01",
                ANALYSIS_KEY_EXPIRES_ON="2099-03-01",
            )
        )

        self.assertNotIn("No expiry date is set", said)

    def test_it_never_stops_the_service_starting(self) -> None:
        self.assertEqual(_refusals(_base()), [])

    def test_a_date_that_is_not_a_date_is_still_refused(self) -> None:
        """The behaviour that was already there, unchanged by the new notice."""
        found = _refusals(_base(GRAPH_SECRET_EXPIRES_ON="next March"))

        self.assertTrue(any("GRAPH_SECRET_EXPIRES_ON" in p for p in found), found)


class AQuotedValueMeansTheSameThingWhicheverWayItArrives(unittest.TestCase):
    """systemd and the wizard strip a surrounding quote pair. ``docker --env-file`` does not.

    So ``ROUTE_CALLS_ARCHIVE=""`` — how the wizard writes "this route is never archived" —
    arrived by one of the three paths as a two-character string and was taken for a
    driveItem id. Every monthly archive pass then failed against a folder that never
    existed, and which of the three loaders was in use is not something a person should have
    to know.
    """

    def test_an_empty_quoted_archive_is_not_a_folder(self) -> None:
        config = Config.from_env(_routed(ROUTE_CALLS_ARCHIVE='""'))
        route = config.route("calls")

        self.assertEqual(route.archive_folder_id, "")
        self.assertFalse(route.archives, "a pair of quotes was taken for a folder id")

    def test_single_quotes_the_same(self) -> None:
        route = Config.from_env(_routed(ROUTE_CALLS_ARCHIVE="''")).route("calls")

        self.assertEqual(route.archive_folder_id, "")

    def test_the_same_on_the_single_folder_form(self) -> None:
        """The file in the field has no ROUTES line, and it is quoted the same way."""
        config = Config.from_env(_base(ARCHIVE_FOLDER_ID='""'))

        self.assertEqual(config.archive_folder_id, "")
        self.assertFalse(config.routes[0].archives)

    def test_a_quoted_value_is_still_that_value(self) -> None:
        """Stripping the pair may not eat the setting: the two paths have to agree."""
        route = Config.from_env(_routed(ROUTE_CALLS_ARCHIVE='"FOLDER-CALLS-OLD"')).route("calls")

        self.assertEqual(route.archive_folder_id, "FOLDER-CALLS-OLD")
        self.assertTrue(route.archives)

    def test_a_quoted_top_level_setting_is_read_as_written(self) -> None:
        config = Config.from_env(_base(SMTP_HOST='"mail.example.invalid"', SMTP_PORT='"2525"'))

        self.assertEqual(config.smtp_host, "mail.example.invalid")
        self.assertEqual(config.smtp_port, 2525)

    def test_a_quoted_empty_required_setting_reads_as_missing(self) -> None:
        """Not as a one-character password: an empty value is an unset value, both ways."""
        found = _refusals(_base(SMTP_PASSWORD='""'))

        self.assertTrue(any("SMTP_PASSWORD is not set" in p for p in found), found)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
