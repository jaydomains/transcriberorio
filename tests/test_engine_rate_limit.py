"""The engine rate limit: a queue in front of the provider, never a dropped recording.

Worker concurrency is about this machine; a provider's per-minute allowance is about the
account. Eight people recording into eight folders hits the second one first, and a service
that answers a rate limit by failing recordings has turned a busy morning into a loss.

So every test here asserts one of four things:

  * unconfigured, the limiter is not in the way at all;
  * configured, nothing gets past it — including through an engine that built its own HTTP
    client without being told about it;
  * it cannot deadlock, against itself or against a shutdown;
  * it does not replace the 429 backoff, which is a different mechanism for a different
    moment: the limiter avoids provoking the limit, the backoff obeys the provider when it
    says no anyway.

Nothing here sleeps for a clock. The bucket is driven by an injected clock and the two
threaded tests are woken by an event or by a shutdown, both of which are immediate.
"""

from __future__ import annotations

import threading
import unittest
import urllib.error
from typing import Any

from transcriber import ratelimit
from transcriber.engines.base import HttpClient, LimitedEngine, RetryPolicy, create_engine
from transcriber.models import Hints, Transcript

from . import support
from .support import ScriptedResponse, ScriptedOpener


def ok(body: bytes = b'{"text":"hello"}') -> ScriptedResponse:
    return ScriptedResponse(200, body)


class _Clock:
    """A monotonic clock a test moves by hand. Never advances on its own."""

    def __init__(self, start: float = 1000.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value

    def advance(self, seconds: float) -> None:
        self.now += seconds


class UnconfiguredItIsNotInTheWay(unittest.TestCase):
    """The deployment that exists today has one person on it and must not change."""

    def test_nothing_is_limited_until_a_limit_is_set(self) -> None:
        limiter = ratelimit.RateLimiter(clock=_Clock())

        self.assertFalse(limiter.enabled)
        self.assertTrue(all(limiter.try_acquire() for _ in range(50)))
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_the_defaults_are_right_for_one_person(self) -> None:
        """Three at once, no per-minute cap: nothing one person does can reach either."""
        concurrent, per_minute = ratelimit.limits_from_config(support.make_config())

        self.assertEqual(concurrent, 3)
        self.assertEqual(per_minute, 0)


class ConcurrencyIsCappedAcrossEveryRouteAndThread(unittest.TestCase):
    def test_only_as_many_as_the_limit_are_ever_in_flight(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=2, clock=_Clock())

        taken = [limiter.try_acquire(want_token=False) for _ in range(3)]

        self.assertEqual(taken, [True, True, False])
        self.assertEqual(limiter.snapshot().in_flight, 2)

    def test_a_finished_transcription_lets_the_next_one_start(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        self.assertTrue(limiter.try_acquire(want_token=False))
        self.assertFalse(limiter.try_acquire(want_token=False))

        limiter.release_slot()

        self.assertTrue(limiter.try_acquire(want_token=False))

    def test_a_waiting_thread_is_admitted_the_moment_a_slot_frees(self) -> None:
        """Waiting is the whole mechanism: it slows down, it does not refuse."""
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        admitted = threading.Event()
        holding = threading.Event()

        def second() -> None:
            with limiter.slot():
                admitted.set()

        with limiter.slot():
            holding.set()
            worker = threading.Thread(target=second, daemon=True)
            worker.start()
            self.assertFalse(admitted.wait(0.15), "it started while the slot was taken")

        self.assertTrue(admitted.wait(2.0), "it never started after the slot was freed")
        worker.join(2.0)
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_one_thread_cannot_deadlock_against_itself(self) -> None:
        """The engine holds a slot for the transcription; its HTTP asks for one inside that."""
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        done = threading.Event()

        def nested() -> None:
            with limiter.slot():
                with limiter.slot():
                    with limiter.slot():
                        done.set()

        worker = threading.Thread(target=nested, daemon=True)
        worker.start()
        worker.join(2.0)

        self.assertTrue(done.is_set(), "a re-entrant acquisition deadlocked")
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_lowering_the_limit_while_work_is_in_flight_admits_nobody_new(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=4, clock=_Clock())
        for _ in range(4):
            limiter.try_acquire(want_token=False)

        limiter.configure(max_concurrent=1)

        self.assertFalse(limiter.try_acquire(want_token=False))
        for _ in range(4):
            limiter.release_slot()
        self.assertTrue(limiter.try_acquire(want_token=False))


class ThePerMinuteAllowanceIsSpentOnAMonotonicClock(unittest.TestCase):
    def test_a_minutes_worth_is_a_minutes_worth(self) -> None:
        clock = _Clock()
        limiter = ratelimit.RateLimiter(max_per_minute=3, clock=clock)

        spent = [limiter.try_acquire(want_slot=False) for _ in range(4)]

        self.assertEqual(spent, [True, True, True, False])

    def test_it_refills_at_the_rate_it_was_given(self) -> None:
        clock = _Clock()
        limiter = ratelimit.RateLimiter(max_per_minute=2, clock=clock)
        self.assertTrue(limiter.try_acquire(want_slot=False))
        self.assertTrue(limiter.try_acquire(want_slot=False))
        self.assertFalse(limiter.try_acquire(want_slot=False))

        clock.advance(29.0)
        self.assertFalse(limiter.try_acquire(want_slot=False), "29s is not yet a token")
        clock.advance(1.5)
        self.assertTrue(limiter.try_acquire(want_slot=False))

    def test_it_never_hands_out_more_than_a_minutes_worth_at_once(self) -> None:
        """An idle hour must not become an hour's traffic in one second."""
        clock = _Clock()
        limiter = ratelimit.RateLimiter(max_per_minute=2, clock=clock)
        clock.advance(3600.0)

        spent = [limiter.try_acquire(want_slot=False) for _ in range(5)]

        self.assertEqual(spent, [True, True, False, False, False])

    def test_a_clock_that_goes_backwards_earns_nothing(self) -> None:
        """time.monotonic cannot, but the reason it is used is that time.time can."""
        clock = _Clock()
        limiter = ratelimit.RateLimiter(max_per_minute=1, clock=clock)
        self.assertTrue(limiter.try_acquire(want_slot=False))

        clock.advance(-600.0)

        self.assertFalse(limiter.try_acquire(want_slot=False))

    def test_a_thread_waiting_for_a_token_proceeds_when_the_clock_says_so(self) -> None:
        clock = _Clock()
        limiter = ratelimit.RateLimiter(max_per_minute=60, clock=clock, poll_s=0.005)
        self.assertTrue(limiter.try_acquire(want_slot=False))  # spends the one it starts with
        while limiter.try_acquire(want_slot=False):
            pass
        went = threading.Event()

        def waiter() -> None:
            limiter.take_token()
            went.set()

        worker = threading.Thread(target=waiter, daemon=True)
        worker.start()
        self.assertFalse(went.wait(0.1), "it went without waiting for a token")

        clock.advance(2.0)

        self.assertTrue(went.wait(2.0), "it never went, even once the clock had earned one")
        worker.join(2.0)

    def test_a_bounded_wait_gives_up_rather_than_hanging(self) -> None:
        clock = _Clock(step=0.05)   # every look at the clock is another 50ms gone
        limiter = ratelimit.RateLimiter(max_per_minute=1, clock=clock, poll_s=0.001)
        self.assertTrue(limiter.try_acquire(want_slot=False))

        with self.assertRaises(ratelimit.RateLimitTimeout):
            limiter.take_token(timeout=0.2)


class AWaitingThreadStillHonoursAShutdown(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(ratelimit.clear_shutdown)

    def test_a_thread_queued_for_a_slot_is_released_by_a_shutdown(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock(), poll_s=0.005)
        self.assertTrue(limiter.try_acquire(want_token=False))
        released: list[BaseException] = []
        arrived = threading.Event()

        def waiter() -> None:
            arrived.set()
            try:
                with limiter.slot():
                    pass
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                released.append(exc)

        worker = threading.Thread(target=waiter, daemon=True)
        worker.start()
        arrived.wait(2.0)

        ratelimit.request_shutdown("the process is going now")
        worker.join(2.0)

        self.assertFalse(worker.is_alive(), "the shutdown did not reach the waiting thread")
        self.assertEqual(len(released), 1)
        self.assertIsInstance(released[0], ratelimit.RateLimitShutdown)
        # And it says, in the message a person will read in the log, that nothing was lost.
        self.assertIn("picked up by the next run", str(released[0]))

    def test_a_thread_already_holding_a_slot_is_left_alone(self) -> None:
        """A stop means finish what is running, not abandon it half-transcribed."""
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())

        with limiter.slot():
            ratelimit.request_shutdown("stopping")
            finished = True

        self.assertTrue(finished)


class NoEngineCanBeOutsideTheLimit(unittest.TestCase):
    def setUp(self) -> None:
        self.limiter = ratelimit.RateLimiter(max_per_minute=2, clock=_Clock())

    def _client(self, responses: list[ScriptedResponse], sleep: Any = None) -> HttpClient:
        return HttpClient(
            timeout_s=5,
            policy=RetryPolicy(max_attempts=3, base_delay=1.0, jitter=False),
            opener=ScriptedOpener(responses),
            sleep=sleep or (lambda _seconds: None),
            limiter=self.limiter,
        )

    def test_the_shared_limiter_is_the_default_for_every_client(self) -> None:
        """An engine that builds its own client without asking still gets the limit."""
        self.assertIs(HttpClient().limiter, ratelimit.shared_limiter())

    def test_every_request_spends_one_of_the_minutes_allowance(self) -> None:
        http = self._client([ok(), ok()])

        http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertLess(self.limiter.snapshot().tokens, 2.0)

    def test_a_retry_is_another_request_and_is_counted_as_one(self) -> None:
        """The provider counts attempts, so the allowance has to count attempts too."""
        before = self.limiter.snapshot().tokens
        http = self._client([ScriptedResponse(429, b"{}", {"Retry-After": "1"}), ok()])

        http.get("https://api.invalid/v1/thing", expected=(200,))

        spent = before - self.limiter.snapshot().tokens
        self.assertAlmostEqual(spent, 2.0, places=6)

    def test_the_limiter_does_not_replace_the_429_backoff(self) -> None:
        waits: list[float] = []
        http = self._client(
            [ScriptedResponse(429, b"{}", {"Retry-After": "11"}), ok()],
            sleep=waits.append,
        )

        http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertEqual(waits, [11.0], "the Retry-After the provider asked for was not honoured")

    def test_the_slot_is_handed_back_even_when_the_request_fails(self) -> None:
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        http = HttpClient(
            timeout_s=1,
            policy=RetryPolicy(max_attempts=1),
            opener=ScriptedOpener([ScriptedResponse(422, b'{"error":"bad audio"}')]),
            sleep=lambda _s: None,
            limiter=limiter,
        )

        with self.assertRaises(Exception):
            http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_a_transport_failure_also_hands_the_slot_back(self) -> None:
        class Broken(ScriptedOpener):
            def open(self, request: Any, timeout: float | None = None) -> Any:
                raise urllib.error.URLError("connection reset")

        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        http = HttpClient(
            timeout_s=1,
            policy=RetryPolicy(max_attempts=1),
            opener=Broken([]),
            sleep=lambda _s: None,
            limiter=limiter,
        )

        with self.assertRaises(Exception):
            http.get("https://api.invalid/v1/thing", expected=(200,))

        self.assertEqual(limiter.snapshot().in_flight, 0)


class EveryEngineIsBuiltInsideTheLimit(unittest.TestCase):
    def test_the_registry_hands_back_a_limited_engine(self) -> None:
        engine = create_engine(support.make_config(engine="openai"))

        self.assertIsInstance(engine, LimitedEngine)
        self.assertEqual(engine.name, "openai")
        self.assertEqual(engine.max_bytes, 25 * 1024 * 1024)

    def test_it_forwards_everything_the_engine_underneath_offers(self) -> None:
        """Azure's batch mode is configured by a method only that engine has."""
        engine = create_engine(support.make_config(
            engine="azure", engine_key="not-a-real-key", azure_region="southafricanorth",
        ))

        self.assertTrue(hasattr(engine, "with_content_url_provider"))
        self.assertFalse(hasattr(create_engine(support.make_config(engine="openai")),
                                "with_content_url_provider"))

    def test_a_transcription_holds_a_slot_for_the_whole_of_it(self) -> None:
        """Not just for each request: Azure submits a job and then polls it for half an hour."""
        limiter = ratelimit.RateLimiter(max_concurrent=1, clock=_Clock())
        seen: list[int] = []

        class Slow:
            name = "slow"
            max_bytes = None

            def transcribe(self, path: str, hints: Hints) -> Transcript:
                seen.append(limiter.snapshot().in_flight)
                return Transcript(text="hello", segments=(), language="en")

        engine = LimitedEngine(Slow(), limiter)
        engine.transcribe("recording.m4a", Hints())

        self.assertEqual(seen, [1], "the slot was not held for the transcription itself")
        self.assertEqual(limiter.snapshot().in_flight, 0, "the slot was not handed back")


class TheLimitsAreReadFromTheConfiguration(unittest.TestCase):
    def test_a_config_that_has_never_heard_of_them_still_works(self) -> None:
        """A .env written before these settings existed must not fail mid-transcription."""

        class OldConfig:
            engine = "openai"

        concurrent, per_minute = ratelimit.limits_from_config(OldConfig())

        self.assertEqual(concurrent, ratelimit.DEFAULT_MAX_CONCURRENT)
        self.assertEqual(per_minute, ratelimit.DEFAULT_MAX_PER_MINUTE)

    def test_nonsense_is_reported_and_replaced_rather_than_raised(self) -> None:
        class BadConfig:
            engine_max_concurrent = "quite a lot"
            engine_max_per_minute = -4

        with self.assertLogs("transcriber.ratelimit", level="WARNING"):
            concurrent, per_minute = ratelimit.limits_from_config(BadConfig())

        self.assertEqual(concurrent, ratelimit.DEFAULT_MAX_CONCURRENT)
        self.assertEqual(per_minute, ratelimit.DEFAULT_MAX_PER_MINUTE)

    def test_configuring_it_twice_with_the_same_numbers_changes_nothing(self) -> None:
        limiter = ratelimit.RateLimiter(max_per_minute=10, clock=_Clock())
        limiter.try_acquire(want_slot=False)
        before = limiter.snapshot().tokens

        self.assertFalse(limiter.configure(max_concurrent=0, max_per_minute=10))
        self.assertAlmostEqual(limiter.snapshot().tokens, before, places=6)

    def test_an_engine_built_from_a_config_points_the_shared_limiter_at_it(self) -> None:
        shared = ratelimit.shared_limiter()
        self.addCleanup(shared.configure, max_concurrent=0, max_per_minute=0)

        create_engine(support.make_config(engine_max_concurrent=5, engine_max_per_minute=90))

        self.assertEqual(shared.max_concurrent, 5)
        self.assertEqual(shared.max_per_minute, 90)


if __name__ == "__main__":
    unittest.main()
