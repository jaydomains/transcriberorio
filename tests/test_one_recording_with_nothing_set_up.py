"""`transcriber try` — one recording, before any of the plumbing exists.

The whole point of this command is that a person can find out whether the service is any
good on their own speech before they have an app registration, a mail server or a ledger.
Everything below defends one of the two promises that makes that safe to do:

  * **it needs two keys and nothing else.** If it ever grows a Microsoft or SMTP
    dependency, the command stops being usable at the moment it exists to be usable at.
    That is checked by reading this module's own source, not by a list somebody maintains —
    a hand-kept list of forbidden imports is exactly the kind of list that misses the
    seventh one.
  * **it writes nothing it was not asked to write.** No ledger file, no output folder, no
    stray temp file in the working directory.

And one that makes it useful rather than merely safe: **work already paid for is never
thrown away.** By the time the reading runs, the transcription has been billed. A failure
after that point — a wrong analysis key, a filename the output contract refuses — must
leave the transcript in the person's hands, not take it down with it.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tempfile
import types
import unittest
from contextlib import redirect_stdout

from transcriber import audio, tryout
from transcriber.extract import (
    AnalysisAuthError,
    AnalysisTransportError,
    Extraction,
    Routing,
    Spend,
)
from transcriber.models import Segment, Transcript

ENGINE_KEY = "sk-engine-must-never-be-printed"
ANALYSIS_KEY = "sk-analysis-must-never-be-printed"

SPEECH = (
    "Ja so on the Blsa job the slab pours Tuesday if the rebar is signed off. "
    "Danie says the polycarb sheeting is two weeks out. "
)


def _recording(tmp: str, name: str = "Call recording Danie_250815_143012.m4a",
               *, duration_s: float = 1320.0, truncated: bool = False) -> str:
    path = os.path.join(tmp, name)
    data = (audio.truncated_mp4_bytes(duration_s=duration_s) if truncated
            else audio.build_mp4_bytes(duration_s=duration_s))
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _transcript(text: str = SPEECH * 30, *, end: float = 1310.0) -> Transcript:
    return Transcript(text=text, segments=[Segment(start=0.0, end=end, text=text[:60])],
                      language="en", engine="openai")


def _extraction(*spend: Spend) -> Extraction:
    return Extraction(
        routing=Routing(label="substantive", forced=False, model="claude-haiku-4-5",
                        one_line="Slab pour and a sheeting lead time."),
        summary="The slab pours Tuesday subject to rebar sign-off.",
        site="Blsa",
        spend=spend or (Spend("claude-haiku-4-5", 2400, 180),
                        Spend("claude-opus-5", 4000, 1500, 3200)),
        models_used=("claude-haiku-4-5", "claude-opus-5"),
    )


class _Fakes:
    """The two network calls, replaced. Everything else in the path is the real thing."""

    def __init__(self, transcript: Transcript | None = None, extraction: object = None,
                 transcribe_error: Exception | None = None,
                 analysis_error: Exception | None = None) -> None:
        self.transcript = transcript if transcript is not None else _transcript()
        self.extraction = extraction if extraction is not None else _extraction()
        self.transcribe_error = transcribe_error
        self.analysis_error = analysis_error
        self.hints_seen: list[object] = []
        self.config_seen: list[object] = []

    def __enter__(self) -> "_Fakes":
        self._saved = (tryout.create_engine, tryout.transcribe, tryout.Extractor)
        outer = self

        def create_engine(config):
            outer.config_seen.append(config)
            return types.SimpleNamespace(name=config.engine, max_bytes=None)

        def transcribe(engine, path, hints, *, duration_s=None, work_dir=None):
            outer.hints_seen.append(hints)
            if outer.transcribe_error is not None:
                raise outer.transcribe_error
            return outer.transcript

        class Extractor:
            @classmethod
            def from_config(cls, config, **kwargs):
                outer.config_seen.append(config)
                return cls()

            def extract(self, transcript, hints=None, **kwargs):
                if outer.analysis_error is not None:
                    raise outer.analysis_error
                return outer.extraction

        tryout.create_engine = create_engine
        tryout.transcribe = transcribe
        tryout.Extractor = Extractor
        return self

    def __exit__(self, *exc: object) -> None:
        tryout.create_engine, tryout.transcribe, tryout.Extractor = self._saved


def _run(path: str, **kwargs: object) -> tryout.TryResult:
    return tryout.run_one(path, engine_name="openai", engine_key=ENGINE_KEY,
                          analysis_key=ANALYSIS_KEY, **kwargs)  # type: ignore[arg-type]


class ItNeedsTwoKeysAndNothingElse(unittest.TestCase):
    #: Everything this command must not touch, because each one needs a setting the
    #: command exists to do without: Microsoft credentials, a mail server, a ledger file,
    #: the held-passage store, the archive move, the digest.
    OFF_LIMITS = frozenset({
        "graph", "smtplib", "email", "ledger", "withheld", "digest", "archive",
        "heartbeat", "worker", "pipeline", "sweep", "review_server", "erase", "group",
    })

    def test_the_module_imports_nothing_that_needs_setting_up(self) -> None:
        """Read the imports, not the prose.

        A hand-kept list of forbidden names is exactly the kind of list that misses the
        next one, and a plain substring search over the source matches the docstring's own
        sentences about what this module does NOT do. So the check parses the module and
        looks at what it actually imports.
        """
        with open(tryout.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
                imported.update(alias.name for alias in node.names)

        reached = {name for name in imported if name.lower() in self.OFF_LIMITS}
        self.assertEqual(reached, set(),
                         f"tryout.py imports {sorted(reached)}, which needs setup this "
                         "command exists to do without")

    def test_that_check_would_actually_fire(self) -> None:
        """A guard nobody has seen fail is a guard nobody knows works."""
        tree = ast.parse("from .graph import Graph\nimport smtplib\n")
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual({n for n in imported if n.lower() in self.OFF_LIMITS},
                         {"graph", "smtplib"})

    def test_a_run_never_builds_a_real_credential_for_anything_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes() as fakes:
                _run(_recording(tmp))
        config = fakes.config_seen[0]
        self.assertEqual(config.engine_key, ENGINE_KEY)
        self.assertEqual(config.analysis_api_key, ANALYSIS_KEY)
        # Everything else is still the obviously-fake offline value, so a mistake here
        # fails at the first call rather than doing something plausible to a real account.
        self.assertEqual(config.graph_client_secret, "offline-not-a-secret")
        self.assertEqual(config.smtp_password, "offline-not-a-secret")
        self.assertEqual(config.ledger_path, ":memory:")

    def test_the_engine_key_follows_the_engine(self) -> None:
        """Switching engines must not silently reuse the previous engine's key."""
        env = {"OPENAI_API_KEY": "open", "ELEVENLABS_API_KEY": "eleven",
               "ANALYSIS_API_KEY": "analysis"}
        self.assertEqual(tryout.keys_from_environment("openai", env)[0], "open")
        self.assertEqual(tryout.keys_from_environment("elevenlabs", env)[0], "eleven")

    def test_a_missing_key_names_the_variable_that_holds_it(self) -> None:
        with self.assertRaises(tryout.TryError) as caught:
            tryout.keys_from_environment("openai", {"ANALYSIS_API_KEY": "y"})
        self.assertIn("OPENAI_API_KEY", str(caught.exception))
        # And it says where to put it, because the obvious place is the wrong one.
        self.assertIn("shell history", str(caught.exception))

    def test_an_engine_nobody_has_heard_of_is_refused_before_anything_runs(self) -> None:
        with self.assertRaises(tryout.TryError) as caught:
            tryout.keys_from_environment("whisper.cpp", {})
        self.assertIn("openai", str(caught.exception))


class AzureNeedsARegionAndSaysWhichVariableHoldsIt(unittest.TestCase):
    def test_azure_without_a_region_is_refused_before_a_byte_is_spent(self) -> None:
        """The engine's own message is about a base url and a region. This one can name
        the variable to set, and it fires before the audio is uploaded anywhere."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _recording(tmp)
            with _Fakes() as fakes:
                with self.assertRaises(tryout.TryError) as caught:
                    tryout.run_one(path, engine_name="azure", engine_key=ENGINE_KEY,
                                   analysis_key=ANALYSIS_KEY)
        self.assertIn("AZURE_SPEECH_REGION", str(caught.exception))
        self.assertEqual(fakes.hints_seen, [], "the audio was sent before the check ran")

    def test_a_region_reaches_the_config_the_engine_is_built_from(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes() as fakes:
                tryout.run_one(_recording(tmp), engine_name="azure", engine_key=ENGINE_KEY,
                               analysis_key=ANALYSIS_KEY, region="westeurope")
        self.assertEqual(fakes.config_seen[0].azure_region, "westeurope")


class ItWritesNothingItWasNotAskedTo(unittest.TestCase):
    def test_a_run_with_no_out_directory_leaves_nothing_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio_dir = os.path.join(tmp, "audio")
            work = os.path.join(tmp, "empty")
            os.makedirs(audio_dir)
            os.makedirs(work)
            path = _recording(audio_dir)
            before = sorted(os.listdir(audio_dir))
            here = os.getcwd()
            os.chdir(work)
            try:
                with _Fakes():
                    result = _run(path)
                self.assertEqual(sorted(os.listdir(work)), [])
            finally:
                os.chdir(here)
            self.assertEqual(sorted(os.listdir(audio_dir)), before)
        self.assertEqual(len(result.files), 3)

    def test_out_writes_exactly_the_three_files_and_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
            out = os.path.join(tmp, "out")
            written = tryout.write_files(result, out)
            self.assertEqual(len(written), 3)
            self.assertEqual(sorted(os.listdir(out)),
                             sorted(f.name for f in result.files))
            for path, rendered in zip(written, result.files):
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), rendered.text)


class NeitherKeyIsEverPrinted(unittest.TestCase):
    def test_no_key_reaches_the_report_or_any_rendered_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
            printed = tryout.render_report(result, full_transcript=True)
        for key in (ENGINE_KEY, ANALYSIS_KEY):
            self.assertNotIn(key, printed)
            for rendered in result.files:
                self.assertNotIn(key, rendered.text)


class WorkAlreadyPaidForIsNeverThrownAway(unittest.TestCase):
    def test_a_refused_analysis_key_still_hands_back_the_transcript(self) -> None:
        """The transcription was billed before the reading ran. Losing it would mean
        paying twice to find out the second key was wrong."""
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes(analysis_error=AnalysisAuthError("401 from the analysis API")):
                result = _run(_recording(tmp))

        self.assertIsNone(result.extraction)
        self.assertIn(SPEECH.strip()[:40], result.transcript.text)
        # And the note names the variable holding the key that was refused. "401" on its
        # own does not say WHICH of the two keys is wrong, which is the only question a
        # person has at that moment.
        refused = [n for n in result.notes if "REFUSED" in n]
        self.assertEqual(len(refused), 1, result.notes)
        self.assertIn("ANALYSIS_API_KEY", refused[0])
        self.assertIn("not the same key as the transcription engine", refused[0])
        self.assertIn("does not need transcribing again", refused[0])
        # The three files still render — with the transcript, which is the evidence, and
        # without a summary, which is the reading.
        self.assertEqual(len(result.files), 3)
        transcript_file = result.file("transcript")
        self.assertIn(SPEECH.strip()[:40], transcript_file.text)

    def test_an_analysis_failure_that_is_not_a_key_says_so_generically(self) -> None:
        """Only the auth case gets the key sentence. A timeout is not a wrong key, and
        telling somebody to check their key when their network dropped wastes an hour."""
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes(analysis_error=AnalysisTransportError("connection reset")):
                result = _run(_recording(tmp))
        failed = [n for n in result.notes if "THE READING FAILED" in n]
        self.assertEqual(len(failed), 1, result.notes)
        self.assertNotIn("ANALYSIS_API_KEY", failed[0])
        self.assertIsNotNone(result.file("transcript"))

    def test_a_failed_reading_prints_the_whole_transcript_not_a_preview(self) -> None:
        long_text = " ".join(f"line {n} of the site walk." for n in range(400))
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes(transcript=_transcript(long_text),
                        analysis_error=AnalysisAuthError("401")):
                result = _run(_recording(tmp))
            printed = tryout.render_report(result)
        self.assertNotIn("Pass --full", printed)

    def test_the_cost_line_does_not_read_as_free_when_nothing_was_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes(analysis_error=AnalysisAuthError("401")):
                result = _run(_recording(tmp))
            printed = tryout.render_report(result)
        self.assertIn("the reading did not run", printed)
        self.assertNotIn("$0.0000", printed)


class ATruncatedRecordingIsSaidOutLoud(unittest.TestCase):
    def test_it_transcribes_anyway_and_says_the_service_would_not_have(self) -> None:
        """Refusing here would teach nothing about the transcription, which is the whole
        question. Being quiet about it would be worse than either."""
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp, truncated=True))
        self.assertTrue(result.audio.truncated)
        loud = [n for n in result.notes if "CUT OFF" in n]
        self.assertEqual(len(loud), 1, result.notes)
        self.assertIn("quarantined", loud[0])
        self.assertEqual(len(result.files), 3)
        self.assertIn("CUT OFF", tryout.render_report(result))


class TheCostIsTheReadingAndSaysSo(unittest.TestCase):
    def test_the_transcription_is_not_folded_into_the_figure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
            printed = tryout.render_report(result)
        self.assertIn("READING only", printed)
        self.assertIn("bills by the minute", printed)

    def test_a_model_with_no_price_reads_as_an_undercount_not_as_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes(extraction=_extraction(Spend("claude-haiku-4-5", 2400, 180),
                                               Spend("some-new-model", 9000, 4000))):
                result = _run(_recording(tmp))
            printed = tryout.render_report(result)
        self.assertEqual(result.unpriced, ("some-new-model",))
        self.assertIn("UNDERCOUNT", printed)
        self.assertIn("not in the price list", printed)

    def test_every_figure_carries_the_day_the_prices_were_read(self) -> None:
        from transcriber import prices
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
            self.assertIn(prices.CHECKED_ON, tryout.render_report(result))


class TheFilesAreThePipelinesOwn(unittest.TestCase):
    def test_all_three_kinds_come_back_with_distinct_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
        self.assertEqual([f.kind for f in result.files],
                         ["transcript", "summary", "actions"])
        self.assertEqual(len({f.name for f in result.files}), 3)

    def test_the_filename_dates_the_recording_not_today(self) -> None:
        """The same rule as the running service: the phone's clock wins over the file's."""
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
        for rendered in result.files:
            self.assertIn("20250815-143012", rendered.name)

    def test_a_name_with_no_moment_in_it_falls_back_to_the_files_own_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _recording(tmp, "site walk with the plumber.m4a")
            os.utime(path, (1_700_000_000, 1_700_000_000))
            with _Fakes():
                result = _run(path)
        self.assertEqual(len(result.files), 3)
        for rendered in result.files:
            self.assertTrue(re.match(r"_?2023111[45]-", rendered.name), rendered.name)

    def test_the_hints_carry_the_counterparty_the_filename_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes() as fakes:
                _run(_recording(tmp))
        self.assertEqual(fakes.hints_seen[0].counterparty, "Danie")

    def test_vocabulary_and_languages_reach_the_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes() as fakes:
                _run(_recording(tmp), vocabulary=("Blsa", "polycarb"),
                     languages=("af-ZA", "en-ZA"))
        hints = fakes.hints_seen[0]
        self.assertEqual(hints.vocabulary, ("Blsa", "polycarb"))
        self.assertEqual(hints.languages, ("af-ZA", "en-ZA"))


class TheReportSaysWhatItDidNotTest(unittest.TestCase):
    def test_the_gate_not_running_is_stated_on_every_run(self) -> None:
        """Silence here would read as 'nothing in this recording was sensitive'."""
        with tempfile.TemporaryDirectory() as tmp:
            with _Fakes():
                result = _run(_recording(tmp))
            printed = tryout.render_report(result)
        self.assertIn("sensitivity gate did not run", printed)
        self.assertIn("nothing was published", printed)


class TheCommandLineRunsIt(unittest.TestCase):
    def test_try_reads_both_keys_from_the_environment_and_prints_a_report(self) -> None:
        from transcriber.__main__ import main

        with tempfile.TemporaryDirectory() as tmp:
            path = _recording(tmp)
            out = os.path.join(tmp, "out")
            saved = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "ANALYSIS_API_KEY")}
            os.environ["OPENAI_API_KEY"] = ENGINE_KEY
            os.environ["ANALYSIS_API_KEY"] = ANALYSIS_KEY
            buffer = io.StringIO()
            try:
                with _Fakes():
                    with redirect_stdout(buffer):
                        code = main(["try", path, "--out", out])
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            printed = buffer.getvalue()
            self.assertEqual(code, 0)
            self.assertEqual(len(os.listdir(out)), 3)
        self.assertIn("What it read", printed)
        self.assertNotIn(ENGINE_KEY, printed)
        self.assertNotIn(ANALYSIS_KEY, printed)

    def test_a_missing_key_is_a_sentence_and_a_non_zero_exit(self) -> None:
        from transcriber.__main__ import main

        saved = {k: os.environ.pop(k, None) for k in ("OPENAI_API_KEY", "ANALYSIS_API_KEY")}
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = main(["try", "/does/not/matter.m4a"])
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
