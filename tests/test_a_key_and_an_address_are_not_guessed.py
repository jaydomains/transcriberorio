"""Two places the service filled in a blank it should have left blank, or left one filled.

Each sits directly under a comment describing the opposite behaviour, which is how both
survived: the file tells the next reader the rule is already enforced.
"""

from __future__ import annotations

import unittest

from transcriber.config import Config, ConfigError
from transcriber.logging_setup import SecretScrubber

_BASE = dict(
    GRAPH_TENANT_ID="t", GRAPH_CLIENT_ID="c", GRAPH_CLIENT_SECRET="s", GRAPH_USER_ID="u",
    TRANSCRIBE_ENGINE="openai", OPENAI_API_KEY="sk-fake-openai-key-for-this-test-0000000000",
    SMTP_HOST="h", SMTP_USER="u", SMTP_PASSWORD="p", SMTP_FROM="f@example.co",
    SMTP_TO="t@example.co", HEARTBEAT_URL="https://hc.example/x",
    SOURCE_FOLDER_ID="s", OUTPUT_FOLDER_ID="o", LEDGER_PATH=":memory:", WORK_DIR="/tmp",
)


def _complains_about_the_analysis_key(**extra: str) -> bool:
    env = dict(_BASE)
    env.update(extra)
    try:
        Config.from_env(env)
    except ConfigError as exc:
        return "ANALYSIS_API_KEY" in str(exc)
    return False


class AnOpenAIKeyIsNotAnAnthropicKey(unittest.TestCase):
    """The fallback fired on the key's mere presence, whoever the analysis pass calls.

    The shipped default analysis provider is Anthropic, so an OpenAI key was accepted as the
    Anthropic credential and the "not set" problem was struck off the list. The service then
    starts clean and every analysis call comes back 401 — which reads like the provider
    having a bad morning rather than like a setting nobody set.
    """

    def test_the_default_provider_does_not_take_an_openai_key(self) -> None:
        self.assertTrue(
            _complains_about_the_analysis_key(),
            "an OpenAI key was silently accepted as the Anthropic credential",
        )

    def test_but_it_is_taken_when_the_provider_really_is_openai(self) -> None:
        self.assertFalse(_complains_about_the_analysis_key(ANALYSIS_PROVIDER="openai"))

    def test_and_when_the_base_url_says_so(self) -> None:
        self.assertFalse(
            _complains_about_the_analysis_key(ANALYSIS_BASE_URL="https://api.openai.com/v1")
        )

    def test_an_explicit_key_is_always_enough(self) -> None:
        self.assertFalse(
            _complains_about_the_analysis_key(ANALYSIS_API_KEY="sk-ant-fake-key-000000000000")
        )


class TheLogGetsTheSameThreeFiltersAsEverythingElse(unittest.TestCase):
    """An address is stripped from the ledger, the email and every file by three filters.

    The log was getting one of them. OneDrive writes the drive owner's own address into
    every webUrl as `/personal/james_kbc_co_za/` — the encoding OWNER_PATH_RE exists for
    precisely because no address check can see it — so it went to stdout and into the
    journal on every line carrying a URL.
    """

    def setUp(self) -> None:
        self.scrub = SecretScrubber()

    def test_the_owner_path_encoding_is_removed(self) -> None:
        line = "fetched https://kbc-my.sharepoint.com/personal/james_kbc_co_za/Documents/a.m4a"
        self.assertNotIn("james_kbc_co_za", self.scrub.scrub(line))

    def test_a_dictated_address_is_removed(self) -> None:
        self.assertNotIn("example dot co", self.scrub.scrub("he said carel at example dot co dot za"))

    def test_an_ordinary_address_is_still_removed(self) -> None:
        self.assertNotIn("@", self.scrub.scrub("mailed müller@site.co.za about the slab"))

    def test_and_ordinary_log_lines_are_untouched(self) -> None:
        line = "downloaded 01ABC -> /var/cache/transcriber/01ABC.m4a (9400000 bytes)"
        self.assertEqual(self.scrub.scrub(line), line)


if __name__ == "__main__":
    unittest.main()
