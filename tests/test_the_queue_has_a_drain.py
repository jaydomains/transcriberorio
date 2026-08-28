"""The one way a held passage ever gets answered, and whether anybody can reach it.

Decision 1 is "a web page, linked from the 06:00 email". Decision 4 is "nothing is decided
for him, ever" — no auto-release, no auto-discard, no deadline. Together those make the queue
load-bearing state with exactly one drain, and the drain is the link in that email.

It did not work. ``links_for_pending`` existed, was exported, and was called from nowhere.
The morning email printed the bare ``GATE_REVIEW_BASE_URL``, which without a token renders
"This link has expired. Open the link in this morning's email" — pointing at the email the
dead link came from. There is no login form and no other issuance path, so the only working
link came from an operator running ``transcriber review --link <person>`` on the service
host, by hand, per person, every thirty-six hours. A queue nobody can open never drains, and
because nothing expires or releases itself it simply fills — and the record hollows out,
which is the failure this whole service exists to cure, wearing a different coat.

And a second half of the same hole: a staff member reviews their own held passages, and a
staff member is not on ``SMTP_TO``. Even a working link in the digest would only ever have
reached the principal, whose own decision says most of the queue is not his to read.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any

from tests import support
from transcriber import digest as digest_module
from transcriber import review_server
from transcriber.ledger import Ledger
from transcriber.models import Route
from transcriber.withheld import Decision, HeldSpan, WithheldStore

BASE_URL = "https://review.invalid/held"

ROUTE = Route(
    name="calls", label="Phone calls", source_folder_id="S", output_folder_id="O",
    archive_folder_id="", engine="", enabled=True,
)

JAMES = "james@invalid"
SIPHO = "sipho@invalid"


class _Smtp:
    """An SMTP server that keeps every message it was handed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.sent: list[Any] = []

    def __enter__(self) -> "_Smtp":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def send_message(self, message: Any) -> None:
        self.sent.append(message)

    def bodies_by_recipient(self) -> dict[str, str]:
        return {
            str(message["To"]): message.get_content()
            for message in self.sent
        }


class _World:
    """A configured service with two people's passages waiting in the store."""

    def __init__(self, mode: str = "on") -> None:
        self.dir = tempfile.mkdtemp()
        self.config = support.make_config(
            routes=(ROUTE,),
            work_dir=os.path.join(self.dir, "work"),
            ledger_path=os.path.join(self.dir, "ledger.sqlite3"),
            smtp_to=(JAMES,),
            gate_mode=mode,
            gate_held_store=os.path.join(self.dir, "held.sqlite3"),
            gate_review_base_url=BASE_URL,
            route_reviewers={"calls": SIPHO},
        )
        self.ledger = Ledger(self.config.ledger_path)
        self.store = WithheldStore(self.config.gate_held_store)
        for who, category, words in (
            (SIPHO, "personal_circumstances", "his wife is in hospital until Thursday"),
            (JAMES, "staff_matter", "the second written warning goes on file on Friday"),
        ):
            self.store.hold(
                HeldSpan(
                    item_id=f"item-{who}", start=0, end=len(words), text=words,
                    category=category, route="calls", site="Beach Court",
                    source_name="Call_260827_120055.m4a",
                    recorded_at="2026-08-27T09:00:00Z",
                    recorded_by=SIPHO, reviewer=who,
                ),
                mode="on",
            )

    def digest(self) -> Any:
        return digest_module.build(self.config, self.ledger, day="2026-08-27", now=0.0)

    def send(self) -> _Smtp:
        server = _Smtp()
        result = digest_module.send(
            self.config,
            self.digest(),
            smtp_factory=lambda *a, **k: server,
            links=digest_module.review_links(self.config),
        )
        self.result = result
        return server

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


class TheMorningEmailCarriesALinkThatOpens(unittest.TestCase):
    def setUp(self) -> None:
        self.world = _World("on")
        self.addCleanup(self.world.close)

    def test_the_held_section_carries_a_tokenised_url(self) -> None:
        server = self.world.send()
        body = server.bodies_by_recipient()[JAMES]
        self.assertIn("Answer them here:", body)
        line = next(l for l in body.splitlines() if "Answer them here:" in l)
        self.assertIn("?k=", line, "the emailed link carries no token, so it renders as expired")
        self.assertNotEqual(
            line.strip(), f"Answer them here: {BASE_URL}",
            "the bare base URL is the dead link this test exists to catch",
        )

    def test_the_link_in_the_email_actually_verifies(self) -> None:
        """End to end: take the URL out of the sent body and present it to the server."""
        server = self.world.send()
        body = server.bodies_by_recipient()[JAMES]
        line = next(l for l in body.splitlines() if "Answer them here:" in l)
        url = line.split("Answer them here:", 1)[1].strip()
        token = url.partition("?k=")[2]
        self.assertTrue(token)

        service = review_server.service_from_config(self.world.config)
        session = service.tokens.verify(token)
        self.assertIsNotNone(session, "the link in the email does not open the page")
        self.assertEqual(session.reviewer, JAMES)

    def test_each_person_gets_their_own_link_and_only_their_own(self) -> None:
        """A token is a capability. One body for everybody would hand it round."""
        server = self.world.send()
        bodies = server.bodies_by_recipient()
        self.assertIn(JAMES, bodies)
        self.assertIn(SIPHO, bodies)

        service = review_server.service_from_config(self.world.config)
        for who, body in bodies.items():
            tokens = [
                word.partition("?k=")[2]
                for word in body.split()
                if "?k=" in word
            ]
            self.assertEqual(len(tokens), 1, f"{who}'s message carries {len(tokens)} links")
            session = service.tokens.verify(tokens[0])
            self.assertIsNotNone(session)
            self.assertEqual(
                session.reviewer, who,
                "a message carried somebody else's capability",
            )

    def test_a_staff_reviewer_is_written_to_even_though_the_digest_is_not_theirs(self) -> None:
        """Decision 6's drain. A staff member is never on SMTP_TO."""
        server = self.world.send()
        self.assertNotIn(SIPHO, self.world.config.smtp_to)
        bodies = server.bodies_by_recipient()
        self.assertIn(SIPHO, bodies, "the only person who can answer their own queue is unreachable")
        self.assertEqual(self.world.result.reviewers, 1)

    def test_a_staff_reviewer_is_not_sent_the_whole_service_report(self) -> None:
        """Their message says "you have some waiting", and nothing about anybody else."""
        server = self.world.send()
        theirs = server.bodies_by_recipient()[SIPHO]
        self.assertIn("Answer them here:", theirs)
        for leak in ("Beach Court", "his wife is in hospital", "the second written warning",
                     JAMES, "Recordings:"):
            self.assertNotIn(leak, theirs, f"a reviewer's own-queue email carried {leak!r}")

    def test_no_message_carries_a_word_of_what_was_held(self) -> None:
        server = self.world.send()
        for who, body in server.bodies_by_recipient().items():
            for words in ("his wife is in hospital until Thursday",
                          "the second written warning goes on file on Friday"):
                self.assertNotIn(words, body, f"{who}'s message quotes a held passage")


class ShadowMintsNothing(unittest.TestCase):
    """Nothing is withheld, so there is nothing to approve and no capability to create."""

    def test_no_links_and_one_message(self) -> None:
        world = _World("shadow")
        self.addCleanup(world.close)
        self.assertEqual(digest_module.review_links(world.config), {})
        server = world.send()
        self.assertEqual(list(server.bodies_by_recipient()), [JAMES])
        self.assertEqual(world.result.reviewers, 0)


class TheEmailStillGoesOutWhenLinksCannotBeMinted(unittest.TestCase):
    """A morning that fails silently is the failure this service exists to remove."""

    def test_a_broken_token_store_does_not_stop_the_digest(self) -> None:
        world = _World("on")
        self.addCleanup(world.close)
        # A base URL the link builder refuses is the simplest way to break minting.
        world.config = support.make_config(
            **{
                **{k: getattr(world.config, k) for k in (
                    "routes", "work_dir", "ledger_path", "smtp_to", "gate_mode",
                    "gate_held_store", "route_reviewers",
                )},
                "gate_review_base_url": "",
            }
        )
        self.assertEqual(digest_module.review_links(world.config), {})
        server = world.send()
        self.assertTrue(world.result.ok)
        self.assertEqual(list(server.bodies_by_recipient()), [JAMES])


class TheLinkIsNeverAppendedTwice(unittest.TestCase):
    """The tokenised link starts with the base URL, which makes naive substitution wrong."""

    def test_substituting_the_line_leaves_exactly_one_query_string(self) -> None:
        body = "  Answer them here: https://review.invalid/held\n  Or from the host: ...\n"
        link = "https://review.invalid/held/?k=abc123"
        out = digest_module._personalised(body, "https://review.invalid/held", link)
        self.assertEqual(out.count("?k="), 1, out)
        self.assertIn(f"Answer them here: {link}", out)

    def test_a_body_with_no_link_line_is_untouched(self) -> None:
        body = "Nothing is being held.\n"
        self.assertEqual(
            digest_module._personalised(body, BASE_URL, f"{BASE_URL}/?k=x"), body
        )


class TheTokenNeverReachesALogLine(unittest.TestCase):
    def test_minting_registers_the_token_as_a_secret(self) -> None:
        from transcriber import logging_setup

        world = _World("on")
        self.addCleanup(world.close)
        links = digest_module.review_links(world.config)
        self.assertTrue(links)
        for url in links.values():
            token = url.partition("?k=")[2]
            self.assertNotIn(token, logging_setup.scrub(f"the link is {url}"))


class TheQueueIsStillOnlyEmptiedByAPerson(unittest.TestCase):
    """Whatever else changes, decision 4 does not."""

    def test_sending_the_email_releases_and_discards_nothing(self) -> None:
        world = _World("on")
        self.addCleanup(world.close)
        before = world.store.overview(decision=Decision.PENDING)["count"]
        world.send()
        after = world.store.overview(decision=Decision.PENDING)["count"]
        self.assertEqual(before, after, "the morning email decided something")
        self.assertEqual(after, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
