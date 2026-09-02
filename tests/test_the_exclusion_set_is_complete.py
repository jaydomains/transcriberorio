"""The archive's list of recordings it must not touch, when there are more than two hundred.

The archive moves a sixty-day-old original into its route's archive folder. It first asks
which recordings two routes have both claimed, because for those the route on the row may
not be the route the recording belongs to — moving one of those files it into somebody
else's archive. That list is an exclusion set, and an exclusion set that stops at two
hundred is not a guard; it is a guard-shaped thing that lets the oldest ones through.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.archive import _disputed_items
from transcriber.ledger import Ledger
from transcriber.models import DriveItem


class MoreThanTwoHundredDisputes(unittest.TestCase):
    HOW_MANY = 260

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        for n in range(self.HOW_MANY):
            item_id = f"01ITEM{n:04d}"
            item = DriveItem(
                item_id=item_id, name=f"note-{n}.m4a", size=1000,
                created_at="2026-06-01T09:00:00Z", modified_at="2026-06-01T09:00:00Z",
            )
            # Discovered on one route, then seen again on another — a nested source folder,
            # which is the case the guard exists for.
            self.ledger.upsert_discovered(item, route="james")
            self.ledger.upsert_discovered(item, route="dan")

    def test_every_disputed_recording_is_in_the_set(self) -> None:
        disputed = _disputed_items(self.ledger)
        self.assertEqual(
            len(disputed), self.HOW_MANY,
            "the archive's exclusion set was truncated, so the oldest disputed recordings "
            "would be moved into the wrong route's archive folder",
        )

    def test_the_oldest_one_is_in_it_too(self) -> None:
        """The truncation drops the OLDEST, and the oldest is what the archive acts on."""
        self.assertIn("01ITEM0000", _disputed_items(self.ledger))

    def test_the_display_callers_still_get_a_bounded_list(self) -> None:
        """Two hundred is plenty for a table nobody reads past the top of."""
        self.assertEqual(len(self.ledger.route_disagreements()), 200)


if __name__ == "__main__":
    unittest.main()
