"""A worked-out title must never send a site note to the wrong site.

Everything here runs against the **real record** — the 56 sites in
``kbc-site-memory/build/spine.json``, projected by ``ops/build-site-book.py``, the same
artifact the running service reads. That is deliberate. A vocabulary invented for a test
agrees with whatever rule you wrote next to it; the real one contains ``square`` in two
site titles, ``beach`` in exactly one, ``house`` in five, and a dozen jobs whose names
overlap each other, and it is the only thing that can say whether these rules hold.

Three failures are being tested for, in the order they matter:

1. **Nothing here may cost a recording.** Every failure in the naming path ends in "no
   name" and a published transcript — a renderer that throws, a site list that is not
   there, a model answer that is punctuation. :class:`NothingHereMayCostARecording`.
2. **A confidently wrong name.** The rest of the file. The title is part of the bytes the
   record scores to decide which site a note belongs to, so a wrong one does not merely
   mislabel a note — it can *unfile* one that was filed correctly.
   :class:`TheHazardIsReal` measures that on the real record and
   :class:`TheLastRuleWouldCatchIt` shows the rule that exists for it works, and shows why
   it never fires today.
3. Flooding him is the morning email's problem, not this file's.

**One test in here fails, on purpose.**
:meth:`OnThePhoneAboutAnotherSite.test_a_call_at_both_ends_of_a_walk_does_not_retitle_the_walk`
asserts what has to be true and is not true today: a site walk bookended by two short
calls about a different job is confidently titled with the *other* job's name. It is left
red rather than softened, because a test bent to fit the code would hide exactly the
failure this whole feature was built to avoid.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import tempfile
import unittest
from datetime import datetime

from transcriber import autoname, naming, outputs, sitebook
from transcriber.models import Segment, Transcript

from tests.support import StubExtraction, audio_info

# --------------------------------------------------------------------------- the record

#: Where the record is checked out. Overridable so this suite can be pointed at a copy,
#: never at a fixture: the whole value of these tests is that the vocabulary is his.
RECORD = os.environ.get("KBC_SITE_MEMORY", "/home/user/kbc-site-memory")

_TMP: tempfile.TemporaryDirectory | None = None
BOOK: sitebook.SiteBook = sitebook.EMPTY


def _load_ops_builder() -> object | None:
    """``ops/build-site-book.py`` as a module. Hyphenated, so it needs importlib.

    Loaded rather than reimplemented: the projection this suite reads must be the one the
    cron job writes, or the tests measure a vocabulary the service never sees.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "ops", "build-site-book.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("ops_build_site_book", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def setUpModule() -> None:
    global _TMP, BOOK
    spine_path = os.path.join(RECORD, "build", "spine.json")
    builder = _load_ops_builder()
    if builder is None or not os.path.exists(spine_path):
        raise unittest.SkipTest(
            f"the record is not checked out at {RECORD!r}, so there is no real site "
            f"vocabulary to test against. Set KBC_SITE_MEMORY to a checkout. These "
            f"assertions are worthless against an invented site list and are not run "
            f"against one."
        )
    with open(spine_path, "r", encoding="utf-8") as handle:
        spine = json.load(handle)
    _TMP = tempfile.TemporaryDirectory(prefix="naming-misfile-")
    target = os.path.join(_TMP.name, "sites.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(builder.project(spine), handle, ensure_ascii=False)  # type: ignore[attr-defined]
    BOOK = sitebook.load(target)


def tearDownModule() -> None:
    if _TMP is not None:
        _TMP.cleanup()


# --------------------------------------------------------------------------- scaffolding


def transcript_of(lines, step: float = 30.0) -> Transcript:
    """A transcript whose segments are one line each, a step apart.

    Segments rather than bare prose because that is what the engines return and what the
    published body is built from — and because the gap between the engine's continuous
    ``text`` and the published body is one of the things under test here.
    """
    segments = [
        Segment(start=index * step, end=index * step + step - 1.0, speaker=None, text=line)
        for index, line in enumerate(lines)
    ]
    return Transcript(
        text=" ".join(lines),
        segments=segments,
        language="en-ZA",
        engine="stub-engine",
        duration_s=len(segments) * step,
    )


def context(transcript: Transcript, *, site: str = "", duration_s: float = 754.0,
            source: str = "Voice 260806_162219.m4a") -> outputs.OutputContext:
    """The context the pipeline renders from, with the recorder's own default name.

    ``Voice 260806_162219.m4a`` is the only filename that may be renamed at all; every
    other name in this file's fixtures would be refused at E1 before a site was looked at.
    """
    parsed = naming.parse_source_name(source)
    return outputs.OutputContext(
        item_id="ITEM-NAMING-1",
        source_name=source,
        parsed=parsed,
        recorded_at=datetime(2026, 8, 6, 16, 22, 19),
        timestamp_source=parsed.timestamp_note,
        transcript=transcript,
        extraction=StubExtraction(site=site, summary="a site walk"),
        audio=audio_info(duration_s),
        engine="stub-engine",
    )


def render_with(ctx: outputs.OutputContext):
    """The renderer :func:`transcriber.autoname.decide` is handed in production.

    The real one, not a stand-in: N5 and N9 are only worth anything if they run over the
    exact bytes the record will be given, headers and provenance and all.
    """
    return lambda name: outputs.render_transcript(dataclasses.replace(ctx, display_name=name))


def decide(lines, site: str, *, duration_s: float = 754.0, book: sitebook.SiteBook | None = None,
           spoken: str | None = None, apply: bool = True,
           min_seconds: int = 120) -> autoname.NameDecision:
    """One naming decision over a real render, a real site book and a real transcript."""
    transcript = transcript_of(lines)
    ctx = context(transcript, site=site, duration_s=duration_s)
    return autoname.decide(
        parsed=ctx.parsed,
        extraction=ctx.extraction,
        spoken=outputs.spoken_body(transcript) if spoken is None else spoken,
        duration_s=duration_s,
        book=BOOK if book is None else book,
        render=render_with(ctx),
        apply=apply,
        min_seconds=min_seconds,
    )


def walk_at(site: str, *, intruder: str = "") -> list[str]:
    """An ordinary site walk: the site said at the top and again at the end.

    With ``intruder``, the walk he actually records: a short call about somewhere else
    before he starts and another as he leaves. The record files that document under the
    INTRUDER, because it counts distinct vocabulary terms rather than how much of the
    recording is about what — which is the case this whole file exists for.
    """
    lines = []
    if intruder:
        lines.append(f"Quick call first — {intruder}, the cladding, yes I will send it on.")
    lines += [
        f"Right, we are at {site} this morning with the managing agent.",
        "The scaffold is up to the third floor and the crew started yesterday.",
        f"There is water ingress on the corner unit at {site} and the owner has complained.",
        "Tell the painters to hold off until the waterproofing has been signed off.",
        f"That is {site} done for today, I will send the photographs tonight.",
    ]
    if intruder:
        lines.append(f"Taking this as I go — {intruder} again about the same cladding.")
    return lines


def flat(text: str) -> str:
    """Case and whitespace folded away, which is all a name is allowed to differ by."""
    return " ".join((text or "").split()).lower()


class RealRecordTestCase(unittest.TestCase):
    """Refuses to run against anything but the real record.

    A site book that quietly became a two-site fixture would let every assertion below
    pass while proving nothing, so the vocabulary is checked before any of them run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        assert BOOK, "the real site book failed to load"
        # The real record, not a fixture: if these slugs ever go, the walks below are
        # measuring an invented vocabulary and the file needs rewriting, not patching.
        for slug in ("milton-court-sea-point", "canterbury-square", "beach-court-bc",
                     "wolroy-house", "eagle-house", "ashton-steelworks"):
            assert slug in BOOK.sites, f"{slug} is not in the record any more"
        assert BOOK.size >= 50, f"only {BOOK.size} sites — this is not the real record"


# --------------------------------------------------------------------------- the control


class TheHazardIsReal(RealRecordTestCase):
    """Half (a) of the control: a wrong title does not mislabel a note, it loses it.

    If these two fail, nothing else in this file is worth running: the rules being tested
    are guarding against something that does not happen.
    """

    def test_a_body_that_binds_cleanly_to_one_site_binds_to_nothing_once_another_is_named(self) -> None:
        body = outputs.spoken_body(transcript_of(walk_at("Milton Court")))

        before, before_scores = BOOK.bind(body)
        after, after_scores = BOOK.bind(f"Subject: CANTERBURY — voice note transcript\n{body}")

        # The measured claim in autoname's docstring, on his own 56 sites. Milton Court is
        # the record's answer for this body and there is no argument about it.
        self.assertEqual(before, "milton-court-sea-point")
        # And it is gone. Not moved to Canterbury Square — GONE. The record refuses to
        # answer on a tie, so one wrong word in the subject line turns a note that would
        # have landed in Milton Court's correspondence log into a note that lands nowhere
        # at all and that nobody is ever told about.
        self.assertIsNone(after)
        self.assertEqual(after_scores.get("milton-court-sea-point"),
                         after_scores.get("canterbury-square"),
                         "the binding must be destroyed by a tie, which is the mechanism "
                         "the name has to be kept away from")
        self.assertEqual(before_scores.get("canterbury-square"), None)

    def test_the_same_thing_happens_in_the_bytes_the_record_is_actually_handed(self) -> None:
        """Not on a string we made up — on the rendered transcript file itself."""
        ctx = context(transcript_of(walk_at("Milton Court")), site="Milton Court")
        render = render_with(ctx)

        plain = render("")
        right = render("MILTON COURT")
        wrong = render("CANTERBURY")

        self.assertEqual(BOOK.bind(plain)[0], "milton-court-sea-point")
        # The right name changes nothing, which is the whole permission to write one.
        self.assertEqual(BOOK.bind(right)[0], "milton-court-sea-point")
        # The wrong name destroys the filing of a file that was filed correctly. This is
        # the failure the nine rules exist for, and it is one subject line away.
        self.assertIsNone(BOOK.bind(wrong)[0])
        self.assertIn("Subject: CANTERBURY — voice note transcript", wrong)


class _BookWithTheSixthRuleLoosened(sitebook.SiteBook):
    """The record, with N6's vocabulary check answering "yes" to anything.

    Stands in for the future the module docstring names: *"a later hand loosens N3 or N6
    without re-deriving why they were tight."* Nothing else about the book changes — the
    binding, the scores and the titles are the record's own — so what N9 is asked is
    exactly what it would be asked in that future.
    """

    #: The site the loosened rule pretends the span names.
    pretend: str = ""

    def sites_named_by(self, span: str) -> frozenset[str]:
        return frozenset({self.pretend})


class TheLastRuleReportsRatherThanRefuses(RealRecordTestCase):
    """Half (b): what the record will do with the file is REPORTED, not obeyed.

    **This class asserted the opposite until 2026-08-28, and the reversal was deliberate.**
    N9 used to refuse any name that changed the record's answer, on the reasoning that a
    title must never disagree with the filing. An adversarial pass then built the case that
    breaks it: a walk at Eagle House with a short call about Ashton Steelworks at either
    end. The record binds that document to **Ashton Steelworks**, because it scores a site
    by how many DISTINCT vocabulary terms appear once anywhere — three for the passing call,
    one for the hour on site — and never by how often. So the old rules titled the walk
    after the phone call, and would have *refused* a model answering "Eagle House", which
    was the truth.

    James named the same thing independently: *"the problem with your matching is that there
    are times when in conversation we speak about another site."*

    So the record stopped being the judge. Deferring to it made a misfile look deliberate,
    which is the worst available outcome: a wrong filing that a confident title corroborates
    is a wrong filing nobody ever checks. A title that visibly disagrees with the filing is
    a great deal better — it is the one thing that would make a person look.

    What is asserted now: the disagreement is detected, carried on the decision, and said
    out loud in the morning email.
    """

    #: The published transcript never says "Canterbury". This is the model's own answer,
    #: standing in for an N3 loosened to accept the model's spelling rather than the
    #: recording's words — the other half of the future N9 is kept for.
    LOOSENED_BODY = (
        "[00:00:00] Canterbury, right, we are here this morning with the managing agent.\n"
        "[00:00:30] The scaffold is up to the third floor and the crew started yesterday.\n"
        "[00:01:00] There is water ingress on the corner unit and the owner has complained.\n"
        "[00:01:30] Tell the painters to hold off until the waterproofing is signed off.\n"
        "[00:02:00] That is Canterbury done for today, I will send the photographs tonight."
    )

    def test_a_disagreement_between_the_title_and_the_filing_is_carried_on_the_decision(self) -> None:
        """The recording says one site, the record will file it under another. Both are kept."""
        walk = walk_at("Eagle House", intruder="Ashton Steelworks")
        decision = decide(walk, "Eagle House")

        # The title follows the recording, which is what the recording is about.
        self.assertEqual(decision.code, "ok")
        self.assertEqual(decision.name, "EAGLE HOUSE")
        self.assertEqual(decision.site, "eagle-house")
        # And the record's own answer is carried beside it rather than thrown away, because
        # it is the thing he can act on: it says this note is going somewhere he will not
        # look for it, which means the record's vocabulary for Eagle House needs help.
        self.assertTrue(decision.disagrees)
        self.assertEqual(decision.filed, "ashton-steelworks")
        self.assertIn("Ashton Steelworks", decision.why)
        self.assertIn("Eagle House", decision.why)

    def test_agreement_is_reported_too_so_silence_never_means_either(self) -> None:
        decision = decide(walk_at("Milton Court"), "Milton Court")

        self.assertEqual(decision.code, "ok")
        self.assertFalse(decision.disagrees)
        self.assertEqual(decision.filed, "milton-court-sea-point")
        self.assertIn("the record files it there too", decision.why)

    def test_the_model_naming_the_passing_call_is_still_refused(self) -> None:
        """The other half. Demoting the record did not make the rule credulous.

        The same recording, with the model answering "Ashton Steelworks" — the site of the
        call rather than the walk. Nothing in the record contradicts it; the record in fact
        AGREES with it. It is refused anyway, on the count, which is the only thing here
        that knows the difference between a recording and a phone call inside one.
        """
        walk = walk_at("Eagle House", intruder="Ashton Steelworks")
        decision = decide(walk, "Ashton Steelworks")

        self.assertEqual(decision.code, "N7")
        self.assertEqual(decision.name, "")
        self.assertFalse(decision.applied)

    def test_no_name_taken_from_the_published_body_can_ever_change_the_filing(self) -> None:
        """Why N9 cannot fire, checked against every site the record has rather than argued.

        The record scores a term by whether it appears at all, and N3 has already put the
        span in the published body — so adding it to the subject line introduces no term
        the record did not already see. That is the reasoning; this is the measurement.
        """
        checked = 0
        for slug in sorted(BOOK.sites):
            title = BOOK.title_of(slug)
            decision = decide(walk_at(title), title)
            if decision.code != "ok":
                continue
            checked += 1
            with self.subTest(site=slug):
                ctx = context(transcript_of(walk_at(title)), site=title)
                render = render_with(ctx)
                before = BOOK.bind(render(""))[0]
                after = BOOK.bind(render(decision.name))[0]
                # If this ever differs, N9 has become reachable and a recording somewhere
                # is being retitled into a different site's log. It is a finding, not a
                # test to relax.
                self.assertEqual(before, after,
                                 f"naming {slug} {decision.name!r} moved the record's own "
                                 f"answer from {before!r} to {after!r}")
                self.assertEqual(after, decision.site)
        # Not vacuous: most of his sites really can be named from an ordinary walk.
        self.assertGreaterEqual(checked, 20,
                                "too few sites were namable for this sweep to mean anything")


# --------------------------------------------------------------------------- the segments


class WordsSplitAcrossTwoLines(RealRecordTestCase):
    """A two-word site name broken by a pause is not in the file, so it is not a name.

    The published body is one line per segment, prefixed ``[MM:SS] ``. "Beach Court" said
    either side of a breath is contiguous in the engine's prose and split in the file the
    record reads. Naming from the prose would propose a title the published bytes do not
    contain — which is how a walk at one site got filed to another in testing.
    """

    #: A breath in the middle of the site's name, and again at the end. Realistic timings:
    #: the renderer cuts a segment on a pause over 0.9 s.
    SPLIT = [
        "Right, we are at Beach",
        "Court this morning with the chairman of the body corporate.",
        "The scaffold is up to the third floor and the crew started yesterday.",
        "There is water ingress on the corner unit and the owner has complained twice.",
        "That is Beach",
        "Court done for today, I will send the photographs tonight.",
    ]

    def test_a_site_name_broken_by_a_pause_is_not_a_name(self) -> None:
        decision = decide(self.SPLIT, "Beach Court")

        # N3, not N6 and not N7: there is nothing in the published file to take the name
        # from, so the question of which site it names never arises.
        self.assertEqual(decision.code, "N3")
        self.assertEqual(decision.name, "")
        self.assertEqual(decision.span, "")

    def test_the_engines_prose_and_the_published_body_really_do_differ(self) -> None:
        """Without this the test above could pass for no reason at all."""
        transcript = transcript_of(self.SPLIT)
        body = outputs.spoken_body(transcript)

        self.assertIn("Beach Court", transcript.text)
        self.assertNotIn("Beach Court", body)
        # The record still files it under Beach Court either way — 'beach' on its own is
        # one of that site's terms — so what is being refused is the title, not the filing.
        self.assertEqual(BOOK.bind(body)[0], "beach-court-bc")
        # And the difference is the whole difference: fed the engine's prose, the very same
        # decision proposes BEACH COURT — a title the file it is written onto does not
        # contain. That is the mistake spoken_body exists to make impossible.
        from_prose = decide(self.SPLIT, "Beach Court", spoken=transcript.text)
        self.assertEqual(from_prose.code, "ok")
        self.assertEqual(from_prose.name, "BEACH COURT")
        self.assertNotIn(flat(from_prose.name), flat(body))


# --------------------------------------------------------------------------- other sites


class OnThePhoneAboutAnotherSite(RealRecordTestCase):
    """He is standing at one job and talking about another. The walk keeps its own name.

    Seven real pairs out of the record. The site he is at and the site he is talking about
    are both really in the vocabulary, and both are really said out loud, which is what
    makes this the hardest case the feature faces on an ordinary day.
    """

    PAIRS = (
        ("Wolroy House", "Milton Court"),
        ("Forest Hill", "Green Park"),
        ("Pine Tops", "Fairmill"),
        ("Leeuwendal", "Dalrie Hof"),
        ("Eagle House", "Ashton Steelworks"),
        ("Beach Court", "Orion"),
        ("Vineyard Office Estate", "Roggebaai"),
    )

    def test_a_call_in_the_middle_of_a_walk_never_retitles_the_walk(self) -> None:
        for here, other in self.PAIRS:
            with self.subTest(at=here, about=other):
                lines = [
                    f"Right, we are at {here} this morning walking the elevation.",
                    "The scaffold is up to the third floor and the crew started yesterday.",
                    f"Sorry, taking a call. Yes, about {other} — the invoice for {other} "
                    f"has still not come through.",
                    "Back to it. There is water ingress on the corner unit here.",
                    f"That is it from {here}, photographs tonight.",
                ]
                decision = decide(lines, other)

                # No name at all. A walk at Wolroy House titled MILTON COURT would put the
                # walk in Milton Court's correspondence log and leave Wolroy House's page
                # saying nothing happened that day — and it would look deliberate.
                self.assertNotEqual(decision.code, "ok")
                self.assertEqual(decision.name, "")
                self.assertFalse(decision.applied)
                # Refused by one of the four rules that can see this, never by chance.
                self.assertIn(decision.code, {"N3", "N5", "N6", "N7"})

    def test_a_call_at_both_ends_of_a_walk_does_not_retitle_the_walk(self) -> None:
        """FAILS TODAY, ON PURPOSE. The defect is in autoname.py, not in this test.

        A site walk at Eagle House — Eagle House said three times, on the recording, at the
        start, in the middle and at the end — with a short call about Ashton Steelworks
        before it starts and a call back at the end, is confidently titled ASHTON
        STEELWORKS.

        Why: the record scores a document by how many *distinct* terms it recognises, not
        how often. "Ashton Steelworks" carries three of its terms ('ashton', 'ashton
        steelworks', 'steelworks') and scores 6; "Eagle House" carries one and scores 2. So
        the record binds the walk to Ashton Steelworks, N6 is satisfied by the wrong site,
        and N7 — the rule whose comment says "a phone call taken during a walk is local: it
        is a cluster somewhere in the middle, and it fails the spread" — passes, because a
        call at the start and a call back at the end is not a cluster in the middle.

        What it costs: Eagle House's site record never receives the walk, and Ashton
        Steelworks' record receives a walk that did not happen there under a title that
        says it did. An unnamed voice note in the wrong log is something a person opens; a
        note titled ASHTON STEELWORKS in Ashton Steelworks' log is something nobody ever
        opens again. That is hazard 2 word for word.

        Worse, the rule prefers the wrong answer: on this same recording a model answering
        "Eagle House" — the truth — is refused at N6, because the record's binding is
        Ashton Steelworks and the honest span does not name it.

        Fixing it belongs in autoname.py, not here. The shape of a fix: N7 must weigh the
        proposed site against the *other* sites named in the same body rather than only
        against its own spread — a site mentioned twice cannot outrank one mentioned three
        times — or the first-mention window must be measured from the first *substantive*
        span rather than from character zero.
        """
        lines = [
            "Taking a call before we start — yes, Ashton Steelworks, the invoice has not "
            "come through.",
            "Right. We are at Eagle House this morning, walking the north elevation with "
            "the caretaker.",
            "The scaffold at Eagle House is up to the third floor and the crew started "
            "yesterday.",
            "There is water ingress on the corner unit and the owner has complained twice.",
            "Tell the painters to hold off at Eagle House until the waterproofing is "
            "signed off.",
            "Ringing Ashton Steelworks back now about that invoice. Right, that is us done "
            "here.",
        ]

        decision = decide(lines, "Ashton Steelworks")

        self.assertNotEqual(
            decision.name, "ASHTON STEELWORKS",
            "DELIBERATE RED. A site walk at Eagle House has been titled ASHTON STEELWORKS, "
            "the job he was on the phone about at the start and the end of the recording. "
            "Two mentions at the two ends satisfy N7's 'named early and spread across the "
            "recording'. Eagle House's record loses the walk; Ashton Steelworks' record "
            "gains one that never happened there, under a title confident enough that "
            "nobody will open it. Fix autoname.py, then delete this sentence — do not "
            "relax the assertion.",
        )
        self.assertNotEqual(decision.code, "ok")


class OrdinaryEnglishWords(RealRecordTestCase):
    """Every one of these is a word in a real site title and none of them names a site.

    The model is asked for a site and will sometimes answer with a word. Each is offered
    as its answer on a walk that really is at Wolroy House, and each really is said out
    loud on that walk, so N3 cannot be what refuses it.
    """

    WORDS = ("house", "north", "green", "forest", "garden", "village", "mill", "pine",
             "beach", "milton", "eagle")

    def test_a_word_that_appears_in_a_real_site_title_is_never_a_name(self) -> None:
        for word in self.WORDS:
            with self.subTest(word=word):
                lines = list(walk_at("Wolroy House"))
                lines[1] += f" The {word} side needs a look while we are here."
                lines[3] += f" Same again at the {word} end."

                decision = decide(lines, word)

                # Refused every time, and by the record's own rules rather than a stop list
                # we would have to keep up to date. Two of them do it, and which one depends
                # on the word rather than on anything we chose: "house" and "north" name no
                # site at all, because the record drops any term it uses of more than two
                # sites (N6); "beach", "milton" and "village" do name exactly one, and are
                # then refused for not being what the recording is about (N7). Both are
                # correct and the outcome is what is asserted strictly — a transcript titled
                # HOUSE or NORTH would be read by a person as a claim about where he was.
                self.assertIn(decision.code, ("N6", "N7"),
                              f"{word!r} was refused by {decision.code}")
                self.assertEqual(decision.name, "")
                self.assertFalse(decision.applied)

    def test_the_walk_underneath_those_words_really_can_be_named(self) -> None:
        """So the test above is measuring the word, not a walk that could never be named."""
        self.assertEqual(decide(walk_at("Wolroy House"), "Wolroy House").name, "WOLROY HOUSE")


class TheFourWaysAModelGetsItWrong(RealRecordTestCase):
    """Garbled, canonicalised, evasive, and about somewhere the record has never heard of.

    Each asserts *which* rule refused it, because a case that starts being refused by a
    different rule has changed meaning even when it still refuses.
    """

    def test_a_misheard_site_name_is_refused_because_the_record_does_not_know_it(self) -> None:
        # He said Beach Court; the engine wrote "Beech Court"; the model repeated the
        # engine. The body still binds — "beach court" survives once — so this gets all the
        # way to the vocabulary check before it is stopped.
        lines = [
            "Right, we are at Beech Court this morning with the chairman.",
            "The scaffold is up and the beach court crew started yesterday on the north side.",
            "There is water ingress on the corner unit and the owner has complained twice.",
            "Tell the painters to hold off until the waterproofing has been signed off.",
            "That is Beech Court done for today, photographs tonight.",
        ]
        decision = decide(lines, "Beech Court")

        self.assertEqual(decision.code, "N6")
        # No site, because nothing bound it and nothing may. The old rule set filled this
        # in from the record's binding of the whole document, which is exactly the binding
        # that titled an Eagle House walk after a phone call.
        self.assertEqual(decision.site, "")
        # One letter. BEECH COURT would read to a person as a real place and sort next to
        # nothing in the record.
        self.assertEqual(decision.name, "")

    def test_a_misheard_name_the_record_cannot_place_at_all_is_refused_earlier(self) -> None:
        lines = [
            "Right, we are at Beech Court this morning with the chairman.",
            "The scaffold is up and the crew started yesterday on the north side.",
            "There is water ingress on the corner unit and the owner has complained twice.",
            "Tell the painters to hold off until the waterproofing has been signed off.",
            "That is Beech Court done for today, photographs tonight.",
        ]
        decision = decide(lines, "Beech Court")

        # N5: the record itself cannot say where this belongs, so a title would claim more
        # than the recording does. This is the honest answer to a transcript nobody can file.
        # N6: the span is not a site the record knows about. (This was N5 while the rule
        # set also asked the record to bind the whole document; that check was removed when
        # deferring to the record turned out to title walks after phone calls.)
        self.assertEqual(decision.code, "N6")
        self.assertEqual(decision.site, "")
        self.assertEqual(decision.name, "")

    def test_the_records_own_punctuated_title_is_refused_because_nobody_said_it(self) -> None:
        lines = [
            "Right, we are at 22 Chepstow this morning, up in Sea Point.",
            "The scaffold is up to the third floor and the crew started yesterday.",
            "There is water ingress on the corner unit and the owner has complained twice.",
            "Tell the painters to hold off until the waterproofing has been signed off.",
            "That is 22 Chepstow done for today, photographs tonight.",
        ]
        decision = decide(lines, "22 Chepstow, Sea Point")

        # N3. The model canonicalised to the record's own title, comma and all — which is
        # neither what he says nor what was said. A name has to be the recording's own
        # characters or it is the model asserting something about the world.
        self.assertEqual(decision.code, "N3")
        self.assertEqual(decision.name, "")
        # And the same recording, with the words that were actually spoken, is namable —
        # so N3 is refusing the canonicalisation and not the site.
        self.assertEqual(decide(lines, "22 Chepstow").name, "22 CHEPSTOW")

    def test_a_placeholder_is_refused_before_the_record_is_consulted(self) -> None:
        for placeholder in ("the site", "here", "site", "various", "unknown"):
            with self.subTest(placeholder=placeholder):
                lines = list(walk_at("Wolroy House"))
                lines[0] += f" We are on {placeholder}, as it were."
                lines[4] += f" That is {placeholder} finished."

                decision = decide(lines, placeholder)

                # N2, and deliberately before N3/N5/N6: every one of these is a real thing
                # to say on a walk, so they would otherwise sail through the body check and
                # be argued about by the vocabulary. "HERE" is not a title.
                self.assertEqual(decision.code, "N2")
                self.assertEqual(decision.name, "")

    def test_a_site_the_record_has_no_folder_for_is_refused(self) -> None:
        lines = walk_at("Rondebosch Heights")

        decision = decide(lines, "Rondebosch Heights")

        # N5. Nothing in the record recognises it, so nothing binds, so there is nothing to
        # check a title against. A new job that has not been set up yet keeps the recorder's
        # own name until it has — which is exactly today's behaviour and costs nothing.
        self.assertEqual(decision.code, "N6")
        self.assertEqual(decision.name, "")

    def test_a_new_site_sharing_one_word_with_an_old_one_is_partly_still_a_problem(self) -> None:
        """A residual, asserted as it is rather than as I would like it. Half closed.

        "Beach Road" is not in the record. The record has "Beach Court bc", and *beach* is
        one of its discriminating terms — so the record files a recording about Beach Road
        into Beach Court's log with or without a title, and the naming rule agrees with it
        and writes BEACH ROAD on the subject line.

        **Tightening ``sites_named_by`` closed one of the three.** A span now has to carry
        at least half of what the record itself calls the site, so "Milton Road" no longer
        names *Milton Court - Sea Point* — one word of three. "Beach Road" and "Canterbury
        Road" still do, because those titles are two words and the shared one is half.

        The remaining two cannot be separated from the case they look exactly like:
        ``CANTERBURY`` naming *Canterbury Square* is the same shape, and it is what he
        writes on his own files. Closing it would need a hand-kept list of road names, which
        is the maintained vocabulary this whole design exists to avoid.

        The bound on the damage is the last assertion below: the record files it in the same
        place with the title and without, so a title never *moved* anything. It makes a
        pre-existing misfile look deliberate, which is bad — and strictly less bad than
        causing one.
        """
        # Closed by the half-of-the-title rule.
        self.assertEqual(decide(walk_at("Milton Road"), "Milton Road").code, "N6")

        for spoken_name, expected_slug in (("Beach Road", "beach-court-bc"),
                                           ("Canterbury Road", "canterbury-square")):
            with self.subTest(site=spoken_name):
                decision = decide(walk_at(spoken_name), spoken_name)

                self.assertEqual(decision.code, "ok")
                self.assertEqual(decision.name, spoken_name.upper())
                self.assertEqual(decision.site, expected_slug)
                # The one thing that saves this from being a misfile: the record binds the
                # file to the same site with the name and without it, so the title never
                # moved anything. It only makes a pre-existing misfile look deliberate.
                ctx = context(transcript_of(walk_at(spoken_name)), site=spoken_name)
                render = render_with(ctx)
                self.assertEqual(BOOK.bind(render(""))[0], BOOK.bind(render(decision.name))[0])


class AHallucinationLoop(RealRecordTestCase):
    """Wind noise comes back as a site name said twice, early, and spread. On purpose.

    The two conditions that look like evidence — mentioned twice, mentioned early — ARE
    the signature of an engine looping on a short file. Length is the only cheap thing that
    separates them, and these tests say exactly how much work it is doing.
    """

    LOOP = ["Canterbury Square. Thank you for watching.",
            "Canterbury Square, thank you for watching."]

    def test_a_forty_second_loop_is_refused_on_length_before_anything_else(self) -> None:
        decision = decide(self.LOOP, "Canterbury Square", duration_s=40.0)

        # E4, and before the site list is even opened. A forty-second recording cannot
        # supply the evidence the later rules would read, so it is never asked to.
        self.assertEqual(decision.code, "E4")
        self.assertIn("40s", decision.why)
        self.assertEqual(decision.name, "")

    def test_the_same_loop_at_four_hundred_seconds_is_refused_but_not_for_being_a_loop(self) -> None:
        """The known residual, recorded as it actually behaves.

        Past the length floor this text is refused — but by N6, and only because 'square'
        happens to appear in two of his site titles. Nothing in the refusal has anything to
        do with the recording being a hallucination.
        """
        decision = decide(self.LOOP, "Canterbury Square", duration_s=400.0)

        self.assertEqual(decision.code, "N6")
        self.assertIn("could be more than one site", decision.why)

    def test_the_same_loop_on_an_unambiguous_site_is_named_past_the_length_floor(self) -> None:
        """Also the residual, and the sharp end of it. Asserted, not wished away.

        Swap Canterbury Square for a site whose name is unambiguous in the record and the
        identical loop produces a confident title on what may be seven minutes of wind. The
        duration floor is the whole defence; nothing downstream of it can tell a looping
        engine from a man saying where he is.
        """
        for site, slug in (("Wolroy House", "wolroy-house"),
                           ("Milton Court", "milton-court-sea-point")):
            with self.subTest(site=site):
                loop = [f"{site}. Thank you for watching.",
                        f"{site}, thank you for watching."]
                decision = decide(loop, site, duration_s=400.0)

                self.assertEqual(decision.code, "ok")
                self.assertEqual(decision.name, site.upper())
                self.assertEqual(decision.site, slug)

    def test_the_length_floor_is_what_is_holding_it_and_it_is_configurable(self) -> None:
        """So a lowered NAMING_MIN_SECONDS is understood to be lowering the only guard."""
        loop = ["Wolroy House. Thank you for watching.",
                "Wolroy House, thank you for watching."]

        self.assertEqual(decide(loop, "Wolroy House", duration_s=100.0).code, "E4")
        self.assertEqual(decide(loop, "Wolroy House", duration_s=100.0,
                                min_seconds=60).code, "ok")


class WhatAnEmittedNameMayContain(RealRecordTestCase):
    """Two properties of every name this service will ever write, over the whole record."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.emitted = []
        for slug in sorted(BOOK.sites):
            title = BOOK.title_of(slug)
            decision = decide(walk_at(title), title)
            if decision.code == "ok":
                cls.emitted.append((slug, title, decision))
        assert len(cls.emitted) >= 20, "too few names to assert anything about"

    def test_no_emitted_name_carries_a_four_digit_run(self) -> None:
        for slug, _title, decision in self.emitted:
            with self.subTest(site=slug):
                # A date, a year or a monday item id in a subject line is the record's own
                # date parser's problem: it reads the first date-shaped thing it finds, and
                # the item's id and month folder come from that. A title carrying 260806
                # would file the note in a month it was not recorded in.
                self.assertIsNone(re.search(r"\d{4}", decision.name),
                                  f"{decision.name!r} carries a four-digit run")

    def test_every_emitted_name_is_a_contiguous_run_of_the_published_body(self) -> None:
        for slug, title, decision in self.emitted:
            with self.subTest(site=slug):
                body = outputs.spoken_body(transcript_of(walk_at(title)))
                # Case and whitespace are the only things a name may differ by. Anything
                # else — a word reordered, a comma dropped, a spelling tidied — is the
                # service saying something the recording did not.
                self.assertIn(flat(decision.name), flat(body),
                              f"{decision.name!r} is not in the file it will be written on")
                self.assertEqual(flat(decision.name), flat(decision.span))


class TheSameRecordingDecidesTheSameWay(RealRecordTestCase):
    """A retry must not reach the opposite answer and write a second subject line."""

    def test_deciding_twice_reaches_the_same_answer(self) -> None:
        for site in ("Wolroy House", "Beech Court", "here", "Milton Court"):
            with self.subTest(site=site):
                lines = walk_at("Wolroy House") if site != "Wolroy House" else walk_at(site)
                first = decide(lines, site)
                second = decide(lines, site)

                # Same transcript, same book, same item. A publish that failed halfway and
                # retried the next morning writing a different subject line would leave the
                # record holding two documents for one recording and no way to reconcile
                # them — which is the incumbent's failure, committed by us.
                self.assertEqual(first.as_meta(), second.as_meta())

    def test_a_stored_decision_reads_back_identical(self) -> None:
        decision = decide(walk_at("Wolroy House"), "Wolroy House")

        restored = autoname.NameDecision.from_meta(json.loads(json.dumps(decision.as_meta())))

        self.assertIsNotNone(restored)
        assert restored is not None
        # Through JSON, because that is how it sits on the ledger row between the ANALYSED
        # write and the publish that reads it back.
        self.assertEqual(restored.as_meta(), decision.as_meta())
        self.assertEqual(restored.name, "WOLROY HOUSE")


class NothingHereMayCostARecording(RealRecordTestCase):
    """Rule one. Every failure in this path ends in a published transcript with no title."""

    def test_a_renderer_that_throws_ends_in_no_name_rather_than_an_exception(self) -> None:
        transcript = transcript_of(walk_at("Wolroy House"))
        ctx = context(transcript, site="Wolroy House")

        def always_throws(name: str) -> str:
            raise RuntimeError("Graph fell over")

        def throws_only_when_named(name: str) -> str:
            if name:
                raise RuntimeError("Graph fell over")
            return outputs.render_transcript(ctx)

        for label, render in (("both renders", always_throws),
                              ("the second render", throws_only_when_named)):
            with self.subTest(broken=label):
                decision = autoname.decide(
                    parsed=ctx.parsed, extraction=ctx.extraction,
                    spoken=outputs.spoken_body(transcript), duration_s=754.0,
                    book=BOOK, render=render, apply=True, min_seconds=120,
                )
                # No exception, and a decision reached. The three files still get written
                # either way; losing one is the only thing this service exists to prevent.
                self.assertTrue(decision.decided)

                # And the NAME still stands, which is the change worth stating. The renderer
                # is used for one thing now — asking the record where it will file this —
                # and that answer is a note in the morning email, not a veto. It used to be
                # a veto, and the reversal is why: the record decides by counting distinct
                # vocabulary words rather than how much of a recording is about what, so it
                # titles a walk after a phone call taken during it. A renderer that falls
                # over should cost the footnote, not the title.
                self.assertEqual(decision.code, "ok")
                self.assertEqual(decision.name, "WOLROY HOUSE")
                self.assertTrue(decision.applied)
                self.assertEqual(decision.filed, "")
                self.assertFalse(decision.disagrees)

    def test_a_model_answer_that_is_not_a_name_never_raises_and_never_names(self) -> None:
        for answer in ("(.*)+", "\x00\x01", "  ", "22", "!!!", "a" * 61, "Wolroy" * 40,
                       "[[[unclosed", "\\", "site — ?"):
            with self.subTest(answer=answer):
                decision = decide(walk_at("Wolroy House"), answer)

                # The model's answer reaches a regular expression. Anything it can say has
                # to come back as a refusal, not as a traceback in the publish path.
                self.assertTrue(decision.decided)
                self.assertEqual(decision.name, "")

    def test_no_site_list_means_no_name_and_says_so(self) -> None:
        decision = decide(walk_at("Wolroy House"), "Wolroy House", book=sitebook.EMPTY)

        # N0, with a reason that reaches the morning email. A site list that quietly stopped
        # being written must not look like a quiet week.
        self.assertEqual(decision.code, "N0")
        self.assertEqual(decision.name, "")
        self.assertTrue(decision.why)

    def test_a_name_he_typed_is_never_touched(self) -> None:
        for source in ("BEACH COURT SITE WALK 270826.m4a", "CJ.m4a", "Q.m4a", "JORDS.m4a",
                       "Morne Interview.m4a", "voice 260806_162219.m4a",
                       "Voice 260806_162219 CANTERBURY.m4a"):
            with self.subTest(source=source):
                parsed = naming.parse_source_name(source)
                ok, code, why = autoname.eligible(
                    parsed, StubExtraction(site="Wolroy House"), 754.0, min_seconds=120)

                # Every one of these looks nameless to a machine and is not: they are what
                # he calls those people and those jobs. A service that renames what a person
                # chose is a service he switches off, and then he loses the recordings again.
                self.assertFalse(ok, f"{source!r} would have been renamed")
                self.assertEqual(code, "E1")
                self.assertEqual(why, "he named this one himself")

    def test_the_one_filename_that_may_be_renamed_is_the_recorders_own_default(self) -> None:
        parsed = naming.parse_source_name("Voice 260806_162219.m4a")
        ok, code, _why = autoname.eligible(
            parsed, StubExtraction(site="Wolroy House"), 754.0, min_seconds=120)

        self.assertTrue(ok, "the whole feature applies to this name and no other")
        self.assertEqual(code, "")


if __name__ == "__main__":                                          # pragma: no cover
    unittest.main()
