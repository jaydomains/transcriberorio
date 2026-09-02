"""Three things that undid a person's work, or told them it had been done when it had not.

Each is the same shape: the service treating an act by a human as though it were one of its
own. A requeue that the sweep reverses hours later, an email it re-sends to people who
already have it, and a watchdog told to expect a signal the service never sends.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber.ledger import Ledger
from transcriber.models import DriveItem, State


class RequeueingAfterFixingTheCause(unittest.TestCase):
    """The count from the failures a person just fixed must not survive their fix.

    A recording quarantined after three engine failures carries attempts=3. Put back in the
    queue with that intact, the first transient failure takes it straight past max_attempts
    again — and if the worker does not reach it first, the nightly sweep re-quarantines it
    on the count alone, naming the errors from before the fix. Either way the requeue was
    undone within hours and the recording was never actually retried once.
    """

    def setUp(self) -> None:
        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)
        self.ledger.upsert_discovered(
            DriveItem(item_id="rec-9", name="Call Carel.m4a", size=1000,
                      created_at="2026-08-26T09:00:00Z", modified_at="2026-08-26T09:00:00Z"),
            route="james",
        )
        for _ in range(3):
            self.ledger.record_attempt("rec-9", "the engine answered 503")
        self.ledger.quarantine("rec-9", "three engine failures in a row")

    def test_the_recording_starts_out_of_attempts(self) -> None:
        row = self.ledger.get("rec-9")
        self.assertEqual(row.attempts, 3)
        self.assertEqual(row.state, State.QUARANTINED)

    def test_a_persons_requeue_gives_it_its_attempts_back(self) -> None:
        self.ledger.requeue("rec-9", "azure was down, it is back", reset_attempts=True)
        row = self.ledger.get("rec-9")
        self.assertEqual(
            row.attempts, 0,
            "the count from the failures he just fixed survived his fix, so the next sweep "
            "re-quarantined it without it ever being retried",
        )
        self.assertEqual(row.state, State.DISCOVERED)

    def test_and_the_stale_reason_does_not_follow_it_back(self) -> None:
        self.ledger.requeue("rec-9", "azure was down, it is back", reset_attempts=True)
        row = self.ledger.get("rec-9")
        self.assertFalse(row.last_error)

    def test_but_the_machines_own_requeue_still_counts(self) -> None:
        """The sweep re-queues rows too. max_attempts must still bound the machine."""
        self.ledger.requeue("rec-9", "the sweep found it unfinished")
        self.assertEqual(self.ledger.get("rec-9").attempts, 3)

    def test_and_nothing_is_destroyed_either_way(self) -> None:
        """Every attempt that ever happened is still in the history."""
        self.ledger.requeue("rec-9", "azure was down, it is back", reset_attempts=True)
        kinds = [e["kind"] for e in self.ledger.history("rec-9")]
        self.assertEqual(kinds.count("attempt-failed"), 3)
        self.assertIn("requeued", kinds)


class _Relay:
    """An SMTP relay that refuses one named address and takes everything else."""

    sent: list[str] = []
    refused_address = ""

    def __init__(self, *a: object, **k: object) -> None:
        pass

    def __enter__(self) -> "_Relay":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, *a: object, **k: object) -> None: return None
    def login(self, *a: object, **k: object) -> None: return None
    def quit(self) -> None: return None

    def send_message(self, message, *a: object, **k: object) -> None:
        to = str(message["To"])
        if to == type(self).refused_address:
            raise RuntimeError("550 5.1.1 unknown recipient")
        type(self).sent.append(to)


class OneDeadMailboxDoesNotStopTheMorningEmail(unittest.TestCase):
    """A mailbox deleted when somebody left used to abort the whole send.

    Everyone earlier in the list had the email; everyone later got nothing; and the send
    reported failure, which leaves the day unmarked — so the worker rebuilt and re-sent the
    same email a quarter of an hour later, and again, roughly seventy-two times before
    midnight, to people who already had it. The log and the monitor both said it could not
    be sent.
    """

    def setUp(self) -> None:
        from . import support

        _Relay.sent = []
        _Relay.refused_address = "office@kbc.example"
        self.config = support.make_config()
        self.config.smtp_to = ["jay@kbc.example", "office@kbc.example", "sam@kbc.example"]
        tmp = tempfile.mkdtemp()
        self.ledger = Ledger(os.path.join(tmp, "ledger.sqlite"))
        self.ledger.migrate()
        self.addCleanup(self.ledger.close)

    def _send(self):
        from transcriber import digest as digest_module

        built = digest_module.build(self.config, self.ledger, day="2026-08-27")
        return digest_module.send(self.config, built, smtp_factory=_Relay)

    def test_everyone_reachable_still_gets_it(self) -> None:
        self._send()
        self.assertIn("jay@kbc.example", _Relay.sent)
        self.assertIn(
            "sam@kbc.example", _Relay.sent,
            "the address after the dead one was skipped, so that person never got the email",
        )

    def test_the_day_is_not_left_unmarked_so_it_cannot_re_send(self) -> None:
        result = self._send()
        self.assertTrue(
            result.ok,
            "reporting the whole send as failed leaves the day unmarked, and the worker "
            "re-sends the same email to the same people every fifteen minutes",
        )

    def test_but_the_refused_address_is_named(self) -> None:
        result = self._send()
        self.assertIn("office@kbc.example", result.detail)

    def test_a_temporary_refusal_says_so(self) -> None:
        """Greylisting is "not now"; a deleted mailbox is "not ever". Different jobs."""
        import smtplib

        class _Greylisting(_Relay):
            def send_message(self, message, *a: object, **k: object) -> None:
                to = str(message["To"])
                if to == type(self).refused_address:
                    raise smtplib.SMTPRecipientsRefused({to: (451, b"4.7.1 greylisted")})
                type(self).sent.append(to)

        _Greylisting.sent = []
        _Greylisting.refused_address = "office@kbc.example"
        from transcriber import digest as digest_module

        built = digest_module.build(self.config, self.ledger, day="2026-08-27")
        result = digest_module.send(self.config, built, smtp_factory=_Greylisting)
        self.assertIn("temporarily", result.detail)
        self.assertTrue(result.ok, "the reachable people still have it and must not be re-sent")

    def test_a_permanent_refusal_says_that_instead(self) -> None:
        import smtplib

        class _Gone(_Relay):
            def send_message(self, message, *a: object, **k: object) -> None:
                to = str(message["To"])
                if to == type(self).refused_address:
                    raise smtplib.SMTPRecipientsRefused({to: (550, b"5.1.1 unknown")})
                type(self).sent.append(to)

        _Gone.sent = []
        _Gone.refused_address = "office@kbc.example"
        from transcriber import digest as digest_module

        built = digest_module.build(self.config, self.ledger, day="2026-08-27")
        result = digest_module.send(self.config, built, smtp_factory=_Gone)
        self.assertIn("permanently", result.detail)

    def test_and_a_relay_that_takes_nothing_is_still_a_failed_morning(self) -> None:
        _Relay.refused_address = "jay@kbc.example"
        self.config.smtp_to = ["jay@kbc.example"]
        self.assertFalse(self._send().ok)


if __name__ == "__main__":
    unittest.main()
