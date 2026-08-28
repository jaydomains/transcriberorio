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
