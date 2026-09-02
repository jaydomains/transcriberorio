"""Pointing a route at a different folder, and the bookmark left in the old one.

A delta cursor is a bookmark in one folder's change feed. Change the folder a route
watches — `routes edit`, the wizard, or a hand-edited .env — and the stored cursor still
describes changes somewhere the service is no longer looking. Graph answers it happily,
because it is a perfectly valid link. So the live poll reports "0 new" every cycle, for
ever, while recordings pile up in the new folder and nothing says the live path has gone
deaf.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.ledger import Ledger, delta_cursor_name
from transcriber.models import Route


class _Graph:
    """Records which folder it was asked about, and hands back nothing."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    def delta_with_resync(self, folder_id, cursor, on_resync):  # noqa: ANN001
        self.asked.append((folder_id, cursor))
        return iter(())


class WhenTheWatchedFolderChanges(unittest.TestCase):
    def setUp(self) -> None:
        from transcriber.worker import Worker

        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        self.graph = _Graph()
        self.worker = Worker.__new__(Worker)
        self.worker.ledger = self.ledger
        self.worker.graph = self.graph
        self.cursor_name = delta_cursor_name("james")

    def _route(self, folder: str) -> Route:
        return Route(name="james", label="James", source_folder_id=folder, output_folder_id="O")

    def _poll(self, folder: str) -> None:
        self.worker.poll_route(self._route(folder), own_outputs=frozenset())

    def test_the_first_poll_simply_remembers_the_folder(self) -> None:
        self._poll("FOLDER-A")
        self.assertTrue(self.ledger.cursor_get("watched:james"))

    def test_what_is_remembered_survives_being_stored(self) -> None:
        """The mark is written through the ledger's scrubber, like every stored string.

        So it may not be anything the scrubber would alter: a mark that came back changed
        would never match, the folder would look different on every poll, and the cursor
        would be rewound every two minutes — delta polling defeated by the check meant to
        protect it. A hex fingerprint passes through unchanged; a folder id is not
        guaranteed to.
        """
        self._poll("FOLDER-A")
        stored = self.ledger.cursor_get("watched:james")
        self.assertEqual(self.ledger._clean(stored), stored)

    def test_and_the_same_folder_looks_the_same_next_time(self) -> None:
        self._poll("FOLDER-A")
        first = self.ledger.cursor_get("watched:james")
        self._poll("FOLDER-A")
        self.assertEqual(self.ledger.cursor_get("watched:james"), first)

    def test_a_folder_id_the_scrubber_would_alter_still_works(self) -> None:
        """The case the fingerprint exists for, made concrete."""
        awkward = "carel@example.co.za"
        self._poll(awkward)
        before = self.ledger.cursor_get("watched:james")
        self._poll(awkward)
        self.assertEqual(
            self.ledger.cursor_get("watched:james"), before,
            "an unchanged folder was seen as changed, so the cursor would be thrown away "
            "on every single poll",
        )
        kinds = [e["kind"] for e in self.ledger.recent_events()]
        self.assertNotIn("cursor-rewound", kinds)

    def test_a_service_already_running_adopts_its_folder_without_rewinding(self) -> None:
        """Nothing was recorded before this existed, so the first sight is not a change."""
        self.ledger.record_page([], "https://graph.example/delta?token=1", route="james",
                                cursor_name=self.cursor_name)
        self._poll("FOLDER-A")
        self.assertEqual(
            self.ledger.cursor_get(self.cursor_name), "https://graph.example/delta?token=1"
        )

    def test_changing_the_folder_throws_the_old_bookmark_away(self) -> None:
        self._poll("FOLDER-A")
        self.ledger.record_page([], "https://graph.example/delta?token=1", route="james",
                                cursor_name=self.cursor_name)
        self._poll("FOLDER-B")
        self.assertIsNone(
            self.ledger.cursor_get(self.cursor_name),
            "the cursor bookmarking the OLD folder survived, so the live poll would keep "
            "reading a feed for a folder nobody is watching and report nothing new for ever",
        )

    def test_and_the_new_folder_is_what_gets_asked_about(self) -> None:
        self._poll("FOLDER-A")
        self._poll("FOLDER-B")
        self.assertEqual(self.graph.asked[-1][0], "FOLDER-B")
        self.assertIsNone(self.graph.asked[-1][1], "it must start that folder from zero")

    def test_and_it_is_recorded_as_having_happened(self) -> None:
        self._poll("FOLDER-A")
        self.ledger.record_page([], "https://graph.example/delta?token=1", route="james",
                                cursor_name=self.cursor_name)
        self._poll("FOLDER-B")
        kinds = [e["kind"] for e in self.ledger.recent_events()]
        self.assertIn("cursor-rewound", kinds)

    def test_polling_the_same_folder_twice_changes_nothing(self) -> None:
        self._poll("FOLDER-A")
        self.ledger.record_page([], "https://graph.example/delta?token=1", route="james",
                                cursor_name=self.cursor_name)
        self._poll("FOLDER-A")
        self.assertEqual(
            self.ledger.cursor_get(self.cursor_name), "https://graph.example/delta?token=1"
        )


if __name__ == "__main__":
    unittest.main()
