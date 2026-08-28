"""Can he see what it did? A name he never hears about is a name nobody checked.

``test_naming_never_loses_a_recording.py`` asks whether a title can cost a recording.
``test_naming_never_misfiles.py`` asks whether a title is right. This file asks the third
question, which is the one the shipped configuration depends on: **is the decision
visible?**

The feature ships reporting and not applying. ``NAMING`` is on, ``NAMING_APPLY`` is off, and
the morning email is therefore not a nicety — it *is* the measurement. Nobody has counted how
often the rule fires or how often it is right, and the only thing that will ever produce that
count is the section of the 06:00 email asserted below. A reporting-only feature whose report
does not arrive is a feature that will be armed on an estimate, which is precisely what the
two-boolean design exists to prevent.

Four claims, and each of them is a way the report can be there and still be useless:

* **It prints on a quiet morning.** A section that appears only when there is something to
  say makes silence mean "nothing came in" and "the site list stopped being written a
  fortnight ago" at the same time. Those need opposite responses and they must never look
  alike, so the site-list line is printed on every single day naming is on — with names,
  without names, with an empty book and with no book at all.

* **It survives the section it lives in.** The naming lines are inside ``WORTH A LOOK``,
  whose guard was written for three keys. Naming is a fourth. Miss it and the whole
  section — naming, withheld quotes, split recordings, degraded transcripts — never prints
  on a day whose only news is a name.

* **A burst does not hide it.** Eighty recordings in one morning is his real worst case, a
  week's backlog syncing after the phone has been offline. The review list five lines above
  prints five and then stops with no overflow line at all, so on that morning fifty-five
  became five and silence. Copying that half of the precedent would be copying the bug.

* **It is keyed on the decision, not the discovery.** A naming decision is written when a
  recording *publishes*. Every other cohort in the digest is selected by *discovery*. A
  recording found at 21:31 on Friday and published on Sunday morning is selected by a digest
  built before its decision existed and by no later one — so its name appears in no email,
  ever. That is exactly the deferred, backed-off, burst-day population where a name is most
  likely to be wrong.

And under all of it: what he reads is plain English. No slug, no rule code, no OneDrive item
id, no address. He does not look things up; a line he cannot read is a line he stops reading,
and then the measurement is gone again by a different route.

Everything runs against the **real 56-site record** — ``ops/build-site-book.py``'s projection
of ``kbc-site-memory/build/spine.json`` when that repository is checked out beside this one.

------------------------------------------------------------------------------------------
A note on the one seam this file stands in for.

``Ledger.naming_for_day`` raises on every call in the shipped code. The last section of this
file proves that, unpatched, and fails. Everything *between* that read and the sent email — the "he named this
one himself" filter, the site-list line, the ``WORTH A LOOK`` guard, the five-row cut, the
overflow count, the redaction backstop — is what this file is about, so where a test needs
rows the ledger cannot currently deliver, the rows that method *documents returning* are
handed in at that one seam and everything downstream of it runs shipped. Nothing here papers
over the defect: it is asserted on its own, in the open, at the foot of the file.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
import unittest
from typing import Any, Iterator, Mapping, Sequence
from unittest import mock

from tests import support
from transcriber import digest, sitebook
from transcriber.ledger import Ledger
from transcriber.models import DEFAULT_ROUTE, DriveItem, State, contains_email

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------- the record's own sites


#: Enough of the record's real titles to stand the tests up if this repository is ever
#: checked out without ``kbc-site-memory`` beside it. The real projection is preferred and
#: is what these tests are meant to run against; nothing below is asserted by count.
_FALLBACK_SITES = (
    ("canterbury-square", "Canterbury Square"),
    ("milton-court-sea-point", "Milton Court - Sea Point"),
    ("beach-court-bc", "Beach Court bc"),
    ("eagle-house", "Eagle House"),
    ("ashton-steelworks", "Ashton Steelworks"),
    ("22-chepstow-sea-point", "22 Chepstow, Sea Point"),
    ("village-square", "Village Square"),
    ("wolroy-house", "Wolroy House"),
)


def _the_records_own_projection() -> dict[str, Any]:
    """The eight fields ``ops/build-site-book.py`` projects, out of the record's own spine.

    Read-only. That repository is not ours and is never written to.
    """
    for candidate in (
        "/home/user/kbc-site-memory/build/spine.json",
        os.path.join(_REPO, "..", "kbc-site-memory", "build", "spine.json"),
    ):
        path = os.path.abspath(candidate)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                spine = json.load(handle)
        except (OSError, ValueError):     # the record is not ours to keep readable
            continue
        projected = {
            slug: {field: entry.get(field) for field in sitebook.VOCAB_FIELDS}
            for slug, entry in (spine.get("sites") or {}).items()
            if str((entry or {}).get("title") or "").strip()
        }
        if projected:
            return projected
    return {slug: {"title": title} for slug, title in _FALLBACK_SITES}


def _write_book(directory: str, name: str, sites: Mapping[str, Any] | None = None,
                **overrides: Any) -> str:
    payload: dict[str, Any] = {
        "vocab_contract": sitebook.CONTRACT,
        "generated_at": "2026-08-28",
        "sites": dict(_the_records_own_projection() if sites is None else sites),
    }
    payload.update(overrides)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return path


_BOOK_DIR = tempfile.mkdtemp(prefix="naming-visible-book-")

#: The site list the emails below are rendered against, and how many sites it holds. The
#: count is read off the book rather than written down, so this file says the same true
#: thing whether it is run against the real record or the vendored fallback.
SITE_BOOK = _write_book(_BOOK_DIR, "sites.json")
SITE_BOOK_SIZE = sitebook.load(SITE_BOOK).size

#: A book the nightly build wrote but that lists nothing, and a path where no book has ever
#: been written. Both mean "no recording will be named" and they are different faults.
EMPTY_BOOK = _write_book(_BOOK_DIR, "empty.json", sites={})
MISSING_BOOK = os.path.join(_BOOK_DIR, "never-written.json")


def tearDownModule() -> None:
    shutil.rmtree(_BOOK_DIR, ignore_errors=True)


# ------------------------------------------------------------------------ the fixtures

#: The day the digest is built for, and the two around it.
DAY_BEFORE = "2026-08-25"
DAY = "2026-08-26"
DAY_AFTER = "2026-08-27"

#: The voice recorder's own default name — the only shape this feature ever acts on.
RECORDER_DEFAULT = "Voice 260806_162219.m4a"

#: A real OneDrive driveItem id, in the shape Graph actually hands them over. It is the
#: kind of token he would have to look something up to read, so it must never be in a
#: sentence addressed to him.
ITEM_ID = "01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K"

#: The site the record files it under, the way the record writes it, and the way a person
#: says it. Only the last two may ever reach his screen.
SLUG = "canterbury-square"
TITLE = "Canterbury Square"
NAME = "CANTERBURY"


def _decision(**overrides: Any) -> dict[str, Any]:
    """One naming decision in the shape :meth:`Ledger.naming_for_day` documents returning.

    The stored decision (``autoname.NameDecision.as_meta``) plus the three fields the
    ledger adds when it reads the row back: which recording it was, what it was called when
    it arrived, and which route it came in on.
    """
    row: dict[str, Any] = {
        "decided": True,
        "name": NAME,
        "applied": False,
        "site": SLUG,
        "span": "Canterbury",
        "mentions": 4,
        "first_pct": 3,
        "spread_pct": 88,
        "code": "ok",
        "why": (f"Canterbury is named 4 times, spread right across the recording, and the "
                f"record files this one under {TITLE} either way"),
        "book": "2026-08-28",
        "item_id": ITEM_ID,
        "source_name": RECORDER_DEFAULT,
        "route": DEFAULT_ROUTE,
    }
    row.update(overrides)
    return row


def _refusal(code: str, why: str, **overrides: Any) -> dict[str, Any]:
    """A decision that reached "no name" — the ordinary outcome, and still reported."""
    fields: dict[str, Any] = {"name": "", "site": "", "code": code, "why": why}
    fields.update(overrides)
    return _decision(**fields)


@contextlib.contextmanager
def _ledger_answering(by_day: Mapping[str, Sequence[Mapping[str, Any]]]) -> Iterator[None]:
    """Hand the digest the rows ``Ledger.naming_for_day`` documents returning.

    The one seam described in the module docstring. Everything above it — the E1 filter in
    ``naming_report``, the site-list line, the ``WORTH A LOOK`` guard, the five-row cut, the
    redaction — is shipped code called in the shipped order. The failing section at the foot
    of this file asserts the real method with nothing patched at all.
    """
    def answer(self: Ledger, day: str, route: str | None = None) -> list[dict[str, Any]]:
        return [dict(row) for row in by_day.get(day, ())]

    with mock.patch.object(Ledger, "naming_for_day", answer):
        yield


def naming_block(body: str) -> str:
    """The naming part of the email, as he sees it: from the site-list line to the next rule.

    Sliced rather than re-rendered, because what is under test is what actually lands in the
    body — a section that renders beautifully and is never reached is the failure this whole
    file is about.
    """
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if "site list:" in line]
    if not starts:
        return ""
    start = starts[0]
    for index in range(start, len(lines)):
        if set(lines[index].strip()) == {"-"}:
            return "\n".join(lines[start:index - 1])
    return "\n".join(lines[start:])


class _Morning(unittest.TestCase):
    """One deployment, one ledger, and the 06:00 email it would send."""

    APPLY = False
    BOOK = SITE_BOOK

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="naming-visible-")
        self.addCleanup(self.dir.cleanup)
        self.ledger_path = os.path.join(self.dir.name, "ledger.sqlite3")
        self.ledger = Ledger(self.ledger_path)
        self.addCleanup(self.ledger.close)
        self.config = self.deployment()

    def deployment(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "ledger_path": self.ledger_path,
            "work_dir": os.path.join(self.dir.name, "work"),
            "naming": True,
            "naming_apply": self.APPLY,
            "naming_sites_file": self.BOOK,
        }
        values.update(overrides)
        return support.make_config(**values)

    # -- the email ----------------------------------------------------------------

    def body(self, decisions: Sequence[Mapping[str, Any]], *, day: str = DAY,
             config: Any = None) -> str:
        """The rendered email for ``day``, with those naming decisions on that day."""
        with _ledger_answering({day: decisions}):
            return digest.build(config or self.config, self.ledger, day=day).body

    def shipped_body(self, *, day: str = DAY, config: Any = None) -> str:
        """The rendered email with nothing patched at all — the shipped read included."""
        return digest.build(config or self.config, self.ledger, day=day).body

    def section(self, decisions: Sequence[Mapping[str, Any]], **kwargs: Any) -> str:
        return naming_block(self.body(decisions, **kwargs))

    # -- rows on the ledger -------------------------------------------------------

    def arrive(self, item_id: str, name: str, *, discovered_at: str) -> None:
        """A recording on the ledger, discovered on a day of our choosing.

        ``discovered_at`` is deliberately not writable through the ledger's own API — it is
        identity, not a field — so a test that needs a row from Friday writes the column
        itself rather than pretending the guard is not there.
        """
        self.ledger.upsert_discovered(DriveItem(item_id=item_id, name=name, size=4096))
        conn = self.ledger._conn()
        conn.execute("UPDATE items SET discovered_at=? WHERE item_id=?",
                     (discovered_at, item_id))
        conn.commit()

    def publish(self, item_id: str, *, done_at: str, decision: Mapping[str, Any] | None) -> None:
        """Finish a recording, with or without a naming decision written on its row."""
        meta: dict[str, Any] = {}
        if decision is not None:
            meta["naming"] = {k: v for k, v in decision.items()
                              if k not in ("item_id", "source_name", "route")}
        self.ledger.advance(item_id, State.DONE, done_at=done_at, meta=meta)


# ================================================== 1. the site list line, every morning


class TheSiteListLinePrintsEveryMorning(_Morning):
    """One line about the site list, in every digest, on every kind of day.

    This is the line that separates the two states that look identical from the outside: a
    fortnight in which nothing arrived under the recorder's own name, and a fortnight in
    which the record's nightly build stopped writing the file and nothing *could* be named.
    Both produce an email with no names in it. Only this line tells them apart.
    """

    def test_it_prints_on_a_day_when_a_recording_was_named(self) -> None:
        block = self.section([_decision()])
        self.assertIn(f"site list: {SITE_BOOK_SIZE} sites", block)
        # And the recording it named is there too, or the line is describing nothing.
        self.assertIn(RECORDER_DEFAULT, block)
        self.assertIn(NAME, block)

    def test_it_prints_on_a_day_when_nothing_came_in_under_the_recorders_name(self) -> None:
        # Most mornings. Without this line, this email and the one below are the same email.
        block = self.section([])
        self.assertIn(f"site list: {SITE_BOOK_SIZE} sites", block)
        self.assertIn("Nothing came in under the voice recorder's own name.", block)

    def test_it_says_so_when_the_book_lists_no_sites(self) -> None:
        # The nightly build ran and produced a file with nothing in it. Nothing will be
        # named tonight, and this is the only place that fact is ever said.
        block = self.section([], config=self.deployment(naming_sites_file=EMPTY_BOOK))
        self.assertIn("site list: not loaded", block)
        self.assertIn("lists no sites", block)
        self.assertIn("nothing is being named", block)

    def test_it_says_so_when_the_book_was_never_written(self) -> None:
        block = self.section([], config=self.deployment(naming_sites_file=MISSING_BOOK))
        self.assertIn("site list: not loaded", block)
        self.assertIn("is not there yet", block)
        self.assertIn("nothing is being named", block)

    def test_it_says_so_when_no_book_is_configured_at_all(self) -> None:
        block = self.section([], config=self.deployment(naming_sites_file=""))
        self.assertIn("site list: empty, so nothing is being named", block)

    def test_the_four_kinds_of_morning_do_not_read_alike(self) -> None:
        """A working book, an empty one, a missing one and none at all: four sentences.

        If any two of these rendered the same line, the morning the record's nightly build
        died would be indistinguishable from a quiet week — which is the exact failure this
        line exists to remove, arriving through the line itself.
        """
        lines = {
            "a book with sites in it": self.section([], config=self.config),
            "a book listing nothing": self.section(
                [], config=self.deployment(naming_sites_file=EMPTY_BOOK)),
            "a book never written": self.section(
                [], config=self.deployment(naming_sites_file=MISSING_BOOK)),
            "no book configured": self.section(
                [], config=self.deployment(naming_sites_file="")),
        }
        first = {
            what: next(line.strip() for line in block.splitlines() if "site list:" in line)
            for what, block in lines.items()
        }
        self.assertEqual(len(set(first.values())), 4, first)

    def test_the_line_is_there_even_when_the_book_names_no_recording_that_day(self) -> None:
        # A book that loaded fine and a day on which the rule refused every recording. The
        # site list is still reported, because "the rule refused" and "there was no list to
        # refuse against" are different mornings.
        block = self.section([_refusal("N5", "the record cannot tell which site this "
                                             "belongs to, so a name would say more than "
                                             "the recording does")])
        self.assertIn(f"site list: {SITE_BOOK_SIZE} sites", block)
        self.assertIn("left as it is", block)


# ================================================= 2. the section it lives in must print


class TheSectionPrintsOnANamingOnlyDay(_Morning):
    """``WORTH A LOOK`` is guarded on three keys, and naming is a fourth.

    Miss the fourth and nothing in the section prints on a day whose only news is a name —
    and because the other three are about withheld quotes, split recordings and degraded
    transcripts, a day with a name and nothing else is the *ordinary* day. The measurement
    would then exist, be correct, be stored, and never be printed.
    """

    def test_the_day_really_has_nothing_else_in_it(self) -> None:
        # Guards the two tests below: if the ledger were quietly supplying a review row or a
        # degraded transcript, they would pass while the fourth key was missing.
        facts = self.ledger.attention_for_day(DAY)
        self.assertEqual(facts["review"], 0)
        self.assertEqual(facts["unverified_duration_guard"], 0)
        self.assertEqual(facts["degraded_transcripts"], 0)

    def test_worth_a_look_prints_when_a_name_is_the_only_news(self) -> None:
        body = self.body([_decision()])
        self.assertIn("WORTH A LOOK", body)
        self.assertIn(RECORDER_DEFAULT, body)

    def test_worth_a_look_prints_even_when_no_recording_was_named(self) -> None:
        # The quiet morning still has to carry the site-list line, and the section is the
        # only place it is printed from.
        body = self.body([])
        self.assertIn("WORTH A LOOK", body)
        self.assertIn("site list:", body)

    def test_the_naming_lines_are_inside_the_section_and_before_the_ledger(self) -> None:
        body = self.body([_decision()])
        heading = body.index("WORTH A LOOK")
        site_list = body.index("site list:")
        ledger_section = body.index("THE LEDGER")
        self.assertLess(heading, site_list)
        self.assertLess(site_list, ledger_section)

    def test_switching_naming_off_takes_the_whole_section_away(self) -> None:
        # The other half of the same claim: the section prints on this day *because* of
        # naming and nothing else, so the guard's fourth key is doing the work.
        body = self.body([_decision()], config=self.deployment(naming=False))
        self.assertNotIn("WORTH A LOOK", body)
        self.assertNotIn("site list:", body)

    def test_a_review_row_and_a_name_both_print_in_the_one_section(self) -> None:
        # The section is shared. A name must not displace the withheld quotes above it, and
        # the quotes must not swallow the name.
        self.arrive("R1", "BEACH COURT SITE WALK 270826.m4a", discovered_at=f"{DAY}T08:00:00Z")
        self.ledger.set_fields("R1", meta={"analysis": {"review": 2, "review_items": []}})
        body = self.body([_decision()])
        self.assertIn("proposed item(s) were withheld", body)
        self.assertIn(f"site list: {SITE_BOOK_SIZE} sites", body)
        self.assertIn(RECORDER_DEFAULT, naming_block(body))


# ============================================================ 3. a burst does not hide it


class ABurstDoesNotHideIt(_Morning):
    """Fifty-five decisions on one morning. Five rows, and the number it did not print.

    His real worst case is a week's backlog syncing at once after the phone has been
    offline. The review list five lines above this section prints five rows and then stops —
    no overflow line at all — so on that morning fifty-five became five and silence. Half of
    that precedent is right (the email stays readable) and half of it is the bug (the
    reader cannot tell five from fifty-five). This asserts both halves.
    """

    COUNT = 55
    SHOWN = 5

    def burst(self) -> list[dict[str, Any]]:
        return [
            _decision(
                item_id=f"{ITEM_ID}{index:02d}",
                source_name=f"Voice 260806_09{index:02d}30.m4a",
            )
            for index in range(self.COUNT)
        ]

    def test_exactly_five_recordings_are_listed(self) -> None:
        block = self.section(self.burst())
        self.assertEqual(block.count("  ->  "), self.SHOWN,
                         "the naming section listed a number of recordings other than five; "
                         "a burst morning must not push the failures off the top of the email")

    def test_the_number_it_did_not_print_is_printed(self) -> None:
        block = self.section(self.burst())
        # The literal count, not "and more" and not nothing at all. Without it, fifty-five
        # decisions and five decisions produce the same email, and the one morning the rule
        # ran fifty-five times is the morning it is most worth checking.
        self.assertIn(f"...and {self.COUNT - self.SHOWN} more", block)

    def test_the_count_of_them_is_stated_before_the_rows(self) -> None:
        block = self.section(self.burst())
        self.assertIn(f"{self.COUNT} recordings came in with the voice recorder's own name",
                      block)

    def test_the_section_stays_a_readable_size(self) -> None:
        block = self.section(self.burst())
        self.assertLess(len(block.splitlines()), 40,
                        "fifty-five recordings turned the naming section into a page; the "
                        "failures above it are what the email is for")

    def test_it_does_not_copy_the_silent_truncation_five_lines_above(self) -> None:
        """The burst morning in full: withheld quotes AND fifty-five naming decisions.

        The review list is left exactly as it is — this asserts nothing about it — but the
        naming section immediately below it has to say how many it left out, or the two
        lists in one section disagree about what a reader may assume from a short list.
        """
        for index in range(8):
            item = f"Q{index}"
            self.arrive(item, f"Voice 260806_10{index:02d}00.m4a",
                        discovered_at=f"{DAY}T10:00:00Z")
            self.ledger.set_fields(item, meta={"analysis": {"review": 1, "review_items": []}})

        body = self.body(self.burst())
        block = naming_block(body)
        self.assertIn("8 proposed item(s) were withheld", body)
        self.assertEqual(block.count("  ->  "), self.SHOWN)
        self.assertIn(f"...and {self.COUNT - self.SHOWN} more", block)

    def test_a_burst_of_refusals_is_reported_the_same_way(self) -> None:
        # The load is not only names. Fifty-five recordings the rule refused are fifty-five
        # rows, and the same cut has to hold or a bad night is the one that floods him.
        rows = [
            _refusal("N7", f"Canterbury is only mentioned once, so it looks like something "
                           f"that came up rather than where he was",
                     item_id=f"{ITEM_ID}{index:02d}",
                     source_name=f"Voice 260806_11{index:02d}30.m4a")
            for index in range(self.COUNT)
        ]
        block = self.section(rows)
        self.assertEqual(block.count("  ->  "), self.SHOWN)
        self.assertIn(f"...and {self.COUNT - self.SHOWN} more", block)
        self.assertIn("left as it is", block)


# ================================================ 4. plain English, and nothing to look up


class HeReadsPlainEnglishAndNothingElse(_Morning):
    """No slug, no rule code, no item id, no address — asserted on the rendered body.

    Not politeness. The morning email is the one thing he reads every day, and the moment a
    line in it needs a lookup he stops reading the section — at which point the measurement
    this feature ships to produce is gone, and gone in a way that looks exactly like a
    feature that is working.
    """

    #: The rule codes the decision carries. Every one of them is a bare token he would have
    #: to look up in a module docstring.
    CODES = ("N0", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9",
             "E1", "E2", "E3", "E4")

    def every_kind_of_decision(self) -> list[dict[str, Any]]:
        """One row per outcome the rule can reach, each carrying its own real wording."""
        return [
            _decision(),
            _refusal("N1", "nothing in it says which site it is about",
                     source_name="Voice 260806_090000.m4a"),
            _refusal("N2", "it only says 'the site', which does not name anywhere",
                     source_name="Voice 260806_091000.m4a"),
            _refusal("N6", "'Beach' could be more than one site", site="beach-court-bc",
                     span="Beach", source_name="Voice 260806_092000.m4a"),
            _refusal("E4", "too short to name from (44s)",
                     source_name="Voice 260806_093000.m4a"),
        ]

    def test_the_records_slug_for_a_site_never_appears(self) -> None:
        block = self.section(self.every_kind_of_decision())
        # ``canterbury-square`` and ``beach-court-bc`` are how the record files things. In
        # his morning email they are the kind of thing that makes a person stop reading it.
        for slug in (SLUG, "beach-court-bc"):
            self.assertNotIn(slug, block)
        # And the sites are still named — in the words a person would say.
        self.assertIn(TITLE, block)

    def test_no_rule_code_appears_as_a_bare_token(self) -> None:
        block = self.section(self.every_kind_of_decision())
        found = sorted({
            code for code in self.CODES
            if re.search(rf"(?<![A-Za-z0-9]){code}(?![A-Za-z0-9])", block)
        })
        self.assertEqual(found, [],
                         f"a rule code reached the morning email: {found}. He would have to "
                         f"open a source file to find out what it meant.")

    def test_no_onedrive_item_id_appears(self) -> None:
        block = self.section(self.every_kind_of_decision())
        self.assertNotIn(ITEM_ID, block)
        # And the recording is still identified — by the name it arrived under.
        self.assertIn(RECORDER_DEFAULT, block)

    def test_an_address_that_reached_a_decision_does_not_ride_out_in_the_email(self) -> None:
        """The backstop, on the one path that can carry an address into this section.

        The rule quotes the model's own answer back when it cannot find those words in the
        transcript. The model reads the transcript, and a site walk in which somebody's
        address is dictated can put one in that answer — at which point the sentence
        explaining why nothing was named carries an address into an email that never emits
        one.
        """
        rows = [_refusal(
            "N3",
            "it says 'nic@renoroofs.net', but not in those words, so there is nothing in "
            "the recording to take the name from",
        )]
        body = self.body(rows)
        self.assertFalse(contains_email(body),
                         "an address reached the morning email through the naming section")
        self.assertNotIn("renoroofs", body)

    def test_the_email_says_what_it_would_have_called_it_when_nothing_was_renamed(self) -> None:
        # ``NAMING_APPLY`` is off in the shipped configuration, and an email that said
        # "named it CANTERBURY" while nothing in OneDrive or the record had changed would be
        # a false statement in the one report he trusts.
        block = self.section([_decision()])
        self.assertIn("would call it", block)
        self.assertIn("Nothing has been renamed", block)

    def test_the_email_says_it_named_it_when_it_did(self) -> None:
        applying = self.deployment(naming_apply=True)
        block = naming_block(self.body([_decision(applied=True)], config=applying))
        self.assertIn("named it", block)
        self.assertNotIn("Nothing has been renamed", block)


class TheOnesHeNamedHimselfAreNotNews(_Morning):
    """"He named this one himself" is most recordings, and is not worth a line.

    Every recording he titles by hand reaches a decision and that decision is stored. If all
    of them were reported, the section would be a daily list of non-events, he would stop
    reading it within a week, and the five that matter would be inside something nobody
    opens — which is the flood failure arriving through the report rather than through the
    queue.
    """

    def test_a_name_he_typed_is_never_mentioned_in_the_email(self) -> None:
        his_own = _refusal("E1", "he named this one himself",
                           source_name="BEACH COURT SITE WALK 270826.m4a")
        block = self.section([his_own, _decision()])
        self.assertNotIn("BEACH COURT SITE WALK", block)
        self.assertNotIn("he named this one himself", block)

    def test_a_day_of_nothing_but_his_own_names_reads_as_a_quiet_day(self) -> None:
        rows = [
            _refusal("E1", "he named this one himself", source_name=f"CJ {index}.m4a")
            for index in range(40)
        ]
        block = self.section(rows)
        self.assertIn("Nothing came in under the voice recorder's own name.", block)
        self.assertNotIn("CJ ", block)
        # Still short: forty non-events must not become forty lines.
        self.assertLess(len(block.splitlines()), 6)

    def test_a_filename_that_names_a_party_is_not_news_either(self) -> None:
        # E2 is the parser saying the filename names a person. Same class of non-event.
        rows = [_refusal("E2", "the filename names a party, so it is not an unnamed note",
                         source_name="Call Carel_260824_091500.m4a")]
        block = self.section(rows)
        self.assertNotIn("Call Carel", block)
        self.assertIn("Nothing came in under the voice recorder's own name.", block)

    def test_a_recording_that_was_too_short_to_judge_IS_news(self) -> None:
        # The line between the two: he did not name this one, the service could have, and it
        # decided not to. That is the measurement, and it must not be filtered out with the
        # non-events.
        block = self.section([_refusal("E4", "too short to name from (44s)")])
        self.assertIn(RECORDER_DEFAULT, block)
        self.assertIn("too short to name from", block)


# ============================================================== 5. the email still sends


class TheEmailGoesOutWhateverNamingDoes(_Morning):
    """Nothing in the naming report may cost the morning email.

    The digest is the whole alarm: if it stops arriving, the service has stopped, and that
    is the only signal there is. A report about titles that could suppress it would be a
    nicety that broke the thing it was decorating.
    """

    def send(self, *, day: str = DAY, config: Any = None) -> Any:
        sent: list[Any] = []

        class _Server:
            def __enter__(self_inner) -> Any:
                return self_inner

            def __exit__(self_inner, *exc: Any) -> None:
                return None

            def send_message(self_inner, message: Any) -> None:
                sent.append(message)

            def starttls(self_inner, *a: Any, **k: Any) -> None:
                return None

            def login(self_inner, *a: Any, **k: Any) -> None:
                return None

            def ehlo(self_inner, *a: Any, **k: Any) -> None:
                return None

        result = digest.run(config or self.config, self.ledger, day=day,
                            smtp_factory=lambda *a, **k: _Server())
        return result, sent

    def test_it_sends_when_the_site_book_cannot_be_read_at_all(self) -> None:
        # ``sitebook.load`` says it never raises. This asserts the email survives it being
        # wrong about that, because a book on a disk that has gone read-only, or a file
        # being rewritten by the nightly build at 06:00, is not an imagined failure.
        with mock.patch.object(sitebook, "load", side_effect=RuntimeError("boom")):
            result, sent = self.send()
        self.assertTrue(result.sent.ok, result.sent.detail)
        self.assertEqual(len(sent), 1)
        self.assertIn("site list:", result.digest.body)

    def test_it_sends_when_the_site_book_path_is_a_directory(self) -> None:
        # The ordinary shape of a mis-set NAMING_SITES_FILE.
        result, sent = self.send(config=self.deployment(naming_sites_file=_BOOK_DIR))
        self.assertTrue(result.sent.ok, result.sent.detail)
        self.assertEqual(len(sent), 1)
        self.assertIn("nothing is being named", naming_block(result.digest.body))

    def test_it_sends_when_the_ledger_cannot_answer_about_naming(self) -> None:
        def explode(self_inner: Ledger, day: str, route: str | None = None) -> Any:
            raise RuntimeError("the ledger fell over reading the naming decisions")

        with mock.patch.object(Ledger, "naming_for_day", explode):
            result, sent = self.send()
        self.assertTrue(result.sent.ok, result.sent.detail)
        self.assertEqual(len(sent), 1)
        # The rest of the email is intact: the counts, the routes and the ledger section.
        self.assertIn("THE LEDGER", result.digest.body)

    def test_it_sends_when_the_book_is_a_json_array_instead_of_a_site_list(self) -> None:
        path = os.path.join(self.dir.name, "wrong-shape.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(["canterbury-square"], handle)
        result, sent = self.send(config=self.deployment(naming_sites_file=path))
        self.assertTrue(result.sent.ok, result.sent.detail)
        self.assertIn("is not a site list", naming_block(result.digest.body))

    def test_it_sends_when_the_book_was_written_for_another_version(self) -> None:
        path = _write_book(self.dir.name, "old.json", vocab_contract=sitebook.CONTRACT + 99)
        result, sent = self.send(config=self.deployment(naming_sites_file=path))
        self.assertTrue(result.sent.ok, result.sent.detail)
        block = naming_block(result.digest.body)
        self.assertIn("written for a different version", block)
        self.assertIn("nothing is being named", block)


# ############################################################################
# ###  FAILING ON PURPOSE — DEFECTS IN THE SHIPPED CODE, NOT IN THESE TESTS ###
# ############################################################################
#
# Everything below asserts the behaviour this feature's own docstrings promise, against the
# shipped code with nothing patched. Each class says which line is wrong and what it costs.
# Do not relax an assertion to make one of these green.


class TheLedgerCannotReadBackASingleDecision(_Morning):
    """**These three tests fail. ``src/transcriber/ledger.py`` is what is wrong.**

    ``Ledger.naming_for_day`` opens its query with ``with self._connect() as conn:``. There
    is no ``_connect`` on :class:`~transcriber.ledger.Ledger` — every other reader in that
    file uses ``self._conn()`` — so the method raises ``AttributeError`` on every call, for
    every day, always.

    It is silent. Its only caller, :func:`transcriber.digest.naming_report`, catches
    ``Exception``, logs a warning nobody reads, and carries on with an empty list.

    What it costs: the feature ships reporting and not applying, deliberately, because
    nobody has measured how often the rule fires or how often it is right. The morning email
    IS that measurement. With this line as it stands the shipped configuration produces no
    measurement at all, indefinitely, while printing "Nothing came in under the voice
    recorder's own name" on the morning fifty-five recordings did — and the decision to arm
    ``NAMING_APPLY`` would then be taken on exactly the estimate the two-boolean design
    exists to avoid.

    Nothing is lost, delayed or misnamed by it. The decisions are on the rows. The fix is
    one word.
    """

    def setUp(self) -> None:
        super().setUp()
        self.arrive("V1", RECORDER_DEFAULT, discovered_at=f"{DAY}T14:31:00Z")
        self.publish("V1", done_at=f"{DAY}T15:02:00Z", decision=_decision())

    def test_the_ledger_reads_back_the_decision_it_stored(self) -> None:
        decisions = self.ledger.naming_for_day(DAY)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].get("name"), NAME)
        self.assertEqual(decisions[0].get("source_name"), RECORDER_DEFAULT)

    def test_a_recording_with_no_decision_on_it_is_not_reported(self) -> None:
        # A recording that finished before this feature existed, or one whose row was
        # written by an older version. It is not a naming decision and must not become one.
        self.arrive("V2", "BEACH COURT SITE WALK 270826.m4a",
                    discovered_at=f"{DAY}T08:00:00Z")
        self.publish("V2", done_at=f"{DAY}T09:00:00Z", decision=None)
        self.assertEqual([d["item_id"] for d in self.ledger.naming_for_day(DAY)], ["V1"])

    def test_a_row_whose_meta_is_damaged_does_not_stop_the_report(self) -> None:
        # One unreadable row must not cost the other fifty-four their line in the email.
        self.arrive("V3", "Voice 260806_170000.m4a", discovered_at=f"{DAY}T17:00:00Z")
        self.publish("V3", done_at=f"{DAY}T17:30:00Z", decision=_decision())
        conn = self.ledger._conn()
        conn.execute("UPDATE items SET meta=? WHERE item_id=?", ("{not json at all", "V3"))
        conn.commit()

        decisions = self.ledger.naming_for_day(DAY)     # must not raise
        self.assertEqual([d["item_id"] for d in decisions], ["V1"])


class ARecordingPublishedLaterIsReportedOnceAndOnce(_Morning):
    """**This test fails, and it fails for the reason above: the ledger read raises.**

    The claim is the one :meth:`Ledger.naming_for_day` was written for, and it is the reason
    that method keys on ``done_at`` rather than ``discovered_at``. A recording discovered on
    Friday evening and published on Sunday morning — deferred, backed off, or one of eighty
    that landed at 17:00 — must appear in exactly one morning email: Sunday's. Zero is a
    failure, because that is the population whose names are most likely to be wrong and
    least likely to be looked at. Two is a failure, because a name reported twice is a name
    he checks twice and then stops checking.
    """

    def setUp(self) -> None:
        super().setUp()
        self.arrive("V1", RECORDER_DEFAULT, discovered_at=f"{DAY_BEFORE}T21:31:00Z")
        self.publish("V1", done_at=f"{DAY_AFTER}T09:12:00Z", decision=_decision())

    def test_it_appears_in_exactly_one_morning_email(self) -> None:
        days = (DAY_BEFORE, DAY, DAY_AFTER)
        carried = [day for day in days
                   if RECORDER_DEFAULT in naming_block(self.shipped_body(day=day))]
        self.assertEqual(
            carried, [DAY_AFTER],
            "a recording found on Friday night and published on Sunday morning was reported "
            f"on {carried or 'no day at all'}. It must be reported on the day it was named "
            "and on no other: none at all is the deferred and burst-day population going "
            "unmeasured, and twice is a name he learns to skim past.",
        )

    def test_the_day_it_arrived_says_nothing_about_a_name(self) -> None:
        # This half holds today, and holds for the wrong reason — every day says nothing.
        # It is asserted anyway so that a fix which keys on discovery instead of publication
        # cannot pass the class above by reporting it on the wrong morning.
        block = naming_block(self.shipped_body(day=DAY_BEFORE))
        self.assertNotIn(RECORDER_DEFAULT, block)
        self.assertIn("Nothing came in under the voice recorder's own name.", block)


class AFailedReadIsReportedAsGoodNews(_Morning):
    """**This test fails. ``src/transcriber/digest.py``, ``naming_report``, is wrong.**

    ``naming_report`` catches every exception from the ledger read and carries on with an
    empty list, which ``_naming_lines`` then renders as **"Nothing came in under the voice
    recorder's own name."** That sentence is a statement of fact about the day, and on a
    morning when the read failed it is false.

    Every neighbouring report in this module already knows better. ``QueueReport`` carries an
    ``unavailable`` field and prints "The queue could not be counted this morning". So does
    ``HeldReport``. The naming report has no such field, so a failure and a quiet day render
    the same three lines.

    This is not a second bug so much as the mechanism by which the first one is invisible:
    with the ``_connect`` fault above, this sentence is printed every single morning, and it
    reads as the feature running and finding nothing.

    The fix is the one the other two reports already carry — an ``unavailable`` field set in
    the ``except`` branch, and a sentence saying the decisions could not be read.
    """

    def test_the_email_does_not_claim_nothing_came_in_when_it_could_not_look(self) -> None:
        def explode(self_inner: Ledger, day: str, route: str | None = None) -> Any:
            raise RuntimeError("the ledger fell over reading the naming decisions")

        with mock.patch.object(Ledger, "naming_for_day", explode):
            block = naming_block(digest.build(self.config, self.ledger, day=DAY).body)

        self.assertNotIn(
            "Nothing came in under the voice recorder's own name.", block,
            "the naming decisions could not be read and the morning email said nothing came "
            "in. A fault and a quiet day must never render the same sentence — that is the "
            "whole reason the site-list line above it exists.",
        )
        self.assertTrue(block.strip(), "the section vanished instead of saying it was broken")


class ARecordingWithNoNameOfItsOwnIsNotAnItemId(_Morning):
    """**This test fails. ``src/transcriber/digest.py``, ``_naming_lines``, is wrong.**

    ``source = str(row.get("source_name") or row.get("item_id") or "a recording")``. When a
    row reaches the report with no source name, the second choice is the raw OneDrive
    driveItem id — ``01BYE5RZ6QN3ZWBTUFOFD3GSPGOHDJD36K`` — printed in the middle of an
    English sentence in his morning email. The third choice, "a recording", is the one that
    reads.

    Latent rather than live: the ledger's ``name`` column is ``NOT NULL`` and Graph fills it,
    so a blank name needs an item Graph handed over without one. It is listed here because
    the cost is not proportional to the odds — the fallback exists precisely for the case
    nobody planned, and what it does in that case is print the one kind of token this email
    is supposed never to contain. Swapping the two fallbacks is the whole fix.
    """

    def test_a_decision_with_no_recording_name_reads_as_english(self) -> None:
        block = self.section([_decision(source_name="")])
        self.assertNotIn(ITEM_ID, block,
                         "a OneDrive item id was printed in his morning email as the name "
                         "of a recording")
        self.assertIn("a recording", block)


class TheVerbComesFromTodaysSwitchNotFromWhatHappened(_Morning):
    """**This test fails. ``src/transcriber/digest.py``, ``_naming_lines``, is wrong.**

    ``verb = "named" if facts.get("applying") else "would call"``. ``applying`` is
    ``config.naming_apply`` *as it stands when the email is rendered* — a fact about this
    morning. Every row it is applied to carries its own ``applied`` flag, which is the fact
    about what actually happened to that recording, and ``_naming_lines`` never reads it.

    The two disagree on exactly one morning, and it is the morning that matters. He switches
    ``NAMING_APPLY`` on in the evening — the whole point of shipping the feature dark is that
    one day he will — and the 06:00 email reports *yesterday's* decisions, every one of them
    taken while it was off, with "named it CANTERBURY". Nothing was renamed. Nothing in
    OneDrive changed and no document in the record carries that title. The one morning he is
    certain to read this section closely is the one morning it describes something that did
    not happen, and it goes the other way too: switching it back off reports recordings that
    really were retitled as ones it "would call" something.

    The fix is to read the flag that is already on every row.
    """

    def test_a_decision_taken_before_the_switch_is_not_reported_as_a_rename(self) -> None:
        armed_last_night = self.deployment(naming_apply=True)
        yesterday = _decision(applied=False)     # decided while nothing was being renamed
        block = naming_block(self.body([yesterday], config=armed_last_night))
        self.assertNotIn(
            f"named it {NAME}", block,
            "the morning email said a recording had been named, on a day when nothing was "
            "renamed. He would go looking in the record for a document that is not there.",
        )
        self.assertIn("would call it", block)

    def test_a_decision_that_really_did_rename_is_not_reported_as_a_suggestion(self) -> None:
        # The same fault the other way round, on the morning he switches it back off: three
        # files really were written under a worked-out title, and the email calls them
        # suggestions.
        disarmed_last_night = self.deployment(naming_apply=False)
        block = naming_block(
            self.body([_decision(applied=True)], config=disarmed_last_night)
        )
        self.assertIn(f"named it {NAME}", block)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
