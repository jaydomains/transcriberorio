# The sensitivity gate — decisions taken

Answered by James, 2026-08-28. These are settled; the design serves them rather than revisiting them.

| # | Decision | His answer |
|---|---|---|
| 1 | Where he approves or rejects | **A web page, linked from the 06:00 email.** Opened on a phone, on site. Seconds per item or it will not be used. |
| 2 | What happens to the rest of the recording while an item waits | **Publish immediately with sensitive passages masked**, released in place on approval. The record gets the value straight away, and a withheld passage is *visible as withheld* — never silently absent. |
| 3 | What is held for review | **All four categories.** Money (prices, rates, margins, payment terms) · People (conduct, pay, performance, complaints) · Disputes, liability and blame · Client confidences and third-party personal information. |
| 4 | What happens to an item he has not reviewed for a week | **Nothing is decided for him, ever.** No auto-release, no auto-discard. The morning email escalates: count, then age, then the oldest by name. |

## The tension these four answers create, stated plainly

Decisions 3 and 4 together are the safest possible configuration against a leak and **the most
exposed to the queue dying**. A wide net catches more; nothing ever drains itself; only he can clear
it. If the classifier is loose, he gets a wall of items every morning, stops opening the page, and
the record quietly hollows out — the same failure this whole service was built to cure, wearing a
different coat.

So the burden falls entirely on three things, and the design is not finished until all three hold:

1. **Precision.** Holding a passage must be rare and obviously right. A false positive is not a
   harmless bit of caution here — it is a withdrawal from the only budget that matters, his
   willingness to keep reviewing. Tune for a small, defensible held fraction, and measure it.
2. **Standing rules.** After the tenth "yes, prices with this contractor are fine", it must stop
   asking. His answers are the policy; a gate that never learns is a gate that gets switched off.
3. **Seconds per item.** Enough context to decide without opening the transcript — which recording,
   which site, who was on the call, the passage with a little either side. Grouped, not a flat list.

Decision 4 also means **the queue is now load-bearing state**. A held passage is the only copy of
that information outside the audio, so it inherits the whole service's ledger discipline: never
dropped, never silently expired, and visible in the morning email whatever else is happening.


---

# Settled, 2026-08-28 — after the investigation reported

The first four answers stand except where these supersede them. Both of these were put to him in
plain text and answered **1A, 2A**.

## 5 · Prices are LET THROUGH, labelled. Only KBC's own margin holds.

This **supersedes** the earlier "all four categories" answer on money, and it was his call to make
because he had named price as his own example.

The measurement that changed it, taken over 25,917 lines of his real record: **21% of content lines
mention money and 6.3% carry an actual rand figure.** At 44–60 recordings a working day, holding
prices means **ten to fifteen approvals every day** — and a gate he stops opening does not fail
safely, it silently swallows the record.

The reframe underneath it: **the leak he fears is a price being repeated, not written down.** His
internal record is *meant* to hold his margin — it already does, under a standing header reading
"Internal record. Not client-facing." Removing that is not protection; it is the information loss
this project exists to cure. A price reaching a *client* is a problem to solve on the way **out**.

| | |
|---|---|
| **Held** | A staff matter (warning, hearing, pay, performance, dismissal) · an identifiable person's health or personal circumstances · legal exposure (an admission of KBC's own liability, attorney or insurer strategy, "this must not leave the firm") · bare identifiers (ID number, bank details, home address) · anybody asking that something not be written down, **in any language** · **KBC's own cost-against-charge position in one breath** ("we raised R1.65m and we'll land at R1.604m") |
| **Let through, labelled** | A price quoted to or by a client · a supplier rate · an invoice or fee · a contract sum · a defect · a contractor's poor workmanship · a named person doing their job · a complaint about a company |
| **Not sensitive at all** | Materials, deliveries, programme dates, defects in a building, somebody straightforwardly doing their job. **This should be the answer most of the time.** |

⚠ **The label is only worth having if something reads it.** Today the outbound client-facing check
can spot a rand sign next to a digit and nothing more, so it cannot tell "R4,500 to a contractor"
from a margin. Making that check consume the label is a **separate piece of work and it is the half
that actually prevents the leak.** A label nothing reads is decoration.

## 6 · A staff member reviews their own held passages.

He sees **the count and the site, never the words.** Staff disciplinary matters route to him, because
those are genuinely his to hold.

The reasoning is not privacy for its own sake: staff record **voluntarily** and choose whether to
keep a folder at all. If they work out that he reads the held text from their calls, the rational
response is to stop keeping a folder — and then the recordings are gone entirely. That is the
original loss failure arriving as a social effect rather than a technical one, and it is not fixable
in code afterwards.

## What the investigation corrected in its own designs, and which binds the build

1. **The mask lands on the TRANSCRIPT TEXT**, before any of the three files is generated. Three of
   five design passes put it in the actions file — but the actions file is deliberately named so the
   record never ingests it. **Only the transcript reaches the record.** A redaction in the wrong file
   is not a redaction.
2. **A marker only in the transcript is invisible.** The record's read path is built from six
   sources and the inbox is not one of them. The marker must be phrased as a *stated unknown* so it
   rides the record's existing question harvester onto the site's live page — so a hold reads as
   *"a rate was recorded 24 Aug and is held pending James"* rather than the assistant answering
   *"there is no record of a rate."* **A confident answer built on a quietly partial record is worse
   than the leak it prevents.**
3. **Three mechanisms are REMOVED, not built:** an automatic release deadline, a daily cap that
   commits the overflow unasked, and self-creating rules. As proposed, five paths defaulted to
   committing and none to withholding — so under fatigue the thing that would silently empty was not
   the record but **the gate**, while still presenting itself as a gate.
4. **Phase 1 holds nothing.** It classifies and measures only, because the estimates of how much this
   touches differ across the design passes by a factor of twenty-five. Arming it before that number
   is real is how the queue becomes a wall.
