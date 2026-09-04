"""One recording, from the change feed to three confirmed files in OneDrive.

The whole state machine lives in :meth:`Pipeline.process_one`, and two rules shape every
line of it.

**Every transition is persisted before the work that follows it.** The row says FETCHED
before anything probes the audio; it says TRANSCRIBED before the AI pass starts; it says
ANALYSED before a byte is uploaded. A crash anywhere leaves a row that says exactly how far
the recording got, and the next poll picks it up from there. Nothing is ever lost, and the
expensive steps are not paid for twice: the downloaded audio and the engine's transcript are
kept in the work directory, keyed to the content hash, so a resumed recording re-uses them
only when the bytes are provably the same ones.

**There are two endings for a failure and no third.** A failure either retries with backoff
or quarantines loudly. There is no path here that marks a recording done on incomplete
evidence, no path that drops one quietly, and no path that treats "we could not tell" as
success — the check that cannot be made is the failure, and it is written down as one.

One distinction the classification turns on: a fault in the *recording* quarantines the
recording; a fault in the *service* — a rejected credential, an unusable configuration, a
broken ledger — stops the service. Quarantining a thousand recordings because a key expired
would bury the one fact anybody needs, so those raise :class:`PipelineFatal` and the worker
shuts down saying why.

**Where a recording's outputs go is decided by its route, and by nothing else.** The row
carries the route it was discovered on; the route carries the output folder and, when it
overrides it, the transcription engine. Nothing here reads a service-wide output folder, and
nothing here falls back to one: a row whose route is no longer in the configuration is
quarantined, loudly, naming the route and the routes that do exist. Writing a site meeting's
transcript into the phone-calls folder because the route it named had been renamed is
exactly the kind of quiet wrongness this service exists to make impossible.

**The sensitivity gate sits between the transcript and the three files, and its order is
not a preference.** Four steps, in this order and no other:

1. *Read the transcript for sensitive passages, before the AI pass.* The mechanical rules —
   an explicit instruction not to write something down, a bare identifier that validates as
   one — run here, on the text exactly as the engine returned it. They run here rather than
   after the analysis because a twelve-second recording is never sent to the strong model
   at all: it is routed as trivial, produces no sensitive-passage list, and would otherwise
   pass through the gate unread while saying "don't write this down" out loud.
2. *Run the AI pass on the unredacted text.* Its quote verification asks "did the model
   invent this?", and only the text as transcribed can answer that. Analyse a masked
   transcript and every item quoting a masked passage fails verification and is discarded —
   a redaction that silently destroys action items. The model's own reading of what is
   sensitive comes back in the same answer and is folded into the classification here.
3. *Store the words, then cut them.* A held passage, once cut, exists in two places: the
   audio and the held-passage store. So the store is written first, all spans in one
   transaction, and a store that will not take them means nothing is cut and nothing is
   published. Never the other way round.
4. *Mask the transcript text, then everything derived from it.* On the transcript, not on
   the actions file: the actions file is named so the record never ingests it, and only the
   transcript reaches the record. A redaction in the wrong file is not a redaction.

Every mode runs all four steps. ``GATE_MODE`` is carried into them and decides one thing
only — whether anything is actually withheld — so ``shadow`` measures the real classifier
on real recordings down the same code path that ``on`` will use, and changes nothing else
about what is written. It ships dark for a reason: the design passes' estimates of how much
this touches differ by a factor of twenty-five, and arming it against an estimate is how the
queue becomes a wall he bounces off.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from . import audio as audio_probe
from . import autoname, completeness, naming, outputs, plausibility, redact, sensitivity
from . import sitebook
from .engines import (
    EngineAudioTooLarge,
    EngineAuthError,
    EngineConfigError,
    SplitDurationError,
    SplitUnsupported,
    create_engine,
)
from .engines import transcribe as run_engine
from .extract import (
    AnalysisAuthError,
    AnalysisConfigError,
    Extraction,
    Extractor,
    TranscriptTooLarge,
)
from .graph import (
    GraphAuthError,
    GraphClient,
    GraphConfigError,
    GraphHTTPError,
    IncompleteDownload,
    RetryPolicy,
)
from .ledger import Ledger, LedgerError, LedgerStateError
from .logging_setup import get_logger, item_context
from .models import (
    AUDIO_EXTENSIONS,
    DEFAULT_ROUTE,
    AudioInfo,
    Hints,
    Route,
    Row,
    Segment,
    State,
    Transcript,
    utc_now_iso,
)
from .naming import TimestampUnavailable
from .outputs import HeldTextWouldLeak, OutputContractError, UploadIncompleteError
from .ratelimit import RateLimitShutdown
from .sensitivity import GateSettings
from .withheld import HeldSpan, WithheldStore, held_spans_from

__all__ = [
    "Pipeline",
    "Outcome",
    "GateResult",
    "PipelineFatal",
    "build_graph",
    "build_engine",
    "process_one",
    "RESULT_DONE",
    "RESULT_QUARANTINED",
    "RESULT_SKIPPED_EMPTY",
    "RESULT_RETRY",
    "RESULT_DEFERRED",
    "RESULT_NOT_CLAIMED",
    "RESULT_ALREADY_FINISHED",
    "INCOMPLETE_DEADLINE_S",
]

log = get_logger(__name__)

RESULT_DONE = "done"
RESULT_QUARANTINED = "quarantined"
RESULT_SKIPPED_EMPTY = "skipped-empty"
RESULT_RETRY = "retry"
RESULT_DEFERRED = "deferred"
RESULT_NOT_CLAIMED = "not-claimed"
RESULT_ALREADY_FINISHED = "already-finished"

#: How long a recording may sit at the completeness gate before the wait itself becomes the
#: failure. Six hours is far longer than any upload from a phone on a bad line, and shorter
#: than a working day, so a file that never finishes uploading is on somebody's list the same
#: day rather than pending invisibly forever.
INCOMPLETE_DEADLINE_S = 6 * 3600.0

#: Retry backoff between attempts on the same recording: a minute, then two, then four, up to
#: an hour. Jittered so two workers that failed on the same throttled response do not return
#: together.
RETRY_BASE_S = 60.0
RETRY_MAX_S = 3600.0

_META_RETRY_AT = "retry_at"
_META_RETRY_REASON = "retry_reason"
_META_GATE_FIRST_SEEN = "gate_first_seen"

#: Where the sensitivity gate's own record of this recording goes in the ledger row. Counts,
#: categories and hold references only — never a word of what was held. It is also what stops
#: a retry counting the same recording twice in the measurement the arming decision rests on.
_META_SENSITIVITY = "sensitivity"
_META_NAMING = "naming"
#: When the recording was made, pinned on the row the first time it is worked out.
#:
#: The three output filenames open with this moment, so anything that changes how it is
#: DERIVED changes the names — and a recording republished across such a change writes three
#: new files while the three it already wrote stay in OneDrive forever. Nothing can clean
#: them up: the ledger row has been overwritten with the new names, the collision guard only
#: looks at other rows, Graph has no delete here, and the sweep never enumerates an output
#: folder. Downstream the record keys a document on its date and its bytes, so it logs a
#: second document, in a different month, with a second row in the site's log.
#:
#: This is not hypothetical: the change that added it moved this moment for exactly the
#: recordings it targets — an unnamed one used to be dated by when OneDrive finished
#: receiving it. Pinned here, the names are a function of the row rather than of whichever
#: build happens to be running.
_META_RECORDED_AT = "recorded_at"
_META_RECORDED_NOTE = "recorded_at_note"
#: What the analysis pass used, one entry per model call. TOKENS, not money -
#: ``transcriber.prices`` does the multiplication at reporting time so a price
#: change does not make the record retrospectively wrong.
_META_SPEND = "spend"

_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._-]+")


class PipelineFatal(RuntimeError):
    """The service cannot work at all: a credential, a configuration, or the ledger.

    Never used for a fault in one recording. It stops the worker, loudly, because a bad key
    that quarantined the whole backlog would destroy the very evidence a person needs.
    """


@dataclass(frozen=True)
class Outcome:
    """What happened to one recording, in a form the worker can count and print."""

    item_id: str
    name: str = ""
    result: str = RESULT_RETRY
    state: str = ""
    reason: str = ""
    attempts: int = 0
    elapsed_s: float = 0.0
    outputs: dict[str, str] = field(default_factory=dict)
    #: The route this recording arrived on, carried out with the result so a report can be
    #: broken down per route without going back to the ledger for every row.
    route: str = ""

    @property
    def ok(self) -> bool:
        """Finished cleanly. ``SKIPPED_EMPTY`` counts: verified silence is a real answer."""
        return self.result in (RESULT_DONE, RESULT_SKIPPED_EMPTY, RESULT_ALREADY_FINISHED)

    @property
    def needs_a_person(self) -> bool:
        return self.result == RESULT_QUARANTINED

    def line(self) -> str:
        head = f"{self.result}: {self.name or self.item_id}"
        return f"{head} — {self.reason}" if self.reason else head


@dataclass(frozen=True)
class GateResult:
    """What the sensitivity gate did to one recording, before any file was rendered.

    ``transcript`` and ``extraction`` are what the three files are rendered from. In
    ``shadow`` and ``off`` they are the originals, word for word, because those modes
    withhold nothing; in ``on`` they are the same things with every held passage replaced by
    its marker.

    ``held`` is what was actually cut, and it is empty in every mode but ``on``. It travels
    to :mod:`transcriber.outputs`, which refuses to publish any of the three files if one of
    these passages survived into one of them.

    Nothing in this object carries a held word except ``held`` itself, which carries them
    because the backstop has to search for them. ``note`` is the ledger's copy and is counts
    and references only.
    """

    report: Any                                  # sensitivity.Report
    transcript: Transcript
    extraction: Any
    held: tuple[HeldSpan, ...] = ()
    notes: tuple[str, ...] = ()
    note: dict[str, Any] = field(default_factory=dict)
    measured: bool = False

    @property
    def withheld(self) -> bool:
        return bool(self.held)


# --------------------------------------------------------------------------- construction


def build_graph(config: Any, **overrides: Any) -> GraphClient:
    """A Graph client from our own :class:`~transcriber.config.Config` field names.

    Built explicitly rather than through ``GraphClient.from_config``: that classmethod is
    duck-typed against a different set of attribute names, and a silently empty tenant id is
    the kind of thing that fails at 3am rather than at startup.
    """
    kwargs: dict[str, Any] = {
        "tenant_id": str(getattr(config, "graph_tenant_id", "") or ""),
        "client_id": str(getattr(config, "graph_client_id", "") or ""),
        "client_secret": str(getattr(config, "graph_client_secret", "") or ""),
        "user_id": str(getattr(config, "graph_user_id", "") or ""),
        "timeout": float(getattr(config, "http_timeout_s", 60) or 60),
        "retry": RetryPolicy(max_attempts=max(2, int(getattr(config, "max_retries", 5) or 5))),
    }
    kwargs.update(overrides)
    return GraphClient(**kwargs)


def engine_config_for(config: Any, route: Route | None) -> Any:
    """The config an engine factory should read for this route.

    A route may transcribe with a different engine from the service default, and the
    factories all read ``config.engine`` and ``config.engine_key``. Rather than teach three
    engines about routes, the route's choice is written into a copy of the config here — one
    place, so a route override cannot be honoured by one engine and ignored by another.

    ``ENGINE_BASE_URL`` and the Azure region describe the *service* engine's endpoint, so
    they are dropped when a route asks for a different engine: pointing ElevenLabs at an
    OpenAI-compatible URL somebody set months ago would fail in a way nobody could read.
    """
    wanted = (getattr(route, "engine", "") or "").strip().lower()
    default = (getattr(config, "engine", "") or "").strip().lower()
    if not wanted or wanted == default:
        return config
    key_for = getattr(config, "engine_key_for", None)
    key = key_for(route) if callable(key_for) else ""
    if not key:
        raise EngineConfigError(
            f"the route {getattr(route, 'name', '?')!r} transcribes with {wanted!r} instead "
            f"of the service default {default!r}, and no API key for {wanted!r} is "
            f"configured, so nothing on that route can be transcribed"
        )
    try:
        return replace(config, engine=wanted, engine_key=key, engine_base_url="", azure_region=(
            getattr(config, "azure_region", "") if wanted == "azure" else ""
        ))
    except TypeError:
        # A stand-in config that is not a dataclass. Say so rather than quietly transcribing
        # this route with the wrong engine.
        raise EngineConfigError(
            f"this configuration cannot be specialised for route "
            f"{getattr(route, 'name', '?')!r}, which asks for the {wanted!r} engine"
        ) from None


def build_engine(config: Any, graph: Any = None, route: Route | None = None) -> Any:
    """The transcription engine for this route, wired for Graph if it needs a content URL.

    Azure's batch API fetches the audio itself and never receives an upload, so it needs a
    URL rather than a path. Only the pipeline knows the Graph item id behind a local file, so
    the mapping is supplied here.
    """
    engine = create_engine(engine_config_for(config, route))
    if graph is not None and hasattr(engine, "with_content_url_provider"):
        def provider(path: str) -> str:
            item_id = _item_id_of_local(path)
            if not item_id:
                return ""
            return str(getattr(graph.get_item(item_id), "download_url", "") or "")

        engine.with_content_url_provider(provider)
    return engine


#: Local audio path -> Graph item id, populated as each download lands. Only the Azure
#: content-URL provider reads it; nothing else needs to invert a path.
_LOCAL_ITEM_IDS: dict[str, str] = {}
_LOCAL_LOCK = threading.Lock()


def _remember_local(path: str, item_id: str) -> None:
    with _LOCAL_LOCK:
        _LOCAL_ITEM_IDS[os.path.abspath(path)] = item_id


def _item_id_of_local(path: str) -> str:
    with _LOCAL_LOCK:
        return _LOCAL_ITEM_IDS.get(os.path.abspath(path), "")


# --------------------------------------------------------------------------- the pipeline


class Pipeline:
    """The state machine for one recording at a time. Safe to call from several threads."""

    def __init__(
        self,
        config: Any,
        ledger: Ledger,
        graph: Any,
        *,
        engine: Any = None,
        extractor: Any = None,
        withheld: Any = None,
        owner: str | None = None,
        clock: Any = time.time,
        sleep: Any = time.sleep,
        verify_uploaded_bytes: bool = False,
        incomplete_deadline_s: float = INCOMPLETE_DEADLINE_S,
        keep_work_files: bool = False,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.graph = graph
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.clock = clock
        self.sleep = sleep
        self.verify_uploaded_bytes = verify_uploaded_bytes
        self.incomplete_deadline_s = float(incomplete_deadline_s)
        self.keep_work_files = keep_work_files
        self.lease_seconds = int(getattr(config, "lease_seconds", 900) or 900)
        self.settle_seconds = float(getattr(config, "settle_interval_s", 60) or 60)
        self.max_attempts = max(1, int(getattr(config, "max_attempts", 3) or 3))
        self.work_dir = str(getattr(config, "work_dir", "") or "") or os.path.join(
            os.path.abspath("."), ".transcriber-work"
        )
        self._engine = engine
        #: One engine per engine *name*, not per route: two routes that both use the service
        #: default share one client and one connection pool, which is what the concurrency
        #: limit assumes. Keyed by name so a route override builds its own, once.
        self._engines: dict[str, Any] = {}
        self._extractor = extractor
        #: Resolved once, at construction: which mode the gate runs in, and who reviews
        #: which route's held passages. Reading it per recording would mean two recordings
        #: of one poll could be classified under different rules if the file changed
        #: underneath, and "when did it start holding these?" would have no answer.
        self.gate = GateSettings.from_config(config)
        self._withheld = withheld
        self._build_lock = threading.Lock()
        self._rng = random.Random()
        #: The record's site list, for naming a recording that arrived without a name.
        #: Loaded on first use and re-read only when the file's modification time changes,
        #: so the nightly rebuild is picked up without a restart and without a read per
        #: recording. Never fatal: an unreadable list is an empty one, and an empty one
        #: means nothing is named — exactly the behaviour before naming existed.
        self._site_book = sitebook.EMPTY
        self._site_book_lock = threading.Lock()

    # -- lazily built collaborators ------------------------------------------------

    @property
    def site_book(self) -> sitebook.SiteBook:
        """The record's sites, re-read when the nightly build has rewritten them.

        Wrapped whole, and :func:`transcriber.sitebook.load` does not raise either. Two
        layers for one file because this is read on the path that publishes a recording,
        and the worst outcome it may ever cause is a plainer title.
        """
        path = str(getattr(self.config, "naming_sites_file", "") or "").strip()
        if not path:
            return sitebook.EMPTY
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        with self._site_book_lock:
            book = self._site_book
            if book.path == path and book.mtime == mtime and (book or book.fault):
                return book
            try:
                book = sitebook.load(path)
            except Exception as exc:                                # pragma: no cover
                book = sitebook.SiteBook(path=path, fault=f"could not be read ({exc})")
            # Stamp the mtime we looked at, even on a fault. Without it a corrupt-but-present
            # file never matches the cache key, so every recording re-reads and re-parses it
            # — blocking file I/O on the publish path, behind a lock every worker shares.
            book = replace(book, mtime=mtime, path=path)
            self._site_book = book
            if book.fault:
                log.warning("site-list", book.line(), path=path)
            return book

    @property
    def engine(self) -> Any:
        """The service default engine, built on first use.

        ``status`` and ``selftest`` therefore need no engine credential. A recording is
        transcribed by :meth:`engine_for`, which honours its route's override; this is what
        that falls back to and what a caller with no route in hand gets.
        """
        return self.engine_for(None)

    def engine_for(self, route: Route | None) -> Any:
        """The engine that transcribes this route: its override, or the service default.

        An engine passed to the constructor is a forced one — a test, the selftest, ``once
        --engine`` — and it is used for every route, because being told which engine to use
        outranks a configuration file.
        """
        if self._engine is not None:
            return self._engine
        name = (getattr(route, "engine", "") or "").strip().lower() or str(
            getattr(self.config, "engine", "") or ""
        ).strip().lower()
        found = self._engines.get(name)
        if found is not None:
            return found
        with self._build_lock:
            found = self._engines.get(name)
            if found is None:
                try:
                    found = build_engine(self.config, self.graph, route)
                except EngineConfigError as exc:
                    raise self._engine_fault(route, exc) from exc
                self._engines[name] = found
        return found

    def _engine_fault(self, route: Route | None, exc: Exception) -> Exception:
        """Whose fault an unusable engine is: this route's, or the whole service's.

        The service default being unusable stops the service — every recording would fail
        the same way and quarantining the backlog would bury the one fact anybody needs. One
        route's *override* being unusable is that route's problem, and it must not take the
        other routes down with it, so its recordings quarantine and the rest keep running.
        """
        override = (getattr(route, "engine", "") or "").strip().lower()
        default = str(getattr(self.config, "engine", "") or "").strip().lower()
        if override and override != default:
            return _RouteFault(
                f"the route {getattr(route, 'name', '?')!r} is set to transcribe with "
                f"{override!r} instead of the service default {default!r}, and that engine is "
                f"not usable: {exc}. Only this route is affected — every other route is still "
                f"running. Nothing was written, moved or deleted."
            )
        return PipelineFatal(f"the transcription engine is not usable: {exc}")

    @property
    def extractor(self) -> Any:
        if self._extractor is None:
            with self._build_lock:
                if self._extractor is None:
                    try:
                        self._extractor = Extractor.from_config(self.config)
                    except AnalysisConfigError as exc:
                        raise PipelineFatal(f"the analysis pass is not usable: {exc}") from exc
        return self._extractor

    @property
    def withheld(self) -> WithheldStore:
        """The held-passage store, opened on first use and never in ``off``.

        Opened at :attr:`transcriber.config.Config.held_store_path` — the path startup
        already validated is outside the work directory — rather than at the store's own
        default, so the path that was checked is the path that is written. A held passage is
        the only copy of that text outside the audio, and the work directory is cleared on a
        disk budget: a queue that empties itself when a disk fills is the one failure this
        gate may never have.
        """
        if self._withheld is None:
            with self._build_lock:
                if self._withheld is None:
                    path = self.gate.held_store or WithheldStore.path_beside(
                        str(getattr(self.config, "ledger_path", ":memory:") or ":memory:")
                    )
                    self._withheld = WithheldStore(path, scrub=getattr(self.config, "scrub", None))
        return self._withheld

    # -- the route -----------------------------------------------------------------

    def route_of(self, row: Row) -> Route:
        """The route this recording arrived on, from the configuration. Never a guess.

        Raises :class:`_RouteFault` when the row names a route the configuration no longer
        describes — he removed or renamed it while this file was in flight. There is no
        fallback on purpose: the alternative to a loud stop is writing a site meeting's
        transcript into whichever folder happened to be first, which nothing downstream
        would ever notice.
        """
        name = str(getattr(row, "route", "") or "").strip() or DEFAULT_ROUTE
        lookup = getattr(self.config, "route", None)
        found = lookup(name) if callable(lookup) else None
        if isinstance(found, Route):
            self._refuse_if_disputed(row, found)
            return found
        known = ", ".join(
            str(getattr(r, "name", "")) for r in (getattr(self.config, "routes", ()) or ())
        )
        raise _RouteFault(
            f"this recording arrived on the route {name!r}, and there is no route called "
            f"{name!r} in the configuration any more, so there is nowhere it may be written. "
            f"The routes that do exist are: {known or '(none)'}. Nothing has been written, "
            f"moved or deleted — the recording is where it was. Either put that route back "
            f"in ROUTES, or move this recording to one of the routes above, and it will be "
            f"picked up again."
        )

    def _refuse_if_disputed(self, row: Row, route: Route) -> None:
        """Stop a recording two routes have both claimed, rather than filing it and saying so.

        The disagreement was already being recorded, logged at error level, kept out of the
        archive and reported in the morning email. What it was not doing was stopping the
        publish: the row keeps the route it was **discovered** on, so the three files went
        to that route's output folder and its held passages to that route's reviewer, and
        the email said so afterwards.

        Afterwards is too late. When routes are kinds of recording — calls, site meetings —
        the wrong folder is untidy. When routes are people, the wrong folder is one person's
        conversation in another person's folder, and no amount of reporting takes it back
        out. So the recording waits for a person instead, which is what quarantine is for:
        nothing is written, nothing is moved, the audio is untouched, and every other route
        keeps running.

        This runs on the way to publishing, not at discovery, because the disagreement is
        only knowable on the *second* sighting. A recording already published before the
        second route ever saw it is past saving — that one the digest still reports, and
        that is now the only case where it has to.
        """
        try:
            because = self.ledger.disagreement_about(row.item_id)
        except Exception:  # noqa: BLE001 - a ledger that cannot answer must not stop a route
            log.warning("route-dispute-check", "could not check whether this recording's route "
                        "is disputed; continuing", item=row.item_id)
            return
        if not because:
            return
        raise _RouteFault(
            f"two routes have both claimed this recording, so which one it belongs to is "
            f"exactly what is in doubt: {because}. It stayed on {route.display}, and its "
            f"transcript would be written into that route's output folder and its held "
            f"passages sent to that route's reviewer — which is the wrong place if the "
            f"other route is the right one. Nothing has been written, moved or deleted. "
            f"Either the recording was moved between watched folders, or one route's "
            f"folder sits inside another's; sort that out and requeue it."
        )

    # -- entry point ---------------------------------------------------------------

    def process_one(self, item: Row | str) -> Outcome:
        """Walk one recording as far as it can go, and record where that was.

        Claims the row, holds the claim alive while the slow steps run, and releases it on
        every path out — including the ones that raise.
        """
        item_id = item.item_id if isinstance(item, Row) else str(item)
        started = self.clock()
        with item_context(item_id):
            row = self.ledger.get(item_id)
            if row is None:
                raise PipelineFatal(
                    f"{item_id} has no ledger row, so it cannot be processed; a recording is "
                    "discovered before it is worked on, and this one never was"
                )
            if row.state == State.DONE:
                return self._outcome(row, RESULT_ALREADY_FINISHED, "already done", started)
            if row.state in (State.QUARANTINED, State.SKIPPED_EMPTY):
                return self._outcome(
                    row, RESULT_ALREADY_FINISHED,
                    row.quarantine_reason or row.skipped_reason or f"already {row.state}", started,
                )

            waiting = self._retry_wait(row)
            if waiting > 0:
                return self._outcome(
                    row, RESULT_DEFERRED,
                    f"waiting {waiting:.0f}s more before the next attempt: "
                    f"{row.meta.get(_META_RETRY_REASON) or 'previous attempt failed'}",
                    started,
                )

            if not self.ledger.claim(row.item_id, self.lease_seconds, owner=self.owner):
                return self._outcome(row, RESULT_NOT_CLAIMED, "another worker holds it", started)

            row = self.ledger.get(item_id) or row
            keeper = _LeaseKeeper(self.ledger, item_id, self.owner, self.lease_seconds)
            keeper.start()
            try:
                return self._walk(row, started)
            except PipelineFatal:
                # Not this recording's fault. Give the claim straight back so that whatever
                # fixes the service finds the work where it left it.
                self.ledger.release(
                    item_id,
                    "the service stopped; this was not the recording's fault",
                    owner=self.owner,
                )
                raise
            except LedgerStateError as exc:
                # Losing a race is a benign, expected ending — another worker finished this
                # recording first and the ledger refused to move it backwards. Taking the
                # whole service down for it would turn the one thing leases exist to make
                # survivable into an outage.
                log.warning("state-refused", f"{item_id}: {exc}")
                return self._outcome(
                    self.ledger.get(item_id) or row, RESULT_NOT_CLAIMED,
                    f"another worker finished this recording first ({exc})", started,
                )
            except LedgerError as exc:
                raise PipelineFatal(f"the ledger refused a write for {item_id}: {exc}") from exc
            except BaseException as exc:
                return self._fail(row, exc, started)
            finally:
                keeper.stop()
                self._release_if_ours(item_id)

    # -- the walk ------------------------------------------------------------------

    def _walk(self, row: Row, started: float) -> Outcome:
        # Where this recording's outputs go is settled before a byte is downloaded. A row on
        # a route the configuration no longer has fails here, having cost nothing and having
        # touched nothing, rather than after the engine has been paid.
        route = self.route_of(row)
        log.info("processing", f"{row.name or row.item_id} from {row.state} on {route.display}",
                 state=row.state, attempts=row.attempts, route=route.name)

        parsed = naming.parse_source_name(row.name)
        if parsed.extension not in AUDIO_EXTENSIONS:
            return self._quarantine(
                row,
                f"{row.name!r} is not a recording this service knows how to read "
                f"(extension {parsed.extension or 'none'}). Nothing was transcribed and "
                f"nothing was moved or deleted; a person should say what it is.",
                started,
            )

        # --- 1. the completeness gate ---------------------------------------------
        item = self._gate(row, started)
        if isinstance(item, Outcome):
            return item

        graph_hash = _best_hash(item)
        self.ledger.set_fields(
            row.item_id,
            name=str(getattr(item, "name", "") or row.name),
            size=int(getattr(item, "size", 0) or 0),
            graph_hash=graph_hash,
            web_url=str(getattr(item, "web_url", "") or row.web_url or ""),
        )
        row = self.ledger.get(row.item_id) or row

        # --- 2. download and verify, then persist FETCHED --------------------------
        audio_path = self._fetch(row, item)
        row = self.ledger.get(row.item_id) or row

        # --- 3. is the audio itself intact? ---------------------------------------
        info = audio_probe.probe(audio_path)
        self.ledger.set_fields(
            row.item_id,
            duration_s=float(info.duration_s or 0.0),
            container=info.container,
            truncated=bool(info.truncated),
        )
        log.info("probed", f"{info.container}, {info.duration_s:.1f}s, probed by {info.probed_by}",
                 duration_s=round(float(info.duration_s or 0.0), 2), container=info.container,
                 truncated=info.truncated)
        if info.truncated:
            return self._quarantine(
                row,
                f"the audio is not a whole recording — {info.reason}. It was NOT transcribed: "
                f"a cut-off recording transcribes as a plausible fragment and is the exact "
                f"failure that goes unnoticed forever.",
                started,
            )

        # --- 4. transcribe, then persist TRANSCRIBED ------------------------------
        hints = self._hints(row, parsed, info)
        engine = self.engine_for(route)
        transcript = self._transcribe(row, audio_path, info, hints, engine)

        verdict = plausibility.assess(transcript, info)
        fields: dict[str, Any] = {
            "engine": transcript.engine or str(getattr(engine, "name", "") or ""),
            "language": transcript.language,
            "word_count": verdict.words,
        }
        # Everything the engines and the splitter wrote down about what they could *not*
        # check — "the engine returned no timestamps, so the assembled transcript could not
        # be checked", the per-piece word counts, a piece produced with its hints stripped —
        # lived only in the work directory, which _cleanup deletes the moment a recording
        # reaches DONE. So on the default engine every split recording carried a written
        # statement that its duration guard did not run, and success destroyed it.
        engine_meta = dict(transcript.engine_metadata or {})
        log.info("transcribed", verdict.reason, words=verdict.words, verdict=verdict.verdict,
                 wpm=verdict.wpm, engine=fields["engine"])

        if verdict.is_implausible:
            if engine_meta:
                fields["meta"] = _merge_meta(row.meta, {"engine": engine_meta})
            self.ledger.set_fields(row.item_id, **fields)
            return self._quarantine(row, verdict.reason, started)
        if verdict.is_silent:
            self.ledger.advance(
                row.item_id, State.SKIPPED_EMPTY, skipped_reason=verdict.reason, **fields
            )
            self._cleanup(row.item_id)
            return self._outcome(
                self.ledger.get(row.item_id) or row, RESULT_SKIPPED_EMPTY, verdict.reason, started
            )

        self.ledger.advance(
            row.item_id,
            State.TRANSCRIBED,
            meta=_merge_meta(row.meta, {"engine": engine_meta}) if engine_meta else row.meta,
            **fields,
        )
        row = self.ledger.get(row.item_id) or row

        # --- 5. the sensitivity gate reads the transcript -------------------------
        # Before the AI pass and on the text exactly as transcribed. A trivial recording
        # never reaches the strong model, so a gate that only read the model's answer would
        # never read a twelve-second "don't write this down" at all.
        report = self._assess(row, transcript)

        # --- 6. the AI pass, on the UNREDACTED transcript -------------------------
        # Its quote verification asks whether the model invented the words, and only the
        # text as transcribed can answer that. Masking first would fail every item quoting a
        # held passage and discard it — a redaction that destroys action items.
        extraction = self._analyse(row, transcript, hints)
        report = self._assess(row, transcript, extraction, standing=report)

        # --- 7. store the held words, then cut them out ---------------------------
        gate = self._withhold(row, route, transcript, extraction, report)

        # --- 7a. what to call it, if anything -------------------------------------
        # BEFORE the ANALYSED write, because the decision is stored on that write and a
        # decision made after it would be serialised nowhere — silently, with no error and
        # no symptom, and the stickiness that stops two attempts disagreeing would simply
        # not exist. Wrapped whole: the only thing this can do for a recording is give it a
        # better title, and nothing about a better title is worth a transcript.
        # The same notes the publish will carry, computed once. The probe MUST render the
        # bytes the record is actually handed: notes are rendered into the transcript, so a
        # probe without them scores a file that does not exist, and the one check that asks
        # the record instead of reasoning about it would be asking about the wrong file.
        publish_notes = _engine_notes(engine_meta) + gate.notes
        decision = self._name(row, parsed, gate, info, route, notes=publish_notes)

        # --- 8. persist ANALYSED --------------------------------------------------
        # The redacted analysis, deliberately: `_review_row` keeps an unverifiable quote in
        # the row so `transcriber status` can show a person what the model produced, and the
        # ledger is printed. A held passage must not reach it any more than it reaches a file.
        pinned_at, pinned_note = self._recorded_at(row, parsed)
        changes: dict[str, Any] = {
            "analysis": _analysis_note(gate.extraction),
            _META_NAMING: decision.as_meta(),
            _META_RECORDED_AT: pinned_at.isoformat(),
            _META_RECORDED_NOTE: pinned_note,
        }
        spent = tuple(getattr(gate.extraction, "spend", ()) or ())
        if spent:
            # Written even for a recording the reader never saw: the router ran, and a meter
            # that only counted the expensive half would undercount the cheap recordings,
            # which are the numerous ones.
            #
            # Stamped with its own date. The alternative is inferring the month from one of
            # the row's timestamp columns, and every one of them is wrong in some case a
            # month boundary makes visible: ``discovered_at`` puts a recording found on the
            # 31st and analysed on the 1st in the wrong month, ``updated_at`` moves whenever
            # anything touches the row, and ``done_at`` is unset until it finishes. The
            # spend happened at a moment; the record says which.
            changes[_META_SPEND] = {
                "at": utc_now_iso(),
                "calls": [x.to_dict() for x in spent],
            }
        if gate.note:
            # Only when the gate actually read the recording. An empty entry on every row in
            # ``off`` would read like a gate that ran and found nothing, which is a different
            # claim from a gate that was never asked.
            changes[_META_SENSITIVITY] = gate.note
        self.ledger.advance(row.item_id, State.ANALYSED, meta=_merge_meta(row.meta, changes))
        row = self.ledger.get(row.item_id) or row

        # --- 9. write the three files, confirm them, then persist DONE ------------
        result = self._publish(row, parsed, gate.transcript, gate.extraction, info, route,
                               notes=publish_notes,
                               held=gate.held,
                               display_name=decision.name if decision.applied else "")
        self.ledger.advance(
            row.item_id,
            State.DONE,
            transcript_name=result.names.get("transcript"),
            summary_name=result.names.get("summary"),
            actions_name=result.names.get("actions"),
            output_item_ids=result.item_ids,
        )
        self._cleanup(row.item_id)
        row = self.ledger.get(row.item_id) or row
        log.info("done", ", ".join(sorted(result.names.values())),
                 route=route.name, **result.names)
        return self._outcome(row, RESULT_DONE, "three files written and confirmed", started,
                             outputs=result.names)

    # -- steps ---------------------------------------------------------------------

    def _gate(self, row: Row, started: float) -> Any:
        """Is the upload finished? Never asks delta — it re-``GET``s the item."""
        ready, reason = completeness.is_upload_complete(
            self.graph, row.item_id, settle_seconds=self.settle_seconds, sleep=self.sleep
        )
        if ready:
            log.info("upload-complete", reason)
            return self.graph.get_item(row.item_id)

        first_seen = float(row.meta.get(_META_GATE_FIRST_SEEN) or 0.0) or self.clock()
        waited = self.clock() - first_seen
        if waited > self.incomplete_deadline_s:
            return self._quarantine(
                row,
                f"the upload never finished: after {waited / 3600.0:.1f} hours it is still "
                f"not complete ({reason}). Nothing was deleted; the recording is where it was.",
                started,
            )
        self._defer(row, self.settle_seconds, reason, gate_first_seen=first_seen)
        log.info("upload-incomplete", reason, waited_s=round(waited, 1))
        return self._outcome(row, RESULT_DEFERRED, reason, started)

    def _fetch(self, row: Row, item: Any) -> str:
        """Download to a stable path, verify against Graph's hash, persist FETCHED."""
        directory = self._item_dir(row.item_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, _safe_component(row.name) or "recording")
        expected_size = int(getattr(item, "size", 0) or 0)
        expected_hashes = getattr(item, "hashes", None) or item

        if row.content_hash and os.path.exists(path):
            reused = _sha256_file(path)
            if reused == row.content_hash:
                log.info("download-reused", f"{os.path.basename(path)} is already here and matches "
                                            f"the hash recorded for it", bytes=os.path.getsize(path))
                _remember_local(path, row.item_id)
                return path
            log.warning("download-stale", "the file in the work directory is not the one the "
                                          "ledger recorded; downloading it again")

        result = self.graph.download(
            row.item_id, path, download_url=str(getattr(item, "download_url", "") or ""),
            expected_size=expected_size or None,
        )
        ok, reason = completeness.verify_download(
            path, expected_hashes, expected_size=expected_size or None
        )
        if not ok:
            # A file that failed its hash is not evidence of anything. Remove it so the next
            # attempt cannot mistake it for a good download.
            _unlink(path)
            raise _DownloadNotVerified(reason)

        log.info("downloaded", reason, bytes=result.bytes_written, resumed=result.resumed)
        _remember_local(path, row.item_id)
        self.ledger.advance(
            row.item_id,
            State.FETCHED,
            content_hash=result.sha256,
            size=result.bytes_written,
            graph_hash=_best_hash(item),
        )
        return path

    def _transcribe(
        self, row: Row, path: str, info: AudioInfo, hints: Hints, engine: Any = None
    ) -> Transcript:
        """This route's engine, or the cached answer from a run that crashed after paying."""
        engine = self.engine if engine is None else engine
        cache = self._transcript_cache(row.item_id)
        cached = _load_transcript(cache, row.content_hash)
        if cached is not None:
            log.info("transcript-reused", f"{cached.word_count} words already transcribed by "
                                          f"{cached.engine} for these exact bytes",
                     words=cached.word_count, engine=cached.engine)
            return cached

        duration = float(info.duration_s or 0.0)
        transcript = run_engine(
            engine,
            path,
            hints,
            duration_s=duration if duration > 0 and audio_probe.duration_is_known(info) else None,
            work_dir=self._item_dir(row.item_id),
        )
        # Written before the state moves, so a crash between the two costs a re-read of a
        # file rather than a second bill from the engine.
        _save_transcript(cache, transcript, row.content_hash)
        return transcript

    def _analyse(self, row: Row, transcript: Transcript, hints: Hints) -> Extraction:
        if not transcript.text.strip():
            # Unreachable through the plausibility gate above, which sends an empty
            # transcript to SKIPPED_EMPTY or QUARANTINED. Asserted rather than assumed,
            # because the AI pass reads a blank transcript as a configuration fault.
            raise _ItemFault(
                "the transcript is empty at the analysis step, which the plausibility check "
                "should already have caught; nothing was written"
            )
        extraction = self.extractor.extract(transcript, hints, source_item_id=row.item_id)
        log.info(
            "analysed",
            f"{extraction.routing.label}: {len(extraction.proposals)} proposal(s), "
            f"{len(extraction.review)} rejected on quote verification",
            routing=extraction.routing.label,
            proposals=len(extraction.proposals),
            review=len(extraction.review),
            models=",".join(extraction.models_used),
        )
        return extraction

    # -- the sensitivity gate ------------------------------------------------------

    def _assess(
        self,
        row: Row,
        transcript: Transcript,
        extraction: Any = None,
        *,
        standing: Any = None,
    ) -> Any:
        """Which passages of this transcript must not be written down yet.

        Called twice for one recording, and the two calls are not a duplication.

        The first, before the AI pass, runs the mechanical rules on their own: an explicit
        instruction not to write something down, in any language, and a bare identifier that
        validates as one. Those need no model to agree with them, and they have to run on
        every recording — including the short ones the router never sends to the strong
        model, which is exactly where "don't put that in writing" gets said.

        The second folds in the model's own reading, which arrives inside the analysis
        answer. When the analysis carried none — the recording was trivial, or the analysis
        pass is not asking the question yet — the first answer stands rather than being
        replaced by a poorer one; ``standing`` is that answer.

        Offsets are into the transcript exactly as the engine returned it. Nothing has
        rewritten it at this point and nothing may: :mod:`transcriber.redact` cuts on these
        numbers.
        """
        if not self.gate.classifies:
            return sensitivity.assess("", None, settings=self.gate)

        data = None if extraction is None else getattr(extraction, "sensitive_passages", None)
        if extraction is not None and data is None:
            # Asked and not answered. The rules pass stands; the report says so out loud in
            # its own notes, which reach the file a person reads when the gate is armed.
            return standing if standing is not None else sensitivity.assess(
                transcript.text or "", None, settings=self.gate
            )
        return sensitivity.assess(transcript.text or "", data, settings=self.gate)

    def _withhold(
        self,
        row: Row,
        route: Route,
        transcript: Transcript,
        extraction: Any,
        report: Any,
    ) -> GateResult:
        """Store the held words, cut them out, and mask everything derived from them.

        One code path for all three modes, with :attr:`GateSettings.mode` carried into it
        and deciding one thing only: whether anything is actually cut. That is what makes
        ``shadow`` worth running — it measures the real classifier on real recordings
        through the same code ``on`` will use, and changes nothing about what is written.
        Every ``if`` below that reads the mode is either "does the store call it a hold or a
        would-have-held" or "is anything cut", and there is no third kind.

        Two orderings are structural. **The store is written before the transcript is cut**,
        because after the cut the words exist only in the audio and in that database, and a
        store that will not take them means nothing is cut and nothing is published.
        **Everything derived from the transcript is masked from the same redaction**, so an
        item's quote comes out as the same words either side of the same marker — still a
        literal substring of the published transcript, so the render-time quote check passes
        because it is true rather than because it was loosened.
        """
        if not self.gate.classifies:
            return GateResult(report=report, transcript=transcript, extraction=extraction)

        text = transcript.text or ""
        spans = held_spans_from(
            report.would_hold(),
            item_id=row.item_id,
            route=route.name,
            transcript=text,
            site=str(getattr(extraction, "site", "") or ""),
            source_name=row.name,
            recorded_at=str(row.created_at or ""),
            recorded_by=self.gate.reviewer_for(route.name),
            principal=self._service_owner(),
        )
        measured = self._record_pass(row, route, spans, report, extraction)

        if spans:
            self._hold(spans)

        cut, redaction, problems = redact.redact_transcript(
            transcript, spans, mode=self.gate.mode, held_on=utc_now_iso()
        )
        if problems:
            # Neither withheld silently nor published silently. A span the redactor could
            # not find is the one case where it cannot say what the file contains, so the
            # recording goes to a person with the reason in plain words and nothing is
            # uploaded. Not retried: the same transcript reaches the same answer.
            raise _ItemFault(
                "this recording has passages that must not be written down yet, and they "
                "could not all be taken out of the transcript, so none of its three files "
                "has been written: " + "; ".join(problems)
            )
        masked, outcomes = redact.redact_extraction(extraction, redaction)

        held = redaction.cut_spans
        notes = tuple(report.notes) if redaction.armed else ()
        if held and not str(getattr(extraction, "site", "") or "").strip():
            # The record files a transcript's questions against a site it recognises, and a
            # recording that names none files none — so this recording's holds would be
            # marked in the transcript and invisible on every live page. Said out loud in
            # the file rather than left to be discovered by an assistant answering a client
            # from a record it does not know is partial.
            notes = notes + (
                "this recording had passages held for review and names no site the record "
                "will recognise, so the marks in the transcript are the only place they are "
                "visible — a person should say which site it belongs to",
            )
        note = _sensitivity_note(report, redaction, spans, measured=measured)
        log.info(
            "sensitivity",
            report.describe()
            + (f", {len(held)} cut out of the transcript" if held else "")
            + (
                f", {sum(1 for o in outcomes if o.action == 'held')} proposal(s) held with them"
                if any(o.action == "held" for o in outcomes)
                else ""
            ),
            mode=self.gate.mode,
            would_hold=len(report.would_hold()),
            cut=len(held),
            route=route.name,
        )
        return GateResult(
            report=report,
            transcript=cut,
            extraction=masked,
            held=held,
            notes=notes,
            note=note,
            measured=measured,
        )

    def _hold(self, spans: Sequence[HeldSpan]) -> None:
        """Put the words in the store, before anything cuts them out of the transcript.

        All of them in one transaction: a store holding four of five spans is a service that
        has taken words out of the record with no way for anybody to ask for them back.

        A store that will not take them ends the run **when the gate is armed** — nothing is
        cut, nothing is published, the recording retries and then goes to a person, and the
        transcript is still whole in the work directory and still whole in OneDrive.

        When it is not armed it is loud and survivable, and that asymmetry is the point.
        ``shadow`` cuts nothing, so a store it cannot write to costs a row of measurement and
        risks nothing at all — and a measurement that can stop a recording reaching the
        record is a measurement somebody switches off in the first bad week, which leaves the
        gate to be armed against an estimate. That is the failure this whole mode exists to
        prevent, so it may not be caused by it.
        """
        try:
            self.withheld.hold_many(spans, mode=self.gate.mode)
        except Exception as exc:  # noqa: BLE001 - re-raised above when it actually matters
            if self.gate.withholds:
                raise
            log.error(
                "gate-store-unavailable",
                f"the sensitivity gate is measuring in {self.gate.mode} mode and could not "
                f"record {len(spans)} passage(s) it would have held, so they are missing "
                f"from the measurement the decision to arm the gate rests on. Nothing was "
                f"withheld and nothing was lost from the record: {_plain(exc)}",
                mode=self.gate.mode,
                would_hold=len(spans),
            )

    def _record_pass(
        self, row: Row, route: Route, spans: Sequence[HeldSpan], report: Any, extraction: Any
    ) -> bool:
        """One row per recording the classifier read, held or not. The denominator.

        "Eleven passages held this week" answers nothing. "Eleven passages across 214
        recordings, 0.4% of the text, eight of them one category" is the number that decides
        whether the gate can be armed at all, and it is the whole reason it ships dark.

        Written once per recording rather than once per attempt: a recording that retried
        three times would otherwise contribute its characters three times and quietly
        understate the fraction that gets held. Recorded before the hold and before the cut,
        because a recording that was classified and then failed to publish was still
        classified, and dropping it would flatter the number.

        A store that cannot be written is fatal to a run that is withholding — the words are
        about to be cut — and is loud but survivable to one that is not. Shadow must never
        be able to stop a recording reaching the record: a measurement that can break the
        service is a measurement nobody leaves switched on.
        """
        if (row.meta or {}).get(_META_SENSITIVITY):
            return False
        classifier = (
            ",".join(str(m) for m in (getattr(extraction, "models_used", ()) or ()))
            if getattr(report, "model_answered", False)
            else "rules"
        )
        try:
            self.withheld.record_pass(
                row.item_id,
                route=route.name,
                mode=self.gate.mode,
                spans=spans,
                transcript_chars=int(getattr(report, "transcript_chars", 0) or 0),
                classifier=classifier,
                # In shadow nothing is cut, so no file carries these and the ledger row's
                # meta is the only other place they exist — which nobody opens at 06:00.
                # Stored here, they reach the morning email in every mode.
                notes=tuple(getattr(report, "notes", ()) or ()),
            )
            return True
        except Exception as exc:  # noqa: BLE001 - re-raised below when it actually matters
            if self.gate.withholds:
                raise
            log.error(
                "gate-store-unavailable",
                f"the sensitivity gate is measuring in {self.gate.mode} mode and could not "
                f"write to its store, so this recording is missing from the measurement the "
                f"decision to arm the gate rests on. Nothing was withheld and nothing was "
                f"lost from the record: {_plain(exc)}",
                mode=self.gate.mode,
            )
            return False

    def _service_owner(self) -> str:
        """Who a staff disciplinary matter is held for, whoever recorded the call.

        A staff member reviews their own held passages — he sees the count and the site,
        never the words — and the one exception he named is a staff matter, which is
        genuinely his. That needs a name for "him", and the one this service already has is
        the address the morning email escalates the queue to. Never logged and never
        printed, on the same footing as every other address here.

        ``SMTP_TO`` is required at startup, so a running service always has one. A
        hand-built configuration without one — a test, the selftest — leaves this empty, and
        :func:`transcriber.withheld.reviewer_for` then falls back to whoever recorded the
        call for a staff matter too. That is stated rather than papered over: it is the one
        case where the routing is weaker than decision 6, and it cannot arise in a
        deployment that started.
        """
        recipients = tuple(getattr(self.config, "smtp_to", ()) or ())
        return str(recipients[0]).strip() if recipients else ""

    def _name(
        self,
        row: Row,
        parsed: naming.ParsedName,
        gate: Any,
        info: AudioInfo,
        route: Route,
        *,
        notes: tuple[str, ...] = (),
    ) -> autoname.NameDecision:
        """What to call this recording, or nothing. **Never raises, never delays.**

        Every failure — the site list missing, the renderer throwing, an unreadable stored
        decision, anything at all — comes back as "no name", which is the behaviour before
        this existed: the file keeps the voice recorder's own name and the transcript is
        written on time. Nothing here can hold a recording up, because the whole feature is
        worth less than one recording.

        A decision already on the row is reused rather than made again. A publish that
        failed halfway and is retried the next morning must write the same subject line, or
        the record ends up holding two documents for one recording with no way to tell they
        are the same.
        """
        # The stored answer FIRST, above the switch. A recording whose transcript already
        # reached OneDrive under a worked-out subject line, and whose publish then half
        # failed, must republish under that same subject line — even if he saw a title he
        # did not like at 06:00 and switched naming off while it was still in flight. The
        # record keys a document on its bytes, so republishing the same recording under a
        # different subject is a second document, and overwriting the stored decision would
        # destroy the only evidence that the first one was ever published.
        stored = autoname.NameDecision.from_meta(row.meta.get(_META_NAMING))
        if stored is not None:
            return stored

        if not bool(getattr(self.config, "naming", False)):
            return autoname.NO_NAME

        try:
            probe = self._context(row, parsed, gate.transcript, gate.extraction, info,
                                  notes=tuple(notes), held=tuple(gate.held))
            return autoname.decide(
                parsed=parsed,
                extraction=gate.extraction,
                spoken=outputs.spoken_body(gate.transcript),
                duration_s=probe.duration_s,
                book=self.site_book,
                render=lambda name: outputs.render_transcript(
                    replace(probe, display_name=name)
                ),
                apply=bool(getattr(self.config, "naming_apply", False)),
                min_seconds=int(getattr(self.config, "naming_min_seconds", 120) or 120),
                # The moment pinned on the row, so the date in the title is the same one the
                # output filenames open with and cannot drift from it between attempts.
                recorded_at=probe.recorded_at,
                opening_seconds=float(
                    getattr(self.config, "naming_opening_seconds", 60) or 60
                ),
            )
        except Exception as exc:
            log.warning(
                "naming",
                "could not work out what to call this recording; publishing it under the "
                "name it arrived with",
                item=row.item_id, route=route.name, error=str(exc),
            )
            return autoname.NameDecision(
                decided=True, code="error",
                why="something went wrong working out a name for it",
            )

    def _recorded_at(self, row: Row, parsed: naming.ParsedName) -> tuple[datetime, str]:
        """When the recording was made — the row's own answer once it has one.

        Worked out once and pinned, because the output filenames are built from it. See
        :data:`_META_RECORDED_AT` for what a moment that moves between attempts costs.
        """
        stored = str(row.meta.get(_META_RECORDED_AT) or "")
        if stored:
            try:
                return (
                    datetime.fromisoformat(stored),
                    str(row.meta.get(_META_RECORDED_NOTE) or "") or "read from the filename",
                )
            except (TypeError, ValueError):
                # Unreadable rather than absent. Fall through and work it out again: a
                # wrong-looking prefix is recoverable, a crash in the publish path is not.
                log.warning("recorded-at", "the stored moment could not be read; working it "
                            "out again", item=row.item_id, stored=stored)
        return naming.resolve_timestamp(parsed, row.created_at)

    def _context(
        self,
        row: Row,
        parsed: naming.ParsedName,
        transcript: Transcript,
        extraction: Extraction,
        info: AudioInfo,
        *,
        notes: tuple[str, ...] = (),
        held: Sequence[HeldSpan] = (),
        display_name: str = "",
    ) -> outputs.OutputContext:
        """Everything the three files are rendered from.

        Lifted out of :meth:`_publish` so that the naming step can render the very same
        files a few lines earlier to check its own answer, without a second code path that
        could drift from the one that actually publishes. ``resolve_timestamp`` is pure, so
        building this twice costs nothing.
        """
        recorded_at, note = self._recorded_at(row, parsed)
        return outputs.OutputContext(
            item_id=row.item_id,
            source_name=row.name,
            parsed=parsed,
            recorded_at=recorded_at,
            timestamp_source=note,
            transcript=transcript,
            extraction=extraction,
            audio=info,
            content_hash=row.content_hash or "",
            graph_hash=row.graph_hash or "",
            web_url=row.web_url or "",
            engine=row.engine or transcript.engine or "",
            notes=tuple(notes),
            # What was cut out of this transcript, so the renderer can refuse to publish any
            # of the three files if one of those passages survived into one of them. Empty
            # in every mode but ``on``: nothing was cut, so nothing can have leaked.
            held=tuple(held),
            display_name=display_name,
        )

    def _publish(
        self,
        row: Row,
        parsed: naming.ParsedName,
        transcript: Transcript,
        extraction: Extraction,
        info: AudioInfo,
        route: Route,
        *,
        notes: tuple[str, ...] = (),
        held: Sequence[HeldSpan] = (),
        display_name: str = "",
    ) -> outputs.UploadResult:
        # The route's folder, and only ever the route's folder. Not a service-wide default,
        # not the first route's, not the folder the recording came from. Asked before
        # anything is rendered, so a route that cannot say where its outputs go costs
        # nothing and touches nothing.
        parent = str(route.output_folder_id or "")
        if not parent:
            raise _RouteFault(
                f"the route {route.name!r} ({route.display}) has no output folder, so there "
                f"is nowhere to write this recording's three files. Set "
                f"{route.env_var('OUTPUT')} in the .env — or move this recording to a route "
                f"that has one. Nothing was written and nothing was moved."
            )
        ctx = self._context(row, parsed, transcript, extraction, info,
                            notes=tuple(notes), held=tuple(held),
                            display_name=display_name)
        self._refuse_name_collision(row, ctx)
        log.info("publishing", f"three files into the {route.display} output folder",
                 route=route.name, parent=parent)
        return outputs.publish(
            self.graph,
            parent,
            ctx,
            verify_bytes=self.verify_uploaded_bytes,
            # NOT the archive folder. Its contract is aged original recordings that nothing
            # ever deletes and nothing ever scans, so a stray .md dropped there sits among
            # the audio forever. Left unset, a stray is named in the error and replaced by
            # name on the next attempt, which is stale for one cycle rather than for good.
            orphan_folder_id=str(getattr(self.config, "orphan_folder_id", "") or ""),
            work_dir=self._item_dir(row.item_id),
        )

    def _refuse_name_collision(self, row: Row, ctx: outputs.OutputContext) -> None:
        """Never write over another recording's output. Quarantine loudly instead.

        An upload replaces by name and hands back the same driveItem id, so a collision is
        invisible to the read-back, to the sweep and to the archive: both rows say DONE and
        one recording's transcript no longer exists anywhere. The names now carry the item
        id, so this can only fire on a genuine bug — which is exactly when it is wanted.
        """
        for name in ctx.names.as_tuple():
            other = self.ledger.owner_of_output_name(name)
            if other and other != row.item_id:
                raise _ItemFault(
                    f"{name!r} is already recorded as an output of {other!r}. Writing it "
                    f"would replace that recording's file in OneDrive and leave both rows "
                    f"claiming to be finished. Nothing was uploaded."
                )

    # -- hints ---------------------------------------------------------------------

    def _hints(self, row: Row, parsed: naming.ParsedName, info: AudioInfo) -> Hints:
        languages = tuple(getattr(self.config, "languages", ()) or ())
        return Hints(
            vocabulary=tuple(getattr(self.config, "vocabulary", ()) or ()),
            counterparty=parsed.party,
            language=languages[0] if languages else None,
            languages=languages,
            recorded_at=row.created_at,
            source_name=row.name,
            duration_s=float(info.duration_s or 0.0) or None,
        )

    # -- endings -------------------------------------------------------------------

    def _fail(self, row: Row, exc: BaseException, started: float) -> Outcome:
        """The only failure path: retry with backoff, or quarantine loudly."""
        if isinstance(exc, _NOT_THE_RECORDINGS_FAULT):
            return self._interrupted(row, exc, started)
        retryable, reason = _classify(exc)
        attempts = self.ledger.record_attempt(row.item_id, reason, owner=self.owner)
        current = self.ledger.get(row.item_id) or row

        if not retryable or attempts >= self.max_attempts:
            why = (
                f"{reason} (attempt {attempts} of {self.max_attempts}; not retried because "
                f"repeating it would not change the answer)"
                if not retryable
                else f"{reason} (gave up after {attempts} attempt(s))"
            )
            log.error("quarantined", why, attempts=attempts, exc_info=exc)
            self.ledger.quarantine(row.item_id, why, owner=self.owner)
            return self._outcome(
                self.ledger.get(row.item_id) or current, RESULT_QUARANTINED, why, started,
                attempts=attempts,
            )

        delay = self._backoff(attempts)
        self._defer(current, delay, reason)
        log.warning("retrying", f"{reason}; next attempt in {delay:.0f}s",
                    attempts=attempts, retry_in_s=round(delay), exc_info=exc)
        return self._outcome(current, RESULT_RETRY, reason, started, attempts=attempts)

    def _interrupted(self, row: Row, exc: BaseException, started: float) -> Outcome:
        """An ending that costs the recording nothing: no attempt, no backoff, no quarantine.

        The service stopping while a recording waited its turn at the engine rate limit is a
        fault in nothing at all — the recording was never started. Counted as a failed
        attempt it was three redeploys from being quarantined, and a quarantine needs a
        person, so restarting the service during a backlog quietly took recordings out of
        the queue. With ``CONCURRENCY`` at 8 and ``ENGINE_MAX_CONCURRENT`` at 3, five
        recordings are queued at the limiter at any moment under load, so one stop charged
        five of them at once. So: the claim goes straight back, the attempt count does not
        move, and the row is claimable again the moment the service is up.
        """
        reason = (
            f"the service stopped before this recording was started, so it has not been "
            f"tried and nothing has been counted against it — it is still queued and the "
            f"next run picks it up ({_plain(exc)})"
        )
        log.info("interrupted", reason, attempts=row.attempts)
        try:
            self.ledger.release(row.item_id, reason, owner=self.owner)
        except LedgerError as exc_release:
            raise PipelineFatal(
                f"the ledger refused to release {row.item_id}: {exc_release}"
            ) from exc_release
        return self._outcome(
            self.ledger.get(row.item_id) or row, RESULT_RETRY, reason, started,
            attempts=row.attempts,
        )

    def _quarantine(self, row: Row, reason: str, started: float) -> Outcome:
        log.error("quarantined", reason, state=row.state)
        self.ledger.quarantine(row.item_id, reason, owner=self.owner)
        return self._outcome(self.ledger.get(row.item_id) or row, RESULT_QUARANTINED, reason, started)

    def _defer(self, row: Row, seconds: float, reason: str, **extra: Any) -> None:
        """Come back to this one later, with the reason visible in the meantime."""
        meta = _merge_meta(
            row.meta,
            {_META_RETRY_AT: self.clock() + max(0.0, seconds), _META_RETRY_REASON: reason, **extra},
        )
        self.ledger.set_fields(row.item_id, meta=meta)
        self.ledger.release(row.item_id, reason, owner=self.owner)

    def _retry_wait(self, row: Row) -> float:
        return max(0.0, float(row.meta.get(_META_RETRY_AT) or 0.0) - self.clock())

    def _backoff(self, attempts: int) -> float:
        span = min(RETRY_MAX_S, RETRY_BASE_S * (2 ** max(0, attempts - 1)))
        return self._rng.uniform(span / 2.0, span)

    def _release_if_ours(self, item_id: str) -> None:
        row = self.ledger.get(item_id)
        if row is not None and row.claimed_by == self.owner and not row.is_terminal:
            self.ledger.release(item_id, "the worker finished this pass", owner=self.owner)

    def _outcome(
        self, row: Row, result: str, reason: str, started: float, **extra: Any
    ) -> Outcome:
        return Outcome(
            item_id=row.item_id,
            name=row.name,
            result=result,
            state=row.state,
            reason=reason,
            route=str(getattr(row, "route", "") or ""),
            attempts=extra.pop("attempts", row.attempts),
            elapsed_s=round(self.clock() - started, 3),
            outputs=extra.pop("outputs", {}),
        )

    # -- the work directory --------------------------------------------------------

    def _item_dir(self, item_id: str) -> str:
        return os.path.join(self.work_dir, "items", _safe_component(item_id) or "unnamed")

    def _transcript_cache(self, item_id: str) -> str:
        return os.path.join(self._item_dir(item_id), "transcript.json")

    def _cleanup(self, item_id: str) -> None:
        """Scratch is removed only once the recording is finished, never before.

        A failure keeps its downloaded audio and its transcript: they are what makes the next
        attempt cheap, and what a person looks at when the failure is not obvious.
        """
        if self.keep_work_files:
            return
        shutil.rmtree(self._item_dir(item_id), ignore_errors=True)


def process_one(
    item: Row | str,
    config: Any,
    ledger: Ledger,
    graph: Any,
    **kwargs: Any,
) -> Outcome:
    """One recording, one call. Builds a :class:`Pipeline` for a caller that has no loop."""
    return Pipeline(config, ledger, graph, **kwargs).process_one(item)


# --------------------------------------------------------------------------- lease renewal


class _LeaseKeeper:
    """Keeps a claim alive while a long step runs.

    A forty-minute recording can outlast the lease inside a single engine call. Without this
    the lease expires mid-transcription, another worker claims the same file, and the work is
    done twice — not a loss, but a bill and a race for nothing.
    """

    def __init__(self, ledger: Ledger, item_id: str, owner: str, lease_seconds: int) -> None:
        self._ledger = ledger
        self._item_id = item_id
        self._owner = owner
        self._lease = max(30, int(lease_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self._item_id[:12]}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        interval = max(10.0, self._lease / 3.0)
        while not self._stop.wait(interval):
            try:
                if not self._ledger.renew(self._item_id, self._lease, self._owner):
                    log.warning(
                        "lease-lost",
                        "the claim on this recording expired and somebody else may hold it; "
                        "this pass will finish but its writes may be redundant",
                    )
                    return
            except LedgerError as exc:  # the walk will hit the same wall and report it
                log.warning("lease-renewal-failed", str(exc))
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


# --------------------------------------------------------------------------- classification


class _ItemFault(RuntimeError):
    """A fault in this recording that will not come right by being retried."""


class _RouteFault(_ItemFault):
    """This recording's route cannot say where its outputs go.

    Deliberately not fatal. A route he removed or renamed while a file was mid-flight is one
    route's problem: the recording is quarantined with the route named, in words, and every
    other route keeps running. Deliberately not retried either — the configuration will not
    change by being asked again, and a person has to decide where that recording belongs.
    """


class _DownloadNotVerified(RuntimeError):
    """The bytes on disk are not the bytes Graph described. Worth one more try."""


#: Faults in the recording itself: retrying performs the same work and reaches the same
#: answer, so the recording goes to a person now rather than after three identical failures.
_NEVER_RETRY = (
    _ItemFault,
    SplitDurationError,
    SplitUnsupported,
    # Named although ``OutputContractError`` below already covers it, because it is the one
    # entry here whose meaning is "a held passage nearly reached the record". Retrying
    # renders the same bytes and reaches the same answer, and a person has to look at it.
    HeldTextWouldLeak,
    OutputContractError,
    TimestampUnavailable,
    TranscriptTooLarge,
    EngineAudioTooLarge,
)

#: Endings that are not about this recording at all, and cost it nothing: no attempt, no
#: backoff, no quarantine. The service stopping while a recording was still queued behind
#: the engine rate limit is the whole of this list — it was never started, so there is
#: nothing to have failed. Deliberately NOT ``PipelineFatal``: an ordinary redeploy is not a
#: service fault, and routing it that way would exit the worker non-zero and ping the
#: heartbeat's failure endpoint every time somebody restarted the service.
_NOT_THE_RECORDINGS_FAULT = (RateLimitShutdown,)

#: Faults in the service: a credential, a configuration, or the durable state. These stop the
#: worker rather than consuming the backlog.
_FATAL = (
    GraphAuthError,
    GraphConfigError,
    EngineAuthError,
    EngineConfigError,
    AnalysisAuthError,
    AnalysisConfigError,
    LedgerError,
)


def _classify(exc: BaseException) -> tuple[bool, str]:
    """(retryable, reason in plain words). Raises :class:`PipelineFatal` for a service fault."""
    if isinstance(exc, PipelineFatal):
        raise exc
    if isinstance(exc, _FATAL):
        raise PipelineFatal(f"{type(exc).__name__}: {exc}") from exc
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc

    if isinstance(exc, _NEVER_RETRY):
        return False, f"{_plain(exc)}"

    if isinstance(exc, GraphHTTPError):
        status = int(getattr(exc, "status", 0) or 0)
        if status == 404:
            return False, (
                "the recording is no longer in the drive, so there is nothing to process — "
                "it was moved or deleted while it was queued"
            )
        if status and status < 500 and status not in (408, 429):
            return False, f"Microsoft Graph refused the request with HTTP {status}: {_plain(exc)}"
        return True, f"Microsoft Graph answered HTTP {status or '?'}: {_plain(exc)}"

    if isinstance(exc, UploadIncompleteError):
        missing = ", ".join(getattr(exc, "missing", ()) or ()) or "none named"
        return True, (
            f"the three output files did not all land, so nothing was marked done "
            f"(missing: {missing}) — {_plain(exc)}"
        )
    if isinstance(exc, (IncompleteDownload, _DownloadNotVerified)):
        return True, _plain(exc)

    # Anything unrecognised is retried and then quarantined. Both endings are visible, and
    # neither one is "assume it worked".
    return True, f"{type(exc).__name__}: {_plain(exc)}"


def _plain(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return " ".join(text.split())


# --------------------------------------------------------------------------- small helpers


def _best_hash(item: Any) -> str:
    hashes = getattr(item, "hashes", None)
    if isinstance(hashes, dict) and hashes:
        for key in ("sha256Hash", "quickXorHash", "sha1Hash"):
            if hashes.get(key):
                return str(hashes[key])
        return str(next(iter(hashes.values())))
    return str(getattr(item, "best_hash", "") or "")


def _safe_component(value: str) -> str:
    return _UNSAFE_PATH.sub("_", os.path.basename(str(value or "")).strip())[:180]


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _merge_meta(existing: Mapping[str, Any] | None, changes: Mapping[str, Any]) -> dict[str, Any]:
    """The ledger stores meta as one JSON blob, so a partial write has to merge by hand."""
    merged = dict(existing or {})
    merged.update(changes)
    return merged


#: How many review items are kept in the row. The summary and actions files tell a person
#: that withheld items "were kept on the review list against this recording"; without this
#: the only thing that survived the run was the integer, and the file promised a list that
#: did not exist anywhere.
_MAX_REVIEW_KEPT = 25


def _analysis_note(extraction: Any) -> dict[str, Any]:
    routing = getattr(extraction, "routing", None)
    review = list(getattr(extraction, "review", ()) or ())
    return {
        "at": utc_now_iso(),
        "routing": getattr(routing, "label", ""),
        "escalated": bool(getattr(routing, "escalated", False)),
        "proposals": len(getattr(extraction, "proposals", ()) or ()),
        "review": len(review),
        "review_items": [_review_row(item) for item in review[:_MAX_REVIEW_KEPT]],
        "notes": [str(n) for n in (getattr(extraction, "notes", ()) or ())],
        "models": list(getattr(extraction, "models_used", ()) or ()),
        "observed_by": "agent",
    }


def _sensitivity_note(
    report: Any, redaction: Any, spans: Sequence[HeldSpan], *, measured: bool
) -> dict[str, Any]:
    """The gate's own line in the ledger row: counts, categories, references. No words.

    ``transcriber status`` prints this row and a person copies it into an email, so nothing
    here may be a word of what was held. The references are safe by construction — they are
    printed in the published transcript beside the marker, which is how somebody asks for a
    passage back.
    """
    return {
        "at": utc_now_iso(),
        "mode": str(getattr(report, "mode", "") or ""),
        "model_answered": bool(getattr(report, "model_answered", False)),
        "would_hold": len(report.would_hold()),
        "labelled": len(report.labelled()),
        "categories": report.counts(),
        "held_chars": int(getattr(report, "held_chars", 0) or 0),
        "transcript_chars": int(getattr(report, "transcript_chars", 0) or 0),
        "cut": len(getattr(redaction, "applied", ()) or ()),
        "refs": [span.ref for span in spans],
        "counted_in_the_measurement": bool(measured),
        "notes": [str(n) for n in (getattr(report, "notes", ()) or ())],
        "observed_by": "agent",
    }


def _review_row(item: Any) -> dict[str, Any]:
    """One withheld item, in the shape ``transcriber status`` prints.

    The unverifiable quote is kept deliberately: it is the evidence that the model produced
    words the recording does not contain, which is the whole reason a person is being shown
    the item at all. It never reaches an output file.
    """
    ratio = getattr(item, "ratio", None)
    return {
        "category": str(getattr(item, "category", "") or ""),
        "text": str(getattr(item, "text", "") or "")[:400],
        "quote": str(getattr(item, "quote", "") or "")[:400],
        "why": str(getattr(item, "reason", "") or "")[:200],
        "closest_match": round(float(ratio), 3) if isinstance(ratio, (int, float)) else None,
    }


def _engine_notes(engine_meta: Mapping[str, Any]) -> tuple[str, ...]:
    """The two facts from the engine a person reading the file needs in plain words.

    An unverifiable guard has to be visible in the artefact itself, not only in a JSON blob
    somebody would have to know to look for. A short transcript that was silently split and
    never duration-checked is otherwise indistinguishable from a short conversation.
    """
    notes: list[str] = []
    split = dict((engine_meta or {}).get("split") or {})
    guard = str(split.get("duration_guard") or "").strip()
    if guard:
        pieces = split.get("pieces") or split.get("piece_word_counts") or ()
        count = len(pieces) if isinstance(pieces, (list, tuple)) else 0
        notes.append(
            "This recording was too large for the transcription engine and was split into "
            + (f"{count} pieces" if count else "several pieces")
            + f". {guard}. The pieces were checked against the recording's length before "
            "transcription, and each piece came back with words in it."
        )
    if (engine_meta or {}).get("degraded"):
        notes.append(
            "The transcription ran with some of its settings stripped after the engine "
            "refused them, so this transcript may be less accurate than usual."
        )
    return tuple(notes)


# -- the transcript cache ----------------------------------------------------------


def _save_transcript(path: str, transcript: Transcript, content_hash: str | None) -> None:
    """Keyed to the audio's hash: a cached transcript is only ever re-used for the same bytes."""
    payload = {
        "content_hash": content_hash or "",
        "engine": transcript.engine,
        "language": transcript.language,
        "duration_s": transcript.duration_s,
        "text": transcript.text,
        "engine_metadata": transcript.engine_metadata,
        "segments": [
            {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
            for s in transcript.segments
        ],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = path + ".part"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _load_transcript(path: str, content_hash: str | None) -> Transcript | None:
    if not content_hash or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("content_hash") != content_hash:
        return None
    text = str(payload.get("text") or "")
    if not text:
        return None
    return Transcript(
        text=text,
        segments=[
            Segment(
                start=float(s.get("start") or 0.0),
                end=float(s.get("end") or 0.0),
                speaker=s.get("speaker"),
                text=str(s.get("text") or ""),
            )
            for s in payload.get("segments") or []
            if isinstance(s, dict)
        ],
        language=payload.get("language"),
        engine_metadata=dict(payload.get("engine_metadata") or {}),
        engine=str(payload.get("engine") or ""),
        duration_s=payload.get("duration_s"),
    )
