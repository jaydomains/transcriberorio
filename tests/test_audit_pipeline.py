"""Three findings from the pipeline audit, each with the negative control beside it.

The three have nothing in common except where they were found. What they share is a shape:
in each one the service asked a question it could already answer instead of the question it
actually needed answered — is the file on disk the one I downloaded, rather than is it the
one that is in the drive now; has this recording finished, rather than has it changed; did
Microsoft refuse us, rather than could we reach Microsoft at all. Each answer was right and
each one was about the wrong thing.

Every test here has a partner asserting the opposite case still behaves, because a fix that
re-downloads every file, writes an alarming line into every recording's history, or keeps
the service running through an expired secret would pass the first half of this module and
be worse than the fault it replaced.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import unittest
import urllib.error
from typing import Any

from transcriber import graph as graph_module
from transcriber import worker
from transcriber.graph import (
    GraphAuthError,
    GraphAuthUnreachable,
    GraphClient,
    RetryPolicy,
)
from transcriber.ledger import Ledger
from transcriber.models import DriveItem, State
from transcriber.pipeline import Pipeline, PipelineFatal, _classify

from . import support

#: Yesterday's recording, and today's replacement for it. Different lengths, so a reader can
#: tell from a byte count alone which one a test is looking at.
OLD_BYTES = b"yesterday's site meeting" * 40
NEW_BYTES = b"today's site meeting, recorded over the top of it" * 60


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drive_item(data: bytes, name: str = "a.m4a", item_id: str = "A") -> Any:
    """What a fresh ``GET`` on the item would say about these exact bytes.

    Built through ``from_api`` rather than by hand so the test is describing a Graph
    response and not a shape only this file believes in.
    """
    return graph_module.DriveItem.from_api(
        {
            "id": item_id,
            "name": name,
            "size": len(data),
            "eTag": f'"{item_id}-{len(data)}"',
            "file": {"mimeType": "audio/mp4", "hashes": {"sha256Hash": _sha256(data)}},
            "parentReference": {"id": "SOURCE"},
            "@microsoft.graph.downloadUrl": "https://storage.invalid/pre-auth",
        }
    )


class _CountingGraph:
    """Serves whatever bytes it was built with, and counts how often it was asked.

    The count is the assertion in half of these tests: "it downloaded again" and "it did not
    download again" are the two outcomes the reuse branch chooses between, and neither of
    them is visible in the file on disk once the right bytes are there either way.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.downloads: list[str] = []

    def download(
        self, item_id: str, path: str, *, download_url: str = "", expected_size: int | None = None
    ) -> Any:
        self.downloads.append(item_id)
        with open(path, "wb") as handle:
            handle.write(self.data)
        return graph_module.DownloadResult(
            path=path, bytes_written=len(self.data), sha256=_sha256(self.data)
        )


class _Fetching:
    """One row parked at FETCHED with a file already in the work directory.

    This is the state a worker restart lands in: the download finished, the bytes are on
    disk, the row remembers their hash, and the next pass has to decide whether to use them.
    """

    def __init__(self, on_disk: bytes, *, serves: bytes) -> None:
        self.dir = tempfile.mkdtemp()
        self.config = support.make_config(
            work_dir=os.path.join(self.dir, "work"),
            ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.ledger.upsert_discovered(
            DriveItem(item_id="A", name="a.m4a", size=len(on_disk), etag='"A-old"')
        )
        self.ledger.advance(
            "A",
            State.FETCHED,
            content_hash=_sha256(on_disk),
            size=len(on_disk),
            graph_hash=_sha256(on_disk),
        )
        self.graph = _CountingGraph(serves)
        self.pipeline = Pipeline(self.config, self.ledger, self.graph)
        self.path = os.path.join(self.pipeline._item_dir("A"), "a.m4a")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as handle:
            handle.write(on_disk)

    def fetch(self, item: Any) -> str:
        row = self.ledger.get("A")
        assert row is not None
        return self.pipeline._fetch(row, item)

    def close(self) -> None:
        self.ledger.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class ReplacedBytesAreFetchedAgainRatherThanReused(unittest.TestCase):
    """The reuse branch asked the row about itself, and the row always agreed.

    Somebody records over a note in OneDrive while it is still queued — a second take of the
    same site walk, saved over the first. The row is at FETCHED, holding the hash of the
    first take, and the first take is still in the work directory. Hashing that file and
    comparing it to the row's own ``content_hash`` can only ever say yes, because those two
    describe the same bytes: it is the row remembering its own download. So the old audio was
    reused, transcribed, and published under the new recording's name and marked done, and
    nothing anywhere said the second take had never been read.
    """

    def setUp(self) -> None:
        self.run = _Fetching(OLD_BYTES, serves=NEW_BYTES)
        self.addCleanup(self.run.close)

    def test_the_replacement_is_downloaded_and_the_stale_file_is_gone(self) -> None:
        path = self.run.fetch(_drive_item(NEW_BYTES))

        self.assertEqual(len(self.run.graph.downloads), 1,
                         "the file on disk is last week's recording and was reused anyway")
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), NEW_BYTES)

    def test_the_row_then_remembers_the_replacement_and_not_the_original(self) -> None:
        """Otherwise the transcript cache below serves the old recording's words."""
        self.run.fetch(_drive_item(NEW_BYTES))

        row = self.run.ledger.get("A")
        assert row is not None
        self.assertEqual(row.content_hash, _sha256(NEW_BYTES))
        self.assertEqual(row.size, len(NEW_BYTES))

    def test_a_replacement_of_the_very_same_length_is_caught_too(self) -> None:
        """Not a size check wearing a hash's clothes.

        A re-record of the same length is the case a byte count cannot see, and it is not
        exotic: two takes of the same short approval come out within a few bytes of each
        other, and OneDrive pads nothing.
        """
        same_length = bytes(b ^ 0x5A for b in OLD_BYTES)
        self.assertEqual(len(same_length), len(OLD_BYTES))
        run = _Fetching(OLD_BYTES, serves=same_length)
        self.addCleanup(run.close)

        path = run.fetch(_drive_item(same_length))

        self.assertEqual(len(run.graph.downloads), 1)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), same_length)


class AnUntouchedRecordingIsStillReused(unittest.TestCase):
    """The negative control, and the reason the branch exists.

    An hour-long site meeting that was downloaded before the service was restarted must not
    be pulled down the wire a second time. If this test ever fails, the fix above has turned
    a restart into a full re-download of the backlog over somebody's uncapped mobile link.
    """

    def setUp(self) -> None:
        self.run = _Fetching(OLD_BYTES, serves=OLD_BYTES)
        self.addCleanup(self.run.close)

    def test_nothing_is_downloaded_and_the_file_is_handed_back(self) -> None:
        path = self.run.fetch(_drive_item(OLD_BYTES))

        self.assertEqual(self.run.graph.downloads, [])
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), OLD_BYTES)


class AChangeWhileItIsStillBeingWorkedOnIsWrittenDown(unittest.TestCase):
    """The history only ever recorded a replacement after the recording had finished.

    Which is the half nobody can act on. A recording replaced after its transcript was
    written is a loss already taken; a recording replaced while it is queued is one somebody
    can still do something about, and it was the one that left no trace at all. The state
    column cannot show it either — the row quietly takes the new size and etag — so a person
    looking for why a transcript does not match the audio had nothing to find.
    """

    def _replaced(self, state: str) -> list[dict[str, Any]]:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(
                DriveItem(item_id="A", name="a.m4a", size=900, etag='"e1"')
            )
            if state != State.DISCOVERED:
                fields: dict[str, Any] = {}
                if state == State.DONE:
                    fields = {"transcript_name": "t.md", "summary_name": "_s.md",
                              "actions_name": "_a.md"}
                ledger.advance("A", state, **fields)
            ledger.upsert_discovered(
                DriveItem(item_id="A", name="a.m4a", size=15000, etag='"e2"')
            )
            return [e for e in ledger.history("A") if str(e["kind"]).startswith("changed")]

    def test_a_row_at_fetched_gets_an_event_naming_the_two_sizes(self) -> None:
        events = self._replaced(State.FETCHED)

        self.assertEqual([e["kind"] for e in events], ["changed-while-working"])
        self.assertIn("900->15000", events[0]["detail"])
        self.assertIn("e1", events[0]["detail"])

    def test_the_row_keeps_its_state_so_nothing_is_reprocessed_from_the_top(self) -> None:
        with Ledger(":memory:") as ledger:
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a", size=900, etag='"e1"'))
            ledger.advance("A", State.FETCHED)
            ledger.upsert_discovered(DriveItem(item_id="A", name="a.m4a", size=15000, etag='"e2"'))
            row = ledger.get("A")
            assert row is not None
            self.assertEqual(row.state, State.FETCHED)
            self.assertEqual(row.size, 15000)

    def test_a_finished_row_still_says_it_was_replaced_after_we_finished(self) -> None:
        """The name matters. The two situations need different things from a person."""
        events = self._replaced(State.DONE)

        self.assertEqual([e["kind"] for e in events], ["changed-after-finish"])

    def test_a_recording_that_did_not_change_writes_nothing(self) -> None:
        """Otherwise every poll of every unchanged recording fills the history with noise."""
        with Ledger(":memory:") as ledger:
            item = DriveItem(item_id="A", name="a.m4a", size=900, etag='"e1"')
            ledger.upsert_discovered(item)
            ledger.advance("A", State.FETCHED)
            ledger.upsert_discovered(item)
            self.assertEqual(
                [e for e in ledger.history("A") if str(e["kind"]).startswith("changed")], []
            )


class _DeadNetworkOpener:
    """Every call fails the way a machine with no network yet fails: no answer at all."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, request: Any, timeout: float | None = None) -> Any:
        self.calls += 1
        raise urllib.error.URLError("[Errno -3] Temporary failure in name resolution")


class _RefusingOpener:
    """The token endpoint answers, and what it says is no."""

    def open(self, request: Any, timeout: float | None = None) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        raise urllib.error.HTTPError(
            url, 401, "Unauthorized", {},  # type: ignore[arg-type]
            io.BytesIO(b'{"error":"invalid_client","error_description":'
                       b'"AADSTS7000222: the provided client secret keys are expired"}'),
        )


def _client(opener: Any) -> GraphClient:
    return GraphClient(
        tenant_id="tenant", client_id="client", client_secret="secret", user_id="drive-owner",
        retry=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=False),
        sleep=lambda _s: None,
        opener=opener,
    )


class _SilentHeartbeat:
    configured = False

    def success(self, note: str = "") -> None:
        return None

    def fail(self, note: str = "") -> None:
        return None


class AnUnreachableSignInEndpointDoesNotStopTheService(unittest.TestCase):
    """The other half of ``AnExpiredCredentialStopsTheService``, and its opposite.

    A rejected credential has to stop the service, and does. But the boot after a power cut
    brings the unit up before the network, the first token call fails on name resolution,
    and that was reported as the same thing — a credential fault, which exits the worker.
    Systemd restarts it, the network is still not up, and after a handful of quick restarts
    the unit is parked in ``failed`` and stays down until somebody notices, over a blip that
    would have cleared itself on the next poll thirty seconds later.

    Nothing refused anything here. Nobody has looked at our secret at all.
    """

    def test_a_name_resolution_failure_is_not_reported_as_a_bad_credential(self) -> None:
        graph = _client(_DeadNetworkOpener())

        with self.assertRaises(GraphAuthUnreachable) as raised:
            graph.get_item("A")
        self.assertNotIsInstance(raised.exception, GraphAuthError)

    def test_the_poll_records_a_cycle_error_instead_of_taking_the_service_down(self) -> None:
        with Ledger(":memory:") as ledger:
            hand = worker.Worker(support.make_config(), ledger, _client(_DeadNetworkOpener()),
                                 heartbeat=_SilentHeartbeat())
            result = hand.poll()          # no PipelineFatal: this is a bad minute, not a fault
            self.assertFalse(result.ok)
            self.assertIn("Unreachable", result.error)

    def test_the_pipeline_calls_it_retryable_and_says_no_credential_was_refused(self) -> None:
        retryable, reason = _classify(
            GraphAuthUnreachable("could not reach the token endpoint: name resolution failed")
        )
        self.assertTrue(retryable)
        self.assertIn("no credential was refused", reason)

    def test_but_a_refused_credential_still_stops_the_service(self) -> None:
        """The control. If this ever passes quietly, the fix has disarmed the alarm."""
        with Ledger(":memory:") as ledger:
            hand = worker.Worker(support.make_config(), ledger, _client(_RefusingOpener()),
                                 heartbeat=_SilentHeartbeat())
            with self.assertRaises(PipelineFatal) as raised:
                hand.poll()
            self.assertIn("expired", str(raised.exception))

    def test_and_a_refused_credential_still_raises_out_of_the_classifier(self) -> None:
        with self.assertRaises(PipelineFatal):
            _classify(GraphAuthError("invalid_client AADSTS7000222: client secret keys are expired"))


if __name__ == "__main__":
    unittest.main()
