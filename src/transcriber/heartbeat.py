"""Ping an external monitor, so something OUTSIDE this service notices when it goes quiet.

Every check inside a service shares the service's fate. If the process dies, its own
alerting dies with it, and the silence looks exactly like a quiet week — which is precisely
how four days of recordings went missing without anybody noticing. The only cure is a
watcher that is not us: a healthchecks.io-shaped URL that expects to hear from us on a
schedule and raises the alarm when it does not.

Two properties follow from that, and the first is inverted from how errors are handled
everywhere else in this service:

  * **A failed ping never breaks the pipeline.** The monitor observes the work; it does not
    gate it. Every entry point here catches everything short of a process signal and returns
    a result object instead of raising. A recording must not go untranscribed because a
    status page was down.
  * **A failed ping is still never silent.** It is logged at WARNING and reported in the
    return value so the morning digest can carry it. And note what a failed ping means at
    the other end: the monitor did not hear from us, so *it* alerts. Losing the ping and
    losing the service look the same from outside, on purpose.

The URL is a secret (it is a bearer capability — anyone holding it can silence the alarm),
so it is never logged and never printed. Only the ping's outcome is.

healthchecks.io endpoint shape, which this follows::

    POST <url>            the cycle finished; reset the timer
    POST <url>/start      the cycle began; measures duration and catches a hang
    POST <url>/fail       the cycle failed; alert now rather than at the timeout
    POST <url>/log        a note, with no effect on the timer
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .models import strip_emails
from .redirects import no_redirect_opener, redirect_host

log = logging.getLogger("transcriber.heartbeat")

__all__ = ["Heartbeat", "PingResult", "MAX_NOTE_BYTES", "USER_AGENT"]

USER_AGENT = "kbc-transcriber/1.0 (+stdlib-urllib)"

#: healthchecks.io keeps the first 10 KB of a ping body and discards the rest. Truncating
#: here rather than there means what is stored is a whole sentence, not half of one.
MAX_NOTE_BYTES = 10_000

#: Statuses worth trying again. Everything else is a statement about our request — a wrong
#: monitor id repeated five times is still a wrong monitor id.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

#: Built once, and deliberately not the plain default opener. A ping is a claim that this
#: service is alive, and it is only true if it reached the monitor: following a redirect
#: would let some other host answer "200" on the monitor's behalf, and the check would go
#: on quietly counting down to an alert while the log said the ping was acknowledged.
_OPENER = no_redirect_opener()


@dataclass(frozen=True)
class PingResult:
    """What happened when we tried to tell the outside world we are alive.

    ``skipped`` is distinct from ``ok=False``: no monitor configured is an operator's
    choice, while a monitor that would not answer is a fact worth reporting.
    """

    kind: str                       # "success" | "start" | "fail" | "log"
    ok: bool
    skipped: bool = False
    status: int | None = None
    attempts: int = 0
    elapsed_s: float = 0.0
    detail: str = ""

    def summary(self) -> str:
        """One line for a log or the morning digest. Never contains the URL."""
        if self.skipped:
            return f"heartbeat {self.kind}: not configured, nothing outside this service is watching"
        if self.ok:
            return f"heartbeat {self.kind}: acknowledged (HTTP {self.status})"
        return (
            f"heartbeat {self.kind}: FAILED after {self.attempts} "
            f"attempt{'' if self.attempts == 1 else 's'} — {self.detail}"
        )


class Heartbeat:
    """A healthchecks.io-shaped external monitor. Nothing here can raise at a caller."""

    def __init__(
        self,
        url: str,
        *,
        timeout_s: float = 10.0,
        attempts: int = 3,
        backoff_s: float = 2.0,
        scrub: Callable[[str], str] | None = None,
        opener: Callable[..., Any] = _OPENER.open,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = (url or "").strip()
        self.timeout_s = float(timeout_s)
        self.attempts = max(1, int(attempts))
        self.backoff_s = float(backoff_s)
        self._scrub = scrub or (lambda text: text)
        self._opener = opener
        self._sleep = sleep
        self._clock = clock
        self._register_secret()

    def _register_secret(self) -> None:
        """Register the URL *and its id segment* with the log scrubber.

        Redaction is a literal substring replacement, and ``endpoint("fail")`` builds
        ``.../<uuid>/fail?create=1`` — a string the registered URL is not a substring of, so
        neither scrubber would match it. The URL is a bearer capability: anyone holding it
        can silence the alarm. Registering the id itself covers every derived form.
        """
        if not self.url:
            return
        try:
            from . import logging_setup  # noqa: PLC0415 - avoids an import cycle at module load

            parts = urllib.parse.urlsplit(self.url)
            segments = [seg for seg in parts.path.split("/") if len(seg) >= 8]
            logging_setup.add_secrets([self.url, *segments])
        except Exception:  # noqa: BLE001 - a monitor must never fail to be built
            pass

    @classmethod
    def from_config(cls, config: Any, **overrides: Any) -> "Heartbeat":
        """Build from a :class:`~transcriber.config.Config`.

        ``config.scrub`` is passed in so that if a note ever picks up a secret — an error
        string that quoted a URL with a token in it, say — the secret is removed on the way
        out rather than posted to a third-party status page.
        """
        kwargs: dict[str, Any] = {
            "timeout_s": float(getattr(config, "http_timeout_s", 10) or 10),
            "scrub": getattr(config, "scrub", None),
        }
        kwargs.update(overrides)
        return cls(getattr(config, "heartbeat_url", "") or "", **kwargs)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    # -- the four pings ------------------------------------------------------------

    def success(self, note: str = "") -> PingResult:
        """The cycle finished. Resets the monitor's timer; this is the important one."""
        return self._ping("success", "", note)

    def start(self, note: str = "") -> PingResult:
        """The cycle began. Lets the monitor catch a run that hangs rather than fails."""
        return self._ping("start", "start", note)

    def fail(self, note: str = "") -> PingResult:
        """The cycle failed. Alerts now instead of waiting for the grace period to lapse."""
        return self._ping("fail", "fail", note)

    def log(self, note: str) -> PingResult:
        """A note attached to the check, with no effect on the timer."""
        return self._ping("log", "log", note)

    # -- internals -----------------------------------------------------------------

    def endpoint(self, suffix: str) -> str:
        """Append healthchecks' path suffix while preserving any query string it carries."""
        if not suffix:
            return self.url
        parts = urllib.parse.urlsplit(self.url)
        path = parts.path.rstrip("/") + "/" + suffix.strip("/")
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))

    def _body(self, note: str) -> bytes:
        """The note, with any secret and any email address removed before it leaves us.

        The monitor is a third party. A ping body is the least likely place for an address
        to end up and it is still checked, because "unlikely" is not the standard.
        """
        text = strip_emails(self._clean(note or ""))
        raw = text.encode("utf-8", "replace")
        if len(raw) <= MAX_NOTE_BYTES:
            return raw
        return raw[: MAX_NOTE_BYTES - 3].rstrip() + b"..."

    def _clean(self, text: str) -> str:
        """The caller's scrubber and the log's, so a derived monitor URL cannot ride out."""
        value = self._scrub(text or "")
        try:
            from . import logging_setup  # noqa: PLC0415

            value = logging_setup.scrub(value)
        except Exception:  # noqa: BLE001
            pass
        return value

    def _ping(self, kind: str, suffix: str, note: str) -> PingResult:
        if not self.configured:
            log.warning(
                "no heartbeat URL is configured — nothing outside this service will notice "
                "if it stops running, which is the failure this ping exists to prevent"
            )
            return PingResult(kind=kind, ok=False, skipped=True, detail="no HEARTBEAT_URL configured")

        url = self.endpoint(suffix)
        body = self._body(note)
        started = self._clock()
        detail = ""
        status: int | None = None

        for attempt in range(1, self.attempts + 1):
            try:
                request = urllib.request.Request(
                    url,
                    data=body,
                    method="POST",
                    headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain; charset=utf-8"},
                )
                with self._opener(request, timeout=self.timeout_s) as response:
                    status = int(getattr(response, "status", 0) or 200)
                    response.read(2048)
                elapsed = self._clock() - started
                log.debug("heartbeat %s acknowledged (HTTP %s) in %.2fs", kind, status, elapsed)
                return PingResult(kind=kind, ok=True, status=status, attempts=attempt, elapsed_s=elapsed)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                detail = f"HTTP {status} from the monitor"
                if 300 <= status < 400:
                    # Said plainly, because the fix is a person's: the URL is pointing at
                    # something that is not the monitor, and the ping was not delivered.
                    detail = (
                        "the ping URL redirected to "
                        + redirect_host(exc.headers.get("Location", "") if exc.headers else "")
                        + " instead of answering; the monitor was not reached. Check that "
                        "HEARTBEAT_URL is the ping URL the monitor gave you."
                    )
                if status not in RETRYABLE_STATUSES:
                    # A 404 here means the monitor id is wrong: the check has never been
                    # pinged, so it is either already alerting or was never created. Either
                    # way, repeating the request cannot fix it.
                    break
            except Exception as exc:  # noqa: BLE001 - deliberate: a ping may never propagate
                # Deliberately broad. DNS, TLS, a proxy, a socket timeout, a monitor that
                # closed the connection — none of it is a reason to stop transcribing.
                #
                # The message is scrubbed rather than interpolated raw: several of the
                # exceptions that can land here (``ValueError: unknown url type``,
                # ``http.client.InvalidURL``) carry the URL — and therefore the live monitor
                # id — in their text.
                status = None
                detail = self._clean(f"{type(exc).__name__}: {exc}")

            if attempt < self.attempts:
                self._sleep(self.backoff_s * attempt)

        elapsed = self._clock() - started
        # WARNING, not ERROR: from the monitor's side an unsent ping and a dead service are
        # the same event, and it is about to say so.
        log.warning(
            "heartbeat %s could not be delivered after %d attempt(s) — %s; the external "
            "monitor will treat this as silence and alert, which is the intended behaviour",
            kind, self.attempts, detail,
        )
        return PingResult(
            kind=kind, ok=False, status=status, attempts=self.attempts, elapsed_s=elapsed, detail=detail
        )
