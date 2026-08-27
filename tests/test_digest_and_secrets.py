"""The 06:00 digest: sent on good days too, and never carrying an address or a secret.

The digest is the only thing that says the service is alive. A report that arrives only
when something breaks is indistinguishable from a service that has died, so a quiet night
still sends — and says so in the subject line, which is where he reads it, on his phone,
before opening anything.

It is also the one piece of rendered output that leaves the building by email, which makes
it the place where the two absolute rules are most easily broken: it must carry no address
and no credential, however loudly it is reporting a failure.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber import digest
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, State

from . import support
from .vendored_ingest import ADDR_RE


class TheSubjectLineCarriesTheWholeMessage(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def discover(self, *ids: str) -> str:
        self.ledger.record_page(
            [DriveItem(item_id=i, name=f"Call Carel_260827_1200{n:02d}.m4a") for n, i in enumerate(ids)],
            "cursor-1",
        )
        return self.ledger.get(ids[0]).discovered_at[:10]

    def build(self, day: str) -> digest.Digest:
        return digest.build(self.config, self.ledger, day=day)

    def test_a_good_day_still_sends_and_says_all_done(self) -> None:
        day = self.discover("A", "B", "C")
        for item in ("A", "B", "C"):
            self.ledger.advance(item, State.DONE)

        built = self.build(day)

        self.assertEqual(built.subject, "Recordings: all 3 done")
        self.assertFalse(built.needs_a_person)

    def test_a_bad_day_leads_with_the_failures(self) -> None:
        day = self.discover("A", "B", "C")
        self.ledger.advance("A", State.DONE)
        self.ledger.quarantine("B", "the audio is truncated: no moov index")
        self.ledger.quarantine("C", "the transcript is implausible for the duration")

        built = self.build(day)

        self.assertEqual(built.subject, "Recordings: 1 done, 2 FAILED")
        self.assertTrue(built.needs_a_person)
        self.assertIn("NEEDS YOU", built.body)
        self.assertLess(
            built.body.index("NEEDS YOU"),
            built.body.index("WHAT ARRIVED"),
            "the counts were put above the failures; failures come first",
        )
        # And the reason is in plain words, with the technical detail kept underneath it.
        self.assertIn("the phone ran out of battery or storage", built.body)
        self.assertIn("Technical detail: the audio is truncated", built.body)

    def test_a_silent_night_is_flagged_rather_than_reported_as_success(self) -> None:
        """Nothing arriving is not the same as nothing going wrong."""
        built = self.build("2026-08-26")

        self.assertIn("nothing arrived yesterday", built.subject)
        self.assertTrue(built.needs_a_person)

    def test_a_recording_still_in_flight_yesterday_counts_as_a_failure_this_morning(self) -> None:
        day = self.discover("A", "B")
        self.ledger.advance("A", State.DONE)

        built = self.build(day)

        self.assertIn("FAILED", built.subject)
        self.assertEqual(built.open_failures, 1)

    def test_verified_silence_is_reported_as_itself(self) -> None:
        day = self.discover("A", "B")
        self.ledger.advance("A", State.DONE)
        self.ledger.advance("B", State.SKIPPED_EMPTY, skipped_reason="9s of verified silence")

        built = self.build(day)
        self.assertEqual(built.subject, "Recordings: all 2 done (1 silent)")


class TheDigestNeverCarriesAnAddressOrASecret(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(
            work_dir=self.dir.name,
            graph_client_secret="SECRET-graph-value",
            analysis_api_key="SECRET-analysis-value",
            smtp_password="SECRET-smtp-value",
            smtp_to=("james@example.co.za",),
            smtp_from="digest@example.co.za",
        )
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def test_no_address_survives_into_the_body_even_from_an_error_message(self) -> None:
        self.ledger.record_page([DriveItem(item_id="A", name="Call Carel_260827_120055.m4a")], "c1")
        day = self.ledger.get("A").discovered_at[:10]
        self.ledger.quarantine("A", "the engine replied: unknown recipient carel@example.co.za")

        built = digest.build(self.config, self.ledger, day=day)

        self.assertEqual(ADDR_RE.findall(built.body), [])
        self.assertEqual(ADDR_RE.findall(built.subject), [])
        self.assertIn("[address removed]", built.body)

    def test_no_configured_secret_survives_into_the_body(self) -> None:
        self.ledger.record_page([DriveItem(item_id="A", name="one.m4a")], "c1")
        day = self.ledger.get("A").discovered_at[:10]
        self.ledger.quarantine("A", "graph rejected the key SECRET-graph-value outright")

        built = digest.build(self.config, self.ledger, day=day)

        for secret in ("SECRET-graph-value", "SECRET-analysis-value", "SECRET-smtp-value"):
            self.assertNotIn(secret, built.body, f"{secret} reached the digest")

    def test_the_config_repr_and_safe_dict_hold_nothing_worth_stealing(self) -> None:
        text = repr(self.config)
        for secret in ("SECRET-graph-value", "SECRET-analysis-value", "SECRET-smtp-value"):
            self.assertNotIn(secret, text)
        self.assertEqual(ADDR_RE.findall(text), [])

        safe = self.config.safe_dict()
        self.assertEqual(ADDR_RE.findall(repr(safe)), [])
        for secret in ("SECRET-graph-value", "SECRET-analysis-value", "SECRET-smtp-value"):
            self.assertNotIn(secret, repr(safe))

    def test_a_sweep_report_folded_into_the_digest_is_scrubbed_too(self) -> None:
        """The sweep's findings travel out inside the digest, so they are scrubbed with it."""
        from transcriber.sweep import SweepReport

        self.ledger.record_page([DriveItem(item_id="A", name="one.m4a")], "c1")
        day = self.ledger.get("A").discovered_at[:10]
        report = SweepReport(started_at="2026-08-27T01:00:00Z", finished_at="2026-08-27T01:00:09Z")
        report.add(
            "unrecognised", "B", "note.m4a",
            "left alone; a person named carel@example.co.za owns it", needs_a_person=True,
        )

        built = digest.build(self.config, self.ledger, day=day, sweep_report=report)

        self.assertEqual(ADDR_RE.findall(built.body), [])
        self.assertIn("unrecognised", built.body)

    def test_scrub_removes_a_live_secret_from_a_string_about_to_be_logged(self) -> None:
        cleaned = self.config.scrub("failed with key SECRET-analysis-value for james@example.co.za")
        self.assertNotIn("SECRET-analysis-value", cleaned)
        self.assertEqual(ADDR_RE.findall(cleaned), [])


class ConfigFailsWithEveryProblemAtOnce(unittest.TestCase):
    """Nine missing variables must be nine lines, not nine restarts."""

    def test_every_missing_variable_is_named_in_one_message(self) -> None:
        from transcriber.config import Config, ConfigError

        with self.assertRaises(ConfigError) as raised:
            Config.from_env({})

        problems = raised.exception.problems
        self.assertGreater(len(problems), 5)
        for expected in ("GRAPH_TENANT_ID", "SOURCE_FOLDER_ID", "LEDGER_PATH", "SMTP_HOST"):
            self.assertTrue(
                any(expected in problem for problem in problems),
                f"{expected} was not reported as missing",
            )

    def test_each_problem_says_what_the_variable_is_for(self) -> None:
        from transcriber.config import Config, ConfigError

        with self.assertRaises(ConfigError) as raised:
            Config.from_env({})
        for problem in raised.exception.problems:
            self.assertIn(" — ", problem, f"a problem with no explanation: {problem!r}")


if __name__ == "__main__":
    unittest.main()
