"""Which jobs a recording names, handed over as evidence — never as a filing.

A site walk covers the job somebody is standing on and the two that are bothering them. One
real recording ran through HQ, Lonehill and Pick n Pay in half an hour, and everything
downstream had to work that out again from raw text, from scratch, every time.

The work was already being done here and thrown away. ``SiteBook.bind`` runs the record's
own scoring — vendored verbatim — over the exact bytes the record is handed, and scores
EVERY job. Only the winner was kept, to propose a title. The rest went in the bin and the
hunt started again downstream.

**Two rules hold this together, and the first is the one that could do damage.**

*Nothing goes in the transcript.* The transcript is the one file the record ingests as a
source, and it binds a document by scoring the document's own text. A job name written into
it becomes evidence for the very question it was meant to answer: a mis-transcription would
confirm itself, file the recording against the wrong job, and look well supported doing it.
``sitebook.py``'s own docstring says a name asserted wrongly here can *move* a filing that
was previously right. So it goes only in the two files whose leading underscore tells the
record's intake to skip them.

*Candidates, not a verdict.* Scores and the words behind them travel; the decision does not.
The record still decides.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from transcriber import outputs, sitebook

BOOK_DIR = tempfile.mkdtemp(prefix="site-evidence-")

#: Three of his real jobs, including the one the engine actually got wrong.
JOBS = {
    "lonehill-shopping-centre": {"title": "Lonehill Shopping Centre"},
    "beach-court-mouille-point": {"title": "Beach Court Mouille Point"},
    "amidal-industrial": {"title": "Amidal Industrial"},
}


def book_at(name: str, sites=None) -> sitebook.SiteBook:
    path = os.path.join(BOOK_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"vocab_contract": sitebook.CONTRACT, "generated_at": "2026-09-04",
                   "sites": JOBS if sites is None else sites}, fh)
    return sitebook.load(path)


BOOK = book_at("sites.json")

#: What that HQ walk sounds like once the engine has been told the job names — the fix that
#: had to land first, because a name never transcribed cannot be matched by anything.
SPOKEN = (
    "your consumption is going to be wrong on Lonehill Shopping Centre. "
    "we have the same issue at Lonehill Shopping Centre. "
    "the Beach Court Mouille Point trustees want the roof done. "
    "so block C we are pretty much done, it is snags."
)


class TheScoringIsHandedOverInsteadOfThrownAway(unittest.TestCase):
    def test_every_job_the_words_name_comes_through_ranked(self) -> None:
        ev = sitebook.evidence_for(BOOK, SPOKEN)
        slugs = [c.slug for c in ev.candidates]
        self.assertIn("lonehill-shopping-centre", slugs)
        self.assertIn("beach-court-mouille-point", slugs)
        self.assertEqual(slugs, sorted(slugs, key=lambda s: -dict(
            (c.slug, c.score) for c in ev.candidates)[s]))

    def test_the_score_is_about_how_distinctly_a_job_was_named_not_how_often(self) -> None:
        """Worth knowing before anybody reads the table as a popularity contest.

        In this recording Lonehill is named twice and Beach Court Mouille Point once, and
        Beach Court still scores higher — its longer title carries more terms that identify
        it and nothing else. The number says "how surely was this job named", not "how much
        was it discussed". These are the record's own rules, vendored verbatim, and this
        test exists because the obvious reading of the column is the wrong one.
        """
        ev = sitebook.evidence_for(BOOK, SPOKEN)
        by = {c.slug: c.score for c in ev.candidates}
        self.assertEqual(SPOKEN.lower().count("lonehill"), 2)
        self.assertEqual(SPOKEN.lower().count("beach court"), 1)
        self.assertGreater(by["beach-court-mouille-point"], by["lonehill-shopping-centre"])

    def test_a_job_nobody_mentioned_is_not_a_candidate(self) -> None:
        ev = sitebook.evidence_for(BOOK, SPOKEN)
        self.assertNotIn("amidal-industrial", [c.slug for c in ev.candidates])

    def test_as_transcribed_TODAY_the_record_gets_nothing(self) -> None:
        """The before picture, and the reason the engine hint list had to land first.

        This is the real mis-transcription: "on loan" and "at lo" are both Lonehill, and no
        matcher recovers a name that was never written down.
        """
        mangled = ("your consumption is going to be wrong on loan. "
                   "we have the same issue at lo.")
        self.assertEqual(sitebook.evidence_for(BOOK, mangled).candidates, ())


class EachLineCarriesTheJobItsOwnWordsName(unittest.TestCase):
    def test_a_line_that_names_a_job_says_which(self) -> None:
        ev = sitebook.evidence_for(BOOK, SPOKEN,
                                   quotes=["wrong on Lonehill Shopping Centre"])
        self.assertEqual(ev.slugs_for("wrong on Lonehill Shopping Centre"),
                         ("lonehill-shopping-centre",))

    def test_a_line_that_names_none_says_nothing(self) -> None:
        """Silence is a real answer. 'This line does not say' beats a guess."""
        ev = sitebook.evidence_for(BOOK, SPOKEN, quotes=["so block C we are pretty much done"])
        self.assertEqual(ev.slugs_for("so block C we are pretty much done"), ())

    def test_the_lookup_survives_whitespace(self) -> None:
        ev = sitebook.evidence_for(BOOK, SPOKEN, quotes=["wrong on Lonehill Shopping Centre"])
        self.assertEqual(ev.slugs_for("  wrong   on  LONEHILL Shopping Centre "),
                         ("lonehill-shopping-centre",))

    def test_it_is_keyed_by_quote_not_by_position(self) -> None:
        """The actions file renders proposals grouped by category, not in extraction order.
        A lookup that depended on the caller iterating in the right order would eventually
        be wrong, and silently."""
        ev = sitebook.evidence_for(BOOK, SPOKEN, quotes=[
            "the Beach Court Mouille Point trustees want the roof done",
            "wrong on Lonehill Shopping Centre",
        ])
        self.assertEqual(ev.slugs_for("wrong on Lonehill Shopping Centre"),
                         ("lonehill-shopping-centre",))
        self.assertEqual(ev.slugs_for("the Beach Court Mouille Point trustees want the roof done"),
                         ("beach-court-mouille-point",))


class NothingGoesInTheTranscript(unittest.TestCase):
    """The rule that stops this from being able to cause the damage it exists to prevent."""

    def _rendered(self):
        import datetime

        from tests import support
        from transcriber import naming
        from transcriber.models import Segment, Transcript

        parsed = naming.parse_source_name("HQ SITE WALK 030926.m4a")
        when = datetime.datetime(2026, 9, 3, 14, 40, tzinfo=datetime.timezone.utc)
        ctx = outputs.OutputContext(
            item_id="evidence-test", source_name="HQ SITE WALK 030926.m4a",
            parsed=parsed, recorded_at=when, timestamp_source="the file's own time",
            transcript=Transcript(text=SPOKEN,
                                  segments=[Segment(start=0.0, end=60.0, text=SPOKEN)],
                                  engine="openai", duration_s=1860.0),
            extraction=support.StubExtraction(summary="A walk of the HQ roof."),
            audio=support.audio_info(duration_s=1860.0),
            engine="openai",
            site_evidence=sitebook.evidence_for(BOOK, SPOKEN, quotes=[]),
        )
        return ctx, {f.kind: f.text for f in outputs.render_all(ctx)}

    def test_the_evidence_block_never_reaches_the_transcript(self) -> None:
        """Not "no job name appears" — a job name appears in the transcript whenever
        somebody said it, and it must. What must never appear is OUR reading of it: the
        section, the scores, the matching. Those are the bytes the record would score as
        though a person had spoken them.
        """
        _ctx, files = self._rendered()
        transcript = files["transcript"]
        for ours in ("Which jobs this recording names", "| Job | Score |",
                     "evidence, not a filing", "Matched against the job list"):
            self.assertNotIn(ours, transcript)

    def test_the_spoken_words_are_left_exactly_alone(self) -> None:
        """The other half: this must not have edited the evidence either."""
        _ctx, files = self._rendered()
        self.assertIn("wrong on Lonehill Shopping Centre", files["transcript"])

    def test_the_candidates_do_reach_the_summary(self) -> None:
        _ctx, files = self._rendered()
        self.assertIn("Which jobs this recording names", files["summary"])
        self.assertIn("Lonehill Shopping Centre", files["summary"])

    def test_the_summary_and_actions_are_the_files_the_record_skips(self) -> None:
        """The underscore is what makes writing job names into them safe at all."""
        ctx, _files = self._rendered()
        self.assertTrue(ctx.names.summary.startswith("_"))
        self.assertTrue(ctx.names.actions.startswith("_"))
        self.assertFalse(ctx.names.transcript.startswith("_"))

    def test_it_says_it_is_evidence_and_not_a_filing(self) -> None:
        _ctx, files = self._rendered()
        self.assertIn("evidence, not a filing", files["summary"])


class NothingHereCanCostARecording(unittest.TestCase):
    def test_no_book_gives_a_fault_not_a_crash(self) -> None:
        ev = sitebook.evidence_for(sitebook.EMPTY, SPOKEN)
        self.assertFalse(ev.candidates)
        self.assertTrue(ev.fault)

    def test_a_fault_is_reported_as_a_fault_not_as_no_jobs(self) -> None:
        """'No job matched' and 'the job list could not be read' are different facts and
        must not render the same."""
        ctx_lines = outputs._site_evidence_lines(
            type("C", (), {"site_evidence": sitebook.SiteEvidence(fault="it is missing")})()
        )
        self.assertTrue(any("could not be read" in line for line in ctx_lines))
        self.assertFalse(any("| Job | Score |" in line for line in ctx_lines))

    def test_no_evidence_at_all_renders_nothing_rather_than_an_empty_table(self) -> None:
        self.assertEqual(
            outputs._site_evidence_lines(type("C", (), {"site_evidence": None})()), [])


if __name__ == "__main__":
    unittest.main()
