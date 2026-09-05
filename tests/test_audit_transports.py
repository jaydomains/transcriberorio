"""Two ways a credential could leave this service without anybody seeing it happen.

Both are transport faults, and both are invisible in a log: the service reports success,
the recording is transcribed, and the key is simply also in somebody else's hands.

  * **A redirect carries the headers with it.** urllib follows a 3xx by default and copies
    the request's headers onto the new host, stripping only ``Content-Length`` and
    ``Content-Type``. Every credential this service holds travels in a header, so one 302
    from anything answering on a provider's address — a captive portal, a proxy, a typo in
    a configured URL, a hijacked DNS answer — hands that credential to whoever sent the
    redirect. A POST is the worst case, because the audio goes too.
  * **"The token endpoint said no" and "the token endpoint never answered" are opposite
    facts.** The first means a person must issue a new client secret; the second means the
    network was not up yet, which is the ordinary shape of a boot after a power cut. One
    exception type for both is how a service that needed to wait thirty seconds instead
    burns its restart budget and sits dead until somebody notices.

These tests do not mock the transport. Two real HTTP servers are stood up on loopback: the
first stands in for a provider that answers with a redirect, and the second records every
header it is handed. Anything reaching the second server is something that left this
service, so the assertion is simply that it received nothing — and, on the one path that
is *supposed* to fetch from a second host, that what it received carried no credential.
"""

from __future__ import annotations

import http.server
import os
import socket
import tempfile
import threading
import unittest
import urllib.request

from transcriber import heartbeat as heartbeat_module
from transcriber.engines.base import EngineHTTPError, HttpClient
from transcriber.engines.base import RetryPolicy as EngineRetryPolicy
from transcriber.graph import (
    GraphAuthError,
    GraphAuthUnreachable,
    GraphClient,
    GraphError,
    GraphHTTPError,
    RetryPolicy,
)
from transcriber.ratelimit import RateLimiter
from transcriber.redirects import NoRedirect

#: The strings that must never reach the second server. Long enough to be unmistakable in
#: a captured header, and shaped like the three providers' real credentials.
BEARER = "test-bearer-token-0123456789"
ELEVENLABS_KEY = "test-xi-api-key-0123456789"
AZURE_KEY = "test-ocp-apim-key-0123456789"

CREDENTIAL_HEADERS = {
    "Authorization": f"Bearer {BEARER}",
    "xi-api-key": ELEVENLABS_KEY,
    "ocp-apim-subscription-key": AZURE_KEY,
}

BLOB = b"0123456789"  # ten bytes, and the item metadata below declares ten


#: What the suite-wide network ban was replaced with while this module runs. See
#: :func:`setUpModule`.
_BANNED: dict = {}

_LOOPBACK = ("127.0.0.1", "::1", "localhost")

#: The unpatched connect, taken from the C-level class ``socket.socket`` is built on,
#: because by the time this module is imported the ban has already replaced the one on
#: ``socket.socket`` itself and there is no other copy of it left to borrow.
_RAW_CONNECT = socket.socket.__base__.connect
_RAW_CONNECT_EX = socket.socket.__base__.connect_ex


def setUpModule() -> None:
    """Allow connections to 127.0.0.1, and nothing else, for the length of this module.

    ``tests/__init__.py`` refuses every outbound connection, and it is right to: a test
    that quietly reaches a real endpoint is how a suite becomes slow, flaky and dependent
    on a credential. But the thing under test here is what urllib's own opener does with a
    3xx before this service's code ever sees it, and no stand-in opener can answer that —
    a fake opener is the exact layer whose behaviour is in question.

    So the ban is lifted here in the narrowest form that lets the question be asked: this
    module's own loopback servers, for the length of this module, with everything else
    still refused by the same assertion and the original ban put back in ``tearDownModule``
    whether the tests pass or not. Narrower than the documented escape hatch
    (``TRANSCRIBER_TEST_ALLOW_NETWORK=1``), which lifts it for the whole run.
    """
    _BANNED["connect"] = socket.socket.connect
    _BANNED["connect_ex"] = socket.socket.connect_ex
    _BANNED["create_connection"] = socket.create_connection

    def loopback_only(address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK:
            raise AssertionError(
                f"this test module tried to connect to {address!r}. It may talk to its own "
                "loopback servers and to nothing else."
            )

    def connect(self, address, *args, **kwargs):            # socket.socket.connect
        loopback_only(address)
        return _RAW_CONNECT(self, address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):         # socket.socket.connect_ex
        loopback_only(address)
        return _RAW_CONNECT_EX(self, address, *args, **kwargs)

    def create_connection(address, timeout=None, source_address=None, **kwargs):
        loopback_only(address)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Anything that is not a number is urllib's "no timeout given" sentinel, which is
        # the default this socket already has.
        if isinstance(timeout, (int, float)):
            sock.settimeout(timeout)
        if source_address:
            sock.bind(source_address)
        try:
            sock.connect(address)
        except OSError:
            sock.close()
            raise
        return sock

    socket.socket.connect = connect             # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex       # type: ignore[method-assign]
    socket.create_connection = create_connection  # type: ignore[assignment]


def tearDownModule() -> None:
    socket.socket.connect = _BANNED["connect"]              # type: ignore[method-assign]
    socket.socket.connect_ex = _BANNED["connect_ex"]        # type: ignore[method-assign]
    socket.create_connection = _BANNED["create_connection"]  # type: ignore[assignment]


def _start_server(respond):
    """Run ``respond`` as a real HTTP server on a loopback port until the test ends.

    ``respond(method, path, headers, body)`` returns ``(status, headers, body)``. A real
    server rather than a scripted opener because the thing under test is what urllib's
    opener does with a status the client never sees — a stand-in opener would be the very
    layer whose behaviour is in question.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._answer("GET")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self._answer("POST")

        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, headers, payload = respond(method, self.path, self.headers, body)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            """Silence. The test asserts on what was received, not on stderr."""

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    # A short poll because ``shutdown`` waits for the loop to come round to it, and
    # the default half-second turns eleven tests into five seconds of waiting.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                     daemon=True).start()
    return server


def _closed_port() -> int:
    """A port nothing is listening on, for the endpoint-never-answers case."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ARedirectNeverCarriesACredential(unittest.TestCase):
    """The provider answers 3xx; nothing of ours may arrive at the host it named."""

    def setUp(self) -> None:
        self.leaked: list[tuple[str, str, dict]] = []
        self.provider_saw: list[tuple[str, str, dict]] = []
        self.redirect_status = 302

        def recorder(method, path, headers, body):
            self.leaked.append((method, path, {k.lower(): v for k, v in headers.items()}))
            return 200, {"Content-Type": "application/octet-stream"}, BLOB

        self.recorder_server = _start_server(recorder)
        self.addCleanup(self.recorder_server.server_close)
        self.addCleanup(self.recorder_server.shutdown)
        self.recorder_root = "http://127.0.0.1:%d" % self.recorder_server.server_address[1]

        def provider(method, path, headers, body):
            self.provider_saw.append((method, path, {k.lower(): v for k, v in headers.items()}))
            if path.endswith("/oauth2/v2.0/token"):
                return (
                    200,
                    {"Content-Type": "application/json"},
                    b'{"access_token":"' + BEARER.encode() + b'","expires_in":3600}',
                )
            if "/items/01ABC/content" in path:
                return self.redirect_status, {"Location": self.recorder_root + "/blob"}, b""
            if "/items/01ABC" in path:
                return (
                    200,
                    {"Content-Type": "application/json"},
                    b'{"id":"01ABC","name":"note.m4a","size":10}',
                )
            return self.redirect_status, {"Location": self.recorder_root + "/leak"}, b""

        self.provider_server = _start_server(provider)
        self.addCleanup(self.provider_server.server_close)
        self.addCleanup(self.provider_server.shutdown)
        self.provider_root = "http://127.0.0.1:%d" % self.provider_server.server_address[1]

    # -- helpers ---------------------------------------------------------------

    def _graph(self) -> GraphClient:
        """A real client pointed at the two loopback servers, with no waiting."""
        return GraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="not-a-real-secret",
            drive_id="DRIVE",
            graph_root=self.provider_root + "/v1.0",
            login_root=self.provider_root,
            retry=RetryPolicy(max_attempts=1, base_delay=0.0, jitter=False),
            sleep=lambda _seconds: None,
        )

    def _engine_client(self) -> HttpClient:
        """The engines' shared client, built the way an engine builds it.

        No opener is passed: the opener it chooses for itself is precisely what is being
        tested. The limiter is a fresh unconfigured one so this test cannot be paced by
        whatever another test left on the process-wide one.
        """
        return HttpClient(
            timeout_s=5,
            policy=EngineRetryPolicy(max_attempts=1, base_delay=0.0, jitter=False),
            secrets=(BEARER, ELEVENLABS_KEY, AZURE_KEY),
            limiter=RateLimiter(),
        )

    def assert_nothing_leaked(self) -> None:
        for method, path, headers in self.leaked:
            for name, value in headers.items():
                for secret in (BEARER, ELEVENLABS_KEY, AZURE_KEY):
                    self.assertNotIn(
                        secret,
                        value,
                        f"{method} {path} handed {name} to the host named in the Location",
                    )
        self.assertEqual(
            [(m, p) for m, p, _ in self.leaked],
            [],
            "a request reached the redirect target at all; it should have been refused",
        )

    # -- the engines' client ---------------------------------------------------

    def test_the_engine_client_refuses_a_redirect_on_a_get(self) -> None:
        http = self._engine_client()

        with self.assertRaises(EngineHTTPError) as caught:
            http.get(
                self.provider_root + "/v1/models",
                headers=dict(CREDENTIAL_HEADERS),
                expected=(200,),
            )

        self.assert_nothing_leaked()
        self.assertEqual(caught.exception.status, 302)
        self.assertIn("redirect", str(caught.exception))
        self.assertIn("127.0.0.1", str(caught.exception), "the message does not say where to")
        for secret in (BEARER, ELEVENLABS_KEY, AZURE_KEY):
            self.assertNotIn(secret, str(caught.exception))

    def test_the_engine_client_refuses_a_redirect_on_a_post(self) -> None:
        """The POST case is the expensive one: the audio would go too.

        302 and 303 are checked because urllib turns both into a GET of the new address
        while keeping the headers, and 307 because it repeats the whole request — body
        included — at whatever host answered.
        """
        for status in (302, 303, 307):
            with self.subTest(status=status):
                self.redirect_status = status
                self.leaked.clear()
                http = self._engine_client()

                with self.assertRaises(EngineHTTPError) as caught:
                    http.post(
                        self.provider_root + "/v1/audio/transcriptions",
                        headers=dict(CREDENTIAL_HEADERS),
                        json_body={"model": "whatever", "audio": "not-really-audio"},
                        expected=(200,),
                    )

                self.assert_nothing_leaked()
                self.assertEqual(caught.exception.status, status)

    # -- the Graph client ------------------------------------------------------

    def test_the_graph_client_refuses_a_redirect_on_a_get(self) -> None:
        graph = self._graph()

        with self.assertRaises(GraphHTTPError) as caught:
            graph.get_item("REDIRECT")

        self.assert_nothing_leaked()
        self.assertEqual(caught.exception.status, 302)
        self.assertIn("redirect", str(caught.exception))
        self.assertNotIn(BEARER, str(caught.exception))

    def test_the_graph_client_refuses_a_redirect_on_a_post(self) -> None:
        """Opening an upload session is the POST every recording's output goes through."""
        self.redirect_status = 303
        graph = self._graph()

        with self.assertRaises(GraphHTTPError) as caught:
            graph.upload_session("PARENT", "note.m4a", data=b"a transcript")

        self.assert_nothing_leaked()
        self.assertEqual(caught.exception.status, 303)

    def test_the_download_still_fetches_the_file_and_sends_no_authorization(self) -> None:
        """Refusing redirects must not cost the one path that legitimately follows one.

        ``/content`` answers with a redirect to storage, and that redirect is the answer
        the caller came for: it reads the Location off the refusal and fetches it itself,
        with no header on the second request, because the pre-authenticated URL is the
        credential and storage rejects a request carrying two of them. The negative
        control is the metadata call on the provider, which must be authenticated — a
        download that sent no credential anywhere would pass this test for the wrong
        reason.
        """
        graph = self._graph()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "note.m4a")

            result = graph.download("01ABC", dest)

            self.assertEqual(result.bytes_written, len(BLOB))
            with open(dest, "rb") as handle:
                self.assertEqual(handle.read(), BLOB)

        fetched = [(m, p, h) for m, p, h in self.leaked if p == "/blob"]
        self.assertEqual(len(fetched), 1, "storage was not asked for the file exactly once")
        _, _, headers = fetched[0]
        self.assertNotIn("authorization", headers, "the Graph token was sent to storage")
        for name, value in headers.items():
            self.assertNotIn(BEARER, value, f"the token travelled in {name}")

        authenticated = [
            h for m, p, h in self.provider_saw if "/items/01ABC" in p and "authorization" in h
        ]
        self.assertTrue(
            authenticated,
            "the metadata call carried no credential either, so this test proves nothing",
        )


class NeitherClientOwnsARedirectHandler(unittest.TestCase):
    """The refusal is a property of the opener, not of a branch somebody must remember.

    Asserted on the handler list rather than only on behaviour because this is the thing
    that decays: a later change that hands one of these clients a plain
    ``build_opener()`` restores the leak on every status and every method at once, and
    every behavioural test above would still be looking at only the statuses it names.
    """

    def _kinds(self, opener: urllib.request.OpenerDirector) -> set:
        return {type(handler) for handler in opener.handlers}

    def test_the_engines_client_has_no_redirect_handler(self) -> None:
        kinds = self._kinds(HttpClient(limiter=RateLimiter())._opener)

        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        self.assertIn(NoRedirect, kinds)

    def test_the_graph_client_has_no_redirect_handler(self) -> None:
        graph = GraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="not-a-real-secret",
            drive_id="DRIVE",
        )

        kinds = self._kinds(graph._opener)

        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        self.assertIn(NoRedirect, kinds)

    def test_the_heartbeat_has_no_redirect_handler(self) -> None:
        """A ping that was answered by a host the monitor redirected to proves nothing."""
        kinds = self._kinds(heartbeat_module._OPENER)

        self.assertNotIn(urllib.request.HTTPRedirectHandler, kinds)
        self.assertIn(NoRedirect, kinds)


class ARejectedCredentialIsNotAnUnreachableEndpoint(unittest.TestCase):
    """The two token failures must not arrive as the same exception.

    What hangs on this: one of them is fatal and should stop the service until a person
    acts, and the other clears itself on the next attempt. The pipeline decides which by
    the exception's type, so the type is the contract.
    """

    def _graph(self, login_root: str) -> GraphClient:
        return GraphClient(
            tenant_id="tenant",
            client_id="client",
            client_secret="not-a-real-secret",
            drive_id="DRIVE",
            login_root=login_root,
            retry=RetryPolicy(max_attempts=1, base_delay=0.0, jitter=False),
            sleep=lambda _seconds: None,
            timeout=2.0,
        )

    def test_unreachable_is_not_a_kind_of_auth_error(self) -> None:
        """Stated at the class level, because a caller's ``except`` clause is written once."""
        self.assertFalse(issubclass(GraphAuthUnreachable, GraphAuthError))
        self.assertTrue(issubclass(GraphAuthUnreachable, GraphError))

    def test_a_token_endpoint_that_never_answers_is_reported_as_unreachable(self) -> None:
        graph = self._graph("http://127.0.0.1:%d" % _closed_port())

        with self.assertRaises(GraphAuthUnreachable) as caught:
            graph._access_token()

        exc = caught.exception
        self.assertNotIsInstance(
            exc,
            GraphAuthError,
            "an endpoint that never answered has not rejected anything, and treating it "
            "as a rejection parks the service on a fault that clears itself",
        )
        self.assertIsInstance(exc, GraphError)
        self.assertNotIn("not-a-real-secret", str(exc))

    def test_a_rejected_credential_is_still_an_auth_error(self) -> None:
        def refuse(method, path, headers, body):
            return 401, {"Content-Type": "application/json"}, b'{"error":"invalid_client"}'

        server = _start_server(refuse)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        graph = self._graph("http://127.0.0.1:%d" % server.server_address[1])

        with self.assertRaises(GraphAuthError) as caught:
            graph._access_token()

        self.assertNotIsInstance(caught.exception, GraphAuthUnreachable)
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("not-a-real-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
