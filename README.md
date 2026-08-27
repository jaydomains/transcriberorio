# transcriber

**What it is.** James talks while he walks a site. His phone drops the recording into a
OneDrive folder. This service picks it up, writes out the transcript, and puts three
markdown files back into OneDrive so the site-memory record can file them.

**What it is for.** The old arrangement lost recordings and never said so. A file that
never synced, a battery that died mid-recording, a summary written with no transcript
behind it — all of them looked exactly like a quiet week. Four days of recordings went
missing before anybody noticed. Everything in this service is built around one rule:

> **If something goes wrong, you find out. If it cannot be checked, that counts as
> going wrong.**

There is no path in here that marks a recording finished on incomplete evidence.

**What it will never do.** It does not decide anything. It writes down what was said, it
summarises it, and it puts commitments and questions to you as *proposals* with the exact
words they came from. Nothing it produces closes an item, sets a status, or asserts a fact
about a job. It also never writes down an email address, anywhere, in any spelling.

---

## The three files it writes

For one recording named `Call Carel_260827_143005.m4a` you get:

| File | What it is |
| --- | --- |
| `20260827-143005-Call Carel_260827_143005-a1b2c3d4.md` | **The transcript.** What was actually said. This is the evidence, and it is the only one the record files as a source. |
| `_20260827-143005-…-summary.md` | What was discussed, as a machine read it. |
| `_20260827-143005-…-actions.md` | Commitments and questions, each with its verbatim quote, each framed as a proposal for you to confirm. |

Three things about those names are load-bearing, so please do not "tidy" them:

- **The date-and-time in front** keeps two recordings made in the same second apart, and
  sorts the folder into the order the recordings were made.
- **The short code at the end** is derived from the recording's own OneDrive id. It is what
  stops two different recordings that happen to share a name — his phone re-uploading after
  an interrupted sync produces `... (1).m4a` — from writing over each other's transcript.
- **The underscore in front of the last two** is how the record knows to file only the
  transcript as evidence. Its intake skips any name starting with `_`. Without it, a
  machine's summary gets filed as if somebody had said it.

All three files go up together or none of them do. Each one is read back out of OneDrive
before the recording is marked finished. That is deliberate: the old system has at least one
recording with a summary and no transcript, which is the worst possible remainder — the
conclusion survived and the evidence for it did not.

---

## How to tell it is working

**The morning email, at 06:00, every single day.** Including the days when everything was
fine. A report that only turns up when something breaks is indistinguishable from a service
that has died, so this one always arrives and the subject line carries the whole story:

```
Recordings: all 23 done                       nothing to do
Recordings: 20 done, 3 FAILED                 three need you, they are listed first
⚠ Recordings: nothing arrived yesterday       either you recorded nothing, or something broke
⚠ the OneDrive app secret expires in 9 day(s) — Recordings: all 6 done
```

**If the email does not arrive at all, that is the alarm.** It means the service is not
running. There is also an external monitor (see `HEARTBEAT_URL`) which is told every morning
whether the news was good; if the service goes silent, or the news is bad, the monitor
alerts on its own without depending on anything inside the service being alive.

**To check by hand at any time:**

```
transcriber status
```

That prints how many recordings are known, how many are done, what is waiting for a person
and why, and when the service last successfully did each of its jobs.

---

## What to do when the morning email says something failed

Failures are listed first, in plain English, each with a link straight to the file in
OneDrive. The technical detail is underneath the plain sentence — the sentence is for
reading, the detail is for whoever fixes it.

| What it says | What it means | What to do |
| --- | --- | --- |
| *the recording stops part-way through* | The audio file itself is incomplete — usually the phone ran out of battery or storage while recording. It was **not** transcribed, on purpose: a fragment filed as a whole recording is worse than none. | Nothing to fix technically. Open the recording and see how much you got. Nothing was deleted. |
| *OneDrive refused the connection… credentials have most likely expired* | The app registration's secret has run out. **Nothing will process until it is renewed.** | Renew it — see `ops/AZURE.md`. Then restart the service. |
| *the transcript that came back was far too short for how long the audio runs* | Transcription went wrong, not the recording. | Fix nothing; re-queue it (below). If it happens twice, the engine is having a bad day. |
| *the audio had to be split… the pieces did not add back up* | The recording was too big for the engine in one piece, and the reassembled text does not account for the whole length. This is the guard against a silently shortened transcript. | Re-queue it. If it repeats, tell whoever maintains this — it is a real bug and it is meant to be loud. |
| *the file was no longer there when we went back for it* | It was moved or deleted between being noticed and being fetched. | If you moved it, nothing to do. If you did not, somebody else did. |
| *the transcript and its summary could not be written back to OneDrive* | The output folder was unreachable or full. The recording itself is untouched. | Check the output folder exists and has room, then re-queue. |
| *nothing arrived yesterday* | No recording reached the folder at all. | If you recorded nothing, ignore it. If you did record something, either the phone did not sync or the service cannot see the folder. Both need looking at today. |
| *the service itself reported a fault* | Not about any one recording. Until it is fixed, **nothing** is processed. | Read the detail line. It is almost always a credential. |

**To put a recording back in the queue after fixing whatever was wrong:**

```
transcriber requeue <the recording's id>   # the id is in the email and in `transcriber status`
```

A re-queue takes effect immediately — there is no waiting period.

**"Worth a look" items.** Under the counts you may see a short block that is *not* a
failure. It means one of three things happened, and each is worth an eye rather than an
alarm:

- *proposals were withheld* — the machine produced a note whose quote it could not find in
  the transcript, so the note was not written out. That is the guard against a misheard word
  hardening into a task. The items are kept; `transcriber status --json` shows them.
- *a split recording could not be measured against the clock* — a long recording had to be
  cut up, and the transcription engine returned no timings, so we checked that every piece
  came back with words in it rather than measuring the total. If one of those transcripts
  reads short, that is the one to check.
- *transcripts produced with settings stripped* — the engine refused some of our hints and
  we ran without them. Slightly less accurate than usual.

---

## Running it

Python 3.11 or newer. **There is nothing to install** — every import is from Python's own
standard library, on purpose: a service that has to survive years unattended should have
nothing underneath it that can rot.

```
cd src
export $(grep -v '^#' ../.env | xargs)      # or use the systemd unit, which is better
python3 -m transcriber run
```

### The commands

| Command | What it does |
| --- | --- |
| `transcriber run` | The service. Polls every two minutes, processes what it finds, runs the nightly and morning jobs. This is what the systemd unit runs. |
| `transcriber once` | One poll and one pass, then exit. Good for a first try. |
| `transcriber status` | Counts, failures with reasons, and when each job last worked. |
| `transcriber selftest` | Proves the parsing, the state machine, the quote checking and the markdown format — **offline, with no credential and no network.** Run it after any change and before any deploy. |
| `transcriber sweep` | Runs the nightly re-enumeration now. `--dry-run` to see what it would do. |
| `transcriber digest` | Sends the morning email now. `--dry-run` prints it instead. |
| `transcriber archive` | Runs the monthly archive pass now. `--dry-run` is safe. |
| `transcriber backfill` | Walks the whole folder from the beginning, for a first run against an existing pile of recordings. |
| `transcriber requeue <id>` | Puts one recording back in the queue. |

### Checking a change

```
make check      # compiles every module, then runs the whole test suite
make selftest   # the service proving itself the way it does in production
```

Both run offline with no credentials and no network. If either fails, do not deploy.

---

## Every environment variable

Copy `.env.example` to `.env` and fill it in. **REQUIRED** means the service refuses to
start without it — and it reports *every* missing variable at once, not one per restart.

### Microsoft Graph — reaching OneDrive

| Variable | Meaning |
| --- | --- |
| `GRAPH_TENANT_ID` | **REQUIRED.** The Microsoft 365 tenant. See `ops/AZURE.md`. |
| `GRAPH_CLIENT_ID` | **REQUIRED.** The app registration's id. |
| `GRAPH_CLIENT_SECRET` | **REQUIRED.** The app registration's secret. Never logged, never emailed, never written to the ledger. |
| `GRAPH_USER_ID` | **REQUIRED.** Whose OneDrive holds the recordings. |
| `SOURCE_FOLDER_ID` | **REQUIRED.** The recordings folder (`/CALLS`). |
| `OUTPUT_FOLDER_ID` | **REQUIRED.** Where the three `.md` files are written. |
| `ARCHIVE_FOLDER_ID` | **REQUIRED.** Where recordings older than 60 days are moved. Nothing is ever deleted. |
| `ORPHAN_FOLDER_ID` | Optional. If an upload half-finishes, the stray files are moved here rather than left in the output folder. Leave it empty and the strays are named in the error and replaced on the next attempt. **Never point this at the archive folder** — nothing ever looks in there. |

**These must be four different folders.** The service refuses to start if any two are the
same, because a file it wrote and a file it must read would then be indistinguishable.

### The transcription engine

| Variable | Meaning |
| --- | --- |
| `TRANSCRIBE_ENGINE` | **REQUIRED.** `openai`, `elevenlabs` or `azure`. |
| `OPENAI_API_KEY` / `ELEVENLABS_API_KEY` / `AZURE_SPEECH_KEY` | **REQUIRED** — the one matching your engine. Each engine's key has its own variable so switching engines cannot leave the old key quietly in use. |
| `AZURE_SPEECH_REGION` | **REQUIRED** only for `azure`. |
| `ENGINE_BASE_URL` | Optional. Point the engine somewhere other than its default. |

### The AI pass

A cheap model looks at every recording and decides whether it needs a full reading, so
nothing is skipped on a guess. A stronger model reads the substantive ones. Neither decides
anything: every item they produce carries the words it came from, and an item whose words
cannot be found in the transcript never reaches a file.

| Variable | Meaning |
| --- | --- |
| `ANALYSIS_API_KEY` | **REQUIRED**, unless `OPENAI_API_KEY` is set and is the same key. |
| `ANALYSIS_BASE_URL` | The API endpoint. Default is OpenAI's. |
| `ANALYSIS_MODEL_CHEAP` | The router model. Default `gpt-4o-mini`. |
| `ANALYSIS_MODEL_STRONG` | The reader model. Default `gpt-4o`. |

### The morning email

| Variable | Meaning |
| --- | --- |
| `SMTP_HOST`, `SMTP_PORT` | **REQUIRED / 587.** Port 465 uses implicit TLS; anything else uses STARTTLS. |
| `SMTP_USER`, `SMTP_PASSWORD` | **REQUIRED.** Never logged, never in the email body. |
| `SMTP_FROM` | **REQUIRED.** Who the email is from. |
| `SMTP_TO` | **REQUIRED.** Who gets it. Comma-separated for more than one. |
| `SMTP_STARTTLS` | `true` by default. Turn off only for a relay on the same machine. |
| `HEARTBEAT_URL` | **REQUIRED.** An external monitor (healthchecks.io or similar). It is pinged as *healthy* only on a morning when the email went out **and** the news was good. Treat this URL as a password: anyone holding it can silence the alarm. |
| `DIGEST_HOUR` | Local hour the email is sent. Default 6. |

### Credential expiry — the one that will bite you in a year

| Variable | Meaning |
| --- | --- |
| `GRAPH_SECRET_EXPIRES_ON` | Optional but **strongly recommended.** The date on `GRAPH_CLIENT_SECRET`, as `YYYY-MM-DD`. From 45 days out the morning email counts down to it; from 14 days out it goes in the subject line and the external monitor is told the morning is not fine. |
| `ENGINE_KEY_EXPIRES_ON` | Same, for the transcription key, if it has an expiry. |
| `ANALYSIS_KEY_EXPIRES_ON` | Same, for the analysis key. |

Without these the service runs perfectly for a year and then stops dead on a Tuesday with
no prior notice of any kind. One date turns a cliff into a countdown.

### State, timing and the rest

| Variable | Meaning |
| --- | --- |
| `LEDGER_PATH` | **REQUIRED.** The SQLite file that remembers every recording. No default on purpose: two ledgers is the same as none. Back this up. |
| `WORK_DIR` | Scratch space for downloads. **Put this on a real disk owned by the service account, not in `/tmp`** — it holds the raw audio of confidential conversations. The service creates it readable only by itself. |
| `POLL_INTERVAL_S` | Seconds between checks of the OneDrive change feed. Default 120. |
| `SETTLE_INTERVAL_S` | How long to wait between the two size readings that decide an upload has finished. Default 60. |
| `LEASE_SECONDS` | How long one worker's hold on a recording lasts. Must be longer than `SETTLE_INTERVAL_S`. Default 900. |
| `CONCURRENCY` | Recordings handled at once. Default 2. |
| `MAX_ATTEMPTS` | Failures before a recording is set aside for a person. Default 3. |
| `ARCHIVE_AGE_DAYS` | Age at which a finished recording's original is moved to the archive folder. Default 60. Failures are never moved and nothing is ever deleted. |
| `SWEEP_HOUR` | Local hour of the nightly re-check. Default 1. |
| `ARCHIVE_DAY_OF_MONTH` | Day of the month the archive pass runs. 1–28, so it exists in February. |
| `TIMEZONE` | Default `Africa/Johannesburg`. |
| `LANGUAGES` | Expected languages, best first. Default `en-ZA,af-ZA`. |
| `VOCABULARY`, `VOCABULARY_FILE` | Site and construction terms, names, materials. Hints to make a misheard word less likely — never treated as facts. |
| `HTTP_TIMEOUT_S`, `MAX_RETRIES` | Network patience. Defaults 60 and 5. Rate limiting is always honoured as instructed. |
| `LOG_LEVEL`, `LOG_FORMAT` | `INFO` by default. Set `LOG_FORMAT=json` for machine-readable logs. |

---

## Deploying it

See **`ops/`**:

- `ops/AZURE.md` — the app registration, the exact permissions, and the fact that a tenant
  administrator has to approve them. **Read this first: nothing works until it is done.**
- `ops/Dockerfile` — a container with no build dependencies and no root.
- `ops/transcriber.service` — a systemd unit for running it directly on a machine.
- `ops/DEPLOY.md` — the order to do things in, and how to tell it worked.

---

## What happens to a recording, in order

```
the phone uploads to /CALLS
   ↓  the OneDrive change feed tells us within two minutes
recorded in the ledger  ← the file and its place in the change feed are saved together,
   ↓                       so the feed can never move past a recording that was not written down
is the upload finished?  ← the size has to be the same twice, a minute apart
   ↓
downloaded and checked against OneDrive's own hash
   ↓
is the audio itself whole?  ← a recording cut off by a dying battery uploads perfectly and
   ↓                           transcribes as a plausible fragment. This is the check nobody had.
transcribed  (split into pieces first if it is too big for the engine, and the pieces are
   ↓          proved to account for the whole length)
is the transcript plausible for the length of the audio?
   ↓
read by the AI pass  ← every proposal must quote words that are genuinely in the transcript
   ↓
three files written to OneDrive, all three read back
   ↓
marked done
```

Alongside that: **a nightly sweep at 01:00** re-reads the whole folder from scratch and
compares it against the ledger, so anything the live feed missed is found; **the email at
06:00**; and **a monthly archive pass** that moves originals older than 60 days, but only
ones whose three output files it can still see in OneDrive.

Every step is written to the ledger *before* the work that follows it, so a crash anywhere
leaves a recording that says exactly how far it got, and the next pass carries on from
there. Nothing is done twice and nothing is lost.

---

## Things to leave alone

- **The `Subject:` and `Date:` lines and the blank line after them.** The record's reader
  treats the first block of lines as a header and stops at the first blank line. A third
  line in there reaches neither the header nor the body — it just disappears, with no
  error. And a `From:` line would reclassify a site walk as an email from a sender who does
  not exist.
- **The underscore on the summary and actions filenames.** Removing it files a machine's
  reading as evidence.
- **`decided_by`.** This pipeline is a machine; it cannot decide anything, and there is no
  field in which it could claim it did.
- **The ledger file.** It is the only proof a recording ever existed. Nothing in it is ever
  deleted, including the history of recordings that failed.
