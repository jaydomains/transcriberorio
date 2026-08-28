"""The sensitivity gate: what it holds, what it lets through, and what it refuses to do.

The gate is the one part of this service that can *remove* information from the record, so
these tests are written against the two failures that removal makes possible, and they are
not symmetrical.

  * **Holding too much is the failure that kills it.** Nothing here drains itself — no
    automatic release, no deadline, no cap that commits the overflow — so every held
    passage costs an approval that only one person can give. Ten of those a day and he
    stops opening the page, and a gate nobody opens silently swallows the record. So the
    largest group of tests below asserts what is *not* held: prices, deliveries, dates, a
    named person doing their job, an invoice number that looks like an identity number, and
    "don't write to the trustees yet", which is an instruction about correspondence and not
    about the record.
  * **Holding the wrong span is a leak wearing a redaction's clothes.** Spans are exact and
    non-overlapping because another module cuts text on them, so they are asserted by
    cutting the text and reading what is left.

And one ordering, which the module exists inside rather than enforces: quote verification
runs against the ORIGINAL transcript, before masking. Mask first and every extracted item
quoting a masked passage fails verification and is silently destroyed — a redaction that
deletes somebody's action items. ``Report.covers`` is the residue of that ordering and is
tested here.
"""

from __future__ import annotations

import json
import unittest

from transcriber import prompts
from transcriber import sensitivity as sen
from transcriber.config import Config, ConfigError
from transcriber.extract import normalise_for_match, verify_quote
from transcriber.setup_wizard import routes_to_values
from transcriber.models import Route


# A recording of the kind that makes up most of his day: money in it, people in it, and
# nothing in it that anybody would be harmed by. Long enough that a held span is a small
# fraction of it, as it is in life.
ORDINARY = (
    "James: Right, I'm at Beach Court, block C, Tuesday morning.\n"
    "James: The chromadek arrives Tuesday and the scaffold comes down Friday.\n"
    "James: Thabo finished the flashing on block C, it looks good.\n"
    "James: The quote to the body corporate is R4,500 for the torch-on repair, and they "
    "have accepted it.\n"
    "James: The supplier rate is R92 a square and the contract sum is R840,000.\n"
    "James: The waterproofing at unit 12 is lifting again, their workmanship is poor.\n"
    "James: I'll send the trustees a written instruction on Friday.\n"
)


def passage(quote: str, category: str, *, confidence: float = 0.9, what: str = "", reason: str = "") -> dict:
    """One entry as the model returns it in ``sensitive_passages``."""
    return {
        "quote": quote,
        "category": category,
        "who_is_harmed": "somebody",
        "what_it_is": what or sen.PUBLIC_SUBJECTS[category],
        "reason": reason or "because it was said",
        "confidence": confidence,
    }


def assess(transcript: str, *passages: dict, mode: str = "on", **kwargs) -> sen.Report:
    return sen.assess(
        transcript,
        {"sensitive_passages": list(passages)},
        settings=sen.GateSettings(mode=mode, **kwargs),
    )


def masked(transcript: str, report: sen.Report) -> str:
    """The transcript with every masked span cut out — what the record would receive."""
    out = []
    at = 0
    for finding in report.spans_to_mask():
        out.append(transcript[at:finding.start])
        out.append(f"[held: {finding.subject}]")
        at = finding.end
    out.append(transcript[at:])
    return "".join(out)


class MostOfTheTimeTheAnswerIsNothing(unittest.TestCase):
    """Treating ordinary site talk as sensitive buries the few items that matter.

    This is the first test in the file on purpose. Materials, deliveries, programme dates
    and somebody doing their job are not sensitive, and a gate that thinks otherwise has
    already failed however well it does on the hard cases.
    """

    def test_an_ordinary_recording_with_no_model_findings_holds_nothing(self) -> None:
        report = assess(ORDINARY)

        self.assertEqual(report.findings, ())
        self.assertEqual(report.spans_to_mask(), ())
        self.assertEqual(masked(ORDINARY, report), ORDINARY)

    def test_the_rules_alone_never_react_to_a_rand_figure(self) -> None:
        """6.3% of his record's content lines carry one. A rule that fired on them is a wall."""
        findings = sen.rule_findings(ORDINARY)

        self.assertEqual(findings, ())

    def test_a_named_person_doing_their_job_is_not_sensitive(self) -> None:
        report = assess("James: Thabo finished the flashing on block C and it looks good.")

        self.assertEqual(report.findings, ())


class PricesFlow(unittest.TestCase):
    """His own decision, taken against his first instinct, on a measurement.

    Holding prices is ten to fifteen approvals a day. A price reaching a client is a problem
    to solve on the way out, and the record is *meant* to hold his margin — it already does,
    under a standing "internal record, not client-facing" header.
    """

    def test_a_price_to_a_client_is_published_with_a_label(self) -> None:
        report = assess(
            ORDINARY,
            passage("The quote to the body corporate is R4,500 for the torch-on repair",
                    "commercial_figure", what="a price quoted to a client"),
        )

        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertFalse(finding.held)
        self.assertEqual(finding.disposition, sen.LABELLED)
        self.assertIn("R4,500", ORDINARY[finding.start:finding.end])
        self.assertEqual(report.spans_to_mask(), ())
        self.assertIn("R4,500", masked(ORDINARY, report))

    def test_a_supplier_rate_and_a_contract_sum_are_the_same_answer(self) -> None:
        report = assess(
            ORDINARY,
            passage("The supplier rate is R92 a square and the contract sum is R840,000",
                    "commercial_figure"),
        )

        self.assertEqual([f.disposition for f in report.findings], [sen.LABELLED])

    def test_the_label_says_which_kind_it_is(self) -> None:
        """A label nothing can read is decoration; the outbound check reads this one."""
        report = assess(
            ORDINARY,
            passage("The supplier rate is R92 a square", "commercial_figure"),
        )

        self.assertEqual(report.findings[0].category, "commercial_figure")
        self.assertNotEqual(report.findings[0].category, "own_margin")

    def test_only_the_margin_holds(self) -> None:
        transcript = ORDINARY + "James: We raised R1.65m and we'll land at R1.604m, so there's a bit in it.\n"
        report = assess(
            transcript,
            passage("We raised R1.65m and we'll land at R1.604m", "own_margin"),
            passage("The quote to the body corporate is R4,500 for the torch-on repair",
                    "commercial_figure"),
        )

        held = report.would_hold()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].category, "own_margin")
        text = masked(transcript, report)
        self.assertNotIn("R1.604m", text)
        self.assertNotIn("R1.65m", text)
        self.assertIn("R4,500", text)


class DoNotWriteThisDownOutranksEverything(unittest.TestCase):
    """A person's explicit instruction about their own words, in any language.

    It is the one thing here that is not a judgement, so it is mechanical: it holds without
    a model agreeing, and it is never downgraded for want of confidence.
    """

    def test_english(self) -> None:
        transcript = (
            "James: The roof is fine on block C.\n"
            "James: Don't write this down, but the engineer signed off a slab he never "
            "inspected.\n"
            "James: Anyway, the chromadek arrives Tuesday.\n"
        )
        report = assess(transcript)

        held = report.would_hold()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].category, "do_not_write_down")
        self.assertEqual(held[0].source, "rule")
        self.assertNotIn("never inspected", masked(transcript, report))

    def test_afrikaans(self) -> None:
        transcript = (
            "Piet: Die dak lek by die parapet.\n"
            "Piet: Moenie dit neerskryf nie, ons het self die detail verkeerd gehad.\n"
            "Piet: Ons gaan more begin.\n"
        )
        report = assess(transcript)

        self.assertEqual([f.category for f in report.would_hold()], ["do_not_write_down"])
        self.assertNotIn("verkeerd gehad", masked(transcript, report))

    def test_isixhosa(self) -> None:
        transcript = (
            "Thabo: Umsebenzi uqhubeka kakuhle kwiblock C.\n"
            "Thabo: Ungakubhali oku, uSipho akaphangeli namhlanje kuba unyana wakhe ugula.\n"
            "Thabo: Sizoqeda ngomso.\n"
        )
        report = assess(transcript)

        self.assertEqual([f.category for f in report.would_hold()], ["do_not_write_down"])

    def test_it_holds_the_words_it_is_about_and_not_only_the_phrase(self) -> None:
        """Holding four words and publishing what they refer to withholds nothing at all."""
        transcript = (
            "James: The insurer called this morning.\n"
            "James: Off the record, we are going to have to admit the beam was "
            "under-designed.\n"
            "James: I'll call you back.\n"
        )
        report = assess(transcript)

        self.assertNotIn("under-designed", masked(transcript, report))

    def test_it_is_never_downgraded_for_want_of_confidence(self) -> None:
        transcript = "James: Don't write this down, the certificate was backdated.\n"
        report = assess(transcript, passage("the certificate was backdated",
                                            "do_not_write_down", confidence=0.1))

        self.assertTrue(report.would_hold())
        self.assertFalse(any(f.downgraded for f in report.would_hold()))

    def test_an_instruction_about_correspondence_is_not_one_about_the_record(self) -> None:
        """"Don't write to the trustees yet" is a working instruction, not a confidence.

        A rule that could not tell the two apart would hold a large part of an ordinary
        week, which is the failure this whole design is tuned against.
        """
        for line in (
            "James: Don't write to the trustees yet, wait for the engineer.\n",
            "Piet: Moenie skryf aan die trustees nie, wag eers vir die ingenieur.\n",
            "Piet: Die prys is R12,000, maar moenie vir hulle se wat ons betaal het nie.\n",
            "James: Between us we finished twelve units this week.\n",
        ):
            with self.subTest(line=line.strip()):
                self.assertEqual(sen.rule_findings(line), (), line)


class BareIdentifiersAreCheckedNotGuessed(unittest.TestCase):
    """A number is held only when it validates as the thing it claims to be."""

    def test_a_south_african_id_number_is_held(self) -> None:
        transcript = "James: His ID is 8203155009089 for the site register, block C.\n"
        report = assess(transcript)

        held = report.would_hold()
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].category, "bare_identifier")
        self.assertEqual(transcript[held[0].start:held[0].end], "8203155009089")
        self.assertIn("for the site register", masked(transcript, report))

    def test_a_thirteen_digit_invoice_number_is_not_an_identity_number(self) -> None:
        report = assess("James: The invoice number is 4501234567890 for the roof job.\n")

        self.assertEqual(report.findings, ())

    def test_a_bank_account_is_held_and_a_rand_figure_beside_it_is_not(self) -> None:
        transcript = (
            "James: Pay the R48,000 into the account, account number 62731100456, "
            "and send the proof.\n"
        )
        report = assess(transcript)

        text = masked(transcript, report)
        self.assertNotIn("62731100456", text)
        self.assertIn("R48,000", text)

    def test_only_the_identifier_is_cut_not_the_sentence_around_it(self) -> None:
        transcript = "James: His ID is 8203155009089 for the site register.\n"
        report = assess(transcript)

        self.assertIn("for the site register", masked(transcript, report))
        self.assertIn("His ID is", masked(transcript, report))


class TheSpansAreExactBecauseSomethingCutsOnThem(unittest.TestCase):

    def test_findings_are_sorted_and_never_overlap(self) -> None:
        transcript = (
            ORDINARY
            + "James: We raised R1.65m and we'll land at R1.604m.\n"
            + "James: Sipho's disciplinary is on Thursday, it is the second warning.\n"
        )
        report = assess(
            transcript,
            passage("Sipho's disciplinary is on Thursday", "staff_matter"),
            passage("We raised R1.65m and we'll land at R1.604m", "own_margin"),
            passage("The quote to the body corporate is R4,500 for the torch-on repair",
                    "commercial_figure"),
        )

        spans = [(f.start, f.end) for f in report.findings]
        self.assertEqual(spans, sorted(spans))
        for (_, first_end), (second_start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(first_end, second_start)

    def test_a_labelled_passage_inside_a_held_one_does_not_widen_the_hold(self) -> None:
        """Over-holding is the failure being tuned against, so the label is trimmed instead."""
        transcript = "James: We raised R1.65m and we'll land at R1.604m on that job.\n"
        report = assess(
            transcript,
            passage("We raised R1.65m and we'll land at R1.604m", "own_margin"),
            passage("R1.65m", "commercial_figure"),
        )

        held = report.would_hold()
        self.assertEqual(len(held), 1)
        self.assertEqual(transcript[held[0].start:held[0].end],
                         "We raised R1.65m and we'll land at R1.604m")
        self.assertEqual(report.labelled(), ())

    def test_a_span_never_cuts_a_number_in_half(self) -> None:
        """Leaving ',000' behind reads as a complete figure, which is worse than the leak.

        The model is asked for the smallest span that carries the fact, and "smallest" can
        land inside a figure. The span is widened to the whole word or number either side
        before anything is cut on it.
        """
        transcript = "James: We charged R1,650,000 and it will cost us R1,604,000 in the end.\n"
        report = assess(transcript, passage("650,000 and it will cost us R1,604",
                                            "own_margin"))

        finding = report.would_hold()[0]
        self.assertEqual(transcript[finding.start:finding.end],
                         "R1,650,000 and it will cost us R1,604,000")
        text = masked(transcript, report)
        for fragment in ("650", "000", "604", "R1"):
            self.assertNotIn(fragment, text)

    def test_the_same_words_twice_are_held_twice(self) -> None:
        transcript = (
            "James: Sipho's disciplinary is on Thursday.\n"
            "James: I said Sipho's disciplinary is on Thursday.\n"
        )
        report = assess(transcript, passage("Sipho's disciplinary is on Thursday",
                                            "staff_matter"))

        self.assertEqual(len(report.would_hold()), 2)
        self.assertNotIn("disciplinary", masked(transcript, report))


class UnderDoubtItSurfacesRatherThanDecides(unittest.TestCase):
    """Neither withheld silently nor published silently. The notes are the surface."""

    def test_a_quote_that_is_not_in_the_transcript_is_not_held_and_is_said_out_loud(self) -> None:
        report = assess(ORDINARY, passage("the beam was under-designed", "legal_exposure"))

        self.assertEqual(report.findings, ())
        self.assertTrue(any("are not in the transcript" in note for note in report.notes))

    def test_an_uncertain_hold_is_published_with_a_label_and_named(self) -> None:
        transcript = ORDINARY + "James: There was a bit of a to-do with one of the lads.\n"
        report = assess(
            transcript,
            passage("There was a bit of a to-do with one of the lads", "staff_matter",
                    confidence=0.4),
        )

        self.assertEqual(report.would_hold(), ())
        self.assertEqual(len(report.labelled()), 1)
        self.assertTrue(report.labelled()[0].downgraded)
        self.assertTrue(any("rather than held" in note for note in report.notes))

    def test_a_wildly_high_held_fraction_is_reported_and_nothing_is_released(self) -> None:
        transcript = "James: Sipho's disciplinary is on Thursday and it is the second warning.\n"
        report = assess(transcript, passage(
            "Sipho's disciplinary is on Thursday and it is the second warning",
            "staff_matter"))

        self.assertTrue(report.would_hold(), "nothing may be released on account of a threshold")
        self.assertTrue(any("needs looking at" in note for note in report.notes))

    def test_a_category_the_service_does_not_know_is_reported_not_guessed(self) -> None:
        report = assess(ORDINARY, {"quote": "The chromadek arrives Tuesday",
                                   "category": "vibes", "who_is_harmed": "",
                                   "what_it_is": "a thing", "reason": "", "confidence": 1.0})

        self.assertEqual(report.findings, ())
        self.assertTrue(any("not one of the kinds" in note for note in report.notes))

    def test_the_rules_still_run_when_the_model_did_not_answer(self) -> None:
        transcript = "James: Don't write this down, the certificate was backdated.\n"
        report = sen.assess(transcript, None, settings=sen.GateSettings(mode="on"))

        self.assertTrue(report.would_hold())
        self.assertFalse(report.model_answered)
        self.assertTrue(any("did not answer" in note for note in report.notes))


class TheMarkerIsAStatedUnknown(unittest.TestCase):
    """A hold reads as "a rate is held pending James", never as an absence.

    The record's read path is built from six sources and this service's inbox is not one of
    them, so a marker that exists only in the transcript is invisible to the assistant
    answering on site. A confident answer built on a quietly partial record is worse than
    the leak it prevents — which makes the public phrase load-bearing.
    """

    def test_every_finding_carries_a_public_phrase(self) -> None:
        transcript = ORDINARY + "James: We raised R1.65m and we'll land at R1.604m.\n"
        report = assess(transcript, passage("We raised R1.65m and we'll land at R1.604m",
                                            "own_margin"))

        self.assertTrue(all(f.subject for f in report.findings))

    def test_a_phrase_carrying_a_name_or_a_figure_is_replaced(self) -> None:
        transcript = "James: Sipho's disciplinary is on Thursday, the second warning.\n"
        for offered in ("a staff matter about Sipho", "a hold on R1.65m", "a matter for j@kbc.co.za"):
            with self.subTest(offered=offered):
                report = assess(transcript, passage(
                    "Sipho's disciplinary is on Thursday", "staff_matter", what=offered))

                subject = report.findings[0].subject
                self.assertEqual(subject, sen.PUBLIC_SUBJECTS["staff_matter"])
                self.assertNotIn("Sipho", subject)

    def test_a_plain_phrase_from_the_model_is_kept(self) -> None:
        transcript = ORDINARY + "James: We raised R1.65m and we'll land at R1.604m.\n"
        report = assess(transcript, passage("We raised R1.65m and we'll land at R1.604m",
                                            "own_margin", what="our own margin on the job"))

        self.assertEqual(report.findings[0].subject, "our own margin on the job")

    def test_nothing_that_leaves_this_module_carries_the_words(self) -> None:
        transcript = "James: His ID is 8203155009089 and Sipho's hearing is Thursday.\n"
        report = assess(transcript, passage("Sipho's hearing is Thursday", "staff_matter"))

        rendered = json.dumps(report.to_dict())
        self.assertNotIn("8203155009089", rendered)
        self.assertNotIn("Sipho", rendered)
        self.assertNotIn("8203155009089", report.describe())
        self.assertIn("8203155009089", json.dumps(report.to_dict(include_text=True)))


class ItShipsDark(unittest.TestCase):
    """Shadow is the default. It measures; it withholds nothing."""

    def test_shadow_records_what_it_would_have_held_and_holds_nothing(self) -> None:
        transcript = "James: Don't write this down, the certificate was backdated.\n"
        report = assess(transcript, mode="shadow")

        self.assertTrue(report.would_hold())
        self.assertEqual(report.spans_to_mask(), ())
        self.assertEqual(masked(transcript, report), transcript)
        self.assertIn("would have been held", report.describe())

    def test_off_does_not_classify_at_all(self) -> None:
        transcript = "James: Don't write this down, the certificate was backdated.\n"
        report = assess(transcript, mode="off")

        self.assertEqual(report.findings, ())
        self.assertFalse(report.active)

    def test_off_asks_the_model_nothing_extra(self) -> None:
        """Switched off means not in the way, not merely inactive."""
        self.assertNotIn("sensitive_passages", prompts.extraction_schema()["properties"])
        self.assertNotIn("sensitive_passages", prompts.extraction_schema()["required"])
        self.assertEqual(prompts.extraction_system(), prompts.EXTRACTION_SYSTEM)

    def test_on_is_the_only_mode_that_withholds(self) -> None:
        transcript = "James: Don't write this down, the certificate was backdated.\n"

        self.assertTrue(assess(transcript, mode="on").spans_to_mask())

    def test_the_default_mode_is_shadow(self) -> None:
        self.assertEqual(sen.GateSettings().mode, "shadow")
        self.assertFalse(sen.GateSettings().withholds)
        self.assertTrue(sen.GateSettings().classifies)

    def test_an_unknown_mode_is_refused_rather_than_assumed(self) -> None:
        with self.assertRaises(ValueError):
            sen.GateSettings(mode="yes")


class ItIsDeterministic(unittest.TestCase):

    def test_the_same_input_twice_gives_the_same_answer(self) -> None:
        transcript = ORDINARY + "James: We raised R1.65m and we'll land at R1.604m.\n"
        entries = [
            passage("We raised R1.65m and we'll land at R1.604m", "own_margin"),
            passage("The supplier rate is R92 a square", "commercial_figure"),
        ]

        first = assess(transcript, *entries)
        second = assess(transcript, *reversed(entries))

        self.assertEqual(first.findings, second.findings)

    def test_the_normalisation_agrees_with_the_one_quote_verification_uses(self) -> None:
        """Two implementations of one rule is how a quote passes one check and fails the other."""
        for sample in (
            "Ja,   approved — go ahead on “Beach Court”",
            "R1 650 000 – that’s the number",
            "Moenie\tdit  neerskryf nie​",
            "UNIT 12: the flashing…",
        ):
            with self.subTest(sample=sample):
                self.assertEqual(sen._normalise(sample).text, normalise_for_match(sample))


class TheOrderingThatKeepsActionItemsAlive(unittest.TestCase):
    """Mask after verification, and ask the report which quotes are inside a hold.

    ``extract.verify_quote`` discards any item whose quote it cannot find in the transcript.
    Mask the transcript first and every item quoting a masked passage is destroyed — a
    redaction that silently deletes somebody's action items. So verification runs against
    the original text and the overlap is asked about explicitly.
    """

    def test_verification_against_the_masked_text_would_destroy_the_item(self) -> None:
        transcript = (
            "James: Sipho's disciplinary is on Thursday.\n"
            "James: I'll send the trustees a written instruction on Friday.\n"
        )
        quote = "Sipho's disciplinary is on Thursday"
        report = assess(transcript, passage(quote, "staff_matter"))

        self.assertTrue(verify_quote(quote, transcript).ok, "the original must verify")
        self.assertFalse(verify_quote(quote, masked(transcript, report)).ok,
                         "this is the trap: the same quote fails against masked text")

    def test_the_report_says_which_verified_quotes_sit_inside_a_hold(self) -> None:
        transcript = (
            "James: Sipho's disciplinary is on Thursday.\n"
            "James: I'll send the trustees a written instruction on Friday.\n"
        )
        report = assess(transcript, passage("Sipho's disciplinary is on Thursday",
                                            "staff_matter"))

        held_quote = "Sipho's disciplinary is on Thursday"
        free_quote = "I'll send the trustees a written instruction on Friday"
        held_span = sen.locate_spans(held_quote, transcript)[0]
        free_span = sen.locate_spans(free_quote, transcript)[0]

        self.assertIsNotNone(report.covers(held_span.start, held_span.end))
        self.assertIsNone(report.covers(free_span.start, free_span.end))

    def test_an_unverifiable_hold_never_produces_a_span(self) -> None:
        """No fuzzy spans: cutting on an approximate span leaves the wrong words behind."""
        self.assertEqual(sen.locate_spans("a passage nobody said", ORDINARY), ())

    def test_a_near_miss_falls_back_to_the_whole_sentence_never_a_fragment(self) -> None:
        transcript = "James: Sipho's disciplinary hearing is on Thursday morning at ten.\n"
        spans = sen.locate_spans("Sipho disciplinary hearing is on Thursday morning at ten",
                                 transcript)

        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].method, "sentence")
        self.assertIn("Sipho's disciplinary hearing", transcript[spans[0].start:spans[0].end])


class TheStaffMemberReviewsTheirOwn(unittest.TestCase):
    """He sees the count and the site, never the words.

    Not politeness: staff record voluntarily and choose whether to keep a folder at all. If
    they work out that he reads the held text from their calls, the rational answer is to
    stop recording, and then the recordings are gone.
    """

    def test_counts_carry_no_words(self) -> None:
        transcript = "James: Sipho's disciplinary is on Thursday, the second warning.\n"
        report = assess(transcript, passage("Sipho's disciplinary is on Thursday",
                                            "staff_matter"))

        self.assertEqual(report.counts(), {"staff_matter": 1})
        self.assertNotIn("Sipho", json.dumps(report.counts()))

    def test_a_routes_reviewer_is_read_and_never_printed(self) -> None:
        config = Config.from_env(_env(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_SOURCE="src-1", ROUTE_CALLS_OUTPUT="out-1",
            ROUTE_SITE_MEETINGS_SOURCE="src-2", ROUTE_SITE_MEETINGS_OUTPUT="out-2",
            ROUTE_CALLS_REVIEWER="piet@kbc.invalid",
        ))

        self.assertEqual(config.reviewer_for("calls"), "piet@kbc.invalid")
        self.assertEqual(config.reviewer_for("site-meetings"), "",
                         "no reviewer means the service owner")
        self.assertNotIn("piet@kbc.invalid", repr(config))
        self.assertNotIn("piet@kbc.invalid", json.dumps(config.safe_dict(), default=str))
        self.assertNotIn("piet@kbc.invalid", config.scrub("mail to piet@kbc.invalid"))

    def test_a_reviewer_that_is_not_an_address_is_refused_by_name(self) -> None:
        problems = _problems(
            ROUTES="calls", ROUTE_CALLS_SOURCE="src-1", ROUTE_CALLS_OUTPUT="out-1",
            ROUTE_CALLS_REVIEWER="piet",
        )

        self.assertTrue(any("ROUTE_CALLS_REVIEWER" in p for p in problems), problems)

    def test_a_reviewer_survives_the_wizard_rewriting_the_routes(self) -> None:
        """A reviewer that quietly reverted to the owner is the silent change to avoid."""
        values = {
            "ROUTES": "calls",
            "ROUTE_CALLS_SOURCE": "src-1",
            "ROUTE_CALLS_OUTPUT": "out-1",
            "ROUTE_CALLS_REVIEWER": "piet@kbc.invalid",
        }
        rewritten = routes_to_values(dict(values), [
            Route(name="calls", label="Phone calls", source_folder_id="src-1",
                  output_folder_id="out-1"),
        ])

        self.assertEqual(rewritten.get("ROUTE_CALLS_REVIEWER"), "piet@kbc.invalid")

    def test_a_removed_routes_reviewer_is_not_resurrected(self) -> None:
        rewritten = routes_to_values(
            {"ROUTES": "calls,whatsapp", "ROUTE_WHATSAPP_REVIEWER": "piet@kbc.invalid"},
            [Route(name="calls", source_folder_id="src-1", output_folder_id="out-1")],
        )

        self.assertNotIn("ROUTE_WHATSAPP_REVIEWER", rewritten)


BASE_ENV = {
    "GRAPH_TENANT_ID": "tenant-for-tests",
    "GRAPH_CLIENT_ID": "client-for-tests",
    "GRAPH_CLIENT_SECRET": "not-a-real-secret",
    "GRAPH_USER_ID": "drive-owner",
    "TRANSCRIBE_ENGINE": "openai",
    "OPENAI_API_KEY": "not-a-real-engine-key",
    "ANALYSIS_API_KEY": "not-a-real-analysis-key",
    "SMTP_HOST": "smtp.invalid",
    "SMTP_USER": "digest",
    "SMTP_PASSWORD": "not-a-real-password",
    "SMTP_FROM": "digest@invalid",
    "SMTP_TO": "someone@invalid",
    "HEARTBEAT_URL": "https://example.invalid/beat",
    "LEDGER_PATH": "/var/lib/transcriber/ledger.sqlite3",
    "SOURCE_FOLDER_ID": "folder-source",
    "OUTPUT_FOLDER_ID": "folder-output",
}


def _env(**overrides: str) -> dict[str, str]:
    values = dict(BASE_ENV)
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _problems(**overrides: str) -> list[str]:
    try:
        Config.from_env(_env(**overrides))
    except ConfigError as exc:
        return list(exc.problems)
    return []


class TheSettingsAreRefusedAtTheKeyboard(unittest.TestCase):
    """Not at 06:00 on the morning somebody first tries to approve something."""

    def test_the_default_is_shadow_and_a_legacy_env_still_starts(self) -> None:
        config = Config.from_env(_env())

        self.assertEqual(config.gate_mode, "shadow")
        self.assertEqual(config.gate_review_base_url, "")
        self.assertEqual(config.route_reviewers, {})

    def test_an_unknown_mode_is_refused(self) -> None:
        problems = _problems(GATE_MODE="yes")

        self.assertTrue(any("GATE_MODE" in p for p in problems), problems)

    def test_arming_it_without_a_review_page_is_refused(self) -> None:
        problems = _problems(GATE_MODE="on")

        self.assertTrue(any("GATE_REVIEW_BASE_URL" in p for p in problems), problems)
        self.assertTrue(any("nothing would ever be released" in p for p in problems), problems)

    def test_the_review_page_must_be_https(self) -> None:
        problems = _problems(GATE_MODE="on", GATE_REVIEW_BASE_URL="http://review.invalid")

        self.assertTrue(any("https" in p for p in problems), problems)

    def test_armed_with_a_review_page_it_starts(self) -> None:
        # Arming it also requires naming who reviews each enabled route's held passages.
        # Without that, an empty reviewer means "the service owner", and every staff
        # member's own health and personal circumstances land on his review page with the
        # words — decision 6 inverted by a default nobody has to set.
        config = Config.from_env(_env(
            GATE_MODE="on",
            GATE_REVIEW_BASE_URL="https://review.invalid/holds",
            ROUTE_DEFAULT_REVIEWER="reviewer@example.invalid",
        ))

        self.assertEqual(config.gate_mode, "on")
        self.assertTrue(sen.GateSettings.from_config(config).withholds)

    def test_the_held_store_defaults_beside_the_ledger(self) -> None:
        config = Config.from_env(_env())

        self.assertEqual(config.held_store_path,
                         "/var/lib/transcriber/held.sqlite3")

    def test_a_held_store_in_the_work_directory_is_refused(self) -> None:
        """The work directory is cleared on a disk budget. This queue may never empty itself."""
        problems = _problems(WORK_DIR="/var/tmp/transcriber",
                             GATE_HELD_STORE="/var/tmp/transcriber/held.sqlite3")

        self.assertTrue(any("work directory" in p for p in problems), problems)

    def test_a_ledger_inside_the_work_directory_still_starts_and_says_so(self) -> None:
        """It is a configuration people have in the field; it starts, loudly."""
        config = Config.from_env(_env(WORK_DIR="/var/tmp/transcriber",
                                      LEDGER_PATH="/var/tmp/transcriber/ledger.sqlite3"))

        self.assertTrue(any("work directory" in n for n in config.notices), config.notices)

    def test_arming_it_over_a_store_that_gets_swept_is_refused(self) -> None:
        problems = _problems(
            WORK_DIR="/var/tmp/transcriber",
            LEDGER_PATH="/var/tmp/transcriber/ledger.sqlite3",
            GATE_MODE="on",
            GATE_REVIEW_BASE_URL="https://review.invalid/holds",
        )

        self.assertTrue(any("work directory" in p for p in problems), problems)

    def test_every_gate_setting_can_be_seen_and_changed(self) -> None:
        """A setting no group claims is invisible to ``config list`` — and refuses startup."""
        from transcriber import config_cmd

        for name in ("GATE_MODE", "GATE_HELD_STORE", "GATE_REVIEW_BASE_URL"):
            with self.subTest(name=name):
                self.assertIn(name, config_cmd.SETTINGS)
                self.assertNotEqual(config_cmd.SETTINGS[name].group, "other")

    def test_config_set_refuses_arming_it_without_a_review_page(self) -> None:
        from transcriber import config_cmd

        problem = config_cmd.check_value("GATE_MODE", "on", {})
        self.assertIn("GATE_REVIEW_BASE_URL", problem)
        self.assertEqual(
            "", config_cmd.check_value(
                "GATE_MODE", "on", {"GATE_REVIEW_BASE_URL": "https://review.invalid"}))

    def test_config_set_points_a_reviewer_at_the_routes_command(self) -> None:
        from transcriber import config_cmd

        problem = config_cmd.check_value("ROUTE_CALLS_REVIEWER", "piet@kbc.invalid", {})
        self.assertIn("route", problem.lower())


class TheSeamWithTheStoreAndTheMask(unittest.TestCase):
    """One vocabulary across the three modules, asserted rather than hoped for.

    The classifier decides a category, the store validates against a closed list of them,
    and the marker written into the transcript is keyed on the same name. A passage held
    under a name the store refuses is words cut out of the record with nowhere to approve
    them from — so the lists are one list, and this is where that is checked.
    """

    def test_every_held_category_is_one_the_store_accepts(self) -> None:
        from transcriber import withheld

        self.assertEqual(set(sen.HELD_CATEGORIES), set(withheld.CATEGORIES))

    def test_a_finding_becomes_a_held_span_the_store_will_take(self) -> None:
        from transcriber.withheld import HeldSpan

        transcript = "James: Sipho's disciplinary is on Thursday, the second warning.\n"
        report = assess(transcript, passage("Sipho's disciplinary is on Thursday",
                                            "staff_matter"))
        finding = report.would_hold()[0]

        span = HeldSpan(
            item_id="01ITEM",
            start=finding.start,
            end=finding.end,
            text=finding.text,
            category=finding.category,
            reason=finding.reason,
            confidence=finding.confidence,
        )

        self.assertEqual(span.text, transcript[span.start:span.end])
        self.assertEqual(span.category, "staff_matter")

    def test_the_labelled_band_is_never_offered_to_the_store(self) -> None:
        from transcriber.withheld import CATEGORIES

        for category in sen.LABELLED_CATEGORIES:
            with self.subTest(category=category):
                self.assertNotIn(category, CATEGORIES)

    def test_the_modes_are_spelled_one_way_across_the_service(self) -> None:
        from transcriber import config as config_mod
        from transcriber import withheld

        self.assertEqual(tuple(config_mod.GATE_MODES), tuple(sen.GATE_MODES))
        self.assertEqual(tuple(withheld.GATE_MODES), tuple(sen.GATE_MODES))

    def test_the_marker_phrase_and_the_published_phrase_are_the_same_words(self) -> None:
        """A passage described one way in the record and another on the approval page is two."""
        from transcriber.withheld import CATEGORY_PHRASE

        for category, phrase in CATEGORY_PHRASE.items():
            with self.subTest(category=category):
                self.assertEqual(sen.PUBLIC_SUBJECTS[category], phrase)


class TheSchemaAsksForItOnTheOneCallThatIsAlreadyBeingMade(unittest.TestCase):
    """A second model call per recording is a cost and one more thing that can fail."""

    def test_the_field_is_added_only_when_the_gate_is_running(self) -> None:
        with_gate = prompts.extraction_schema(sensitivity=True)

        self.assertIn("sensitive_passages", with_gate["properties"])
        self.assertIn("sensitive_passages", with_gate["required"],
                      "the openai path sends strict:true, which requires every property")

    def test_asking_for_it_does_not_mutate_the_shared_schema(self) -> None:
        prompts.extraction_schema(sensitivity=True)["properties"]["sensitive_passages"] = None

        self.assertNotIn("sensitive_passages", prompts.EXTRACTION_SCHEMA["properties"])

    def test_the_prompt_names_the_languages_and_the_instruction_that_outranks_it(self) -> None:
        note = prompts.SENSITIVITY_NOTE

        for expected in ("Afrikaans", "isiXhosa", "In any language", "PRICES FLOW",
                         "who is harmed"):
            with self.subTest(expected=expected):
                self.assertIn(expected.lower(), note.lower())

    def test_every_category_the_prompt_offers_has_a_disposition(self) -> None:
        self.assertEqual(set(prompts.SENSITIVITY_CATEGORIES), set(sen.DISPOSITIONS))
        self.assertEqual(set(prompts.SENSITIVITY_CATEGORIES), set(sen.PUBLIC_SUBJECTS))

    def test_the_model_does_not_decide_what_is_withheld(self) -> None:
        """The band follows from the category, in code, so a model cannot widen it."""
        self.assertNotIn("disposition", prompts.SENSITIVE_PASSAGE_SCHEMA["properties"])
        self.assertNotIn("held", prompts.SENSITIVE_PASSAGE_SCHEMA["properties"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AStaffMemberReviewsTheirOwn(unittest.TestCase):
    """Decision 6, and the default that inverted it.

    An unset ``ROUTE_<NAME>_REVIEWER`` means "the service owner reviews them", and
    :func:`transcriber.withheld.reviewer_for` then returns the principal for *every*
    category — not only the staff matters that are genuinely his. So the first deployment
    that armed the gate on a staff route put that person's own health, their family
    circumstances and everything they asked not be written down onto James's review page,
    with the words and the stored context. Nobody had to configure anything for that; it is
    what the default did, and nothing warned.

    GATE-DECISIONS.md §6 says why it is not recoverable afterwards: staff record voluntarily
    and choose whether to keep a folder at all. One of them works out that he reads the held
    text from their calls, the rational answer is to stop recording, and then the recordings
    are gone — the original loss failure arriving as a social effect rather than a technical
    one.
    """

    def test_arming_it_with_a_route_that_names_no_reviewer_is_refused(self) -> None:
        problems = _problems(
            GATE_MODE="on", GATE_REVIEW_BASE_URL="https://review.invalid/holds"
        )

        self.assertTrue(
            any("ROUTE_DEFAULT_REVIEWER" in p for p in problems),
            f"startup was allowed with no reviewer configured: {problems}",
        )
        self.assertTrue(
            any("goes to the service owner" in p for p in problems), problems
        )

    def test_the_refusal_names_every_route_that_is_missing_one(self) -> None:
        problems = _problems(
            ROUTES="calls,site-meetings",
            ROUTE_CALLS_SOURCE="S1", ROUTE_CALLS_OUTPUT="O1",
            ROUTE_CALLS_REVIEWER="sipho@example.invalid",
            ROUTE_SITE_MEETINGS_SOURCE="S2", ROUTE_SITE_MEETINGS_OUTPUT="O2",
            SOURCE_FOLDER_ID=None, OUTPUT_FOLDER_ID=None, ARCHIVE_FOLDER_ID=None,
            GATE_MODE="on", GATE_REVIEW_BASE_URL="https://review.invalid/holds",
        )

        joined = " ".join(problems)
        self.assertIn("ROUTE_SITE_MEETINGS_REVIEWER", joined)
        self.assertNotIn(
            "ROUTE_CALLS_REVIEWER", joined,
            "the route that names a reviewer must not be complained about",
        )

    def test_shadow_says_so_out_loud_and_still_starts(self) -> None:
        """Nothing is withheld in shadow, so it is a notice — but he still has to see it."""
        config = Config.from_env(_env(GATE_MODE="shadow"))

        self.assertEqual(config.gate_mode, "shadow")
        self.assertTrue(
            any("ROUTE_DEFAULT_REVIEWER" in n for n in config.notices),
            f"nothing warned: {config.notices}",
        )

    def test_off_says_nothing_because_nothing_is_classified(self) -> None:
        config = Config.from_env(_env(GATE_MODE="off"))

        self.assertFalse(any("REVIEWER" in n for n in config.notices), config.notices)

    def test_with_every_route_named_it_starts_clean(self) -> None:
        config = Config.from_env(_env(
            GATE_MODE="on",
            GATE_REVIEW_BASE_URL="https://review.invalid/holds",
            ROUTE_DEFAULT_REVIEWER="sipho@example.invalid",
        ))

        self.assertEqual(config.route_reviewers, {"default": "sipho@example.invalid"})
        self.assertFalse(any("REVIEWER" in n for n in config.notices), config.notices)

    def test_a_disabled_route_is_not_asked_for_one(self) -> None:
        """A paused route holds nothing, so it needs nobody."""
        problems = _problems(
            ROUTES="calls,whatsapp",
            ROUTE_CALLS_SOURCE="S1", ROUTE_CALLS_OUTPUT="O1",
            ROUTE_CALLS_REVIEWER="sipho@example.invalid",
            ROUTE_WHATSAPP_SOURCE="S2", ROUTE_WHATSAPP_OUTPUT="O2",
            ROUTE_WHATSAPP_ENABLED="false",
            SOURCE_FOLDER_ID=None, OUTPUT_FOLDER_ID=None, ARCHIVE_FOLDER_ID=None,
            GATE_MODE="on", GATE_REVIEW_BASE_URL="https://review.invalid/holds",
        )

        self.assertEqual(problems, [])

    def test_the_routing_rule_itself_is_unchanged(self) -> None:
        """With a reviewer configured, only a staff matter routes to the principal."""
        from transcriber.withheld import reviewer_for

        boss, staff = "james@example.invalid", "sipho@example.invalid"
        self.assertEqual(reviewer_for("personal_circumstances", staff, boss), staff)
        self.assertEqual(reviewer_for("do_not_write_down", staff, boss), staff)
        self.assertEqual(reviewer_for("legal_exposure", staff, boss), staff)
        self.assertEqual(reviewer_for("staff_matter", staff, boss), boss)
