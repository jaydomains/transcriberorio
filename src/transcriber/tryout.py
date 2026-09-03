"""One recording, start to finish, with nothing set up.

The service is built to run unattended: it finds recordings in OneDrive, transcribes them,
reads them, publishes three files and emails a digest. That shape needs thirteen settings
before it will start — Microsoft credentials, a mail server, a heartbeat URL — and every one
of them is about running by itself for months.

None of that is needed to answer the question a person actually asks first, which is
**"what does it do to one of my recordings?"**

So this module does exactly that and nothing else. Point it at an audio file on disk and it
transcribes it, reads it, and shows the three files it would have published. It needs two
keys and no Microsoft anything, which matters because the app registration is the one step
that can be held up by somebody else's IT department — and there is no reason to wait on
that to find out whether the transcription is any good on South African site speech.

**It publishes nothing and records nothing.** No ledger, no OneDrive, no email, no held
store, no archive move. The only thing it will ever write is the three rendered files, into
a directory the caller names, because reading a summary in a terminal is not reading it.
That is the whole contract, and it is what makes this safe to run against a real recording
of a real conversation before anybody has decided anything.

**What it will not tell you.** It cannot tell you whether the polling finds your recordings,
whether the archive pass moves them, or whether the morning email arrives — those need the
full setup, and they are also the parts least likely to surprise anybody. It does not run
the sensitivity gate, so nothing is held back here that the running service would hold back.
What it will tell you is the thing worth knowing early: whether the words come back right,
whether what the reader makes of them is worth having, and what the reading cost.
"""

from __future__ import annotations

import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from . import naming, outputs, prices
from .audio import probe
from .config import ENGINE_KEY_VARS, Config
from .engines import create_engine, transcribe
from .extract import AnalysisAuthError, AnalysisError, Extractor
from .models import AudioInfo, Hints, Transcript

log = logging.getLogger(__name__)

__all__ = ["TryError", "TryResult", "keys_from_environment", "render_report", "run_one",
           "write_files"]

#: How much of the transcript the report prints inline. A site walk runs to thousands of
#: words and nobody reads those in a terminal; ``write_files`` is how you read the whole
#: thing. The count is generous enough that a short call prints in full.
TRANSCRIPT_PREVIEW_LINES = 40


class TryError(RuntimeError):
    """Something about the request itself was wrong, said in a whole sentence."""


@dataclass
class TryResult:
    """What came back. Every field is something a person can look at and judge."""

    path: str = ""
    source_name: str = ""
    engine: str = ""
    audio: AudioInfo | None = None
    transcript: Transcript | None = None
    extraction: Any = None
    files: tuple[Any, ...] = ()
    transcribe_seconds: float = 0.0
    analyse_seconds: float = 0.0
    #: USD for the model calls this recording made, and the models that could not be
    #: priced. ``cost_usd`` is the reading, never the transcription: the engine bills by
    #: the minute of audio and this command has no price list for that, so reporting one
    #: total would be reporting a fraction as the whole.
    cost_usd: float = 0.0
    unpriced: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def words(self) -> int:
        return len((self.transcript.text if self.transcript else "").split())

    @property
    def duration_s(self) -> float:
        return float(self.audio.duration_s) if self.audio else 0.0

    def file(self, kind: str) -> Any:
        for rendered in self.files:
            if getattr(rendered, "kind", "") == kind:
                return rendered
        return None


def keys_from_environment(engine_name: str, env: Any = None) -> tuple[str, str]:
    """The two keys, read from the environment rather than from a command line.

    A key typed as an argument lands in shell history and in the process list, where it
    outlives the command by months. The engine's key lives under the engine's own variable
    name — the same one the running service reads — so switching engines cannot silently
    reuse the previous engine's key.
    """
    source = os.environ if env is None else env
    var = ENGINE_KEY_VARS.get(engine_name, "")
    if not var:
        raise TryError(
            f"{engine_name!r} is not a transcription engine this service knows — one of: "
            + ", ".join(sorted(ENGINE_KEY_VARS))
        )
    engine_key = (source.get(var) or "").strip()
    analysis_key = (source.get("ANALYSIS_API_KEY") or "").strip()
    missing = [name for name, value in ((var, engine_key), ("ANALYSIS_API_KEY", analysis_key))
               if not value]
    if missing:
        raise TryError(
            "this needs two keys and " + " and ".join(missing) + " "
            + ("is" if len(missing) == 1 else "are") + " not set. Put them in the "
            "environment rather than on the command line, where they would be kept in "
            "your shell history: export " + "=... ".join(missing) + "=..."
        )
    return engine_key, analysis_key


def run_one(
    path: str,
    *,
    engine_name: str,
    engine_key: str,
    analysis_key: str,
    languages: Sequence[str] = (),
    vocabulary: Sequence[str] = (),
    work_dir: str = "",
    counterparty: str = "",
    region: str = "",
    use_ffprobe: bool = True,
) -> TryResult:
    """Transcribe and read one local file. Writes nothing anywhere.

    The keys are passed in rather than read from a ``.env`` so this can run before there is
    a ``.env`` to read — which is the point, since assembling one is most of what a person
    is trying to avoid at this stage.
    """
    # `~` is expanded here rather than left to the shell. A recording is named something
    # like "Call Chester_260903_085842.m4a", so the path has to be quoted — and the shell
    # does NOT expand a tilde inside double quotes. Without this, following the documented
    # example verbatim fails on the very first command this whole thing exists to make
    # frictionless.
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise TryError(f"there is no file at {path}")

    config = _config_for(engine_name, engine_key, analysis_key,
                         languages=languages, vocabulary=vocabulary, work_dir=work_dir,
                         region=region)
    notes: list[str] = []

    info = probe(path, use_ffprobe=use_ffprobe)
    if info.truncated:
        # The running service would quarantine this and never transcribe it. Here it is
        # said loudly and the run continues anyway: refusing would teach nothing about the
        # transcription, which is the thing the person is trying to find out.
        notes.append(
            f"THE AUDIO LOOKS CUT OFF — {info.reason}. The running service would have "
            "quarantined this recording rather than transcribing it. Transcribing it here "
            "anyway, so you can see what a truncated file actually produces."
        )
    if info.is_silent:
        notes.append(
            "no duration could be measured, so the split guard has nothing to check a long "
            "recording against; a file long enough to need splitting may be refused"
        )

    parsed = naming.parse_source_name(os.path.basename(path))
    recorded_at, timestamp_note = naming.resolve_timestamp(parsed, _file_time(path))
    hints = Hints(
        vocabulary=tuple(config.vocabulary),
        counterparty=counterparty or parsed.party,
        languages=tuple(config.languages),
        recorded_at=recorded_at.isoformat(),
        source_name=os.path.basename(path),
        duration_s=info.duration_s or None,
    )

    engine = create_engine(config)
    started = time.monotonic()
    transcript = transcribe(engine, path, hints, duration_s=info.duration_s or None,
                            work_dir=config.work_dir)
    transcribe_seconds = time.monotonic() - started

    # The transcription has already been paid for by the time we get here, so a failure in
    # the reading must not take it with it. The run continues with no extraction: the
    # transcript still renders, and the person still gets the thing they were actually
    # trying to judge. This is not defensiveness for its own sake — the first run of this
    # command is exactly when one of the two keys is the wrong one.
    extractor = Extractor.from_config(config)
    extraction: Any = None
    started = time.monotonic()
    try:
        extraction = extractor.extract(transcript, hints)
    except AnalysisError as exc:
        because = (
            "THE ANALYSIS KEY WAS REFUSED — ANALYSIS_API_KEY must hold a key for the "
            "analysis provider (Anthropic unless ANALYSIS_BASE_URL says otherwise), which "
            f"is not the same key as the transcription engine's. The provider said: {exc}"
            if isinstance(exc, AnalysisAuthError) else f"THE READING FAILED — {exc}"
        )
        notes.append(
            because + ". The transcription is real and is shown in full below; only the "
            "summary and the proposals are missing. Nothing is wrong with the recording, "
            "and it does not need transcribing again."
        )
    analyse_seconds = time.monotonic() - started

    cost, unpriced = prices.cost_of_all(getattr(extraction, "spend", ()) or ())
    notes.extend(_standing_notes())

    # Rendered by the very same renderer the pipeline publishes through, so what is shown
    # is what would have been published rather than a preview written for this command.
    ctx = outputs.OutputContext(
        item_id=_TRY_ITEM_ID,
        source_name=os.path.basename(path),
        parsed=parsed,
        recorded_at=recorded_at,
        timestamp_source=timestamp_note,
        transcript=transcript,
        extraction=extraction,
        audio=info,
        engine=transcript.engine or config.engine,
        notes=("This was produced by `transcriber try` from a local file. Nothing was "
               "published, filed or emailed.",),
    )
    try:
        rendered: tuple[Any, ...] = outputs.render_all(ctx)
    except outputs.OutputContractError as exc:
        # Same reasoning as the reading: two API calls have been paid for and the words are
        # in hand. A file this recording could not legally be named under is worth saying
        # out loud, not worth losing the run to.
        rendered = ()
        notes.append(
            f"THE THREE FILES COULD NOT BE RENDERED — {exc}. What was transcribed and read "
            "is reported above regardless."
        )

    return TryResult(
        path=path,
        source_name=os.path.basename(path),
        engine=transcript.engine or config.engine,
        audio=info,
        transcript=transcript,
        extraction=extraction,
        files=tuple(rendered),
        transcribe_seconds=transcribe_seconds,
        analyse_seconds=analyse_seconds,
        cost_usd=cost,
        unpriced=unpriced,
        notes=tuple(notes),
    )


#: Every output name carries a short tag from the item id so two recordings made in the same
#: minute cannot collide. A try run has no item, and a fixed tag is the honest answer: these
#: three names are not the names the real recording would be published under, and a tag that
#: says so is better than one that looks like a real id.
_TRY_ITEM_ID = "try-run-local-file"


def _standing_notes() -> list[str]:
    """What this run did not do, said every time rather than only when it matters."""
    return [
        "the sensitivity gate did not run, so nothing was held back here that the running "
        "service would hold back",
        "nothing was published, filed, emailed or recorded — this run left no trace but the "
        "files you asked for",
    ]


def _config_for(engine_name: str, engine_key: str, analysis_key: str, *,
                languages: Sequence[str] = (), vocabulary: Sequence[str] = (),
                work_dir: str = "", region: str = "") -> Config:
    """An offline config with only the two credentials that are real.

    ``Config.offline`` fills every other field with an obviously fake value on purpose, so
    a mistake here fails at the first call rather than doing something plausible against a
    real account. Nothing below replaces a Microsoft or SMTP field, and nothing in this
    module reaches for one.
    """
    if not engine_key or not analysis_key:
        raise TryError("both a transcription key and an analysis key are needed")
    if engine_name == "azure" and not region:
        # Caught here rather than inside the engine, because the engine's own message is
        # about a base url and a region and this one can name the variable to set.
        raise TryError(
            "the azure engine needs a region as well as a key, and none was given. Set "
            "AZURE_SPEECH_REGION to the region your Speech resource is in — it is on the "
            "resource's overview page in the Azure portal."
        )
    config = Config.offline(ledger_path=":memory:")
    config.engine = engine_name
    config.engine_key = engine_key
    config.engine_keys = {engine_name: engine_key}
    config.analysis_api_key = analysis_key
    if languages:
        config.languages = tuple(languages)
    if vocabulary:
        config.vocabulary = tuple(vocabulary)
    if work_dir:
        config.work_dir = os.path.expanduser(work_dir)
    if region:
        config.azure_region = region
    return config


def _file_time(path: str) -> datetime:
    """When the file was last written, standing in for OneDrive's created time.

    Only consulted when the filename carries no moment of its own; the filename always
    wins, exactly as it does in the running service.
    """
    return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)


# --------------------------------------------------------------------------- reporting


def write_files(result: TryResult, directory: str) -> tuple[str, ...]:
    """Write the three rendered files into ``directory``. The only writing this does.

    A summary read in a terminal is skimmed; a summary opened in an editor is read. The
    directory is created if it is not there, and existing files of the same name are
    overwritten, because a try run is repeated with a changed setting and a numbered pile
    of near-identical files helps nobody.
    """
    # Same reason as the audio path, and here the cost of not doing it is worse than an
    # error: an unexpanded `--out ~/try-output` silently creates a directory literally
    # named `~` in whatever folder the command was run from.
    directory = os.path.expanduser(directory)
    os.makedirs(directory, exist_ok=True)
    written: list[str] = []
    for rendered in result.files:
        target = os.path.join(directory, rendered.name)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(rendered.text)
        written.append(target)
    return tuple(written)


def render_report(result: TryResult, *, full_transcript: bool = False,
                  written: Sequence[str] = ()) -> str:
    """The whole run, as a person would want it read out. Pure — builds a string."""
    # When the reading failed the transcript is the only thing there is, and truncating it
    # to forty lines would be hiding the one part of the run that worked.
    full_transcript = full_transcript or result.extraction is None
    out: list[str] = []
    add = out.append

    add(_rule(result.source_name))
    add("")
    add(_heading("The file"))
    out.extend(_indented(_file_lines(result)))

    add("")
    add(_heading("Transcription"))
    out.extend(_indented(_transcription_lines(result)))

    add("")
    add(_heading("What it read"))
    out.extend(_indented(_reading_lines(result)))

    add("")
    add(_heading("What the reading cost"))
    out.extend(_indented(_cost_lines(result)))

    if result.notes:
        add("")
        add(_heading("Worth knowing"))
        for note in result.notes:
            # Wrapped here rather than left to the terminal: these are the longest lines in
            # the report and the one a person most needs to actually read.
            add(textwrap.fill(note, width=84, initial_indent="  - ",
                              subsequent_indent="    ", break_on_hyphens=False))

    add("")
    add(_heading("The three files it would have published"))
    if not result.files:
        add("  none — see above for why")
        return "\n".join(out)
    if written:
        for target in written:
            add(f"  written to {target}")
        add("")
    for rendered in result.files:
        add(_rule(rendered.name))
        text = rendered.text
        if rendered.kind == "transcript" and not full_transcript:
            lines = text.splitlines()
            if len(lines) > TRANSCRIPT_PREVIEW_LINES:
                shown = "\n".join(lines[:TRANSCRIPT_PREVIEW_LINES])
                add(shown)
                add(f"... {len(lines) - TRANSCRIPT_PREVIEW_LINES} more lines. "
                    "Pass --full to print all of it, or --out DIR to write the file.")
                add("")
                continue
        add(text)
        add("")
    return "\n".join(out)


def _indented(lines: Sequence[str], width: int = 84) -> list[str]:
    """Two spaces in front of every line, and a wrap for the ones that need it.

    The aligned label/value rows and the cost table are all comfortably short and pass
    through untouched, which is what keeps their columns lined up; only the prose wraps.
    """
    out: list[str] = []
    for line in lines:
        body = f"  {line}"
        if len(body) <= width:
            out.append(body)
            continue
        # A wrapped label/value row hangs under its value, not under its label — otherwise
        # the second line reads as a new row and the column the eye is following stops
        # meaning anything.
        label = re.match(r"^(\S.{0,14}?\s{2,})\S", line)
        first = 2 + (len(line) - len(line.lstrip()))
        out.append(textwrap.fill(
            line.strip(), width=width, initial_indent=" " * first,
            subsequent_indent=" " * (2 + len(label.group(1)) if label else first),
            break_on_hyphens=False,
        ))
    return out


def _file_lines(result: TryResult) -> list[str]:
    info = result.audio
    lines = [f"name        {result.source_name}"]
    if info is None:
        return lines
    lines.append(f"container   {info.container}")
    lines.append(f"length      {_clock(info.duration_s)}"
                 + ("" if info.duration_s else "  (could not be measured)"))
    lines.append(f"size        {info.size_bytes / 1_000_000:.1f} MB")
    lines.append(f"checked by  {info.probed_by or 'the container walk'}")
    if info.truncated:
        lines.append(f"CUT OFF     {info.reason}")
    return lines


def _transcription_lines(result: TryResult) -> list[str]:
    transcript = result.transcript
    words = result.words
    lines = [f"engine      {result.engine}",
             f"took        {_clock(result.transcribe_seconds)}"]
    if result.duration_s > 0:
        lines[-1] += f"  ({result.transcribe_seconds / result.duration_s:.2f}x the audio)"
    lines.append(f"words       {words:,}")
    if result.duration_s > 60 and words:
        pace = round(words / (result.duration_s / 60))
        lines.append(f"pace        {pace} word{'' if pace == 1 else 's'} a minute"
                     "  (ordinary speech is 120-160; far below that means it missed some)")
    if transcript is not None and transcript.language:
        lines.append(f"language    {transcript.language}")
    if transcript is not None and transcript.segments:
        lines.append(f"segments    {len(transcript.segments):,}, covering "
                     f"{_clock(transcript.covered_duration_s)}")
    return lines


def _reading_lines(result: TryResult) -> list[str]:
    extraction = result.extraction
    if extraction is None:
        return ["nothing — the reading did not run"]
    routing = extraction.routing
    lines = [f"routed as   {routing.label}"
             + ("  (the safety pre-check overruled the router)" if routing.escalated else "")]
    if routing.one_line:
        lines.append(f"in a line   {routing.one_line}")
    if extraction.site:
        lines.append(f"site        {extraction.site}")
    if extraction.participants:
        lines.append("people      " + ", ".join(
            p.name_or_role for p in extraction.participants if p.name_or_role))
    lines.append(f"proposals   {len(extraction.proposals)} verified against the transcript")
    for category, items in extraction.by_category().items():
        lines.append(f"              {category}: {len(items)}")
    if extraction.review:
        lines.append(f"for review  {len(extraction.review)} the quote check could not confirm")
    if extraction.unclear:
        lines.append(f"unclear     {len(extraction.unclear)} passages it could not make out")
    if extraction.summary:
        lines.append("")
        lines.append("summary")
        for line in extraction.summary.splitlines():
            lines.append(f"  {line}")
    return lines


def _cost_lines(result: TryResult) -> list[str]:
    extraction = result.extraction
    spends = tuple(getattr(extraction, "spend", ()) or ())
    if extraction is None:
        return ["nothing — the reading did not run, so no model call was billed. "
                "The transcription was, by the engine, and this command cannot price that."]
    if not spends:
        return ["nothing was recorded for this run, which should not happen — "
                "every recording makes at least a router call"]
    lines = []
    for spend in spends:
        amount = prices.cost_of(spend)
        money = "not in the price list" if amount is None else f"${amount:.4f}"
        lines.append(f"{spend.model:<28} {spend.input_tokens:>7,} in  "
                     f"{spend.output_tokens:>6,} out   {money}")
    lines.append("")
    lines.append(f"reading this recording cost ${result.cost_usd:.4f} "
                 f"(prices read on {prices.CHECKED_ON})")
    if result.unpriced:
        lines.append("UNDERCOUNT: no price on file for " + ", ".join(result.unpriced)
                     + " — its tokens are counted above, its money is not")
    lines.append("this is the READING only. The transcription engine bills by the minute of "
                 "audio and this command has no price list for it, so that part is not in "
                 "the figure above.")
    if result.duration_s >= 60:
        per_hour = result.cost_usd / (result.duration_s / 3600.0)
        lines.append(f"at this rate an hour of talking reads for about ${per_hour:.2f}. "
                     "One recording is a small sample.")
    return lines


def _clock(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {rest:02d}s" if hours else f"{minutes}m {rest:02d}s"


def _heading(text: str) -> str:
    return f"{text}\n{'-' * len(text)}"


def _rule(text: str) -> str:
    return f"=== {text} " + "=" * max(3, 76 - len(text))
