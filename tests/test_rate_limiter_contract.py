"""The engine rate limit, pushed at from every direction that could hang or leak.

``ENGINE_MAX_CONCURRENT`` and ``ENGINE_MAX_PER_MINUTE`` are the one guard that lives in the
middle of every transcription: it holds threads back, and a guard that holds threads back
is a guard that can hold them back forever. So the questions here are the unpleasant ones —
does a slot come back when the body raises, can a thread wait on itself, does a stop reach a
thread that is queued, and does the whole thing genuinely do nothing when nobody has
configured it.

Two rules the suite keeps:

  * **the clock is injected.** Every decision the limiter makes about time is a function of
    ``clock()``, so a minute passes because a test says it did. Nothing here sleeps to make
    an assertion true;
  * **every thread is joined with a timeout, and a thread that does not come back is a
    failure.** That is what "cannot deadlock" has to mean in a test — not that it did not
    hang this time, but that hanging is the failure the assertion catches.
"""

from __future__ import annotations

import threading
import time
import unittest

from transcriber import __main__ as cli
from transcriber import ratelimit
from transcriber.engines.base import create_engine
from transcriber.ratelimit import (
    RateLimiter,
    RateLimitShutdown,
    RateLimitTimeout,
)

from . import support


#: How long a test waits for a thread before it calls the limiter deadlocked. Generous
#: enough for a loaded CI box, short enough that a genuine hang is a failed test rather
#: than a stuck build.
JOIN_S = 5.0

#: The limiter re-checks the clock and the shutdown flag this often while waiting. Small,
#: because it is what bounds how long a stop takes to reach a queued thread — and because a
#: test that waits for a waiter should not be waiting on this.
POLL_S = 0.005


class _Clock:
    """A monotonic clock a test moves by hand. It never advances on its own."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += float(seconds)


def until(predicate, timeout: float = JOIN_S) -> bool:
    """Wait for something another thread does. Polls; never sleeps a fixed amount."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


class _Runner:
    """Threads a test starts, joined and checked at the end of it."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.case = case
        self.threads: list[threading.Thread] = []
        self.errors: list[BaseException] = []
        self._lock = threading.Lock()

    def start(self, target, *args) -> threading.Thread:
        def run() -> None:
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                with self._lock:
                    self.errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def join_all(self, *, expect_errors: bool = False) -> None:
        for thread in self.threads:
            thread.join(JOIN_S)
            self.case.assertFalse(
                thread.is_alive(),
                "a thread never came back from the limiter — that is a deadlock",
            )
        if not expect_errors:
            self.case.assertEqual([type(e).__name__ for e in self.errors], [])


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        # The shutdown flag is module-level, shared by every limiter that does not bring its
        # own event. A test that sets it and leaves it set breaks every test after it.
        ratelimit.clear_shutdown()
        self.addCleanup(ratelimit.clear_shutdown)
        self.clock = _Clock()
        self.runner = _Runner(self)

    def limiter(self, **kwargs) -> RateLimiter:
        kwargs.setdefault("clock", self.clock)
        kwargs.setdefault("poll_s", POLL_S)
        return RateLimiter(**kwargs)


class WithNoLimitsSetItIsNotThere(_Case):
    """The deployment that exists today has one person on it. Nothing may change for it."""

    def test_it_takes_nothing_and_reports_nothing(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=0)

        self.assertFalse(limiter.enabled)
        for _ in range(200):
            with limiter.guard():
                pass
        self.assertEqual(limiter.snapshot().in_flight, 0)
        self.assertEqual(limiter.snapshot().tokens, 0.0)
        self.assertEqual(limiter.describe(), "no engine rate limit is set")

    def test_a_hundred_threads_all_go_at_once(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=0)
        inside = threading.Semaphore(0)
        release = threading.Event()

        def hold() -> None:
            with limiter.guard():
                inside.release()
                self.assertTrue(release.wait(JOIN_S))

        for _ in range(20):
            self.runner.start(hold)
        for _ in range(20):
            self.assertTrue(inside.acquire(timeout=JOIN_S),
                            "an unconfigured limiter made a thread wait")
        release.set()
        self.runner.join_all()

    def test_taking_a_token_that_does_not_exist_is_free(self) -> None:
        limiter = self.limiter(max_concurrent=2, max_per_minute=0)

        for _ in range(50):
            limiter.take_token()  # would hang if the bucket were live and empty

        self.assertEqual(limiter.snapshot().tokens, 0.0)


class ExactlyAsManyAsTheLimitRunAtOnce(_Case):
    def test_three_run_the_rest_queue_and_all_six_finish(self) -> None:
        limiter = self.limiter(max_concurrent=3)
        release = threading.Event()
        state = threading.Lock()
        inside = 0
        peak = 0
        finished: list[int] = []

        def work(n: int) -> None:
            nonlocal inside, peak
            with limiter.slot():
                with state:
                    inside += 1
                    peak = max(peak, inside)
                self.assertTrue(release.wait(JOIN_S))
                with state:
                    inside -= 1
                    finished.append(n)

        for n in range(6):
            self.runner.start(work, n)

        self.assertTrue(until(lambda: limiter.snapshot().in_flight == 3))
        self.assertTrue(until(lambda: limiter.snapshot().waiting == 3),
                        "the three that did not fit should be waiting, not refused")
        # The important negative: the limit is a ceiling, not an average.
        with state:
            self.assertEqual(peak, 3)
        self.assertEqual(finished, [])

        release.set()
        self.runner.join_all()

        self.assertEqual(sorted(finished), [0, 1, 2, 3, 4, 5],
                         "a queued transcription was dropped instead of delayed")
        self.assertLessEqual(peak, 3)
        self.assertEqual(limiter.snapshot().in_flight, 0)
        self.assertEqual(limiter.snapshot().waiting, 0)

    def test_a_waiting_thread_starts_the_moment_a_slot_is_handed_back(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        first_in = threading.Event()
        let_first_go = threading.Event()
        second_in = threading.Event()

        def first() -> None:
            with limiter.slot():
                first_in.set()
                self.assertTrue(let_first_go.wait(JOIN_S))

        def second() -> None:
            with limiter.slot():
                second_in.set()

        self.runner.start(first)
        self.assertTrue(first_in.wait(JOIN_S))
        self.runner.start(second)
        self.assertTrue(until(lambda: limiter.snapshot().waiting == 1))
        self.assertFalse(second_in.is_set(), "two ran at a limit of one")

        let_first_go.set()

        self.assertTrue(second_in.wait(JOIN_S), "the queued thread was never let in")
        self.runner.join_all()


class ASlotComesBackWhateverHappensInside(_Case):
    def test_an_exception_in_the_body_hands_the_slot_back(self) -> None:
        limiter = self.limiter(max_concurrent=1)

        class Boom(RuntimeError):
            pass

        for _ in range(3):
            with self.assertRaises(Boom):
                with limiter.slot():
                    raise Boom("the provider hung up")
            self.assertEqual(limiter.snapshot().in_flight, 0)

        # And the next transcription starts immediately rather than waiting on a ghost.
        self.assertTrue(limiter.try_acquire(want_token=False))
        limiter.release_slot()

    def test_the_whole_guard_is_released_when_the_body_raises(self) -> None:
        limiter = self.limiter(max_concurrent=1, max_per_minute=60)

        with self.assertRaises(ValueError):
            with limiter.guard():
                raise ValueError("a transcript that could not be parsed")

        self.assertEqual(limiter.snapshot().in_flight, 0)
        # The token is spent, though: the request was made. Only the slot is given back.
        self.assertAlmostEqual(limiter.snapshot().tokens, 59.0, places=3)

    def test_the_with_statement_form_releases_per_thread(self) -> None:
        """``with limiter:`` is used by hand in places; one thread's exit is its own."""
        limiter = self.limiter(max_concurrent=2)
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with limiter:
                held.set()
                self.assertTrue(release.wait(JOIN_S))

        self.runner.start(holder)
        self.assertTrue(held.wait(JOIN_S))

        with self.assertRaises(RuntimeError):
            with limiter:
                raise RuntimeError("this thread's guard, and only this thread's")

        self.assertEqual(limiter.snapshot().in_flight, 1,
                         "one thread's failure closed another thread's slot")
        release.set()
        self.runner.join_all()
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_a_failed_try_acquire_takes_neither_a_slot_nor_a_token(self) -> None:
        """A half-taken permission is a leak that shows up an hour later as a hang."""
        limiter = self.limiter(max_concurrent=1, max_per_minute=60)
        self.assertTrue(limiter.try_acquire())
        before = limiter.snapshot()

        self.assertFalse(limiter.try_acquire())

        after = limiter.snapshot()
        self.assertEqual(after.in_flight, before.in_flight)
        self.assertAlmostEqual(after.tokens, before.tokens, places=6)


class ItCannotWaitOnItself(_Case):
    """Nested acquisition is not a corner case: the client asks inside the engine's slot."""

    def test_a_thread_that_holds_a_slot_may_take_it_again(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        done = threading.Event()

        def nested() -> None:
            with limiter.slot():
                with limiter.slot():
                    with limiter.guard():
                        with limiter.slot():
                            done.set()

        self.runner.start(nested)
        self.runner.join_all()

        self.assertTrue(done.is_set(), "a thread deadlocked against its own slot")
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_the_engine_and_its_http_client_share_one_slot(self) -> None:
        """Otherwise a limit of three means three engines and three of their requests."""
        limiter = self.limiter(max_concurrent=3)
        seen: list[int] = []

        def transcribe() -> None:
            with limiter.slot():            # LimitedEngine.transcribe
                for _ in range(4):          # four requests inside one transcription
                    with limiter.slot():
                        seen.append(limiter.snapshot().in_flight)

        for _ in range(3):
            self.runner.start(transcribe)
        self.runner.join_all()

        self.assertEqual(len(seen), 12)
        self.assertLessEqual(max(seen), 3, "nested requests were counted as new work")

    def test_repeated_acquisition_leaks_nothing_over_a_long_run(self) -> None:
        limiter = self.limiter(max_concurrent=2, max_per_minute=6000)

        for _ in range(500):
            with limiter.guard():
                pass

        self.assertEqual(limiter.snapshot().in_flight, 0)
        self.assertEqual(limiter.snapshot().waiting, 0)

    def test_eight_threads_contending_all_come_back(self) -> None:
        """The deadlock test proper: contention, nesting and a token bucket at once."""
        limiter = self.limiter(max_concurrent=2, max_per_minute=600)
        clock = self.clock
        stop = threading.Event()
        done: list[int] = []
        lock = threading.Lock()

        def ticker() -> None:
            # Time only moves because something moves it; without this a thread waiting for
            # a token would wait forever, which is the point of an injected clock.
            while not stop.wait(0.001):
                clock.advance(0.5)

        def work(n: int) -> None:
            for _ in range(5):
                with limiter.guard():
                    with limiter.slot():
                        pass
            with lock:
                done.append(n)

        tick = threading.Thread(target=ticker, daemon=True)
        tick.start()
        try:
            for n in range(8):
                self.runner.start(work, n)
            self.runner.join_all()
        finally:
            stop.set()
            tick.join(JOIN_S)

        self.assertEqual(sorted(done), list(range(8)))
        self.assertEqual(limiter.snapshot().in_flight, 0)


class TheAllowanceIsSpentOnTheClockAndOnlyOnTheClock(_Case):
    def test_a_minute_is_a_minute_and_then_it_waits(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=5)

        self.assertEqual(sum(1 for _ in range(5) if limiter.try_acquire()), 5)
        self.assertFalse(limiter.try_acquire())

        self.clock.advance(12.0)  # 5 a minute = one token every twelve seconds
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())

    def test_an_idle_hour_does_not_become_an_hours_traffic(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=10)
        self.clock.advance(3600.0)

        allowed = sum(1 for _ in range(100) if limiter.try_acquire())

        self.assertEqual(allowed, 10)

    def test_a_clock_that_goes_backwards_earns_nothing(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=60)
        for _ in range(60):
            limiter.try_acquire()
        self.assertFalse(limiter.try_acquire())

        self.clock.advance(-3600.0)  # what a wall clock does when NTP corrects it

        self.assertFalse(limiter.try_acquire())
        self.clock.advance(3601.0)
        self.assertTrue(limiter.try_acquire())

    def test_a_queued_thread_goes_when_the_clock_says_it_may(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=60)
        for _ in range(60):
            self.assertTrue(limiter.try_acquire())
        went = threading.Event()

        self.runner.start(lambda: (limiter.take_token(), went.set()))
        self.assertTrue(until(lambda: limiter.snapshot().waiting == 1))
        self.assertFalse(went.is_set())

        self.clock.advance(1.0)

        self.assertTrue(went.wait(JOIN_S), "the clock moved and the thread did not")
        self.runner.join_all()

    def test_a_bounded_wait_gives_up_rather_than_hanging_forever(self) -> None:
        limiter = self.limiter(max_concurrent=1, max_per_minute=0)
        self.assertTrue(limiter.try_acquire(want_token=False))

        with self.assertRaises(RateLimitTimeout):
            with limiter.slot(timeout=0.0):
                pass  # pragma: no cover - the acquire raises before the body

        limiter.release_slot()


class AStopReachesAThreadThatIsWaiting(_Case):
    def test_a_thread_queued_for_a_slot_is_let_go_and_keeps_its_recording(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        holder_in = threading.Event()
        let_holder_go = threading.Event()
        raised: list[BaseException] = []

        def holder() -> None:
            with limiter.slot():
                holder_in.set()
                self.assertTrue(let_holder_go.wait(JOIN_S))

        def queued() -> None:
            try:
                with limiter.slot():
                    raise AssertionError("this thread should never have been let in")
            except RateLimitShutdown as exc:
                raised.append(exc)

        self.runner.start(holder)
        self.assertTrue(holder_in.wait(JOIN_S))
        self.runner.start(queued)
        self.assertTrue(until(lambda: limiter.snapshot().waiting == 1))

        ratelimit.request_shutdown("the second signal")

        self.assertTrue(until(lambda: len(raised) == 1),
                        "a stop did not reach the thread queued for a slot")
        # It is a stop, not a failure: the sentence says the recording is still in the
        # ledger, because that is the whole difference between stopping and dropping.
        message = str(raised[0])
        self.assertIn("stays in the ledger", message)
        self.assertIn("picked up by the next run", message)
        let_holder_go.set()
        self.runner.join_all()

    def test_a_thread_queued_for_a_token_is_let_go_and_gives_its_slot_back(self) -> None:
        limiter = self.limiter(max_concurrent=2, max_per_minute=1)
        self.assertTrue(limiter.try_acquire(want_slot=False))  # spend the only token
        raised: list[BaseException] = []

        def queued() -> None:
            try:
                with limiter.guard():
                    raise AssertionError("there was no token to be had")
            except RateLimitShutdown as exc:
                raised.append(exc)

        self.runner.start(queued)
        self.assertTrue(until(lambda: limiter.snapshot().waiting == 1))
        self.assertEqual(limiter.snapshot().in_flight, 1,
                         "it should be holding a slot while it waits for a token")

        ratelimit.request_shutdown("the second signal")
        self.runner.join_all()

        self.assertEqual(len(raised), 1)
        self.assertEqual(limiter.snapshot().in_flight, 0,
                         "the slot was left behind when the wait was abandoned")

    def test_a_thread_already_transcribing_is_left_alone(self) -> None:
        """A stop means finish what is running, not abandon it half-transcribed."""
        limiter = self.limiter(max_concurrent=1)
        inside = threading.Event()
        finished = threading.Event()

        def working() -> None:
            with limiter.slot():
                inside.set()
                ratelimit.request_shutdown("stopping while this one is mid-flight")
                # Still holding it, and still allowed to nest inside its own slot.
                with limiter.slot():
                    pass
                finished.set()

        self.runner.start(working)
        self.runner.join_all()

        self.assertTrue(inside.is_set())
        self.assertTrue(finished.is_set(), "a running transcription was interrupted")
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_clearing_the_stop_makes_the_limiter_usable_again(self) -> None:
        limiter = self.limiter(max_concurrent=1)
        ratelimit.request_shutdown("stopping")
        with self.assertRaises(RateLimitShutdown):
            with limiter.slot():
                pass  # pragma: no cover - the acquire raises first

        ratelimit.clear_shutdown()

        with limiter.slot():
            self.assertEqual(limiter.snapshot().in_flight, 1)


class TurningTheNumbersUpAndDownIsSafeWhileItRuns(_Case):
    def test_lowering_the_limit_lets_the_work_in_flight_finish(self) -> None:
        limiter = self.limiter(max_concurrent=3)
        held = threading.Semaphore(0)
        release = threading.Event()

        def hold() -> None:
            with limiter.slot():
                held.release()
                self.assertTrue(release.wait(JOIN_S))

        for _ in range(3):
            self.runner.start(hold)
        for _ in range(3):
            self.assertTrue(held.acquire(timeout=JOIN_S))

        limiter.configure(max_concurrent=1)

        self.assertEqual(limiter.snapshot().in_flight, 3, "work in flight was cut off")
        self.assertFalse(limiter.try_acquire(want_token=False), "somebody new was admitted")
        release.set()
        self.runner.join_all()
        self.assertEqual(limiter.snapshot().in_flight, 0)

    def test_turning_the_per_minute_limit_off_stops_it_gating(self) -> None:
        limiter = self.limiter(max_concurrent=0, max_per_minute=1)
        self.assertTrue(limiter.try_acquire())
        self.assertFalse(limiter.try_acquire())

        limiter.configure(max_per_minute=0)

        for _ in range(50):
            limiter.take_token()  # a live bucket with no tokens would hang here
        self.assertTrue(limiter.try_acquire())

    def test_the_same_numbers_twice_is_not_a_change(self) -> None:
        limiter = self.limiter(max_concurrent=3, max_per_minute=60)
        for _ in range(30):
            limiter.try_acquire(want_slot=False)
        tokens = limiter.snapshot().tokens

        self.assertFalse(limiter.configure(max_concurrent=3, max_per_minute=60))

        self.assertAlmostEqual(limiter.snapshot().tokens, tokens, places=6,
                               msg="re-reading the config handed out a fresh allowance")


class OneLimiterForEveryRouteAndEveryEngine(_Case):
    """The limit is the account's, not the folder's, and not the engine class's."""

    def setUp(self) -> None:
        super().setUp()
        shared = ratelimit.shared_limiter()
        self.addCleanup(shared.configure, max_concurrent=0, max_per_minute=0)

    def test_two_engines_are_held_back_by_the_same_limiter(self) -> None:
        openai = create_engine(support.make_config(engine="openai"))
        eleven = create_engine(support.make_config(engine="elevenlabs", engine_key="k"))

        self.assertIs(openai.limiter, eleven.limiter)
        self.assertIs(openai.limiter, ratelimit.shared_limiter())

    def test_eight_routes_worth_of_engines_share_one_allowance(self) -> None:
        """Per-route engine overrides must not become eight separate allowances."""
        engines = [
            create_engine(support.make_config(
                engine=name, engine_key="k", azure_region="southafricanorth",
                engine_max_concurrent=2, engine_max_per_minute=0,
            ))
            for name in ("openai", "elevenlabs", "azure") * 3
        ]

        limiters = {id(engine.limiter) for engine in engines}

        self.assertEqual(len(limiters), 1, "each engine brought its own limit")
        self.assertEqual(ratelimit.shared_limiter().max_concurrent, 2)


class TheStopThatReachesAQueuedThreadIsWiredUp(_Case):
    """The limiter can be told to let waiters go. Something has to actually tell it."""

    class _Worker:
        def __init__(self) -> None:
            self.stopping = False

    def test_nothing_is_released_while_the_worker_is_still_running(self) -> None:
        worker = self._Worker()

        cli._release_rate_limited_threads_on_shutdown(worker, after_s=0.0)

        self.assertFalse(ratelimit.shutdown_requested())

    def test_a_stop_releases_the_threads_queued_behind_the_limit(self) -> None:
        worker = self._Worker()
        worker.stopping = True

        thread = cli._release_rate_limited_threads_on_shutdown(worker, after_s=0.0)
        thread.join(JOIN_S)

        self.assertFalse(thread.is_alive())
        self.assertTrue(
            ratelimit.shutdown_requested(),
            "a queued thread would have held the process open through its shutdown",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
