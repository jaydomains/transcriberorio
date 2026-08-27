"""OpenAI's audio transcription endpoint.

Verified against the published OpenAI OpenAPI description (``openai/openai-openapi``,
``CreateTranscriptionRequest``) rather than from memory:

  * ``POST {base_url}/audio/transcriptions``, ``multipart/form-data``, bearer auth;
  * ``model`` accepts ``gpt-transcribe``, ``gpt-4o-transcribe``, ``gpt-4o-mini-transcribe``,
    ``gpt-4o-mini-transcribe-2025-12-15``, ``whisper-1`` and ``gpt-4o-transcribe-diarize``;
  * ``languages[]`` (plural) and ``keywords[]`` are documented as *supported by
    ``gpt-transcribe``* — which is exactly the language hint and the construction-vocabulary
    hint this service wants, so the default model is ``gpt-transcribe``;
  * ``prompt`` is not supported by ``gpt-4o-transcribe-diarize``;
  * ``response_format`` is ``json`` only for ``gpt-4o-transcribe``/``-mini-``, and
    ``diarized_json`` is required to get speaker annotations from the diarize model;
  * ``chunking_strategy: auto`` is required by the diarize model for audio over 30 seconds.

**Not verified, and a human should check each in a minute:**

  1. Whether ``gpt-transcribe`` accepts ``verbose_json`` (for segment timestamps) or
     ``diarized_json`` (for speaker labels). The spec states the restriction for the other
     models and is silent for this one. The default here therefore asks for ``json``, which
     every model supports, and ``OPENAI_RESPONSE_FORMAT``-style overrides are available
     through the constructor. If a richer format works, set it and get timestamps.
  2. The 25 MB upload ceiling. It is the long-standing documented limit for this endpoint
     and is not restated in the schema; if it has moved, ``max_bytes`` below is the one
     number to change.

Where a hint is rejected as unsupported, the request is retried without it and the dropped
hint is recorded in ``engine_metadata['dropped_fields']``. A transcript produced with less
help than intended must not look identical to one produced with all of it.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Sequence

from ..models import Hints, Segment, Transcript
from .base import (
    EngineAudioTooLarge,
    EngineConfigError,
    EngineHTTPError,
    EngineResponseError,
    FilePart,
    HttpClient,
    MultipartBody,
    RetryPolicy,
    guess_audio_content_type,
    iso639_1,
    new_transcript,
    primary_language,
    register,
    safe_vocabulary,
)

__all__ = ["OpenAIEngine", "DEFAULT_MODEL", "MAX_BYTES"]

log = logging.getLogger("transcriber.engines.openai")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-transcribe"

#: 25 MB — the documented ceiling for this endpoint. UNVERIFIED for the newest models; the
#: splitting path in ``engines/splitting.py`` exists because of this number, so if it is
#: wrong it is wrong in the safe direction (we split more than we need to).
MAX_BYTES = 25 * 1024 * 1024

#: Per-model response format. See the module docstring: only the ``gpt-transcribe`` row is
#: a judgement call, and it is the conservative one.
_RESPONSE_FORMAT: Mapping[str, str] = {
    "whisper-1": "verbose_json",             # verified: full segment + word timestamps
    "gpt-4o-transcribe": "json",             # verified: json is the only supported format
    "gpt-4o-mini-transcribe": "json",        # verified: as above
    "gpt-4o-mini-transcribe-2025-12-15": "json",
    "gpt-4o-transcribe-diarize": "diarized_json",   # verified: required for speaker labels
    "gpt-transcribe": "json",                # UNVERIFIED whether richer formats are accepted
}

#: Models documented to accept the plural ``languages[]`` and ``keywords[]`` hints.
_SUPPORTS_KEYWORDS = frozenset({"gpt-transcribe"})
#: The one model documented to reject ``prompt``.
_NO_PROMPT = frozenset({"gpt-4o-transcribe-diarize"})


class OpenAIEngine:
    """``gpt-transcribe`` and its siblings, over the multipart transcriptions endpoint."""

    name = "openai"
    max_bytes: int | None = MAX_BYTES

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        response_format: str | None = None,
        timeout_s: int = 600,
        max_retries: int = 5,
        client: HttpClient | None = None,
        max_bytes: int | None = MAX_BYTES,
    ) -> None:
        if not api_key:
            raise EngineConfigError("the openai engine needs OPENAI_API_KEY")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.response_format = response_format or _RESPONSE_FORMAT.get(self.model, "json")
        self.max_bytes = max_bytes
        self.client = client or HttpClient(
            timeout_s=timeout_s,
            policy=RetryPolicy(max_attempts=max(1, max_retries)),
            secrets=(api_key,),
        )

    # -- construction -------------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any) -> "OpenAIEngine":
        base = (getattr(config, "engine_base_url", "") or DEFAULT_BASE_URL).rstrip("/")
        return cls(
            api_key=getattr(config, "engine_key", "") or "",
            base_url=base,
            # Forward-compatible: if a later config grows an explicit model setting, it is
            # used; until then the default is the model this service was designed against.
            model=getattr(config, "engine_model", "") or DEFAULT_MODEL,
            response_format=getattr(config, "engine_response_format", "") or None,
            # Transcription of a 25MB file is minutes of work, not seconds; the global
            # HTTP timeout is sized for Graph calls and is far too short here.
            timeout_s=max(int(getattr(config, "http_timeout_s", 60) or 60), 600),
            max_retries=int(getattr(config, "max_retries", 5) or 5),
        )

    def __repr__(self) -> str:  # the key never reaches a log line, even by accident
        return f"OpenAIEngine(model={self.model!r}, base_url={self.base_url!r})"

    # -- the contract -------------------------------------------------------------

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        size = os.path.getsize(path)
        if self.max_bytes is not None and size > self.max_bytes:
            # Reached only when something bypassed engines.transcribe(), which splits. Said
            # plainly here rather than left to come back as an opaque 413 from the provider.
            raise EngineAudioTooLarge(
                f"{os.path.basename(path)} is {size} bytes and this endpoint accepts "
                f"{self.max_bytes}. Call transcriber.engines.transcribe(), which splits on "
                "silence and checks the pieces against the recording's duration.",
                size_bytes=size,
                max_bytes=self.max_bytes,
            )
        fields, optional = self._fields(hints)
        file_part = FilePart(
            field="file",
            # The endpoint identifies the format from the filename and content type, so
            # both have to be real: a temp file called "tmp1234" is rejected as unreadable.
            filename=os.path.basename(hints.source_name or path) or os.path.basename(path),
            path=path,
            content_type=guess_audio_content_type(hints.source_name or path),
        )
        doc, dropped = self._post(fields, optional, file_part)
        return self._to_transcript(doc, size=size, dropped=dropped, hints=hints)

    # -- request ------------------------------------------------------------------

    def _fields(self, hints: Hints) -> tuple[list[tuple[str, str]], list[str]]:
        """The form fields, and which of them may be dropped if the API rejects them."""
        fields: list[tuple[str, str]] = [("model", self.model), ("response_format", self.response_format)]
        optional: list[str] = ["response_format"]

        language = iso639_1(primary_language(hints))
        if language:
            fields.append(("language", language))
            optional.append("language")

        languages = [c for c in (iso639_1(x) for x in hints.languages) if c]
        if self.model in _SUPPORTS_KEYWORDS and len(set(languages)) > 1:
            # Repeated field names are how an array is expressed in multipart/form-data.
            for code in dict.fromkeys(languages):
                fields.append(("languages[]", code))
            optional.append("languages[]")

        vocabulary = safe_vocabulary(hints)
        if vocabulary and self.model in _SUPPORTS_KEYWORDS:
            for term in vocabulary:
                fields.append(("keywords[]", term))
            optional.append("keywords[]")

        if self.model not in _NO_PROMPT:
            prompt = hints.prompt_text()
            if prompt:
                fields.append(("prompt", prompt))
                optional.append("prompt")

        if self.model in _NO_PROMPT:
            # Verified requirement: the diarize model needs this for anything over 30s, and
            # every recording this service handles is over 30 seconds except the ones that
            # matter most, so it is always sent rather than sent conditionally.
            fields.append(("chunking_strategy", "auto"))
            optional.append("chunking_strategy")

        if self.response_format == "verbose_json":
            fields.append(("timestamp_granularities[]", "segment"))
            optional.append("timestamp_granularities[]")

        return fields, optional

    def _post(
        self,
        fields: Sequence[tuple[str, str]],
        optional: Sequence[str],
        file_part: FilePart,
    ) -> tuple[Any, list[str]]:
        """POST, and on a 400 that names one of our optional hints, drop it and try again.

        Dropping is bounded and recorded. The alternative — failing the whole recording
        because a hint field was renamed by the provider — loses the audio; the alternative
        to *recording* it is a transcript that silently had no vocabulary help.
        """
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        current = list(fields)
        droppable = list(optional)
        dropped: list[str] = []

        while True:
            body = MultipartBody(fields=current, files=[file_part])
            try:
                response = self.client.post(url, headers=headers, multipart=body, expected=(200,))
            except EngineHTTPError as exc:
                if exc.status not in (400, 422):
                    raise
                offender = _named_field(exc.body, droppable)
                if offender is None:
                    raise
                log.warning(
                    "openai rejected the optional field %r (%s); retrying without it",
                    offender, exc.body[:200],
                )
                current = [(k, v) for k, v in current if k != offender]
                droppable = [k for k in droppable if k != offender]
                dropped.append(offender)
                if offender == "response_format":
                    # Falling back to the format every model supports rather than to none.
                    current.append(("response_format", "json"))
                    self.response_format = "json"
                continue
            return response.json(), dropped

    # -- response mapping ---------------------------------------------------------

    def _to_transcript(
        self,
        doc: Any,
        *,
        size: int,
        dropped: Sequence[str],
        hints: Hints,
    ) -> Transcript:
        if not isinstance(doc, dict):
            raise EngineResponseError(
                f"openai returned {type(doc).__name__} where a transcription object was expected"
            )
        text = str(doc.get("text") or "")
        segments = _segments_of(doc)
        if not text and segments:
            text = " ".join(s.text for s in segments if s.text).strip()
        if not text:
            # Empty is a real answer (silence), but it is the plausibility gate's answer to
            # give, not ours to hide. Pass it on with the evidence attached.
            log.warning("openai returned no text for %s", hints.source_name or "(unnamed)")

        duration = doc.get("duration")
        metadata: dict[str, Any] = {
            "model": self.model,
            "response_format": self.response_format,
            "endpoint": f"{self.base_url}/audio/transcriptions",
            "request_bytes": size,
            "segments_available": bool(segments),
            "speakers_available": any(s.speaker for s in segments),
        }
        if dropped:
            metadata["dropped_fields"] = list(dropped)
            metadata["degraded"] = True
        usage = doc.get("usage")
        if isinstance(usage, dict):
            metadata["usage"] = usage
        return new_transcript(
            engine=self.name,
            text=text,
            segments=segments,
            language=_language_of(doc),
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            metadata=metadata,
        )


def _segments_of(doc: Mapping[str, Any]) -> list[Segment]:
    """Both documented segment shapes: diarized_json carries ``speaker``, verbose_json does not."""
    raw = doc.get("segments")
    if not isinstance(raw, list):
        return []
    out: list[Segment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry.get("start") or 0.0)
            end = float(entry.get("end") or start)
        except (TypeError, ValueError):
            continue
        speaker = entry.get("speaker")
        out.append(
            Segment(
                start=start,
                end=end,
                speaker=str(speaker) if speaker not in (None, "") else None,
                text=str(entry.get("text") or "").strip(),
            )
        )
    return [s for s in out if s.text]


def _language_of(doc: Mapping[str, Any]) -> str | None:
    """Report what the engine said, never what we hoped. ``verbose_json`` returns a language
    name ("english"); ``json`` returns nothing, and nothing is the honest answer."""
    language = doc.get("language")
    if isinstance(language, str) and language.strip():
        return language.strip()
    return None


def _named_field(body: str, candidates: Sequence[str]) -> str | None:
    """Which of our optional fields the error body is complaining about, if any."""
    lowered = (body or "").lower()
    # Longest first: an error naming "languages" must not be blamed on "language".
    for name in sorted(candidates, key=len, reverse=True):
        bare = name.replace("[]", "")
        if bare and bare.lower() in lowered:
            return name
    return None


register("openai", OpenAIEngine.from_config)
