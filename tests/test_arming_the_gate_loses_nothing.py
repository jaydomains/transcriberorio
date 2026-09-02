"""The day the gate is switched on, and the passages already seen in shadow.

The gate ships dark: it reads every recording, records what it *would* have held, and holds
nothing. Those rows are stored ``not-withheld``, which means, in the words of the class that
defines it, "the classifier would have held these words and nothing was actually cut".

Then the gate is armed. Any recording processed again after that — a sweep re-queue, a
`requeue`, a re-run after a crash — is classified again, and the same passage lands on the
same row, because a hold id is content-addressed on the recording, the offsets and the
words. Masking, meanwhile, works off the spans and the mode and never reads that row: with
the gate armed, the words come out of the transcript.

So the row's own statement — nothing was cut — stops being true, and the passage becomes a
hold that has happened and that nobody can answer: not in a queue, not on the review page,
not in the morning email, with the words gone from the record. That is the gate emptying
itself quietly, which is the failure this whole design exists to make impossible.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.withheld import Decision, HeldSpan, WithheldStore

WORDS = "Sipho got his second written warning on Friday."


class APassageFirstSeenInShadow(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.store = WithheldStore(os.path.join(tmp, "held.sqlite"))
        self.addCleanup(self.store.close)
        self.span = HeldSpan(
            item_id="01ITEM", start=10, end=10 + len(WORDS), text=WORDS,
            category="staff_matter", recorded_at="2026-08-26T09:00:00Z",
            route="james", reviewer="james",
        )

    def test_in_shadow_it_is_counted_and_not_held(self) -> None:
        record = self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        self.assertEqual(record.decision, Decision.NOT_WITHHELD)
        self.assertEqual(self.store.queue_for("james", decision=Decision.PENDING), ())

    def test_when_the_gate_is_armed_it_becomes_answerable(self) -> None:
        self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        record = self.store.hold(self.span, mode="on", at="2026-08-27T09:00:00Z")
        self.assertEqual(
            record.decision, Decision.PENDING,
            "the words are cut once the gate is armed, but the row still said nothing was "
            "cut — so the passage was gone from the record with nobody able to release it",
        )

    def test_and_it_reaches_the_person_who_has_to_answer_it(self) -> None:
        self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        self.store.hold(self.span, mode="on", at="2026-08-27T09:00:00Z")
        self.assertEqual(
            [r.ref for r in self.store.queue_for("james", decision=Decision.PENDING)],
            [self.span.ref],
        )
        self.assertEqual(self.store.overview(decision=Decision.PENDING).get("count"), 1)

    def test_the_clock_starts_when_it_was_actually_held(self) -> None:
        """Nobody can have been sitting on something that was not in their queue."""
        self.store.hold(self.span, mode="shadow", at="2026-07-01T09:00:00Z")
        record = self.store.hold(self.span, mode="on", at="2026-08-27T09:00:00Z")
        self.assertEqual(record.age_days("2026-08-28T09:00:00Z"), 1)

    def test_a_persons_answer_is_never_overwritten(self) -> None:
        """RELEASED and REFUSED carry somebody's name. Nothing here may overrule them."""
        self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        held = self.store.hold(self.span, mode="on", at="2026-08-27T09:00:00Z")
        self.store.refuse(held.hold_id, answered_by="james", note="not for the record")
        again = self.store.hold(self.span, mode="on", at="2026-08-28T09:00:00Z")
        self.assertEqual(again.decision, Decision.REFUSED)

    def test_and_a_released_one_stays_released(self) -> None:
        self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        held = self.store.hold(self.span, mode="on", at="2026-08-27T09:00:00Z")
        self.store.release(held.hold_id, answered_by="james", note="fine to keep")
        again = self.store.hold(self.span, mode="on", at="2026-08-28T09:00:00Z")
        self.assertEqual(again.decision, Decision.RELEASED)

    def test_staying_in_shadow_still_decides_nothing(self) -> None:
        """Seeing it twice with the gate still dark must not create a queue out of nothing."""
        self.store.hold(self.span, mode="shadow", at="2026-08-26T09:00:00Z")
        record = self.store.hold(self.span, mode="shadow", at="2026-08-27T09:00:00Z")
        self.assertEqual(record.decision, Decision.NOT_WITHHELD)
        self.assertEqual(self.store.overview(decision=Decision.PENDING).get("count"), 0)


if __name__ == "__main__":
    unittest.main()
