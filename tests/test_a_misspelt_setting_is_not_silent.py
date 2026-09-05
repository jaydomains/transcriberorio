"""A setting nobody reads used to load clean and do the opposite of what the file said.

``GATE_MODEE=on`` left the gate in shadow, withholding nothing while whoever wrote the line
believed passages were being held back. ``ROUTE_CALLS_ENABLD=false`` left the route
watching. ``TRANSCRIBE_ENGIN=azure`` left the engine on openai. Every one of them started
without a word, because the settings are *pulled* — each one is asked for by name and
nothing ever looked at what else was in the file. ``transcriber config set`` refuses an
unknown name, but SETUP.md and ops/DEPLOY.md both say to copy ``.env.example`` and edit it
by hand, which never goes near that command.

The other half of this file is the reason the check has to be narrow. Under systemd the
``.env`` is loaded with ``EnvironmentFile=``, so what ``from_env`` actually sees is the
file's names plus systemd's own — ``INVOCATION_ID``, ``JOURNAL_STREAM``,
``STATE_DIRECTORY`` — plus whatever the shell exports. A check that refused anything it did
not recognise would refuse to start on an ordinary host, which is a worse fault than the one
being fixed. So the tests below matter in both directions: the typos are refused, and a
perfectly normal machine still starts.
"""

from __future__ import annotations

import unittest

from transcriber.config import Config, ConfigError

_BASE = dict(
    GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c", GRAPH_CLIENT_SECRET="s", GRAPH_USER_ID="u",
    TRANSCRIBE_ENGINE="openai", OPENAI_API_KEY="not-a-real-engine-key",
    ANALYSIS_API_KEY="not-a-real-analysis-key",
    SMTP_HOST="h", SMTP_USER="u", SMTP_PASSWORD="p", SMTP_FROM="f@example.co",
    SMTP_TO="t@example.co", HEARTBEAT_URL="https://hc.example/x",
    SOURCE_FOLDER_ID="s", OUTPUT_FOLDER_ID="o", LEDGER_PATH=":memory:",
)

#: What a real machine adds to the environment before the .env is even read: systemd's own
#: variables (ops/transcriber.service loads the file with EnvironmentFile=), the shell's,
#: and the ones a container or a proxy puts there. None of them is this service's business.
_HOST = dict(
    PATH="/usr/bin:/bin", HOME="/var/lib/transcriber", LANG="en_ZA.UTF-8",
    LANGUAGE="en_ZA:en", LC_ALL="C.UTF-8", PWD="/opt/transcriber", SHELL="/usr/sbin/nologin",
    USER="transcriber", LOGNAME="transcriber", TERM="xterm", SHLVL="1", HOSTNAME="site-vm",
    TZ="Africa/Johannesburg", PYTHONPATH="/opt/transcriber/src", PYTHONUNBUFFERED="1",
    INVOCATION_ID="9f2c", JOURNAL_STREAM="8:12345", SYSTEMD_EXEC_PID="812",
    STATE_DIRECTORY="/var/lib/transcriber", CACHE_DIRECTORY="/var/cache/transcriber",
    RUNTIME_DIRECTORY="/run/transcriber", CONFIGURATION_DIRECTORY="/etc/transcriber",
    LOGS_DIRECTORY="/var/log/transcriber", MAINPID="812", MANAGERPID="1", LISTEN_FDS="0",
    NOTIFY_SOCKET="/run/notify", WATCHDOG_PID="812", WATCHDOG_USEC="30000000",
    XDG_RUNTIME_DIR="/run/user/0", SSH_CONNECTION="10.0.0.1 22", MAIL="/var/mail/x",
    EDITOR="vi", TMPDIR="/tmp", VIRTUAL_ENV="/opt/venv", HTTP_PROXY="http://proxy:3128",
    HTTPS_PROXY="http://proxy:3128", NO_PROXY="localhost", AWS_REGION="af-south-1",
    GROUP="transcriber", GROUPS="transcriber", MAX_JOBS="2", LOG_DIR="/var/log",
)

_ROUTED = {k: v for k, v in _BASE.items() if not k.endswith("_FOLDER_ID")}
_ROUTED.update(
    ROUTES="calls,site",
    ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
    ROUTE_SITE_SOURCE="S-SITE", ROUTE_SITE_OUTPUT="O-SITE",
)


def _problems(base: dict, **overrides: str) -> list[str]:
    env = dict(base)
    env.update(overrides)
    try:
        Config.from_env(env)
    except ConfigError as exc:
        return list(exc.problems)
    return []


def _notices(base: dict, **overrides: str) -> str:
    env = dict(base)
    env.update(overrides)
    return " ".join(Config.from_env(env).notices)


class AMisspeltSettingIsRefusedRatherThanIgnored(unittest.TestCase):
    """Each of these was reproduced against the old code: it started, and did the opposite."""

    def _refused(self, name: str, value: str, meant: str) -> None:
        found = _problems(dict(_BASE, **_HOST), **{name: value})
        self.assertTrue(
            any(name in p and meant in p for p in found),
            f"{name}={value} started clean and {meant} was never mentioned: {found}",
        )

    def test_the_gate_mode_typo_that_left_it_in_shadow(self) -> None:
        self._refused("GATE_MODEE", "on", "GATE_MODE")

    def test_the_engine_typo_that_left_the_other_engine_running(self) -> None:
        self._refused("TRANSCRIBE_ENGIN", "azure", "TRANSCRIBE_ENGINE")

    def test_the_naming_typo_that_left_naming_off(self) -> None:
        self._refused("NAMING_APPY", "true", "NAMING_APPLY")

    def test_the_starttls_typo(self) -> None:
        self._refused("SMTP_STARTLS", "false", "SMTP_STARTTLS")

    def test_the_archive_age_typo(self) -> None:
        self._refused("ARCHIVE_AGE_DAY", "5", "ARCHIVE_AGE_DAYS")

    def test_the_problem_says_what_the_service_is_actually_doing(self) -> None:
        """A person has to be able to act on it without reading the code."""
        found = " ".join(_problems(_BASE, GATE_MODEE="on"))

        self.assertIn("nothing uses it", found)
        self.assertIn("running at its default", found)
        self.assertIn("Correct the spelling to GATE_MODE", found)


class AnOrdinaryHostStillStarts(unittest.TestCase):
    """The check is worth nothing if it stops the service starting on a normal machine."""

    def test_systemd_and_shell_variables_are_not_our_business(self) -> None:
        self.assertEqual(_problems(dict(_BASE, **_HOST)), [])

    def test_the_locale_variable_one_letter_from_ours_is_left_alone(self) -> None:
        """LANGUAGE is the machine's locale. LANGUAGES is ours. They are not related."""
        self.assertEqual(_problems(_BASE, LANGUAGE="en_ZA:en"), [])

    def test_nothing_unrecognised_is_even_mentioned(self) -> None:
        said = _notices(dict(_BASE, **_HOST))

        for name in ("HTTP_PROXY", "LOG_DIR", "MAX_JOBS", "GROUP", "JOURNAL_STREAM"):
            self.assertNotIn(name, said, "a warning every morning about somebody else's "
                                         "variable teaches a person to stop reading them")


class ARoutesOwnSettingsAreCheckedTheSameWay(unittest.TestCase):
    """A route has seven settings and will never have others, so an eighth is a typo."""

    def test_a_route_that_stayed_watching(self) -> None:
        found = _problems(_ROUTED, ROUTE_CALLS_ENABLD="false")

        self.assertTrue(any("ROUTE_CALLS_ENABLD" in p for p in found), found)
        self.assertTrue(any("ROUTE_CALLS_ENABLED" in p for p in found),
                        f"the correction has to be shown, not described: {found}")

    def test_a_route_that_was_given_no_archive_folder(self) -> None:
        found = _problems(_ROUTED, ROUTE_CALLS_ARCHIV="A-CALLS")

        self.assertTrue(any("ROUTE_CALLS_ARCHIVE" in p for p in found), found)

    def test_a_reviewer_that_was_discarded(self) -> None:
        found = _problems(_ROUTED, ROUTE_CALLS_REVIEWR="sipho@example.invalid")

        self.assertTrue(any("ROUTE_CALLS_REVIEWER" in p for p in found), found)

    def test_a_suffix_one_letter_short_on_a_real_route(self) -> None:
        """ROUTE_SITE_ENABLE can only be a typo: there is no such setting and there never will be."""
        found = _problems(_ROUTED, ROUTE_SITE_ENABLE="false")

        self.assertTrue(any("ROUTE_SITE_ENABLE" in p and "ROUTE_SITE_ENABLED" in p
                            for p in found), found)

    def test_a_misspelt_route_name_is_refused_with_the_real_one(self) -> None:
        found = _problems(_ROUTED, ROUTE_SITES_ENABLED="false")

        self.assertTrue(any("ROUTE_SITE_ENABLED" in p for p in found), found)
        self.assertTrue(any("'sites'" in p for p in found),
                        f"the name it carries should be shown: {found}")

    def test_a_leftover_from_a_deleted_route_is_a_notice_not_a_refusal(self) -> None:
        """A renamed route leaves these behind. They do nothing, and nothing is at risk."""
        self.assertEqual(_problems(_ROUTED, ROUTE_WHATSAPP_SOURCE="S-WA"), [])
        self.assertIn("ROUTE_WHATSAPP_SOURCE", _notices(_ROUTED, ROUTE_WHATSAPP_SOURCE="S-WA"))

    def test_the_stray_reviewer_notice_is_unchanged(self) -> None:
        said = _notices(_ROUTED, ROUTE_NOSUCH_REVIEWER="sipho@example.invalid")

        self.assertIn("assign nobody", said)
        self.assertIn("ROUTE_NOSUCH_REVIEWER", said)

    def test_route_folders_without_a_routes_line_are_said_to_be_ignored(self) -> None:
        """Without ROUTES the single-folder variables are what the service reads."""
        said = _notices(_BASE, ROUTE_DEFAULT_SOURCE="S-OTHER")

        self.assertIn("ROUTE_DEFAULT_SOURCE", said)
        self.assertIn("not read at all", said)

    def test_every_real_route_setting_is_left_alone(self) -> None:
        self.assertEqual(
            _problems(
                _ROUTED,
                ROUTE_CALLS_LABEL="Phone calls", ROUTE_CALLS_ARCHIVE="A-CALLS",
                ROUTE_CALLS_ENGINE="openai", ROUTE_CALLS_ENABLED="true",
                ROUTE_CALLS_REVIEWER="sipho@example.invalid",
            ),
            [],
        )


class AnApiKeyDoesNotTravelInCleartext(unittest.TestCase):
    """GATE_REVIEW_BASE_URL was refused without https://. Four lines away, these were not.

    An http:// endpoint puts the key on the wire in clear at every hop, and it is where an
    on-path attacker puts the redirect that sends the next request, key and all, to a host
    nobody chose.
    """

    def test_the_engine_endpoint_must_be_https(self) -> None:
        found = _problems(_BASE, ENGINE_BASE_URL="http://someone-elses-host.invalid/v1")

        self.assertTrue(any("ENGINE_BASE_URL" in p and "https://" in p for p in found), found)

    def test_the_analysis_endpoint_must_be_https(self) -> None:
        found = _problems(_BASE, ANALYSIS_BASE_URL="http://someone-elses-host.invalid")

        self.assertTrue(any("ANALYSIS_BASE_URL" in p for p in found), found)
        self.assertTrue(any("transcript" in p for p in found),
                        f"it should say what travels over it: {found}")

    def test_the_heartbeat_must_be_https_and_is_never_printed(self) -> None:
        """It carries an account identifier, so it is named and never shown."""
        found = _problems(_BASE, HEARTBEAT_URL="http://hc.example/an-account-token")

        self.assertTrue(any("HEARTBEAT_URL" in p for p in found), found)
        self.assertFalse(any("an-account-token" in p for p in found),
                         "the address itself must not be printed")

    def test_a_local_mock_can_be_allowed_deliberately_and_is_said_out_loud(self) -> None:
        env = dict(_BASE, ALLOW_PLAINTEXT_ENDPOINTS="true",
                   ENGINE_BASE_URL="http://localhost:1234/v1")

        self.assertEqual(_problems(env), [])
        self.assertIn("in the open", _notices(env))

    def test_the_opt_in_does_not_allow_anything_that_is_not_http(self) -> None:
        found = _problems(_BASE, ALLOW_PLAINTEXT_ENDPOINTS="true",
                          ENGINE_BASE_URL="ftp://someone-elses-host.invalid")

        self.assertTrue(any("ENGINE_BASE_URL" in p for p in found), found)

    def test_the_documented_defaults_are_untouched(self) -> None:
        self.assertEqual(_problems(_BASE), [])


class AQuotedValueMeansTheSameThingByEveryRoute(unittest.TestCase):
    """systemd's EnvironmentFile and the wizard's reader strip a quote pair; --env-file does not."""

    def test_an_empty_quoted_archive_is_not_a_folder(self) -> None:
        config = Config.from_env(dict(_ROUTED, ROUTE_CALLS_ARCHIVE='""'))

        self.assertEqual(config.route("calls").archive_folder_id, "")
        self.assertFalse(config.route("calls").archives,
                         'a two-character string of quotes was taken for a driveItem id')

    def test_a_quoted_value_is_the_value(self) -> None:
        config = Config.from_env(dict(_BASE, SMTP_HOST='"mail.example.invalid"'))

        self.assertEqual(config.smtp_host, "mail.example.invalid")

    def test_a_quoted_empty_required_setting_reads_as_missing(self) -> None:
        found = _problems(_BASE, GRAPH_CLIENT_SECRET='""')

        self.assertTrue(any("GRAPH_CLIENT_SECRET is not set" in p for p in found), found)


class ACredentialWithNoExpiryDateSaysSo(unittest.TestCase):
    """A countdown nobody set looked exactly like one that has not started.

    An expired Entra client secret is the single most likely way this service dies: a year
    of working perfectly, then nothing, on a morning nobody was warned about.
    """

    def test_an_unset_date_is_a_notice_every_day(self) -> None:
        said = _notices(_BASE)

        self.assertIn("GRAPH_SECRET_EXPIRES_ON", said)
        self.assertIn("No expiry date is set", said)

    def test_a_date_that_is_set_says_nothing(self) -> None:
        said = _notices(
            _BASE,
            GRAPH_SECRET_EXPIRES_ON="2099-01-01",
            ENGINE_KEY_EXPIRES_ON="2099-01-01",
            ANALYSIS_KEY_EXPIRES_ON="2099-01-01",
        )

        self.assertNotIn("No expiry date is set", said)

    def test_it_never_starts_and_it_never_stops_anything(self) -> None:
        """It is a notice: nothing here is a reason to refuse to start."""
        self.assertEqual(_problems(_BASE), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
