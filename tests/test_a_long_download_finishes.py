"""Two ways an hour-long recording died on the way down the wire.

Both live only on the download path, and both were invisible on a small file: the failures
they turn into losses are the ones that take a long time to arrive. The recordings at risk
were the site meetings and the long client calls — the ones worth the most.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import urllib.error
import urllib.request

from transcriber.graph import GraphClient, GraphHTTPError, RetryPolicy

BODY = b"x" * 4096


class _Stream(io.BytesIO):
    """A response body that can stop short, the way a dropped connection does."""

    def __init__(self, data: bytes, status: int, headers: dict, *, cut_after: int | None = None):
        super().__init__(data)
        self.status = status
        self.headers = headers
        self._cut_after = cut_after
        self._served = 0

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if self._cut_after is not None:
            if self._served >= self._cut_after:
                raise ConnectionResetError("the link dropped")
            # Never hand back more than the link survives for, so the drop lands mid-file
            # rather than after the last byte — which is the whole point of the scenario.
            size = min(size if size and size > 0 else self._cut_after,
                       self._cut_after - self._served)
        block = super().read(size)
        self._served += len(block)
        return block

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Opener:
    """Serves the token call, then a scripted sequence of download answers."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.urls: list[str] = []

    def open(self, req, timeout=None):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "login.microsoftonline.com" in url or "oauth2" in url:
            return _Stream(b'{"access_token":"t","expires_in":3600}', 200,
                           {"Content-Type": "application/json"})
        self.urls.append(url)
        step = self.script.pop(0)
        return step(url, req)


def _client(script, tmp) -> GraphClient:
    return GraphClient(
        tenant_id="tenant", client_id="client", client_secret="secret", drive_id="DRIVE",
        retry=RetryPolicy(max_attempts=4, base_delay=0.0, jitter=False),
        sleep=lambda _s: None,
        opener=_Opener(script),
    )


def _range_start(req) -> int:
    """What the client asked to resume from. A real server honours this; so does this."""
    value = req.get_header("Range") or req.get_header("range") or ""
    if value.startswith("bytes="):
        try:
            return int(value[len("bytes="):].split("-")[0])
        except ValueError:
            return 0
    return 0


def _http_error(url: str, status: int) -> None:
    raise urllib.error.HTTPError(url, status, "no", {}, io.BytesIO(b'{"error":{"code":"x"}}'))


class ProgressResetsTheRetryBudget(unittest.TestCase):
    """The budget counts consecutive failures, not interruptions over the whole file.

    A long recording over a site link that drops every few megabytes was making steady
    forward progress and still ran out of attempts — and the part-file holding everything
    fetched so far is deleted on the way out, so the next attempt started again from zero
    against the same link and reached the same end.
    """

    def test_a_link_that_drops_repeatedly_still_finishes_the_file(self) -> None:
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "note.m4a")

        served = {"n": 0}

        def flaky(url: str, req):
            served["n"] += 1
            start = _range_start(req)
            body = BODY[start:]
            status = 206 if start else 200
            headers = {"Content-Length": str(len(body))}
            # Every attempt but the last delivers 800 bytes and then the link drops. Five
            # interruptions is more than max_attempts, and every one of them made progress.
            cut = 800 if served["n"] <= 5 else None
            return _Stream(body, status, headers, cut_after=cut)

        graph = _client([flaky] * 6, tmp)
        result = graph.download("01ABC", dest, download_url="https://storage.example/pre-auth")

        self.assertEqual(result.bytes_written, len(BODY))
        self.assertTrue(os.path.exists(dest))

    def test_but_failing_without_progress_still_gives_up(self) -> None:
        """The budget still exists. A link that delivers nothing is not retried forever."""
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "note.m4a")

        def dead(url: str, req):
            return _Stream(b"", 200, {"Content-Length": "4096"}, cut_after=0)

        graph = _client([dead] * 12, tmp)
        with self.assertRaises(Exception):
            graph.download("01ABC", dest, download_url="https://storage.example/pre-auth")
        self.assertFalse(os.path.exists(dest), "a failed download leaves no half file")


class AnExpiredDownloadUrlIsNotARefusal(unittest.TestCase):
    """A pre-authenticated URL expires. A 403 here means stale, not forbidden.

    On an API call a 403 is a credential fault and retrying is pointless, which is why the
    guard raised on it. But the download URL carries its own authorisation and lasts about
    an hour, so a long recording that has been waiting out a storage 503 outlives it. The
    recovery — fetch a fresh URL — was written and sat below a guard that raised first, so
    the recording was quarantined on its first attempt with a message saying a retry could
    not help, when a retry was exactly what would have finished it.
    """

    def test_a_stale_url_is_resolved_again_and_the_download_completes(self) -> None:
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "note.m4a")

        def expired(url: str, req=None):
            _http_error(url, 403)

        def item_lookup(url: str, req=None):
            return _Stream(
                b'{"id":"01ABC","name":"note.m4a","size":4096,'
                b'"@microsoft.graph.downloadUrl":"https://storage.example/fresh"}',
                200, {"Content-Type": "application/json"},
            )

        def good(url: str, req=None):
            return _Stream(BODY, 200, {"Content-Length": str(len(BODY))})

        graph = _client([expired, item_lookup, good], tmp)
        result = graph.download("01ABC", dest, download_url="https://storage.example/stale")

        self.assertEqual(result.bytes_written, len(BODY))

    def test_but_a_url_that_keeps_refusing_gives_up_rather_than_spinning(self) -> None:
        tmp = tempfile.mkdtemp()
        dest = os.path.join(tmp, "note.m4a")

        def expired(url: str, req=None):
            _http_error(url, 403)

        def item_lookup(url: str, req=None):
            return _Stream(
                b'{"id":"01ABC","name":"note.m4a","size":4096,'
                b'"@microsoft.graph.downloadUrl":"https://storage.example/fresh"}',
                200, {"Content-Type": "application/json"},
            )

        script = [expired, item_lookup, expired, item_lookup, expired, item_lookup, expired]
        graph = _client(script, tmp)
        with self.assertRaises(GraphHTTPError):
            graph.download("01ABC", dest, download_url="https://storage.example/stale")


if __name__ == "__main__":
    unittest.main()
