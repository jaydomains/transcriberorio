"""Azure AI Speech. Batch transcription where the audio has a URL, fast transcription where
it only exists on disk.

Both shapes were read from Microsoft's own documentation source
(``MicrosoftDocs/azure-ai-docs``, ``batch-transcription-create.md``,
``batch-transcription-get.md``, ``includes/common/transcription-rest-api.md`` and
``includes/request-configuration-options.md``) during this build, so the following are
verified rather than remembered:

  * batch submit is ``POST {host}/speechtotext/transcriptions:submit?api-version=2024-11-15``
    with a JSON body of ``contentUrls`` / ``contentContainerUrl``, ``locale``,
    ``displayName``, ``model`` and a ``properties`` object carrying ``timeToLiveHours``,
    ``wordLevelTimestampsEnabled``, ``diarizationEnabled``, ``punctuationMode``,
    ``profanityFilterMode`` and ``languageIdentification.candidateLocales``;
  * **batch accepts audio only as a URL.** There is no multipart upload. This is the whole
    reason this engine has two modes;
  * status and results are ``GET .../transcriptions/{id}`` and ``GET .../transcriptions/{id}/files``,
    and the result document is ``combinedRecognizedPhrases`` plus ``recognizedPhrases``
    with ``offsetInTicks`` / ``durationInTicks`` (100-nanosecond units) and ``nBest[0].display``;
  * fast transcription is ``POST {host}/speechtotext/transcriptions:transcribe?api-version=2025-10-15``,
    ``multipart/form-data`` with an ``audio`` file part and a ``definition`` JSON part
    (``locales``, ``diarization: {enabled, maxSpeakers}``, ``channels``, ``phraseList``,
    ``profanityFilterMode``), limited to files under 500 MB and under 5 hours, answering
    with ``durationMilliseconds``, ``combinedPhrases`` and ``phrases`` (each with
    ``offsetMilliseconds``, ``durationMilliseconds``, ``text``, ``speaker``, ``locale``).

**Not verified — check these two in a minute:**

  1. The host. Microsoft's current sample uses ``https://<resource-name>.cognitiveservices.azure.com``;
     the region form ``https://<region>.api.cognitive.microsoft.com`` is the older documented
     one and is what config gives us (``AZURE_SPEECH_REGION``), so it is the default here.
     If the resource-name form is required for your resource, set ``ENGINE_BASE_URL``.
  2. ``phraseList`` on fast transcription is documented as available from API version
     2025-10-15. If your resource is pinned to an older version the field is dropped on the
     retry and the transcript records that it went out without the vocabulary.

``max_bytes`` is ``None``: batch has no small ceiling, which is the property this engine was
chosen for. In fast mode the documented 500 MB limit is checked *before* the upload and
fails loudly with the remedy named, rather than being discovered as an opaque 413.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Callable, Mapping, Sequence

from ..models import Hints, Segment, Transcript, strip_emails
from .base import (
    EngineConfigError,
    EngineError,
    EngineHTTPError,
    EngineResponseError,
    FilePart,
    HttpClient,
    MultipartBody,
    RetryPolicy,
    guess_audio_content_type,
    new_transcript,
    primary_language,
    register,
    safe_vocabulary,
)

__all__ = ["AzureSpeechEngine", "BATCH_API_VERSION", "FAST_API_VERSION"]

log = logging.getLogger("transcriber.engines.azure")

BATCH_API_VERSION = "2024-11-15"
FAST_API_VERSION = "2025-10-15"

#: Documented fast-transcription ceilings. Not a splitting trigger — a loud refusal.
FAST_MAX_BYTES = 500 * 1024 * 1024
FAST_MAX_DURATION_S = 5 * 60 * 60

#: Azure timestamps are in 100-nanosecond ticks.
TICKS_PER_SECOND = 10_000_000


class AzureSpeechEngine:
    """Azure batch transcription, with fast transcription for audio that has no URL.

    ``content_url_provider`` is how the batch path gets its URL: OneDrive's
    ``@microsoft.graph.downloadUrl`` is already a short-lived pre-authenticated URL, so the
    worker can hand one over without staging the audio in blob storage. When no provider is
    configured the engine uses fast transcription instead and says so in the metadata — the
    transcript records which path produced it, because the two do not have identical
    accuracy characteristics and a reader must not have to guess.
    """

    name = "azure"
    #: None: batch imposes no small size ceiling. See the module docstring.
    max_bytes: int | None = None

    def __init__(
        self,
        api_key: str,
        *,
        region: str = "",
        base_url: str = "",
        content_url_provider: Callable[[str], str] | None = None,
        mode: str = "auto",
        languages: Sequence[str] = ("en-ZA", "af-ZA"),
        max_speakers: int = 4,
        timeout_s: int = 300,
        max_retries: int = 5,
        poll_interval_s: float = 10.0,
        max_wait_s: float = 1800.0,
        client: HttpClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise EngineConfigError("the azure engine needs AZURE_SPEECH_KEY")
        if not base_url and not region:
            raise EngineConfigError(
                "the azure engine needs AZURE_SPEECH_REGION (or ENGINE_BASE_URL for a "
                "resource-scoped host such as https://<resource>.cognitiveservices.azure.com)"
            )
        if mode not in ("auto", "batch", "fast"):
            raise EngineConfigError(f"azure mode must be auto, batch or fast — not {mode!r}")
        self.api_key = api_key
        self.region = region
        self.base_url = (base_url or f"https://{region}.api.cognitive.microsoft.com").rstrip("/")
        self.content_url_provider = content_url_provider
        self.mode = mode
        self.languages = tuple(dict.fromkeys(l for l in languages if l)) or ("en-ZA",)
        self.max_speakers = max(2, int(max_speakers))
        self.poll_interval_s = float(poll_interval_s)
        # A batch job that outlives the worker's ledger lease is a file processed twice.
        # This cap is deliberately below a default lease so the failure is ours and visible.
        self.max_wait_s = float(max_wait_s)
        self._sleep = sleep
        self.client = client or HttpClient(
            timeout_s=timeout_s,
            policy=RetryPolicy(max_attempts=max(1, max_retries)),
            secrets=(api_key,),
        )

    @classmethod
    def from_config(cls, config: Any, content_url_provider: Callable[[str], str] | None = None) -> "AzureSpeechEngine":
        return cls(
            api_key=getattr(config, "engine_key", "") or "",
            region=getattr(config, "azure_region", "") or "",
            base_url=getattr(config, "engine_base_url", "") or "",
            content_url_provider=content_url_provider,
            languages=tuple(getattr(config, "languages", ()) or ("en-ZA", "af-ZA")),
            timeout_s=max(int(getattr(config, "http_timeout_s", 60) or 60), 300),
            max_retries=int(getattr(config, "max_retries", 5) or 5),
        )

    def __repr__(self) -> str:
        return f"AzureSpeechEngine(base_url={self.base_url!r}, mode={self.mode!r})"

    def with_content_url_provider(self, provider: Callable[[str], str]) -> "AzureSpeechEngine":
        """Wire in the URL source after construction — the worker knows the Graph item id,
        the registry factory does not."""
        self.content_url_provider = provider
        return self

    # -- the contract -------------------------------------------------------------

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        url = self._content_url(path)
        if self.mode == "batch" and not url:
            raise EngineConfigError(
                "azure mode is 'batch' but no content URL is available for "
                f"{hints.source_name or os.path.basename(path)}. Azure batch transcription "
                "fetches audio from a URL and has no upload; give the engine a "
                "content_url_provider (OneDrive's pre-authenticated download URL will do) "
                "or set mode='fast'."
            )
        if url and self.mode in ("auto", "batch"):
            return self._transcribe_batch(path, hints, url)
        return self._transcribe_fast(path, hints)

    def _content_url(self, path: str) -> str | None:
        if self.mode == "fast" or self.content_url_provider is None:
            return None
        try:
            url = self.content_url_provider(path)
        except Exception as exc:  # a broken provider must not silently become fast mode
            raise EngineError(
                f"the azure content-URL provider failed for {os.path.basename(path)}: {exc}"
            ) from exc
        return url or None

    # -- batch --------------------------------------------------------------------

    def _transcribe_batch(self, path: str, hints: Hints, content_url: str) -> Transcript:
        # The content URL is a bearer capability to the recording: no header, no token,
        # anyone holding the link has the audio. It is handed to Azure on purpose — that is
        # what batch transcription is — but it must not come back out in an error message,
        # and an Azure 4xx that echoes the request body is exactly how it would.
        with self.client.hiding(content_url):
            return self._batch(content_url, hints)

    def _batch(self, content_url: str, hints: Hints) -> Transcript:
        job = self._submit_batch(content_url, hints)
        job_url = job.get("self") or ""
        if not job_url:
            raise EngineResponseError(
                "azure accepted the batch job but returned no 'self' link, so there is "
                "nothing to poll; the API shape has changed"
            )
        status = self._await_batch(job_url)
        result = self._fetch_batch_result(job_url)
        return self._batch_to_transcript(result, hints=hints, status=status, job_url=job_url)

    def _submit_batch(self, content_url: str, hints: Hints) -> Mapping[str, Any]:
        locale = primary_language(hints) or self.languages[0]
        properties: dict[str, Any] = {
            "wordLevelTimestampsEnabled": True,
            "diarizationEnabled": True,
            "punctuationMode": "DictatedAndAutomatic",
            # None, not the "Masked" default: the transcript is evidence of what was said,
            # and a masked word is a changed quote. Nothing downstream may alter a quote.
            "profanityFilterMode": "None",
            # Long enough that a failed run can still be inspected by a person tomorrow
            # morning; short enough that his audio does not sit in Azure for a month.
            "timeToLiveHours": 24,
        }
        candidates = [l for l in self.languages if l]
        if len(candidates) > 1:
            properties["languageIdentification"] = {"candidateLocales": candidates, "mode": "Continuous"}
        body = {
            "contentUrls": [content_url],
            "locale": locale,
            # The display name reaches a third party's console, so it is scrubbed like
            # anything else that leaves this service.
            "displayName": strip_emails(hints.source_name or "kbc recording")[:120],
            "model": None,
            "properties": properties,
        }
        url = f"{self.base_url}/speechtotext/transcriptions:submit?api-version={BATCH_API_VERSION}"
        response = self.client.post(url, headers=self._headers(), json_body=body, expected=(200, 201, 202))
        doc = response.json()
        if not isinstance(doc, dict):
            raise EngineResponseError("azure returned a non-object from the batch submit call")
        return doc

    def _await_batch(self, job_url: str) -> str:
        """Poll until the job leaves NotStarted/Running, or fail loudly on the clock."""
        deadline = time.time() + self.max_wait_s
        delay = self.poll_interval_s
        last = "NotStarted"
        while time.time() < deadline:
            self._sleep(min(delay, max(0.0, deadline - time.time())))
            response = self.client.get(self._with_api_version(job_url), headers=self._headers(), expected=(200,))
            doc = response.json()
            last = str(doc.get("status") or "") if isinstance(doc, dict) else ""
            if last == "Succeeded":
                return last
            if last == "Failed":
                error = doc.get("properties", {}).get("error") if isinstance(doc, dict) else None
                # Through the client's scrubber, not straight out. This is the vendor's own
                # error document dumped whole, and the most likely reason a batch job fails
                # is that Azure could not read the content URL — in which case the document
                # says so BY QUOTING IT. That would carry the pre-authenticated link to the
                # recording into last_error, the ledger and the morning email, on the one
                # path the surrounding `hiding()` block cannot reach on its own, because
                # this message is built here rather than by the HTTP layer.
                raise EngineError(
                    self.client.scrub(
                        "azure batch transcription failed: "
                        + json.dumps(error or doc.get("error") or {"status": last})[:400]
                    )
                )
            if last not in ("NotStarted", "Running"):
                raise EngineResponseError(
                    f"azure reported an unrecognised batch status {last!r}; refusing to "
                    "assume it means success"
                )
            delay = min(delay * 1.5, 60.0)
        raise EngineError(
            f"azure batch transcription was still {last!r} after {self.max_wait_s:.0f}s. "
            "The job may yet finish in Azure, but this worker will not hold a claim longer "
            "than its lease; the recording stays unfinished and visible rather than being "
            "marked done."
        )

    def _fetch_batch_result(self, job_url: str) -> Mapping[str, Any]:
        files_url = self._with_api_version(job_url.rstrip("/") + "/files")
        response = self.client.get(files_url, headers=self._headers(), expected=(200,))
        doc = response.json()
        values = doc.get("values") if isinstance(doc, dict) else None
        if not isinstance(values, list) or not values:
            raise EngineResponseError("azure reported the job succeeded but listed no result files")
        for entry in values:
            if not isinstance(entry, dict) or entry.get("kind") != "Transcription":
                continue
            content_url = (entry.get("links") or {}).get("contentUrl")
            if not content_url:
                continue
            # The result URL is pre-authenticated; sending the subscription key as well is
            # two credentials on one request, which Azure storage rejects. Hidden for the
            # same reason the audio's URL is: it is a link to the transcript that needs no
            # credential, so it must not come back out in an error message.
            with self.client.hiding(str(content_url)):
                payload = self.client.get(content_url, expected=(200,))
            result = payload.json()
            if isinstance(result, dict):
                return result
            raise EngineResponseError("azure's transcription result file was not a JSON object")
        raise EngineResponseError(
            "azure's file list contained no entry of kind 'Transcription' — "
            f"kinds present: {sorted({str(v.get('kind')) for v in values if isinstance(v, dict)})}"
        )

    def _batch_to_transcript(
        self,
        doc: Mapping[str, Any],
        *,
        hints: Hints,
        status: str,
        job_url: str,
    ) -> Transcript:
        segments: list[Segment] = []
        locales: set[str] = set()
        for phrase in doc.get("recognizedPhrases") or []:
            if not isinstance(phrase, dict):
                continue
            if phrase.get("recognitionStatus") not in (None, "Success"):
                # A phrase Azure could not recognise is not silently dropped: it is counted
                # in the metadata below so a short transcript has a stated reason.
                continue
            best = phrase.get("nBest") or []
            text = ""
            if isinstance(best, list) and best and isinstance(best[0], dict):
                text = str(best[0].get("display") or best[0].get("lexical") or "").strip()
            if not text:
                continue
            start = _seconds_from_ticks(phrase.get("offsetInTicks"))
            length = _seconds_from_ticks(phrase.get("durationInTicks"))
            speaker = phrase.get("speaker")
            if speaker is None:
                speaker = phrase.get("channel")
            segments.append(
                Segment(
                    start=start,
                    end=start + length,
                    speaker=f"speaker_{speaker}" if speaker is not None else None,
                    text=text,
                )
            )
            locale = phrase.get("locale")
            if locale:
                locales.add(str(locale))

        combined = doc.get("combinedRecognizedPhrases") or []
        text = ""
        if isinstance(combined, list) and combined and isinstance(combined[0], dict):
            text = str(combined[0].get("display") or combined[0].get("lexical") or "").strip()
        if not text:
            text = " ".join(s.text for s in segments if s.text).strip()

        duration_ms = doc.get("durationMilliseconds")
        duration = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else None
        if duration is None and isinstance(doc.get("durationInTicks"), (int, float)):
            duration = _seconds_from_ticks(doc.get("durationInTicks"))

        unrecognised = sum(
            1
            for p in (doc.get("recognizedPhrases") or [])
            if isinstance(p, dict) and p.get("recognitionStatus") not in (None, "Success")
        )
        metadata: dict[str, Any] = {
            "mode": "batch",
            "api_version": BATCH_API_VERSION,
            "endpoint": f"{self.base_url}/speechtotext/transcriptions:submit",
            "status": status,
            "job": _job_id(job_url),
            "segments_available": bool(segments),
            "speakers_available": any(s.speaker for s in segments),
            "unrecognised_phrases": unrecognised,
        }
        if locales:
            metadata["locales_seen"] = sorted(locales)
        return new_transcript(
            engine=self.name,
            text=text,
            segments=segments,
            language=sorted(locales)[0] if locales else (primary_language(hints) or None),
            duration_s=duration,
            metadata=metadata,
        )

    # -- fast ---------------------------------------------------------------------

    def _transcribe_fast(self, path: str, hints: Hints) -> Transcript:
        size = os.path.getsize(path)
        if size > FAST_MAX_BYTES:
            raise EngineError(
                f"{os.path.basename(path)} is {size / 1024 / 1024:.0f}MB, over Azure fast "
                f"transcription's documented {FAST_MAX_BYTES // 1024 // 1024}MB limit. Give "
                "this engine a content_url_provider so it can use batch transcription, which "
                "has no such ceiling."
            )
        if hints.duration_s and hints.duration_s > FAST_MAX_DURATION_S:
            raise EngineError(
                f"{os.path.basename(path)} is {hints.duration_s / 3600:.1f} hours, over Azure "
                "fast transcription's documented 5-hour limit; use batch transcription."
            )

        definition, droppable = self._fast_definition(hints)
        file_part = FilePart(
            field="audio",
            filename=os.path.basename(hints.source_name or path) or os.path.basename(path),
            path=path,
            content_type=guess_audio_content_type(hints.source_name or path),
        )
        url = f"{self.base_url}/speechtotext/transcriptions:transcribe?api-version={FAST_API_VERSION}"
        dropped: list[str] = []
        while True:
            body = MultipartBody(
                fields=[("definition", json.dumps(definition))],
                files=[file_part],
            )
            try:
                response = self.client.post(url, headers=self._headers(), multipart=body, expected=(200,))
            except EngineHTTPError as exc:
                offender = _named_property(exc.body, droppable) if exc.status in (400, 422) else None
                if offender is None:
                    raise
                log.warning(
                    "azure rejected the fast-transcription property %r (%s); retrying without it",
                    offender, exc.body[:200],
                )
                definition.pop(offender, None)
                droppable = [k for k in droppable if k != offender]
                dropped.append(offender)
                continue
            return self._fast_to_transcript(response.json(), size=size, dropped=dropped, hints=hints)

    def _fast_definition(self, hints: Hints) -> tuple[dict[str, Any], list[str]]:
        locales = [l for l in ([primary_language(hints)] if primary_language(hints) else []) if l]
        for locale in self.languages:
            if locale not in locales:
                locales.append(locale)
        definition: dict[str, Any] = {
            "locales": locales,
            "diarization": {"enabled": True, "maxSpeakers": self.max_speakers},
            "profanityFilterMode": "None",   # see the batch path: a masked word is a changed quote
        }
        droppable = ["diarization", "profanityFilterMode", "locales"]
        vocabulary = safe_vocabulary(hints, limit=50)
        if vocabulary:
            definition["phraseList"] = {"phrases": list(vocabulary)}
            droppable.insert(0, "phraseList")
        return definition, droppable

    def _fast_to_transcript(
        self,
        doc: Any,
        *,
        size: int,
        dropped: Sequence[str],
        hints: Hints,
    ) -> Transcript:
        if not isinstance(doc, dict):
            raise EngineResponseError("azure fast transcription returned a non-object")
        if "phrases" not in doc and "combinedPhrases" not in doc:
            raise EngineResponseError(
                "azure fast transcription returned neither 'phrases' nor 'combinedPhrases' — "
                f"the API shape has changed; keys were: {sorted(doc)[:12]}"
            )
        segments: list[Segment] = []
        locales: set[str] = set()
        for phrase in doc.get("phrases") or []:
            if not isinstance(phrase, dict):
                continue
            text = str(phrase.get("text") or "").strip()
            if not text:
                continue
            start = _seconds_from_ms(phrase.get("offsetMilliseconds"))
            length = _seconds_from_ms(phrase.get("durationMilliseconds"))
            speaker = phrase.get("speaker")
            segments.append(
                Segment(
                    start=start,
                    end=start + length,
                    speaker=f"speaker_{speaker}" if speaker is not None else None,
                    text=text,
                )
            )
            if phrase.get("locale"):
                locales.add(str(phrase["locale"]))

        combined = doc.get("combinedPhrases") or []
        text = ""
        if isinstance(combined, list) and combined and isinstance(combined[0], dict):
            text = str(combined[0].get("text") or "").strip()
        if not text:
            text = " ".join(s.text for s in segments if s.text).strip()

        metadata: dict[str, Any] = {
            "mode": "fast",
            "api_version": FAST_API_VERSION,
            "endpoint": f"{self.base_url}/speechtotext/transcriptions:transcribe",
            "request_bytes": size,
            "segments_available": bool(segments),
            "speakers_available": any(s.speaker for s in segments),
            # Stated, not implied: fast and batch are different services and a reader
            # comparing two transcripts must be able to see which produced which.
            "note": "fast transcription was used because no content URL was available for batch",
        }
        if locales:
            metadata["locales_seen"] = sorted(locales)
        if dropped:
            metadata["dropped_fields"] = list(dropped)
            metadata["degraded"] = True
        return new_transcript(
            engine=self.name,
            text=text,
            segments=segments,
            language=sorted(locales)[0] if locales else (primary_language(hints) or None),
            duration_s=_seconds_from_ms(doc.get("durationMilliseconds")) or None,
            metadata=metadata,
        )

    # -- shared -------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": self.api_key}

    def _with_api_version(self, url: str) -> str:
        if "api-version=" in url:
            return url
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}api-version={BATCH_API_VERSION}"


def _seconds_from_ticks(value: Any) -> float:
    try:
        return float(value) / TICKS_PER_SECOND
    except (TypeError, ValueError):
        return 0.0


def _seconds_from_ms(value: Any) -> float:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def _job_id(job_url: str) -> str:
    return (job_url or "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _named_property(body: str, candidates: Sequence[str]) -> str | None:
    lowered = (body or "").lower()
    # Longest first: an error naming "languages" must not be blamed on "language".
    for name in sorted(candidates, key=len, reverse=True):
        if name.lower() in lowered:
            return name
    return None


register("azure", AzureSpeechEngine.from_config)
