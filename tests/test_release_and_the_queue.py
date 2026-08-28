"""What happens after a person answers, and how the queue is reported while they have not.

Two halves, and they are the two halves of the gate that are not the classifier.

**Release.** Nothing in this service, and nothing downstream of it, can edit a site file the
record has already built. So a released passage reaches the record the only way anything
reaches the record — as a source document. These tests hold that fourth file to exactly the
contract the transcript is held to, using the same vendored copy of the record's own parser:
it must be read as a transcript rather than an email, it must not be named so the record
skips it, it must carry the words, and it must not repeat the marker's own question, which
the record would otherwise harvest a second time onto the site's live page.

**The queue.** A gate with a wall of items in front of it is a gate he stops opening, and a
gate he stops opening does not fail safely — it swallows the record silently. So the morning
email escalates on age and never on anything else: a line after a day, the oldest named after
three, the subject line after a week, and nothing at all decided at any point. The other rule
tested here is decision 6 — a staff member reviews their own held passages, and his email
carries counts and sites and never a word of what was said.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
import unittest
from typing import Any

from tests import support
from tests.vendored_ingest import ADDR_RE, parse_texty
from transcriber import digest as digest_module
from transcriber import naming, redact, release
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, Route
from transcriber.withheld import Decision, HeldSpan, WithheldStore

ROUTE = Route(
    name="calls",
    label="Phone calls",
    source_folder_id="S-CALLS",
    output_folder_id="O-CALLS",
    archive_folder_id="",
    engine="",
    enabled=True,
)
OTHER_ROUTE = Route(
    name="site-meetings",
    label="Site meetings",
    source_folder_id="S-SITE",
    output_folder_id="O-SITE",
    archive_folder_id="",
    engine="",
    enabled=True,
)

TRANSCRIPT_NAME = "20260827-141500-Call Carel_260827_141500-a1b2c3d4.md"
HELD_WORDS = "his ID number is 8001015009087"
OTHER_WORDS = "Carel is off sick with something he asked me not to write down"

JAMES = "james@kbc.invalid"
SIPHO = "sipho@kbc.invalid"


class _Drive:
    """A OneDrive that remembers what was written into it, and can refuse once."""

    def __init__(self, fail_times: int = 0) -> None:
        self.written: list[tuple[str, str, str]] = []
        self.items: dict[str, Any] = {}
        self.fail_times = fail_times

    def upload(self, parent_id: str, name: str, data: bytes) -> Any:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("the drive was busy")
        text = data.decode("utf-8")
        # Replace by name, the way Graph's conflictBehavior=replace does, so a second
        # delivery of the same passage cannot leave two copies in the folder.
        self.written = [w for w in self.written if w[1] != name]
        self.written.append((parent_id, name, text))
        item = type(
            "Item",
            (),
            {"id": f"out-{name}", "name": name, "size": len(data), "web_url": ""},
        )()
        self.items[item.id] = item
        return item

    def get_item(self, item_id: str) -> Any:
        return self.items[item_id]

    def file(self, suffix: str) -> tuple[str, str, str] | None:
        for entry in self.written:
            if suffix in entry[1]:
                return entry
        return None


def _drive_item(item_id: str, name: str) -> DriveItem:
    return DriveItem.from_graph(
        {
            "id": item_id,
            "name": name,
            "size": 1024,
            "eTag": f'"{item_id}"',
            "file": {"mimeType": "audio/mp4", "hashes": {"sha256Hash": "AA"}},
            "parentReference": {"id": "S-CALLS"},
            "createdDateTime": "2026-08-27T12:15:00Z",
            "lastModifiedDateTime": "2026-08-27T12:16:00Z",
            "webUrl": "https://example.invalid/itm",
        }
    )


class _Fixture:
    """One recording, published, with one held passage on it and somewhere to write."""

    def __init__(
        self,
        tmp: str,
        *,
        mode: str = "on",
        routes: tuple[Route, ...] = (ROUTE,),
        held_at: str = "2026-08-27T12:20:00Z",
        drive: _Drive | None = None,
    ) -> None:
        self.config = support.make_config(
            ledger_path=os.path.join(tmp, "ledger.sqlite3"),
            gate_mode=mode,
            gate_held_store=os.path.join(tmp, "held.sqlite3"),
            gate_review_base_url="https://review.invalid",
            smtp_to=(JAMES,),
            routes=routes,
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.ledger.upsert_discovered(_drive_item("itm-1", "Call Carel_260827_141500.m4a"), "calls")
        self.ledger.advance("itm-1", "DONE", transcript_name=TRANSCRIPT_NAME)
        self.store = WithheldStore(self.config.gate_held_store)
        self.drive = drive if drive is not None else _Drive()
        self.releaser = release.Releaser(self.config, self.ledger, self.store, self.drive)
        self.held = self.store.hold(self._span(HELD_WORDS, "bare_identifier"), mode=mode, at=held_at)

    def _span(self, words: str, category: str, *, reviewer: str = JAMES) -> HeldSpan:
        return HeldSpan(
            item_id="itm-1",
            start=40,
            end=40 + len(words),
            text=words,
            category=category,
            route="calls",
            subject="an identity number" if category == "bare_identifier" else "",
            site="Beach Court",
            source_name="Call Carel_260827_141500.m4a",
            recorded_at="2026-08-27T12:15:00Z",
            recorded_by=reviewer,
            reviewer=reviewer,
            context_before="The new chap starts Monday, ",
            context_after=" so put him on the site register.",
        )

    def hold_another(self, words: str = OTHER_WORDS) -> Any:
        span = HeldSpan(
            item_id="itm-1",
            start=400,
            end=400 + len(words),
            text=words,
            category="do_not_write_down",
            route="calls",
            site="Beach Court",
            source_name="Call Carel_260827_141500.m4a",
            recorded_at="2026-08-27T12:15:00Z",
            recorded_by=JAMES,
            reviewer=JAMES,
        )
        return self.store.hold(span, mode="on")

    def release(self, record: Any = None, *, by: str = JAMES) -> Any:
        target = record if record is not None else self.held
        return self.store.release(target.hold_id, answered_by=by, note="fine to write it down")

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


# ------------------------------------------------------------------ the fourth file


class TheFourthFile(unittest.TestCase):
    """A released passage reaches the record as its own source document, or not at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="release-")
        self.addCleanup(self._tmp.cleanup)
        self.fix = _Fixture(self._tmp.name)
        self.addCleanup(self.fix.close)

    def _deliver(self) -> Any:
        released = self.fix.release()
        return self.fix.releaser.deliver(released)

    def test_it_is_written_into_the_route_output_folder(self) -> None:
        delivery = self._deliver()
        self.assertTrue(delivery.ok, delivery.detail)
        self.assertEqual(len(self.fix.drive.written), 1)
        parent, name, _text = self.fix.drive.written[0]
        self.assertEqual(parent, "O-CALLS")
        self.assertTrue(name.endswith(f"-released-{self.fix.held.ref}.md"), name)

    def test_it_goes_to_its_own_routes_folder_and_not_the_first_ones(self) -> None:
        fix = _Fixture(
            tempfile.mkdtemp(dir=self._tmp.name), routes=(OTHER_ROUTE, ROUTE)
        )
        self.addCleanup(fix.close)
        fix.releaser.deliver(fix.release())
        self.assertEqual(fix.drive.written[0][0], "O-CALLS")

    def test_the_record_reads_it_as_a_transcript_carrying_the_words(self) -> None:
        self._deliver()
        _parent, name, text = self.fix.drive.written[0]
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        parsed = parse_texty(path)
        self.assertEqual(parsed["kind"], "transcript")
        self.assertEqual(parsed["from_addr"], "")
        self.assertEqual(parsed["date"], "2026-08-27")
        self.assertIn(HELD_WORDS, parsed["body"])

    def test_it_is_not_named_so_the_records_intake_skips_it(self) -> None:
        # The summary and the actions file carry a leading underscore precisely so the
        # record never ingests them. This one has to be ingested or the release released
        # nothing at all.
        self._deliver()
        name = self.fix.drive.written[0][1]
        self.assertFalse(os.path.basename(name).startswith(naming.DERIVED_PREFIX))
        self.assertTrue(naming.is_output_name(name), name)

    def test_it_names_the_recording_and_the_passage_it_answers(self) -> None:
        self._deliver()
        text = self.fix.drive.written[0][2]
        self.assertIn(TRANSCRIPT_NAME, text)
        self.assertIn("Call Carel_260827_141500.m4a", text)
        self.assertIn(self.fix.held.ref, text)

    def test_it_does_not_repeat_the_markers_own_question(self) -> None:
        # The marker in the transcript is phrased as a stated unknown so the record's
        # question harvester carries it onto the site's live page. Repeating that sentence
        # here would put the same open question up twice, one of them already answered.
        self._deliver()
        text = self.fix.drive.written[0][2]
        self.assertEqual(redact.harvestable(text), "")
        marker = redact.marker_for(self.fix.held.as_span())
        self.assertNotEqual(redact.harvestable(marker), "")

    def test_it_carries_no_email_address_in_any_spelling(self) -> None:
        self._deliver()
        text = self.fix.drive.written[0][2]
        self.assertEqual(ADDR_RE.findall(text), [])
        self.assertNotIn(JAMES, text)
        self.assertIn("james", text)  # the person, named the way this service is allowed to

    def test_it_never_carries_a_passage_that_is_still_held(self) -> None:
        still = self.fix.hold_another(words=HELD_WORDS + " and also nobody released this")
        released = self.fix.release()
        with self.assertRaises(release.WouldLeakAnotherHold) as caught:
            release.render_release(
                released,
                transcript_name=TRANSCRIPT_NAME,
                still_held=(still.as_span(),),
            )
        self.assertIn(still.ref, caught.exception.refs)
        self.assertEqual(self.fix.drive.written, [])

    def test_a_second_hold_on_the_same_recording_is_not_in_the_file(self) -> None:
        still = self.fix.hold_another()
        self.fix.releaser.deliver(self.fix.release())
        text = self.fix.drive.written[0][2]
        self.assertNotIn(OTHER_WORDS, text)
        self.assertFalse(redact.contains_any_held(text, [still.as_span()]))

    def test_a_passage_nobody_released_has_no_file(self) -> None:
        with self.assertRaises(release.NotReleased):
            release.render_release(self.fix.held, transcript_name=TRANSCRIPT_NAME)

    def test_a_shadow_sighting_has_no_file_because_nothing_was_withheld(self) -> None:
        fix = _Fixture(tempfile.mkdtemp(dir=self._tmp.name), mode="shadow")
        self.addCleanup(fix.close)
        with self.assertRaises(release.NotReleased):
            release.render_release(fix.held, transcript_name=TRANSCRIPT_NAME)

    def test_it_carries_no_decided_by_field_because_nothing_here_decides(self) -> None:
        # A person gave permission for words to be written down. That is not a decision
        # about the job, and this pipeline may not emit the record's word for one in any
        # file it writes.
        self._deliver()
        text = self.fix.drive.written[0][2]
        self.assertNotIn("decided_by", text)
        self.assertIn("observed_by: agent", text)

    def test_the_reference_in_its_name_is_the_one_in_the_transcripts_marker(self) -> None:
        # The file is only an answer if a reader can tie it to the hole it fills.
        self._deliver()
        name = self.fix.drive.written[0][1]
        marker = redact.marker_for(self.fix.held.as_span())
        self.assertEqual(redact.refs_in(marker), (self.fix.held.ref,))
        self.assertIn(self.fix.held.ref, name)

    def test_a_shadow_sighting_is_answered_with_a_sentence_not_a_file(self) -> None:
        fix = _Fixture(tempfile.mkdtemp(dir=self._tmp.name), mode="shadow")
        self.addCleanup(fix.close)
        outcome = fix.releaser.deliver_quietly(fix.held)
        self.assertEqual(outcome.state, "nothing-to-write")
        self.assertIn("nothing was withheld", outcome.detail)
        self.assertEqual(fix.drive.written, [])


class ARefusal(unittest.TestCase):
    """A refusal writes nothing, and is a record all the same."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="refuse-")
        self.addCleanup(self._tmp.cleanup)
        self.fix = _Fixture(self._tmp.name)
        self.addCleanup(self.fix.close)
        refused = self.fix.store.refuse(
            self.fix.held.hold_id, answered_by=JAMES, note="no, leave that out"
        )
        self.delivery = self.fix.releaser.deliver(refused)

    def test_nothing_is_written_anywhere(self) -> None:
        self.assertEqual(self.fix.drive.written, [])
        self.assertEqual(self.delivery.state, "nothing-to-write")
        self.assertTrue(self.delivery.ok)

    def test_it_is_recorded_against_the_recording(self) -> None:
        answers = release.recorded_answers(self.fix.ledger, "itm-1")
        self.assertEqual(answers[self.fix.held.ref]["decision"], Decision.REFUSED)
        self.assertEqual(answers[self.fix.held.ref]["file"], "")

    def test_it_is_kept_as_a_refusal_rather_than_deleted(self) -> None:
        # A refused passage that had simply vanished would read, six months later, exactly
        # like a passage the classifier never caught. Those need opposite responses.
        again = self.fix.store.get(self.fix.held.hold_id)
        self.assertEqual(again.decision, Decision.REFUSED)
        self.assertEqual(again.answered_by, JAMES)
        self.assertEqual(again.text, HELD_WORDS)

    def test_the_marker_stays_in_the_transcript(self) -> None:
        text = f"Right. {redact.marker_for(self.fix.held.as_span())} And the bricks land Thursday."
        left, put_back = redact.restore_released(
            text, self.fix.store.released_for("itm-1")
        )
        self.assertEqual(put_back, ())
        self.assertEqual(left, text)
        self.assertNotIn(HELD_WORDS, left)

    def test_it_is_not_counted_as_a_delivery_still_owed(self) -> None:
        owed = release.outstanding(self.fix.store, self.fix.ledger)
        self.assertEqual(owed.count, 0)
        self.assertEqual(owed.problems, ())


class WhenTheDriveWillNotTakeIt(unittest.TestCase):
    """A decision a person made is never lost because a drive was busy."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="owed-")
        self.addCleanup(self._tmp.cleanup)
        self.fix = _Fixture(self._tmp.name, drive=_Drive(fail_times=1))
        self.addCleanup(self.fix.close)
        self.released = self.fix.release()
        self.first = self.fix.releaser.deliver_quietly(self.released)

    def test_the_failure_is_reported_rather_than_raised(self) -> None:
        self.assertFalse(self.first.ok)
        self.assertEqual(self.first.state, "failed")
        self.assertIn("the drive was busy", self.first.detail)

    def test_the_decision_still_stands(self) -> None:
        self.assertEqual(self.fix.store.get(self.fix.held.hold_id).decision, Decision.RELEASED)

    def test_the_delivery_is_owed_and_says_so_without_the_words(self) -> None:
        owed = release.outstanding(self.fix.store, self.fix.ledger)
        self.assertEqual(owed.count, 1)
        self.assertEqual(owed.refs, (self.fix.held.ref,))
        self.assertEqual(owed.sites, ("Beach Court",))
        self.assertNotIn(HELD_WORDS, "\n".join(owed.lines()))

    def test_running_it_again_finishes_the_job(self) -> None:
        done = self.fix.releaser.deliver_outstanding()
        self.assertEqual([d.state for d in done], ["written"])
        self.assertEqual(len(self.fix.drive.written), 1)
        self.assertEqual(release.outstanding(self.fix.store, self.fix.ledger).count, 0)

    def test_and_again_after_that_writes_nothing_further(self) -> None:
        self.fix.releaser.deliver_outstanding()
        again = self.fix.releaser.deliver_quietly(self.fix.store.get(self.fix.held.hold_id))
        self.assertEqual(again.state, "already-there")
        self.assertEqual(len(self.fix.drive.written), 1)


class WhenTheRouteHasGoneAway(unittest.TestCase):
    """A route taken out of the configuration is a sentence, not a file in the wrong folder."""

    def test_it_refuses_to_guess_where_the_words_go(self) -> None:
        tmp = tempfile.mkdtemp()
        fix = _Fixture(tmp, routes=(OTHER_ROUTE,))
        self.addCleanup(fix.close)
        outcome = fix.releaser.deliver_quietly(fix.release())
        self.assertFalse(outcome.ok)
        self.assertEqual(fix.drive.written, [])
        self.assertIn("calls", outcome.detail)
        self.assertEqual(fix.store.get(fix.held.hold_id).decision, Decision.RELEASED)


# ------------------------------------------------------------------ the morning email


def _held_at(days_ago: int, now: str = "2026-08-28T06:00:00Z") -> str:
    when = _dt.datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - _dt.timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class _Queue:
    """A held store with passages of chosen ages, and a config that reads it."""

    NOW = "2026-08-28T06:00:00Z"

    def __init__(self, tmp: str, mode: str = "on") -> None:
        self.config = support.make_config(
            ledger_path=os.path.join(tmp, "ledger.sqlite3"),
            gate_mode=mode,
            gate_held_store=os.path.join(tmp, "held.sqlite3"),
            gate_review_base_url="https://review.invalid",
            smtp_to=(JAMES,),
            routes=(ROUTE,),
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.store = WithheldStore(self.config.gate_held_store)
        self.mode = mode
        self._n = 0

    def add(
        self,
        *,
        days_ago: int,
        site: str = "Beach Court",
        reviewer: str = JAMES,
        category: str = "own_margin",
        words: str = "we raised R1.65m and we will land at R1.604m",
        subject: str = "our own position on the money",
    ) -> Any:
        self._n += 1
        span = HeldSpan(
            item_id=f"itm-{self._n}",
            start=0,
            end=len(words),
            text=words,
            category=category,
            route="calls",
            subject=subject,
            site=site,
            source_name=f"Call number {self._n}.m4a",
            recorded_at=_held_at(days_ago),
            recorded_by=reviewer,
            reviewer=reviewer,
        )
        return self.store.hold(span, mode=self.mode, at=_held_at(days_ago))

    def report(self) -> digest_module.HeldReport:
        return digest_module.held_report(
            self.config, self.ledger, store=self.store, day="2026-08-27", now=self.NOW
        )

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


class TheQueueEscalates(unittest.TestCase):
    """More specific every few days, never merely louder, and never decided."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="queue-")
        self.addCleanup(self._tmp.cleanup)
        self.queue = _Queue(self._tmp.name)
        self.addCleanup(self.queue.close)

    def test_on_the_day_it_is_a_count_and_a_site(self) -> None:
        self.queue.add(days_ago=0)
        report = self.queue.report()
        self.assertEqual(report.escalation, "none")
        self.assertEqual(report.subject_warning(), "")
        body = "\n".join(report.lines())
        self.assertIn("Beach Court", body)
        self.assertNotIn("The oldest has been waiting", body)

    def test_after_a_day_it_states_the_age(self) -> None:
        self.queue.add(days_ago=1)
        report = self.queue.report()
        self.assertEqual(report.escalation, "age")
        self.assertIn("The oldest has been waiting 1 day.", "\n".join(report.lines()))
        self.assertEqual(report.subject_warning(), "")

    def test_after_three_days_it_names_the_oldest(self) -> None:
        self.queue.add(days_ago=3)
        report = self.queue.report()
        self.assertEqual(report.escalation, "named")
        body = "\n".join(report.lines())
        self.assertIn(report.oldest_ref, body)
        self.assertIn("Beach Court", body)
        self.assertEqual(report.subject_warning(), "")

    def test_after_a_week_it_reaches_the_subject_line(self) -> None:
        self.queue.add(days_ago=8)
        report = self.queue.report()
        self.assertEqual(report.escalation, "subject")
        self.assertEqual(report.subject_warning(), "⚠ 1 passage(s) held, oldest 8 days")

    def test_each_step_adds_a_fact_rather_than_an_adjective(self) -> None:
        # The three steps are strictly nested: the sentence a longer wait produces contains
        # everything the shorter wait said and one thing more. A warning that only got
        # louder would be wallpaper by the third morning.
        for days, expected in ((1, "age"), (3, "named"), (8, "subject")):
            with tempfile.TemporaryDirectory() as tmp:
                queue = _Queue(tmp)
                queue.add(days_ago=days)
                report = queue.report()
                self.assertEqual(report.escalation, expected)
                body = "\n".join(report.lines())
                self.assertIn("The oldest has been waiting", body)
                if expected in ("named", "subject"):
                    self.assertIn(report.oldest_ref, body)
                queue.close()

    def test_nothing_is_released_or_discarded_however_old_it_gets(self) -> None:
        held = self.queue.add(days_ago=400)
        report = self.queue.report()
        self.assertEqual(report.pending, 1)
        again = self.queue.store.get(held.hold_id)
        self.assertEqual(again.decision, Decision.PENDING)
        self.assertEqual(again.text, "we raised R1.65m and we will land at R1.604m")
        flat = " ".join("\n".join(report.lines()).split())
        self.assertIn("it will not be released, it will not be discarded", flat)

    def test_it_is_grouped_by_site_because_that_is_how_he_thinks(self) -> None:
        self.queue.add(days_ago=1, site="Beach Court")
        self.queue.add(days_ago=6, site="Beach Court")
        self.queue.add(days_ago=2, site="Rondebosch")
        report = self.queue.report()
        by_site = {s.site: s for s in report.by_site}
        self.assertEqual(by_site["Beach Court"].count, 2)
        self.assertEqual(by_site["Beach Court"].oldest_age_days, 6)
        self.assertEqual(by_site["Rondebosch"].count, 1)
        # The site with the oldest passage is listed first: the ordering is the escalation.
        self.assertEqual(report.by_site[0].site, "Beach Court")


class HisEmailNeverCarriesStaffWords(unittest.TestCase):
    """Decision 6, in the one place it is easiest to lose: the email he actually reads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="staff-")
        self.addCleanup(self._tmp.cleanup)
        self.queue = _Queue(self._tmp.name)
        self.addCleanup(self.queue.close)
        self.words = "Sipho is on a final written warning after Tuesday"
        self.queue.add(
            days_ago=9,
            site="Rondebosch",
            reviewer=SIPHO,
            category="staff_matter",
            words=self.words,
            subject="a written warning for a named person",
        )
        self.report = self.queue.report()
        self.body = "\n".join(self.report.lines())

    def test_he_is_told_how_many_and_where(self) -> None:
        self.assertEqual(self.report.pending, 1)
        self.assertIn("Rondebosch", self.body)
        self.assertIn("sipho", self.body)

    def test_and_not_one_word_of_what_was_said(self) -> None:
        for fragment in ("Sipho is", "final", "warning", "Tuesday"):
            self.assertNotIn(fragment, self.body)

    def test_not_even_the_classifiers_own_summary_of_it(self) -> None:
        # ``subject`` is a public noun phrase and is safe by construction, but "safe by
        # construction" is a claim about a model's output. For a passage that is not his,
        # the email uses the category's own fixed phrase instead — one of six sentences
        # this repository wrote.
        self.assertNotIn("a written warning for a named person", self.body)
        self.assertIn("a staff matter", self.body)

    def test_nor_which_recording_it_came_from(self) -> None:
        self.assertNotIn("Call number 1.m4a", self.body)

    def test_his_own_passages_are_named_in_full(self) -> None:
        queue = _Queue(tempfile.mkdtemp(dir=self._tmp.name))
        self.addCleanup(queue.close)
        queue.add(days_ago=9, reviewer=JAMES, subject="a rate for the remedial")
        body = "\n".join(queue.report().lines())
        self.assertIn("a rate for the remedial", body)
        self.assertIn("Call number 1.m4a", body)


class ShadowReportsWhatItWouldHaveHeld(unittest.TestCase):
    """It ships dark, and the measurement is the whole point of shipping it that way."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="shadow-")
        self.addCleanup(self._tmp.cleanup)
        self.queue = _Queue(self._tmp.name, mode="shadow")
        self.addCleanup(self.queue.close)
        sighting = self.queue.add(days_ago=1)
        span = sighting.as_span()
        for n in range(40):
            self.queue.store.record_pass(
                f"pass-{n}",
                route="calls",
                mode="shadow",
                spans=(span,) if n < 2 else (),
                transcript_chars=6000,
                at=_held_at(1),
            )
        self.report = self.queue.report()
        self.body = "\n".join(self.report.lines())

    def test_nothing_is_pending_because_nothing_was_withheld(self) -> None:
        self.assertEqual(self.report.pending, 0)
        self.assertEqual(self.report.escalation, "none")
        self.assertEqual(self.report.subject_warning(), "")

    def test_it_says_so_in_the_first_sentence(self) -> None:
        self.assertIn("NOTHING WAS WITHHELD", self.body)
        self.assertIn("holds nothing", self.body)

    def test_it_reports_the_measurement_that_has_to_be_real_first(self) -> None:
        self.assertEqual(self.report.classified, 40)
        self.assertEqual(self.report.with_a_hold, 2)
        self.assertEqual(self.report.would_have_held, 2)
        self.assertIn("recordings read", self.body)
        self.assertIn("share of the words", self.body)

    def test_and_never_a_word_of_what_it_would_have_held(self) -> None:
        self.assertNotIn("R1.65m", self.body)
        self.assertNotIn("R1.604m", self.body)


class TheWholeEmail(unittest.TestCase):
    """The section as it lands in the message that actually goes out at 06:00."""

    def _build(self, days_ago: int | None, mode: str = "on") -> Any:
        tmp = tempfile.mkdtemp()
        queue = _Queue(tmp, mode=mode)
        self.addCleanup(queue.close)
        if days_ago is not None:
            queue.add(days_ago=days_ago)
        return queue, digest_module.build(queue.config, queue.ledger, day="2026-08-27")

    def test_the_held_queue_is_in_the_body(self) -> None:
        _queue, built = self._build(2)
        self.assertIn("HELD PASSAGES", built.body)
        self.assertIn("Beach Court", built.body)
        self.assertTrue(built.needs_a_person)

    def test_a_week_old_passage_reaches_the_subject_line(self) -> None:
        _queue, built = self._build(9)
        self.assertIn("⚠ 1 passage(s) held, oldest 9 days", built.subject)
        # And the counts he opens the email for are still in there behind it.
        self.assertIn("Recordings:", built.subject)

    def test_a_fresh_deployment_says_nothing_at_all_about_the_gate(self) -> None:
        # It ships in shadow. One line every morning about a gate that has never seen a
        # recording is wallpaper, and wallpaper is what stops him reading the rest.
        _queue, built = self._build(None, mode="shadow")
        self.assertNotIn("HELD PASSAGES", built.body)
        self.assertNotIn("THE GATE IS WATCHING", built.body)

    def test_but_an_armed_gate_that_has_read_nothing_says_so(self) -> None:
        # Armed and silent is the one shape that has to be said out loud: a classifier that
        # is not running looks exactly like a classifier that is finding nothing.
        _queue, built = self._build(None, mode="on")
        self.assertIn("HELD PASSAGES", built.body)
        self.assertIn("has not read a recording yet", built.body)

    def test_the_email_still_carries_no_address(self) -> None:
        _queue, built = self._build(9)
        self.assertEqual(ADDR_RE.findall(built.body), [])
        self.assertEqual(ADDR_RE.findall(built.subject), [])


class AnOwedDeliveryReachesTheMorningEmail(unittest.TestCase):
    """A release nobody could write is not finished, and the email says so."""

    def test_the_email_names_it_without_naming_the_words(self) -> None:
        tmp = tempfile.mkdtemp()
        fix = _Fixture(tmp, drive=_Drive(fail_times=1))
        self.addCleanup(fix.close)
        fix.releaser.deliver_quietly(fix.release())
        built = digest_module.build(fix.config, fix.ledger, day="2026-08-27")
        flat = " ".join(built.body.split())
        self.assertIn("have been approved but their words have not been written", flat)
        self.assertIn(fix.held.ref, built.body)
        self.assertNotIn(HELD_WORDS, built.body)
        self.assertTrue(built.needs_a_person)


class TheGateIsOffButPassagesAreStillWaiting(unittest.TestCase):
    """Switching the classifier off does not answer anybody's held passage."""

    def test_the_queue_is_still_reported(self) -> None:
        tmp = tempfile.mkdtemp()
        queue = _Queue(tmp, mode="on")
        self.addCleanup(queue.close)
        queue.add(days_ago=4)
        queue.config.gate_mode = "off"
        report = digest_module.held_report(
            queue.config, queue.ledger, store=queue.store, day="2026-08-27", now=_Queue.NOW
        )
        self.assertEqual(report.pending, 1)
        self.assertEqual(report.escalation, "named")
        self.assertIn("switched off", "\n".join(report.lines()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
