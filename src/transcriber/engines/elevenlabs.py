"""ElevenLabs Scribe. Takes whole files, so this engine never triggers the splitter.

Verified against ElevenLabs' own published skill documentation (``elevenlabs/skills``,
``speech-to-text``):

  * ``model_id`` values ``scribe_v1`` and ``scribe_v2``; ``scribe_v2`` is what their current
    documentation uses throughout, and is the default here;
  * ``diarize`` (boolean, up to 32 speakers), ``num_speakers``, ``timestamps_granularity``
    (``none`` | ``word`` | ``character``), ``tag_audio_events``, ``language_code``
    (ISO 639-1 or 639-3), ``keyterms`` for vocabulary biasing;
  * limits of 5 GB and 10 hours — which is why ``max_bytes`` is ``None``: nothing this
    service will ever record comes close, so there is no splitting and no stitching, and
    therefore no split-duration guard to get wrong;
  * the response: ``text``, ``language_code``, ``language_probability`` and a ``words``
    array whose entries carry ``text``, ``start``, ``end``, ``type``
    (``word`` | ``spacing`` | ``audio_event``) and ``speaker_id``.

**Not verified, and each is a one-minute check for a human:**

  1. The HTTP surface itself. ElevenLabs' current documentation is written against their
     SDKs; ``POST https://api.elevenlabs.io/v1/speech-to-text`` with an ``xi-api-key``
     header is their long-standing REST shape and is what is coded here, but it was not
     re-read from the REST reference during this build.
  2. How ``keyterms`` is encoded in a multipart form. A JSON array in a single field is the
     shape used below; repeated ``keyterms[]`` fields are the other plausible encoding. If
     the API rejects it, the request is retried without the vocabulary and the transcript
     records ``dropped_fields: ['keyterms']`` rather than pretending it had the help.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

from ..models import Hints, Transcript
from .base import (
    EngineConfigError,
    EngineHTTPError,
    EngineResponseError,
    FilePart,
    HttpClient,
    MultipartBody,
    RetryPolicy,
    Word,
    guess_audio_content_type,
    iso639_3,
    new_transcript,
    register,
    safe_vocabulary,
    segments_from_words,
)

__all__ = ["ElevenLabsEngine", "DEFAULT_MODEL"]

log = logging.getLogger("transcriber.engines.elevenlabs")

DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = "scribe_v2"


class ElevenLabsEngine:
    """Scribe over the batch speech-to-text endpoint, with diarisation on."""

    name = "elevenlabs"
    #: None on purpose — Scribe takes the whole file. See the module docstring.
    max_bytes: int | None = None

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model_id: str = DEFAULT_MODEL,
        diarize: bool = True,
        tag_audio_events: bool = True,
        timeout_s: int = 1800,
        max_retries: int = 5,
        client: HttpClient | None = None,
    ) -> None:
        if not api_key:
            raise EngineConfigError("the elevenlabs engine needs ELEVENLABS_API_KEY")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model_id = model_id or DEFAULT_MODEL
        self.diarize = bool(diarize)
        # Left on: a site recording's non-speech events (a saw, a truck) are part of why a
        # sentence was misheard, and dropping them makes a transcript look cleaner than the
        # audio was. Nothing downstream treats them as speech.
        self.tag_audio_events = bool(tag_audio_events)
        self.client = client or HttpClient(
            timeout_s=timeout_s,
            policy=RetryPolicy(max_attempts=max(1, max_retries)),
            secrets=(api_key,),
        )

    @classmethod
    def from_config(cls, config: Any) -> "ElevenLabsEngine":
        return cls(
            api_key=getattr(config, "engine_key", "") or "",
            base_url=(getattr(config, "engine_base_url", "") or DEFAULT_BASE_URL),
            model_id=getattr(config, "engine_model", "") or DEFAULT_MODEL,
            # A whole file means a long upload and a long wait; the Graph-sized timeout
            # would abort a perfectly healthy transcription of a forty-minute walk.
            timeout_s=max(int(getattr(config, "http_timeout_s", 60) or 60), 1800),
            max_retries=int(getattr(config, "max_retries", 5) or 5),
        )

    def __repr__(self) -> str:
        return f"ElevenLabsEngine(model_id={self.model_id!r}, base_url={self.base_url!r})"

    # -- the contract -------------------------------------------------------------

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        size = os.path.getsize(path)
        fields, optional = self._fields(hints)
        file_part = FilePart(
            field="file",
            filename=os.path.basename(hints.source_name or path) or os.path.basename(path),
            path=path,
            content_type=guess_audio_content_type(hints.source_name or path),
        )
        doc, dropped = self._post(fields, optional, file_part)
        return self._to_transcript(doc, size=size, dropped=dropped)

    # -- request ------------------------------------------------------------------

    def _fields(self, hints: Hints) -> tuple[list[tuple[str, str]], list[str]]:
        fields: list[tuple[str, str]] = [
            ("model_id", self.model_id),
            ("diarize", "true" if self.diarize else "false"),
            ("timestamps_granularity", "word"),
            ("tag_audio_events", "true" if self.tag_audio_events else "false"),
        ]
        optional: list[str] = ["timestamps_granularity", "tag_audio_events", "diarize"]

        # One language, or none at all. Scribe identifies the language itself, and pinning
        # it to one of two we merely expect would be this pipeline asserting a fact about
        # the audio — which is the one thing it may never do. A site walk switches between
        # English and Afrikaans mid-sentence often enough that this matters.
        candidates = tuple(dict.fromkeys(
            code for code in ((hints.language,) if hints.language else tuple(hints.languages)) if code
        ))
        code = iso639_3(candidates[0]) if len(candidates) == 1 else None
        if code:
            fields.append(("language_code", code))
            optional.append("language_code")

        vocabulary = safe_vocabulary(hints, limit=50)
        if vocabulary:
            fields.append(("keyterms", json.dumps(list(vocabulary))))
            optional.append("keyterms")
        return fields, optional

    def _post(
        self,
        fields: Sequence[tuple[str, str]],
        optional: Sequence[str],
        file_part: FilePart,
    ) -> tuple[Any, list[str]]:
        url = f"{self.base_url}/speech-to-text"
        headers = {"xi-api-key": self.api_key}
        current = list(fields)
        droppable = list(optional)
        dropped: list[str] = []
        while True:
            body = MultipartBody(fields=current, files=[file_part])
            try:
                response = self.client.post(url, headers=headers, multipart=body, expected=(200, 201))
            except EngineHTTPError as exc:
                if exc.status not in (400, 422):
                    raise
                offender = _named_field(exc.body, droppable)
                if offender is None:
                    raise
                log.warning(
                    "elevenlabs rejected the optional field %r (%s); retrying without it",
                    offender, exc.body[:200],
                )
                current = [(k, v) for k, v in current if k != offender]
                droppable = [k for k in droppable if k != offender]
                dropped.append(offender)
                continue
            return response.json(), dropped

    # -- response mapping ---------------------------------------------------------

    def _to_transcript(self, doc: Any, *, size: int, dropped: Sequence[str]) -> Transcript:
        if not isinstance(doc, dict):
            raise EngineResponseError(
                f"elevenlabs returned {type(doc).__name__} where a transcription object was expected"
            )
        if "text" not in doc and "words" not in doc:
            raise EngineResponseError(
                "elevenlabs returned an object with neither 'text' nor 'words' — the API "
                f"shape has changed; keys were: {sorted(doc)[:12]}"
            )
        words = _words_of(doc.get("words"))
        segments = segments_from_words(words)
        text = str(doc.get("text") or "").strip()
        if not text and segments:
            text = " ".join(s.text for s in segments if s.text).strip()

        duration = doc.get("audio_duration_secs")
        if not isinstance(duration, (int, float)) and words:
            duration = max(w.end for w in words)

        metadata: dict[str, Any] = {
            "model_id": self.model_id,
            "endpoint": f"{self.base_url}/speech-to-text",
            "request_bytes": size,
            "diarize": self.diarize,
            "segments_available": bool(segments),
            "speakers_available": any(s.speaker for s in segments),
            "audio_events": sum(1 for w in words if w.kind == "audio_event"),
        }
        probability = doc.get("language_probability")
        if isinstance(probability, (int, float)):
            metadata["language_probability"] = float(probability)
        transcription_id = doc.get("transcription_id")
        if transcription_id:
            metadata["transcription_id"] = str(transcription_id)
        if dropped:
            metadata["dropped_fields"] = list(dropped)
            metadata["degraded"] = True

        return new_transcript(
            engine=self.name,
            text=text,
            segments=segments,
            language=str(doc.get("language_code") or "") or None,
            duration_s=float(duration) if isinstance(duration, (int, float)) else None,
            metadata=metadata,
        )


def _words_of(raw: Any) -> list[Word]:
    """Map Scribe's word array. ``spacing`` entries are kept so timing stays exact."""
    if not isinstance(raw, list):
        return []
    out: list[Word] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = float(entry.get("start") or 0.0)
            end = float(entry.get("end") or start)
        except (TypeError, ValueError):
            continue
        speaker = entry.get("speaker_id")
        out.append(
            Word(
                text=str(entry.get("text") or ""),
                start=start,
                end=end,
                speaker=str(speaker) if speaker not in (None, "") else None,
                kind=str(entry.get("type") or "word"),
            )
        )
    return out


def _named_field(body: str, candidates: Sequence[str]) -> str | None:
    lowered = (body or "").lower()
    # Longest first: an error naming "languages" must not be blamed on "language".
    for name in sorted(candidates, key=len, reverse=True):
        if name.lower() in lowered:
            return name
    return None


register("elevenlabs", ElevenLabsEngine.from_config)
