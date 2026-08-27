# transcriber — architecture and module contracts

The transcriber James's site-memory record already expects. `kbc-site-memory/transcripts/README.md`
opens: *"James talks while he walks a site. **The transcriber writes the text into OneDrive.**"*
This is that transcriber. It does not exist yet; everything downstream of it does.

## The one rule that overrides every design preference

From `kbc-site-memory/transcripts/README.md`:

> Nothing in this path decides anything. A recording is somebody thinking out loud, and a machine
> deciding what they concluded is the failure this record exists to prevent.

And from its `CLAUDE.md`:

> `decided_by: <a person>` is applied; `observed_by: agent` is filed as a question.

**This pipeline is an agent. It therefore decides nothing, ever.** It transcribes, it summarises what
was said, and it surfaces commitments and questions **as proposals carrying a verbatim quote**. It
never writes a status, never closes an item, never concludes that something is done. Any module that
finds itself asserting a fact about the business rather than about the audio is wrong.

Second inviolable rule, from the same `CLAUDE.md`: **never type an email address.** This pipeline
emits none, ever — not into a transcript header, not into a summary, not into a proposal.

## What it does, in order

```
OneDrive /CALLS  ──delta──▶  ledger  ──▶  completeness gate  ──▶  download+verify
      ──▶  audio validity  ──▶  transcribe  ──▶  plausibility  ──▶  AI pass
      ──▶  write .md outputs to OneDrive  ──▶  ledger DONE
                    │
   nightly sweep ───┘   (re-enumerates, re-queues anything unfinished)
   morning digest       (06:00, every day, good days included)
   archive by age       (60 days, outputs confirmed, failures never move)
```

Downstream, unchanged and not ours: `graph_pull.py` (or a Power Automate flow) moves our `.md`
output into `kbc-site-memory/transcripts/processing/`, whose workflow analyses it, files questions
into `inbox/backlog/`, and surfaces them in `09-portfolio/12-ask-james.md`.

## Hard constraints, all verified against live systems

**Output format** — `kbc-site-memory/tools/ingest.py:parse_texty`. A `.md` drop is parsed as:
a header block of `^(from|to|cc|subject|date|sent)\s*:\s*(.*)$` lines, terminated by the first
blank line, then the body.
- Emitting a `From:` header reclassifies the file as an **email**, not a transcript. Never emit one.
- **Only those six keys are recognised.** A non-matching, non-blank line inside the header block is
  silently swallowed — it lands in neither the header nor the body. So we emit `Subject:` and
  `Date:` only, then one blank line, then everything else. All other metadata goes **in the body**.
- Filename convention: `<YYYYMMDD-HHMMSS>-<original stem>.md`. The timestamp prefix is what stops
  two files landing in the same second from colliding.

**Microsoft Graph** — verified during the investigation:
- The change feed (`/delta`) is the trigger. Not webhooks: on OneDrive a dead subscription sends no
  warning, which is the silent-failure mode we exist to remove.
- **`@microsoft.graph.downloadUrl` and the `file.hashes` facet are NOT returned by delta on business
  accounts.** Any completeness check must re-`GET` the item directly. A check built on delta's
  payload either never passes or never fires.
- **A plain folder listing (`/children`) is not guaranteed complete while writes are happening** —
  and his phone writes continuously. The nightly sweep therefore uses delta from a zero cursor, not
  `/children`. (A `/children` cap is exactly how the original measurement got stuck at 200 items.)
- Throttling returns 429 with `Retry-After`. Honour it.

## Module contracts

Every module is stdlib-only. No third-party runtime dependency, deliberately: a service that must
survive years unattended should have nothing that can rot underneath it.

### `config.py`
`Config.from_env()`. Fails loudly at startup listing every missing variable at once, never one at a
time. Secrets are never logged, never included in a digest, never written to the ledger.

### `ledger.py` — the durable state
SQLite, WAL mode. One row per recording, keyed on Graph `item_id` (stable across a move within a
drive). Nothing is ever deleted.

States: `DISCOVERED → CLAIMED → FETCHED → TRANSCRIBED → ANALYSED → DONE`, plus `QUARANTINED`
(loud, needs a person) and `SKIPPED_EMPTY` (verified silence — still a row, never a deletion).

```python
class Ledger:
    def upsert_discovered(self, item: DriveItem) -> bool   # True if newly inserted
    def claim(self, item_id: str, lease_seconds: int) -> bool   # atomic; False if already claimed
    def advance(self, item_id: str, state: str, **fields) -> None
    def quarantine(self, item_id: str, reason: str) -> None
    def record_attempt(self, item_id: str, error: str) -> int   # returns attempt count
    def unfinished(self) -> list[Row]
    def cursor_get(self, name: str) -> str | None
    def cursor_set(self, name: str, value: str) -> None
    def counts_for_day(self, day: str) -> dict
```

**The load-bearing invariant:** `cursor_set` for the delta link is committed **in the same
transaction** as the `upsert_discovered` rows from that page. The cursor cannot advance past a file
that was not recorded. This single property is what makes a lost recording structurally impossible,
and it is the thing the incumbent lacks.

Claiming uses a lease with an expiry, so a worker that dies mid-job releases its claim rather than
stranding the file forever.

### `graph.py` — Microsoft Graph client
Client-credentials token with refresh-before-expiry. `delta(folder_id, cursor)` yielding pages and
the next cursor; `get_item(item_id)` for the full item (the fields delta omits); `download(item_id)`
streaming to a temp path; `upload_small` / `upload_session` for outputs; `move(item_id, parent_id)`;
`list_children` (used only where a complete listing is not required).

Retries: 429/503/504 honour `Retry-After`, then exponential backoff with jitter. A 5xx is retried; a
4xx that is not 429 is not.

### `completeness.py` — is the upload actually finished?
Re-`GET`s the item, never trusting delta. Complete when: `size` unchanged across two reads separated
by the settle interval, **and** no `pendingOperations`/`pendingContentUpdate` facet, **and** a
`file.hashes` value is present. Returns `(ready: bool, reason: str)`.

### `audio.py` — is the audio itself intact?
**The check nobody had.** A recording cut off by a dying battery uploads perfectly, matches its
hash byte-for-byte, transcribes as a fragment, and is filed as a success — invisible forever.

`probe(path) -> AudioInfo(duration_s, container, truncated: bool, reason: str)`.
- Uses `ffprobe` when present.
- **Falls back to a pure-Python container walk, which must work with no ffprobe installed**: for
  MP4/M4A, walk the atom tree and require a complete `moov` and an `mdat` whose declared length does
  not overrun the file; for MP3, scan frame headers and estimate duration. A structurally incomplete
  container is `truncated=True`.
- Truncated ⇒ **quarantine, loudly**. Never transcribe-and-mark-done.

### `engines/` — transcription, pluggable
```python
class Engine(Protocol):
    name: str
    max_bytes: int | None          # None = takes whole files
    def transcribe(self, path: str, hints: Hints) -> Transcript
```
`Transcript` carries `text`, `segments` (start, end, speaker, text), `language`, and
`engine_metadata`. Implement `openai.py` (`gpt-transcribe`), `elevenlabs.py` (Scribe),
`azure.py` (batch, `en-ZA`/`af-ZA`), selected by config. `Hints` carries the construction
vocabulary and the counterparty name parsed from the filename.

**Splitting**, only for engines with a `max_bytes`: split on silence, never fixed offsets, with
overlap. **Mandatory guard — the reassembled pieces must account for the original duration within a
small tolerance, or the file fails loudly.** A splitting bug shortens a transcript without erroring,
which is a silent loss by another name.

### `plausibility.py`
Rejects a transcript that is empty, or whose word count is implausible for the audio duration
(a forty-minute recording yielding eleven words). Rejection ⇒ quarantine, not silent acceptance.
Genuine silence is `SKIPPED_EMPTY` with the evidence recorded — a distinct, visible state.

### `extract.py` — the AI pass
Two tiers: a cheap model classifies and routes everything (so nothing is skipped on a guess); a
stronger model runs only on substantive recordings.

**Safety override:** any mention of a person, site, number, date, amount, approval or promise forces
`substantive`, however short. A twelve-second *"ja, approved, go ahead on Beach Court"* must survive.

**Quote verification is mandatory and mechanical.** Every extracted item carries `quote`, and
`extract.py` verifies that quote appears in the transcript (normalised whitespace/case). An item
whose quote cannot be found **never reaches an output** — it goes to the review list. This is the
guard against a mis-heard word hardening into a task.

Every item carries `observed_by: agent`. **No item ever carries `decided_by`** — this pipeline is
not a person and cannot decide.

### `outputs.py` — writing the markdown
Renders and uploads, **all three or none**, then reads each back to confirm it exists before the
ledger advances. (The incumbent has at least one recording with a summary and no transcript.)
- `<stamp>-<stem>.md` — the transcript. `Subject:`/`Date:` header, blank line, then speaker-labelled
  body. This is the file the record ingests.
- `<stamp>-<stem>-summary.md` — what was discussed.
- `<stamp>-<stem>-actions.md` — commitments and questions, each with its verbatim quote, each
  marked `observed_by: agent`, framed as proposals for a person to confirm.

### `sweep.py`
Nightly. Re-enumerates the source folder **via delta from a zero cursor** (never `/children`), diffs
against the ledger, re-queues anything unfinished. Shares no name-matching logic with the live path —
a file whose name breaks a pattern must not be invisible to both.

### `digest.py`
06:00 email. Subject line carries the whole message: `Recordings: all 23 done` /
`Recordings: 20 done, 3 FAILED` / `⚠ Recordings: nothing arrived yesterday`. Failures first, plain
reason, link. **Sent on good days too** — a report that only arrives when something breaks is
indistinguishable from a system that has died. Pings an external heartbeat URL on success, so
something outside notices when the whole thing goes quiet.

### `archive.py`
His pick: **archive by age.** Monthly, moves recordings older than 60 days whose outputs are
confirmed present. **Failures are never moved, nothing is ever deleted**, and nothing recent is
touched. Originals are never modified on the strength of the system's own belief that it finished.

### `worker.py`
The loop: poll delta every 2 minutes, process claimable work with bounded concurrency, run the
scheduled jobs. CLI: `once`, `run`, `sweep`, `digest`, `archive`, `backfill`, `selftest`, `status`.
`selftest` must prove parsing, state machine, quote verification and the markdown contract
**offline, with no credential and no network** — the same discipline `graph_pull.py --selftest` uses.

## Testing

`tests/` runs under `python3 -m unittest`, offline, no credentials. Cover: the cursor/row atomicity
invariant, lease expiry and re-claim, the truncated-MP4 detector against a deliberately truncated
fixture, quote verification rejecting a fabricated quote, the split-duration guard, and — as a
literal assertion — that rendered output is parsed back as `kind == "transcript"` by a vendored copy
of the real `parse_texty` logic, carries no `From:` line, and contains no email address.
