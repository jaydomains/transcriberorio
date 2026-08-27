"""Structured stdout logging, one line per event, with the recording's id on every line.

Two properties are the reason this module exists rather than a `basicConfig` call.

**One recording is traceable end to end.** Every line carries ``item=<graph item id>``, taken
from a context variable set once at the top of :func:`transcriber.pipeline.Pipeline.process_one`,
so nothing downstream has to remember to pass it. ``grep <item id>`` over a day of stdout
returns that recording's whole life, in order, including the failure that ended it.

**Secrets are removed mechanically, not carefully.** Redaction happens in the formatter, on
the finished line, which is the only place that sees everything: the message, its arguments,
and the formatted traceback. A key quoted back by a provider's 401 body, or a pre-authenticated
download URL that landed in an exception, is scrubbed on the way out whether or not the code
that logged it thought about it. Email addresses go the same way, under the same house rule
that this service never emits one anywhere, for any reason — a log line included.

A traceback is folded into one escaped field rather than printed over twenty lines, because
"one line per event" is what makes the stream greppable, and a service nobody can grep is a
service whose failures are found by accident.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterable, Iterator, Mapping, TextIO

from .models import EMAIL_RE, EMAIL_PLACEHOLDER

__all__ = [
    "configure",
    "add_secrets",
    "scrub",
    "EventLogger",
    "get_logger",
    "item_context",
    "set_item",
    "current_item",
    "SecretScrubber",
    "ItemFilter",
    "LineFormatter",
    "JsonFormatter",
    "REDACTED",
    "NO_ITEM",
]

REDACTED = "***REDACTED***"

#: What the item field says when a line is not about one particular recording.
NO_ITEM = "-"

#: Below this length a "secret" is a substring of ordinary English and scrubbing it would
#: shred every line. A credential shorter than this is not a credential.
MIN_SECRET_LEN = 6

_ITEM: ContextVar[str] = ContextVar("transcriber_item", default=NO_ITEM)

#: Attributes every LogRecord has. Anything else a caller attached via ``extra=`` is an
#: event field and is rendered as ``key=value``.
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "stacklevel", "thread", "threadName", "taskName", "item", "event",
    }
)


# --------------------------------------------------------------------------- redaction


class SecretScrubber:
    """The configured secret values, and one method that removes them from a string.

    Values are held once, process-wide, so a module that never saw the config still cannot
    print a key: everything goes through the same formatter on the way to stdout.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._values: tuple[str, ...] = ()
        self.add(values)

    def add(self, values: Iterable[str]) -> None:
        with self._lock:
            merged = set(self._values)
            for value in values or ():
                if isinstance(value, str) and len(value.strip()) >= MIN_SECRET_LEN:
                    merged.add(value.strip())
            # Longest first: a key that contains a shorter secret must not be half-replaced
            # into something that still shows the tail of the longer one.
            self._values = tuple(sorted(merged, key=len, reverse=True))

    @property
    def values(self) -> tuple[str, ...]:
        return self._values

    def __call__(self, text: str) -> str:
        return self.scrub(text)

    def scrub(self, text: str) -> str:
        if not text:
            return text or ""
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        # The house rule, applied to our own diagnostics as well as to our outputs.
        return EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)


#: The one scrubber every handler this module installs shares.
_SCRUBBER = SecretScrubber()


def add_secrets(values: Iterable[str]) -> None:
    """Register secret values to be removed from every log line from now on."""
    _SCRUBBER.add(values)


def scrub(text: str) -> str:
    """Remove every registered secret and every email address from a string."""
    return _SCRUBBER.scrub(text)


# --------------------------------------------------------------------------- item context


def set_item(item_id: str | None) -> Any:
    """Bind the recording every subsequent log line in this context is about."""
    return _ITEM.set(str(item_id or NO_ITEM))


def current_item() -> str:
    return _ITEM.get()


@contextmanager
def item_context(item_id: str | None) -> Iterator[str]:
    """Bind an item id for the duration of a block.

    Set inside the worker thread rather than at submit time: a ``ThreadPoolExecutor`` worker
    runs in its own context and does not inherit the submitting thread's.
    """
    token = set_item(item_id)
    try:
        yield current_item()
    finally:
        _ITEM.reset(token)


class ItemFilter(logging.Filter):
    """Give every record an ``item`` field, from ``extra=`` or from the context."""

    def filter(self, record: logging.LogRecord) -> bool:
        existing = getattr(record, "item", None)
        record.item = str(existing) if existing else current_item()
        if not getattr(record, "event", None):
            record.event = record.name.rsplit(".", 1)[-1]
        return True


# --------------------------------------------------------------------------- formatting


def _quote(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if text == "":
        return '""'
    if any(ch in text for ch in ' ="'):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def _fields(record: logging.LogRecord) -> list[tuple[str, Any]]:
    return [
        (key, value)
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    ]


def _stamp(created: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(created)) + f".{int(created % 1 * 1000):03d}Z"


class _ScrubbingFormatter(logging.Formatter):
    """Base: whatever the subclass builds, the secrets come out of it before it is written."""

    def format(self, record: logging.LogRecord) -> str:
        return _SCRUBBER.scrub(self._line(record))

    def _line(self, record: logging.LogRecord) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _trace(self, record: logging.LogRecord) -> str:
        if not record.exc_info:
            return ""
        return self.formatException(record.exc_info)


class LineFormatter(_ScrubbingFormatter):
    """``<ts> <LEVEL> item=<id> event=<event> msg="..." k=v`` — one line, always."""

    def _line(self, record: logging.LogRecord) -> str:
        parts = [
            _stamp(record.created),
            record.levelname,
            f"item={_quote(getattr(record, 'item', NO_ITEM))}",
            f"event={_quote(getattr(record, 'event', record.name))}",
            f"msg={_quote(record.getMessage())}",
        ]
        for key, value in _fields(record):
            parts.append(f"{key}={_quote(value)}")
        parts.append(f"logger={_quote(record.name)}")
        trace = self._trace(record)
        if trace:
            parts.append(f"traceback={_quote(trace)}")
        return " ".join(parts)


class JsonFormatter(_ScrubbingFormatter):
    """The same event as one JSON object per line, for a log shipper rather than a person."""

    def _line(self, record: logging.LogRecord) -> str:
        import json

        payload: dict[str, Any] = {
            "ts": _stamp(record.created),
            "level": record.levelname,
            "item": getattr(record, "item", NO_ITEM),
            "event": getattr(record, "event", record.name),
            "msg": record.getMessage(),
            "logger": record.name,
        }
        for key, value in _fields(record):
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        trace = self._trace(record)
        if trace:
            payload["traceback"] = trace
        return json.dumps(payload, ensure_ascii=False, sort_keys=False)


# --------------------------------------------------------------------------- setup


#: Marks the handler this module owns, so configure() can be called twice without stacking
#: two copies of every line — which is what makes a duplicated log look like duplicated work.
_OURS = "_transcriber_handler"


def configure(
    config: Any = None,
    *,
    level: str | int | None = None,
    stream: TextIO | None = None,
    json_lines: bool | None = None,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    """Install one stdout handler on the root logger. Idempotent.

    ``config`` is a :class:`~transcriber.config.Config` when there is one: its
    ``secret_values()`` are registered for redaction and its ``log_level`` is used. There is
    deliberately no file handler and no rotation — the process logs to stdout and whatever
    runs it decides where that goes.
    """
    if config is not None:
        getter = getattr(config, "secret_values", None)
        if callable(getter):
            add_secrets(getter())
        if level is None:
            level = getattr(config, "log_level", None)
    add_secrets(secrets)

    if json_lines is None:
        json_lines = (os.environ.get("LOG_FORMAT", "") or "").strip().lower() == "json"
    resolved = _level(level)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _OURS, False):
            root.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter() if json_lines else LineFormatter())
    handler.addFilter(ItemFilter())
    handler.setLevel(resolved)
    setattr(handler, _OURS, True)
    root.addHandler(handler)
    root.setLevel(resolved)
    return logging.getLogger("transcriber")


def _level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    name = str(level or "INFO").strip().upper()
    value = logging.getLevelName(name)
    return value if isinstance(value, int) else logging.INFO


# --------------------------------------------------------------------------- the caller's API


class EventLogger:
    """A thin wrapper that names the event and keeps the fields structured.

    ``log.info("downloaded", "89.4 MB in 41s", bytes=93_782_016)`` rather than an f-string:
    the message stays readable and the numbers stay machine-readable, and neither has to be
    parsed back out of English later.
    """

    __slots__ = ("_logger", "_item", "_base")

    def __init__(self, logger: logging.Logger, item: str | None = None, **base: Any) -> None:
        self._logger = logger
        self._item = item
        self._base = base

    def bind(self, item: str | None = None, **fields: Any) -> "EventLogger":
        merged = dict(self._base)
        merged.update(fields)
        return EventLogger(self._logger, item if item is not None else self._item, **merged)

    def _emit(self, level: int, event: str, message: str, fields: Mapping[str, Any], exc_info: Any = None) -> None:
        extra = dict(self._base)
        extra.update(fields)
        extra["event"] = event
        if self._item:
            extra["item"] = self._item
        self._logger.log(level, "%s", message, extra=_safe_extra(extra), exc_info=exc_info)

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self._emit(logging.DEBUG, event, message, fields)

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self._emit(logging.INFO, event, message, fields)

    def warning(self, event: str, message: str = "", **fields: Any) -> None:
        self._emit(logging.WARNING, event, message, fields)

    def error(self, event: str, message: str = "", *, exc_info: Any = None, **fields: Any) -> None:
        self._emit(logging.ERROR, event, message, fields, exc_info=exc_info)

    def exception(self, event: str, message: str = "", **fields: Any) -> None:
        self._emit(logging.ERROR, event, message, fields, exc_info=True)


#: Names logging itself owns. A field called "module" or "name" would make the standard
#: library raise instead of logging, which is a poor way for a diagnostic to fail.
_RESERVED = frozenset(_STANDARD_ATTRS) - {"item", "event"}


def _safe_extra(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {(f"{k}_" if k in _RESERVED else k): v for k, v in fields.items()}


def get_logger(name: str, item: str | None = None, **base: Any) -> EventLogger:
    return EventLogger(logging.getLogger(name), item, **base)
