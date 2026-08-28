"""The setup wizard.

Every test here is a bug that actually happened, found by running the wizard rather than by
reading it. The first is the one that matters: ``ask()`` returned the answer and never
stored it, so the wizard walked all the way through, printed a cheerful summary, and wrote a
completely empty ``.env``. It looked like it worked.
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import tempfile
import unittest

from transcriber import setup_wizard as wiz


class _FakeIn:
    """Feeds scripted answers and records what was asked, without a terminal."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    def __call__(self, _prompt=""):
        self.asked += 1
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def _ctx(values=None, **kw):
    return wiz._Ctx(values=dict(values or {}), style=wiz._Style(False), **kw)


class AnAnswerIsActuallyStored(unittest.TestCase):
    """The wizard wrote an empty .env: ask() returned the value but never kept it."""

    def test_a_typed_answer_reaches_the_values(self) -> None:
        ctx = _ctx()
        wiz.getpass.getpass = _FakeIn([])  # never used here
        original = __builtins__["input"] if isinstance(__builtins__, dict) else __builtins__.input
        try:
            import builtins

            builtins.input = _FakeIn(["tenant-123"])
            with contextlib.redirect_stdout(io.StringIO()):
                got = ctx.ask("GRAPH_TENANT_ID", "Tenant?")
        finally:
            import builtins

            builtins.input = original
        self.assertEqual(got, "tenant-123")
        self.assertEqual(ctx.values["GRAPH_TENANT_ID"], "tenant-123", "the answer was discarded")

    def test_an_empty_optional_answer_clears_a_stale_value(self) -> None:
        import builtins

        ctx = _ctx({"ORPHAN_FOLDER_ID": "left-over"})
        original = builtins.input
        try:
            builtins.input = _FakeIn([""])
            # An existing value is offered as the default, so it takes two: blank then blank.
            ctx.values["ORPHAN_FOLDER_ID"] = ""
            builtins.input = _FakeIn([""])
            with contextlib.redirect_stdout(io.StringIO()):
                got = ctx.ask("ORPHAN_FOLDER_ID", "Orphan folder?", required=False)
        finally:
            builtins.input = original
        self.assertEqual(got, "")
        self.assertEqual(ctx.values["ORPHAN_FOLDER_ID"], "")

    def test_a_closed_stdin_stops_rather_than_spinning(self) -> None:
        import builtins

        ctx = _ctx()
        original = builtins.input
        try:
            builtins.input = _FakeIn([])  # immediate EOF
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ctx.ask("GRAPH_TENANT_ID", "Tenant?")
        finally:
            builtins.input = original


class SecretsAreNeverShown(unittest.TestCase):
    def test_mask_keeps_only_the_tail(self) -> None:
        self.assertEqual(wiz.mask("sk-ant-abcdefgh1234"), "••••••••1234")
        self.assertEqual(wiz.mask("abc"), "•••")
        self.assertEqual(wiz.mask(""), "")

    def test_a_remembered_secret_is_scrubbed_from_output(self) -> None:
        ctx = _ctx()
        ctx.remember_secret("sk-live-supersecret")
        self.assertNotIn("sk-live-supersecret", ctx._clean("failed with key sk-live-supersecret"))

    def test_a_failing_check_cannot_print_the_secret(self) -> None:
        ctx = _ctx(assume_yes=True)
        ctx.remember_secret("sk-live-supersecret")
        buf = io.StringIO()
        import contextlib

        def boom() -> str:
            raise RuntimeError("401 from https://api/x?key=sk-live-supersecret")

        with contextlib.redirect_stdout(buf):
            ctx.check("the key", boom)
        self.assertNotIn("sk-live-supersecret", buf.getvalue())
        self.assertIn("••••", buf.getvalue())


class TheEnvFileRoundTrips(unittest.TestCase):
    def test_write_then_load_is_lossless(self) -> None:
        values = {
            "GRAPH_TENANT_ID": "abc-123",
            "SMTP_TO": "a@example.invalid, b@example.invalid",
            "GRAPH_CLIENT_SECRET": 'has spaces "quotes" and #hash',
            "ANALYSIS_MODEL_STRONG": "claude-opus-5",
            "WEIRD_EMPTY": "",
        }
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            wiz.write_env_file(path, values)
            back = wiz.load_env_file(path)
        for key, value in values.items():
            self.assertEqual(back.get(key), value, f"{key} did not survive the round trip")

    def test_the_file_is_never_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            wiz.write_env_file(path, {"GRAPH_CLIENT_SECRET": "s3cret"})
            mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, 0o600, f"a file holding a client secret was mode {mode:o}")

    def test_no_temp_file_is_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            wiz.write_env_file(path, {"A": "1"})
            self.assertEqual(sorted(os.listdir(d)), [".env"])

    def test_a_hand_edited_file_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write(
                    "# a comment\n"
                    "export GRAPH_TENANT_ID=abc\n"
                    "SMTP_PORT = 587\n"
                    'SMTP_FROM="bot@example.invalid"\n'
                    "\n"
                    "BROKEN LINE WITHOUT EQUALS\n"
                )
            back = wiz.load_env_file(path)
        self.assertEqual(back["GRAPH_TENANT_ID"], "abc")
        self.assertEqual(back["SMTP_PORT"], "587")
        self.assertEqual(back["SMTP_FROM"], "bot@example.invalid")
        self.assertNotIn("BROKEN", "".join(back))


class TheChosenModelsAreTheDocumentedOnes(unittest.TestCase):
    """A hallucinated model id fails at 06:00, not here — so pin them."""

    def test_the_tiers_name_real_documented_models(self) -> None:
        allowed = {"claude-haiku-4-5", "claude-opus-5"}
        for _key, _label, cheap, strong in wiz._ANALYSIS_TIERS:
            self.assertIn(cheap, allowed)
            self.assertIn(strong, allowed)

    def test_the_recommended_tier_is_first(self) -> None:
        self.assertEqual(wiz._ANALYSIS_TIERS[0][0], "balanced")
        self.assertIn("recommended", wiz._ANALYSIS_TIERS[0][1])


class EveryRequiredSettingIsAsked(unittest.TestCase):
    """The wizard finishing must mean the service can start.

    It once skipped all three folder ids whenever the drive could not be listed, so a
    completed run still produced a .env the service refused to boot from.
    """

    def test_the_wizard_covers_every_required_config_var(self) -> None:
        from transcriber import config as config_mod

        required = {
            v.env
            for v in config_mod._SPEC
            if getattr(v, "default", None) is getattr(config_mod, "_REQUIRED", object())
        }
        asked = {name for _group, members in wiz._GROUPS for name in members}
        # Engine keys are conditional on the chosen engine; the wizard asks the right one.
        conditional = {"OPENAI_API_KEY", "ELEVENLABS_API_KEY", "AZURE_SPEECH_KEY",
                       "AZURE_SPEECH_REGION"}
        missing = required - asked - conditional
        self.assertEqual(missing, set(), f"the wizard never asks for: {sorted(missing)}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
