"""The cost meter: it counts what was used, and it never reports a guess as a fact.

A meter has one failure mode worse than being absent, and it is reporting a number that
looks whole and is not. Two ways that could happen here, and both are tested:

  * a model this deployment uses that the price list has no entry for. Switching models is
    one line in the settings, so this WILL happen. Its tokens must be counted, its money
    must not be invented, and the email must say the total is short.
  * the router call, which runs on every recording including the trivial ones the reader
    never sees. It used to be dropped on the floor. A meter without it undercounts exactly
    the recordings that are most numerous.

And one thing it must never do: stop a recording. It was asked for as a meter and not as a
brake, so a ledger it cannot read costs an email section, never a transcription.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from transcriber import digest as D
from transcriber import prices
from transcriber.extract import Spend, spend_of
from transcriber.ledger import Ledger, State
from transcriber.models import DriveItem

DAY = "2026-09-02"


def _ledger(tmp: str) -> Ledger:
    led = Ledger(os.path.join(tmp, "ledger.sqlite"))
    led.migrate()
    return led


def _record(led: Ledger, item: str, calls: list[Spend], day: str = DAY) -> None:
    led.upsert_discovered(DriveItem(item_id=item, name=f"{item}.m4a", size=1,
                                    created_at=f"{day}T09:00:00Z"))
    led.set_fields(item, done_at=f"{day}T09:06:00Z")
    led.advance(item, State.ANALYSED, meta={"spend": {
        "at": f"{day}T09:05:00Z", "calls": [c.to_dict() for c in calls],
    }})


class AnUnknownModelIsUnpricedAndNeverFree(unittest.TestCase):
    def test_a_model_the_price_list_does_not_know_costs_None_not_zero(self) -> None:
        """Zero is the one wrong answer: it reads as 'that was free'."""
        self.assertIsNone(prices.price_for("claude-opus-9"))
        self.assertIsNone(prices.cost_of(Spend("claude-opus-9", 1_000_000, 1_000_000)))

    def test_a_near_miss_model_name_is_not_guessed_at(self) -> None:
        """Assuming opus-9 prices like opus-5 is how a price list starts lying."""
        self.assertIsNotNone(prices.price_for("claude-opus-5"))
        for near in ("claude-opus-5-20260101", "claude-opus", "CLAUDE-OPUS-5 "):
            self.assertIsNone(prices.price_for(near), near)

    def test_the_total_and_the_unpriced_list_come_back_together(self) -> None:
        known = Spend("claude-haiku-4-5", 1_000_000, 0)
        unknown = Spend("some-new-model", 1_000_000, 1_000_000)
        total, missing = prices.cost_of_all([known, unknown])
        self.assertAlmostEqual(total, 1.00, places=6)
        self.assertEqual(missing, ("some-new-model",))

    def test_the_email_says_the_total_is_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _record(led, "a", [Spend("claude-haiku-4-5", 2000, 200),
                                   Spend("some-new-model", 5000, 4000)])
                report = D.spend_report(None, led, day=DAY)

        self.assertEqual(report.unpriced, ("some-new-model",))
        body = "\n".join(D._spend_section(report))
        self.assertIn("UNDERCOUNT", body)
        self.assertIn("some-new-model", body)
        # Its tokens are still counted, so the token lines stay truthful.
        self.assertEqual(report.month_input, 7000)
        self.assertEqual(report.month_output, 4200)


class TheRouterCallIsCounted(unittest.TestCase):
    def test_a_trivial_recording_still_shows_a_cost(self) -> None:
        """It cost a router call. One entry, not none — this used to be discarded."""
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _record(led, "trivial", [Spend("claude-haiku-4-5", 2100, 180)])
                report = D.spend_report(None, led, day=DAY)
        self.assertEqual(report.month_recordings, 1)
        self.assertEqual(report.month_calls, 1)
        self.assertGreater(report.month_usd, 0.0)

    def test_both_calls_are_counted_on_a_recording_read_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _record(led, "full", [Spend("claude-haiku-4-5", 2450, 200),
                                      Spend("claude-opus-5", 1800, 3000, 3250)])
                report = D.spend_report(None, led, day=DAY)
        self.assertEqual(report.month_calls, 2)
        self.assertEqual(report.month_cache_read, 3250)


class BothProvidersReportTheSameSpend(unittest.TestCase):
    """OpenAI's prompt_tokens INCLUDES its cached share; Anthropic's does not."""

    def test_the_cached_share_is_not_counted_twice(self) -> None:
        openai = spend_of("m", {"prompt_tokens": 5000, "completion_tokens": 900,
                                "prompt_tokens_details": {"cached_tokens": 1200}})
        self.assertEqual(openai.input_tokens + openai.cache_read_tokens, 5000)
        self.assertEqual(openai.cache_read_tokens, 1200)

    def test_anthropic_counts_are_taken_as_given(self) -> None:
        got = spend_of("m", {"input_tokens": 3000, "output_tokens": 2500,
                             "cache_read_input_tokens": 3250,
                             "cache_creation_input_tokens": 40})
        self.assertEqual((got.input_tokens, got.output_tokens), (3000, 2500))
        self.assertEqual((got.cache_read_tokens, got.cache_write_tokens), (3250, 40))

    def test_a_usage_block_full_of_rubbish_reads_as_zero_rather_than_raising(self) -> None:
        """Telemetry must never be the reason a recording fails."""
        for junk in ({"input_tokens": "lots"}, {"input_tokens": None}, {}, None,
                     {"input_tokens": -5}, {"prompt_tokens_details": "nope",
                                            "prompt_tokens": 10}):
            got = spend_of("m", junk)
            self.assertGreaterEqual(got.input_tokens, 0)
            self.assertGreaterEqual(got.output_tokens, 0)


class ItIsAMeterAndNotABrake(unittest.TestCase):
    def test_a_ledger_it_cannot_read_costs_a_section_and_nothing_else(self) -> None:
        class Broken:
            def spend_since(self, since, route=None):
                raise sqlite_error()

        def sqlite_error() -> Exception:
            return RuntimeError("the ledger is locked")

        report = D.spend_report(None, Broken(), day=DAY)
        self.assertEqual(report.month_calls, 0)
        self.assertEqual(report.day, DAY)
        # And the section is simply not printed, rather than printing zeros that would read
        # as "nothing was spent yesterday".
        self.assertFalse(report.month_calls or report.unpriced)

    def test_nothing_in_the_report_can_stop_anything(self) -> None:
        """There is no ceiling, by decision. If one is ever added it will not be by
        accident, and this test is what makes that visible."""
        for name in dir(D.SpendReport):
            self.assertNotIn(name.lower(), {"ceiling", "cap", "limit", "halt", "stop"})


class TheFiguresCarryTheirProvenance(unittest.TestCase):
    def test_every_report_names_the_day_the_prices_were_read(self) -> None:
        report = D.spend_report(None, _BrokenButEmpty(), day=DAY)
        self.assertEqual(report.priced_on, prices.CHECKED_ON)

    def test_the_section_prints_that_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _record(led, "a", [Spend("claude-opus-5", 1000, 1000)])
                report = D.spend_report(None, led, day=DAY)
        self.assertIn(prices.CHECKED_ON, "\n".join(D._spend_section(report)))

    def test_last_months_spend_is_not_counted_in_this_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with _ledger(tmp) as led:
                _record(led, "aug", [Spend("claude-opus-5", 5000, 5000)], day="2026-08-31")
                _record(led, "sep", [Spend("claude-opus-5", 1000, 1000)], day=DAY)
                report = D.spend_report(None, led, day=DAY)
        self.assertEqual(report.month_recordings, 1)
        self.assertEqual(report.month_input, 1000)


class _BrokenButEmpty:
    def spend_since(self, since, route=None):
        return []


if __name__ == "__main__":
    unittest.main()
