"""Where a worked-out name is allowed to go, and everywhere it is not.

:mod:`transcriber.autoname` decides what to call a recording that arrived under the voice
recorder's own default name. This file is about what happens to that answer afterwards: it
reaches the ``Subject:`` line and the ``# `` heading of three markdown files, and it reaches
**nothing else at all**. Three separate disasters live in the gap between those two claims.

**One — the output filename.** ``OutputContext.names`` must be a pure function of the
recording: the moment, the source stem, OneDrive's copy marker and the Graph item id. If a
worked-out name could reach it, then a publish that failed halfway and retried the next
morning — with a newer site list, or a book that failed to load that night — would write its
three files under *different* names. The first attempt's files are already in OneDrive and
this service has no delete, so what is left is six files for one recording, three of them
undeletable strays, and a second document in the record that nothing can reconcile with the
first. :class:`TheOutputFilenameNeverDependsOnTheName` is the anti-regression for that whole
class, and it is parameterised rather than exemplary on purpose.

**Two — the record mis-reading the file.** ``kbc-site-memory/tools/ingest.py:parse_texty``
reads a header block of six recognised keys terminated by the first blank line. A seventh
key is not an error there: it is **silently deleted**, reaching neither the header nor the
body. So the obvious place to put a worked-out name — a ``Name:`` line up with the subject —
is the one place it would vanish without a symptom. The name goes in the subject, and the
sentence explaining it goes in the *body*. Asserted here against a vendored copy of the
record's real parser, not against what we meant.

**Three — the name moving the filing.** The subject is part of the bytes the record scores
to decide which site a note belongs to. Measured below on the real 56-site record: a body
binding cleanly to Milton Court binds to **nothing at all** once ``CANTERBURY`` is in its
subject line, and is *stolen* by Canterbury Square once ``CANTERBURY SQUARE`` is. That is
what the whole naming rule is defending, and this file checks the defence at the far end —
on the exact bytes that were rendered.

Everything here renders real files through the real renderers. Nothing is mocked except
OneDrive itself.
"""

from __future__ import annotations

import difflib
import importlib.util
import inspect
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from transcriber import archive, autoname, naming, outputs, sitebook
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, ExtractedItem, Segment, State, Transcript
from transcriber.outputs import OutputContext
from transcriber.pipeline import Pipeline, _ItemFault

from . import support
from .vendored_ingest import parse_texty

SAST = timezone(timedelta(hours=2), "SAST")

#: The one filename this service may ever rename. Every other name he typed himself, and
#: E1 refuses it before a site is even looked at.
RECORDER_DEFAULT = "Voice 260806_162219.m4a"

#: A site walk at a real site in the real record, said the way he says it. "Milton" is a
#: discriminating term in the record's own vocabulary; "court" is not, because the record
#: drops any term it uses of more than two sites.
SPOKEN = (
    "Right, I'm at Milton Court now, walking the north elevation.",
    "The sheeting was never sealed at the ridge, so it is coming in at unit four.",
    "I told Carel we would get a price for the remedial before the end of the month.",
    "Back at Milton Court on Thursday to look at the scaffolding again.",
)

#: What :mod:`transcriber.autoname` would propose for that walk: the span's own words,
#: upper-cased. Written out rather than derived, so a change to the decision rule does not
#: quietly change what this file is asserting about the renderers.
WORKED_OUT_NAME = "MILTON COURT"


# --------------------------------------------------------------------------- scaffolding


def transcript_of(lines=SPOKEN, step: float = 30.0) -> Transcript:
    """A transcript as the engines actually return one: one segment per line.

    Segments rather than bare prose because the published body is built from them, and the
    published body is what both the naming rule and the record read.
    """
    segments = [
        Segment(start=i * step, end=i * step + step - 1.0, speaker="James", text=line)
        for i, line in enumerate(lines)
    ]
    return Transcript(
        text=" ".join(lines),
        segments=segments,
        language="en-ZA",
        engine="test-engine",
        duration_s=len(segments) * step,
    )


def extraction_of(site: str = "Milton Court") -> Any:
    return support.StubExtraction(
        summary="He walked Milton Court and spoke to Carel about a roof leak.",
        proposals=[
            support.StubProposal(
                "commitments",
                ExtractedItem(
                    kind="commitment",
                    text="James was said to be doing this: get a price for the remedial",
                    quote="I told Carel we would get a price for the remedial before the "
                          "end of the month.",
                    quote_verified=True,
                    speaker="James",
                    site=site,
                    due="before the end of the month",
                    confidence=0.8,
                ),
            )
        ],
        site=site,
        site_quote="Right, I'm at Milton Court now",
    )


def context(display_name: str = "", *, source_name: str = RECORDER_DEFAULT,
            item_id: str = "01ABCDEF", **overrides: Any) -> OutputContext:
    """The context the pipeline renders all three files from, for one recording."""
    parsed = naming.parse_source_name(source_name)
    fields: dict[str, Any] = {
        "item_id": item_id,
        "source_name": parsed.original_name,
        "parsed": parsed,
        "recorded_at": datetime(2026, 8, 6, 16, 22, 19, tzinfo=SAST),
        "timestamp_source": parsed.timestamp_note,
        "transcript": transcript_of(),
        "extraction": extraction_of(),
        "audio": support.audio_info(754.0),
        "content_hash": "a" * 64,
        "graph_hash": "QUlDSA==",
        "web_url": "https://example.invalid/items/01ABCDEF",
        "engine": "test-engine",
        "display_name": display_name,
    }
    fields.update(overrides)
    return OutputContext(**fields)


def subject_of(text: str) -> str:
    """The Subject: value, as the record reads it off the first line."""
    return text.split("\n", 1)[0][len("Subject: "):]


def heading_of(text: str) -> str:
    """The ``# `` line — what a person sees at the top of the file in OneDrive."""
    return [line for line in text.split("\n") if line.startswith("# ")][0]


def name_rows(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("- Name: ")]


def changed_lines(before: str, after: str) -> tuple[list[str], list[str]]:
    """Every line that left and every line that arrived. No context, no near-misses."""
    old, new = before.split("\n"), after.split("\n")
    gone: list[str] = []
    arrived: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old, b=new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        gone.extend(old[i1:i2])
        arrived.extend(new[j1:j2])
    return gone, arrived


# --- vendored from kbc-site-memory/tools/gen_common.py:clean, as of 2026-08-28 ----------
#
# Copied rather than imported, under the discipline ``tests/vendored_ingest.py`` states:
# that repository is read-only to this service and is not on the path in CI, and an import
# would make this suite pass or fail depending on a checkout that is not ours. Behaviour
# intact, including the ellipsis and the rstrip.
#
# ``ingest.py`` writes every correspondence row as ``clean(item['subject'], 90)``. That is
# where our ninety comes from, and this is the function that decides whether our subject is
# the thing that gets cut.

def record_clean(s, n=None):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    if not n or len(s) <= n:
        return s
    cut = s[:n]
    if len(s) > n and not s[n].isspace():
        sp = cut.rfind(" ")
        if sp > 0:
            cut = cut[:sp]
        else:
            nxt = s.find(" ", n)          # no earlier boundary: keep the whole token
            cut = s[:nxt] if nxt > 0 else s
    return cut.rstrip(" ,;:-") + "…"


# --------------------------------------------------------------------------- the record

#: Where the record is checked out. Overridable so this suite can be pointed at a copy,
#: never at a fixture: the value of the binding assertions is that the vocabulary is his.
RECORD = os.environ.get("KBC_SITE_MEMORY", "/home/user/kbc-site-memory")


def real_site_book(tmpdir: str) -> sitebook.SiteBook:
    """The real 56 sites, projected by the same script the cron job runs.

    Loaded through ``ops/build-site-book.py`` rather than reimplemented, so these tests
    measure the vocabulary the running service actually sees.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    builder_path = os.path.join(here, "ops", "build-site-book.py")
    spine_path = os.path.join(RECORD, "build", "spine.json")
    if not os.path.exists(builder_path) or not os.path.exists(spine_path):
        raise unittest.SkipTest(
            f"the record is not checked out at {RECORD!r}, so there is no real site "
            f"vocabulary to test against. Set KBC_SITE_MEMORY to a checkout. These "
            f"assertions are worthless against an invented site list and are not run "
            f"against one."
        )
    spec = importlib.util.spec_from_file_location("ops_build_site_book", builder_path)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    with open(spine_path, "r", encoding="utf-8") as handle:
        spine = json.load(handle)
    target = os.path.join(tmpdir, "sites.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(builder.project(spine), handle, ensure_ascii=False)
    return sitebook.load(target)


# --------------------------------------------------------------------------- fake drive


class Drive:
    """A OneDrive that replaces by name, exactly as the real one does.

    Keyed by name rather than appended to, because "upload replaces the file of that name
    and hands back the same driveItem id" is the behaviour the whole output-name guarantee
    is built on. A fake that appended would make a second publish look harmless.
    """

    def __init__(self) -> None:
        self.by_name: dict[str, Any] = {}
        self.items: dict[str, Any] = {}
        self.uploads: list[str] = []

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        self.uploads.append(name)
        existing = self.by_name.get(name)
        item_id = existing.id if existing is not None else f"out-{len(self.by_name)}"
        item = type("Item", (), {
            "id": item_id, "name": name, "size": len(data),
            "web_url": f"https://example.invalid/{item_id}",
            "text": data.decode("utf-8"), "is_deleted": False, "is_folder": False,
        })()
        self.by_name[name] = item
        self.items[item_id] = item
        return item

    def get_item(self, item_id: str) -> Any:
        return self.items[item_id]

    def list_children(self, folder_id: str) -> list[Any]:
        return list(self.by_name.values())

    def text_of(self, kind: str) -> str:
        for name, item in self.by_name.items():
            if kind == "summary" and name.endswith("-summary.md"):
                return item.text
            if kind == "actions" and name.endswith("-actions.md"):
                return item.text
            if kind == "transcript" and not name.startswith("_"):
                return item.text
        raise KeyError(kind)


class Route:
    """Only what :func:`archive._outputs_confirmed` reads off a route."""

    def __init__(self, output_folder_id: str = "OUTPUT") -> None:
        self.output_folder_id = output_folder_id
        self.name = "default"
        self.display = "the default route"


# ============================================================ 1. the output filename


class TheOutputFilenameNeverDependsOnTheName(unittest.TestCase):
    """The permanent-duplicate class, closed off at the only place it can open.

    A publish is recovered by writing the same three names again. If the names could move
    between attempts, a half-failed publish leaves three files nobody can delete — this
    service has no delete, by design — and the record gains a second document for one
    recording with nothing tying the two together.
    """

    #: Every shape a worked-out name can take, plus several it never will. The point is that
    #: the answer does not depend on the input at all, so the input range is deliberately
    #: wider than what ``autoname`` can produce.
    NAMES = (
        "",
        WORKED_OUT_NAME,
        "CANTERBURY SQUARE",
        "22 CHEPSTOW",
        "A" * autoname._MAX_NAME,
        "MILTON COURT NORTH ELEVATION REMEDIAL WATERPROOFING PACKAGE",
        "  padded  ",
        "with | a pipe",
        "with **bold**",
        "Voice 260806_162219",
    )

    def test_the_three_output_names_are_byte_identical_whatever_the_name_is(self) -> None:
        baseline = context("").names.as_tuple()
        for candidate in self.NAMES:
            with self.subTest(display_name=candidate):
                self.assertEqual(
                    context(candidate).names.as_tuple(), baseline,
                    "the output filename moved with the title. A publish retried after a "
                    "partial failure would then write three NEW files beside three "
                    "undeletable old ones, and the record would hold two documents for one "
                    "recording.",
                )

    def test_the_output_name_function_cannot_be_given_a_name_at_all(self) -> None:
        """Structural, not behavioural: there is no parameter to pass it through.

        A behavioural check can be satisfied by a caller that happens not to pass the name
        today. This fails the moment somebody adds the parameter, which is the moment the
        mistake becomes possible rather than the moment it is made.
        """
        for function in (naming.output_stem, naming.output_names):
            with self.subTest(function=function.__name__):
                parameters = set(inspect.signature(function).parameters)
                self.assertNotIn("display_name", parameters)
                self.assertEqual(
                    parameters, {"when", "stem", "copy_marker", "item_id"},
                    "the output name is a function of the recording and nothing else",
                )

    def test_the_stem_is_the_filename_he_uploaded_not_the_title(self) -> None:
        """The output folder sorts by moment and reads as the source folder does. A title
        in the filename would also mean two attempts could disagree about it."""
        names = context(WORKED_OUT_NAME).names
        for name in names.as_tuple():
            with self.subTest(name=name):
                self.assertIn("Voice 260806_162219", name)
                self.assertNotIn("MILTON", name.upper())

    def test_a_second_attempt_after_a_half_failed_publish_writes_the_same_three_files(self) -> None:
        """The scenario the stickiness rule exists for, run through a real publish.

        First attempt: nothing was named — the site list had not been rebuilt. Second
        attempt the next morning: a name. Both write the same three names, so the second
        replaces the first in place and OneDrive is left holding three files, not six.
        """
        drive = Drive()
        first = outputs.publish(drive, "OUTPUT", context(""))
        second = outputs.publish(drive, "OUTPUT", context(WORKED_OUT_NAME))

        self.assertEqual(first.names, second.names)
        self.assertEqual(len(drive.uploads), 6, "two publishes, three files each")
        self.assertEqual(
            len(drive.by_name), 3,
            "the second publish created new files instead of replacing the first "
            "publish's. Three of the six are now strays this service cannot delete.",
        )
        self.assertEqual(
            first.item_ids, second.item_ids,
            "the same names must come back as the same driveItems, or the ledger's "
            "recorded ids point at files that are no longer the outputs",
        )
        # And the second attempt's title is the one that survived.
        self.assertIn(WORKED_OUT_NAME, subject_of(drive.text_of("transcript")))


# ============================================================ 2. two lines and one row


class ANameChangesThreeLinesAndNothingElse(unittest.TestCase):
    """Diffed line for line against the same recording with no name.

    Anything else that moved is something a person did not ask for and would not look for:
    a changed body line changes the bytes the record scores, and a changed provenance row
    changes what the file claims about itself.
    """

    def setUp(self) -> None:
        self.plain = outputs.render_all(context(""))
        self.named = outputs.render_all(context(WORKED_OUT_NAME))
        self.assertEqual([f.kind for f in self.plain], ["transcript", "summary", "actions"])

    def test_only_the_subject_the_heading_and_one_new_row_are_different(self) -> None:
        for before, after in zip(self.plain, self.named):
            with self.subTest(kind=before.kind):
                gone, arrived = changed_lines(before.text, after.text)
                self.assertEqual(len(gone), 2, gone)
                self.assertEqual(len(arrived), 3, arrived)
                self.assertTrue(gone[0].startswith("Subject: "))
                self.assertTrue(arrived[0].startswith("Subject: "))
                self.assertTrue(gone[1].startswith("# "))
                self.assertTrue(arrived[1].startswith("# "))
                self.assertTrue(
                    arrived[2].startswith("- Name: "),
                    "something other than the provenance row was added to a file a name "
                    "was applied to",
                )

    def test_the_file_is_exactly_one_line_longer(self) -> None:
        for before, after in zip(self.plain, self.named):
            with self.subTest(kind=before.kind):
                self.assertEqual(
                    len(after.text.split("\n")), len(before.text.split("\n")) + 1
                )

    def test_the_name_itself_appears_in_exactly_two_places(self) -> None:
        """The subject and the heading. The provenance row explains it without repeating it.

        Every further occurrence would be another copy of a title the machine invented,
        sitting in a file whose whole claim is that it is evidence of what was said.
        """
        for rendered in self.named:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(
                    rendered.text.count(WORKED_OUT_NAME), 2,
                    f"{WORKED_OUT_NAME!r} appears "
                    f"{rendered.text.count(WORKED_OUT_NAME)} times in the "
                    f"{rendered.kind}, not twice",
                )
                self.assertIn(WORKED_OUT_NAME, subject_of(rendered.text))
                self.assertIn(WORKED_OUT_NAME, heading_of(rendered.text))

    def test_the_words_of_the_recording_are_untouched(self) -> None:
        """The evidence is the evidence. A title may never edit it."""
        after_body = self.named[0].text.partition("## What was said")[2]
        before_body = self.plain[0].text.partition("## What was said")[2]
        self.assertEqual(after_body, before_body)
        self.assertIn(outputs.spoken_body(transcript_of()), after_body)

    def test_no_other_provenance_row_moves(self) -> None:
        """Recording, Recorded, Length, hashes, the OneDrive item — all unchanged.

        These are what a person checks a transcript against when they doubt it. A title
        that quietly rewrote one of them would make the file's own account of itself wrong.
        """
        for before, after in zip(self.plain, self.named):
            with self.subTest(kind=before.kind):
                rows_before = [l for l in before.text.split("\n") if l.startswith("- ")]
                rows_after = [l for l in after.text.split("\n") if l.startswith("- ")]
                self.assertEqual(
                    [r for r in rows_after if not r.startswith("- Name: ")], rows_before,
                    "a provenance row other than the new one changed, so the file's own "
                    "account of where it came from is no longer the same file's",
                )
                # And it sits among the other provenance rows rather than opening the file,
                # so it reads as one more fact about the recording and not as a banner.
                self.assertGreater(rows_after.index(name_rows(after.text)[0]), 0)

    def test_no_name_means_the_file_the_service_wrote_before_this_existed(self) -> None:
        """The second of the two outcomes, and the one that must stay exactly as it was."""
        for rendered in self.plain:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(name_rows(rendered.text), [])
                self.assertIn("Voice 260806_162219", subject_of(rendered.text))


# ============================================================ 3. the record still reads it


class TheRecordStillReadsTheFile(unittest.TestCase):
    """Through ``parse_texty`` itself, from a real file on disk, because that is the reader.

    Not "what we intended": what ``kbc-site-memory`` will actually do with the bytes.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.files = outputs.render_all(context(WORKED_OUT_NAME))

    def parse(self, rendered) -> dict:
        path = os.path.join(self.dir.name, rendered.name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rendered.text)
        return parse_texty(path)

    def test_a_named_file_is_still_read_as_a_note_and_not_an_email(self) -> None:
        """A ``From:`` reclassifies the whole file and attributes it to a sender who does
        not exist. Adding a title must not have gone anywhere near the header keys."""
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                parsed = self.parse(rendered)
                self.assertEqual(parsed["kind"], "transcript")
                self.assertEqual(parsed["from_addr"], "")
                self.assertEqual(parsed["from_name"], "")

    def test_the_header_block_is_still_subject_date_and_one_blank_line(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                lines = rendered.text.split("\n")
                self.assertTrue(lines[0].startswith("Subject: "))
                self.assertTrue(lines[1].startswith("Date: "))
                self.assertEqual(
                    lines[2], "",
                    "a third line above the blank one reaches neither the header nor the "
                    "body: the record deletes it and says nothing",
                )

    def test_the_record_reads_the_worked_out_name_as_the_subject(self) -> None:
        subject = self.parse(self.files[0])["subject"]
        self.assertTrue(subject.startswith(WORKED_OUT_NAME), subject)
        self.assertIn("voice note transcript", subject)

    def test_the_date_is_still_the_day_it_was_recorded(self) -> None:
        """Not the first of the month. That date becomes the item's id and its month folder
        in the record, so a title that disturbed the ``Date:`` line would file the recording
        somewhere nobody looks for it."""
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                self.assertEqual(self.parse(rendered)["date"], "2026-08-06")

    def test_the_provenance_row_is_in_the_body_where_the_record_can_see_it(self) -> None:
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                body = self.parse(rendered)["body"]
                rows = name_rows(body)
                self.assertEqual(len(rows), 1, rows)
                self.assertIn("chosen by this service", rows[0])

    def test_no_line_of_a_named_file_is_swallowed_by_the_header_scan(self) -> None:
        """The whole body, line for line, against what the record recovers.

        The failure this catches has no symptom anywhere else: a swallowed line is not an
        error downstream, it is simply gone.
        """
        for rendered in self.files:
            with self.subTest(kind=rendered.kind):
                _header, _, body = rendered.text.partition("\n\n")
                self.assertEqual(self.parse(rendered)["body"], body)

    def test_the_name_put_in_the_header_instead_would_be_silently_deleted(self) -> None:
        """Why the sentence is in the body: proof, on the record's own parser.

        A seventh header key is not rejected and does not warn. It reaches neither
        ``head`` nor ``body`` and disappears. This is the mistake somebody will reasonably
        make the next time this file is edited, so it is asserted rather than commented.
        """
        good = self.files[0].text
        row = name_rows(good)[0]
        # The row moved out of the body and up into the header block, which is exactly the
        # edit somebody makes when they decide the name is "metadata".
        moved = row[len("- "):]
        broken = "\n".join(
            line for line in good.replace("Date:", f"{moved}\nDate:", 1).split("\n")
            if line != row
        )

        path = os.path.join(self.dir.name, "name-in-the-header.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(broken)
        swallowed = parse_texty(path)

        self.assertNotIn("name", swallowed, "it reached no header key")
        self.assertNotIn(
            "chosen by this service", swallowed["body"],
            "and it reached no body line either — the record ate it without a word",
        )
        # Our own contract check does complain, offline, before anything is uploaded.
        problems = outputs.check_contract(broken)
        self.assertTrue(
            any("disappears without erroring" in p for p in problems), problems
        )


# ============================================================ 4. the contract still holds


class TheOutputContractStillPassesWithANameApplied(unittest.TestCase):
    """Every existing mechanical guard, re-run with a title in the subject line."""

    def test_check_contract_is_clean_on_all_three_named_files(self) -> None:
        for rendered in outputs.render_all(context(WORKED_OUT_NAME)):
            with self.subTest(kind=rendered.kind):
                self.assertEqual(outputs.check_contract(rendered.text), [])

    def test_check_contract_still_sees_the_subject_it_was_told_to_expect(self) -> None:
        """``_finalise`` passes the subject it built; a title that did not survive
        rendering intact would fail here rather than reach OneDrive."""
        for rendered in outputs.render_all(context(WORKED_OUT_NAME)):
            with self.subTest(kind=rendered.kind):
                self.assertEqual(
                    outputs.check_contract(
                        rendered.text,
                        expected_subject=subject_of(rendered.text),
                        expected_date="2026-08-06",
                    ),
                    [],
                )

    def test_the_three_names_are_still_names_this_service_may_write(self) -> None:
        names = context(WORKED_OUT_NAME).names.as_tuple()
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(outputs.check_name(name), [])
        self.assertEqual(len(set(names)), 3)

    def test_a_name_does_not_smuggle_an_address_past_the_subject_scrub(self) -> None:
        """The subject is the one surface the body's scrub cannot reach, so it has its own.

        Not reachable from ``autoname`` today — its shape rule has no ``@`` in it — but the
        subject is built from ``ctx.label``, and ``label`` is whatever it is handed.
        """
        rendered = outputs.render_transcript(context("carel at example dot co dot za"))

        self.assertEqual(outputs.check_contract(rendered), [])
        self.assertEqual(
            subject_of(rendered), "[address removed] — voice note transcript",
            "an address in the title reached the Subject line, where the body's scrub "
            "cannot see it and the record's own address check does not look",
        )
        self.assertIn("removed from this text", rendered, "and the removal is stated")

    def test_no_title_the_rule_can_produce_leaves_the_subject_saying_nothing(self) -> None:
        """A title of nothing but spaces publishes a row nobody can identify.

        ``OutputContext.label`` asks ``if self.display_name``, not ``if
        self.display_name.strip()``, so a title of ``"   "`` is truthy, wins over the
        filename, and collapses to the empty string in ``_one_line``. The subject then reads
        ``— voice note transcript``, the file passes every contract check, and the record
        writes a correspondence row for the site with no way at all to tell which recording
        it came from.

        It is not reachable from the decision rule **because of the shape rule asserted
        here**, and that is the whole point of asserting it: ``_NAME_SHAPE`` requires at
        least one alphanumeric character, so the day somebody widens it — to allow a comma,
        an ampersand, a hyphenated site — this fails first, in the place that says what it
        would cost.
        """
        for blank in ("", " ", "   ", "\t", "\n", " \t "):
            with self.subTest(candidate=blank):
                self.assertIsNone(
                    autoname._NAME_SHAPE.fullmatch(blank),
                    f"{blank!r} would be accepted as a name, and it renders a subject line "
                    f"that identifies no recording",
                )


# ============================================================ 5. the subject line's limit


class ALongNameStillFitsTheSubjectLine(unittest.TestCase):
    """``ingest.py`` writes every correspondence row as ``clean(subject, 90)``.

    Two things must hold at the maximum name length. The subject must stay inside ninety, so
    the record is never the thing that cuts it; and the suffix must survive, because the
    suffix is the only thing telling the three files apart. Reserving the suffix *before*
    the label is cut is what does it — appending it afterwards gave three identical
    subjects for one site walk, with no way to tell the evidence from the machine's reading
    of it.
    """

    #: Names at or near ``autoname``'s ceiling, in the shape speech actually produces:
    #: several words, none of them long. Built from real site titles in the record.
    LONG_NAMES = (
        "MILTON COURT NORTH ELEVATION REMEDIAL WATERPROOFING PACKAGE",   # 59
        "CANTERBURY SQUARE PHASE TWO REMEDIAL AND SCAFFOLDING WORKS",    # 58
        "GARDEN ROUTE MALL ROOF REMEDIALS AND STORM DAMAGE MAKE GOOD",   # 59
        "THE OVAL COLLINGTON AND FERNWOOD OFFICE WINDOW REPLACEMENT",    # 58
    )

    def test_the_fixtures_really_are_at_the_ceiling(self) -> None:
        """Otherwise this whole class quietly tests short names."""
        for name in self.LONG_NAMES:
            with self.subTest(name=name):
                self.assertLessEqual(len(name), autoname._MAX_NAME)
                self.assertGreaterEqual(len(name), autoname._MAX_NAME - 3)

    def test_every_subject_stays_inside_the_records_ninety_characters(self) -> None:
        for name in self.LONG_NAMES:
            for rendered in outputs.render_all(context(name)):
                with self.subTest(name=name, kind=rendered.kind):
                    self.assertLessEqual(len(subject_of(rendered.text)), 90)

    def test_the_record_would_not_truncate_our_subject(self) -> None:
        """Asserted with the record's own ``clean``, not with a length check of our own.

        If the record truncates the subject it appends an ellipsis and drops the suffix —
        and the correspondence row for a site walk then reads as a sentence cut in half.
        """
        for name in self.LONG_NAMES:
            for rendered in outputs.render_all(context(name)):
                with self.subTest(name=name, kind=rendered.kind):
                    subject = subject_of(rendered.text)
                    self.assertEqual(record_clean(subject, 90), subject)

    def test_a_long_name_is_never_cut_in_the_middle_of_a_word(self) -> None:
        """A name sliced mid-token reads as a whole one and is the name of nothing.

        ``MILTON COURT NORTH ELEVATION REMEDIAL WATERPROOFING PACKAGE`` cut to
        ``...WATERPROO`` is a title a person would read straight past. The record's own
        ``clean`` backtracks for exactly this reason; so does ours.
        """
        for name in self.LONG_NAMES:
            for rendered in outputs.render_all(context(name)):
                with self.subTest(name=name, kind=rendered.kind):
                    shown = subject_of(rendered.text).split(" — ")[0]
                    self.assertTrue(
                        name.startswith(shown),
                        f"{shown!r} is not a prefix of {name!r} at all",
                    )
                    if shown != name:
                        self.assertTrue(
                            name[len(shown)] == " ",
                            f"{shown!r} stops in the middle of a word of {name!r}",
                        )

    def test_the_three_subjects_still_tell_the_three_files_apart(self) -> None:
        """Two rows in the correspondence log that read the same are worse than one long
        one: nobody can tell which is the evidence."""
        for name in self.LONG_NAMES:
            with self.subTest(name=name):
                subjects = [
                    subject_of(f.text) for f in outputs.render_all(context(name))
                ]
                self.assertEqual(len(set(subjects)), 3, subjects)
                for subject, suffix in zip(subjects, (
                    "voice note transcript",
                    "voice note summary",
                    "proposals to confirm (nothing decided)",
                )):
                    self.assertTrue(
                        subject.endswith(f" — {suffix}"),
                        f"the suffix was the thing that got cut: {subject!r}",
                    )


# ============================================================ 6. the provenance row


class TheProvenanceRowNamesTheFileInOneDrive(unittest.TestCase):
    """The title is no longer the filename, so the file says which file it is.

    Without it, a person looking at ``MILTON COURT — voice note transcript`` in a folder of
    files called ``Voice 260806_162219`` has two names for one thing and no way to know
    which is wrong. Nothing in OneDrive is ever renamed by this service.
    """

    def test_the_row_names_the_recording_as_onedrive_has_it(self) -> None:
        rendered = outputs.render_transcript(context(WORKED_OUT_NAME))
        row = name_rows(rendered)[0]
        self.assertIn(repr(RECORDER_DEFAULT), row)
        self.assertIn("still called", row)

    def test_the_row_and_the_recording_row_name_the_same_file(self) -> None:
        """Two provenance rows naming two different files would be worse than one naming
        none: a reader cannot tell which of them is the recording."""
        rendered = outputs.render_transcript(context(WORKED_OUT_NAME))
        recording = [l for l in rendered.split("\n") if l.startswith("- Recording: ")][0]
        stated = recording[len("- Recording: "):]
        self.assertEqual(stated, RECORDER_DEFAULT)
        self.assertIn(repr(stated), name_rows(rendered)[0])

    def test_a_re_uploaded_duplicate_names_itself_with_its_copy_marker(self) -> None:
        """His phone re-uploads after an interrupted sync, so ``/CALLS`` holds both
        ``Voice 260806_162219.m4a`` and ``Voice 260806_162219 (1).m4a``. They are two
        different recordings. The row has to name the one this file came from, marker and
        all, or it points at the other recording's audio.
        """
        duplicate = "Voice 260806_162219 (1).m4a"
        parsed = naming.parse_source_name(duplicate)
        self.assertEqual(parsed.copy_marker, 1)
        self.assertTrue(
            autoname.is_recorder_default(parsed.stem),
            "a re-upload is still an unnamed recording and is still eligible for a name",
        )

        rendered = outputs.render_transcript(
            context(WORKED_OUT_NAME, source_name=duplicate, item_id="01DUPLICATE")
        )
        self.assertIn(repr(duplicate), name_rows(rendered)[0])
        self.assertIn("-copy1-", context("", source_name=duplicate).names.transcript)

    def test_the_row_is_absent_when_nothing_was_worked_out(self) -> None:
        """No name is not a lesser name; it is the file this service wrote before the
        feature existed, and it must not gain a row explaining an absence."""
        for rendered in outputs.render_all(context("")):
            with self.subTest(kind=rendered.kind):
                self.assertEqual(name_rows(rendered.text), [])
                self.assertNotIn("chosen by this service", rendered.text)


# ============================================================ 7. the ledger and the archive


class TheLedgerStillPointsAtWhatWasUploaded(unittest.TestCase):
    """The archive moves an original only once all three outputs are confirmed present.

    It confirms them by the names on the ledger row. If a worked-out name had reached those
    names, or the ledger had recorded anything but what was uploaded, the check would fail
    quietly for ever: the original would never age out of ``/CALLS`` and nobody would be
    told why.
    """

    def setUp(self) -> None:
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.close)
        self.drive = Drive()
        self.route = Route()
        self.ledger.upsert_discovered(
            DriveItem(item_id="01ABCDEF", name=RECORDER_DEFAULT, size=4096)
        )

    def publish_and_record(self, display_name: str, *, record_ids: bool = True):
        ctx = context(display_name)
        result = outputs.publish(self.drive, "OUTPUT", ctx)
        self.ledger.advance(
            "01ABCDEF", State.DONE,
            transcript_name=result.names.get("transcript"),
            summary_name=result.names.get("summary"),
            actions_name=result.names.get("actions"),
            output_item_ids=result.item_ids if record_ids else {},
        )
        return result, self.ledger.get("01ABCDEF")

    def test_the_ledger_names_are_the_names_that_were_uploaded(self) -> None:
        result, row = self.publish_and_record(WORKED_OUT_NAME)
        self.assertEqual(
            [row.transcript_name, row.summary_name, row.actions_name],
            [result.names["transcript"], result.names["summary"], result.names["actions"]],
        )
        for name in (row.transcript_name, row.summary_name, row.actions_name):
            with self.subTest(name=name):
                self.assertIn(
                    name, self.drive.by_name,
                    "the ledger names a file that is not in OneDrive, so the archive can "
                    "never confirm this recording and the original never ages out",
                )

    def test_the_ledger_names_are_the_same_whether_or_not_a_name_was_applied(self) -> None:
        _result, named_row = self.publish_and_record(WORKED_OUT_NAME)
        unnamed = context("").names
        self.assertEqual(named_row.transcript_name, unnamed.transcript)
        self.assertEqual(named_row.summary_name, unnamed.summary)
        self.assertEqual(named_row.actions_name, unnamed.actions)

    def test_the_archive_confirms_the_outputs_of_a_named_recording(self) -> None:
        _result, row = self.publish_and_record(WORKED_OUT_NAME)
        confirmed, detail = archive._outputs_confirmed(self.drive, row, self.route)
        self.assertTrue(confirmed, detail)
        self.assertIn("3 outputs confirmed", detail)

    def test_the_archives_named_output_check_still_finds_them_without_recorded_ids(self) -> None:
        """The fallback path: no driveItem id was stored, so the archive looks in the
        route's output folder **by name**. That is the check a worked-out name would break
        if it had ever reached the filename."""
        _result, row = self.publish_and_record(WORKED_OUT_NAME, record_ids=False)
        self.assertEqual(row.output_item_ids, {})
        confirmed, detail = archive._outputs_confirmed(self.drive, row, self.route)
        self.assertTrue(confirmed, detail)

    def test_an_output_that_is_not_there_is_still_refused(self) -> None:
        """Proof the confirmation has teeth rather than passing on everything."""
        _result, _row = self.publish_and_record(WORKED_OUT_NAME, record_ids=False)
        self.ledger.set_fields("01ABCDEF", transcript_name="20260806-162219-not-written.md")
        row = self.ledger.get("01ABCDEF")
        confirmed, detail = archive._outputs_confirmed(self.drive, row, self.route)
        self.assertFalse(confirmed)
        self.assertIn("not confirmed", detail)


# ============================================================ 8. the collision guard


class TwoRecordingsNeverShareAnOutputName(unittest.TestCase):
    """``_refuse_name_collision`` is the backstop under the whole output-name guarantee.

    An upload replaces by name and hands back the same driveItem id, so a collision is
    invisible to the read-back, to the sweep and to the archive: both rows say DONE and one
    recording's transcript no longer exists anywhere. Since the names now carry the item id
    this can only fire on a genuine bug — which is exactly when it is wanted, and exactly
    why it must not have been weakened by anything the naming feature did.
    """

    def setUp(self) -> None:
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.close)
        self.pipeline = Pipeline(support.make_config(), self.ledger, None, owner="worker-A")
        self.ledger.upsert_discovered(DriveItem(item_id="01ABCDEF", name=RECORDER_DEFAULT))
        self.row = self.ledger.get("01ABCDEF")

    def test_a_recordings_own_names_are_never_a_collision(self) -> None:
        for display_name in ("", WORKED_OUT_NAME):
            with self.subTest(display_name=display_name):
                self.pipeline._refuse_name_collision(self.row, context(display_name))

    def test_another_recordings_name_is_refused_even_when_this_one_was_named(self) -> None:
        ctx = context(WORKED_OUT_NAME)
        self.ledger.upsert_discovered(DriveItem(item_id="01OTHER", name="something.m4a"))
        self.ledger.advance(
            "01OTHER", State.DONE,
            transcript_name=ctx.names.transcript,
            summary_name=ctx.names.summary,
            actions_name=ctx.names.actions,
        )
        with self.assertRaises(_ItemFault) as raised:
            self.pipeline._refuse_name_collision(self.row, ctx)
        self.assertIn("01OTHER", str(raised.exception))
        self.assertIn("Nothing was uploaded", str(raised.exception))

    def test_the_guard_checks_the_same_three_names_whatever_the_title(self) -> None:
        """It reads ``ctx.names``. If a title could move those, the guard would be looking
        for a collision on names that are not the ones about to be written."""
        ctx = context(WORKED_OUT_NAME)
        self.ledger.upsert_discovered(DriveItem(item_id="01OTHER", name="something.m4a"))
        self.ledger.advance(
            "01OTHER", State.DONE, transcript_name=context("").names.transcript
        )
        with self.assertRaises(_ItemFault):
            self.pipeline._refuse_name_collision(self.row, ctx)


# ============================================================ 9. the real record


class TheRealRecordStillFilesItUnderTheSameSite(unittest.TestCase):
    """Measured on the 56 real sites, on the exact bytes the record would be handed.

    The subject line is part of what the record scores. This is the far end of the check
    ``autoname`` makes at N9 — asserted here on the published file rather than on a probe,
    because the published file is the one that gets filed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="naming-output-contract-")
        cls.book = real_site_book(cls._tmp.name)
        if cls.book.size < 50:
            raise unittest.SkipTest(f"only {cls.book.size} sites loaded; not the real one")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_worked_out_name_leaves_the_filing_exactly_where_it_was(self) -> None:
        plain = outputs.render_transcript(context(""))
        named = outputs.render_transcript(context(WORKED_OUT_NAME))

        before, _ = self.book.bind(plain)
        after, _ = self.book.bind(named)
        self.assertEqual(before, "milton-court-sea-point")
        self.assertEqual(
            after, before,
            "a title worked out from the site spoken in the recording moved which site the "
            "record files the note under. That is the one thing a name must never do.",
        )

    def test_the_hazard_this_is_guarding_is_real_and_not_theoretical(self) -> None:
        """Without this, the assertion above passes for a rule that does nothing.

        A body that binds cleanly to Milton Court binds to **nothing at all** once
        ``CANTERBURY`` is in its subject line — the two sites tie at 2, and a tie is not an
        answer — and is filed under Canterbury Square outright once ``CANTERBURY SQUARE``
        is. Milton Court's record loses the walk either way, and nobody is told.
        """
        clean, _ = self.book.bind(outputs.render_transcript(context("")))
        self.assertEqual(clean, "milton-court-sea-point")

        tied, _ = self.book.bind(outputs.render_transcript(context("CANTERBURY")))
        self.assertIsNone(tied, "the unfiling hazard has stopped being reproducible")

        stolen, _ = self.book.bind(outputs.render_transcript(context("CANTERBURY SQUARE")))
        self.assertEqual(stolen, "canterbury-square")

    def test_the_only_bytes_a_name_adds_are_the_two_lines_and_the_row(self) -> None:
        """Why the filing cannot move: the name is added where N3 has already proved its
        words are, and the provenance row it brings names only the recorder's own filename,
        which carries no term the record recognises."""
        added = changed_lines(
            outputs.render_transcript(context("")),
            outputs.render_transcript(context(WORKED_OUT_NAME)),
        )[1]
        self.assertEqual(
            self.book.sites_named_by(added[2]), frozenset(),
            "the provenance row a name brings with it names a site to the record. It would "
            "then score that site on every named recording, whatever the recording is "
            "about.",
        )


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
