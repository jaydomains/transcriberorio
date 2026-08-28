"""A nicer title may never cost a recording. Nothing here is about whether a name is good.

``test_naming_never_misfiles.py`` asks whether a name is *right*. This file asks the older
and larger question: whether working one out can ever stop, delay, duplicate or hold up a
transcript. The service exists because Fireflies silently dropped 44% of his recordings,
and the whole naming feature is worth less than one of them.

So every claim below is of the same shape — **something in the naming path fails, and the
three files are still written, on time, with the same bytes.** The failures are not
imagined: they are the ones a live deployment actually produces.

* The decision rule throws. A model answer of an unexpected shape, a regex that blows the
  stack, a name nobody anticipated — :func:`transcriber.autoname.decide` says it never
  raises, and this asserts the pipeline survives it being wrong about that.
* The site list is missing, half-written, a directory, a JSON array, a book from a version
  this service does not read, or a file the nightly build has not produced for a fortnight.
  Every one of them must be worth exactly zero names and zero incidents.
* ``NAMING=0``. The bytes must be the ones :mod:`transcriber.outputs` writes on its own,
  and the naming modules must not be consulted at all.
* The renderer throws. This one is the subtle one, and it is the reason the try/except in
  :meth:`transcriber.pipeline.Pipeline._name` is drawn where it is: the naming step renders
  the very same file the publish is about to render, so a naming step that swallowed a
  render failure would swallow a *quarantine*. A recording that should have gone to a person
  would be published broken, or not at all, silently.
* The machine dies between the ledger write that stores the decision and the upload that
  uses it. The next pass must reach the same subject line — not re-decide it — or the record
  ends up holding two documents for one recording with no way to tell they are the same.
* Eighty recordings arrive in one morning. Nothing queues, nothing waits for a person, and
  the morning email stays readable.

Everything runs against the **real 56-site record**: ``ops/build-site-book.py``'s projection
of ``kbc-site-memory/build/spine.json`` when that repository is checked out beside this one,
and otherwise the record's own 56 site titles, vendored below. A made-up vocabulary would
let a name pass that his own record would reject, and the one measured hazard in this
feature — a body binding cleanly to Milton Court binding to nothing once ``CANTERBURY`` is
in its subject line — only exists against the real thing.
"""

from __future__ import annotations

import builtins
import contextlib
import datetime
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import time
import types
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock

from tests import support
from transcriber import autoname, digest, naming, outputs, sitebook
from transcriber import pipeline as pipeline_module
from transcriber.config import Config
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route, Segment, State, Transcript
from transcriber.outputs import OutputContractError
from transcriber.pipeline import (
    RESULT_DONE,
    RESULT_QUARANTINED,
    RESULT_RETRY,
    Pipeline,
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------- the record

#: Every site the record knows about, as ``(slug, title, monday item id)``, copied verbatim
#: from ``kbc-site-memory/build/spine.json`` on 2026-08-28. Used only when the record is not
#: checked out beside this repository — in CI it never is, and a suite that skipped itself
#: there would be a suite that proves nothing on the only run anybody watches.
#:
#: The titles and the ids are what the record's vocabulary is mostly built from, so the
#: terms that matter here are the real ones: ``square`` names both Canterbury Square and
#: Village Square and so discriminates nothing, ``court`` is used of three sites and is
#: dropped entirely, and ``canterbury`` names exactly one. The free-text fields — the
#: contractors, the client, the supervisor — are deliberately left out: they are other
#: people's names and addresses, and nothing in this file depends on them.
_THE_RECORDS_SITES = (
    ("22-chepstow-sea-point", "22 Chepstow, Sea Point", "1993226121"),
    ("250-voortrekker-fire-damage", "250 Voortrekker - Fire Damage", "3040184927"),
    ("250-voortrekker-waterproofing", "250 Voortrekker - Waterproofing", "2942599356"),
    ("277-imam-haron-road", "277 Imam Haron Road", "2828032579"),
    ("58-de-wet-road-bantry-bay", "58 De wet Road - Bantry Bay", "2829421618"),
    ("amidal-body-corporate-painting-project", "Amidal Body Corporate Painting Project", "3023311709"),
    ("ashton-steelworks", "Ashton Steelworks", "3098545320"),
    ("beach-court-bc", "Beach Court bc", "2828031638"),
    ("blsa-flooring", "BLSA Flooring", "3090747853"),
    ("blsa-installation-polycarb-sheeting", "BLSA - INSTALLATION POLYCARB SHEETING", "3023354674"),
    ("botmazicht-jps-trust-jacky", "Botmazicht - JPS Trust / Jacky", "2552041006"),
    ("canterbury-square", "Canterbury Square", "1917247803"),
    ("dalrie-hof", "Dalrie Hof", "5049457059"),
    ("de-waal-bc-jps-trust", "De Waal BC - JPS Trust", "2551899632"),
    ("de-waterkant-centre", "De Waterkant Centre", "2767250892"),
    ("eagle-house", "Eagle House", "2829421586"),
    ("eagle-house-storm-damage", "Eagle house - Storm Damage", "3015005505"),
    ("fairmill", "Fairmill", "2697281194"),
    ("forest-hill", "Forest Hill", "2975396079"),
    ("garden-route-mall-roof-remedials", "Garden Route Mall - Roof Remedials", "2829422515"),
    ("garden-route-mall-stormdamage-work", "Garden Route Mall - Stormdamage Work", "3014988504"),
    ("gp-mont-clare-waterproofing", "GP - Mont Clare - Waterproofing", "2628699429"),
    ("green-park", "Green Park", "2002009976"),
    ("house-swart-snags", "House Swart Snags", "2651398710"),
    ("hq-bedford-view", "HQ Bedford View", "2922889705"),
    ("kamal-cisco", "Kamal CISCO", "5030089098"),
    ("leeuwendal", "Leeuwendal", "2734082943"),
    ("lonehill-phase-2", "Lonehill Phase 2", "2722824606"),
    ("lonehill-shopping-centre", "Lonehill Shopping Centre", "2544529925"),
    ("longkloof-zappi-balcony", "Longkloof Zappi Balcony", ""),
    ("mandela-rhodes", "Mandela Rhodes", "3066716714"),
    ("mandela-rhodes-place", "Mandela Rhodes Place", "2544667123"),
    ("mill-road-hardstand-extension", "Mill Road Hardstand Extension", "5006073580"),
    ("milton-court-sea-point", "Milton Court - Sea Point", "2749978017"),
    ("mont-clare-place-window-project", "Mont Clare Place - Window Project", "2776419647"),
    ("n1-city-mall-roof", "N1 City Mall Roof", "2042336472"),
    ("n1-netcare-roofing", "N1 Netcare Roofing", "2942683449"),
    ("north-warf-carports", "North Warf Carports", "3066702578"),
    ("north-warf-urgent-works", "North Warf - Urgent Works", "2825259588"),
    ("orion-concrete-yard", "Orion Concrete Yard", "3099637555"),
    ("paardelvei-walkway-destructive-testing", "Paardelvei - Walkway Destructive Testing", "3128475126"),
    ("pine-tops", "Pine Tops", "2738178171"),
    ("prince-court-redec-project", "Prince Court Redec Project", "2701895750"),
    ("roggebaai-cladding", "Roggebaai Cladding", "2922746197"),
    ("roggebaai-urgent-works", "Roggebaai - Urgent Works", "2829412884"),
    ("sbv", "SBV", "2891587924"),
    ("shoprite-elsiesriver", "Shoprite Elsiesriver", "2975565702"),
    ("stellenberg-gardens-storm-damage", "Stellenberg Gardens - Storm Damage", "3015283716"),
    ("tassenwyk-phase-2", "Tassenwyk Phase 2", "2722813746"),
    ("the-estuaries-block-c", "The Estuaries Block C", "2991183660"),
    ("the-oval-collington-fernwood-office", "THE OVAL (COLLINGTON + FERNWOOD OFFICE)", "2830241274"),
    ("the-towers-century-city", "The Towers Century City", "2767198379"),
    ("urban-artisan-urgent-works", "Urban Artisan - urgent Works", "2754126824"),
    ("village-square", "Village Square", "1917247813"),
    ("vineyard-office-estate", "Vineyard Office Estate", "3025526474"),
    ("wolroy-house", "Wolroy House", "3004302809"),
)


def _the_records_own_projection() -> tuple[dict[str, Any], str]:
    """``ops/build-site-book.py``'s projection of the record, if the record is here.

    The same eight fields the record's own vocabulary function reads, taken out of the same
    file the cron entry reads. Read-only, and never written to: that repository is not ours.
    """
    for candidate in (
        "/home/user/kbc-site-memory/build/spine.json",
        os.path.join(_REPO, "..", "kbc-site-memory", "build", "spine.json"),
    ):
        path = os.path.abspath(candidate)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                spine = json.load(handle)
        except (OSError, ValueError):    # the record is not ours to keep readable
            continue
        sites = spine.get("sites") or {}
        projected = {
            slug: {field: entry.get(field) for field in sitebook.VOCAB_FIELDS}
            for slug, entry in sites.items()
            if str((entry or {}).get("title") or "").strip()
        }
        if projected:
            return projected, f"the record itself ({path})"
    return {}, ""


def _write_the_site_book(directory: str) -> tuple[str, str]:
    """The site book these tests run against, and where its sites came from."""
    sites, source = _the_records_own_projection()
    if not sites:
        sites = {
            slug: {"title": title, "monday_item_id": monday or None}
            for slug, title, monday in _THE_RECORDS_SITES
        }
        source = "the record's own site titles, vendored into this file"
    path = os.path.join(directory, "sites.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {"vocab_contract": sitebook.CONTRACT, "generated_at": "2026-08-28",
             "sites": sites},
            handle, ensure_ascii=False,
        )
    return path, source


_BOOK_DIR = tempfile.mkdtemp(prefix="naming-site-book-")

#: The real record's sites, in the shape the service reads them.
SITE_BOOK, SITE_BOOK_SOURCE = _write_the_site_book(_BOOK_DIR)


def tearDownModule() -> None:
    shutil.rmtree(_BOOK_DIR, ignore_errors=True)

ROUTE = Route(
    name="site-meetings",
    label="Site meetings",
    source_folder_id="S-SITE",
    output_folder_id="O-SITE",
    archive_folder_id="",
    engine="",
    enabled=True,
)

#: The voice recorder's own default name — the only shape this feature may ever act on.
RECORDER_DEFAULT = "Voice 260806_162219.m4a"

#: When OneDrive says it finished receiving it. Later than the moment in the name, which is
#: the ordinary case: he records on the walk and it uploads on the drive home.
RECEIVED_AT = "2026-08-06T15:00:00Z"

#: What the transcript would say. Written to satisfy all nine of the naming rules against
#: the real record — Canterbury named in the first line and the last, nothing else in it
#: that any of the other 55 sites is recognised by — because the tests that matter here are
#: the ones where a name WAS reached and something then went wrong. A recording that was
#: never going to be named cannot demonstrate that a name survives a crash.
SPOKEN_LINES = (
    "Right, I am at Canterbury this morning and the scaffold is up on the sea facing side.",
    "The painters have finished the second and third floors and the snag list is down to eleven.",
    "Marius says the mesh comes off on Thursday if the wind drops, otherwise it waits for Monday.",
    "I have asked for a price on the balustrade repairs before the trustees meet this month.",
    "The lift lobby ceiling still shows a damp mark, so somebody has to get above it and look.",
    "Two of the units on the ground floor gave no access, and I still need those keys from them.",
    "The rubbish skip has been standing full for a week now and it has to be taken away.",
    "I told the chairman we would have a written report to him by Friday afternoon at the latest.",
    "Nobody has signed the daily register since Tuesday, which is the sort of thing that gets noticed.",
    "I will be back at Canterbury next week to walk the last of it and close the list off.",
)

#: What the record calls the site those words are about, and what the service would title it.
EXPECTED_SITE = "canterbury-square"
#: The site, then the date and time the way he writes them on his own files —
#: ``BEACH COURT SITE WALK 270826``. Day-first, from the moment pinned on the row, which is
#: the recorder's own clock rather than when OneDrive finished receiving the file.
EXPECTED_SITE_NAME = "CANTERBURY"
EXPECTED_NAME = "CANTERBURY 060826 1622"

#: Long enough to clear ``NAMING_MIN_SECONDS`` and to make the word count plausible.
DURATION_S = 300.0

#: The audio bytes the fake download writes. Fixed, so two deployments processing the same
#: recording produce the same content hash and therefore the same rendered bytes.
AUDIO_BYTES = b"not really an m4a, but the same not-really-an-m4a every time"


def _transcript(lines: tuple[str, ...] = SPOKEN_LINES) -> Transcript:
    """One segment per line, spread across the recording, as the engine returns them."""
    step = DURATION_S / max(len(lines), 1)
    segments = [
        Segment(index * step, index * step + step * 0.9, "James", line)
        for index, line in enumerate(lines)
    ]
    return Transcript(
        text=" ".join(lines), segments=segments, language="en-ZA", engine="test-engine"
    )


def _extraction(site: str = "Canterbury") -> Any:
    return support.StubExtraction(
        summary="He walked the site; the scaffold comes off on Thursday and a price is due.",
        site=site,
    )


class _Drive:
    """A OneDrive that remembers exactly what was written into it, and can refuse."""

    def __init__(self) -> None:
        self.written: list[tuple[str, str, str]] = []   # (parent, name, text)
        self.items: dict[str, Any] = {}
        self.refuse = False
        #: Accept this many uploads and then start refusing. ``None`` accepts everything.
        #: A half-failed publish is the ordinary way this service meets a bad afternoon,
        #: and it is the case the whole stickiness rule is written for.
        self.refuse_after: int | None = None

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        if self.refuse or (self.refuse_after is not None
                           and len(self.written) >= self.refuse_after):
            raise RuntimeError("the drive refused this one")
        self.written.append((parent_id, name, data.decode("utf-8")))
        item = type("Item", (), {
            "id": f"out-{len(self.items)}", "name": name, "size": len(data), "web_url": "",
        })()
        self.items[item.id] = item
        return item

    def get_item(self, item_id: str) -> Any:
        return self.items[item_id]

    @property
    def names(self) -> list[str]:
        return [name for _p, name, _t in self.written]

    def by_kind(self) -> dict[str, str]:
        """The three files keyed by which of the three they are, for a byte comparison."""
        out: dict[str, str] = {}
        for _parent, name, text in self.written:
            kind = ("summary" if name.endswith("-summary.md")
                    else "actions" if name.endswith("-actions.md")
                    else "transcript")
            out[kind] = text
        return out

    def subject(self) -> str:
        """The transcript's first line — the one thing a worked-out name is allowed to move."""
        return self.by_kind()["transcript"].splitlines()[0]


#: A finished upload, as the completeness gate hands one back.
_ITEM = type("Item", (), {
    "id": "unused", "name": RECORDER_DEFAULT, "size": len(AUDIO_BYTES),
    "web_url": "https://example.invalid/voice", "hashes": {"quickXorHash": "AAAA"},
})()

#: The audio probe's answer. Patched in for the walk: a real probe of the fake bytes above
#: would call them truncated and quarantine the recording before naming was ever reached.
_AUDIO = support.audio_info(DURATION_S)
_PROBE = types.SimpleNamespace(
    probe=lambda path: _AUDIO, duration_is_known=lambda info: True
)


class _WalkingPipeline(Pipeline):
    """The real pipeline, with only the four steps that need the outside world stood in for.

    The completeness gate, the download, the engine and the analysis pass are replaced.
    Everything this file is about — the naming decision, the ledger write that stores it, the
    publish that uses it and the DONE that follows — is the shipped code, called in the
    shipped order by the shipped :meth:`process_one`. A harness that reimplemented that order
    would pass while the pipeline had it wrong, which is the failure this whole file exists
    to catch.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.transcript = kwargs.pop("transcript", None) or _transcript()
        self.extraction = kwargs.pop("extraction", None) or _extraction()
        self.publish_dies_with: BaseException | None = None
        super().__init__(*args, **kwargs)

    def _gate(self, row: Any, started: float) -> Any:
        return _ITEM

    def _fetch(self, row: Any, item: Any) -> str:
        directory = self._item_dir(row.item_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "recording.m4a")
        with open(path, "wb") as handle:
            handle.write(AUDIO_BYTES)
        self.ledger.advance(
            row.item_id, State.FETCHED,
            content_hash="a" * 64, size=len(AUDIO_BYTES), graph_hash="AAAA",
        )
        return path

    def _transcribe(self, row: Any, path: str, info: Any, hints: Any, engine: Any = None) -> Any:
        return self.transcript

    def _analyse(self, row: Any, transcript: Any, hints: Any) -> Any:
        return self.extraction

    def _publish(self, *args: Any, **kwargs: Any) -> Any:
        if self.publish_dies_with is not None:
            raise self.publish_dies_with
        return super()._publish(*args, **kwargs)


class Deployment:
    """One installation of the service: a config, a ledger, a drive and a pipeline.

    Built rather than mocked, because every claim in this file is about what the pipeline
    does with a ledger row, and a fake ledger would decide the answer.
    """

    def __init__(
        self,
        *,
        naming_on: bool = True,
        apply: bool = False,
        sites_file: str | None = SITE_BOOK,
        min_seconds: int = 120,
        drive: _Drive | None = None,
        clock: Any = None,
        directory: str | None = None,
        transcript: Transcript | None = None,
        extraction: Any = None,
    ) -> None:
        self.dir = directory or tempfile.mkdtemp(prefix="naming-loss-")
        self.config = support.make_config(
            routes=(ROUTE,),
            work_dir=os.path.join(self.dir, "work"),
            ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
            # The gate is not what this file is about, and ``off`` keeps it from writing to a
            # held-passage store that would add a second reason for a publish to fail.
            gate_mode="off",
            naming=naming_on,
            naming_apply=apply,
            naming_sites_file="" if sites_file is None else sites_file,
            naming_min_seconds=min_seconds,
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.drive = drive or _Drive()
        self.pipeline = _WalkingPipeline(
            self.config, self.ledger, self.drive,
            engine=object(),          # never called: _transcribe is stood in for
            transcript=transcript,
            extraction=extraction,
            clock=clock or time.time,
        )

    # -- driving it ----------------------------------------------------------------

    def arrive(
        self,
        item_id: str = "V1",
        name: str = RECORDER_DEFAULT,
        created_at: str = RECEIVED_AT,
    ) -> str:
        self.ledger.record_page(
            [DriveItem(item_id=item_id, name=name, size=len(AUDIO_BYTES),
                       etag=f'"{item_id}"', created_at=created_at)],
            f"cursor-{item_id}",
            route=ROUTE.name,
        )
        return item_id

    def walk(self, item_id: str = "V1") -> Any:
        """One pass of the real ``process_one`` over one recording."""
        with mock.patch.object(pipeline_module, "audio_probe", _PROBE):
            return self.pipeline.process_one(item_id)

    def reboot(self, *, after_seconds: float = 3600.0) -> "Deployment":
        """The service is killed and comes back up, an hour later, on the same ledger.

        A new :class:`Pipeline` on the same durable state — which is the only thing that
        survives a kill — with a clock far enough forward that any backoff has expired.
        """
        self.pipeline = _WalkingPipeline(
            self.config, self.ledger, self.drive,
            engine=object(),
            transcript=self.pipeline.transcript,
            extraction=self.pipeline.extraction,
            clock=lambda: time.time() + after_seconds,
        )
        return self

    def row(self, item_id: str = "V1") -> Any:
        found = self.ledger.get(item_id)
        assert found is not None
        return found

    def decision(self, item_id: str = "V1") -> dict[str, Any]:
        return dict(self.row(item_id).meta.get("naming") or {})

    def close(self) -> None:
        try:
            self.ledger.close()
        except Exception:       # noqa: BLE001 - a closed ledger is not a test failure
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def _published_once(case: unittest.TestCase, deployment: Deployment) -> dict[str, str]:
    """Three files, one of each, and the row says DONE. The floor under every test here."""
    case.assertEqual(len(deployment.drive.written), 3,
                     f"three files, or the recording is lost: {deployment.drive.names}")
    files = deployment.drive.by_kind()
    case.assertEqual(sorted(files), ["actions", "summary", "transcript"])
    case.assertEqual(deployment.row().state, State.DONE)
    return files


# ===================================================================== the fixture itself


class TheRealRecordIsWhatTheseTestsRunAgainst(unittest.TestCase):
    """If the fixture ever stops being the real record, everything below goes quiet.

    Not a formality. A site book that silently became empty would make every "no name was
    proposed" assertion in this file pass for the wrong reason, and the file would keep
    reporting green while proving nothing at all.
    """

    def test_the_site_book_is_the_real_fifty_six_site_record(self) -> None:
        book = sitebook.load(SITE_BOOK)
        self.assertEqual(book.fault, "", "the projection of the record must load cleanly")
        self.assertEqual(book.size, 56, "56 real sites; a smaller book weakens every test here")

    def test_the_recording_these_tests_use_would_really_be_named(self) -> None:
        # Every "a name survived X" test below is worthless if no name was reachable in the
        # first place. This is the one place that is asserted directly.
        book = sitebook.load(SITE_BOOK)
        parsed = naming.parse_source_name(RECORDER_DEFAULT)
        transcript = _transcript()
        recorded_at, note = naming.resolve_timestamp(parsed, RECEIVED_AT)
        ctx = outputs.OutputContext(
            item_id="V1", source_name=RECORDER_DEFAULT, parsed=parsed,
            recorded_at=recorded_at, timestamp_source=note, transcript=transcript,
            extraction=_extraction(), audio=_AUDIO, engine="test-engine",
        )
        decided = autoname.decide(
            parsed=parsed, extraction=_extraction(), spoken=outputs.spoken_body(transcript),
            duration_s=DURATION_S, book=book,
            render=lambda name: outputs.render_transcript(replace(ctx, display_name=name)),
            apply=True, min_seconds=120, recorded_at=recorded_at,
        )
        self.assertEqual(decided.code, "ok", decided.why)
        self.assertEqual(decided.name, EXPECTED_NAME)
        self.assertEqual(decided.site, EXPECTED_SITE)


# ============================================================ 1. the decision rule throws


class WhenDecidingANameExplodesTheRecordingIsUntouched(unittest.TestCase):
    """``autoname.decide`` raises on every call. Nothing downstream may notice.

    :func:`transcriber.autoname.decide` documents that it never raises. That is a promise
    about today's code, not a property of the universe: a model answer of an unexpected
    shape, a book field that changed type, a regex on a pathological span. The pipeline has
    to hold when the promise is broken, and the measure of holding is not "it recovers" — it
    is that the bytes reaching OneDrive are the ones a run with naming switched off would
    have written, to the character.
    """

    def setUp(self) -> None:
        self.exploding = Deployment(apply=True)
        self.addCleanup(self.exploding.close)
        self.exploding.arrive()
        self.calls = 0

        def blow_up(**kwargs: Any) -> Any:
            self.calls += 1
            raise ValueError("the naming rule fell over on this one")

        with mock.patch.object(autoname, "decide", side_effect=blow_up):
            self.outcome = self.exploding.walk()

        self.without_naming = Deployment(naming_on=False, directory=None)
        self.addCleanup(self.without_naming.close)
        self.without_naming.arrive()
        self.without_naming.walk()

    def test_the_three_files_are_still_written(self) -> None:
        _published_once(self, self.exploding)

    def test_the_recording_finishes_rather_than_retrying_or_quarantining(self) -> None:
        # A retry would delay it by the backoff and a quarantine would stop it dead until a
        # person looked. Either one is a recording delayed by a title.
        self.assertEqual(self.outcome.result, RESULT_DONE, self.outcome.reason)
        self.assertEqual(self.exploding.row().state, State.DONE)

    def test_the_rule_really_was_called_and_really_did_raise(self) -> None:
        # Without this, a decide() that was never reached would make the whole class pass.
        self.assertGreaterEqual(self.calls, 1)

    def test_every_byte_matches_a_run_with_naming_switched_off(self) -> None:
        exploded = self.exploding.drive.by_kind()
        plain = self.without_naming.drive.by_kind()
        for kind in ("transcript", "summary", "actions"):
            self.assertEqual(
                exploded[kind], plain[kind],
                f"the {kind} file differs from what NAMING=0 writes. The record reads these "
                f"bytes: a difference here is a difference in what it files and how it "
                f"scores the site, caused by a naming step that was supposed to have failed "
                f"invisibly.",
            )

    def test_the_three_filenames_are_the_ones_naming_off_would_have_used(self) -> None:
        # The names are how a half-failed publish is recovered: the next attempt writes the
        # same three names and replaces them. A name that moved would leave files nobody
        # can delete and a second document in the record.
        self.assertEqual(sorted(self.exploding.drive.names),
                         sorted(self.without_naming.drive.names))

    def test_the_row_records_that_it_was_decided_so_a_retry_does_not_try_again(self) -> None:
        # "Decided" means an answer was reached and stored, not that a name was found. A
        # failure that stored nothing would be re-decided on the next attempt, and could
        # reach the opposite answer with a different subject line.
        stored = self.exploding.decision()
        self.assertTrue(stored.get("decided"))
        self.assertEqual(stored.get("name"), "")
        self.assertFalse(stored.get("applied"))
        self.assertEqual(stored.get("code"), "error")

    def test_a_second_pass_over_the_finished_row_writes_nothing_more(self) -> None:
        again = self.exploding.walk()
        self.assertEqual(len(self.exploding.drive.written), 3,
                         "a finished recording was published twice")
        self.assertNotEqual(again.result, RESULT_DONE)


class EveryStepOfTheNamingPathMayThrowWithoutCostingAFile(unittest.TestCase):
    """Not just ``decide``: the span search, the book, the parser, the renderer callback.

    Each of these is a different line in the naming path, and each is patched to raise in
    turn. The claim is uniform and deliberately boring — three files, DONE, no name.
    """

    def _walk_with(self, target: Any, attribute: str) -> Deployment:
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        with mock.patch.object(target, attribute,
                               side_effect=RuntimeError("this one fell over")):
            deployment.walk()
        return deployment

    def test_a_broken_span_search_still_publishes(self) -> None:
        deployment = self._walk_with(autoname, "_find_span")
        _published_once(self, deployment)
        self.assertNotIn(EXPECTED_NAME, deployment.drive.subject())

    def test_a_broken_eligibility_check_still_publishes(self) -> None:
        deployment = self._walk_with(autoname, "eligible")
        _published_once(self, deployment)

    def test_a_broken_recorder_default_check_still_publishes(self) -> None:
        deployment = self._walk_with(autoname, "is_recorder_default")
        _published_once(self, deployment)

    def test_a_broken_published_body_reader_still_reaches_a_decision(self) -> None:
        """``spoken_body`` is called by the pipeline OUTSIDE ``decide``.

        Not asserted through a whole walk, because ``spoken_body`` is not naming's: the
        renderer writes the transcript's body with it too, so breaking it for a walk breaks
        the publish for reasons that have nothing to do with a title. What is naming's is
        the wrapper in ``_name``, which has to reach round this call as well as round
        ``decide`` — drawn one line tighter and this would raise into ``process_one`` and
        the recording would retry, back off, and eventually go to a person over a title.
        """
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        row = deployment.row()
        gate = types.SimpleNamespace(
            transcript=deployment.pipeline.transcript,
            extraction=deployment.pipeline.extraction,
            held=(),
        )
        with mock.patch.object(outputs, "spoken_body",
                               side_effect=RuntimeError("this one fell over")):
            decision = deployment.pipeline._name(
                row, naming.parse_source_name(RECORDER_DEFAULT), gate, _AUDIO, ROUTE
            )
        self.assertTrue(decision.decided)
        self.assertEqual(decision.name, "")
        self.assertEqual(decision.code, "error")

    def test_a_broken_site_binding_still_publishes(self) -> None:
        deployment = self._walk_with(sitebook, "bind_site")
        _published_once(self, deployment)


# ================================================================== 2. a sick site list


class ASickSiteListNamesNothingAndCostsNothing(unittest.TestCase):
    """Seven ways the site list can be wrong, and one answer to all of them.

    The list is written by a cron entry in another repository, after a nightly build of a
    record this service may not write to. It will be absent, stale, half-written and — the
    day the record changes which fields feed its vocabulary — written for a version this
    code does not read. None of that is exotic; all of it is a Tuesday.
    """

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="sick-books-")

    def _book(self, filename: str, contents: str | None) -> str:
        path = os.path.join(self.dir, filename)
        if contents is not None:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(contents)
        return path

    def _cases(self) -> dict[str, str]:
        """Every sick book, keyed by what is wrong with it."""
        a_directory = os.path.join(self.dir, "a-directory")
        os.makedirs(a_directory, exist_ok=True)
        return {
            "there is no file": self._book("missing.json", None),
            "it is a directory": a_directory,
            "it is not JSON at all": self._book("half.json", '{"sites": {"a": '),
            "it is JSON but not an object": self._book("array.json", '["beach court"]'),
            "it is an object but not a site list": self._book("other.json", '{"hello": 1}'),
            "it was written for another version": self._book(
                "contract.json",
                '{"vocab_contract": 99, "generated_at": "2026-08-28", '
                '"sites": {"beach-court-bc": {"title": "Beach Court bc"}}}',
            ),
            "it lists no sites": self._book(
                "empty.json", '{"vocab_contract": 1, "sites": {}}'
            ),
            "its sites have no names": self._book(
                "nameless.json",
                '{"vocab_contract": 1, "sites": {"beach-court-bc": {"title": "  "}}}',
            ),
        }

    def test_loading_a_sick_book_never_raises_and_never_yields_a_site(self) -> None:
        for what, path in self._cases().items():
            with self.subTest(what):
                book = sitebook.load(path)     # must not raise, whatever is in there
                self.assertFalse(book, f"{what}: a sick book must hold no sites")
                self.assertEqual(book.size, 0)
                self.assertIsNone(book.bind("Right, I am at Canterbury this morning.")[0])
                self.assertEqual(book.sites_named_by("Canterbury"), frozenset())

    def test_a_sick_book_says_what_is_wrong_in_the_morning_email(self) -> None:
        for what, path in self._cases().items():
            with self.subTest(what):
                line = sitebook.load(path).line()
                # Printed every day, including the bad ones: silence must never mean
                # "working" and "the list stopped being written a fortnight ago" at once.
                self.assertIn("nothing is being named", line)
                self.assertNotIn("\n", line, "one line, in an email he reads on a phone")

    def test_every_sick_book_still_publishes_three_files_and_names_nothing(self) -> None:
        for what, path in self._cases().items():
            with self.subTest(what):
                deployment = Deployment(apply=True, sites_file=path)
                self.addCleanup(deployment.close)
                deployment.arrive()
                outcome = deployment.walk()

                self.assertEqual(outcome.result, RESULT_DONE, f"{what}: {outcome.reason}")
                files = _published_once(self, deployment)
                self.assertNotIn(
                    EXPECTED_NAME, files["transcript"].splitlines()[0],
                    f"{what}: a book that could not be read proposed a name anyway",
                )
                self.assertEqual(deployment.decision().get("name"), "")

    def test_no_site_list_configured_at_all_publishes_and_names_nothing(self) -> None:
        deployment = Deployment(apply=True, sites_file=None)
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.walk()

        _published_once(self, deployment)
        self.assertEqual(deployment.decision().get("name"), "")
        self.assertFalse(deployment.pipeline.site_book,
                         "an unconfigured path must give an empty book, not a guess")

    def test_a_book_that_raises_on_load_publishes_and_names_nothing(self) -> None:
        # ``sitebook.load`` promises never to raise. The pipeline wraps it anyway, and this
        # is what that second wrapper is for.
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        with mock.patch.object(sitebook, "load", side_effect=MemoryError("out of memory")):
            outcome = deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        _published_once(self, deployment)
        self.assertEqual(deployment.decision().get("name"), "")

    def test_a_book_that_returns_garbage_publishes_and_names_nothing(self) -> None:
        # Not a SiteBook at all. Whatever the naming path then does with it — an
        # AttributeError somewhere inside ``decide`` — the recording is not the thing that
        # pays for it.
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        with mock.patch.object(sitebook, "load", return_value="not a site book at all"):
            outcome = deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        _published_once(self, deployment)
        self.assertEqual(deployment.decision().get("name"), "")

    def test_the_book_is_read_once_and_not_again_per_recording(self) -> None:
        # Read per recording, an 80-file morning is 80 reads of the same 80 KB file on the
        # path that publishes a transcript. It is cached on modification time instead.
        deployment = Deployment()
        self.addCleanup(deployment.close)
        with mock.patch.object(sitebook, "load", wraps=sitebook.load) as loader:
            for _ in range(5):
                self.assertTrue(deployment.pipeline.site_book)
        self.assertEqual(loader.call_count, 1)


class AServiceWithASickSiteListStillStarts(unittest.TestCase):
    """Startup. A recording cannot be delayed by a title if the service will not boot.

    ``ops/build-site-book.py`` runs from a cron entry in a repository this service does not
    own. The morning it does not run, or runs half way, must be a morning with plainer
    titles and nothing else.
    """

    #: A complete environment with nothing real in it. Built here rather than read from the
    #: process, so no test can pick up a live credential.
    BASE = {
        "GRAPH_TENANT_ID": "tenant-for-tests",
        "GRAPH_CLIENT_ID": "client-for-tests",
        "GRAPH_CLIENT_SECRET": "not-a-real-secret",
        "GRAPH_USER_ID": "drive-owner",
        "SOURCE_FOLDER_ID": "SOURCE",
        "OUTPUT_FOLDER_ID": "OUTPUT",
        "ARCHIVE_FOLDER_ID": "ARCHIVE",
        "TRANSCRIBE_ENGINE": "openai",
        "OPENAI_API_KEY": "not-a-real-engine-key",
        "ANALYSIS_API_KEY": "not-a-real-analysis-key",
        "SMTP_HOST": "smtp.invalid",
        "SMTP_USER": "digest",
        "SMTP_PASSWORD": "not-a-real-password",
        "SMTP_FROM": "digest@invalid",
        "SMTP_TO": "someone@invalid",
        "HEARTBEAT_URL": "https://example.invalid/beat",
        "LEDGER_PATH": ":memory:",
    }

    def _start(self, **overrides: str) -> Config:
        return Config.from_env(dict(self.BASE, **overrides))

    def test_a_missing_site_list_is_a_notice_and_not_a_refusal_to_start(self) -> None:
        config = self._start(NAMING="1", NAMING_SITES_FILE="/no/such/sites.json")
        self.assertTrue(config.naming)
        self.assertTrue(
            any("not there" in notice for notice in config.notices),
            "a list that stopped being written must say so, not fail silently",
        )

    def test_no_site_list_configured_is_a_notice_and_not_a_refusal_to_start(self) -> None:
        config = self._start(NAMING="1")
        self.assertEqual(config.naming_sites_file, "")
        self.assertTrue(any("no recording will be given a name" in n for n in config.notices))

    def test_the_shipped_default_reports_and_applies_nothing(self) -> None:
        # The feature ships measuring itself. Nobody has counted how often it fires or how
        # often it is right, and arming it against an estimate is how a wrong name gets
        # into the record before anybody has seen one.
        config = self._start()
        self.assertTrue(config.naming)
        self.assertFalse(config.naming_apply)

    def test_a_directory_where_the_site_list_should_be_still_starts(self) -> None:
        config = self._start(NAMING="1", NAMING_SITES_FILE=tempfile.gettempdir())
        self.assertTrue(config.naming)


# ======================================================================= 3. NAMING=0


class WithNamingOffNothingInTheNamingPathIsConsulted(unittest.TestCase):
    """``NAMING=0`` is the escape hatch. It has to be a real one.

    If this feature ever does something he does not like, the answer at 06:00 is one
    environment variable and a restart. That is only true if switching it off takes the
    naming code out of the path entirely rather than running it and discarding the answer —
    and only worth anything if the bytes go back to exactly what they were.
    """

    def setUp(self) -> None:
        self.off = Deployment(naming_on=False, apply=True)
        self.addCleanup(self.off.close)
        self.off.arrive()
        # Both doors into the naming code are wired to explode. If NAMING=0 touches either,
        # the recording fails and this class goes red rather than passing quietly.
        with mock.patch.object(
            autoname, "decide",
            side_effect=AssertionError("NAMING=0 must never reach the decision rule"),
        ), mock.patch.object(
            sitebook, "load",
            side_effect=AssertionError("NAMING=0 must never read the site list"),
        ):
            self.outcome = self.off.walk()

    def test_the_recording_is_published_and_finished(self) -> None:
        self.assertEqual(self.outcome.result, RESULT_DONE, self.outcome.reason)
        _published_once(self, self.off)

    def test_the_site_list_was_never_loaded(self) -> None:
        self.assertIs(self.off.pipeline._site_book, sitebook.EMPTY)

    def test_the_bytes_are_the_ones_outputs_writes_on_its_own(self) -> None:
        """The baseline is the renderers alone — no autoname, no sitebook, no book.

        This is as close as a live suite can get to "a build with the naming modules never
        imported": the three files rendered straight out of :mod:`transcriber.outputs` from
        a context with no display name, which is the only thing naming can contribute.
        """
        parsed = naming.parse_source_name(RECORDER_DEFAULT)
        recorded_at, note = naming.resolve_timestamp(parsed, RECEIVED_AT)
        ctx = outputs.OutputContext(
            item_id="V1", source_name=RECORDER_DEFAULT, parsed=parsed,
            recorded_at=recorded_at, timestamp_source=note,
            transcript=self.off.pipeline.transcript, extraction=self.off.pipeline.extraction,
            audio=_AUDIO, content_hash="a" * 64, graph_hash="AAAA",
            web_url="https://example.invalid/voice", engine="test-engine",
        )
        expected = {f.kind: f.text for f in outputs.render_all(ctx)}
        written = self.off.drive.by_kind()
        for kind in ("transcript", "summary", "actions"):
            self.assertEqual(written[kind], expected[kind],
                             f"NAMING=0 did not write the pre-naming {kind} bytes")

    def test_the_row_says_off_rather_than_saying_nothing(self) -> None:
        # A row with no naming entry at all would be indistinguishable from one written
        # before the feature existed, and the morning email counts on the difference.
        stored = self.off.decision()
        self.assertEqual(stored.get("code"), "off")
        self.assertTrue(stored.get("decided"))

    def test_the_morning_email_says_nothing_about_naming_at_all(self) -> None:
        report = digest.naming_report(self.off.config, self.off.ledger, day="2026-08-28")
        self.assertEqual(dict(report), {}, "a switched-off feature must not occupy his email")


class ReportingOnlyChangesNoBytesEither(unittest.TestCase):
    """``NAMING=1`` with ``NAMING_APPLY=0`` — the shipped default — writes the same file.

    This is the configuration his service will actually run for the first weeks. If it moved
    a single byte, the measurement it exists to produce would be taken on a population that
    had already been changed by taking it.
    """

    def setUp(self) -> None:
        self.reporting = Deployment(apply=False)
        self.addCleanup(self.reporting.close)
        self.reporting.arrive()
        self.reporting.walk()

        self.off = Deployment(naming_on=False)
        self.addCleanup(self.off.close)
        self.off.arrive()
        self.off.walk()

    def test_a_name_was_worked_out(self) -> None:
        # Otherwise the byte comparison below is comparing two runs that both did nothing.
        stored = self.reporting.decision()
        self.assertEqual(stored.get("name"), EXPECTED_NAME, stored.get("why"))
        self.assertEqual(stored.get("code"), "ok")

    def test_but_it_was_not_applied(self) -> None:
        self.assertFalse(self.reporting.decision().get("applied"))

    def test_and_not_one_byte_of_the_three_files_moved(self) -> None:
        reported = self.reporting.drive.by_kind()
        plain = self.off.drive.by_kind()
        for kind in ("transcript", "summary", "actions"):
            self.assertEqual(
                reported[kind], plain[kind],
                f"reporting a name changed the {kind} file. The record scores these bytes to "
                f"decide which site the note belongs to, so a byte that moves here can move "
                f"a filing that was right.",
            )

    def test_the_subject_line_still_carries_the_filename(self) -> None:
        self.assertIn("Voice 260806_162219", self.reporting.drive.subject())
        self.assertNotIn(EXPECTED_NAME, self.reporting.drive.subject())


# =========================================================== 4. the renderer throws


class TheNamingProbeNeverSwallowsAPublishFailure(unittest.TestCase):
    """The subtlest way this feature could lose a recording, and the one to watch.

    The naming step renders the transcript to check its own answer against the record. That
    is the same call the publish makes a moment later. Both are inside code that catches
    everything, and if the naming step's catch reached the publish's render, a recording
    that should have stopped and gone to a person would instead have been swallowed by a
    feature whose entire job is to be optional.

    So: the renderer is made to throw, and the assertion is that the run ends *exactly* as
    it ends with naming switched off — same result, same reason, same nothing-uploaded.
    """

    def _run_with_a_broken_renderer(self, *, naming_on: bool) -> tuple[Any, Deployment]:
        deployment = Deployment(naming_on=naming_on, apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        boom = OutputContractError("the transcript would carry a seventh header key")
        with mock.patch.object(outputs, "render_transcript", side_effect=boom):
            outcome = deployment.walk()
        return outcome, deployment

    def setUp(self) -> None:
        self.with_naming, self.named = self._run_with_a_broken_renderer(naming_on=True)
        self.without_naming, self.plain = self._run_with_a_broken_renderer(naming_on=False)

    def test_the_failure_is_not_swallowed(self) -> None:
        self.assertEqual(self.with_naming.result, RESULT_QUARANTINED, self.with_naming.reason)
        self.assertEqual(self.named.row().state, State.QUARANTINED)

    def test_it_ends_exactly_as_it_would_have_without_naming(self) -> None:
        self.assertEqual(self.with_naming.result, self.without_naming.result)
        self.assertEqual(self.with_naming.reason, self.without_naming.reason,
                         "the naming step changed how a broken render is reported")
        self.assertEqual(self.named.row().quarantine_reason,
                         self.plain.row().quarantine_reason)

    def test_nothing_was_uploaded_on_either_run(self) -> None:
        # All-or-none. A transcript that could not be rendered must leave no summary and no
        # actions file behind for the record to ingest on its own.
        self.assertEqual(self.named.drive.written, [])
        self.assertEqual(self.plain.drive.written, [])

    def test_the_naming_step_still_worked_out_a_name(self) -> None:
        # The render is used for one thing: asking the record where it will file this, which
        # is a note in the morning email rather than a veto. So a render that throws costs
        # the note and not the title. It used to cost the title, and the reversal is why:
        # the record decides by counting distinct vocabulary words rather than how much of a
        # recording is about what, so it titles a walk after a phone call taken during it.
        #
        # None of this reaches OneDrive — the sibling tests above assert the publish still
        # quarantines on the same fault, in the same words, with nothing written.
        stored = self.named.decision()
        self.assertEqual(stored.get("name"), EXPECTED_NAME)
        self.assertEqual(stored.get("code"), "ok")
        self.assertEqual(stored.get("filed"), "")

    def test_the_person_reading_the_quarantine_is_told_the_real_fault(self) -> None:
        # The reason has to be the render fault in its own words. A naming step that
        # rewrote it — or caught it and reported something of its own — would send whoever
        # reads it at 06:00 looking at the wrong feature.
        reason = str(self.named.row().quarantine_reason or "")
        self.assertIn("seventh header key", reason)


# ================================================= 5. a kill between the ledger and the drive


class ADecisionSurvivesAKillBetweenTheLedgerAndTheDrive(unittest.TestCase):
    """The stickiness rule, which is what stops one recording becoming two documents.

    The decision is written on the ANALYSED row, before a byte is uploaded. If the machine
    dies in between — and it will, that is what a half-failed publish IS — the next pass has
    to publish the decision it finds rather than making a new one. The world moves overnight:
    the record's nightly build runs, the site list is rewritten, a site is added or renamed.
    A second decision could easily be a different one, and the two subject lines would land
    in the record as two documents for one recording with nothing to reconcile them.
    """

    def setUp(self) -> None:
        self.deployment = Deployment(apply=True)
        self.addCleanup(self.deployment.close)
        self.deployment.arrive()

        # Pass one: the decision is reached and stored, and the machine dies on the publish.
        self.deployment.pipeline.publish_dies_with = RuntimeError("the machine was powered off")
        self.first = self.deployment.walk()

    def test_the_first_pass_stored_the_decision_and_published_nothing(self) -> None:
        self.assertEqual(self.deployment.drive.written, [], "nothing may have been uploaded")
        self.assertEqual(self.deployment.row().state, State.ANALYSED)
        stored = self.deployment.decision()
        self.assertEqual(stored.get("name"), EXPECTED_NAME)
        self.assertTrue(stored.get("applied"))

    def test_the_recording_is_not_lost_it_is_waiting_to_be_retried(self) -> None:
        self.assertEqual(self.first.result, RESULT_RETRY, self.first.reason)
        self.assertNotEqual(self.deployment.row().state, State.QUARANTINED)

    def test_the_next_pass_publishes_once_with_the_stored_name(self) -> None:
        self.deployment.reboot()
        outcome = self.deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        files = _published_once(self, self.deployment)
        self.assertTrue(files["transcript"].startswith(f"Subject: {EXPECTED_NAME} "),
                        files["transcript"].splitlines()[0])

    def test_the_stored_name_wins_even_though_the_world_moved_overnight(self) -> None:
        # The site list is rewritten every night. Here it comes back without the site the
        # first pass named — the record renamed it, or the build half-ran. The decision that
        # was already made is the one that publishes, because the alternative is a second
        # subject line for a recording the record may already hold under the first.
        empty = os.path.join(self.deployment.dir, "sites-now-empty.json")
        with open(empty, "w", encoding="utf-8") as handle:
            handle.write('{"vocab_contract": 1, "sites": {}}')
        self.deployment.config.naming_sites_file = empty

        self.deployment.reboot()
        outcome = self.deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        self.assertFalse(self.deployment.pipeline.site_book, "the book really is empty now")
        self.assertTrue(self.deployment.drive.subject().startswith(f"Subject: {EXPECTED_NAME}"),
                        self.deployment.drive.subject())

    def test_the_three_filenames_never_depended_on_the_name_anyway(self) -> None:
        # Which is why re-publishing is safe at all: the same three names are written again
        # and OneDrive replaces them. A name in the filename would leave the first attempt's
        # files orphaned under a name nothing points at.
        self.deployment.reboot()
        self.deployment.walk()
        for written in self.deployment.drive.names:
            self.assertNotIn(EXPECTED_NAME, written)
            self.assertIn("Voice 260806_162219", written)

    def test_a_finished_recording_is_never_published_a_second_time(self) -> None:
        self.deployment.reboot()
        self.deployment.walk()
        self.deployment.reboot()
        self.deployment.walk()      # already DONE: must be a no-op
        self.assertEqual(len(self.deployment.drive.written), 3)


class AHalfFailedPublishReplacesRatherThanDuplicating(unittest.TestCase):
    """The transcript lands, the summary does not, and the whole thing is tried again.

    This is the case the stickiness rule was written for, and it is not rare: an upload that
    fails two files in is a slow afternoon at Microsoft, not a disaster. The transcript is
    already in the output folder under a name the record has very possibly already ingested.
    If the retry wrote a different subject line, the record would file a second document for
    one recording and there would be nothing to tell them apart.
    """

    def setUp(self) -> None:
        self.deployment = Deployment(apply=True)
        self.addCleanup(self.deployment.close)
        self.deployment.arrive()
        self.deployment.drive.refuse_after = 1      # the transcript lands; nothing else does
        self.first = self.deployment.walk()

        self.landed = list(self.deployment.drive.written)
        self.deployment.drive.refuse_after = None
        self.deployment.reboot()
        self.second = self.deployment.walk()

    def test_the_first_attempt_left_one_file_and_did_not_mark_it_done(self) -> None:
        self.assertEqual(len(self.landed), 1, "the harness must really have half-failed")
        self.assertEqual(self.first.result, RESULT_RETRY, self.first.reason)

    def test_the_second_attempt_finishes_it(self) -> None:
        self.assertEqual(self.second.result, RESULT_DONE, self.second.reason)
        self.assertEqual(self.deployment.row().state, State.DONE)

    def test_the_record_is_left_holding_three_files_and_not_four(self) -> None:
        # Uploaded four times, under three names: OneDrive replaces by name, so the stray
        # from the failed attempt is the file that was rewritten rather than a second copy.
        self.assertEqual(len(self.deployment.drive.written), 4)
        self.assertEqual(len(set(self.deployment.drive.names)), 3)

    def test_the_replaced_transcript_carries_the_same_subject_line_as_the_stray(self) -> None:
        # The one assertion this class exists for. The first attempt's transcript is in the
        # record already; the second must be the same document, not a new one.
        first_subject = self.landed[0][2].splitlines()[0]
        self.assertEqual(first_subject, self.deployment.drive.subject())
        self.assertTrue(first_subject.startswith(f"Subject: {EXPECTED_NAME}"), first_subject)


class ARefusalIsAsStickyAsAName(unittest.TestCase):
    """Both directions, because only one of them is the obvious one.

    Refused once, refused forever: the site list gains the site tomorrow and the recording
    still publishes plain, because the record may already hold it under the plain title.

    Applied once, applied forever: the site list LOSES the site tomorrow and the recording
    still publishes under the name it was given, for exactly the same reason. The second is
    the direction that looks wrong and is not.
    """

    def test_refused_once_stays_refused_when_the_book_gains_the_site(self) -> None:
        deployment = Deployment(apply=True, sites_file=None)     # no book: nothing is named
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.pipeline.publish_dies_with = RuntimeError("powered off")
        deployment.walk()

        refused = deployment.decision()
        self.assertEqual(refused.get("name"), "")
        self.assertTrue(refused.get("decided"))

        # Overnight, the site list is configured and the site is in it.
        deployment.config.naming_sites_file = SITE_BOOK
        deployment.reboot()
        outcome = deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        self.assertTrue(deployment.pipeline.site_book, "the book is real now")
        self.assertEqual(deployment.decision().get("name"), "",
                         "a stored refusal was overturned on a retry")
        self.assertNotIn(EXPECTED_NAME, deployment.drive.subject())

    def test_applied_once_stays_applied_when_the_book_loses_the_site(self) -> None:
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.pipeline.publish_dies_with = RuntimeError("powered off")
        deployment.walk()
        self.assertEqual(deployment.decision().get("name"), EXPECTED_NAME)

        deployment.config.naming_sites_file = "/gone/overnight/sites.json"
        deployment.reboot()
        outcome = deployment.walk()

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        self.assertFalse(deployment.pipeline.site_book)
        self.assertTrue(deployment.drive.subject().startswith(f"Subject: {EXPECTED_NAME}"),
                        deployment.drive.subject())

    def test_a_stored_decision_is_reused_rather_than_recomputed(self) -> None:
        # Asserted directly on the seam, because the two tests above would also pass if the
        # rule happened to reach the same answer twice by luck.
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.pipeline.publish_dies_with = RuntimeError("powered off")
        deployment.walk()

        deployment.reboot()
        with mock.patch.object(
            autoname, "decide",
            side_effect=AssertionError("a stored decision must never be re-decided"),
        ):
            outcome = deployment.walk()
        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)

    def test_an_unreadable_stored_decision_is_decided_again_rather_than_raising(self) -> None:
        # A row written by an older version, or edited by hand. "Unreadable" has to mean
        # "not decided yet", which decides again — never "stop".
        for junk in ("", 7, [], {"decided": True, "mentions": "not a number"}, {"decided": 0}):
            with self.subTest(repr(junk)):
                self.assertIsNone(autoname.NameDecision.from_meta(junk))

    def test_a_decision_read_back_from_the_row_is_the_one_that_was_written(self) -> None:
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.pipeline.publish_dies_with = RuntimeError("powered off")
        deployment.walk()

        restored = autoname.NameDecision.from_meta(deployment.decision())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.name, EXPECTED_NAME)
        self.assertEqual(restored.site, EXPECTED_SITE)
        self.assertTrue(restored.applied)


# ============================================================== 6. what the path touches


class TheNamingPathTouchesNothingButTheSiteList(unittest.TestCase):
    """No file, no socket, no database. One stat of a cached site list, and arithmetic.

    This is what makes the timing claim true rather than hopeful. Anything on this path that
    opened a file, resolved a name or took a lock would put a recording behind it — on the
    morning eighty arrive at once, eighty times over.
    """

    def setUp(self) -> None:
        self.deployment = Deployment(apply=True)
        self.addCleanup(self.deployment.close)
        self.deployment.arrive()
        self.row = self.deployment.row()
        self.parsed = naming.parse_source_name(RECORDER_DEFAULT)
        self.gate = types.SimpleNamespace(
            transcript=self.deployment.pipeline.transcript,
            extraction=self.deployment.pipeline.extraction,
            held=(),
        )
        # Warm the cache first: the one read this path is allowed is the first one.
        self.assertTrue(self.deployment.pipeline.site_book)

    def test_a_decision_is_reached_with_open_socket_and_sqlite_all_broken(self) -> None:
        stats: list[str] = []
        real_stat = os.stat

        def counted_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            stats.append(str(path))
            return real_stat(path, *args, **kwargs)

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the naming path touched the outside world")

        # The decision is computed inside the sabotage and asserted outside it: an assertion
        # failing in here would send unittest's own traceback machinery through the broken
        # ``open`` and report something unrelated.
        with mock.patch.object(builtins, "open", refuse), \
                mock.patch.object(socket, "socket", refuse), \
                mock.patch.object(sqlite3, "connect", refuse), \
                mock.patch.object(os, "stat", counted_stat):
            decision = self.deployment.pipeline._name(
                self.row, self.parsed, self.gate, _AUDIO, ROUTE
            )

        self.assertEqual(decision.code, "ok", decision.why)
        self.assertEqual(decision.name, EXPECTED_NAME)
        self.assertEqual(
            stats, [self.deployment.config.naming_sites_file],
            "the naming path stat'd something other than the site list, or stat'd it twice",
        )

    def test_the_decision_rule_on_its_own_touches_nothing_at_all(self) -> None:
        book = sitebook.load(SITE_BOOK)
        transcript = self.deployment.pipeline.transcript
        recorded_at, note = naming.resolve_timestamp(self.parsed, RECEIVED_AT)
        ctx = outputs.OutputContext(
            item_id="V1", source_name=RECORDER_DEFAULT, parsed=self.parsed,
            recorded_at=recorded_at, timestamp_source=note, transcript=transcript,
            extraction=_extraction(), audio=_AUDIO, engine="test-engine",
        )

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the decision rule touched the outside world")

        with mock.patch.object(builtins, "open", refuse), \
                mock.patch.object(socket, "socket", refuse), \
                mock.patch.object(sqlite3, "connect", refuse), \
                mock.patch.object(os, "stat", refuse):
            decision = autoname.decide(
                parsed=self.parsed, extraction=_extraction(),
                spoken=outputs.spoken_body(transcript), duration_s=DURATION_S, book=book,
                render=lambda name: outputs.render_transcript(replace(ctx, display_name=name)),
                apply=True, min_seconds=120,
            )

        self.assertEqual(decision.code, "ok", decision.why)

    def test_naming_asks_the_ledger_for_nothing(self) -> None:
        # The stored decision comes off the row that is already in hand. A read here would
        # be a second query per recording on the path that publishes it.
        with mock.patch.object(self.deployment.ledger, "get",
                               side_effect=AssertionError("naming queried the ledger")):
            decision = self.deployment.pipeline._name(
                self.row, self.parsed, self.gate, _AUDIO, ROUTE
            )
        self.assertEqual(decision.code, "ok")


class NamingNeverMovesAnythingInOneDrive(unittest.TestCase):
    """Not the audio, not the three files. The only thing that moves is a line of text.

    A rename in OneDrive is the one operation here that could destroy something: the source
    recording is the only copy of what was said, and a service that renames it has taken a
    file he can find and made it a file he cannot.
    """

    def setUp(self) -> None:
        self.deployment = Deployment(apply=True)
        self.addCleanup(self.deployment.close)
        self.deployment.arrive()
        self.deployment.walk()

    def test_a_name_really_was_applied(self) -> None:
        self.assertTrue(self.deployment.decision().get("applied"))
        self.assertTrue(self.deployment.drive.subject().startswith(f"Subject: {EXPECTED_NAME}"))

    def test_the_drive_was_only_ever_asked_to_upload(self) -> None:
        # The fake drive offers ``upload`` and ``get_item`` and nothing else, so a rename or
        # a move would have raised. Asserted out loud so a future drive with a ``rename``
        # does not make this silently vacuous.
        self.assertFalse(hasattr(self.deployment.drive, "rename"))
        self.assertFalse(hasattr(self.deployment.drive, "move"))

    def test_the_source_recording_still_has_the_name_it_arrived_with(self) -> None:
        self.assertEqual(self.deployment.row().name, RECORDER_DEFAULT)

    def test_the_transcript_says_the_audio_is_still_called_what_it_is_called(self) -> None:
        # Because the title above it no longer matches, and a reader comparing the two would
        # otherwise conclude one of them is wrong.
        body = self.deployment.drive.by_kind()["transcript"]
        self.assertIn(RECORDER_DEFAULT, body)

    def test_the_ledger_still_points_at_the_three_files_that_were_written(self) -> None:
        row = self.deployment.row()
        written = set(self.deployment.drive.names)
        for name in (row.transcript_name, row.summary_name, row.actions_name):
            self.assertIn(name, written, "the ledger names a file that is not in the drive")


# ==================================================================== 7. eighty at once


class EightyRecordingsInOneMorning(unittest.TestCase):
    """The burst day. Eighty eligible recordings, and nothing waits for anybody.

    Eighty is his real worst case — a week's backlog syncing at once after the phone has
    been offline. Every one of them is a recording that arrived under the voice recorder's
    own name, so every one of them goes down the naming path. Nothing here may queue, hold,
    defer, ask or send a second email.
    """

    COUNT = 80

    @classmethod
    def setUpClass(cls) -> None:
        cls.deployment = Deployment(apply=True)
        cls.outcomes = []
        for index in range(cls.COUNT):
            hour, minute = 8 + index // 60, index % 60
            item_id = f"V{index:03d}"
            cls.deployment.arrive(
                item_id=item_id,
                name=f"Voice 260806_{hour:02d}{minute:02d}30.m4a",
                created_at="2026-08-06T12:00:00Z",
            )
            cls.outcomes.append(cls.deployment.walk(item_id))
        cls.day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.deployment.close()

    def test_every_one_of_them_was_published(self) -> None:
        self.assertEqual([o.result for o in self.outcomes], [RESULT_DONE] * self.COUNT)
        self.assertEqual(len(self.deployment.drive.written), self.COUNT * 3)

    def test_none_of_them_was_deferred_held_or_quarantined(self) -> None:
        for index in range(self.COUNT):
            row = self.deployment.row(f"V{index:03d}")
            self.assertEqual(row.state, State.DONE)
            self.assertEqual(row.attempts, 0, "a recording was retried by the naming path")
            self.assertNotIn("retry_at", row.meta,
                             "a recording was put on a backoff by a question about its title")

    def test_every_one_of_them_was_named(self) -> None:
        # The point of the burst test: this is the load, not a load of refusals that cost
        # nothing. All eighty went the whole way through the rule.
        named = [self.deployment.decision(f"V{i:03d}").get("name") for i in range(self.COUNT)]
        for name in named:
            self.assertTrue(str(name).startswith(EXPECTED_SITE_NAME), name)

    def test_the_eighty_names_are_eighty_different_names(self) -> None:
        """What the time in the name buys, on the day it matters most.

        Eighty recordings arrive in one morning, all from the same site — a burst across
        eight staff is a normal Monday. Without the time they would be eighty documents in
        that site's correspondence log all titled ``CANTERBURY``, which is a log nobody can
        read and no way to say which one somebody means. With it they are eighty distinct
        titles in the order they were recorded.

        The files were never at risk of colliding — the output names carry a hash of the
        item id for exactly that reason — but the TITLES are what a person reads, and they
        were.
        """
        named = [self.deployment.decision(f"V{i:03d}").get("name") for i in range(self.COUNT)]
        self.assertEqual(len(set(named)), self.COUNT)
        # And they sort into the order they happened, because the stamp ends the name.
        self.assertEqual(named, sorted(named))

    def test_no_recording_is_waiting_on_a_person(self) -> None:
        # Nothing in this feature asks him anything, by design. A queue of eighty questions
        # is the outcome the whole "two outcomes and no third" rule exists to prevent.
        for index in range(self.COUNT):
            stored = self.deployment.decision(f"V{index:03d}")
            self.assertTrue(stored.get("decided"))
            self.assertNotIn("pending", str(stored.get("code")))

    def _report_of_the_eighty(self) -> dict[str, Any]:
        """The report the morning email is meant to print for this day.

        Assembled from the rows themselves rather than through
        :func:`transcriber.digest.naming_report`, because that call cannot reach them today
        — see :class:`TheMorningEmailCannotReportAnyOfThisYet`. The shape is exactly the one
        ``naming_report`` documents, so what is asserted below is the email's own behaviour
        under eighty rows, which is what has to hold whichever way the rows arrive.
        """
        rows = []
        for index in range(self.COUNT):
            item_id = f"V{index:03d}"
            entry = self.deployment.decision(item_id)
            entry["item_id"] = item_id
            entry["source_name"] = self.deployment.row(item_id).name
            rows.append(entry)
        return {
            "book": sitebook.load(SITE_BOOK).line(),
            "applying": True,
            "eligible": len(rows),
            "named": sum(1 for r in rows if r.get("name")),
            "rows": rows,
        }

    def test_the_morning_email_prints_five_of_them_and_a_count(self) -> None:
        body = "\n".join(digest._naming_lines(self._report_of_the_eighty()))
        self.assertEqual(body.count("->"), 5, "the email listed more than five recordings")
        self.assertIn(f"...and {self.COUNT - 5} more", body,
                      "eighty became five and silence, which is how a burst day hides")

    def test_the_naming_section_stays_a_readable_size(self) -> None:
        lines = digest._naming_lines(self._report_of_the_eighty())
        self.assertLess(len(lines), 40,
                        "eighty recordings must not push the failures off the top of the email")

    def test_the_email_names_the_site_the_way_a_person_would_say_it(self) -> None:
        body = "\n".join(digest._naming_lines(self._report_of_the_eighty()))
        # Never the record's slug. ``canterbury-square`` in his morning email is exactly the
        # kind of thing that makes a person stop reading it.
        self.assertNotIn(EXPECTED_SITE, body)
        self.assertIn(EXPECTED_SITE_NAME, body)

    def test_the_email_says_nothing_was_renamed_when_nothing_was(self) -> None:
        # From the DECISIONS, not from today's setting. He may have switched it on this
        # morning; yesterday's recordings were still only being watched, and telling him
        # they were renamed sends him looking in the record for a document that is not
        # there. The reverse is worse: a rename reported as a suggestion is a change he does
        # not know he made.
        base = self._report_of_the_eighty()
        rows = [dict(row, applied=False) for row in base["rows"]]
        body = "\n".join(digest._naming_lines(dict(base, applying=False, rows=rows)))

        self.assertIn("Nothing has been renamed", body)
        self.assertIn("would call it", body)
        self.assertNotIn("named it", body)

    def test_one_email_goes_out_and_only_one(self) -> None:
        sent: list[Any] = []

        class _Server:
            def __enter__(self_inner) -> Any:
                return self_inner

            def __exit__(self_inner, *exc: Any) -> None:
                return None

            def send_message(self_inner, message: Any) -> None:
                sent.append(message)

            def starttls(self_inner, *a: Any, **k: Any) -> None:
                return None

            def login(self_inner, *a: Any, **k: Any) -> None:
                return None

            def ehlo(self_inner, *a: Any, **k: Any) -> None:
                return None

        now = time.time()
        first = digest.run(self.deployment.config, self.deployment.ledger, day=self.day,
                           now=now, smtp_factory=lambda *a, **k: _Server())
        self.assertTrue(first.sent.ok, first.sent.detail)
        self.assertEqual(len(sent), 1, "one recipient, one email")

        # And the day is marked, so the worker's next cycle does not send a second one.
        self.assertFalse(
            digest.should_run(self.deployment.config, self.deployment.ledger, now=now + 60),
            "a burst of recordings produced a second morning email",
        )


# ############################################################################
# ###  FAILING ON PURPOSE — A DEFECT IN THE SHIPPED CODE, NOT IN THE TEST  ###
# ############################################################################


class TheMorningEmailCannotReportAnyOfThisYet(unittest.TestCase):
    """**These two tests fail, and the code is what is wrong.**

    ``Ledger.naming_for_day`` calls ``self._connect()``. There is no such method on
    :class:`transcriber.ledger.Ledger` — every other reader in that file uses
    ``self._conn()`` — so the call raises ``AttributeError`` on every invocation, for every
    day, always. ``src/transcriber/ledger.py``, the ``with self._connect() as conn:`` line
    inside ``naming_for_day``.

    It is silent. Its one caller, :func:`transcriber.digest.naming_report`, wraps it in
    ``except Exception``, logs a warning nobody reads and carries on with an empty list — so
    the morning email prints "Nothing came in under the voice recorder's own name" on the
    morning eighty came in under the voice recorder's own name.

    What that costs: the feature ships **reporting and not applying**, deliberately, because
    nobody has measured how often it fires or how often it is right and there is no corpus
    to measure it against. The morning email IS the measurement. With this line as it
    stands, the shipped configuration produces no measurement at all, indefinitely, while
    looking exactly like a feature that is running and finding nothing — and the decision to
    arm ``NAMING_APPLY`` would then be taken on the estimate the two-boolean design exists
    to avoid.

    Nothing is lost, delayed or misnamed by it: the decisions are on the rows, the
    recordings publish, and the fix is one word.
    """

    def setUp(self) -> None:
        self.deployment = Deployment(apply=True)
        self.addCleanup(self.deployment.close)
        self.deployment.arrive()
        self.deployment.walk()
        self.day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    def test_the_ledger_can_read_back_the_naming_decisions_for_a_day(self) -> None:
        decisions = self.deployment.ledger.naming_for_day(self.day)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].get("name"), EXPECTED_NAME)
        self.assertEqual(decisions[0].get("source_name"), RECORDER_DEFAULT)

    def test_the_morning_email_reports_the_recording_that_was_named(self) -> None:
        report = digest.naming_report(self.deployment.config, self.deployment.ledger,
                                      day=self.day)
        self.assertEqual(
            report["eligible"], 1,
            "the morning email reported nothing about a recording that WAS named. This is "
            "the whole measurement the shipped default exists to produce.",
        )
        self.assertEqual(report["named"], 1)


class TheRuleDoesNotJudgeTheFileThatIsPublished(unittest.TestCase):
    """**This test fails, and the code is what is wrong.**

    Both modules state the same contract in the same words. :mod:`transcriber.autoname`:
    "``render`` takes a candidate name and returns *the exact bytes the record will be
    handed*, so N5 and N9 run against reality rather than against a model of it."
    :mod:`transcriber.sitebook`: "the record's rules are vendored here, verbatim, and run
    over *the exact bytes it will be handed*."

    They are not the same bytes. ``Pipeline._name`` builds the probe with
    ``self._context(row, parsed, gate.transcript, gate.extraction, info, held=...)`` and
    passes no ``notes``, while ``_publish`` a dozen lines later passes
    ``notes=_engine_notes(engine_meta) + gate.notes``. Those notes are rendered into the
    transcript by ``outputs._all_notes``, so the file the naming rule scored and the file
    the record is handed differ by every one of them.

    Reachable on any recording that was split for the engine, on any that was transcribed
    with settings stripped, and on any the sensitivity gate wrote a note for — which on a
    long site walk is a normal Tuesday, not an edge case.

    What it costs today: nothing measurable, because the notes are service prose and none
    of them carries a term the record recognises. What it costs the moment one does — a note
    that names the supplier, echoes the filename, or quotes a passage — is the whole point
    of N9: the one check that asks the record instead of reasoning about it would be asking
    it about a file that is not the one being filed, and it would answer "nothing changes"
    about a change it never saw. The fix is to pass the same notes to both.
    """

    def test_the_bytes_the_rule_scored_are_the_bytes_that_were_published(self) -> None:
        deployment = Deployment(apply=False, transcript=Transcript(
            text=" ".join(SPOKEN_LINES),
            segments=_transcript().segments,
            language="en-ZA",
            engine="test-engine",
            # The engine refused some of its settings, so the published transcript carries a
            # note saying so. Ordinary, and enough to separate the two renders.
            engine_metadata={"degraded": True},
        ))
        self.addCleanup(deployment.close)
        deployment.arrive()

        scored: list[str] = []
        real_decide = autoname.decide

        def capture(**kwargs: Any) -> Any:
            scored.append(kwargs["render"](""))
            return real_decide(**kwargs)

        with mock.patch.object(autoname, "decide", side_effect=capture):
            deployment.walk()

        published = deployment.drive.by_kind()["transcript"]
        self.assertEqual(len(scored), 1)
        # No name was applied, so the published file IS ``render("")``. Anything that
        # differs is something the rule never saw the record score.
        self.assertEqual(
            scored[0], published,
            "the naming rule scored a different file from the one that was published",
        )


# ============================================================ 8. eligibility, from the top


class OnlyTheRecordersOwnNameIsEverTouched(unittest.TestCase):
    """The first rule, and the one that protects everything he has already named himself.

    ``CJ.m4a``, ``Q.m4a``, ``JORDS.m4a`` and ``Morne Interview.m4a`` look nameless to a
    machine and are not — they are what he calls those people. A service that renames what a
    person chose is a service he turns off, and turning it off is how recordings get lost
    again.
    """

    HIS_OWN_NAMES = (
        "BEACH COURT SITE WALK 270826.m4a",
        "CJ.m4a",
        "Q.m4a",
        "JORDS.m4a",
        "Morne Interview.m4a",
        "Call Carel_260824_091500.m4a",
        "VOICE NOTE FOR CAREL.m4a",
        "voice 260806_162219.m4a",              # lower case: not the recorder's shape
        "Voice 260806_162219 CANTERBURY.m4a",   # he started naming it and it uploaded
    )

    def test_none_of_the_names_he_typed_is_the_recorders_default(self) -> None:
        for name in self.HIS_OWN_NAMES:
            with self.subTest(name):
                stem = os.path.splitext(name)[0]
                self.assertFalse(autoname.is_recorder_default(stem))

    def test_a_recording_he_named_is_published_untouched(self) -> None:
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.arrive(item_id="H1", name="BEACH COURT SITE WALK 270826.m4a",
                          created_at="2026-08-27T09:00:00Z")
        outcome = deployment.walk("H1")

        self.assertEqual(outcome.result, RESULT_DONE, outcome.reason)
        self.assertEqual(len(deployment.drive.written), 3)
        stored = deployment.decision("H1")
        self.assertEqual(stored.get("code"), "E1", "he named this one himself")
        self.assertEqual(stored.get("name"), "")
        self.assertIn("BEACH COURT SITE WALK 270826",
                      deployment.drive.by_kind()["transcript"].splitlines()[0])

    def test_a_day_with_none_of_them_says_nothing_at_all(self) -> None:
        # Most recordings are his own names, and reporting them would be a daily list of
        # non-events. The state a daily line used to guard — the site list having quietly
        # stopped being written — is guarded instead by the fault line, which prints every
        # morning until somebody fixes it. See test_naming_is_visible.
        lines = digest._naming_lines(
            {"book": sitebook.load(SITE_BOOK).line(), "applying": False,
             "eligible": 0, "named": 0, "rows": []}
        )
        body = "\n".join(lines)
        self.assertEqual(body.strip(), "")
        self.assertEqual(body, "", "a healthy site list with nothing to report says nothing")

    def test_a_recording_too_short_to_judge_is_published_with_no_name(self) -> None:
        # A short recording of wind noise comes back as the engine repeating itself, which
        # is indistinguishable from a site being named twice, early, in the first line.
        deployment = Deployment(apply=True, min_seconds=600)
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.walk()

        _published_once(self, deployment)
        stored = deployment.decision()
        self.assertEqual(stored.get("code"), "E4")
        self.assertEqual(stored.get("name"), "")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()


# ================================ 21. the moment is the row's, not the running build's


class TheRecordedMomentIsPinnedToTheRow(unittest.TestCase):
    """The output filenames open with when the recording was made, so it may never move.

    Found by an adversarial pass against the commit that introduced it, which is the worst
    place to find it: **this very change moved that moment**, for exactly the recordings it
    targets. An unnamed recording used to be dated by when OneDrive finished receiving it;
    it is now dated by the recorder's own clock in the filename. Hours apart, and often
    across midnight.

    So a recording that was in flight when the service restarted onto the new build would
    resume, work the moment out again from the new code, and write three files under three
    new names — while the three it had already written stayed in OneDrive. Nothing can
    clean those up. The ledger row has been overwritten with the new names; the collision
    guard only looks at other rows; there is no delete in the Graph client; and the sweep
    never enumerates an output folder. Downstream the record keys a document on its date
    and its bytes, so it logs a **second document, in a different month folder**, with a
    second row in that site's correspondence log.

    The commit message for the change that introduced it says, of the output filenames:
    *"a name that could change between attempts leaves three files nobody can delete and a
    second document in the record."* It was right, and it had done it.

    The moment is now pinned on the row the first time it is worked out, on the same write
    that stores the naming decision and for the same reason.
    """

    def _row_meta(self, deployment: Deployment) -> dict[str, Any]:
        return dict(deployment.row().meta or {})

    def test_the_moment_is_written_to_the_row(self) -> None:
        deployment = Deployment()
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.walk()

        meta = self._row_meta(deployment)
        self.assertIn("recorded_at", meta)
        # The recorder's clock from the filename, not when OneDrive received it.
        self.assertTrue(str(meta["recorded_at"]).startswith("2026-08-06T16:22:19"))

    def test_a_republish_under_a_changed_rule_writes_the_same_three_names(self) -> None:
        """The regression itself: the parser changes its mind, the filenames do not."""
        deployment = Deployment()
        self.addCleanup(deployment.close)
        deployment.arrive()
        deployment.walk()

        first = sorted(deployment.drive.names)
        self.assertTrue(first, "nothing was published, so this proves nothing")

        # A future build that reads the moment differently — which is precisely what the
        # change introducing this test was. Anything that alters resolve_timestamp for this
        # population stands in the same place.
        moved = datetime.datetime(2011, 1, 1, 1, 1, 1, tzinfo=naming.SAST)
        # `transcriber requeue` — the remedy the morning email itself tells him to run, and
        # the on-demand half of the same hazard.
        deployment.drive.written.clear()
        deployment.ledger.requeue(deployment.row().item_id, "a person re-ran it")
        with mock.patch.object(naming, "resolve_timestamp",
                               return_value=(moved, "a different rule entirely")):
            deployment.reboot().walk()

        self.assertEqual(
            sorted(deployment.drive.names), first,
            "the recording republished under different filenames, orphaning the three "
            "already in OneDrive and putting a second document in the record",
        )
        self.assertNotIn("2011", " ".join(deployment.drive.names))


class TurningNamingOffDoesNotRewriteWhatWasAlreadyPublished(unittest.TestCase):
    """The switch he is most likely to reach for must not republish under a new title.

    He sees a title he does not like at 06:00 and sets ``NAMING=0``. Any recording still in
    flight — a publish that got the transcript up and failed on the summary — resumes, and
    before this fix the flag was read *above* the stored decision: the transcript already in
    OneDrive under ``Subject: CANTERBURY`` was replaced in place with different bytes, and
    the stored ``{"applied": true, "name": "CANTERBURY"}`` was overwritten with
    ``{"code": "off"}`` — destroying the only evidence that the first one was ever
    published. The record keys a document on its bytes, so that is a second logged document
    and a second correspondence row, one filed under the site and one not.

    Note the asymmetry that made it easy to miss: flipping ``NAMING_APPLY`` in either
    direction was correctly sticky. Only ``NAMING`` overrode an answer that had already
    reached OneDrive.
    """

    def test_a_decision_that_reached_onedrive_survives_the_switch(self) -> None:
        deployment = Deployment(apply=True)
        self.addCleanup(deployment.close)
        deployment.drive.refuse_after = 1     # the transcript lands; nothing else does
        deployment.arrive()
        with contextlib.suppress(Exception):
            deployment.walk()

        published = deployment.drive.subject()
        stored = deployment.decision()
        self.assertEqual(stored.get("name"), EXPECTED_NAME)
        self.assertIn(EXPECTED_NAME, published)

        # He switches it off overnight, with this recording still unfinished.
        deployment.config = replace(deployment.config, naming=False)
        deployment.drive.refuse_after = None
        deployment.drive.written.clear()
        deployment.reboot().walk()

        self.assertIn(
            EXPECTED_NAME, deployment.drive.subject(),
            "the transcript was republished under a different subject line, which the "
            "record reads as a second document for one recording",
        )
        self.assertEqual(deployment.decision().get("name"), EXPECTED_NAME,
                         "the record of what was published was overwritten")
