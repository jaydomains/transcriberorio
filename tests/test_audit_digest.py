"""What "done" is evidence of — and the sentence that stops it being read as more.

Every count in the morning email is this service watching itself: it wrote the transcript
to OneDrive and read it back from OneDrive to check the bytes arrived whole. Carrying the
file on from there into the record is a separate flow, outside this service, holding its
own access that expires like every other credential here. When that access lapses the
record simply stops receiving transcripts — and every number in this email stays perfect,
because from in here everything genuinely did work.

That is the original failure wearing a clean shirt. "Recordings: all 23 done" on a phone
screen is read as a promise that the recordings are where somebody will look for them, and
for as long as the body did not say otherwise, the email was making a promise it had no way
of keeping. Four days of recordings once went missing while nothing looked wrong; an email
reporting a perfect morning through a broken hand-over is that same silence with a subject
line on it.

So these tests hold two things in place:

  * **The qualification exists, in plain words, against the numbers it qualifies.** Not
    only in the deployment notes, which are read once, and not in a section at the bottom
    that somebody who has already seen "all 23 done" will never reach.
  * **The empty receipt slot says nothing at all.** The honest fix is a receipt from
    outside — something that can state the record actually holds the file. Nothing emits
    one yet. Until something does, the ordinary morning email must not grow a heading with
    nothing under it: a section that is always empty is a section people learn to skip, and
    the day it fills is the day they do not read it. A receipt source that is configured
    and *broken*, though, has to be loud. A slot whose source died a fortnight ago and a
    slot nobody ever configured must never look the same — that is the same mistake as the
    one this whole file is about, one level down.
"""

from __future__ import annotations

import datetime
import difflib
import json
import os
import tempfile
import unittest
from typing import Any

from transcriber import digest
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, State

from . import support

#: A fixed moment to build every digest at, so an age rendered in the body is arithmetic
#: rather than a race with the wall clock.
CLOCK = 1_800_000_000.0

#: The sentence in the counts block that actually claims the transcript was filed, and the
#: first words of the paragraph that qualifies it. Kept as names because several tests need
#: to find where each one sits relative to the other.
COUNTS_CLAIM = "transcribed and filed"
QUALIFICATION = '"Transcribed and filed" means'


def flat(text: str) -> str:
    """One long line, so a phrase can be looked for across a line break.

    The email is wrapped to a readable width, which means any sentence worth asserting on
    is split over two or three lines with leading spaces on each. Collapsing the whitespace
    lets a test say what the email must tell somebody without also freezing where the
    wrapper happened to break it.
    """
    return " ".join(text.split())


def iso(epoch: float) -> str:
    """A UTC timestamp of the shape everything in this service writes."""
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class DigestCase(unittest.TestCase):
    """A real ledger, a real config and a really rendered email. Nothing here is a fake."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = support.make_config(work_dir=self.dir.name)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)

    def discover(self, *ids: str) -> str:
        self.ledger.record_page(
            [
                DriveItem(item_id=i, name=f"Call Carel_260827_1200{n:02d}.m4a")
                for n, i in enumerate(ids)
            ],
            "cursor-1",
        )
        return self.ledger.get(ids[0]).discovered_at[:10]

    def perfect_morning(self) -> digest.Digest:
        """Three recordings, all transcribed and filed, nothing outstanding anywhere."""
        day = self.discover("A", "B", "C")
        for item in ("A", "B", "C"):
            self.ledger.advance(item, State.DONE)
        return digest.build(self.config, self.ledger, day=day, now=CLOCK)

    def point_at_a_receipt(self, name: str = "receipts.json", body: Any = None) -> str:
        """Point the config at a receipt file, writing one first unless asked not to.

        The setting is put on the config object directly rather than through the
        environment because it is not a declared setting yet: the reading half of the
        receipt exists and the writing half does not. The digest reads it with ``getattr``
        precisely so it stays inert until somebody wires a source up, and both sides of
        that have to be testable today.
        """
        path = os.path.join(self.dir.name, name)
        if body is not None:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(body, handle)
        self.config.record_receipts_file = path
        return path


class TheEmailSaysWhatItCannotSee(DigestCase):
    """The counts must not be left to imply the transcript reached the record."""

    def test_a_perfect_morning_still_says_the_record_is_not_confirmed(self) -> None:
        """The morning this sentence matters most is the one where nothing is wrong."""
        built = self.perfect_morning()
        text = flat(built.body)

        self.assertEqual(built.subject, "Recordings: all 3 done")

        # Three things the qualification has to actually say. Each is checked against a
        # short list of acceptable wordings rather than one exact sentence, so somebody
        # with a better ear can rewrite the email without this test standing in the way —
        # but it cannot be rewritten into silence, which is the whole point.
        self.assertTrue(
            any(
                phrase in text
                for phrase in ("into the record", "reaching the record", "to the record")
            ),
            "The body never mentions the record — the place the transcript still has to "
            "reach after this service has finished with it. Without that, the counts read "
            "as a promise this service cannot keep.",
        )
        self.assertTrue(
            any(
                phrase in text
                for phrase in (
                    "nothing here can see whether it got there",
                    "cannot see whether it got there",
                    "nothing here can confirm",
                    "is not confirmed",
                )
            ),
            "The body never says that this service cannot see whether the transcript "
            "reached the record. That is precisely the fact the counts hide.",
        )
        self.assertTrue(
            any(
                phrase in text
                for phrase in (
                    "still report a perfect morning",
                    "still look like a perfect morning",
                    "still report a perfect",
                )
            ),
            "The body never says what a broken hand-over looks like from in here — a "
            "perfect morning. Somebody who is not told that has no reason to doubt the "
            "counts on exactly the morning they should.",
        )

    def test_the_all_done_line_does_not_stand_alone(self) -> None:
        """The qualification sits with the numbers, not in a section further down.

        Somebody who has read "all 3 done" in the subject line and five clean counts under
        it has already formed their view of the morning. A qualification below the queue,
        the routes and the spend meter is a qualification that never gets read.
        """
        text = flat(self.perfect_morning().body)

        self.assertIn(
            QUALIFICATION,
            text,
            "The counts block is not qualified at all: nothing anywhere near the numbers "
            "explains what \"transcribed and filed\" is evidence of.",
        )
        self.assertLess(
            text.index(COUNTS_CLAIM),
            text.index(QUALIFICATION),
            "The qualification was rendered above the counts it qualifies.",
        )
        self.assertLess(
            text.index(QUALIFICATION),
            text.index("THE QUEUE"),
            "The qualification has drifted out of the counts section into a part of the "
            "email that somebody who has already read the numbers will not reach.",
        )

    def test_it_is_there_on_a_bad_morning_too(self) -> None:
        """A morning with a failure on it is not a morning where the rest is confirmed.

        Two recordings filed and one stopped makes exactly the same claim about the two,
        and somebody chasing the failure is even less likely to question them.
        """
        day = self.discover("A", "B", "C")
        self.ledger.advance("A", State.DONE)
        self.ledger.advance("B", State.DONE)
        self.ledger.quarantine("C", "the audio is truncated: no moov index")

        built = digest.build(self.config, self.ledger, day=day, now=CLOCK)

        self.assertIn("FAILED", built.subject)
        self.assertIn(
            QUALIFICATION,
            flat(built.body),
            "The qualification is only rendered on some mornings. It describes the "
            "hand-over, and the hand-over is the same every morning.",
        )


class TheEmptySlotIsSilent(DigestCase):
    """No receipt source configured — which is today, always — renders nothing at all."""

    def test_the_helper_says_nothing_when_no_source_is_configured(self) -> None:
        self.assertEqual(digest.newest_confirmed_arrival(self.config, CLOCK), "")

    def test_the_ordinary_morning_email_grows_no_empty_section(self) -> None:
        body = self.perfect_morning().body
        text = flat(body)

        for phrase in (
            "the record confirms it holds",
            "the record's receipt",
            "has not reported back",
        ):
            self.assertNotIn(
                phrase,
                text,
                "The email is talking about a receipt from the record on a deployment "
                "where nothing emits one. An unfilled slot must say nothing at all.",
            )

        # And it must not have left a hole behind either: a heading with nothing under it,
        # a rule with nothing under the rule, or a gap where a line was going to go. All
        # three teach somebody to skip the region, and the region they would learn to skip
        # is the one holding the qualification above.
        between = body[body.index(COUNTS_CLAIM):body.index("THE QUEUE")]
        self.assertNotIn(
            "\n\n\n",
            between,
            "There is a blank gap under the counts where the unfilled receipt line would "
            "have gone. The ordinary morning email should not show that the slot exists.",
        )
        self.assertNotIn(
            "---",
            between,
            "A section rule has appeared under the counts for a receipt nothing emits. "
            "An unfilled slot must not be visible at all.",
        )
        for line in between.split("\n"):
            stripped = line.strip()
            self.assertFalse(
                stripped and stripped == stripped.upper() and stripped[0].isalpha(),
                f"A heading — {stripped!r} — has appeared under the counts for a receipt "
                f"nothing emits. An unfilled slot must not be visible at all.",
            )

    def test_configuring_a_source_adds_the_line_and_nothing_else(self) -> None:
        """The slot's whole cost, measured: one sentence, and only when it can be filled.

        Built twice over the same ledger and the same morning, once with no receipt source
        and once with one. The difference between the two emails is the entire footprint of
        this slot, and it has to be the sentence and nothing around it — no heading, no
        rule, no spacer. That is what makes "renders nothing when unconfigured" a fact
        about the email rather than a claim about the helper.
        """
        day = self.discover("A", "B", "C")
        for item in ("A", "B", "C"):
            self.ledger.advance(item, State.DONE)

        before = digest.build(self.config, self.ledger, day=day, now=CLOCK).body
        self.point_at_a_receipt(body={"newest_at": iso(CLOCK - 2 * 3600.0)})
        after = digest.build(self.config, self.ledger, day=day, now=CLOCK).body

        added = [
            line[2:]
            for line in difflib.ndiff(before.split("\n"), after.split("\n"))
            if line.startswith("+ ")
        ]

        self.assertTrue(
            any("record confirms it holds" in line for line in added),
            "Configuring a receipt source changed nothing in the email. The slot reads "
            "its source and then says nothing about what it found.",
        )
        for line in added:
            self.assertTrue(
                line.strip() == "" or line.startswith("  "),
                f"Filling the slot added {line!r}, which is not part of the sentence. The "
                f"slot is meant to be one line under the counts, so on a deployment "
                f"without a source there is nothing there to be empty.",
            )
            self.assertNotIn(
                "---",
                line,
                "Filling the slot added a section rule, which means the unfilled slot has "
                "a section of its own waiting to be empty.",
            )


class AConfiguredReceiptIsReadOrSaidToBeBroken(DigestCase):
    """Once something does emit a receipt, the slot has to be as honest as the counts.

    Every branch below is a different thing having gone wrong, and each gets its own
    sentence. Silence is not available to any of them: a slot that reads as empty when its
    source has broken is the same failure as an email that reads as perfect when the
    hand-over has broken, which is what the slot exists to fix.
    """

    def test_a_receipt_becomes_one_line_under_the_qualification(self) -> None:
        self.point_at_a_receipt(body={"newest_at": iso(CLOCK - 2 * 3600.0)})
        day = self.discover("A")
        self.ledger.advance("A", State.DONE)

        built = digest.build(self.config, self.ledger, day=day, now=CLOCK)
        text = flat(built.body)

        self.assertIn("record confirms it holds a transcript", text)
        self.assertIn(
            "2.0 hours ago",
            text,
            "The receipt line does not say how old the newest confirmed transcript is. "
            "The age is the whole signal: a receipt from a fortnight ago is a broken "
            "hand-over being reported as a confirmation.",
        )
        self.assertLess(
            text.index(QUALIFICATION),
            text.index("record confirms it holds"),
            "The receipt line was rendered above the sentence that explains what it is "
            "answering.",
        )

    def test_a_missing_receipt_file_is_said_out_loud(self) -> None:
        """Configured and not there is a fault, not an ordinary quiet morning."""
        self.point_at_a_receipt(name="never-written.json")

        said = digest.newest_confirmed_arrival(self.config, CLOCK)

        self.assertNotEqual(
            said,
            "",
            "A receipt source is configured, its file is not there, and the email says "
            "nothing about it. That is a slot whose source has broken looking exactly "
            "like a slot nobody ever configured.",
        )
        self.assertIn("never-written.json", said)
        self.assertIn(
            "has not reported back: never-written.json is not there yet",
            flat(self.perfect_morning().body),
            "The fault was worked out and then not put in the email anybody reads.",
        )

    def test_an_unreadable_receipt_is_said_out_loud(self) -> None:
        path = os.path.join(self.dir.name, "receipts.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not JSON at all")
        self.config.record_receipts_file = path

        said = digest.newest_confirmed_arrival(self.config, CLOCK)

        self.assertIn("could not be read", said)
        self.assertNotIn(
            "confirms it holds",
            said,
            "An unreadable receipt was reported as a confirmation.",
        )

    def test_a_receipt_naming_no_transcript_confirms_nothing(self) -> None:
        """The worst branch: a file that exists, parses, and says nothing.

        A source that ran and produced an empty answer is the shape a source takes when it
        has quietly lost its own access. Reading that as "nothing to report" would put the
        original failure straight back, one layer further out.
        """
        for body in ({}, {"newest_at": ""}, {"newest_at": "not a timestamp"}):
            with self.subTest(body=body):
                self.point_at_a_receipt(name="receipts.json", body=body)
                said = digest.newest_confirmed_arrival(self.config, CLOCK)
                self.assertIn("names no transcript", said)
                self.assertNotIn("confirms it holds", said)

    def test_the_slot_never_raises_however_broken_the_source_is(self) -> None:
        """The morning email goes out. Nothing about a receipt may stop it.

        Same rule as every other reader in this module: the digest is the only thing that
        says the service is alive, so a fault inside one of its sections becomes a sentence
        in that section and never an exception on the way to the mail server.
        """
        binary = os.path.join(self.dir.name, "binary.json")
        with open(binary, "wb") as handle:
            handle.write(b"\xff\xfe\x00\x01not text")

        broken = (
            self.dir.name,                        # a directory, not a file
            binary,                               # bytes that are not text at all
            os.path.join(self.dir.name, "gone"),  # nothing there
        )
        for path in broken:
            with self.subTest(source=os.path.basename(path) or "a directory"):
                self.config.record_receipts_file = path
                said = digest.newest_confirmed_arrival(self.config, CLOCK)
                self.assertIsInstance(said, str)
                self.assertNotEqual(said, "")
                self.assertNotIn("confirms it holds", said)

        for body in ([1, 2, 3], "a string", 7):
            with self.subTest(body=body):
                self.point_at_a_receipt(name="odd.json", body=body)
                said = digest.newest_confirmed_arrival(self.config, CLOCK)
                self.assertIsInstance(said, str)
                self.assertNotIn("confirms it holds", said)

    def test_a_receipt_does_not_make_the_qualification_go_away(self) -> None:
        """A receipt narrows the doubt; it does not answer it for the day's recordings.

        The line says the record holds *a* transcript, of some age. It says nothing about
        whether the ones counted this morning are among them, so the sentence explaining
        what the counts are evidence of has to stay exactly where it was.
        """
        self.point_at_a_receipt(body={"newest_at": iso(CLOCK - 60.0)})

        self.assertIn(QUALIFICATION, flat(self.perfect_morning().body))


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
