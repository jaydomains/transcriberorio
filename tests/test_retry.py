"""Retry honours ``Retry-After``, and a non-429 4xx is not retried.

Graph throttles with 429 and a ``Retry-After`` header, and it means it: guessing a shorter
wait gets the app throttled harder, and a service that hammers a throttled tenant is a
service an administrator eventually turns off. So the header is obeyed exactly.

The other half matters just as much. A 400 or a 404 is a statement about our request, not
about the weather — retrying it turns a visible bug into a slow visible bug, and burns the
attempt budget that a genuinely transient failure needs.

Every sleep here is captured rather than taken, so the suite asserts on the waits without
ever waiting.
"""

from __future__ import annotations

import unittest
import urllib.error
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from typing import Any

from transcriber import graph as graph_module
from transcriber.engines.base import (
    EngineAuthError,
    EngineHTTPError,
    HttpClient,
    RetryPolicy as EngineRetryPolicy,
    parse_retry_after,
)
from transcriber.graph import GraphClient, GraphHTTPError, RetryPolicy

from .support import ScriptedResponse, TokenThenScriptedOpener


class _Recorder:
    """Stands in for ``time.sleep``: records what would have been waited, waits nothing."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def client(responses, sleep) -> GraphClient:
    return GraphClient(
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        drive_id="DRIVE",
        retry=RetryPolicy(max_attempts=4, base_delay=1.0, jitter=False),
        sleep=sleep,
        opener=TokenThenScriptedOpener(responses),
    )


def ok(body: bytes = b'{"id":"01ABC","name":"note.m4a","size":10}') -> ScriptedResponse:
    return ScriptedResponse(200, body, {"Content-Type": "application/json"})


class RetryAfterIsObeyedExactly(unittest.TestCase):
    def test_a_429_waits_the_number_of_seconds_it_was_told(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(429, b"{}", {"Retry-After": "17"}), ok()], sleep)

        item = graph.get_item("01ABC")

        self.assertEqual(item.id, "01ABC")
        self.assertEqual(sleep.waits, [17.0], "the wait Graph asked for was not the wait taken")

    def test_a_503_and_a_504_are_obeyed_the_same_way(self) -> None:
        for status in (503, 504):
            with self.subTest(status=status):
                sleep = _Recorder()
                graph = client([ScriptedResponse(status, b"{}", {"Retry-After": "9"}), ok()], sleep)
                graph.get_item("01ABC")
                self.assertEqual(sleep.waits, [9.0])

    def test_several_throttles_in_a_row_are_each_obeyed(self) -> None:
        sleep = _Recorder()
        graph = client(
            [
                ScriptedResponse(429, b"{}", {"Retry-After": "3"}),
                ScriptedResponse(429, b"{}", {"Retry-After": "8"}),
                ok(),
            ],
            sleep,
        )
        graph.get_item("01ABC")
        self.assertEqual(sleep.waits, [3.0, 8.0])

    def test_a_retry_after_given_as_an_http_date_is_honoured(self) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        sleep = _Recorder()
        graph = client(
            [ScriptedResponse(429, b"{}", {"Retry-After": format_datetime(when)}), ok()], sleep
        )
        graph.get_item("01ABC")

        self.assertEqual(len(sleep.waits), 1)
        self.assertAlmostEqual(sleep.waits[0], 30.0, delta=2.0)

    def test_a_throttle_with_no_header_backs_off_instead_of_not_waiting(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(429, b"{}"), ok()], sleep)
        graph.get_item("01ABC")
        self.assertEqual(sleep.waits, [1.0])

    def test_an_absurd_retry_after_is_capped_rather_than_held(self) -> None:
        """A worker asleep for an hour is a worker whose ledger lease has expired."""
        sleep = _Recorder()
        graph = client([ScriptedResponse(429, b"{}", {"Retry-After": "36000"}), ok()], sleep)
        graph.get_item("01ABC")

        self.assertEqual(sleep.waits, [RetryPolicy().max_retry_after])

    def test_the_header_parser_reads_both_forms_and_refuses_nonsense(self) -> None:
        self.assertEqual(parse_retry_after("12"), 12.0)
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("soon please"))
        self.assertEqual(graph_module._parse_retry_after("12"), 12.0)


class ANon429FourHundredIsNotRetried(unittest.TestCase):
    def test_a_404_is_raised_at_once(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(404, b'{"error":{"code":"itemNotFound"}}')], sleep)

        with self.assertRaises(GraphHTTPError) as raised:
            graph.get_item("01ABC")

        self.assertEqual(sleep.waits, [], "a 404 was retried; nothing about it will change")
        self.assertEqual(raised.exception.status, 404)
        self.assertTrue(raised.exception.is_not_found)
        self.assertEqual(raised.exception.attempts, 1)

    def test_a_400_is_raised_at_once(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(400, b'{"error":{"code":"invalidRequest"}}')], sleep)

        with self.assertRaises(GraphHTTPError):
            graph.get_item("01ABC")
        self.assertEqual(sleep.waits, [])

    def test_a_409_and_a_422_are_raised_at_once(self) -> None:
        for status in (409, 422):
            with self.subTest(status=status):
                sleep = _Recorder()
                graph = client([ScriptedResponse(status, b"{}")], sleep)
                with self.assertRaises(GraphHTTPError):
                    graph.get_item("01ABC")
                self.assertEqual(sleep.waits, [])

    def test_a_403_is_a_credential_problem_and_is_not_retried(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(403, b'{"error":{"code":"accessDenied"}}')], sleep)

        with self.assertRaises(GraphHTTPError):
            graph.get_item("01ABC")
        self.assertEqual(sleep.waits, [])

    def test_a_401_is_retried_exactly_once_with_a_fresh_token(self) -> None:
        """An expired token looks exactly like a permissions fault. Try once, then say so."""
        sleep = _Recorder()
        graph = client([ScriptedResponse(401, b"{}"), ok()], sleep)

        item = graph.get_item("01ABC")

        self.assertEqual(item.id, "01ABC")
        self.assertEqual(sleep.waits, [], "a token refresh is not a backoff")

    def test_a_second_401_is_reported_rather_than_looped_on(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(401, b"{}"), ScriptedResponse(401, b"{}")], sleep)

        with self.assertRaises(GraphHTTPError):
            graph.get_item("01ABC")


class FivexxIsRetriedAndThenGivenUpOnLoudly(unittest.TestCase):
    def test_a_500_is_retried_with_exponential_backoff(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(500, b"{}"), ScriptedResponse(500, b"{}"), ok()], sleep)

        graph.get_item("01ABC")

        self.assertEqual(sleep.waits, [1.0, 2.0])

    def test_it_gives_up_after_the_attempt_budget_and_says_so(self) -> None:
        sleep = _Recorder()
        graph = client([ScriptedResponse(500, b'{"error":{"message":"nope"}}')] * 4, sleep)

        with self.assertRaises(GraphHTTPError) as raised:
            graph.get_item("01ABC")

        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(raised.exception.attempts, 4)
        self.assertEqual(len(sleep.waits), 3, "attempts and waits must agree")

    def test_a_transport_failure_is_retried_and_then_reported(self) -> None:
        sleep = _Recorder()

        class Flaky(TokenThenScriptedOpener):
            def open(self, request: Any, timeout: float | None = None) -> Any:
                url = request.full_url
                if "/oauth2/" in url:
                    return super().open(request, timeout)
                raise urllib.error.URLError("connection reset")

        graph = GraphClient(
            tenant_id="t", client_id="c", client_secret="s", drive_id="DRIVE",
            retry=RetryPolicy(max_attempts=3, base_delay=1.0, jitter=False),
            sleep=sleep, opener=Flaky([]),
        )
        with self.assertRaises(graph_module.GraphTransportError):
            graph.get_item("01ABC")
        self.assertEqual(len(sleep.waits), 2)


class TheEngineClientFollowsTheSameRules(unittest.TestCase):
    """The transcription and analysis engines share one HTTP client. Same two rules."""

    def _client(self, responses, sleep) -> HttpClient:
        return HttpClient(
            timeout_s=5,
            policy=EngineRetryPolicy(max_attempts=4, base_delay=1.0, jitter=False),
            secrets=("not-a-real-key",),
            opener=TokenThenScriptedOpener(responses),
            sleep=sleep,
        )

    def test_a_429_waits_what_it_was_told(self) -> None:
        sleep = _Recorder()
        http = self._client([ScriptedResponse(429, b"{}", {"Retry-After": "11"}), ok()], sleep)

        http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertEqual(sleep.waits, [11.0])

    def test_a_422_is_not_retried(self) -> None:
        sleep = _Recorder()
        http = self._client([ScriptedResponse(422, b'{"error":"bad audio"}')], sleep)

        with self.assertRaises(EngineHTTPError) as raised:
            http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertEqual(sleep.waits, [])
        self.assertEqual(raised.exception.status, 422)

    def test_a_401_is_an_auth_error_and_is_not_retried(self) -> None:
        sleep = _Recorder()
        http = self._client([ScriptedResponse(401, b'{"error":"bad key"}')], sleep)

        with self.assertRaises(EngineAuthError):
            http.get("https://api.invalid/v1/thing", expected=(200,))
        self.assertEqual(sleep.waits, [])

    def test_a_secret_never_reaches_an_error_message(self) -> None:
        """Providers echo the offending key back in a 401 body more often than not."""
        sleep = _Recorder()
        http = self._client([ScriptedResponse(400, b'{"error":"key not-a-real-key rejected"}')], sleep)

        with self.assertRaises(EngineHTTPError) as raised:
            http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertNotIn("not-a-real-key", str(raised.exception))
        self.assertIn("REDACTED", str(raised.exception))

    def test_a_url_query_is_redacted_before_it_is_reported(self) -> None:
        """A SAS url and a single-use download token are credentials in query-string form."""
        redacted = graph_module.redact_url("https://host.invalid/path?token=secret-value")
        self.assertNotIn("secret-value", redacted)
        self.assertIn("?<redacted>", redacted)

    def test_an_absurd_retry_after_stops_rather_than_holding_a_lease(self) -> None:
        sleep = _Recorder()
        http = self._client([ScriptedResponse(429, b"{}", {"Retry-After": "3600"})], sleep)

        with self.assertRaises(EngineHTTPError):
            http.get("https://api.invalid/v1/thing", expected=(200,))
        self.assertEqual(sleep.waits, [])


class TheSuiteItselfIsOffline(unittest.TestCase):
    """The guard in ``tests/__init__`` is only worth having if it actually fires."""

    def test_an_outbound_connection_is_refused(self) -> None:
        import socket

        with self.assertRaises(AssertionError) as raised:
            socket.create_connection(("example.invalid", 443), timeout=1)
        self.assertIn("runs offline", str(raised.exception))

        with socket.socket() as handle:
            with self.assertRaises(AssertionError):
                handle.connect(("example.invalid", 443))

    def test_a_real_http_client_would_therefore_fail_rather_than_call_out(self) -> None:
        http = HttpClient(timeout_s=1, policy=EngineRetryPolicy(max_attempts=1))
        with self.assertRaises(Exception) as raised:
            http.get("https://api.invalid/v1/thing", expected=(200,))
        self.assertNotIsInstance(raised.exception, unittest.SkipTest)


if __name__ == "__main__":
    unittest.main()
