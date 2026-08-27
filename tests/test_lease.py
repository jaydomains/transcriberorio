"""Lease expiry and re-claim: a worker that dies must not strand a recording forever.

The claim is a conditional UPDATE with an expiry, and the two halves of that are equally
load-bearing. Without the expiry, a worker killed mid-job holds the file until a person
notices — which, for a service whose whole purpose is that nobody has to notice, is the
same as losing it. Without the condition, two workers transcribe the same recording and
race each other to write its outputs.

The clock is injected everywhere below, so none of this waits and none of it is flaky.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.ledger import Ledger, LedgerError, default_owner
from transcriber.models import DriveItem, State


class LeaseExpiryAndReclaim(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = Ledger(os.path.join(self.dir.name, "ledger.sqlite3"))
        self.addCleanup(self.ledger.close)
        self.ledger.record_page(
            [DriveItem(item_id="A", name="Call Carel_260827_120055.m4a", size=4096)], "cursor-1"
        )
        self.now = 1_000_000.0

    # -- the two workers -----------------------------------------------------------

    def test_a_second_worker_cannot_take_a_live_claim(self) -> None:
        taken = self.ledger.claim("A", 900, owner="worker-one", now=self.now)
        self.assertTrue(taken)

        for offset in (0.0, 1.0, 899.0, 899.9):
            with self.subTest(seconds_into_the_lease=offset):
                self.assertFalse(
                    self.ledger.claim("A", 900, owner="worker-two", now=self.now + offset),
                    "a second worker took a claim that was still live",
                )

        row = self.ledger.get("A")
        self.assertEqual(row.claimed_by, "worker-one")
        self.assertEqual(row.state, State.CLAIMED)

    def test_the_claim_can_be_taken_once_the_lease_has_expired(self) -> None:
        """Worker one dies here. Nothing releases the claim; the lease simply runs out."""
        self.assertTrue(self.ledger.claim("A", 900, owner="worker-one", now=self.now))

        self.assertFalse(self.ledger.claim("A", 900, owner="worker-two", now=self.now + 900.0))
        self.assertTrue(self.ledger.claim("A", 900, owner="worker-two", now=self.now + 900.1))

        row = self.ledger.get("A")
        self.assertEqual(row.claimed_by, "worker-two")
        self.assertEqual(row.lease_until, self.now + 900.1 + 900)

    def test_the_boundary_is_exactly_the_lease(self) -> None:
        """Stated as its own case because an off-by-one here is two workers on one file."""
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.assertFalse(self.ledger.claim("A", 60, owner="worker-two", now=self.now + 60.0))
        self.assertTrue(self.ledger.claim("A", 60, owner="worker-two", now=self.now + 60.001))

    # -- what a claim does and does not move ---------------------------------------

    def test_claiming_a_discovered_row_moves_it_to_claimed(self) -> None:
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.assertEqual(self.ledger.get("A").state, State.CLAIMED)

    def test_re_claiming_a_part_finished_row_keeps_its_progress(self) -> None:
        """A resumed recording must not lose the download it already paid for."""
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.ledger.advance("A", State.FETCHED, content_hash="abc123")

        self.assertTrue(self.ledger.claim("A", 60, owner="worker-two", now=self.now + 61))
        row = self.ledger.get("A")
        self.assertEqual(row.state, State.FETCHED)
        self.assertEqual(row.content_hash, "abc123")

    def test_a_finished_recording_is_not_claimable_at_all(self) -> None:
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.ledger.advance("A", State.DONE)
        self.assertFalse(self.ledger.claim("A", 60, owner="worker-two", now=self.now + 10_000))

    def test_a_quarantined_recording_is_not_silently_re_claimed(self) -> None:
        """Quarantine means a person. A worker picking it up again would hide it."""
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.ledger.quarantine("A", "the audio is truncated")
        self.assertFalse(self.ledger.claim("A", 60, owner="worker-two", now=self.now + 10_000))
        self.assertIsNone(self.ledger.get("A").claimed_by)

    # -- releasing and renewing ----------------------------------------------------

    def test_release_makes_it_claimable_at_once(self) -> None:
        self.ledger.claim("A", 900, owner="worker-one", now=self.now)
        self.ledger.release("A", "shutting down cleanly")
        self.assertTrue(self.ledger.claim("A", 900, owner="worker-two", now=self.now + 1))

    def test_record_attempt_lets_go_of_the_claim(self) -> None:
        """A failed attempt is a worker that has stopped working on it. Hold nothing."""
        self.ledger.claim("A", 900, owner="worker-one", now=self.now)
        attempts = self.ledger.record_attempt("A", "the engine timed out")

        self.assertEqual(attempts, 1)
        self.assertIsNone(self.ledger.get("A").lease_until)
        self.assertTrue(self.ledger.claim("A", 900, owner="worker-two", now=self.now + 1))

    def test_renew_extends_a_claim_we_still_hold(self) -> None:
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.assertTrue(self.ledger.renew("A", 60, "worker-one", now=self.now + 30))
        self.assertFalse(
            self.ledger.claim("A", 60, owner="worker-two", now=self.now + 61),
            "a renewed claim must still be live past the original expiry",
        )

    def test_renew_fails_once_somebody_else_has_taken_it(self) -> None:
        """The dead worker waking up late must not stamp on the live one's lease."""
        self.ledger.claim("A", 60, owner="worker-one", now=self.now)
        self.ledger.claim("A", 60, owner="worker-two", now=self.now + 61)
        self.assertFalse(self.ledger.renew("A", 60, "worker-one", now=self.now + 62))
        self.assertEqual(self.ledger.get("A").claimed_by, "worker-two")

    # -- what is claimable ---------------------------------------------------------

    def test_claimable_lists_a_row_whose_lease_has_lapsed_and_not_one_that_holds(self) -> None:
        self.ledger.record_page([DriveItem(item_id="B", name="two.m4a")], "cursor-2")
        self.ledger.claim("A", 900, owner="worker-one", now=self.now)

        live = {row.item_id for row in self.ledger.claimable(now=self.now + 10)}
        self.assertEqual(live, {"B"})

        lapsed = {row.item_id for row in self.ledger.claimable(now=self.now + 1000)}
        self.assertEqual(lapsed, {"A", "B"})

    def test_a_lease_of_zero_is_refused(self) -> None:
        """A claim that never expires is the exact bug the lease is here to prevent."""
        with self.assertRaises(LedgerError):
            self.ledger.claim("A", 0, owner="worker-one", now=self.now)

    def test_the_default_owner_says_which_process_holds_it(self) -> None:
        owner = default_owner()
        self.assertEqual(owner.count(":"), 2, f"an owner a person cannot trace: {owner!r}")


if __name__ == "__main__":
    unittest.main()
