"""One shared limiter in front of the transcription providers.

Worker concurrency is a statement about *this machine*: how many recordings it can hold on
disk and push through ffprobe at once. A provider's rate limit is a statement about *the
account*: how many requests a minute it will accept before it starts refusing them. They
are different numbers about different things, and the moment eight people record into eight
folders the second one binds first — eight parallel transcriptions is eight parallel
uploads against a per-minute allowance sized for one person.

So this module is deliberately not "worker concurrency for engines". It is a process-wide
gate every provider call passes through:

  * ``ENGINE_MAX_CONCURRENT`` — how many transcriptions may be in the air at once;
  * ``ENGINE_MAX_PER_MINUTE`` — how many requests a minute may leave this process at all,
    as a token bucket on a **monotonic** clock, so a machine whose wall clock is corrected
    backwards by NTP cannot hand out a minute's allowance twice.

Four properties are load-bearing.

**It slows down; it never drops.** Waiting is the entire mechanism. Nothing here refuses a
recording, marks one done, or lets one past unrecorded — a thread that cannot go now waits
until it can, and the recording it is carrying is still in the ledger the whole time.

**It cannot deadlock.** There is one lock, held only for arithmetic, and never held across
a network call. A thread waits for two things only: a slot, which the thread using it hands
back in a ``finally``, and a token, which is earned from the clock and needs nothing from
any other thread. Acquisition is re-entrant per thread, so a call nested inside a call that
already holds a slot passes straight through rather than waiting for itself — which is
exactly what the HTTP client does inside an engine that is already holding one.

**It does not replace the 429 backoff.** The limiter's job is to avoid provoking the limit;
:class:`~transcriber.engines.base.RetryPolicy`'s job is to obey the provider when it says no
anyway. The limiter sits outside that retry loop and knows nothing about it.

**A waiting thread still honours SIGTERM.** Waits are done on a condition variable in short
slices, re-checking a shutdown flag each time, so a stop reaches a thread queued behind a
token within ``_POLL_S`` rather than at the end of a minute. Only threads that are *waiting*
are woken this way: a thread that already holds a slot is mid-transcription, and shutdown
means "finish what is running", not "abandon it". Which of the two a thread is decides the
order of the checks. A thread holding nothing is starting new work, and a stop turns it back
at once, room or no room. A thread that already holds a slot is mid-transcription: it takes
what is free before the stop flag is looked at, so an hour-long call being transcribed piece
by piece spends the allowance it has and finishes. Only when it would have to *wait* for
something is it let go — and then nothing has been thrown away, because the clock and the
other threads are what it was waiting for.

Nothing in here sleeps on a real clock in tests: the clock is injectable
(``RateLimiter(clock=fake)``) and every decision is a pure function of it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

__all__ = [
    "RateLimitError",
    "RateLimitShutdown",
    "RateLimitTimeout",
    "LimiterState",
    "RateLimiter",
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_PER_MINUTE",
    "limits_from_config",
    "shared_limiter",
    "configure_shared",
    "request_shutdown",
    "clear_shutdown",
    "shutdown_requested",
]

log = logging.getLogger("transcriber.ratelimit")

#: Three at once. Comfortably under every provider's smallest published concurrency and
#: above what one person's recordings will ever ask for, so turning routes on for eight
#: people changes behaviour and nothing changes for the deployment that exists today.
DEFAULT_MAX_CONCURRENT = 3
#: Off. A per-minute ceiling is an account-specific number nobody should have to guess, and
#: guessing it low would slow a service that is not being throttled at all.
DEFAULT_MAX_PER_MINUTE = 0

#: The longest a waiting thread sleeps before re-checking the clock and the shutdown flag.
#: It bounds how long a SIGTERM takes to reach a thread queued for a token, so it is small.
_POLL_S = 0.05


class RateLimitError(RuntimeError):
    """Base class for the two ways a wait ends without permission to proceed."""


class RateLimitShutdown(RateLimitError):
    """The process is stopping and this thread was still waiting for its turn.

    Raised only at a thread that had **not** started its request. The recording it was
    carrying is untouched in the ledger and is picked up by the next run — which is what
    every other interruption in this service does too.
    """


class RateLimitTimeout(RateLimitError):
    """A bounded wait ran out. Only ever raised when a caller asked for a bound."""


@dataclass(frozen=True)
class LimiterState:
    """What the limiter is doing right now, for a log line or ``transcriber status``."""

    max_concurrent: int
    max_per_minute: int
    in_flight: int
    waiting: int
    tokens: float

    @property
    def enabled(self) -> bool:
        return self.max_concurrent > 0 or self.max_per_minute > 0

    def describe(self) -> str:
        if not self.enabled:
            return "no engine rate limit is set"
        parts: list[str] = []
        if self.max_concurrent > 0:
            parts.append(f"{self.in_flight}/{self.max_concurrent} transcription(s) in flight")
        if self.max_per_minute > 0:
            parts.append(f"{self.tokens:.1f} of {self.max_per_minute} request(s) per minute left")
        if self.waiting:
            parts.append(f"{self.waiting} thread(s) waiting their turn")
        return ", ".join(parts)


class RateLimiter:
    """A concurrency gate and a token bucket behind one lock. Safe from any thread.

    Both halves are optional and both are off when set to zero, which is what makes this
    object safe to leave in the request path of a deployment that has never configured it:
    an unconfigured limiter takes the lock, does two comparisons and returns.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 0,
        max_per_minute: int = 0,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        stop_event: threading.Event | None = None,
        name: str = "engine",
        poll_s: float = _POLL_S,
    ) -> None:
        self.name = name
        self._clock = clock
        self._poll_s = max(0.001, float(poll_s))
        self._stop = stop_event if stop_event is not None else _SHUTDOWN
        self._cond = threading.Condition(threading.Lock())
        self._local = threading.local()

        self._max_concurrent = 0
        self._max_per_minute = 0
        self._burst = 0.0
        self._rate = 0.0          # tokens per second
        self._tokens = 0.0
        self._filled_at = clock()
        self._in_flight = 0
        self._waiting = 0
        self.configure(max_concurrent=max_concurrent, max_per_minute=max_per_minute, burst=burst)

    # -- configuration ---------------------------------------------------------------

    def configure(
        self,
        *,
        max_concurrent: int | None = None,
        max_per_minute: int | None = None,
        burst: int | None = None,
    ) -> bool:
        """Set the limits. Returns True when something actually changed.

        Safe to call while threads hold slots: the limits are a comparison made at
        acquisition, never a fixed pool of objects, so lowering the concurrency below what
        is already in flight lets the current work finish and admits nobody new — which is
        the behaviour a person turning a number down is asking for.
        """
        with self._cond:
            wanted_concurrent = self._max_concurrent if max_concurrent is None else _positive(max_concurrent)
            wanted_minute = self._max_per_minute if max_per_minute is None else _positive(max_per_minute)
            wanted_burst = float(_positive(burst) or wanted_minute)
            changed = (
                wanted_concurrent != self._max_concurrent
                or wanted_minute != self._max_per_minute
                or wanted_burst != self._burst
            )
            if not changed:
                return False
            was_off = self._max_per_minute <= 0
            self._max_concurrent = wanted_concurrent
            self._max_per_minute = wanted_minute
            self._burst = wanted_burst
            self._rate = wanted_minute / 60.0 if wanted_minute > 0 else 0.0
            self._filled_at = self._clock()
            if wanted_minute <= 0:
                self._tokens = 0.0
            elif was_off:
                # Starting full: the limit describes a minute's traffic, and a service that
                # has just started has sent none of it.
                self._tokens = self._burst
            else:
                self._tokens = min(self._tokens, self._burst)
            self._cond.notify_all()
        log.info(
            "engine rate limit for %s: %s",
            self.name,
            self.snapshot().describe(),
        )
        return True

    @property
    def enabled(self) -> bool:
        return self._max_concurrent > 0 or self._max_per_minute > 0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_per_minute(self) -> int:
        return self._max_per_minute

    def snapshot(self) -> LimiterState:
        """A consistent reading of the state, for reporting. Takes no permission."""
        with self._cond:
            self._refill()
            return LimiterState(
                max_concurrent=self._max_concurrent,
                max_per_minute=self._max_per_minute,
                in_flight=self._in_flight,
                waiting=self._waiting,
                tokens=self._tokens,
            )

    def describe(self) -> str:
        return self.snapshot().describe()

    # -- the token bucket ------------------------------------------------------------

    def _refill(self) -> None:
        """Add whatever the monotonic clock has earned since the last look. Lock held."""
        if self._rate <= 0.0:
            return
        now = self._clock()
        elapsed = now - self._filled_at
        if elapsed <= 0.0:
            # A monotonic clock cannot go backwards; a fake one in a test can, and pretending
            # time passed would hand out an allowance nobody earned.
            self._filled_at = now
            return
        self._filled_at = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)

    def _seconds_until_token(self) -> float:
        """How long until one token exists. Lock held, refill already done."""
        if self._rate <= 0.0 or self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._rate

    # -- acquisition -----------------------------------------------------------------

    @property
    def _depth(self) -> int:
        return int(getattr(self._local, "depth", 0))

    def _acquire(self, *, want_slot: bool, want_token: bool, timeout: float | None) -> None:
        """Take a slot and/or a token, waiting until everything asked for is available.

        Both are decided in one critical section, so a caller that asks for both never ends
        up holding one while queueing for the other. A caller that asks for them separately
        — the HTTP client holds a slot for a whole request and spends a token per attempt —
        can hold a slot while waiting for a token, and that still cannot deadlock: tokens
        are earned from the clock, not handed over by another thread, so the wait ends
        whether or not anything else in this process ever moves again.
        """
        if not (want_slot or want_token):
            return
        deadline = None if timeout is None else self._clock() + max(0.0, float(timeout))
        first_wait = True
        with self._cond:
            self._waiting += 1
            try:
                while True:
                    if self._stop.is_set() and self._depth <= 0:
                        # Starting something new while the process is stopping. This thread
                        # holds nothing and is carrying a recording that has not been
                        # touched, so it stops here whether or not there is room for it.
                        raise RateLimitShutdown(
                            f"the service is stopping; this recording was still waiting for a "
                            f"turn at the {self.name} rate limit and was not started. It stays "
                            f"in the ledger and is picked up by the next run."
                        )
                    self._refill()
                    slot_free = (
                        not want_slot
                        or self._max_concurrent <= 0
                        or self._in_flight < self._max_concurrent
                    )
                    token_free = (
                        not want_token or self._rate <= 0.0 or self._tokens >= 1.0
                    )
                    if slot_free and token_free:
                        # Before the stop is looked at, deliberately. What is asked for is
                        # available right now, so taking it is not a wait and nothing is
                        # queued behind anything. Checked the other way round, a stop
                        # abandoned engine requests that were mid-transcription and had an
                        # allowance to spend — an hour-long call forty minutes into being
                        # split and transcribed piece by piece died on its next attempt with
                        # tokens still in the bucket, which is the opposite of "finish what
                        # is running".
                        if want_slot and self._max_concurrent > 0:
                            self._in_flight += 1
                        if want_token and self._rate > 0.0:
                            self._tokens -= 1.0
                        return

                    if self._stop.is_set():
                        # About to wait, and the process is going. Waiting is all this
                        # thread can do — a slot is handed back by another thread and a
                        # token is earned from the clock — so letting it go throws away
                        # nothing that has already been paid for.
                        raise RateLimitShutdown(
                            f"the service is stopping; this recording was still waiting for a "
                            f"turn at the {self.name} rate limit and was not started. It stays "
                            f"in the ledger and is picked up by the next run."
                        )

                    wait = self._poll_s
                    if slot_free and not token_free:
                        # Only the clock can help, and it is known exactly how long for.
                        wait = min(wait, max(self._seconds_until_token(), 0.001))
                    if deadline is not None:
                        left = deadline - self._clock()
                        if left <= 0.0:
                            raise RateLimitTimeout(
                                f"waited {timeout:.1f}s for the {self.name} rate limit and it "
                                f"did not come free: {self.snapshot_unlocked().describe()}"
                            )
                        wait = min(wait, left)
                    if first_wait:
                        first_wait = False
                        log.debug(
                            "waiting for the %s rate limit: %s",
                            self.name, self.snapshot_unlocked().describe(),
                        )
                    self._cond.wait(wait)
            finally:
                self._waiting -= 1

    def snapshot_unlocked(self) -> LimiterState:
        """The state as the caller already holding the lock sees it. Internal use."""
        return LimiterState(
            max_concurrent=self._max_concurrent,
            max_per_minute=self._max_per_minute,
            in_flight=self._in_flight,
            waiting=self._waiting,
            tokens=self._tokens,
        )

    def try_acquire(self, *, want_slot: bool = True, want_token: bool = True) -> bool:
        """Take what is free right now, or return False having taken nothing.

        The whole decision, with no waiting anywhere in it — which is what lets the wait
        loop above be tested on an injected clock without a single real sleep.
        """
        if not (want_slot or want_token):
            return True
        with self._cond:
            if self._stop.is_set():
                return False
            self._refill()
            if want_slot and 0 < self._max_concurrent <= self._in_flight:
                return False
            if want_token and self._rate > 0.0 and self._tokens < 1.0:
                return False
            if want_slot and self._max_concurrent > 0:
                self._in_flight += 1
            if want_token and self._rate > 0.0:
                self._tokens -= 1.0
            return True

    def release_slot(self) -> None:
        """Hand a concurrency slot back. Tokens are never handed back: they are spent."""
        with self._cond:
            if self._in_flight > 0:
                self._in_flight -= 1
            self._cond.notify_all()

    def take_token(self, timeout: float | None = None) -> None:
        """Spend one request from the per-minute allowance, waiting if there is none.

        Called once per *attempt*, retries included, because the provider counts attempts
        and not intentions. A no-op when ``ENGINE_MAX_PER_MINUTE`` is 0.
        """
        if self._rate <= 0.0:
            return
        self._acquire(want_slot=False, want_token=True, timeout=timeout)

    @contextmanager
    def slot(self, timeout: float | None = None) -> Iterator[None]:
        """Hold one concurrency slot for the body of the ``with``.

        Re-entrant per thread: the engine wrapper takes a slot for a whole transcription and
        the HTTP client inside it asks for one too. The inner ask is the same thread already
        being in flight, so it passes through rather than waiting for a slot it holds.
        """
        if self._max_concurrent <= 0 or self._depth > 0:
            self._local.depth = self._depth + 1
            try:
                yield
            finally:
                self._local.depth = self._depth - 1
            return
        self._acquire(want_slot=True, want_token=False, timeout=timeout)
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            self.release_slot()

    @contextmanager
    def guard(self, timeout: float | None = None) -> Iterator[None]:
        """A slot *and* a request from the allowance: the whole limiter, once."""
        with self.slot(timeout):
            self.take_token(timeout)
            yield

    def __enter__(self) -> "RateLimiter":
        """``with limiter:`` is :meth:`guard` — a slot and a request from the allowance.

        The open guards are kept per thread rather than on the object: this limiter is
        shared by every worker thread in the process, and a single ``_entered`` attribute
        would have one thread's exit close another thread's guard.
        """
        stack = getattr(self._local, "entered", None)
        if stack is None:
            stack = []
            self._local.entered = stack
        guard = self.guard()
        guard.__enter__()
        stack.append(guard)
        return self

    def __exit__(self, *exc: Any) -> None:
        stack = getattr(self._local, "entered", None)
        if stack:
            stack.pop().__exit__(*exc)


def _positive(value: Any) -> int:
    """A limit as a whole number of things, or 0 for off. Never raises: a limiter that
    refused to be built would stop transcription over a typo in an optional setting."""
    try:
        number = int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# --------------------------------------------------------------------------- shutdown

#: Set once, by the process, when a wait should stop being a wait. Module-level so the
#: engines' limiter and any limiter a test builds share one answer to "are we stopping".
_SHUTDOWN = threading.Event()


def request_shutdown(reason: str = "") -> None:
    """Wake every thread *waiting* on a limiter and let it stop.

    Deliberately not called on the first SIGTERM: that one means "finish what is running",
    and a thread queued for a token is carrying a recording that has not been started yet
    only because we asked it to wait. This is for the second signal — the one that says the
    process is going now — so that a queued thread cannot hold the shutdown open.
    """
    if not _SHUTDOWN.is_set():
        log.warning("release the engine rate limit: %s", reason or "the process is stopping")
    _SHUTDOWN.set()


def clear_shutdown() -> None:
    """Undo :func:`request_shutdown`. For tests and for a long-lived process that recovers."""
    _SHUTDOWN.clear()


def shutdown_requested() -> bool:
    return _SHUTDOWN.is_set()


# --------------------------------------------------------------------------- the shared one

_SHARED = RateLimiter(name="engine")
_SHARED_LOCK = threading.Lock()


def shared_limiter() -> RateLimiter:
    """The one limiter every engine's HTTP goes through, in this process."""
    return _SHARED


def limits_from_config(config: Any) -> tuple[int, int]:
    """``(max_concurrent, max_per_minute)`` from a config, an environment, or the defaults.

    Read duck-typed rather than imported: the settings live in :mod:`transcriber.config`,
    and an engine handed a stand-in config in a test — or a config from a deployment written
    before these settings existed — must still get a working limiter rather than an
    ``AttributeError`` in the middle of a transcription.
    """
    concurrent = _setting(config, "engine_max_concurrent", "ENGINE_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)
    per_minute = _setting(config, "engine_max_per_minute", "ENGINE_MAX_PER_MINUTE", DEFAULT_MAX_PER_MINUTE)
    return concurrent, per_minute


def _setting(config: Any, attribute: str, variable: str, default: int) -> int:
    value = getattr(config, attribute, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        value = os.environ.get(variable)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    resolved = _positive(value)
    if resolved == 0 and str(value).strip() not in ("0", "off", "none"):
        log.warning(
            "%s is %r, which is not a number of requests; using %d instead",
            variable, value, default,
        )
        return default
    return resolved


def configure_shared(config: Any) -> RateLimiter:
    """Point the shared limiter at this configuration. Idempotent, and never raises.

    Called from engine construction, which is the one path everything transcribing goes
    through, so the limiter cannot be left unconfigured by a caller that forgot it.
    """
    limiter = shared_limiter()
    with _SHARED_LOCK:
        try:
            concurrent, per_minute = limits_from_config(config)
            limiter.configure(max_concurrent=concurrent, max_per_minute=per_minute)
        except Exception as exc:  # noqa: BLE001 - a limiter must never stop a transcription
            log.warning("could not read the engine rate limits from the configuration: %s", exc)
    return limiter
