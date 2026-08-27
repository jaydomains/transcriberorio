"""The test suite. Offline, no credentials, no network — that is a property, not a habit.

``src`` is put on ``sys.path`` here rather than in each test module, so the suite runs the
same way from ``python3 -m unittest discover -s tests`` at the repository root, from
``make test``, and from an editor that imports one file on its own.

Nothing in this package may import a third-party module: the service has no runtime
dependency by design, and a test suite that needs one would quietly make it two.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _quieten_service_logging() -> None:
    """Keep the service's own log lines out of the test report.

    They are deliberately loud — that is the whole design — and several of these tests
    exercise the failure paths that shout. The suite's output should be the assertions, so
    the service's logger is muted here and can be turned back on with
    ``TRANSCRIBER_TEST_LOGS=1`` when something needs diagnosing.
    """
    import logging

    if os.environ.get("TRANSCRIBER_TEST_LOGS"):
        logging.basicConfig(level=logging.DEBUG)
        return
    logger = logging.getLogger("transcriber")
    logger.handlers[:] = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)


_quieten_service_logging()


def _forbid_the_network() -> None:
    """Make "offline" a property of the run rather than a claim in a docstring.

    Every HTTP client in this service takes an injectable opener and every test passes one,
    so nothing here should ever open a socket. Enforcing it means a test that quietly starts
    talking to a real endpoint — the classic way a suite becomes slow, flaky and dependent
    on a credential — fails immediately and says which call did it.

    ``socket.gethostname`` and the like are untouched: only an outbound connection is
    refused. Set ``TRANSCRIBER_TEST_ALLOW_NETWORK=1`` to lift it for a deliberate
    experiment.
    """
    import socket

    if os.environ.get("TRANSCRIBER_TEST_ALLOW_NETWORK"):
        return

    def _refuse(where: object) -> None:
        raise AssertionError(
            f"the test suite tried to open a network connection to {where!r}. It runs "
            "offline with no credential; pass an opener or a caller instead."
        )

    def refuse_method(self, address, *args, **kwargs):      # socket.socket.connect
        _refuse(address)

    def refuse_function(address, *args, **kwargs):          # socket.create_connection
        _refuse(address)

    socket.socket.connect = refuse_method           # type: ignore[method-assign]
    socket.socket.connect_ex = refuse_method        # type: ignore[method-assign]
    socket.create_connection = refuse_function      # type: ignore[assignment]


_forbid_the_network()
