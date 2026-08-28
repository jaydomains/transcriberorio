"""The page held passages are answered on, and the server that hands it out.

This page shows the most sensitive text in the service on a phone, on a site, with a client
standing there. So the tests here are not mostly about rendering: they are about the four
things that would each be a real harm.

* A link opens one person's queue and nobody else's — including James's link, which must
  never carry a staff member's words. Decision 6 is the decision that keeps staff willing to
  record at all, and it is enforced in the query rather than in the template.
* Nothing held reaches a URL, a query string, a redirect or a log line.
* An answer is idempotent, undoable for a few seconds, and safe when two devices answer at
  once — and no answer is ever recorded in a machine's name.
* Approving and refusing cost exactly the same tap, because a page where refusing is easier
  hollows out the record while still looking like a gate.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import unittest
import urllib.parse

from transcriber import review_page as rp
from transcriber import review_server as rs
from transcriber.withheld import (
    ASKED_NOT_RECORDED,
    LEGAL_EXPOSURE,
    MODE_ON,
    MODE_SHADOW,
    STAFF_MATTER,
    Decision,
    HeldSpan,
    WithheldStore,
)

JAMES = "james@example.invalid"
STAFF = "thabo@example.invalid"

TRANSCRIPT = (
    "Right, so on Beach Court the scaffolding comes down Thursday. "
    "Between us, we are exposed on the balustrade failure and the attorney says do not put "
    "that in writing. "
    "Also the rate for the remedial is R4,500 a day, which is fine."
)
HELD_WORDS = "we are exposed on the balustrade failure"
STAFF_WORDS = "he is on a final written warning after the Tuesday hearing"


def span_at(text: str, words: str, **kw) -> HeldSpan:
    """A held span whose offsets really do hold those words — the store insists on it."""
    start = text.index(words)
    fields = {
        "item_id": "rec-1",
        "category": LEGAL_EXPOSURE,
        "site": "Beach Court",
        "source_name": "Call Anton_260824_091500.m4a",
        "recorded_at": "2026-08-24T09:15:00Z",
        "reviewer": JAMES,
        "recorded_by": JAMES,
        "context_before": text[max(0, start - 60):start],
        "context_after": text[start + len(words):start + len(words) + 60],
        "reason": "an admission of our own liability, and a request not to write it down",
        "subject": "a liability question",
    }
    fields.update(kw)
    return HeldSpan(start=start, end=start + len(words), text=words, **fields)


class Fixture:
    """A store, a token store and a service, on disk, with two people's queues in it."""

    def __init__(self, case: unittest.TestCase, *, mode: str = MODE_ON, undo: int = 8) -> None:
        self.dir = tempfile.TemporaryDirectory()
        case.addCleanup(self.dir.cleanup)
        self.store = WithheldStore(os.path.join(self.dir.name, "held.sqlite3"))
        self.tokens = rs.TokenStore(os.path.join(self.dir.name, "held-tokens.sqlite3"))
        self.service = rs.ReviewService(
            self.store,
            self.tokens,
            principal=JAMES,
            mode=mode,
            undo_seconds=undo,
        )
        self.mode = mode

    def hold_james(self) -> str:
        span = span_at(TRANSCRIPT, HELD_WORDS)
        return self.store.hold(span, mode=self.mode).hold_id

    def hold_staff(self) -> str:
        text = "So about Sipho, " + STAFF_WORDS + ", and that is between us."
        span = span_at(
            text,
            STAFF_WORDS,
            item_id="rec-2",
            category=STAFF_MATTER,
            reviewer=STAFF,
            recorded_by=STAFF,
            site="Rosebank",
            source_name="Call Sipho_260825_140000.m4a",
            subject="a staff matter",
            reason="a warning and a hearing",
        )
        return self.store.hold(span, mode=self.mode).hold_id

    def session(self, who: str) -> rs.Session:
        issued = self.tokens.issue(who)
        session = self.tokens.verify(issued.token, principal=JAMES)
        assert session is not None
        self.last_token = issued.token
        return session

    def html(self, who: str) -> str:
        session = self.session(who)
        model = rs._with_token(self.service.page_for(session), self.last_token)
        return rp.render(model, nonce="test-nonce")


class ALinkShowsOneQueueAndNobodyElses(unittest.TestCase):
    """Decision 6, which is what keeps staff willing to keep a folder at all."""

    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.fx.hold_james()
        self.fx.hold_staff()

    def test_james_sees_his_own_words(self) -> None:
        self.assertIn(HELD_WORDS, self.fx.html(JAMES))

    def test_james_never_sees_a_staff_members_words(self) -> None:
        html = self.fx.html(JAMES)
        self.assertNotIn(STAFF_WORDS, html)
        self.assertNotIn("final written warning", html)

    def test_james_does_see_the_count_and_the_site(self) -> None:
        html = self.fx.html(JAMES)
        self.assertIn("Waiting with other people", html)
        self.assertIn("Rosebank", html)
        self.assertIn("1 waiting", html)

    def test_a_staff_member_sees_only_their_own(self) -> None:
        html = self.fx.html(STAFF)
        self.assertIn(STAFF_WORDS, html)
        self.assertNotIn(HELD_WORDS, html)

    def test_a_staff_member_is_not_shown_anybody_elses_counts(self) -> None:
        self.assertNotIn("Waiting with other people", self.fx.html(STAFF))

    def test_the_scoping_is_in_the_query_not_the_template(self) -> None:
        """The page model handed to the renderer already has no other queue in it."""
        session = self.fx.session(JAMES)
        model = self.fx.service.page_for(session)
        every_word = " ".join(
            i.words + i.before + i.after + i.reason
            for r in model.recordings for i in r.items
        )
        self.assertNotIn(STAFF_WORDS, every_word)
        for row in model.elsewhere:
            self.assertFalse(hasattr(row, "text"), "a summary row must have nowhere to put words")

    def test_answering_somebody_elses_passage_is_refused(self) -> None:
        staff_hold = self.fx.hold_staff()
        session = self.fx.session(JAMES)
        outcome = self.fx.service.answer(session, staff_hold, "release")
        self.assertFalse(outcome.ok)
        self.assertEqual("refused-scope", outcome.state)
        self.assertEqual(Decision.PENDING, self.fx.store.get(staff_hold).decision)

    def test_a_missing_passage_and_somebody_elses_answer_the_same_way(self) -> None:
        session = self.fx.session(JAMES)
        mine = self.fx.service.answer(session, "0" * 16, "release")
        theirs = self.fx.service.answer(session, self.fx.hold_staff(), "release")
        self.assertEqual(mine.message, theirs.message)


class TheLinkIsACapabilityAndNotAPassword(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.fx.hold_james()

    def test_a_good_token_signs_the_right_person_in(self) -> None:
        issued = self.fx.tokens.issue(JAMES)
        session = self.fx.tokens.verify(issued.token, principal=JAMES)
        self.assertIsNotNone(session)
        self.assertEqual(JAMES, session.reviewer)
        self.assertTrue(session.is_principal)

    def test_a_staff_token_is_not_the_principal(self) -> None:
        issued = self.fx.tokens.issue(STAFF)
        self.assertFalse(self.fx.tokens.verify(issued.token, principal=JAMES).is_principal)

    def test_the_token_itself_is_not_stored_anywhere(self) -> None:
        issued = self.fx.tokens.issue(JAMES)
        self.fx.tokens.close()
        with open(self.fx.tokens.path, "rb") as handle:
            raw = handle.read()
        _, _, verifier = issued.token.partition(".")
        self.assertNotIn(verifier.encode(), raw)
        self.assertNotIn(issued.token.encode(), raw)

    def test_the_right_selector_with_the_wrong_secret_fails(self) -> None:
        issued = self.fx.tokens.issue(JAMES)
        forged = issued.selector + "." + "x" * 43
        self.assertIsNone(self.fx.tokens.verify(forged, principal=JAMES))

    def test_a_token_that_has_expired_stops_working(self) -> None:
        issued = self.fx.tokens.issue(JAMES, hours=1, now=time.time() - 7200)
        self.assertIsNone(self.fx.tokens.verify(issued.token))

    def test_a_revoked_token_stops_working_immediately(self) -> None:
        issued = self.fx.tokens.issue(JAMES)
        self.assertEqual(1, self.fx.tokens.revoke_for(JAMES, why="lost phone"))
        self.assertIsNone(self.fx.tokens.verify(issued.token))

    def test_issuing_todays_link_kills_yesterdays(self) -> None:
        old = self.fx.tokens.issue(JAMES)
        new = self.fx.tokens.issue(JAMES)
        self.assertIsNone(self.fx.tokens.verify(old.token))
        self.assertIsNotNone(self.fx.tokens.verify(new.token))
        self.assertEqual(1, self.fx.tokens.live_for(JAMES))

    def test_rubbish_is_refused_without_raising(self) -> None:
        for junk in ("", "   ", "no-dot", "..", "a.b", "x" * 400):
            self.assertIsNone(self.fx.tokens.verify(junk))

    def test_a_link_needs_a_named_person(self) -> None:
        with self.assertRaises(rs.ReviewError):
            self.fx.tokens.issue("  ")

    def test_the_form_token_is_tied_to_the_link(self) -> None:
        one = self.fx.tokens.verify(self.fx.tokens.issue(JAMES).token)
        two = self.fx.tokens.verify(self.fx.tokens.issue(STAFF).token)
        self.assertNotEqual(one.csrf, two.csrf)
        self.assertEqual(64, len(one.csrf))


class AnAnswerIsIdempotentAndBrieflyUndoable(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self, undo=8)
        self.hold = self.fx.hold_james()
        self.session = self.fx.session(JAMES)

    def test_an_answer_waits_out_its_undo_window_before_it_is_written(self) -> None:
        outcome = self.fx.service.answer(self.session, self.hold, "release")
        self.assertTrue(outcome.ok)
        self.assertEqual("queued", outcome.state)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_undo_inside_the_window_leaves_the_passage_waiting(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        undone = self.fx.service.undo(self.session, self.hold)
        self.assertTrue(undone.ok)
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_after_the_window_it_is_recorded_in_the_persons_name(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.commit_due(force=True)
        record = self.fx.store.get(self.hold)
        self.assertEqual(Decision.RELEASED, record.decision)
        self.assertEqual(JAMES, record.answered_by)

    def test_undo_after_the_window_changes_nothing_and_says_so(self) -> None:
        self.fx.service.answer(self.session, self.hold, "refuse")
        self.fx.service.commit_due(force=True)
        late = self.fx.service.undo(self.session, self.hold)
        self.assertFalse(late.ok)
        self.assertEqual("too-late", late.state)
        self.assertEqual(Decision.REFUSED, self.fx.store.get(self.hold).decision)

    def test_the_same_answer_twice_is_the_same_answer(self) -> None:
        first = self.fx.service.answer(self.session, self.hold, "refuse")
        second = self.fx.service.answer(self.session, self.hold, "refuse")
        self.assertTrue(second.ok)
        self.assertEqual(first.undo_until_ms, second.undo_until_ms, "a re-send must not extend the window")
        self.fx.service.commit_due(force=True)
        self.assertEqual(1, self.fx.store.get(self.hold).decisions_made)

    def test_the_same_answer_again_after_it_is_written_is_not_an_error(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.commit_due(force=True)
        again = self.fx.service.answer(self.session, self.hold, "release")
        self.assertTrue(again.ok)
        self.assertEqual("already", again.state)
        self.assertEqual(1, self.fx.store.get(self.hold).decisions_made)

    def test_a_second_device_answering_the_other_way_is_a_conflict_not_an_overwrite(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.commit_due(force=True)
        clash = self.fx.service.answer(self.session, self.hold, "refuse")
        self.assertFalse(clash.ok)
        self.assertEqual("conflict", clash.state)
        record = self.fx.store.get(self.hold)
        self.assertEqual(Decision.RELEASED, record.decision)
        self.assertEqual(1, record.decisions_made)

    def test_changing_your_mind_inside_the_window_is_allowed(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.answer(self.session, self.hold, "refuse")
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.REFUSED, self.fx.store.get(self.hold).decision)

    def test_the_answer_is_stamped_when_the_person_tapped_not_when_it_was_written(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        tapped = self.fx.service._pending[self.hold].answered_at
        time.sleep(0.01)
        self.fx.service.commit_due(force=True)
        self.assertEqual(tapped, self.fx.store.get(self.hold).decided_at)

    def test_stopping_the_server_writes_the_answers_a_person_gave(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.assertEqual(1, self.fx.service.flush())
        self.assertEqual(Decision.RELEASED, self.fx.store.get(self.hold).decision)

    def test_nothing_is_ever_recorded_in_a_machines_name(self) -> None:
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.commit_due(force=True)
        answered_by = self.fx.store.get(self.hold).answered_by
        self.assertNotIn(answered_by.casefold(), {"auto", "system", "timer", "agent", "transcriber"})
        self.assertEqual(JAMES, answered_by)

    def test_an_answer_the_page_does_not_know_is_refused(self) -> None:
        outcome = self.fx.service.answer(self.session, self.hold, "maybe")
        self.assertFalse(outcome.ok)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_a_release_can_be_followed_up_without_being_able_to_break_it(self) -> None:
        seen = []
        self.fx.service._on_decision = lambda record: seen.append(record.ref) or (_ for _ in ()).throw(RuntimeError("boom"))
        self.fx.service.answer(self.session, self.hold, "release")
        self.fx.service.commit_due(force=True)
        self.assertEqual(1, len(seen))
        self.assertEqual(Decision.RELEASED, self.fx.store.get(self.hold).decision)


class ItHoldsNothingWhileItIsDark(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self, mode=MODE_SHADOW)
        self.hold = self.fx.hold_james()
        self.session = self.fx.session(JAMES)

    def test_the_page_says_plainly_that_nothing_is_being_held(self) -> None:
        html = self.fx.html(JAMES)
        self.assertIn("The gate is in shadow", html)
        self.assertIn("Nothing is waiting for you", html)

    def test_a_shadow_row_cannot_be_answered(self) -> None:
        outcome = self.fx.service.answer(self.session, self.hold, "release")
        self.assertFalse(outcome.ok)
        self.assertEqual("shadow", outcome.state)

    def test_the_shadow_page_shows_the_measurement_rather_than_words(self) -> None:
        self.fx.store.record_pass("rec-1", spans=[span_at(TRANSCRIPT, HELD_WORDS)],
                                  transcript_chars=len(TRANSCRIPT), mode=MODE_SHADOW)
        html = self.fx.html(JAMES)
        self.assertIn("would", html)
        self.assertNotIn(HELD_WORDS, html)


class BothAnswersCostTheSameTap(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.fx.hold_james()
        self.html = self.fx.html(JAMES)

    def test_both_buttons_are_on_the_page(self) -> None:
        self.assertIn(rp.RELEASE_LABEL, self.html)
        self.assertIn(rp.REFUSE_LABEL, self.html)

    def test_the_two_buttons_differ_only_in_their_class_and_their_words(self) -> None:
        """Two buttons on screen, identical but for the class and the label."""
        visible = self.html.split("<template")[0]
        buttons = re.findall(
            r'<button class="(in|out)" type="submit" name="answer" value="(\w+)">([^<]+)</button>',
            visible,
        )
        self.assertEqual(2, len(buttons))
        self.assertEqual({"in", "out"}, {kind for kind, _, _ in buttons})
        self.assertEqual({"release", "refuse"}, {value for _, value, _ in buttons})

    def test_neither_button_is_bigger_than_the_other(self) -> None:
        """One rule sizes both; only the border colour is set per button."""
        size_rules = re.findall(r"\.decide button\.(in|out)\{([^}]*)\}", rp._CSS)
        self.assertEqual(2, len(size_rules))
        for _, body in size_rules:
            self.assertNotIn("font-size", body)
            self.assertNotIn("padding", body)
            self.assertNotIn("flex", body)
            self.assertIn("border-color", body)

    def test_there_is_no_way_to_refuse_everything_at_once(self) -> None:
        lowered = self.html.lower()
        for shortcut in ("refuse all", "keep all out", "reject all", "release all", "approve all"):
            self.assertNotIn(shortcut, lowered)


class NothingHeldReachesAUrlOrALog(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.fx.hold_james()

    def test_the_default_request_logger_is_silenced(self) -> None:
        """The base class writes ``GET /?k=<the token> HTTP/1.1`` to stderr. It must not."""
        captured = io.StringIO()
        real, sys.stderr = sys.stderr, captured
        try:
            rs.ReviewHandler.log_message(object(), "%s", "GET /?k=secret HTTP/1.1")
        finally:
            sys.stderr = real
        self.assertEqual("", captured.getvalue())

    def test_our_own_log_lines_carry_a_route_name_and_no_path(self) -> None:
        self.assertNotIn("self.path", _source_of(rs.ReviewHandler._respond))
        self.assertIn("route=route", _source_of(rs.ReviewHandler._respond))

    def test_a_link_carries_no_words_and_a_clean_base(self) -> None:
        url = rs.link_for("https://review.example.invalid", self.fx.tokens.issue(JAMES).token)
        self.assertTrue(url.startswith("https://review.example.invalid/?k="))
        self.assertNotIn(HELD_WORDS.split()[0], url)

    def test_a_page_with_no_address_to_send_to_is_refused_loudly(self) -> None:
        with self.assertRaises(rs.ReviewError):
            rs.link_for("", "anything")


class ThePageStandsOnItsOwn(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)
        self.fx.hold_james()
        self.html = self.fx.html(JAMES)

    def test_it_asks_the_network_for_nothing(self) -> None:
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<img", self.html)
        self.assertNotIn("<script src", self.html)
        self.assertNotIn("@import", self.html)

    def test_it_does_not_even_let_the_browser_ask_for_a_favicon(self) -> None:
        self.assertIn('<link rel="icon" href="data:,">', self.html)

    def test_the_style_and_the_script_carry_the_nonce(self) -> None:
        self.assertIn('<style nonce="test-nonce">', self.html)
        self.assertIn('<script nonce="test-nonce">', self.html)

    def test_held_words_are_escaped_not_rendered(self) -> None:
        text = "he said <script>alert(1)</script> & then left"
        store = self.fx.store
        store.hold(span_at(text, "<script>alert(1)</script>", item_id="rec-x",
                           category=ASKED_NOT_RECORDED, subject="a request", reason="asked"))
        html = self.fx.html(JAMES)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_the_page_shows_what_is_needed_to_decide_without_the_transcript(self) -> None:
        self.assertIn("Beach Court", self.html)
        self.assertIn("Anton", self.html)
        self.assertIn("Call Anton_260824_091500", self.html)
        self.assertIn("A legal matter", self.html)
        self.assertIn("scaffolding comes down Thursday", self.html)  # the context either side

    def test_an_email_address_is_never_typed_on_the_page(self) -> None:
        self.assertNotIn("@example.invalid", self.html)
        self.assertIn("james", self.html.lower())

    def test_an_expired_link_page_gives_nothing_away(self) -> None:
        notice = rp.render_notice("This link has expired", "Open today's email.", nonce="n")
        self.assertNotIn(HELD_WORDS, notice)
        self.assertNotIn("revoked", notice.lower())


class TheServerRefusesToPutHeldTextOnTheOpenNetwork(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture(self)

    def test_a_public_bind_without_a_certificate_is_refused_by_name(self) -> None:
        with self.assertRaises(rs.ReviewError) as caught:
            rs.build_server(self.fx.service, host="0.0.0.0", port=0)
        self.assertIn("certificate", str(caught.exception))

    def test_loopback_is_allowed_because_a_proxy_terminates_in_front(self) -> None:
        server = rs.build_server(self.fx.service, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.assertTrue(server.server_address[1] > 0)


class _Wire:
    """A request and its response, without a socket.

    The suite refuses outbound connections on purpose, and it is right to: the property it
    protects is that nothing here talks to anything real. So the handler is driven the way
    :mod:`http.server` drives it — bytes in, bytes out — with a stand-in for the socket. It
    exercises the same header parsing, the same routing, the same response writing, and it
    is faster and quieter than a real port.
    """

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = bytearray()

    def makefile(self, mode: str = "rb", *args, **kwargs):
        if "r" in mode:
            return io.BytesIO(self.payload)
        return io.BytesIO()

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def settimeout(self, *_a) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeServer:
    """What the handler reads off its server: the service, the throttle, and two flags."""

    def __init__(self, service: rs.ReviewService, *, https: bool = False) -> None:
        self.service = service
        self.https = https
        self.trust_forwarded = False
        self.throttle = rs._Throttle()


class _Reply:
    def __init__(self, raw: bytes) -> None:
        head, _, body = raw.partition(b"\r\n\r\n")
        first, _, rest = head.partition(b"\r\n")
        self.status = int(first.split()[1])
        self.headers = http.client.parse_headers(io.BytesIO(rest + b"\r\n\r\n"))
        self.body = body

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body or b"{}")


class ThroughTheServer(unittest.TestCase):
    """The handler itself: routing, headers, cookies, redirects and refusals."""

    def setUp(self) -> None:
        self.fx = Fixture(self, undo=8)
        self.hold = self.fx.hold_james()
        self.fx.hold_staff()
        self.server = _FakeServer(self.fx.service)
        self.issued = self.fx.tokens.issue(JAMES)
        self.session = self.fx.tokens.verify(self.issued.token, principal=JAMES)

    def request(self, method: str, path: str, *, headers: dict | None = None,
                fields: dict | None = None, client: str = "203.0.113.9") -> _Reply:
        head = dict(headers or {})
        body = b""
        if fields is not None:
            body = urllib.parse.urlencode(fields).encode()
            head.setdefault("Content-Type", "application/x-www-form-urlencoded")
            head["Content-Length"] = str(len(body))
        head.setdefault("Host", "review.example.invalid")
        head.setdefault("Connection", "close")
        lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in head.items()]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        wire = _Wire(raw)
        rs.ReviewHandler(wire, (client, 51234), self.server)
        return _Reply(bytes(wire.sent))

    def get(self, path: str, **kw) -> _Reply:
        return self.request("GET", path, **kw)

    def post(self, path: str, fields: dict, **kw) -> _Reply:
        return self.request("POST", path, fields=fields, **kw)

    def signed_in(self) -> dict:
        return {"Cookie": f"{rs.COOKIE_NAME}={self.issued.token}"}

    # -- the link ------------------------------------------------------------------

    def test_the_emailed_link_trades_the_token_for_a_cookie_and_a_clean_address(self) -> None:
        reply = self.get(f"/?k={urllib.parse.quote(self.issued.token)}")
        self.assertEqual(303, reply.status)
        self.assertEqual("/review", reply.headers["Location"])
        self.assertNotIn("?", reply.headers["Location"])
        self.assertNotIn(self.issued.token, reply.headers["Location"])
        cookie = reply.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(rs.COOKIE_NAME + "="))
        for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)

    def test_the_cookie_alone_opens_the_page_afterwards(self) -> None:
        reply = self.get("/review", headers=self.signed_in())
        self.assertEqual(200, reply.status)
        self.assertIn(HELD_WORDS, reply.text())

    def test_this_mornings_link_beats_yesterdays_cookie(self) -> None:
        """Issuing today's link revokes yesterday's, so the stale cookie must not win."""
        stale = self.issued
        fresh = self.fx.tokens.issue(JAMES)
        reply = self.get(
            f"/?k={urllib.parse.quote(fresh.token)}",
            headers={"Cookie": f"{rs.COOKIE_NAME}={stale.token}"},
        )
        self.assertEqual(303, reply.status)
        self.assertIn(fresh.token, reply.headers["Set-Cookie"])

    def test_the_page_comes_back_hardened(self) -> None:
        reply = self.get("/review", headers=self.signed_in())
        headers = {k.lower(): v for k, v in reply.headers.items()}
        self.assertEqual("no-referrer", headers["referrer-policy"])
        self.assertEqual("DENY", headers["x-frame-options"])
        self.assertEqual("nosniff", headers["x-content-type-options"])
        self.assertIn("no-store", headers["cache-control"])
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertNotIn("unsafe-inline", csp)

    def test_no_link_at_all_looks_exactly_like_a_wrong_one(self) -> None:
        nothing = self.get("/review")
        wrong = self.get("/?k=" + "a" * 40)
        self.assertEqual(401, nothing.status)
        self.assertEqual(401, wrong.status)
        strip = lambda body: re.sub(r'nonce="[^"]+"', 'nonce=""', body.decode())
        self.assertEqual(strip(nothing.body), strip(wrong.body))

    def test_a_revoked_link_stops_working_mid_morning(self) -> None:
        self.assertEqual(200, self.get("/review", headers=self.signed_in()).status)
        self.fx.tokens.revoke_for(JAMES, why="lost phone")
        self.assertEqual(401, self.get("/review", headers=self.signed_in()).status)

    def test_an_unknown_address_is_a_plain_page_with_nothing_on_it(self) -> None:
        reply = self.get("/admin", headers=self.signed_in())
        self.assertEqual(404, reply.status)
        self.assertNotIn(HELD_WORDS, reply.text())

    # -- answering -----------------------------------------------------------------

    def test_an_answer_is_recorded_after_its_window(self) -> None:
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "release", "csrf": self.session.csrf, "k": self.issued.token,
        }, headers={"Accept": "application/json"})
        payload = reply.json()
        self.assertTrue(payload["ok"])
        self.assertEqual("queued", payload["state"])
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.RELEASED, self.fx.store.get(self.hold).decision)

    def test_an_answer_without_the_form_token_records_nothing(self) -> None:
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "release", "csrf": "not-it", "k": self.issued.token,
        }, headers={"Accept": "application/json"})
        self.assertEqual(409, reply.status)
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_an_answer_with_no_link_at_all_records_nothing(self) -> None:
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "release", "csrf": self.session.csrf,
        }, headers={"Accept": "application/json"})
        self.assertEqual(401, reply.status)
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_an_answer_from_another_persons_link_records_nothing(self) -> None:
        theirs = self.fx.tokens.issue(STAFF)
        session = self.fx.tokens.verify(theirs.token)
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "release", "csrf": session.csrf, "k": theirs.token,
        }, headers={"Accept": "application/json"})
        self.assertFalse(reply.json()["ok"])
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_a_phone_with_no_javascript_is_sent_back_to_a_clean_page(self) -> None:
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "refuse", "csrf": self.session.csrf, "k": self.issued.token,
        })
        self.assertEqual(303, reply.status)
        location = reply.headers["Location"]
        self.assertTrue(location.startswith("/review"))
        self.assertNotIn("?", location)
        self.assertNotIn(self.issued.token, location)

    def test_the_undo_button_works_without_javascript_too(self) -> None:
        fields = {"hold": self.hold, "csrf": self.session.csrf, "k": self.issued.token}
        self.post("/review/answer", dict(fields, answer="refuse"))
        self.post("/review/undo", fields)
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_the_answered_item_shows_its_undo_while_the_window_is_open(self) -> None:
        self.post("/review/answer", {"hold": self.hold, "answer": "release",
                                     "csrf": self.session.csrf, "k": self.issued.token})
        html = self.get("/review", headers=self.signed_in()).text()
        self.assertIn("data-undo-until", html)
        self.assertIn("Undo", html)
        self.assertIn(rp.RELEASED_SAID, html)

    def test_an_oversized_body_is_not_read_into_memory(self) -> None:
        reply = self.post("/review/answer", {
            "hold": self.hold, "answer": "release", "csrf": self.session.csrf,
            "k": self.issued.token, "junk": "x" * (rs.MAX_BODY_BYTES + 10),
        }, headers={"Accept": "application/json"})
        self.assertEqual(401, reply.status)  # nothing parsed, so no session, so nothing done
        self.fx.service.commit_due(force=True)
        self.assertEqual(Decision.PENDING, self.fx.store.get(self.hold).decision)

    def test_guessing_is_slowed_down(self) -> None:
        for _ in range(rs.BAD_TOKEN_LIMIT):
            self.assertEqual(401, self.get("/?k=" + "b" * 40).status)
        self.assertEqual(429, self.get("/?k=" + "b" * 40).status)

    def test_one_address_guessing_does_not_lock_out_the_person_with_the_link(self) -> None:
        for _ in range(rs.BAD_TOKEN_LIMIT + 2):
            self.get("/?k=" + "b" * 40, client="198.51.100.7")
        self.assertEqual(200, self.get("/review", headers=self.signed_in()).status)

    # -- the rest ------------------------------------------------------------------

    def test_the_health_check_says_nothing_about_anybody(self) -> None:
        reply = self.get("/healthz")
        self.assertEqual(200, reply.status)
        self.assertEqual(b"ok\n", reply.body)

    def test_the_server_does_not_announce_its_python_version(self) -> None:
        reply = self.get("/healthz")
        self.assertEqual("kbc-review", reply.headers["Server"])
        self.assertNotIn("Python", str(reply.headers))

    def test_nothing_held_and_no_token_appears_in_any_log_line(self) -> None:
        lines: list[str] = []

        class Catcher(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                fields = " ".join(f"{k}={v}" for k, v in vars(record).items())
                lines.append(record.getMessage() + " " + fields)

        catcher = Catcher()
        logger = logging.getLogger("transcriber")
        previous = logger.level, logger.propagate
        logger.addHandler(catcher)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (logger.removeHandler(catcher), logger.setLevel(previous[0])))

        self.get(f"/?k={urllib.parse.quote(self.issued.token)}")
        self.get("/review", headers=self.signed_in())
        self.post("/review/answer", {"hold": self.hold, "answer": "release",
                                     "csrf": self.session.csrf, "k": self.issued.token})
        self.fx.service.commit_due(force=True)

        blob = "\n".join(lines)
        self.assertNotIn(HELD_WORDS, blob)
        self.assertNotIn(STAFF_WORDS, blob)
        self.assertNotIn(self.issued.token, blob)
        self.assertNotIn("?k=", blob)
        self.assertIn("route=", blob)


def _source_of(function) -> str:
    import inspect

    return inspect.getsource(function)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
