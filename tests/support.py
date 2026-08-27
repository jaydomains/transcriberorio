"""Builders and stand-ins shared by the suite. No network, no credentials, no clock.

Everything a test needs to stand a real object up: a :class:`~transcriber.config.Config`
with plausible values and no secrets worth having, a fake Graph client whose delta pages
are scripted, a scripted urllib opener for the retry tests, and minimal stand-ins for the
analysis pass's results so the renderers can be tested without it.

The fakes are deliberately thin. A fake that reimplements the thing it stands in for ends
up testing itself.
"""

from __future__ import annotations

import io
import os
import tempfile
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from transcriber import graph as graph_module
from transcriber.config import Config
from transcriber.models import AudioInfo

__all__ = [
    "make_config",
    "FakeDeltaPage",
    "FakeGraph",
    "ScriptedOpener",
    "ScriptedResponse",
    "TokenThenScriptedOpener",
    "audio_info",
    "StubProposal",
    "StubQuoteCheck",
    "StubExtraction",
    "StubRouting",
]


def make_config(**overrides: Any) -> Config:
    """A Config with every required field filled and nothing real in it.

    Built directly rather than through ``from_env`` so a test never depends on the ambient
    environment — and so no test can accidentally pick up a live credential.
    """
    values: dict[str, Any] = {
        "graph_tenant_id": "tenant-for-tests",
        "graph_client_id": "client-for-tests",
        "graph_client_secret": "not-a-real-secret",
        "graph_user_id": "drive-owner",
        "source_folder_id": "SOURCE",
        "output_folder_id": "OUTPUT",
        "archive_folder_id": "ARCHIVE",
        "engine": "openai",
        "engine_key": "not-a-real-engine-key",
        "analysis_api_key": "not-a-real-analysis-key",
        "smtp_host": "smtp.invalid",
        "smtp_user": "digest",
        "smtp_password": "not-a-real-password",
        "smtp_from": "digest@invalid",
        "smtp_to": ("someone@invalid",),
        "heartbeat_url": "",
        "ledger_path": ":memory:",
        "work_dir": os.path.join(tempfile.gettempdir(), "transcriber-tests"),
        "poll_interval_s": 1,
        "settle_interval_s": 1,
        "lease_seconds": 60,
        "concurrency": 1,
        "max_attempts": 2,
    }
    values.update(overrides)
    return Config(**values)


# --------------------------------------------------------------------------- Graph


class FakeDeltaPage:
    """One scripted page, shaped like :class:`transcriber.graph.DeltaPage`."""

    def __init__(self, items: Sequence[Any], cursor: str | None, is_final: bool = True) -> None:
        self.items = list(items)
        self.cursor = cursor
        self.is_final = is_final


def _drive_item(item_id: str, name: str, size: int = 1024, **extra: Any) -> Any:
    payload: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "size": size,
        "eTag": f'"{item_id}-1"',
        "cTag": f'"c{item_id}"',
        "file": {"mimeType": "audio/mp4", "hashes": {"quickXorHash": "AAAA"}},
        "parentReference": {"id": "SOURCE"},
        "createdDateTime": "2026-08-27T09:00:00Z",
        "lastModifiedDateTime": "2026-08-27T09:00:05Z",
        "webUrl": f"https://example.invalid/{item_id}",
    }
    payload.update(extra)
    return graph_module.DriveItem.from_api(payload)


class FakeGraph:
    """A Graph client whose change feed is a script and whose calls are counted.

    ``pages`` is a list of (items, cursor) pairs, replayed from whatever cursor the caller
    passes: a caller that has not stored a cursor gets the whole script again, which is
    exactly the behaviour the cursor invariant is asserted against.
    """

    def __init__(self, pages: Sequence[tuple[Sequence[Any], str | None]]) -> None:
        self.pages = [(list(items), cursor) for items, cursor in pages]
        self.delta_calls: list[str | None] = []

    item = staticmethod(_drive_item)

    def _from(self, cursor: str | None) -> Iterator[FakeDeltaPage]:
        start = 0
        if cursor is not None:
            for index, (_items, page_cursor) in enumerate(self.pages):
                if page_cursor == cursor:
                    start = index + 1
                    break
            else:
                start = 0
        for items, page_cursor in self.pages[start:]:
            yield FakeDeltaPage(items, page_cursor, is_final=page_cursor is not None)

    def delta(self, folder_id: str | None = None, cursor: str | None = None) -> Iterator[FakeDeltaPage]:
        self.delta_calls.append(cursor)
        return self._from(cursor)

    def delta_with_resync(
        self,
        folder_id: str | None = None,
        cursor: str | None = None,
        on_resync: Callable[[Any], None] | None = None,
    ) -> Iterator[FakeDeltaPage]:
        self.delta_calls.append(cursor)
        yield from self._from(cursor)


# --------------------------------------------------------------------------- HTTP


class ScriptedResponse:
    """One canned HTTP answer. ``raises`` makes urllib behave as it does for a 4xx/5xx."""

    def __init__(
        self,
        status: int,
        body: bytes = b"{}",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers or {})


class _FakeHandle:
    def __init__(self, response: ScriptedResponse, url: str) -> None:
        self.status = response.status
        self.headers = response.headers
        self._body = response.body
        self.url = url

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHandle":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class ScriptedOpener:
    """Stands in for ``urllib.request.OpenerDirector``. Records every request it served.

    A status outside 2xx is raised as ``HTTPError``, which is what urllib really does and
    what the retry code under test is written against.
    """

    def __init__(self, responses: Iterable[ScriptedResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    def open(self, request: Any, timeout: float | None = None) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        self.requests.append((request.get_method(), url))
        if not self.responses:
            raise AssertionError(f"no scripted response left for {url}")
        response = self.responses.pop(0)
        if 200 <= response.status < 300:
            return _FakeHandle(response, url)
        raise urllib.error.HTTPError(
            url,
            response.status,
            "scripted",
            response.headers,  # type: ignore[arg-type]
            # A readable body, because the code under test reads it: an error message is
            # where a provider echoes the key back, and the redaction of that is a test.
            io.BytesIO(response.body),
        )


class TokenThenScriptedOpener(ScriptedOpener):
    """Answers the Entra token endpoint for free, then follows the script.

    The token call is not what any of these tests are about, and scripting it in every one
    of them would bury the thing being asserted.
    """

    def open(self, request: Any, timeout: float | None = None) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if "/oauth2/" in url or url.endswith("/token"):
            self.requests.append((request.get_method(), url))
            return _FakeHandle(
                ScriptedResponse(
                    200,
                    b'{"access_token":"token-for-tests","expires_in":3600,"token_type":"Bearer"}',
                    {"Content-Type": "application/json"},
                ),
                url,
            )
        return super().open(request, timeout)


# --------------------------------------------------------------------------- records


def audio_info(duration_s: float = 754.0, **overrides: Any) -> AudioInfo:
    fields: dict[str, Any] = {
        "duration_s": duration_s,
        "container": "mp4",
        "truncated": False,
        "reason": "the container is complete",
        "size_bytes": int(duration_s * 4000),
        "probed_by": "walk",
        "detail": {"duration_known": True},
    }
    fields.update(overrides)
    return AudioInfo(**fields)


# --------------------------------------------------------------------------- extraction

# The renderers read the extraction by attribute rather than importing it, so these stand
# in for it without dragging the analysis pass into an output test.


class StubQuoteCheck:
    def __init__(self, ok: bool = True, method: str = "exact", ratio: float = 1.0) -> None:
        self.ok = ok
        self.method = method
        self.ratio = ratio

    def __bool__(self) -> bool:
        return self.ok


class StubProposal:
    def __init__(self, category: str, item: Any, check: Any | None = None) -> None:
        self.category = category
        self.item = item
        self.quote_check = check or StubQuoteCheck()

    @property
    def kind(self) -> str:
        return self.item.kind


class StubRouting:
    def __init__(self, substantive: bool = True, why: str = "read in full") -> None:
        self.label = "substantive" if substantive else "trivial"
        self.substantive = substantive
        self.forced = True
        self.escalated = False
        self.triggers = ()
        self.model_label = "substantive"
        self.one_line = ""
        self._why = why

    def why(self) -> str:
        return self._why


class StubExtraction:
    def __init__(
        self,
        *,
        summary: str = "",
        proposals: Sequence[Any] = (),
        review: Sequence[Any] = (),
        participants: Sequence[Any] = (),
        site: str = "",
        site_quote: str = "",
        notes: Sequence[str] = (),
        unclear: Sequence[Any] = (),
        trivial: bool = False,
        languages: Sequence[str] = ("en-ZA",),
    ) -> None:
        self.routing = StubRouting(substantive=not trivial)
        self.summary = summary
        self.proposals = tuple(proposals)
        self.review = tuple(review)
        self.participants = tuple(participants)
        self.site = site
        self.site_quote = site_quote
        self.notes = tuple(notes)
        self.unclear = tuple(unclear)
        self.trivial = trivial
        self.languages = tuple(languages)
        self.models_used = ("router-model", "reader-model")
        self.redacted = False

    def by_category(self) -> dict[str, tuple[Any, ...]]:
        grouped: dict[str, list[Any]] = {}
        for proposal in self.proposals:
            grouped.setdefault(proposal.category, []).append(proposal)
        return {key: tuple(value) for key, value in grouped.items()}

    def for_category(self, category: str) -> tuple[Any, ...]:
        return tuple(p for p in self.proposals if p.category == category)
