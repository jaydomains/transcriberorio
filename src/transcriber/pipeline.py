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
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import audio as audio_probe
from . import completeness, naming, outputs, plausibility
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
from .models import AUDIO_EXTENSIONS, AudioInfo, Hints, Row, Segment, State, Transcript, utc_now_iso
from .naming import TimestampUnavailable
from .outputs import OutputContractError, UploadIncompleteError

__all__ = [
    "Pipeline",
    "Outcome",
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


def build_engine(config: Any, graph: Any = None) -> Any:
    """The configured transcription engine, wired for Graph if it needs a content URL.

    Azure's batch API fetches the audio itself and never receives an upload, so it needs a
    URL rather than a path. Only the pipeline knows the Graph item id behind a local file, so
    the mapping is supplied here.
    """
    engine = create_engine(config)
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
        self._extractor = extractor
        self._build_lock = threading.Lock()
        self._rng = random.Random()

    # -- lazily built collaborators ------------------------------------------------

    @property
    def engine(self) -> Any:
        """Built on first use so ``status`` and ``selftest`` need no engine credential."""
        if self._engine is None:
            with self._build_lock:
                if self._engine is None:
                    try:
                        self._engine = build_engine(self.config, self.graph)
                    except EngineConfigError as exc:
                        raise PipelineFatal(f"the transcription engine is not usable: {exc}") from exc
        return self._engine

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
        log.info("processing", f"{row.name or row.item_id} from {row.state}",
                 state=row.state, attempts=row.attempts)

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
        transcript = self._transcribe(row, audio_path, info, hints)

        verdict = plausibility.assess(transcript, info)
        fields: dict[str, Any] = {
            "engine": transcript.engine or str(getattr(self.engine, "name", "") or ""),
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

        # --- 5. the AI pass, then persist ANALYSED --------------------------------
        extraction = self._analyse(row, transcript, hints)
        self.ledger.advance(
            row.item_id,
            State.ANALYSED,
            meta=_merge_meta(row.meta, {"analysis": _analysis_note(extraction)}),
        )
        row = self.ledger.get(row.item_id) or row

        # --- 6. write the three files, confirm them, then persist DONE ------------
        result = self._publish(row, parsed, transcript, extraction, info,
                               notes=_engine_notes(engine_meta))
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
        log.info("done", ", ".join(sorted(result.names.values())), **result.names)
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

    def _transcribe(self, row: Row, path: str, info: AudioInfo, hints: Hints) -> Transcript:
        """The engine, or the cached answer from a run that crashed after paying for it."""
        cache = self._transcript_cache(row.item_id)
        cached = _load_transcript(cache, row.content_hash)
        if cached is not None:
            log.info("transcript-reused", f"{cached.word_count} words already transcribed by "
                                          f"{cached.engine} for these exact bytes",
                     words=cached.word_count, engine=cached.engine)
            return cached

        duration = float(info.duration_s or 0.0)
        transcript = run_engine(
            self.engine,
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

    def _publish(
        self,
        row: Row,
        parsed: naming.ParsedName,
        transcript: Transcript,
        extraction: Extraction,
        info: AudioInfo,
        *,
        notes: tuple[str, ...] = (),
    ) -> outputs.UploadResult:
        recorded_at, note = naming.resolve_timestamp(parsed, row.created_at)
        ctx = outputs.OutputContext(
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
        )
        self._refuse_name_collision(row, ctx)
        parent = str(getattr(self.config, "output_folder_id", "") or "")
        if not parent:
            raise PipelineFatal(
                "OUTPUT_FOLDER_ID is not set, so there is nowhere to write the three files"
            )
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


class _DownloadNotVerified(RuntimeError):
    """The bytes on disk are not the bytes Graph described. Worth one more try."""


#: Faults in the recording itself: retrying performs the same work and reaches the same
#: answer, so the recording goes to a person now rather than after three identical failures.
_NEVER_RETRY = (
    _ItemFault,
    SplitDurationError,
    SplitUnsupported,
    OutputContractError,
    TimestampUnavailable,
    TranscriptTooLarge,
    EngineAudioTooLarge,
)

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
