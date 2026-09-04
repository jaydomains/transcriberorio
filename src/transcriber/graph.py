"""Microsoft Graph client — OneDrive change feed, downloads, uploads, moves.

Why urllib and not a HTTP library: this service must run unattended for years, and every
dependency is something that can rot underneath it while nobody is watching.

Two facts established against the live tenant (ARCHITECTURE.md) are load-bearing here:

  * ``@microsoft.graph.downloadUrl`` and the ``file.hashes`` facet are NOT returned by
    ``/delta`` on business accounts. Anything that needs either must re-``GET`` the item.
    ``DriveItem.from_api`` therefore leaves those fields empty rather than pretending.
  * A ``/children`` listing is not guaranteed complete while writes are happening, so
    enumeration that must not lose a file goes through ``delta``, never ``list_children``.

Nothing in here decides anything about the business; it moves bytes and reports failures
loudly. Every failure path raises a typed error carrying the HTTP status, because a Graph
call that fails quietly is the exact bug this service exists to remove.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from typing import Any, Callable, Iterator, Mapping, Sequence

log = logging.getLogger("transcriber.graph")

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
USER_AGENT = "kbc-transcriber/1.0 (+stdlib-urllib)"

#: Graph requires every upload-session chunk except the last to be a multiple of 320 KiB.
UPLOAD_CHUNK_UNIT = 320 * 1024
#: Above this, a simple PUT is not allowed and an upload session is required.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
#: What :meth:`GraphClient.read_small` will hold in memory. A status file is a few
#: hundred bytes, so this is generous by three orders of magnitude and still refuses
#: anything that could be audio.
SIMPLE_READ_LIMIT = 256 * 1024
DEFAULT_UPLOAD_CHUNK = 10 * UPLOAD_CHUNK_UNIT  # 3.2 MiB
DOWNLOAD_CHUNK = 1024 * 1024
#: Refresh this long before the token actually expires, so a long upload cannot straddle it.
TOKEN_REFRESH_SKEW_S = 300.0

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 509})

#: How many times one download may fetch a fresh pre-authenticated URL. Two covers a long
#: recording outliving its URL twice; beyond that a 403 is what it looks like — a refusal —
#: and spinning on it would turn a clear error into a slow one.
_MAX_URL_REFRESHES = 2
RETRY_AFTER_STATUSES = frozenset({429, 503, 504})


# --------------------------------------------------------------------------- errors


class GraphError(Exception):
    """Base for every failure this module raises. Callers may catch this and quarantine."""


class GraphConfigError(GraphError):
    """Missing or contradictory client configuration — a startup fault, not a runtime one."""


class GraphAuthError(GraphError):
    """Token acquisition failed. Carries the AAD status; never the secret."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GraphTransportError(GraphError):
    """The request never produced an HTTP status (DNS, TLS, reset, timeout)."""

    def __init__(self, message: str, *, method: str = "", url: str = "", attempts: int = 0) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.attempts = attempts


class GraphHTTPError(GraphError):
    """A Graph response we will not retry, or one we retried to exhaustion.

    ``status`` is the thing callers branch on, so it is a first-class attribute rather than
    something to be scraped back out of the message.
    """

    def __init__(
        self,
        status: int,
        *,
        method: str = "",
        url: str = "",
        code: str = "",
        message: str = "",
        request_id: str = "",
        retry_after: float | None = None,
        attempts: int = 1,
        body: str = "",
    ) -> None:
        detail = f"{method} {url} -> HTTP {status}"
        if code:
            detail += f" [{code}]"
        if message:
            detail += f": {message}"
        if request_id:
            detail += f" (request-id {request_id})"
        if attempts > 1:
            detail += f" after {attempts} attempts"
        super().__init__(detail)
        self.status = status
        self.method = method
        self.url = url
        self.code = code
        self.graph_message = message
        self.request_id = request_id
        self.retry_after = retry_after
        self.attempts = attempts
        self.body = body

    @property
    def is_not_found(self) -> bool:
        return self.status == 404

    @property
    def is_throttled(self) -> bool:
        return self.status == 429


class ResyncRequired(GraphError):
    """The delta cursor is too old: Graph answered 410 Gone / ``resyncRequired``.

    This is a normal, expected event (it happens after a long outage), not a crash. The
    caller must discard the stored cursor and re-enumerate from zero; because the ledger
    commits its cursor with the rows of the same page, a re-enumeration re-discovers
    everything and loses nothing.
    """

    def __init__(self, *, folder_id: str | None, cursor: str | None, status: int = 410) -> None:
        super().__init__(
            "delta cursor is no longer valid (HTTP %s resyncRequired); "
            "a full re-enumeration from a zero cursor is required" % status
        )
        self.folder_id = folder_id
        self.cursor = cursor
        self.status = status


class IncompleteDownload(GraphError):
    """Fewer bytes arrived than the server declared. Never treated as a successful fetch."""


class UploadSessionError(GraphError):
    """A chunked upload could not be completed or resumed."""


# --------------------------------------------------------------------------- helpers


def redact_url(url: str) -> str:
    """Strip the query string before anything is logged or put in an exception.

    A OneDrive pre-authenticated download URL and an upload-session URL are both bearer
    credentials in query-string form; neither may ever reach a log file or the ledger.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    shown = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return shown + ("?<redacted>" if parts.query else "")


def _parse_retry_after(value: str | None) -> float | None:
    """Retry-After is either delta-seconds or an HTTP-date. Honour both, exactly."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def _error_fields(body: bytes) -> tuple[str, str, str]:
    """Pull (code, message, request-id) out of a Graph error envelope, tolerantly."""
    try:
        doc = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return "", "", ""
    if not isinstance(doc, dict):
        return "", "", ""
    err = doc.get("error")
    if isinstance(err, str):  # AAD token endpoint shape
        return err, str(doc.get("error_description", ""))[:400], str(doc.get("correlation_id", ""))
    if not isinstance(err, dict):
        return "", "", ""
    code = str(err.get("code", ""))
    message = str(err.get("message", ""))[:400]
    request_id = ""
    inner = err.get("innerError")
    if isinstance(inner, dict):
        request_id = str(inner.get("request-id") or inner.get("requestId") or "")
        if not code:
            code = str(inner.get("code", ""))
    return code, message, request_id


def _is_resync(status: int) -> bool:
    """On the delta endpoint a 410 means one thing only: the cursor is too old to use."""
    return status == 410


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx back to the caller instead of following it.

    urllib copies request headers onto the redirect target, which would send our Graph
    Authorization header to Azure blob storage — storage rejects a request carrying two
    authentication mechanisms. Downloads therefore resolve the Location themselves and
    fetch it with no Authorization header at all.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib API
        return None


@dataclass(frozen=True)
class RetryPolicy:
    """429/503/504 honour Retry-After exactly; everything else retryable backs off.

    A non-429 4xx is never retried: it is a statement about our request, and repeating it
    just turns a visible bug into a slow visible bug.
    """

    max_attempts: int = 6
    base_delay: float = 1.0
    max_delay: float = 60.0
    max_retry_after: float = 900.0
    jitter: bool = True

    def backoff(self, attempt: int, rng: random.Random) -> float:
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        if not self.jitter:
            return delay
        # Full jitter: two workers throttled by the same page must not march in step.
        return rng.uniform(delay / 2.0, delay)


@dataclass
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            doc = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GraphError(
                f"Graph returned a body that is not JSON for {redact_url(self.url)}: {exc}"
            ) from exc
        return doc if isinstance(doc, dict) else {"value": doc}


# --------------------------------------------------------------------------- records


@dataclass
class DriveItem:
    """One OneDrive item, flattened to the fields the pipeline actually uses.

    ``download_url`` and ``hashes`` are empty on anything sourced from delta — that is a
    property of Graph on business accounts, not an omission here. ``raw`` keeps the full
    payload so a later question can be answered without another round trip.
    """

    id: str
    name: str
    size: int = 0
    parent_id: str = ""
    parent_path: str = ""
    drive_id: str = ""
    created_datetime: str = ""
    last_modified_datetime: str = ""
    mime_type: str = ""
    etag: str = ""
    ctag: str = ""
    web_url: str = ""
    is_folder: bool = False
    is_deleted: bool = False
    is_package: bool = False
    hashes: dict[str, str] = field(default_factory=dict)
    download_url: str = ""
    pending_operations: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, doc: Mapping[str, Any]) -> "DriveItem":
        file_facet = doc.get("file") or {}
        parent = doc.get("parentReference") or {}
        hashes = {k: str(v) for k, v in (file_facet.get("hashes") or {}).items() if v}
        pending: list[str] = []
        pending_facet = doc.get("pendingOperations")
        if isinstance(pending_facet, Mapping):
            pending.extend(str(k) for k in pending_facet.keys())
        elif pending_facet:
            pending.append("pendingOperations")
        if doc.get("pendingContentUpdate"):
            pending.append("pendingContentUpdate")
        return cls(
            id=str(doc.get("id", "")),
            name=str(doc.get("name", "")),
            size=int(doc.get("size") or 0),
            parent_id=str(parent.get("id", "")),
            parent_path=str(parent.get("path", "")),
            drive_id=str(parent.get("driveId", "")),
            created_datetime=str(doc.get("createdDateTime", "")),
            last_modified_datetime=str(doc.get("lastModifiedDateTime", "")),
            mime_type=str(file_facet.get("mimeType", "")),
            etag=str(doc.get("eTag", "")),
            ctag=str(doc.get("cTag", "")),
            web_url=str(doc.get("webUrl", "")),
            is_folder="folder" in doc,
            is_deleted="deleted" in doc,
            is_package="package" in doc,
            hashes=hashes,
            download_url=str(doc.get("@microsoft.graph.downloadUrl", "")),
            pending_operations=tuple(dict.fromkeys(pending)),
            raw=dict(doc),
        )

    @property
    def is_file(self) -> bool:
        return not self.is_folder and not self.is_deleted and not self.is_package


@dataclass
class DeltaPage:
    """One page of the change feed, with the cursor that supersedes the one we used.

    ``cursor`` is a full URL — a ``@odata.nextLink`` while more pages remain, then a
    ``@odata.deltaLink`` on the final page. Both are opaque and both are stored the same
    way. ``is_final`` says which it is, so the ledger knows when the sweep is complete.

    The ledger must persist ``cursor`` in the same transaction as ``items``. If the cursor
    could advance past a page whose rows were not written, a recording would be lost with
    nothing anywhere saying so.
    """

    items: list[DriveItem]
    cursor: str | None
    is_final: bool


@dataclass
class DownloadResult:
    path: str
    bytes_written: int
    sha256: str
    resumed: bool = False


# --------------------------------------------------------------------------- client


def ancestor_ids(get_item: Any, folder_id: str, *, limit: int = 32) -> tuple[str, ...]:
    """Every folder above this one on the drive, nearest first. Empty when unknown.

    A folder id says nothing about what contains it, and containment is the whole
    difference between two routes that merely sit near each other and two routes where one
    is quietly reading the other's recordings — OneDrive reports a folder and everything
    under it. So the chain has to be walked, and only a live drive can answer.

    Written as a function over ``get_item`` rather than as a method so that the setup
    wizard and the running service share ONE walk. They ask the same question for the same
    reason, and two copies of it are how they come to disagree about which folders overlap.

    **Never raises, and never reports a failed walk as an answer.** A chain that cannot be
    walked comes back empty, which every caller reads as "not known" rather than as "no
    overlap": a refusal invented from a Graph call that failed would be worse than the bug
    it was looking for.

    ``limit`` bounds the walk. A drive tree cannot contain a cycle, but a walk that trusted
    the drive to say so would hang the service if one ever did.
    """
    wanted = (folder_id or "").strip()
    if not wanted:
        return ()
    chain: list[str] = []
    seen = {wanted}
    current = wanted
    for _ in range(max(1, int(limit))):
        try:
            item = get_item(current)
        except Exception:  # noqa: BLE001 - an unanswerable question is not a problem
            break
        parent = str(getattr(item, "parent_id", "") or "").strip()
        if not parent or parent in seen:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return tuple(chain)


class GraphClient:
    """Client-credentials Graph client for exactly one drive.

    Thread-safe: ``worker.py`` runs downloads concurrently, and one token is shared.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        drive_id: str = "",
        user_id: str = "",
        graph_root: str = GRAPH_ROOT,
        login_root: str = LOGIN_ROOT,
        timeout: float = 60.0,
        download_timeout: float = 300.0,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("tenant_id", tenant_id),
                ("client_id", client_id),
                ("client_secret", client_secret),
            )
            if not value
        ]
        if missing:
            raise GraphConfigError(
                "GraphClient is missing required settings: " + ", ".join(missing)
            )
        if not drive_id and not user_id:
            raise GraphConfigError(
                "GraphClient needs either drive_id or user_id to address a drive"
            )
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret = client_secret
        self.drive_id = drive_id
        self.user_id = user_id
        self.graph_root = graph_root.rstrip("/")
        self.login_root = login_root.rstrip("/")
        self.timeout = timeout
        self.download_timeout = download_timeout
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._rng = random.Random()
        self._lock = threading.Lock()
        self._token: str = ""
        self._token_expires_at: float = 0.0
        self._opener = opener or urllib.request.build_opener()
        self._no_redirect_opener = urllib.request.build_opener(_NoRedirect())

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "GraphClient":
        """Build from whatever ``config.py`` exposes, without importing it.

        Duck-typed on purpose: the config module is owned elsewhere, and a hard import
        would couple two modules that only need to agree on five attribute names.
        """
        kwargs: dict[str, Any] = {}
        for name in (
            "tenant_id",
            "client_id",
            "client_secret",
            "drive_id",
            "user_id",
        ):
            value = getattr(config, name, None)
            if value:
                kwargs[name] = str(value)
        for name in ("timeout", "download_timeout", "retry"):
            value = getattr(config, "graph_" + name, None)
            if value is not None:
                kwargs[name] = value
        kwargs.update(overrides)
        kwargs.setdefault("drive_id", "")
        kwargs.setdefault("user_id", "")
        missing = [n for n in ("tenant_id", "client_id", "client_secret") if not kwargs.get(n)]
        if missing:
            raise GraphConfigError(
                "config is missing Graph settings: " + ", ".join(missing)
            )
        return cls(**kwargs)

    def __repr__(self) -> str:  # secrets never reach a log line, even by accident
        return (
            f"GraphClient(tenant_id={self.tenant_id!r}, client_id={self.client_id!r}, "
            f"drive={self._drive_base()!r}, client_secret=<redacted>)"
        )

    # -- addressing --------------------------------------------------------

    def _drive_base(self) -> str:
        if self.drive_id:
            return f"{self.graph_root}/drives/{urllib.parse.quote(self.drive_id, safe='')}"
        return f"{self.graph_root}/users/{urllib.parse.quote(self.user_id, safe='')}/drive"

    def _item_base(self, item_id: str | None) -> str:
        if not item_id or item_id == "root":
            return f"{self._drive_base()}/root"
        return f"{self._drive_base()}/items/{urllib.parse.quote(item_id, safe='')}"

    # -- token -------------------------------------------------------------

    def _access_token(self, force_refresh: bool = False) -> str:
        """Client-credentials token, refreshed before expiry and never logged."""
        with self._lock:
            now = self._clock()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token
            payload = urllib.parse.urlencode(
                {
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "scope": GRAPH_SCOPE,
                    "grant_type": "client_credentials",
                }
            ).encode("ascii")
            url = f"{self.login_root}/{urllib.parse.quote(self.tenant_id, safe='')}/oauth2/v2.0/token"
            try:
                resp = self._request(
                    "POST",
                    url,
                    body=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    auth=False,
                )
            except GraphHTTPError as exc:
                raise GraphAuthError(
                    f"could not obtain a Graph token: {exc.code or 'HTTP %d' % exc.status}"
                    f" {exc.graph_message}".strip(),
                    status=exc.status,
                ) from exc
            except GraphTransportError as exc:
                raise GraphAuthError(f"could not reach the token endpoint: {exc}") from exc
            doc = resp.json()
            token = str(doc.get("access_token", ""))
            if not token:
                raise GraphAuthError("token endpoint returned no access_token")
            try:
                expires_in = float(doc.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600.0
            self._token = token
            self._token_expires_at = self._clock() + max(60.0, expires_in - TOKEN_REFRESH_SKEW_S)
            log.info(
                "graph token acquired, valid for %ds (refreshing %ds early)",
                int(expires_in),
                int(TOKEN_REFRESH_SKEW_S),
            )
            return self._token

    # -- transport ---------------------------------------------------------

    def _build_request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str] | None,
        auth: bool,
    ) -> urllib.request.Request:
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        if auth:
            req.add_header("Authorization", f"Bearer {self._access_token()}")
        return req

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
        allow_redirects: bool = True,
        retry_statuses: frozenset[int] = RETRYABLE_STATUSES,
    ) -> Response:
        """One Graph call with the whole retry policy applied. Raises, never returns junk."""
        shown = redact_url(url)
        opener = self._opener if allow_redirects else self._no_redirect_opener
        attempt = 0
        refreshed_token = False
        while True:
            attempt += 1
            req = self._build_request(method, url, body=body, headers=headers, auth=auth)
            try:
                with opener.open(req, timeout=timeout or self.timeout) as raw:
                    payload = raw.read()
                    return Response(
                        status=raw.status,
                        headers={k.lower(): v for k, v in raw.headers.items()},
                        body=payload,
                        url=url,
                    )
            except urllib.error.HTTPError as exc:
                payload = b""
                try:
                    payload = exc.read()
                except Exception:  # the body is a nicety; the status is the fact
                    pass
                hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
                if not allow_redirects and 300 <= exc.code < 400:
                    return Response(status=exc.code, headers=hdrs, body=payload, url=url)
                code, message, request_id = _error_fields(payload)
                retry_after = _parse_retry_after(hdrs.get("retry-after"))
                err = GraphHTTPError(
                    exc.code,
                    method=method,
                    url=shown,
                    code=code,
                    message=message,
                    request_id=request_id,
                    retry_after=retry_after,
                    attempts=attempt,
                    body=payload[:2000].decode("utf-8", "replace"),
                )
                if exc.code == 401 and auth and not refreshed_token:
                    # An expired token looks exactly like a permissions fault; try once
                    # with a fresh one so a real permissions fault is what actually surfaces.
                    refreshed_token = True
                    log.warning("graph 401 on %s %s; refreshing token and retrying once", method, shown)
                    self._access_token(force_refresh=True)
                    continue
                if exc.code not in retry_statuses or attempt >= self.retry.max_attempts:
                    log.error("graph %s %s failed: %s", method, shown, err)
                    raise err from exc
                delay = self._retry_delay(exc.code, retry_after, attempt)
                log.warning(
                    "graph %s %s -> HTTP %d%s; retrying in %.1fs (attempt %d/%d)",
                    method,
                    shown,
                    exc.code,
                    f" [{code}]" if code else "",
                    delay,
                    attempt,
                    self.retry.max_attempts,
                )
                self._sleep(delay)
            except (urllib.error.URLError, HTTPException, socket.timeout, ConnectionError, OSError) as exc:
                if attempt >= self.retry.max_attempts:
                    log.error("graph %s %s: transport failure: %s", method, shown, exc)
                    raise GraphTransportError(
                        f"{method} {shown} failed after {attempt} attempts: {exc!r}",
                        method=method,
                        url=shown,
                        attempts=attempt,
                    ) from exc
                delay = self.retry.backoff(attempt, self._rng)
                log.warning(
                    "graph %s %s: transport failure %r; retrying in %.1fs (attempt %d/%d)",
                    method, shown, exc, delay, attempt, self.retry.max_attempts,
                )
                self._sleep(delay)

    def _retry_delay(self, status: int, retry_after: float | None, attempt: int) -> float:
        """Retry-After is obeyed exactly on 429/503/504; anything else backs off with jitter."""
        if status in RETRY_AFTER_STATUSES and retry_after is not None:
            return min(retry_after, self.retry.max_retry_after)
        return self.retry.backoff(attempt, self._rng)

    def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        return self._request("GET", url, **kwargs).json()

    # -- change feed -------------------------------------------------------

    def delta(self, folder_id: str | None = None, cursor: str | None = None) -> Iterator[DeltaPage]:
        """Walk the change feed, yielding one ``DeltaPage`` per Graph page.

        ``cursor=None`` means enumerate from zero — the whole folder, which is what the
        nightly sweep uses (``/children`` may be short while his phone is writing).

        Raises ``ResyncRequired`` when Graph rejects the stored cursor with 410. That is a
        signal, not a crash: catch it, drop the cursor, call again with ``cursor=None``.
        ``delta_with_resync`` does exactly that if the caller has nowhere better to do it.
        """
        url = cursor or self._delta_start_url(folder_id)
        while True:
            try:
                doc = self._get_json(url)
            except GraphHTTPError as exc:
                if _is_resync(exc.status):
                    log.warning(
                        "graph delta cursor rejected (HTTP 410 %s); a full resync is required",
                        exc.code or "resyncRequired",
                    )
                    raise ResyncRequired(folder_id=folder_id, cursor=cursor, status=exc.status) from exc
                raise
            values = doc.get("value")
            items = [DriveItem.from_api(v) for v in values if isinstance(v, Mapping)] if isinstance(values, list) else []
            next_link = doc.get("@odata.nextLink")
            delta_link = doc.get("@odata.deltaLink")
            if next_link:
                yield DeltaPage(items=items, cursor=str(next_link), is_final=False)
                url = str(next_link)
                continue
            # No nextLink: this is the last page, and deltaLink is the cursor to store.
            yield DeltaPage(items=items, cursor=str(delta_link) if delta_link else None, is_final=True)
            if not delta_link:
                log.error(
                    "graph delta page had neither @odata.nextLink nor @odata.deltaLink; "
                    "the cursor cannot be advanced and the next poll will repeat this page"
                )
            return

    def delta_with_resync(
        self,
        folder_id: str | None = None,
        cursor: str | None = None,
        on_resync: Callable[[ResyncRequired], None] | None = None,
    ) -> Iterator[DeltaPage]:
        """``delta`` that survives an expired cursor by re-enumerating from zero, once.

        ``on_resync`` is called before the restart so the ledger can clear its stored
        cursor and the digest can say out loud that a full resync happened — a resync is
        visible, never silent.
        """
        try:
            yield from self.delta(folder_id, cursor)
            return
        except ResyncRequired as exc:
            if cursor is None:
                raise  # a zero cursor cannot itself be stale; something else is wrong
            if on_resync is not None:
                on_resync(exc)
            log.warning("graph delta restarting from a zero cursor after resyncRequired")
        yield from self.delta(folder_id, None)

    def _delta_start_url(self, folder_id: str | None) -> str:
        return f"{self._item_base(folder_id)}/delta?%s" % urllib.parse.urlencode({"$top": 200})

    # -- items -------------------------------------------------------------

    def get_item(self, item_id: str) -> DriveItem:
        """Full item, including the two things delta withholds: downloadUrl and hashes.

        No ``$select`` here on purpose — narrowing the projection is how you lose the
        ``file.hashes`` facet and quietly disable the completeness gate.
        """
        return DriveItem.from_api(self._get_json(self._item_base(item_id)))

    def ancestor_ids(self, folder_id: str, *, limit: int = 32) -> tuple[str, ...]:
        """Every folder above this one on the drive, nearest first. Empty when unknown.

        The walk is :func:`ancestor_ids`; this is the method form for callers that already
        hold a client.
        """
        return ancestor_ids(self.get_item, folder_id, limit=limit)

    def get_item_by_path(self, path: str) -> DriveItem:
        """Item addressed by drive-relative path, e.g. ``CALLS/2026-08``."""
        clean = "/".join(urllib.parse.quote(p, safe="") for p in path.strip("/").split("/") if p)
        if not clean:
            return DriveItem.from_api(self._get_json(f"{self._drive_base()}/root"))
        return DriveItem.from_api(self._get_json(f"{self._drive_base()}/root:/{clean}"))

    def list_children(self, folder_id: str | None = None) -> list[DriveItem]:
        """Children of a folder, following every page.

        NOT for enumeration that must be complete: a listing taken while writes are in
        flight can come back short, which is how the original measurement stuck at 200
        items. Use ``delta`` wherever a missed file would matter.
        """
        url = f"{self._item_base(folder_id)}/children?%s" % urllib.parse.urlencode({"$top": 200})
        out: list[DriveItem] = []
        while url:
            doc = self._get_json(url)
            values = doc.get("value")
            if isinstance(values, list):
                out.extend(DriveItem.from_api(v) for v in values if isinstance(v, Mapping))
            url = str(doc.get("@odata.nextLink") or "")
        return out

    def move(self, item_id: str, parent_id: str, new_name: str | None = None) -> DriveItem:
        """Move (and optionally rename). The item id survives a move within a drive, which
        is why the ledger keys on it.

        The conflict behaviour is stated rather than left to Graph's default, because this
        is the one call in the service that touches somebody's only copy of a recording. Two
        kinds of recording can carry the same name — a phone writes
        ``Call Carel_260827_143005.m4a`` and WhatsApp writes ``PTT-20260827-WA0001.opus`` —
        so a destination that already holds that name is reachable, and the two possible
        defaults are "fail" and "replace the file that is there". ``fail`` makes it a visible
        409 the archive pass reports in his words; the alternative would have this module
        destroy an original, which it exists not to do.
        """
        payload: dict[str, Any] = {
            "parentReference": {"id": parent_id},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        if new_name:
            payload["name"] = new_name
        resp = self._request(
            "PATCH",
            self._item_base(item_id),
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return DriveItem.from_api(resp.json())

    # -- download ----------------------------------------------------------

    def download(
        self,
        item_id: str,
        dest_path: str,
        *,
        download_url: str = "",
        expected_size: int | None = None,
        chunk_size: int = DOWNLOAD_CHUNK,
    ) -> DownloadResult:
        """Stream an item to ``dest_path``. A 90 MB recording never sits in memory.

        Writes to ``<dest_path>.part`` and renames on success, so a half-written file can
        never be mistaken for a complete one by anything that comes along later. Resumes
        with a Range request if the connection drops mid-stream.
        """
        url = download_url
        if not url:
            url, reported_size = self._resolve_download_url(item_id)
            if expected_size is None:
                expected_size = reported_size
        part_path = dest_path + ".part"
        parent = os.path.dirname(os.path.abspath(part_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        declared_total: int | None = expected_size
        attempt = 0
        total_attempts = 0
        url_refreshes = 0
        resumed = False
        try:
            with open(part_path, "wb") as sink:
                while True:
                    attempt += 1
                    total_attempts += 1
                    # Where this attempt starts. `attempt` bounds CONSECUTIVE failures, not
                    # total interruptions over the whole file, and this is what tells the two
                    # apart. The chunked-upload loop below has always reset on progress; this
                    # one did not, so an hour-long recording pulled over a site LTE link that
                    # drops every fifteen megabytes exhausted the budget while making steady
                    # forward progress, and the .part file holding everything fetched so far
                    # was deleted on the way out. The next pipeline attempt then started from
                    # zero against the same link and reached the same end.
                    before = written
                    headers: dict[str, str] = {"Accept-Encoding": "identity"}
                    if written:
                        headers["Range"] = f"bytes={written}-"
                    req = urllib.request.Request(url, method="GET")
                    req.add_header("User-Agent", USER_AGENT)
                    for key, value in headers.items():
                        req.add_header(key, value)
                    # No Authorization header: the download URL is itself pre-authenticated,
                    # and storage rejects a request carrying two authentication mechanisms.
                    try:
                        with self._opener.open(req, timeout=self.download_timeout) as raw:
                            if written and raw.status == 200:
                                # Server ignored the Range; start over rather than splice.
                                sink.seek(0)
                                sink.truncate()
                                digest = hashlib.sha256()
                                written = 0
                            content_length = raw.headers.get("Content-Length")
                            if content_length is not None:
                                try:
                                    declared_total = written + int(content_length)
                                except ValueError:
                                    pass
                            while True:
                                block = raw.read(chunk_size)
                                if not block:
                                    break
                                sink.write(block)
                                digest.update(block)
                                written += len(block)
                        break
                    except urllib.error.HTTPError as exc:
                        payload = b""
                        try:
                            payload = exc.read()
                        except Exception:
                            pass
                        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
                        code, message, request_id = _error_fields(payload)
                        if written > before:
                            attempt = 0
                        # A 401 or 403 HERE is not the credential fault it is on an API call.
                        # The download URL is pre-authenticated and expires — roughly an hour
                        # — so on a long recording that has been sleeping through 503s with a
                        # Retry-After, storage answers 403 because the URL went stale, not
                        # because the app may not read it. Re-resolving is the whole answer,
                        # and the line that did it sat below a guard that raised first, so it
                        # could never run: the recording was quarantined on its first attempt
                        # with a message saying a retry would not change the answer, when the
                        # next attempt would have fetched a fresh URL and finished. Bounded,
                        # so a genuinely forbidden download still fails quickly and says so.
                        stale_url = exc.code in (401, 403) and url_refreshes < _MAX_URL_REFRESHES
                        if ((exc.code not in RETRYABLE_STATUSES and not stale_url)
                                or attempt >= self.retry.max_attempts):
                            raise GraphHTTPError(
                                exc.code,
                                method="GET",
                                url=redact_url(url),
                                code=code,
                                message=message,
                                request_id=request_id,
                                attempts=total_attempts,
                                body=payload[:2000].decode("utf-8", "replace"),
                            ) from exc
                        delay = self._retry_delay(
                            exc.code, _parse_retry_after(hdrs.get("retry-after")), max(1, attempt)
                        )
                        log.warning(
                            "download %s -> HTTP %d; retrying in %.1fs "
                            "(%d in a row of %d allowed, %d interruptions so far)",
                            item_id, exc.code, delay,
                            max(1, attempt), self.retry.max_attempts, total_attempts,
                        )
                        self._sleep(delay)
                        if stale_url:
                            url_refreshes += 1
                            url, _ = self._resolve_download_url(item_id)
                    except (urllib.error.URLError, HTTPException, socket.timeout, ConnectionError, OSError) as exc:
                        if written > before:
                            attempt = 0
                        if attempt >= self.retry.max_attempts:
                            raise GraphTransportError(
                                f"download of {item_id} failed after {total_attempts} attempts "
                                f"at {written} bytes: {exc!r}",
                                method="GET",
                                url=redact_url(url),
                                attempts=total_attempts,
                            ) from exc
                        delay = self.retry.backoff(max(1, attempt), self._rng)
                        log.warning(
                            "download %s interrupted at %d bytes (%r); resuming in %.1fs "
                            "(%d in a row of %d allowed, %d interruptions so far)",
                            item_id, written, exc, delay,
                            max(1, attempt), self.retry.max_attempts, total_attempts,
                        )
                        resumed = True
                        self._sleep(delay)
            if declared_total is not None and written != declared_total:
                raise IncompleteDownload(
                    f"{item_id}: wrote {written} bytes but {declared_total} were declared "
                    f"— refusing to treat a short file as a download"
                )
            os.replace(part_path, dest_path)
        except BaseException:
            # A failed download leaves no half-file behind to be picked up as real.
            try:
                if os.path.exists(part_path):
                    os.unlink(part_path)
            except OSError:
                log.warning("could not remove partial download %s", part_path)
            raise
        log.info("downloaded %s -> %s (%d bytes)", item_id, dest_path, written)
        return DownloadResult(
            path=dest_path, bytes_written=written, sha256=digest.hexdigest(), resumed=resumed
        )

    def _resolve_download_url(self, item_id: str) -> tuple[str, int | None]:
        """A pre-authenticated content URL, and the size Graph reports for the item.

        Returning the size means ``download`` can check the byte count it actually wrote
        without the caller having to remember to supply one.
        """
        item = self.get_item(item_id)
        if item.download_url:
            return item.download_url, (item.size or None)
        resp = self._request(
            "GET", f"{self._item_base(item_id)}/content", allow_redirects=False
        )
        location = resp.headers.get("location", "")
        if 300 <= resp.status < 400 and location:
            return location, (item.size or None)
        raise GraphError(
            f"no download URL for item {item_id}: /content answered HTTP {resp.status} "
            f"with no Location header"
        )

    # -- upload ------------------------------------------------------------

    def delete(self, item_id: str) -> bool:
        """Delete one item. True if it went, False if it was already gone.

        **THIS MOVES THE FILE TO THE RECYCLE BIN. IT DOES NOT DESTROY IT.** Graph's DELETE
        on a driveItem is what the web interface's delete button does: the file leaves the
        folder and sits in the recycle bin, where OneDrive keeps it for around 30 days for a
        personal account and up to 93 for a business one, and where an administrator can
        restore it the whole time.

        That is stated here, loudly, because the caller is the erasure path and the person
        who asked to be forgotten deserves an accurate answer. "We deleted it" and "we put
        it in a bin that empties itself in three months" are different sentences, and only
        the second one is true until the bin is emptied. :mod:`transcriber.erase` prints
        that in its report rather than letting the word "deleted" carry a meaning it has not
        earned.

        A 404 is success, not failure. Somebody asked for this to be gone; a file that was
        already gone is the requested state, and treating it as an error would stop an
        erasure part-way through on the one recording that needed no work.
        """
        try:
            self._request("DELETE", self._item_base(item_id))
        except GraphHTTPError as exc:
            if exc.is_not_found:
                return False
            raise
        return True

    def read_small(self, item_id: str, *, limit: int = SIMPLE_READ_LIMIT) -> bytes:
        """A small file's whole content, in memory. For status files and nothing bigger.

        Deliberately NOT a general-purpose read. :meth:`download` exists precisely so a
        90 MB recording never sits in memory, and this does the opposite thing on purpose —
        so it refuses anything that is not small, and a caller who reaches for it with an
        audio file gets an error rather than a machine that swaps.

        The size is checked BEFORE the fetch, against what the item says it is, because
        ``_request`` reads a whole body into memory before returning it: a check afterwards
        would be a check made after the damage. It is checked again after, because the
        length a remote service reports is a claim and the bytes are the fact.

        The content URL is pre-authenticated and points at storage rather than at Graph, so
        it is fetched with ``auth=False``. Sending our bearer token to it would hand a
        credential to a host that has no business holding one.
        """
        item = self.get_item(item_id)
        if item.size and item.size > limit:
            raise GraphError(
                f"read_small refuses {item.name!r} at {item.size} bytes: the limit is "
                f"{limit}. Use download() for anything that is not a small text file."
            )
        url, reported = self._resolve_download_url(item_id)
        if reported and reported > limit:
            raise GraphError(
                f"read_small refuses {item.name!r}: /content reports {reported} bytes, "
                f"over the {limit} limit."
            )
        resp = self._request("GET", url, auth=False)
        body = resp.body or b""
        if len(body) > limit:
            raise GraphError(
                f"read_small refuses {item.name!r}: it sent {len(body)} bytes, whatever "
                "its metadata said."
            )
        return body

    def upload_small(
        self, parent_id: str, name: str, data: bytes, *, conflict: str = "replace"
    ) -> DriveItem:
        """Single-PUT upload. Graph caps this at 4 MB; larger goes to ``upload_session``."""
        if len(data) > SIMPLE_UPLOAD_LIMIT:
            raise GraphError(
                f"upload_small refuses {len(data)} bytes for {name!r}: the limit is "
                f"{SIMPLE_UPLOAD_LIMIT} bytes — use upload_session"
            )
        url = "%s:/%s:/content?%s" % (
            self._item_base(parent_id),
            urllib.parse.quote(name, safe=""),
            urllib.parse.urlencode({"@microsoft.graph.conflictBehavior": conflict}),
        )
        resp = self._request(
            "PUT", url, body=data, headers={"Content-Type": "application/octet-stream"}
        )
        return DriveItem.from_api(resp.json())

    def upload(
        self,
        parent_id: str,
        name: str,
        data: bytes | None = None,
        *,
        source_path: str = "",
        conflict: str = "replace",
    ) -> DriveItem:
        """Upload by whichever route the size allows. Convenience for ``outputs.py``."""
        if (data is None) == (not source_path):
            raise GraphError("upload needs exactly one of data= or source_path=")
        size = len(data) if data is not None else os.path.getsize(source_path)
        if size <= SIMPLE_UPLOAD_LIMIT:
            payload = data if data is not None else open(source_path, "rb").read()
            return self.upload_small(parent_id, name, payload, conflict=conflict)
        return self.upload_session(
            parent_id, name, data=data, source_path=source_path, conflict=conflict
        )

    def upload_session(
        self,
        parent_id: str,
        name: str,
        *,
        data: bytes | None = None,
        source_path: str = "",
        conflict: str = "replace",
        chunk_size: int = DEFAULT_UPLOAD_CHUNK,
    ) -> DriveItem:
        """Chunked upload for anything over 4 MB, streamed from disk a chunk at a time.

        Resumes from Graph's own ``nextExpectedRanges`` after a retryable failure, and
        deletes the session if it gives up, so a dead session is not left holding a name.
        """
        if (data is None) == (not source_path):
            raise GraphError("upload_session needs exactly one of data= or source_path=")
        if chunk_size % UPLOAD_CHUNK_UNIT:
            raise GraphError(
                f"upload chunk size {chunk_size} is not a multiple of {UPLOAD_CHUNK_UNIT} "
                f"bytes, which Graph requires"
            )
        total = len(data) if data is not None else os.path.getsize(source_path)
        if total == 0:
            raise GraphError(f"refusing to open an upload session for an empty file: {name!r}")
        session_url = self._create_upload_session(parent_id, name, conflict)
        try:
            return self._run_upload_session(
                session_url, name, total, data=data, source_path=source_path, chunk_size=chunk_size
            )
        except BaseException:
            self._cancel_upload_session(session_url)
            raise

    def _create_upload_session(self, parent_id: str, name: str, conflict: str) -> str:
        url = "%s:/%s:/createUploadSession" % (
            self._item_base(parent_id),
            urllib.parse.quote(name, safe=""),
        )
        body = json.dumps(
            {"item": {"@microsoft.graph.conflictBehavior": conflict, "name": name}}
        ).encode("utf-8")
        doc = self._request(
            "POST", url, body=body, headers={"Content-Type": "application/json"}
        ).json()
        session_url = str(doc.get("uploadUrl", ""))
        if not session_url:
            raise UploadSessionError(f"Graph returned no uploadUrl for {name!r}")
        return session_url

    def _run_upload_session(
        self,
        session_url: str,
        name: str,
        total: int,
        *,
        data: bytes | None,
        source_path: str,
        chunk_size: int,
    ) -> DriveItem:
        reader = open(source_path, "rb") if source_path else None
        try:
            start = 0
            attempt = 0
            stalled = 0
            while start < total:
                end = min(start + chunk_size, total) - 1
                if reader is not None:
                    reader.seek(start)
                    block = reader.read(end - start + 1)
                else:
                    assert data is not None
                    block = data[start : end + 1]
                if not block:
                    raise UploadSessionError(
                        f"{name}: ran out of bytes at offset {start} of {total}"
                    )
                attempt += 1
                try:
                    resp = self._put_chunk(session_url, block, start, end, total)
                except (GraphHTTPError, GraphTransportError) as exc:
                    status = getattr(exc, "status", None)
                    if status is not None and status not in RETRYABLE_STATUSES:
                        raise
                    if attempt >= self.retry.max_attempts:
                        raise UploadSessionError(
                            f"{name}: upload session failed at byte {start} of {total} "
                            f"after {attempt} attempts: {exc}"
                        ) from exc
                    delay = self._retry_delay(
                        status or 503, getattr(exc, "retry_after", None), attempt
                    )
                    log.warning(
                        "upload %s: chunk at %d failed (%s); resuming in %.1fs",
                        name, start, exc, delay,
                    )
                    self._sleep(delay)
                    start = self._next_expected_offset(session_url, start)
                    continue
                attempt = 0
                if resp.status in (200, 201):
                    item = DriveItem.from_api(resp.json())
                    log.info("uploaded %s (%d bytes, chunked)", name, total)
                    return item
                doc = resp.json() if resp.body else {}
                next_start = _first_expected_offset(doc.get("nextExpectedRanges"), default=end + 1)
                # A session that accepts chunks without ever advancing would otherwise
                # loop forever, uploading the same bytes and reporting nothing.
                stalled = stalled + 1 if next_start <= start else 0
                if stalled >= 3:
                    raise UploadSessionError(
                        f"{name}: upload session stopped advancing at byte {start} of "
                        f"{total} — Graph kept asking for the same range"
                    )
                start = next_start
            raise UploadSessionError(
                f"{name}: all {total} bytes were sent but Graph never returned the item"
            )
        finally:
            if reader is not None:
                reader.close()

    def _put_chunk(self, session_url: str, block: bytes, start: int, end: int, total: int) -> Response:
        # No Authorization header: the session URL carries its own credential, and adding
        # ours would be a second authentication mechanism on the same request.
        return self._request(
            "PUT",
            session_url,
            body=block,
            headers={
                "Content-Length": str(len(block)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            },
            auth=False,
        )

    def _next_expected_offset(self, session_url: str, fallback: int) -> int:
        """Ask Graph what it actually has, rather than assuming our own bookkeeping."""
        try:
            doc = self._request("GET", session_url, auth=False).json()
        except GraphError as exc:
            log.warning("could not query upload session state: %s", exc)
            return fallback
        return _first_expected_offset(doc.get("nextExpectedRanges"), default=fallback)

    def _cancel_upload_session(self, session_url: str) -> None:
        try:
            self._request("DELETE", session_url, auth=False)
        except GraphError as exc:
            log.warning("could not cancel upload session: %s", exc)


def _first_expected_offset(ranges: Any, *, default: int) -> int:
    if isinstance(ranges, Sequence) and not isinstance(ranges, (str, bytes)):
        for entry in ranges:
            head = str(entry).split("-", 1)[0].strip()
            if head.isdigit():
                return int(head)
    return default
