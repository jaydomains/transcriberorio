"""Routes, as the environment describes them: what is read, and what is refused.

The service used to watch one folder and write to one folder. It now runs N routes, and
every test here is a way that change could quietly lose or destroy a recording.

Three of them matter more than the rest:

  * **the ``.env`` in the field has no ``ROUTES``.** It has ``SOURCE_FOLDER_ID`` and its two
    companions and nothing else, and it must keep working untouched, as exactly one route
    called ``default`` — the same name the ledger's migration gives the rows already in the
    database, or an upgraded installation would come back up with its whole history filed
    under a route nobody can name;
  * **an output folder that is somebody's source folder is a feedback loop.** The service
    would read its own transcripts back in as recordings and transcribe them again, for as
    long as nobody notices. It is the one misconfiguration here that is genuinely
    destructive, so it is refused across the whole set and the error names *both* routes —
    "one of these two folders is wrong" is only actionable if you are told which two;
  * **two routes sharing one output folder is fine, and must stay fine.** Pooling several
    kinds of recording into one folder is a thing he asked for by name. A validation that
    forbade it on grounds of tidiness would be this test suite's fault, so it is asserted
    as an allowance rather than left to nobody's attention.
"""

from __future__ import annotations

import unittest

from transcriber.config import Config, ConfigError
from transcriber.models import DEFAULT_ROUTE, Route


BASE = {
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
    "SMTP_FROM": "digest@invalid",
    "SMTP_TO": "someone@invalid",
    "HEARTBEAT_URL": "https://example.invalid/beat",
    "LEDGER_PATH": ":memory:",
}


def env(**overrides: str) -> dict[str, str]:
    """A complete environment with nothing real in it, plus whatever this test needs.

    Built explicitly rather than read from the ambient process, so no test can pick up a
    live credential and none of them depends on what is exported in a shell.
    """
    values = dict(BASE)
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def problems_from(**overrides: str) -> list[str]:
    """Every complaint this environment produces, or an empty list if it is usable."""
    try:
        Config.from_env(env(**overrides))
    except ConfigError as exc:
        return list(exc.problems)
    return []


class ALegacyEnvIsStillAWholeConfiguration(unittest.TestCase):
    """No ``ROUTES``, three folder variables: the shape of every installation in the field.

    This is not a deprecated path being tolerated. It is what is deployed, and an upgrade
    that needed the file edited before the service would start again would be an outage
    dressed up as a feature.
    """

    def test_one_route_called_default(self) -> None:
        config = Config.from_env(
            env(SOURCE_FOLDER_ID="CALLS", OUTPUT_FOLDER_ID="TRANSCRIPTS",
                ARCHIVE_FOLDER_ID="OLD")
        )

        self.assertEqual(len(config.routes), 1, "a single-folder .env is exactly one route")
        route = config.routes[0]
        self.assertEqual(route.name, DEFAULT_ROUTE)
        self.assertEqual(route.name, "default", "the name the ledger's migration writes")
        self.assertEqual(route.source_folder_id, "CALLS")
        self.assertEqual(route.output_folder_id, "TRANSCRIPTS")
        self.assertEqual(route.archive_folder_id, "OLD")
        self.assertTrue(route.enabled)
        self.assertEqual(route.engine, "", "no override: the service default transcribes it")

    def test_the_old_attribute_names_still_read(self) -> None:
        """Nothing that reads ``config.source_folder_id`` breaks before it is migrated."""
        config = Config.from_env(
            env(SOURCE_FOLDER_ID="CALLS", OUTPUT_FOLDER_ID="TRANSCRIPTS",
                ARCHIVE_FOLDER_ID="OLD")
        )

        self.assertEqual(config.source_folder_id, "CALLS")
        self.assertEqual(config.output_folder_id, "TRANSCRIPTS")
        self.assertEqual(config.archive_folder_id, "OLD")

    def test_it_is_an_enabled_route_so_the_service_watches_something(self) -> None:
        config = Config.from_env(env(SOURCE_FOLDER_ID="CALLS", OUTPUT_FOLDER_ID="TRANSCRIPTS"))

        self.assertEqual([r.name for r in config.enabled_routes], ["default"])
        self.assertEqual(config.route("default"), config.routes[0])
        self.assertIsNone(config.route("site-meetings"))

    def test_an_archive_folder_is_optional_and_its_absence_is_a_decision(self) -> None:
        config = Config.from_env(
            env(SOURCE_FOLDER_ID="CALLS", OUTPUT_FOLDER_ID="TRANSCRIPTS", ARCHIVE_FOLDER_ID="")
        )

        self.assertEqual(config.routes[0].archive_folder_id, "")
        self.assertFalse(config.routes[0].archives, "no archive folder means it never archives")

    def test_a_config_built_in_code_is_also_one_route(self) -> None:
        """The wizard, ``offline()`` and every test build a Config directly, not from env."""
        config = Config(source_folder_id="A", output_folder_id="B", archive_folder_id="C")

        self.assertEqual([r.name for r in config.routes], ["default"])
        self.assertEqual(config.routes[0].source_folder_id, "A")

    def test_writing_a_legacy_attribute_moves_the_route_with_it(self) -> None:
        """The two halves of one fact cannot drift: that is how a transcript is misfiled."""
        config = Config(source_folder_id="A", output_folder_id="B")
        config.output_folder_id = "SOMEWHERE-ELSE"

        self.assertEqual(config.routes[0].output_folder_id, "SOMEWHERE-ELSE")
        self.assertEqual(config.output_folder_id, "SOMEWHERE-ELSE")


class RoutesDeclaredInTheEnvironment(unittest.TestCase):
    def test_three_routes_read_their_own_variables(self) -> None:
        config = Config.from_env(
            env(
                ROUTES="calls,site-meetings,whatsapp",
                ROUTE_CALLS_LABEL="Phone calls",
                ROUTE_CALLS_SOURCE="S-CALLS",
                ROUTE_CALLS_OUTPUT="O-CALLS",
                ROUTE_CALLS_ARCHIVE="A-CALLS",
                ROUTE_SITE_MEETINGS_LABEL="Site meetings",
                ROUTE_SITE_MEETINGS_SOURCE="S-SITE",
                ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
                ROUTE_WHATSAPP_LABEL="WhatsApp voice notes",
                ROUTE_WHATSAPP_SOURCE="S-WA",
                ROUTE_WHATSAPP_OUTPUT="O-WA",
                ROUTE_WHATSAPP_ENABLED="false",
            )
        )

        self.assertEqual(list(config.route_names), ["calls", "site-meetings", "whatsapp"])
        # The hyphenated slug reads its variables from the underscored stem, and nothing
        # else: a route that looked for ROUTE_SITE-MEETINGS_SOURCE would find nothing and
        # start up watching an empty string.
        site = config.route("site-meetings")
        self.assertEqual(site.source_folder_id, "S-SITE")
        self.assertEqual(site.label, "Site meetings")
        self.assertEqual(site.env_var("SOURCE"), "ROUTE_SITE_MEETINGS_SOURCE")
        self.assertEqual([r.name for r in config.enabled_routes], ["calls", "site-meetings"])
        self.assertFalse(config.route("whatsapp").enabled)

    def test_a_paused_route_keeps_its_folders(self) -> None:
        """Pausing is not deleting: everything it needs to be switched back on is still here."""
        config = Config.from_env(
            env(
                ROUTES="calls,whatsapp",
                ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
                ROUTE_WHATSAPP_SOURCE="S-WA", ROUTE_WHATSAPP_OUTPUT="O-WA",
                ROUTE_WHATSAPP_ENABLED="off",
            )
        )

        whatsapp = config.route("whatsapp")
        self.assertFalse(whatsapp.enabled)
        self.assertEqual(whatsapp.source_folder_id, "S-WA")
        self.assertEqual(whatsapp.output_folder_id, "O-WA")

    def test_the_legacy_variables_are_ignored_and_said_so_out_loud(self) -> None:
        """Both forms present. Silently preferring one leaves somebody editing the wrong line."""
        config = Config.from_env(
            env(
                ROUTES="calls",
                ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
                SOURCE_FOLDER_ID="OLD-SOURCE", OUTPUT_FOLDER_ID="OLD-OUTPUT",
            )
        )

        self.assertEqual(config.source_folder_id, "S-CALLS", "ROUTES wins, as documented")
        self.assertTrue(config.notices, "a file whose settings are ignored must say so")
        notice = " ".join(config.notices)
        self.assertIn("SOURCE_FOLDER_ID", notice)
        self.assertIn("OUTPUT_FOLDER_ID", notice)
        self.assertIn("ignored", notice)

    def test_a_route_may_override_the_engine_and_needs_that_engine_s_key(self) -> None:
        config = Config.from_env(
            env(
                ROUTES="calls,whatsapp",
                ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
                ROUTE_WHATSAPP_SOURCE="S-WA", ROUTE_WHATSAPP_OUTPUT="O-WA",
                ROUTE_WHATSAPP_ENGINE="elevenlabs",
                ELEVENLABS_API_KEY="not-a-real-scribe-key",
            )
        )

        self.assertEqual(config.engine_for(config.route("calls")), "openai")
        self.assertEqual(config.engine_for(config.route("whatsapp")), "elevenlabs")
        self.assertEqual(config.engine_key_for("whatsapp"), "not-a-real-scribe-key")

    def test_an_override_with_no_key_is_refused_at_startup(self) -> None:
        found = problems_from(
            ROUTES="whatsapp",
            ROUTE_WHATSAPP_SOURCE="S-WA", ROUTE_WHATSAPP_OUTPUT="O-WA",
            ROUTE_WHATSAPP_ENGINE="elevenlabs",
        )

        self.assertTrue(
            any("ELEVENLABS_API_KEY" in p for p in found),
            f"a route that cannot transcribe must be caught before the first recording: {found}",
        )

    def test_an_unusable_slug_is_refused_with_the_name_it_should_have_been(self) -> None:
        found = problems_from(ROUTES="Site Meetings")

        self.assertTrue(found)
        joined = " ".join(found)
        self.assertIn("Site Meetings", joined)
        self.assertIn("site-meetings", joined, "the error should show the name, not describe it")

    def test_the_same_route_named_twice_is_refused(self) -> None:
        found = problems_from(
            ROUTES="calls,calls",
            ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
        )

        self.assertTrue(any("twice" in p for p in found), found)


class TheFeedbackLoopIsRefused(unittest.TestCase):
    """The one validation that prevents a genuinely destructive misconfiguration.

    Point a route's output folder at any route's source folder and the service reads its
    own transcripts back in as new recordings, transcribes them, writes those transcripts
    into the same folder, and does it again. Nothing downstream would ever say so.
    """

    def test_one_route_writing_into_the_folder_another_watches(self) -> None:
        found = problems_from(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_LABEL="Phone calls",
            ROUTE_CALLS_SOURCE="S-CALLS",
            ROUTE_CALLS_OUTPUT="S-SITE",           # the loop: site-meetings watches this
            ROUTE_SITE_MEETINGS_LABEL="Site meetings",
            ROUTE_SITE_MEETINGS_SOURCE="S-SITE",
            ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
        )

        self.assertTrue(found, "an output folder that is a source folder must not start")
        loop = [p for p in found if "transcripts" in p and "recordings" in p]
        self.assertTrue(loop, f"the loop was not the complaint: {found}")
        message = " ".join(loop)
        # Both routes, by name. "One of your folders is wrong" is not something anybody
        # can act on; "calls writes into the folder site-meetings watches" is.
        self.assertIn("calls", message)
        self.assertIn("site-meetings", message)
        self.assertIn("Phone calls", message)
        self.assertIn("Site meetings", message)

    def test_a_route_writing_into_its_own_source_folder(self) -> None:
        found = problems_from(
            ROUTES="calls",
            ROUTE_CALLS_LABEL="Phone calls",
            ROUTE_CALLS_SOURCE="SAME",
            ROUTE_CALLS_OUTPUT="SAME",
        )

        self.assertTrue(any("watches" in p for p in found), found)
        self.assertIn("calls", " ".join(found))

    def test_the_loop_is_caught_even_when_the_watching_route_is_paused(self) -> None:
        """A paused route is a folder somebody switches back on without re-reading the file."""
        found = problems_from(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_SOURCE="S-CALLS",
            ROUTE_CALLS_OUTPUT="S-SITE",
            ROUTE_SITE_MEETINGS_SOURCE="S-SITE",
            ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
            ROUTE_SITE_MEETINGS_ENABLED="false",
        )

        self.assertTrue(
            any("site-meetings" in p and "recordings" in p for p in found),
            f"a paused route's folder is still a folder: {found}",
        )

    def test_the_legacy_shape_catches_it_too(self) -> None:
        found = problems_from(SOURCE_FOLDER_ID="SAME", OUTPUT_FOLDER_ID="SAME")

        self.assertTrue(any("recordings" in p for p in found), found)


class PoolingSeveralInputsIntoOneOutputIsAllowed(unittest.TestCase):
    """He asked for this explicitly. It must not be forbidden for tidiness."""

    def test_two_routes_may_share_one_output_folder(self) -> None:
        config = Config.from_env(
            env(
                ROUTES="calls,site-meetings",
                ROUTE_CALLS_SOURCE="S-CALLS",
                ROUTE_CALLS_OUTPUT="EVERYTHING",
                ROUTE_SITE_MEETINGS_SOURCE="S-SITE",
                ROUTE_SITE_MEETINGS_OUTPUT="EVERYTHING",
            )
        )

        self.assertEqual(
            [r.output_folder_id for r in config.routes], ["EVERYTHING", "EVERYTHING"]
        )
        self.assertEqual(len(config.enabled_routes), 2)

    def test_three_routes_may_share_one_output_folder(self) -> None:
        config = Config.from_env(
            env(
                ROUTES="calls,site-meetings,whatsapp",
                ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="POOL",
                ROUTE_SITE_MEETINGS_SOURCE="S-SITE", ROUTE_SITE_MEETINGS_OUTPUT="POOL",
                ROUTE_WHATSAPP_SOURCE="S-WA", ROUTE_WHATSAPP_OUTPUT="POOL",
            )
        )

        self.assertEqual({r.output_folder_id for r in config.routes}, {"POOL"})

    def test_a_shared_archive_folder_is_also_allowed(self) -> None:
        """Same reasoning: one archive of aged originals is a filing choice, not a fault."""
        config = Config.from_env(
            env(
                ROUTES="calls,site-meetings",
                ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
                ROUTE_CALLS_ARCHIVE="OLD",
                ROUTE_SITE_MEETINGS_SOURCE="S-SITE", ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
                ROUTE_SITE_MEETINGS_ARCHIVE="OLD",
            )
        )

        self.assertEqual({r.archive_folder_id for r in config.routes}, {"OLD"})


class OneSourceFolderBelongsToOneRoute(unittest.TestCase):
    """Two cursors over one folder is two claims on one recording."""

    def test_the_same_source_on_two_enabled_routes_is_refused(self) -> None:
        found = problems_from(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_LABEL="Phone calls",
            ROUTE_CALLS_SOURCE="SHARED",
            ROUTE_CALLS_OUTPUT="O-CALLS",
            ROUTE_SITE_MEETINGS_LABEL="Site meetings",
            ROUTE_SITE_MEETINGS_SOURCE="SHARED",
            ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
        )

        clash = [p for p in found if "same folder" in p]
        self.assertTrue(clash, f"two routes watching one folder must be refused: {found}")
        message = " ".join(clash)
        self.assertIn("Phone calls", message)
        self.assertIn("Site meetings", message)

    def test_a_paused_route_may_keep_the_folder_of_a_route_that_replaced_it(self) -> None:
        """Only *enabled* routes contend. A paused one has no cursor moving."""
        config = Config.from_env(
            env(
                ROUTES="calls,calls-old",
                ROUTE_CALLS_SOURCE="SHARED", ROUTE_CALLS_OUTPUT="O-NEW",
                ROUTE_CALLS_OLD_SOURCE="SHARED", ROUTE_CALLS_OLD_OUTPUT="O-OLD",
                ROUTE_CALLS_OLD_ENABLED="false",
            )
        )

        self.assertEqual([r.name for r in config.enabled_routes], ["calls"])


class EveryProblemIsReportedAtOnce(unittest.TestCase):
    """Nine restarts to find nine problems is how a deployment gets abandoned halfway."""

    def test_several_faults_come_back_together(self) -> None:
        found = problems_from(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_SOURCE="SHARED",
            ROUTE_CALLS_OUTPUT="",                 # no output
            ROUTE_SITE_MEETINGS_SOURCE="SHARED",   # and the same folder as calls
            ROUTE_SITE_MEETINGS_OUTPUT="O-SITE",
        )

        self.assertGreaterEqual(len(found), 2, found)
        self.assertTrue(any("ROUTE_CALLS_OUTPUT" in p for p in found), found)
        self.assertTrue(any("same folder" in p for p in found), found)

    def test_no_enabled_route_at_all_will_not_start(self) -> None:
        found = problems_from(
            ROUTES="calls",
            ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
            ROUTE_CALLS_ENABLED="false",
        )

        self.assertTrue(any("switched off" in p for p in found), found)

    def test_a_missing_folder_names_the_variable_the_operator_must_edit(self) -> None:
        """Naming ROUTE_DEFAULT_SOURCE at somebody whose file says SOURCE_FOLDER_ID sends
        them looking for a setting that is not there."""
        legacy = problems_from(SOURCE_FOLDER_ID="", OUTPUT_FOLDER_ID="O")
        self.assertTrue(any("SOURCE_FOLDER_ID" in p for p in legacy), legacy)

        declared = problems_from(ROUTES="calls", ROUTE_CALLS_OUTPUT="O-CALLS")
        self.assertTrue(any("ROUTE_CALLS_SOURCE" in p for p in declared), declared)

    def test_an_archive_folder_that_is_a_source_or_an_output_is_refused(self) -> None:
        into_source = problems_from(
            ROUTES="calls",
            ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
            ROUTE_CALLS_ARCHIVE="S-CALLS",
        )
        self.assertTrue(any("discovered all over again" in p for p in into_source), into_source)

        into_output = problems_from(
            ROUTES="calls",
            ROUTE_CALLS_SOURCE="S-CALLS", ROUTE_CALLS_OUTPUT="O-CALLS",
            ROUTE_CALLS_ARCHIVE="O-CALLS",
        )
        self.assertTrue(any("archive" in p for p in into_output), into_output)


class TheRouteRecordItself(unittest.TestCase):
    def test_a_route_is_frozen(self) -> None:
        """Read by the worker, the pipeline, the archive and the digest, on several threads."""
        route = Route(name="calls", source_folder_id="S")
        with self.assertRaises(Exception):
            route.source_folder_id = "SOMEWHERE-ELSE"  # type: ignore[misc]

    def test_it_is_named_in_a_sentence_without_repeating_itself(self) -> None:
        self.assertEqual(Route(name="calls", label="Phone calls").display, "Phone calls")
        self.assertEqual(Route(name="calls").display, "calls")

    def test_the_env_stem_is_the_slug_uppercased_with_hyphens_as_underscores(self) -> None:
        route = Route(name="site-meetings")
        self.assertEqual(route.env_stem, "SITE_MEETINGS")
        self.assertEqual(route.env_var("ARCHIVE"), "ROUTE_SITE_MEETINGS_ARCHIVE")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
