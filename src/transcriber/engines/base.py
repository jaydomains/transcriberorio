"""What every transcription engine shares: the protocol, the registry, HTTP, and retries.

Why this file exists at all: there is no ``requests`` in this service and there never will
be, so multipart/form-data has to be encoded by hand exactly once rather than three times
badly. The same argument applies to the retry rules — an engine that invents its own
backoff is an engine that throttles differently from the rest of the pipeline for reasons
nobody can find later.

Three properties are deliberate and load-bearing.

**Nothing degrades quietly.** A request that cannot be made raises. A response that cannot
be understood raises. Where a call is retried after dropping an optional hint, the fact is
written into ``Transcript.engine_metadata`` so the transcript itself records that it was
produced with less help than intended, rather than looking identical to one that was not.

**No secret and no address reaches an exception, a log line or a metadata dict.** Error
bodies are scrubbed of every configured secret before they are allowed into an exception
message, because an engine's 401 body routinely echoes the key that failed.

**No engine can be outside the rate limit.** ``ENGINE_MAX_CONCURRENT`` and
``ENGINE_MAX_PER_MINUTE`` are enforced here, in the client every engine's requests go
through and around the ``transcribe`` of every engine this module builds, rather than in
each engine — where the fourth one would eventually be written without it. The limiter is
about not provoking a provider's limit; the ``RetryPolicy`` below is about obeying it when
it is hit anyway. They are separate on purpose and neither replaces the other.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import contextlib
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any, BinaryIO, Callable, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from ..models import Hints, Segment, Transcript, strip_emails
from ..ratelimit import RateLimiter, configure_shared, shared_limiter

__all__ = [
    "EngineError",
    "EngineConfigError",
    "EngineAuthError",
    "EngineHTTPError",
    "EngineTransportError",
    "EngineResponseError",
    "EngineAudioTooLarge",
    "Engine",
    "Response",
    "RetryPolicy",
    "HttpClient",
    "LimitedEngine",
    "engine_limiter",
    "FilePart",
    "MultipartBody",
    "USER_AGENT",
    "register",
    "registered_engines",
    "create_engine",
    "engine_for_name",
    "guess_audio_content_type",
    "iso639_1",
    "iso639_3",
    "primary_language",
    "safe_vocabulary",
    "segments_from_words",
    "new_transcript",
    "Word",
    "parse_retry_after",
    "redact_url",
]

log = logging.getLogger("transcriber.engines")

USER_AGENT = "kbc-transcriber/1.0 (+stdlib-urllib)"

#: Retried only on these. A 4xx that is not 429 is a statement about our request; repeating
#: it turns a visible bug into a slow visible bug.
RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
#: Statuses where the server told us how long to wait, and we obey it rather than guess.
RETRY_AFTER_STATUSES = frozenset({429, 503, 504})


# --------------------------------------------------------------------------- errors


class EngineError(Exception):
    """Base class. Every failure out of this package is one of these, and every one is loud."""


class EngineConfigError(EngineError):
    """The engine cannot be built or used as configured. Raised before any network call."""


class EngineAuthError(EngineError):
    """401/403 from the provider. Never carries the credential that failed."""


class EngineTransportError(EngineError):
    """The request never produced an HTTP response — DNS, TLS, timeout, reset connection."""

    def __init__(self, message: str, *, url: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.url = url
        self.attempts = attempts


class EngineHTTPError(EngineError):
    """A non-retryable, or finally-failing, HTTP status from a provider."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        url: str = "",
        body: str = "",
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body
        self.attempts = attempts


class EngineResponseError(EngineError):
    """The provider answered, and the answer is not the shape this engine can read.

    Distinct from :class:`EngineHTTPError` on purpose: this is the failure that means the
    provider changed its API under us, which is the one thing a stdlib-only client cannot
    absorb silently.
    """


class EngineAudioTooLarge(EngineError):
    """The file exceeds what this engine accepts and splitting is the caller's job."""

    def __init__(self, message: str, *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes


# --------------------------------------------------------------------------- protocol


@runtime_checkable
class Engine(Protocol):
    """The whole contract. ``max_bytes`` of None means the engine takes whole files."""

    name: str
    max_bytes: int | None

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        ...


# --------------------------------------------------------------------------- registry

#: name -> factory taking a Config and returning an Engine.
_REGISTRY: dict[str, Callable[[Any], Engine]] = {}


def register(name: str, factory: Callable[[Any], Engine]) -> None:
    """Add an engine to the registry. Re-registering the same name replaces it.

    Called at import time by each engine module; :mod:`transcriber.engines` imports all
    three, so ``create_engine`` sees them all with no plugin discovery machinery.
    """
    key = (name or "").strip().lower()
    if not key:
        raise EngineConfigError("an engine must register under a non-empty name")
    _REGISTRY[key] = factory


def registered_engines() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


class LimitedEngine:
    """An engine that holds a concurrency slot for the whole of one transcription.

    The HTTP client is limited too, but per request, and that is not the same statement:
    Azure's batch API submits a job and then polls it for half an hour, so limiting only its
    requests would let eight recordings be in the air at an engine configured for three.
    Holding the slot around ``transcribe`` is what makes ``ENGINE_MAX_CONCURRENT`` mean
    "recordings at this provider at once" for all three engines rather than for two of them.

    It is a wrapper and not a base class because engines satisfy a Protocol rather than
    inherit anything, and a base class is exactly the sort of thing a fourth engine could be
    written without. Everything the engine exposes is forwarded, so the Azure content-URL
    provider and ``max_bytes`` reach their engine unchanged.
    """

    def __init__(self, engine: Engine, limiter: RateLimiter) -> None:
        self.engine = engine
        self.limiter = limiter

    @property
    def name(self) -> str:
        return self.engine.name

    @property
    def max_bytes(self) -> int | None:
        return self.engine.max_bytes

    def transcribe(self, path: str, hints: Hints) -> Transcript:
        with self.limiter.slot():
            return self.engine.transcribe(path, hints)

    def __getattr__(self, item: str) -> Any:
        # Reached only for attributes this wrapper does not define — with_content_url_provider,
        # a model name, whatever a later engine grows. Guarded against the half-built case so
        # a failure in __init__ raises AttributeError rather than recursing.
        try:
            engine = object.__getattribute__(self, "engine")
        except AttributeError:
            raise AttributeError(item) from None
        return getattr(engine, item)

    def __repr__(self) -> str:
        return f"LimitedEngine({self.engine!r}, {self.limiter.describe()})"


def engine_limiter(config: Any) -> RateLimiter:
    """The shared limiter, pointed at this configuration. Never raises."""
    return configure_shared(config)


def engine_for_name(name: str, config: Any) -> Engine:
    """Build one engine by name, or fail naming every engine that does exist.

    Every engine leaves here inside a :class:`LimitedEngine`. This is the one construction
    path the service uses, so the rate limit is not something a caller remembers to apply —
    it is a property of having an engine at all.
    """
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise EngineConfigError(
            f"no transcription engine named {name!r} — registered engines are: "
            + (", ".join(registered_engines()) or "(none)")
        )
    limiter = engine_limiter(config)
    return LimitedEngine(factory(config), limiter)


def create_engine(config: Any, name: str | None = None) -> Engine:
    """The engine named by ``TRANSCRIBE_ENGINE``, or by ``name`` when one is forced."""
    return engine_for_name(name or getattr(config, "engine", ""), config)


# --------------------------------------------------------------------------- retries


@dataclass(frozen=True)
class RetryPolicy:
    """429/503/504 honour ``Retry-After`` exactly; other retryable statuses back off.

    ``max_retry_after`` exists because a provider under load will occasionally ask for an
    hour, and a worker holding a ledger lease for an hour is a worker whose lease expires
    and whose file is picked up twice. Past the cap we stop and let the item be retried by
    the normal attempt machinery, visibly.
    """

    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    max_retry_after: float = 300.0
    jitter: bool = True

    def backoff(self, attempt: int, rng: random.Random) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        if not self.jitter:
            return delay
        # Full jitter: two workers throttled by the same response must not march in step.
        return rng.uniform(delay / 2.0, delay)


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """``Retry-After`` is either delta-seconds or an HTTP-date. Honour both, exactly."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(int(text)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


# --------------------------------------------------------------------------- multipart


@dataclass(frozen=True)
class FilePart:
    """One file in a multipart body, streamed from disk rather than read into memory."""

    field: str
    filename: str
    path: str
    content_type: str = "application/octet-stream"

    @property
    def size(self) -> int:
        return os.path.getsize(self.path)


class _ChainReader(io.RawIOBase):
    """Reads a sequence of byte blocks and open files as one stream, for urllib.

    urllib will happily take a file-like body as long as Content-Length is set, which is
    what lets a 200MB recording be posted without ever being a 200MB bytes object.
    """

    def __init__(self, chunks: Sequence[bytes | str]) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._current: BinaryIO | None = None

    def readable(self) -> bool:
        return True

    def _advance(self) -> None:
        if self._current is not None:
            self._current.close()
            self._current = None
        self._index += 1

    def read(self, size: int = -1) -> bytes:  # noqa: D102 - file-object API
        out = bytearray()
        while size < 0 or len(out) < size:
            if self._index >= len(self._chunks):
                break
            chunk = self._chunks[self._index]
            if isinstance(chunk, bytes):
                if size < 0:
                    out += chunk
                    self._advance()
                    continue
                want = size - len(out)
                if len(chunk) <= want:
                    out += chunk
                    self._advance()
                else:
                    out += chunk[:want]
                    self._chunks[self._index] = chunk[want:]
                continue
            if self._current is None:
                self._current = open(chunk, "rb")
            piece = self._current.read(io.DEFAULT_BUFFER_SIZE if size < 0 else size - len(out))
            if not piece:
                self._advance()
                continue
            out += piece
        return bytes(out)

    def readall(self) -> bytes:  # noqa: D102 - file-object API
        return self.read(-1)

    def close(self) -> None:  # noqa: D102 - file-object API
        if self._current is not None:
            self._current.close()
            self._current = None
        super().close()


class MultipartBody:
    """A multipart/form-data body that can be re-opened for each retry attempt.

    Re-openable matters: a retried POST needs the body from byte zero, and a consumed
    stream silently posts nothing. That is precisely the class of failure this service
    exists to eliminate, so the body is a plan, not a stream, until ``open`` is called.
    """

    def __init__(
        self,
        fields: Sequence[tuple[str, str]] = (),
        files: Sequence[FilePart] = (),
        *,
        boundary: str | None = None,
    ) -> None:
        self.boundary = boundary or ("----kbc-transcriber-" + uuid.uuid4().hex)
        self._chunks: list[bytes | str] = []
        self._length = 0
        dash = f"--{self.boundary}\r\n".encode()
        for name, value in fields:
            header = (
                f'Content-Disposition: form-data; name="{_quote_field(name)}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode()
            payload = str(value).encode("utf-8") + b"\r\n"
            self._add(dash + header + payload)
        for part in files:
            header = (
                f'Content-Disposition: form-data; name="{_quote_field(part.field)}"; '
                f'filename="{_quote_field(part.filename)}"\r\n'
                f"Content-Type: {part.content_type}\r\n\r\n"
            ).encode()
            self._add(dash + header)
            self._add_file(part.path)
            self._add(b"\r\n")
        self._add(f"--{self.boundary}--\r\n".encode())

    def _add(self, blob: bytes) -> None:
        self._chunks.append(blob)
        self._length += len(blob)

    def _add_file(self, path: str) -> None:
        self._chunks.append(path)
        self._length += os.path.getsize(path)

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    @property
    def length(self) -> int:
        return self._length

    def open(self) -> BinaryIO:
        return _ChainReader(list(self._chunks))  # type: ignore[return-value]

    def to_bytes(self) -> bytes:
        """Only for tests and small bodies — the whole point of the reader is not doing this."""
        with self.open() as handle:
            return handle.read()


def _quote_field(name: str) -> str:
    """RFC 7578 leaves quoting under-specified; percent-encoding the two characters that
    can break a header is the interoperable choice every provider accepts."""
    return name.replace("\\", "%5C").replace('"', "%22").replace("\r", "").replace("\n", "")


def guess_audio_content_type(path: str) -> str:
    """A correct content type is not cosmetic: OpenAI's endpoint identifies the format from
    the filename and content type, and rejects the upload when neither says what it is."""
    fixed = {
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
        ".aac": "audio/aac",
        ".amr": "audio/amr",
        ".3gp": "audio/3gpp",
        ".wma": "audio/x-ms-wma",
        ".caf": "audio/x-caf",
    }
    ext = os.path.splitext(path)[1].lower()
    if ext in fixed:
        return fixed[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


# --------------------------------------------------------------------------- http


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise EngineResponseError(
                f"{redact_url(self.url)} answered {self.status} with a body that is not JSON: {exc}"
            ) from exc

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def redact_url(url: str) -> str:
    """Strip the query before anything is logged: a SAS URL and a single-use token are both
    credentials in query-string form."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    shown = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return shown + ("?<redacted>" if parts.query else "")


class HttpClient:
    """One retrying HTTP client, shared by all three engines.

    ``secrets`` are the literal strings that must never appear in an exception message or a
    log line. Providers echo the offending key back in a 401 body more often than not.
    """

    def __init__(
        self,
        *,
        timeout_s: int = 60,
        policy: RetryPolicy | None = None,
        secrets: Sequence[str] = (),
        user_agent: str = USER_AGENT,
        opener: urllib.request.OpenerDirector | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.timeout_s = int(timeout_s)
        self.policy = policy or RetryPolicy()
        self.user_agent = user_agent
        self._secrets = tuple(s for s in secrets if isinstance(s, str) and len(s) >= 4)
        #: Secrets belonging to one request rather than to the client. See :meth:`hiding`.
        self._transient: list[str] = []
        self._opener = opener or urllib.request.build_opener()
        self._sleep = sleep
        self._rng = rng or random.Random()
        #: The process-wide engine limiter unless a caller hands over its own. Defaulted
        #: rather than injected because an engine that built its own client without one
        #: would be an engine outside the rate limit, and there must not be one of those.
        #: The analysis pass borrows this client too, so its calls are paced by the same
        #: budget; a caller that wants its own passes its own ``RateLimiter`` here.
        self.limiter = limiter if limiter is not None else shared_limiter()

    # -- redaction ---------------------------------------------------------------

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for secret in tuple(self._secrets) + tuple(self._transient):
            text = text.replace(secret, "***REDACTED***")
        return strip_emails(text)

    @contextlib.contextmanager
    def hiding(self, *values: str) -> "Iterator[None]":
        """Scrub ``values`` from anything this client reports, for the length of the block.

        For a secret that belongs to one request rather than to the client. The Azure batch
        path is the case it was written for: it hands the vendor OneDrive's
        pre-authenticated download URL, which is a bearer capability to the recording — no
        header, no token, anyone with the link has the audio — and the client's fixed secret
        list holds only the Azure API key. A 4xx whose body echoes the request therefore
        carried that URL verbatim into the exception, into ``last_error``, into the ledger
        and into the 06:00 email.
        """
        keep = [v for v in values if isinstance(v, str) and len(v) >= 8]
        self._transient.extend(keep)
        try:
            yield
        finally:
            for value in keep:
                try:
                    self._transient.remove(value)
                except ValueError:  # pragma: no cover - only if something else cleared it
                    pass

    # -- the one request method --------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        multipart: MultipartBody | None = None,
        json_body: Any = None,
        expected: Sequence[int] = (200, 201, 202, 204),
        max_attempts: int | None = None,
    ) -> Response:
        if sum(x is not None for x in (body, multipart, json_body)) > 1:
            raise EngineConfigError("give request() exactly one of body, multipart or json_body")

        base_headers: dict[str, str] = {"User-Agent": self.user_agent, "Accept": "application/json"}
        base_headers.update({k: v for k, v in (headers or {}).items()})
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            base_headers["Content-Type"] = "application/json"
        if body is not None:
            base_headers["Content-Length"] = str(len(body))
        if multipart is not None:
            base_headers["Content-Type"] = multipart.content_type
            base_headers["Content-Length"] = str(multipart.length)

        attempts = max_attempts or self.policy.max_attempts
        last_status = 0
        last_body = ""
        # Outside the retry loop on purpose: the limiter's job is to avoid provoking a
        # rate limit, and the backoff below is what obeys the provider when it says no
        # anyway. Neither replaces the other, and the slot is held across a backoff so a
        # throttled request cannot be overtaken by three more of the same.
        with self.limiter.slot():
            for attempt in range(1, attempts + 1):
                # One token per attempt, not per call: a retry is another request as far
                # as the provider's per-minute allowance is concerned, and counting it as
                # nothing is how a retry storm walks straight into the limit it is
                # backing off from.
                self.limiter.take_token()
                data: Any = body
                stream: BinaryIO | None = None
                if multipart is not None:
                    stream = multipart.open()
                    data = stream
                request = urllib.request.Request(url, data=data, method=method.upper())
                for key, value in base_headers.items():
                    request.add_header(key, value)
                try:
                    with self._opener.open(request, timeout=self.timeout_s) as handle:
                        payload = handle.read()
                        response = Response(
                            status=handle.status,
                            headers={k.lower(): v for k, v in handle.headers.items()},
                            body=payload,
                            url=url,
                        )
                    if response.status in expected:
                        return response
                    last_status, last_body = response.status, self.scrub(response.text()[:600])
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                except urllib.error.HTTPError as exc:
                    payload = b""
                    try:
                        payload = exc.read()
                    except Exception:  # the body is a courtesy; its absence is not the failure
                        payload = b""
                    last_status = exc.code
                    last_body = self.scrub(payload.decode("utf-8", "replace")[:600])
                    if exc.code in (401, 403):
                        raise EngineAuthError(
                            f"{redact_url(url)} rejected the credential with {exc.code}. "
                            f"Provider said: {last_body or '(no body)'}"
                        ) from None
                    if exc.code not in RETRYABLE_STATUSES:
                        raise EngineHTTPError(
                            f"{method.upper()} {redact_url(url)} failed with {exc.code}: "
                            f"{last_body or '(no body)'}",
                            status=exc.code,
                            url=url,
                            body=last_body,
                            attempts=attempt,
                        ) from None
                    retry_after = parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                except (urllib.error.URLError, HTTPException, socket.timeout, ConnectionError, OSError) as exc:
                    if attempt >= attempts:
                        raise EngineTransportError(
                            f"{method.upper()} {redact_url(url)} never completed after {attempt} "
                            f"attempt(s): {self.scrub(str(exc))}",
                            url=url,
                            attempts=attempt,
                        ) from exc
                    delay = self.policy.backoff(attempt, self._rng)
                    log.warning(
                        "%s %s failed at the transport (%s); retrying in %.1fs (attempt %d/%d)",
                        method.upper(), redact_url(url), self.scrub(str(exc)), delay, attempt, attempts,
                    )
                    self._sleep(delay)
                    continue
                finally:
                    if stream is not None:
                        stream.close()

                # A retryable status. Either wait as instructed, or back off.
                if attempt >= attempts:
                    break
                delay = self._delay_for(last_status, retry_after, attempt)
                if delay is None:
                    break
                log.warning(
                    "%s %s returned %d; retrying in %.1fs (attempt %d/%d)",
                    method.upper(), redact_url(url), last_status, delay, attempt, attempts,
                )
                self._sleep(delay)

        raise EngineHTTPError(
            f"{method.upper()} {redact_url(url)} still failing with {last_status} after "
            f"{attempts} attempt(s): {last_body or '(no body)'}",
            status=last_status,
            url=url,
            body=last_body,
            attempts=attempts,
        )

    def _delay_for(self, status: int, retry_after: float | None, attempt: int) -> float | None:
        """None means stop retrying — the wait the server asked for is longer than a lease."""
        if status in RETRY_AFTER_STATUSES and retry_after is not None:
            if retry_after > self.policy.max_retry_after:
                log.error(
                    "provider asked for a %.0fs wait on %d, which is longer than this "
                    "service is willing to hold a claim; failing loudly instead",
                    retry_after, status,
                )
                return None
            return retry_after
        return self.policy.backoff(attempt, self._rng)

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)


# --------------------------------------------------------------------------- hints


#: The locales this service actually sees, mapped to what the three providers want. Kept
#: tiny and explicit rather than pulling in a locale library: two languages are in play.
_ISO1 = {
    "en": "en", "eng": "en", "en-za": "en", "en-gb": "en", "en-us": "en",
    "af": "af", "afr": "af", "af-za": "af",
    "zu": "zu", "zul": "zu", "zu-za": "zu",
    "xh": "xh", "xho": "xh", "xh-za": "xh",
    "st": "st", "sot": "st", "st-za": "st",
    "tn": "tn", "tsn": "tn", "tn-za": "tn",
}
_ISO3 = {
    "en": "eng", "af": "afr", "zu": "zul", "xh": "xho", "st": "sot", "tn": "tsn",
}


def iso639_1(locale: str | None) -> str | None:
    """``en-ZA`` -> ``en``. Returns None rather than guessing at an unknown tag."""
    if not locale:
        return None
    key = locale.strip().lower().replace("_", "-")
    if key in _ISO1:
        return _ISO1[key]
    head = key.split("-", 1)[0]
    return _ISO1.get(head)


def iso639_3(locale: str | None) -> str | None:
    """``en-ZA`` -> ``eng``. ElevenLabs takes either; the three-letter form is unambiguous."""
    one = iso639_1(locale)
    return _ISO3.get(one) if one else None


def primary_language(hints: Hints) -> str | None:
    """The one language to declare when an engine only accepts one. Never invented."""
    if hints.language:
        return hints.language
    return hints.languages[0] if hints.languages else None


def safe_vocabulary(hints: Hints, limit: int = 100) -> tuple[str, ...]:
    """Vocabulary on its way to a provider: de-duplicated, capped, and address-free.

    The cap is not cosmetic — every provider rejects an over-long keyword list, and a
    rejected request is a transcript we do not get. The address strip is the house rule:
    an operator who puts a contact into VOCABULARY must not have it typed at a third party.
    """
    out: list[str] = []
    seen: set[str] = set()
    for term in hints.vocabulary:
        cleaned = strip_emails((term or "").strip())
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
        if len(out) >= limit:
            break
    if hints.counterparty:
        name = strip_emails(hints.counterparty.strip())
        if name and name.lower() not in seen:
            out.insert(0, name)
    return tuple(out)


# --------------------------------------------------------------------------- mapping


@dataclass
class Word:
    """One timed word, as the word-level engines return them, before grouping."""

    text: str
    start: float
    end: float
    speaker: str | None = None
    kind: str = "word"


def segments_from_words(
    words: Sequence[Word],
    *,
    max_gap_s: float = 0.9,
    max_chars: int = 320,
) -> list[Segment]:
    """Group timed words into readable segments, breaking on speaker, pause and sentence.

    Engines that return words and no segments (ElevenLabs) still have to produce a
    speaker-labelled body downstream, and a body of one word per line is not readable by
    the person who has to confirm what was said.
    """
    segments: list[Segment] = []
    buffer: list[str] = []
    start = 0.0
    end = 0.0
    speaker: str | None = None

    def flush() -> None:
        nonlocal buffer, start, end, speaker
        text = "".join(buffer).strip()
        if text:
            segments.append(Segment(start=start, end=max(end, start), speaker=speaker, text=text))
        buffer = []

    for word in words:
        if word.kind == "spacing":
            if buffer:
                buffer.append(word.text or " ")
                end = max(end, word.end)
            continue
        piece = word.text or ""
        if not piece.strip():
            continue
        boundary = (
            not buffer
            or word.speaker != speaker
            or (word.start - end) > max_gap_s
            or len("".join(buffer)) >= max_chars
        )
        if boundary and buffer:
            flush()
        if not buffer:
            start = word.start
            speaker = word.speaker
        elif buffer and not buffer[-1].endswith((" ", "\n")):
            buffer.append(" ")
        buffer.append(piece)
        end = max(end, word.end)
    flush()
    return segments


def new_transcript(
    *,
    engine: str,
    text: str,
    segments: Sequence[Segment],
    language: str | None,
    duration_s: float | None,
    metadata: Mapping[str, Any],
) -> Transcript:
    """One place that builds a Transcript, so ``engine`` and the address rule are never
    forgotten by one engine and remembered by the other two."""
    clean_segments = [
        Segment(start=s.start, end=s.end, speaker=s.speaker, text=strip_emails(s.text))
        for s in segments
    ]
    return Transcript(
        text=strip_emails(text),
        segments=clean_segments,
        language=language,
        engine_metadata=dict(metadata),
        engine=engine,
        duration_s=duration_s,
    )
