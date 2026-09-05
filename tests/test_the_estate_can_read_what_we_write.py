"""The hand-over to closea, checked against closea's own contract rather than our belief.

⛔ THE FAILURE THIS EXISTS TO PREVENT WAS MEASURED, ON THIS EXACT SEAM. The service's three
markdown files were run through every one of closea's four ingestion sources. Not one filed
a record: three of them do not read ``.md`` at all and said nothing whatsoever — nought
collected, nought refused — and the fourth read all three and refused all three. A hand-over
that produces silence on both sides is the worst shape available: a nightly run reports a
perfect morning for ever while the estate receives nothing.

⭐ SO THE CONTRACT IS PINNED AS DATA HERE, and where a closea checkout is on the machine the
tests below read ITS OWN SOURCE and compare. That is the half that keeps the pin honest: a
field renamed over there breaks a test here rather than breaking every envelope this service
has ever written, silently, at the moment somebody finally connects the two.

⚠ AND IT SKIPS RATHER THAN FAILS WITHOUT THAT CHECKOUT — the opposite of the choice the mail
service made, deliberately. That service is developed alongside closea and its suite may
reasonably demand one. This service has no closea dependency at all: it runs, ships and is
tested on machines where closea does not exist, and a suite that went red on those machines
would be red for a reason that is not a defect. What protects us instead is that the pinned
values below are checked on EVERY run, so an envelope can never drift from the pin, and the
pin can only drift from closea where somebody has a checkout — which CI does not, and a
developer touching this seam does.
"""

from __future__ import annotations

import ast
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests import support
from transcriber import estate, naming, outputs
from transcriber.ledger import State
from transcriber.models import Segment, Transcript

#: Where a closea checkout would be, in the order this looks.
CLOSEA_PATHS = ("../closea", "../../closea")
CLOSEA_VARIABLE = "TRANSCRIBER_CLOSEA_PATH"

SPOKEN = (
    "your consumption is going to be wrong on Lonehill Shopping Centre. "
    "the Beach Court Mouille Point trustees want the roof done. "
    "the scaffold on the east face is signposted unsafe and still in use."
)


def find_closea() -> Path | None:
    named = os.environ.get(CLOSEA_VARIABLE, "").strip()
    if named:
        p = Path(named).expanduser()
        return p if (p / "closea" / "ingest" / "drop.py").is_file() else None
    here = Path(__file__).resolve().parents[1]
    for rel in CLOSEA_PATHS:
        p = (here / rel).resolve()
        if (p / "closea" / "ingest" / "drop.py").is_file():
            return p
    return None


def a_context() -> outputs.OutputContext:
    parsed = naming.parse_source_name("HQ SITE WALK 030926.m4a")
    when = datetime.datetime(2026, 9, 3, 14, 40, tzinfo=datetime.timezone.utc)
    return outputs.OutputContext(
        item_id="01ITEMID", source_name="HQ SITE WALK 030926.m4a",
        parsed=parsed, recorded_at=when, timestamp_source="the file's own time",
        transcript=Transcript(text=SPOKEN,
                              segments=[Segment(start=0.0, end=60.0, text=SPOKEN)],
                              engine="openai", duration_s=1860.0),
        extraction=support.StubExtraction(summary="A walk of the HQ roof."),
        audio=support.audio_info(duration_s=1860.0),
        engine="openai",
    )


class TheEnvelopeIsTheShapeTheEstateAccepts(unittest.TestCase):
    """Every refusal in closea's reader, checked from this side before it can fire."""

    def setUp(self) -> None:
        self.env = estate.envelope(a_context(), "01ITEMID")
        self.item = self.env[estate.ITEM_KEY]

    def test_the_schema_is_stamped_and_is_the_one_it_reads(self) -> None:
        """An unstamped or unknown version is refused whole, not parsed best-effort."""
        self.assertEqual(self.env["schema"], estate.SCHEMA)

    def test_the_item_carries_exactly_the_fields_and_no_others(self) -> None:
        """Checked BOTH ways on their side: an extra key is a refusal, a missing key is a
        refusal. So both directions are checked here too."""
        self.assertEqual(sorted(self.item), sorted(estate.ITEM_FIELDS))

    def test_the_field_that_only_they_can_state_is_absent(self) -> None:
        """`source_fingerprint` is a hash of the bytes THEY read. An envelope naming it is
        refused as an unknown field — the whole envelope, not the field."""
        self.assertNotIn("source_fingerprint", self.item)

    def test_site_is_null_because_a_recording_cannot_know(self) -> None:
        """A non-null `site` MOVES the record into a site's folder. Which job this belongs
        to is a judgement over the whole estate, and the estate is not here."""
        self.assertIsNone(self.item["site"])

    def test_revises_is_null_because_it_retires_a_live_record(self) -> None:
        """The one field that can withdraw something already filed. A recording is evidence
        of what somebody said; it is never an instruction to retire a record."""
        self.assertIsNone(self.item["revises"])

    def test_the_identity_is_the_drive_item_id_and_is_prefixed(self) -> None:
        """Stable across every retry, and prefixed so no other connector can collide."""
        self.assertEqual(self.item["source_id"], "recording:01ITEMID")

    def test_a_second_envelope_for_the_same_recording_is_byte_identical(self) -> None:
        """What makes their second pass file nothing. Two renders of one recording must not
        differ, or every re-run re-files the same walk."""
        again = estate.envelope(a_context(), "01ITEMID")
        self.assertEqual(json.dumps(self.env, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_the_lists_are_lists_because_json_has_no_tuple(self) -> None:
        """They convert these back to tuples on arrival. A tuple here would serialise as a
        list anyway; asserting it keeps the intent visible."""
        self.assertIsInstance(self.item["participants"], list)
        self.assertIsInstance(self.item["tags"], list)


class TheDialogueDoesNotGoIntoTheEstate(unittest.TestCase):
    """closea's own words: the estate holds what the business knows, not what was said."""

    def setUp(self) -> None:
        self.body = estate.envelope(a_context(), "01ITEMID")[estate.ITEM_KEY]["body"]

    def test_the_reading_travels(self) -> None:
        self.assertIn("A walk of the HQ roof.", self.body)

    def test_the_transcript_is_named_so_the_evidence_is_reachable(self) -> None:
        """A reading with no route back to the words behind it is an assertion."""
        self.assertIn("HQ SITE WALK 030926.m4a", self.body)
        self.assertIn("## Where the evidence is", self.body)

    def test_a_recording_nothing_was_read_from_still_travels_and_says_so(self) -> None:
        """Silence would be indistinguishable from a hand-over that never happened."""
        ctx = a_context()
        object.__setattr__(ctx, "extraction", support.StubExtraction(summary=""))
        body = estate.body_for(ctx)
        self.assertIn("Nothing was read out of this recording", body)


class NothingHereCanCostARecording(unittest.TestCase):
    """The three files are already published and confirmed before this runs."""

    def test_an_unwritable_folder_raises_here_and_is_caught_by_the_caller(self) -> None:
        """Proving it raises is half of it; `pipeline._hand_to_the_estate` catches every
        exception and advances the ledger anyway, which its own docstring states."""
        with tempfile.TemporaryDirectory() as tmp:
            wall = os.path.join(tmp, "wall")
            with open(wall, "w", encoding="utf-8") as fh:
                fh.write("not a directory")
            with self.assertRaises(OSError):
                estate.write(os.path.join(wall, "drop"), a_context(), "01ITEMID")

    def test_the_write_is_atomic_so_a_reader_never_sees_half_an_envelope(self) -> None:
        """Their honest response to a truncated file is 'unreadable', which turns our slow
        disk into their lost recording. The temporary name is hidden from `*.json`."""
        with tempfile.TemporaryDirectory() as tmp:
            written = estate.write(tmp, a_context(), "01ITEMID")
            self.assertTrue(written.endswith("01ITEMID.json"))
            self.assertEqual([p.name for p in Path(tmp).glob("*.json")], ["01ITEMID.json"])
            self.assertEqual(json.loads(Path(written).read_text(encoding="utf-8"))["schema"],
                             estate.SCHEMA)


class ThePinStillMatchesCloseasOwnSource(unittest.TestCase):
    """Read closea's source and compare. Skipped where no checkout is on this machine —
    see the module docstring for why this one skips where the mail service's fails."""

    def setUp(self) -> None:
        self.closea = find_closea()
        if self.closea is None:
            self.skipTest(
                "no closea checkout beside this one; set "
                f"{CLOSEA_VARIABLE} to compare against its real source. The pinned values "
                "are still checked on every run by the tests above."
            )

    def _const(self, module: str, name: str):
        tree = ast.parse((self.closea / "closea" / "ingest" / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name) and target.id == name and value is not None:
                return ast.literal_eval(value)
        raise LookupError(f"{name} is not declared in closea/ingest/{module}")

    def test_the_schema_string_is_still_the_one_they_accept(self) -> None:
        self.assertEqual(estate.SCHEMA, self._const("drop.py", "SCHEMA"))

    def test_the_field_list_is_still_theirs_exactly(self) -> None:
        self.assertEqual(tuple(estate.ITEM_FIELDS), tuple(self._const("drop.py", "ITEM_FIELDS")))

    def test_the_field_we_must_not_send_is_still_the_one_we_omit(self) -> None:
        withheld = tuple(self._const("drop.py", "NOT_ON_THE_WIRE"))
        for name in withheld:
            self.assertNotIn(name, estate.ITEM_FIELDS)

    def test_our_kind_is_one_their_record_layer_accepts(self) -> None:
        """A kind outside their closed set does not refuse one file — it raises out of
        their planning pass and the whole batch files nothing."""
        tree = ast.parse((self.closea / "closea" / "memory" / "record.py").read_text(encoding="utf-8"))
        kinds = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                t = node.targets[0]
                if isinstance(t, ast.Name) and t.id == "KINDS":
                    kinds = ast.literal_eval(node.value)
        self.assertIsNotNone(kinds, "closea no longer declares KINDS where this looks")
        self.assertIn(estate.KIND, kinds)


class TheirOwnReaderFilesWhatWeWrite(unittest.TestCase):
    """The claim, driven. Everything above is shape; this is the estate actually reading it."""

    def setUp(self) -> None:
        self.closea = find_closea()
        if self.closea is None:
            self.skipTest(f"no closea checkout beside this one; set {CLOSEA_VARIABLE}")
        self.added = [p for p in (str(self.closea), str(self.closea / "tools"))
                      if p not in sys.path]
        for p in self.added:
            sys.path.insert(0, p)
        try:
            import closea.ingest.drop  # noqa: F401
        except Exception as exc:  # pragma: no cover - a checkout we cannot import
            for p in self.added:
                sys.path.remove(p)
            self.skipTest(f"closea is there but will not import: {exc}")

    def tearDown(self) -> None:
        for p in getattr(self, "added", []):
            if p in sys.path:
                sys.path.remove(p)

    def test_it_collects_and_files_our_envelope_and_refuses_nothing(self) -> None:
        from closea.estate import EstateRoot
        from closea.ingest.base import Ingestor
        from closea.ingest.drop import ItemDropSource

        with tempfile.TemporaryDirectory() as tmp:
            drop = os.path.join(tmp, "drop")
            estate.write(drop, a_context(), "01ITEMID")

            source = ItemDropSource(drop)
            items = source.collect()
            self.assertEqual(source.problems, [], f"the estate refused it: {source.problems}")
            self.assertEqual(len(items), 1)

            root = EstateRoot.create(Path(tmp) / "estate")
            report = Ingestor(root).run(ItemDropSource(drop))
            self.assertEqual(len(report.created), 1, report.summary())
            self.assertEqual(report.problems, [], report.summary())

            # The second run is the one that matters: an identity that drifts files the
            # same recording again on every pass.
            again = Ingestor(root).run(ItemDropSource(drop))
            self.assertFalse(again.created, f"filed twice: {again.summary()}")

    def test_what_it_filed_carries_the_reading_and_not_the_dialogue(self) -> None:
        from closea.estate import EstateRoot
        from closea.ingest.base import Ingestor
        from closea.ingest.drop import ItemDropSource

        with tempfile.TemporaryDirectory() as tmp:
            drop = os.path.join(tmp, "drop")
            estate.write(drop, a_context(), "01ITEMID")
            root = EstateRoot.create(Path(tmp) / "estate")
            Ingestor(root).run(ItemDropSource(drop))
            filed = "\n".join(p.read_text(encoding="utf-8")
                              for p in Path(tmp, "estate").rglob("*.md"))
            self.assertIn("A walk of the HQ roof.", filed)
            self.assertNotIn("the scaffold on the east face is signposted", filed)


class TheProductionPathActuallyWritesOne(unittest.TestCase):
    """The real pipeline, over a real recording, writing a real envelope.

    ⛔ EVERY TEST ABOVE BUILDS ITS OWN `OutputContext` AND CALLS `estate` DIRECTLY, WHICH
    PROVES THE SHAPE AND NOT THE WIRING. `Pipeline._hand_to_the_estate` catches every
    exception on purpose — a drop folder must never cost a recording — and that is exactly
    what makes an unproven path dangerous here: let the context `_context` really builds
    drift from the attributes `estate.item_for` really reads, and the envelope stops being
    written, silently, with the row still finishing DONE. That is the both-sides-silent
    failure this whole change exists to remove, reintroduced one level up.

    So this drives the shipped `process_one` and looks in the folder afterwards.
    """

    def setUp(self) -> None:
        from tests.test_naming_never_loses_a_recording import Deployment

        self.tmp = tempfile.mkdtemp(prefix="estate-handover-")
        self.drop = os.path.join(self.tmp, "drop")
        self.dep = Deployment(directory=self.tmp)
        object.__setattr__(self.dep.config, "closea_drop", self.drop)
        self.dep.arrive()

    def tearDown(self) -> None:
        self.dep.close()

    def _envelopes(self) -> list[Path]:
        return sorted(Path(self.drop).glob("*.json")) if os.path.isdir(self.drop) else []

    def test_one_finished_recording_leaves_one_envelope(self) -> None:
        self.dep.walk()
        found = self._envelopes()
        self.assertEqual(len(found), 1, f"the shipped path wrote {len(found)} envelopes")
        payload = json.loads(found[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], estate.SCHEMA)
        self.assertEqual(sorted(payload[estate.ITEM_KEY]), sorted(estate.ITEM_FIELDS))
        self.assertTrue(payload[estate.ITEM_KEY]["body"].strip())

    def test_the_envelope_names_the_recording_the_way_the_files_do(self) -> None:
        """The estate and OneDrive must call one recording the same thing, or nobody can
        put the two together by looking."""
        self.dep.walk()
        item = json.loads(self._envelopes()[0].read_text(encoding="utf-8"))[estate.ITEM_KEY]
        self.assertTrue(item["title"].strip())
        self.assertTrue(item["origin"].strip())
        self.assertEqual(self.dep.row().transcript_name, item["origin"])

    def test_nothing_is_written_when_no_drop_is_configured(self) -> None:
        """Off unless configured, and off means no folder is created either."""
        object.__setattr__(self.dep.config, "closea_drop", "")
        self.dep.walk()
        self.assertFalse(os.path.exists(self.drop))

    def test_a_drop_that_cannot_be_written_still_finishes_the_recording(self) -> None:
        """The three files are already published and read back by the time this runs. A
        folder on a disk that has gone away is a hand-over that did not happen — never a
        recording that did not."""
        from transcriber import estate as estate_module

        def explode(*_a: object, **_k: object) -> str:
            raise OSError("the disk went away")

        original = estate_module.write
        estate_module.write = explode
        try:
            self.dep.walk()
        finally:
            estate_module.write = original
        self.assertEqual(self._envelopes(), [])
        self.assertEqual(str(self.dep.row().state), str(State.DONE))
        self.assertTrue(self.dep.row().transcript_name, "the three files still landed")


if __name__ == "__main__":
    unittest.main()
