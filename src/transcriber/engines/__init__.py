"""Transcription engines, selected by ``TRANSCRIBE_ENGINE``.

Importing this package registers all three engines, so ``create_engine(config)`` can build
any of them without discovery machinery or a plugin path. Everything else in the service
talks to the :class:`~transcriber.engines.base.Engine` protocol and never to a provider.

The worker's whole interface is two calls::

    engine = create_engine(config)
    transcript = transcribe(engine, path, hints, duration_s=info.duration_s)

:func:`transcribe` is the splitting-aware entry point: an engine that takes whole files
(``max_bytes is None``) is called directly, and one with a size ceiling has the audio cut on
silence, transcribed piece by piece, reassembled, and checked against the recording's
duration before the result is allowed to exist. That check raises
:class:`~transcriber.engines.splitting.SplitDurationError` rather than returning a short
transcript, because a transcript that is quietly short is indistinguishable from a short
conversation and is the exact failure this service was built to remove.

Azure is a special case worth knowing about: its batch transcription API fetches audio from
a URL and has no upload at all. Hand the engine a provider and it uses batch::

    engine = create_engine(config)
    if engine.name == "azure":
        engine.with_content_url_provider(lambda path: graph.download_url(item_id))

Without one it falls back to Azure's fast-transcription endpoint, which does take an upload,
and says so in ``Transcript.engine_metadata`` so the transcript records which service
produced it.
"""

from __future__ import annotations

from ..models import Hints, Segment, Transcript
from .base import (
    Engine,
    EngineAudioTooLarge,
    EngineAuthError,
    EngineConfigError,
    EngineError,
    EngineHTTPError,
    EngineResponseError,
    EngineTransportError,
    HttpClient,
    RetryPolicy,
    create_engine,
    engine_for_name,
    register,
    registered_engines,
)

# Imported for their side effect: each module registers itself. Ordered as the config lists
# them so a stack trace from a bad import reads in the same order as the documentation.
from . import openai as _openai      # noqa: F401  - registers "openai"
from . import elevenlabs as _elevenlabs  # noqa: F401  - registers "elevenlabs"
from . import azure as _azure        # noqa: F401  - registers "azure"

from .azure import AzureSpeechEngine
from .elevenlabs import ElevenLabsEngine
from .openai import OpenAIEngine
from .splitting import (
    DEFAULT_OVERLAP_S,
    Piece,
    SplitDurationError,
    SplitError,
    SplitPlan,
    SplitUnsupported,
    probe_duration,
    split_audio,
    stitch,
    transcribe_with_splitting,
    verify_result_duration,
)

__all__ = [
    "Engine",
    "EngineError",
    "EngineConfigError",
    "EngineAuthError",
    "EngineHTTPError",
    "EngineTransportError",
    "EngineResponseError",
    "EngineAudioTooLarge",
    "HttpClient",
    "RetryPolicy",
    "create_engine",
    "engine_for_name",
    "register",
    "registered_engines",
    "OpenAIEngine",
    "ElevenLabsEngine",
    "AzureSpeechEngine",
    "Piece",
    "SplitPlan",
    "SplitError",
    "SplitDurationError",
    "SplitUnsupported",
    "DEFAULT_OVERLAP_S",
    "probe_duration",
    "split_audio",
    "stitch",
    "verify_result_duration",
    "transcribe_with_splitting",
    "transcribe",
    "Hints",
    "Segment",
    "Transcript",
]


def transcribe(
    engine: Engine,
    path: str,
    hints: Hints,
    *,
    duration_s: float | None = None,
    work_dir: str | None = None,
) -> Transcript:
    """Transcribe one file with one engine, splitting it first only if it has to be split.

    ``duration_s`` should be the duration ``audio.probe`` measured. It is what the split
    guard checks the reassembled pieces against, so passing it is what makes the guard mean
    anything; without it the splitter has to establish the duration itself and will refuse
    to proceed if it cannot.
    """
    return transcribe_with_splitting(
        engine, path, hints, duration_s=duration_s, work_dir=work_dir
    )
