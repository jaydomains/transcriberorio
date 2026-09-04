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

## Routes — a folder for each kind of recording

You do not record one kind of thing. A phone call is not a site walk, and a WhatsApp voice
note is neither. A **route** is one kind: **the folder its recordings arrive in, and the
folder its transcripts are written to.** The service runs as many routes as you give it,
watching each folder separately.

Each route has:

| | |
| --- | --- |
| **a short name** | `calls`, `site-meetings`, `whatsapp`. Lowercase, no spaces. It is written next to every recording in the ledger and appears in the morning email, so it stays as it is once recordings have used it. |
| **a label** | What you call it in plain words — *Site meetings*. This is what the morning email shows. |
| **the folder it watches** | Where those recordings arrive. |
| **the folder it writes to** | Where the three markdown files go. |
| **an archive folder** | Where the originals move once they are 60 days old and their transcripts are confirmed. You can leave this empty, and then that kind of recording simply stays where it is, for good. |
| **an engine** | Usually blank, meaning "the same as everything else". Set it only if one kind of recording needs a different transcription service. |
| **on or off** | A route can be paused. Its folder stops being watched; nothing else changes. |

**Two routes may write into the same folder.** If you want the calls and the site meetings
to land together, point both at one transcripts folder — that is allowed on purpose, and
the transcripts cannot collide because the service names them.

**Two routes may not watch the same folder**, and the service refuses to start if they do:
a recording can only belong to one kind, and the other route would step past it as though
it had been handled.

**No route's watched folder may sit inside another route's watched folder.** OneDrive
reports a folder *and everything underneath it*, so a route watching `/Recordings` also
sees everything in `/Recordings/Site meetings`. Keep the folders side by side. The wizard
and `transcriber routes` check this for you while you are picking the folders, and if it
ever happens anyway, `transcriber status` and the morning email say which two routes
claimed the same recording, and that recording is not archived until you have sorted it out.

**Never point a route's transcripts folder at any route's recordings folder.** The service
would read its own transcripts back in as new recordings and transcribe them, over and over.
It refuses to start on that one, and names both routes.

### The commands

```
transcriber routes                    list them, with the real folder names from OneDrive
transcriber routes add                add one — it asks the questions and shows you the folders
transcriber routes edit calls         change one
transcriber routes disable whatsapp   stop watching that folder, keep everything else
transcriber routes enable whatsapp    start again
transcriber routes remove whatsapp    take it out of the file altogether
```

`remove` deletes nothing. Not one recording is moved, renamed or deleted, and every line the
route ever wrote in the ledger is kept. What stops is the watching. If some of that route's
recordings have not been transcribed yet, `remove` says so before it asks you to confirm,
because those ones will be set aside for you rather than finished — **use `disable` instead
if you just want it to stop watching for a while.** Renaming a route through
`routes edit` has the same effect on work that is still in flight, and says so too.

Nothing takes effect until the service is restarted: it reads the file once, at startup.

### His three kinds, worked through

*Phone calls into their own folder, transcripts to their own folder, originals archived:*

```
transcriber routes add
  What do you call this kind of recording?   Phone calls
  And a short name for it?                   calls
  Which folder do they arrive in?            Recordings / Calls
  Where should their transcripts go?         Transcripts / Calls
  Where should they move to at 60 days?      Archive / Calls
  Which service should transcribe them?      the same as everything else
```

*Site meetings, kept together with the calls' transcripts in one folder:*

```
transcriber routes add
  What do you call this kind of recording?   Site meetings
  And a short name for it?                   site-meetings
  Which folder do they arrive in?            Recordings / Site meetings
  Where should their transcripts go?         Transcripts / Calls        ← the same folder. Allowed.
  Where should they move to at 60 days?      Archive / Site meetings
```

*WhatsApp voice notes, which you would rather leave where they are for good:*

```
transcriber routes add
  What do you call this kind of recording?   WhatsApp voice notes
  And a short name for it?                   whatsapp
  Which folder do they arrive in?            Recordings / WhatsApp
  Where should their transcripts go?         Transcripts / WhatsApp
  Where should they move to at 60 days?      n — no folder, they stay where they are
```

Then `transcriber status` shows one line per route — how many arrived, how many finished,
how many need you, and when each folder was last read successfully — and the morning email
breaks the day down the same way, so *"site meetings all fine, WhatsApp broken"* is one line
rather than something to work out from a total.

### If you have only ever had one folder

You do not have to do anything. A `.env` written before routes existed keeps working exactly
as it did, as a single route called `default`. The first time you run `transcriber routes
add`, it is written out as a list of routes and tells you it is doing so; nothing about what
is watched, what is written, or what is in the ledger changes.

---

## Holding back the things that should not be written down yet

Some of what gets said on a call should not go into the record until you have said it may.
This part of the service reads every recording, decides whether anything in it is one of
those things, and — once you switch it on — cuts those words out of the transcript and puts
them in a queue for a person to approve.

**Right now it holds nothing.** It ships switched to *watching*, which reads every recording
and writes down what it *would* have held, and then holds nothing at all. Read
**Watching first** below before you change that. That order is not caution for its own sake;
it is the difference between a queue you can clear and a wall you bounce off.

### What is held

A short list, on purpose. These are the things that hurt somebody if they are repeated:

- a staff matter — a warning, a hearing, pay, performance, a dismissal;
- an identifiable person's health or personal circumstances;
- legal exposure — an admission that we are at fault, what our attorney or insurer is
  planning, "this must not leave the firm";
- a bare identifier — an identity number, bank details, a home address;
- **anybody asking that something not be written down**, in any language. If somebody says
  it, that settles it, whatever the subject;
- **our own cost set against our own charge, in one breath** — "we raised R1.65m and we'll
  land at R1.604m".

### What flows through, with a label on it

**Prices flow.** A price quoted to or by a client, a supplier's rate, an invoice, a fee, a
contract sum, a defect, poor workmanship, a named person doing their job, a complaint about
a company — all of it goes into the record as it always did, with a note of what kind of
thing it is attached.

That was decided against the first instinct, on a measurement: 6.3% of the content lines in
your record carry a rand figure. Holding prices would be ten to fifteen approvals a day, and
a gate you stop opening does not fail safely — it quietly swallows the record. The leak you
are worried about is a price being repeated to a client, and that is a problem to solve on
the way *out*, not by keeping it out of your own notes.

### What is not sensitive at all

Materials, deliveries, programme dates, defects in a building, somebody straightforwardly
doing their job. **This is the answer most of the time and it is supposed to be.** Treating
ordinary site talk as sensitive buries the few things that matter under the many that do
not, which is its own kind of failure.

### What a held passage looks like in the record

Nothing waits. All three files are still written, on time, exactly as they always were —
only the *words* of the held passage wait. Where they were, the transcript says something
like:

> `[held 7D6A70]` A staff matter was recorded on 24 Aug 2026 and is held pending review, so
> the words are not written here. What was said in held passage 7D6A70 on 24 Aug 2026, and
> may it be released into the record?

The same sentence is repeated at the top of the transcript, before anything that was said on
the call. That is not tidiness: the record files the first twenty questions it finds in a
transcript, and a long site meeting produces forty, so a marker sitting down in the body
falls off the end and the site's page ends up saying nothing at all. At the top it cannot be
pushed off.

The wording matters. It is written as an open question so the record picks it up and puts it
on the site's live page, where it reads as *"a rate was recorded on 24 August and is being
held"* rather than the assistant telling a client *"there is no record of a rate."* **A
confident answer built on a quietly partial record is worse than the leak it prevents.**

### How review works

Every morning the 06:00 email carries a link. Open it on your phone, read the passage with a
little either side, and tap yes or no. Approving it puts the words back exactly where they
were said, in the transcript that is already in the record. Refusing it leaves the note
where it is, saying a passage was held and refused.

**A staff member reviews their own held passages.** You see how many are waiting and which
site they came from — never the words. The one exception is a staff disciplinary matter,
which comes to you whoever recorded it.

That is not politeness. Your people record voluntarily and can stop keeping a folder any
time. If somebody works out that you read the held text from their calls, the sensible thing
for them to do is stop recording — and then those recordings are gone entirely, which is the
exact loss this whole service was built to cure, arriving as a social problem instead of a
technical one.

Each person gets their own link, in their own copy of the email. **The link is a key** — do
not forward it, because anybody holding it can answer that person's queue. It expires, and a
new one comes with tomorrow's email.

### Nothing is ever decided for you

There is no deadline, no automatic release, no automatic discard, and no daily cap that
quietly commits the overflow. A passage waits until a person answers it, however long that
is. The morning email escalates instead: first the count, then how long the oldest has
waited, then which site it is on, and after a week it goes in the subject line.

The reason it is built that way is that under pressure the thing that must never quietly
empty is the gate. Every design that had a timer in it emptied the gate rather than the
record, while still looking like a gate.

### Watching first, and how to read the number

`GATE_MODE=shadow` is what it ships as, and it means: read every recording, write down what
would have been held, **hold nothing**. Every transcript goes into the record complete. The
morning email gains a section that looks like this:

```
THE GATE IS WATCHING AND HOLDING NOTHING

  NOTHING WAS WITHHELD. ...

    recordings read                 214
    of those, read by the model     211  (99%)
    read by the rules alone         3
    of those, carrying something    9  (4.2%)
    passages it would have held     11
    that is, per day                1.6
    share of the words              0.043%
    days measured                   7
```

Read it in this order:

1. **"read by the model"** first, before anything else. Most of what is held can only be
   seen by something that reads the whole conversation — a staff matter, somebody's health,
   what our attorney said, our own margin. If that number is well below the number above it,
   **the rest of this table means nothing**: a small figure would mean the question was not
   asked, not that there was nothing to find. The email says so itself and refuses to tell
   you it is ready.
2. **"that is, per day"** is the real question: *how many times a day would I have to tap
   yes or no?* One or two is a habit. Ten is a wall, and it is the classifier that needs
   changing, not the queue.
3. **"what it would have held, by kind"**, underneath, tells you *why*. If it is mostly one
   category and that category looks wrong, that is the thing to fix.

Leave it watching for a week or two of ordinary work. When the per-day figure is small, the
categories look right, and the model read effectively all of the recordings, set
`GATE_MODE=on`. Until then it costs you nothing and tells you everything.

### The commands

| Command | What it does |
| --- | --- |
| `transcriber gate --status` | What mode it is in, how many are waiting, and the measurement so far. |
| `transcriber held list` | Counts, sites and ages. Never anybody's words. Add `--as <you>` for your own list. |
| `transcriber held show <ref> --as <you>` | One passage of your own, in full. |
| `transcriber held release <ref> --as <you>` | Approve it. The words go back where they were said. |
| `transcriber held refuse <ref> --as <you>` | Refuse it. The note stays, saying so. |
| `transcriber review --link <person>` | Mints a review link by hand, if somebody's email went astray. |
| `transcriber review serve` | Runs the review page itself. |

---

## Telling the engine what your jobs are called

A transcription engine that has never heard of your jobs guesses. On a real site walk of
his, the firm's **Lonehill** job came back as *"wrong on loan"* and *"the same issue at
lo"* — twice, in one recording. No amount of matching afterwards recovers a name that was
never written down, so this happens *before* the transcription rather than after it.

The site list you already point `NAMING_SITES_FILE` at knows every job's name. Those names
are now handed to the engine as a hint list, longest first, behind anything you typed into
`VOCABULARY` yourself — a hint list is capped by the provider, and a cap cuts the tail, so
your own words go first.

`ENGINE_SITE_NAMES=false` turns it off and the engine gets exactly what it got before.
Without a site list it costs nothing and does nothing.

## Naming a recording that arrived without a name

A site note recorded and uploaded before he got to naming it arrives as the voice recorder's
own default — `Voice 260806_162219.m4a` — and stays that way. Nothing in the record ever says
which site it was.

**Only that exact shape is ever considered.** `^Voice \d{6}_\d{6}$`, anchored and
case-sensitive. Everything else he named himself, and the service never touches a name a
person chose. That includes the ones that look nameless to a machine and are not:
`CJ.m4a`, `Q.m4a`, `JORDS.m4a`, `Morne Interview.m4a` — those are what he calls those
people. The tempting rule, *"no recognisable site in the name, so suggest one"*, is wrong on
every one of them. A device whose default name this does not recognise gets zero
suggestions, which is the right cost: the other direction is renaming something he chose.

**Two outcomes and no third: a name, or no name.** No name is exactly the behaviour before
this existed. Nothing is ever held, delayed, queued or waiting on an answer, and there is
nothing to approve — an unnamed recording is not a problem to be resolved, it is a recording
with a plain title. The morning email says one line about it and moves on.

### What it takes to earn a name

Nine conditions, all mechanical, every one failing towards no name. Two of them carry the
weight and neither is obvious:

**The site must be named in the published body, not in the engine's prose.** Those are
different strings. `Transcript.text` is the engine's continuous prose; the published body is
one line per segment, each prefixed `[MM:SS] Speaker: `, cut on a speaker change or a pause
over 0.9 s. So "Beach Court" said either side of a breath is contiguous in the prose and
**split across two lines in the file**. Deciding from the prose proposes a title the file
does not contain — and in testing that filed a walk at one site under a different one.

**It has to be what the recording is *about*, not merely something said in it.** Two ways to
establish that, and a recording needs only one.

The name it writes is his own shape with the moment on the end — `BEACH COURT SITE WALK
060826 1622`. **Day-first**, because that is what is on his files (`BEACH COURT SITE WALK
270826`, `AMIDAL SITE WALK 260826`), and deliberately the opposite way round from the
`YYMMDD` the phone writes into `Voice 260806_162219`. The two are indistinguishable before
the thirteenth of a month, which is exactly why the parser refuses to *read* either from a
hand-typed name — but writing one is safe, because the moment comes from the recorder's own
clock pinned to the row rather than from any text. The time earns its place on a burst
morning: eighty recordings from one site would otherwise be eighty documents in that site's
log all called `CANTERBURY`.

*Declared* — he says at the top what it is: *"this is a site walk of Beach Court, general
inspection."* That is him stating the answer, and it beats any amount of repetition. It is
also where the activity word comes from, and the only place it may: scanning the whole
recording for one attributes events the recording contradicts — *"the site meeting for
Canterbury moves to next Wednesday"* is a meeting that has not happened.

*Counted* — nothing was announced, so the site has to win the conversation: named at least
twice, and strictly more than any other site the record knows about.

**The record's own site-matching was the judge here and has been demoted to a witness.** It
scores a site by how many *distinct* vocabulary words appear anywhere in a document, once
each, never by how often — which is right for an email and wrong for a recording. Measured
on the real record: a walk at Eagle House with a two-minute call about Ashton Steelworks at
either end binds to **Ashton Steelworks**, because that name carries three matchable words
to Eagle House's one. The first version of this rule deferred to that, titled the walk after
the phone call, and would have *refused* a model answering "Eagle House" — the truth.
Deferring to the record made a misfile look deliberate, which is the worst outcome available:
a wrong filing that a confident title corroborates is one nobody ever checks.

So the record is still asked, and its answer is reported rather than obeyed. When the two
disagree the morning email says so — *"the record will file it under Ashton Steelworks rather
than Eagle House"* — which is the one thing that would make a person look.

The rest: the site is not a placeholder ("the site", "here"); the words look like a name; the
span names exactly one site in the record's own vocabulary — which is what stops `House`,
`North`, `Green` and `Beach` becoming names, since the record discards any term it uses of
more than two sites; and the recording is longer than two minutes.

That last one is not arbitrary. Forty seconds of wind noise comes back from a Whisper-family
engine as *"Canterbury Square. Thank you for watching. Canterbury Square, thank you for
watching"* — which passes every plausibility floor this service has, satisfies "mentioned
twice" and satisfies "mentioned early". **The two conditions that look like evidence are the
hallucination's own signature.** Length is the only cheap thing that separates them.

### What it changes, and what it does not

| | Renamed? |
| --- | --- |
| The audio in OneDrive | **Never.** Not automatically, not at any confidence. |
| The three output filenames | **Never.** They stay a pure function of the recorded moment, the source stem, the copy marker and the item id. |
| The transcript's `Subject:` line | Yes, when `NAMING_APPLY` is on. |
| Its `# ` heading | Yes, the same substitution. |
| Anything at all after publishing | **Never.** |

The output filenames are left alone for a specific reason: a half-failed publish is recovered
by writing the same three names again. A name that could differ between attempts — because
the site list was rebuilt overnight — would leave three files nothing can delete and a second
document in the record. And the title is not corrected later either: the record derives a
document's identity partly from its subject, so re-publishing a corrected one files a second
copy rather than fixing the first. Get it right on the first pass or leave it.

### It ships working it out and writing nothing

`NAMING=true`, `NAMING_APPLY=false`. It decides, records the decision, and prints it in the
morning email; the transcript keeps the name it arrived with. Nobody has measured how often
this fires or how often it is right — there is no corpus in either repository to measure it
against — so the shipped default is the measurement, on the population it actually runs on.

Two booleans rather than one mode word, deliberately. The sensitivity gate has three call
sites that read an unrecognised mode word as `on`; a fourth mode word reaching them by a typo
would arm the thing the typo was meant to disarm. A boolean cannot be misread that way.

### The site list

`ops/build-site-book.py` projects the eight fields the record's own vocabulary reads out of
its nightly `build/spine.json` — 80 KB for 56 sites — and the service reads that file, re-read
only when its modification time changes. No network call, no dependency on a git checkout
being present, and no change to the record's repository, which is read-only to this service.

Every failure ends in fewer names and never in different ones: a missing file, an unreadable
one, one written against a contract this code does not know, a site the record has no folder
for.

A missing or unreadable list is reported in the morning email **every morning until it is
fixed**, and says plainly that nothing is being named. A healthy list with nothing to report
says nothing at all — the section sits under *"WORTH A LOOK (nothing failed)"*, and a heading
that appears every day on every install to report a non-event is a heading that stops being
read, taking the real failures underneath it along with it.

---

## Running it for a team

One person recording a few calls a day needs none of this: the settings below already have
the right values for that, and nothing changes until you turn them up. It matters the day
eight people are each recording into their own folder, because eighty files can arrive
between one check of OneDrive and the next.

**The rule the whole service is built on: when it is busy, it slows down. It never drops
anything.** A recording that cannot be started right now is still in OneDrive, still in the
queue, and still gets transcribed — later, not never. There is no setting anywhere here
that can make it throw work away.

### One folder per person, and what keeps them apart

A route is one watched folder and where its results go, so "a folder each" is exactly what
routes are for: `ROUTE_CAREL_SOURCE` in, `ROUTE_CAREL_OUTPUT` out, and
`ROUTE_CAREL_REVIEWER` naming who reviews anything the sensitivity gate holds back from
that folder. Add a person, add a route.

Three things keep one person's recordings out of another person's folder, and it is worth
knowing which is which because they are not equally strong:

| | What it does |
| --- | --- |
| **A held passage never crosses.** | If a route has no reviewer named, the service **refuses to start** with the gate on. Somebody's health or family circumstances reaching a colleague's review page is the one thing that is not left to configuration. |
| **A recording two routes both claimed is not published at all.** | It waits for a person instead — nothing written, nothing moved, the audio untouched. It used to be published to whichever route saw it first and reported the next morning, which is too late: nothing takes a transcript back out of somebody's folder. |
| **One watched folder inside another is refused at startup.** | OneDrive reports a folder and everything under it, so the outer route would see the inner route's recordings. This is checked against the live drive every time the service starts, not just when the wizard set it up. |

**Two routes may still share one output folder.** That is deliberate and stays — pooling
calls and site meetings into one folder is a filing choice, not a fault. But when the routes
sharing a folder have *different* reviewers, they are carrying different people, and the
service says so at startup. It does not refuse: a shared team folder is a real thing. It
just stops being silent.

### The four things that hold it steady

**How many recordings the machine works on at once — `CONCURRENCY`.** This is about the
computer: disk, memory, and how much audio it can chew at the same time. Two is right for
one person. For eight people on a proper server, try four to eight.

**How hard the transcription service is pushed — `ENGINE_MAX_CONCURRENT` and
`ENGINE_MAX_PER_MINUTE`.** This is a different number about a different thing: your
transcription provider has its own limits, and eight recordings uploaded at once will hit
them however powerful your machine is. Three at a time is the default and is safe with every
provider. `ENGINE_MAX_PER_MINUTE` is off by default because the right number is on your
provider's account page; if the log starts mentioning being throttled, set it to whatever
they allow. When the limit is reached the service simply waits its turn.

**How much disk the scratch space may use — `WORK_DIR_MAX_BYTES` and
`WORK_DIR_KEEP_FINISHED_HOURS`.** Every recording is downloaded before it is transcribed,
and an hour-long call is about 58 MB — plus, if it is too long for the provider to take in
one piece, the pieces it is cut into. Eight of those at once is gigabytes. The default of
4 GiB is comfortable for one person and fine for eight on a normal server; on a small VM
with a 20 GB disk, 4 GiB is still the right answer, and what you must not do is set it to 0,
which means "no limit" and eventually means "full disk". When the scratch space is full the
service starts nothing new and says so in the morning email, and the queue starts moving
again as recordings finish.

The second setting is the clean-up. When a recording fails, its audio is kept so that a
retry does not download it again and so you can listen to what went wrong; after two days it
is cleared away. Leave it alone unless the disk is very small, in which case 12 or 24 hours
is fine. The recordings themselves, in OneDrive, are never touched by any of this.

**Where a folder is in the queue — nothing to set.** The service takes a turn from each
person's folder in rotation, so somebody who uploads forty files in one morning does not
bury the seven colleagues behind them. It happens by itself.

### What to turn up when more people are added

| Situation | Change |
| --- | --- |
| More people, and the queue is growing day after day | `CONCURRENCY` up first (4, then 8), then `ENGINE_MAX_CONCURRENT` to match |
| The log mentions being throttled or refused by the transcription provider | Set `ENGINE_MAX_PER_MINUTE` to the number your provider allows, and leave `ENGINE_MAX_CONCURRENT` at 3 |
| The morning email says the work directory is full | Either the disk is genuinely small — raise `WORK_DIR_MAX_BYTES` — or something failed and left its audio behind, which clears itself after two days |
| One recording is refused **by name** for being too large | Only that one setting can fix it: raise `WORK_DIR_MAX_BYTES` past the size the message names |
| Everything is fine and you added a person | Nothing. Add the route and carry on |

### What the morning email tells you about a backlog

The email counts what is **queued**, and a queue is not a loss. This is the whole point:

```
THE QUEUE
  42 recording(s) queued and being worked through (3 being worked on right now).
  Nothing here is lost or missing: each one has a row in the ledger and will be
  transcribed.

    Phone calls (calls): 39 queued, 3 being worked on now, oldest waiting 40 minutes
    Site meetings (site-meetings): 3 queued, oldest waiting 12 minutes
    nothing queued on: WhatsApp voice notes (whatsapp)

  Longest in the queue: Call Nicholas Burgers_260827_141500.m4a, first seen 40 minutes ago.
```

"Queued" means the service knows about those recordings, has them written down, and has not
transcribed them yet. Nothing in that state can be lost: they are still in OneDrive, and the
ledger already has a row for each. What you are watching for is not the number but the
**age** and the **direction**:

- **A big number that shrinks by the next morning** is a busy morning. Ignore it.
- **The oldest waiting far longer than anything should**, or **the number bigger every
  morning for several days**, means the service genuinely cannot keep up: it is not busy, it
  is behind. The email says so in those words — *"that is longer than anything should sit in
  this queue, so the queue is not moving as fast as recordings are arriving"* — rather than
  leaving you to compare numbers between mornings. That is when you turn `CONCURRENCY` up,
  or ask the provider for a higher limit.
- **A line about the work directory** — "no new recording is being started until some of that
  clears" — means the disk is the thing holding it up, not the transcription provider.
- **Anything marked "needs you"** is the one kind that will not resolve itself.

You can ask at any time, without waiting for the morning:

```
transcriber status        # per folder: known, done, failed, queued, and how long the oldest has waited
```

---

## When somebody asks to be forgotten

Everywhere else, this service never deletes anything. The monthly pass moves old recordings
into an archive folder and that is all it will ever do — `archive.py` says so in its own
first rule and there is no delete call in that file.

This is the one deliberate exception, because "we keep everything forever and there is no
way to take it out" is a fine answer for an archive and not one you can give a client or a
member of staff who asks.

```
transcriber forget --name "Beach Court"                  # shows. Removes nothing.
transcriber forget --name "Beach Court" --really \
    --by "James Janeke" --because "the client asked us to remove this job"
```

**It shows first, always.** Without `--really` it prints what it would remove and stops.
Everything else here is safe to re-run; this is the one thing that is not, so the default is
a look.

**It will not move without a name and a reason.** `--really` on its own is refused. A
recording removed at nobody's request cannot be told apart from one lost to a bug, and in a
year that difference is the only thing that will matter.

**There is no way to spell "forget everything".** One of `--id`, `--name` or `--route` is
required. Dates narrow a selection; they are not one on their own, because `--from 2020`
is every recording there has ever been typed in a way that does not look like it.

### What it removes, and what it leaves

Removed: the original recording, all three published files, and the words of any held
passages.

Left behind, on purpose: **a tombstone.** The ledger keeps a row saying a recording arrived
on this date, finished on that one, and was erased on this one at this person's request. The
name is gone, the output filenames are gone, the metadata is gone. You keep the track of
what happened; you do not keep what it was. The recording's *name* is cleared from its
history too — a recording called `Carel dismissal call.m4a` says most of what the erasure
was meant to remove.

### Three things it cannot reach, and tells you so every time

1. **The OneDrive recycle bin.** Deleting a file puts it there, where it stays for up to 93
   days and an administrator can restore it the whole time. **Until that bin is emptied the
   recording is not gone.** The command says this rather than letting the word "deleted"
   carry more than it earned.
2. **The site record.** It ingested the transcripts and derived its own documents from them,
   and this service may only read it. The command names the files so somebody can go and
   deal with them there.
3. **Anything already sent.** A morning email, a transcript somebody downloaded.


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
| `transcriber try <file>` | **Start here.** One recording on disk, transcribed and read, with two keys and nothing else — no Microsoft credential, no mail server, no ledger. Prints the three files it would have published and what the reading cost. Publishes nothing; writes nothing except with `--out DIR`. See **Trying it on one recording** below. |
| `transcriber run` | The service. Polls every two minutes, processes what it finds, runs the nightly and morning jobs. This is what the systemd unit runs. |
| `transcriber once` | One poll and one pass, then exit. Good for a first try. |
| `transcriber status` | Counts, failures with reasons, and when each job last worked. |
| `transcriber selftest` | Proves the parsing, the state machine, the quote checking and the markdown format — **offline, with no credential and no network.** Run it after any change and before any deploy. |
| `transcriber sweep` | Runs the nightly re-enumeration now. `--dry-run` to see what it would do. |
| `transcriber digest` | Sends the morning email now. `--dry-run` prints it instead. |
| `transcriber archive` | Runs the monthly archive pass now. `--dry-run` is safe. |
| `transcriber backfill` | Walks the whole folder from the beginning, for a first run against an existing pile of recordings. |
| `transcriber requeue <id>` | Puts one recording back in the queue. |
| `transcriber forget` | Removes what is held about somebody, at their request. **Shows first and removes only when told to twice.** See **When somebody asks to be forgotten** below. |
| `transcriber routes` | Lists your routes, adds one, changes one, pauses one. See **Routes** above. |
| `transcriber config` | Reads and changes one setting at a time — `config set ANALYSIS_MODEL_STRONG claude-opus-5` — checking it before it writes. |
| `transcriber gate` | What the sensitivity gate is doing, and the measurement it is building. `--status` for the summary. |
| `transcriber held` | The queue of passages waiting for approval: `list`, `show`, `release`, `refuse`. See **Holding back** above. |
| `transcriber review` | Runs the review page (`serve`), or mints one person a link by hand (`--link <person>`). |

`once`, `sweep`, `archive`, `backfill` and `status` all take `--route <short name>` to act
on one route only. Left off, they act on every route that is switched on.

### Trying it on one recording

The service needs thirteen settings before it will start, and every one of them is about
running unattended for months. None of them are needed to answer the question anybody asks
first, which is *what does it do to one of my recordings?*

```
export OPENAI_API_KEY=...        # or ELEVENLABS_API_KEY / AZURE_SPEECH_KEY
export ANALYSIS_API_KEY=...
export PYTHONPATH=src

python3 -m transcriber try "~/OneDrive/calls/Call Carel_260827_143005.m4a" --out ./try-output
```

Both keys are read from the environment and never taken as arguments: a key typed into a
command is kept in your shell history and visible in the process list.

| Option | What it does |
| --- | --- |
| `--engine` | `openai`, `elevenlabs` or `azure`. Run the same file through two of them and compare — that is the whole point of this command. |
| `--out DIR` | Write the three rendered files there. The only thing this command ever writes. |
| `--full` | Print the whole transcript rather than its first forty lines. |
| `--vocabulary` | Site and person names to hint the engine with, comma-separated. Worth doing: it is the difference between `Blsa` and `Bulsa`. |
| `--languages` | Language tags, best first. Defaults to `en-ZA,af-ZA`. |

**What it does not do.** It publishes nothing, files nothing, emails nothing and records
nothing. It does not touch OneDrive and never builds a Graph client. It does not run the
sensitivity gate, so nothing is held back here that the running service would hold back —
and the report says so on every run rather than leaving silence to be read as "there was
nothing sensitive in it".

**What survives a failure.** By the time the reading runs, the transcription has been paid
for. A refused analysis key, or a filename the output contract will not allow, leaves the
transcript in your hands and says what went wrong — it does not take the run down with it.

The three files it prints are rendered by exactly the same code that publishes them, so
what you are looking at is what would have been published. Their names carry a fixed
`try-run-local-file` tag rather than a real OneDrive item id, which is the honest answer:
these are not the names a published recording would get.

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
| `SOURCE_FOLDER_ID` | The recordings folder, if you are running the old single-folder shape. **REQUIRED unless `ROUTES` is set.** |
| `OUTPUT_FOLDER_ID` | Where the three `.md` files are written, in that same single-folder shape. |
| `ARCHIVE_FOLDER_ID` | Where recordings older than 60 days are moved, in that same shape. Nothing is ever deleted. |
| `ORPHAN_FOLDER_ID` | Optional. If an upload half-finishes, the stray files are moved here rather than left in the output folder. Leave it empty and the strays are named in the error and replaced on the next attempt. **Never point this at the archive folder** — nothing ever looks in there. |

### Naming a recording that arrived without a name

| Variable | Meaning |
| --- | --- |
| `NAMING` | Work out a name and report it in the morning email. Default `true`. |
| `NAMING_APPLY` | Write that name into the transcript's subject line and heading. Default `false` — report only, and nothing in the record changes. |
| `NAMING_SITES_FILE` | The site list written by `ops/build-site-book.py`. Without it nothing is ever named, which is safe. |
| `NAMING_MIN_SECONDS` | Shortest recording that may be named. Default `120`. |
| `NAMING_OPENING_SECONDS` | How much of the start counts as him announcing what the recording is. Default `60`. |

### The routes — one entry per kind of recording

Written by `transcriber setup` and `transcriber routes`; you should rarely need to edit
these by hand. See **Routes** above for what they mean.

| Variable | Meaning |
| --- | --- |
| `ROUTES` | The short names, comma separated: `calls,site-meetings,whatsapp`. Set it and the three single-folder variables above are ignored. |
| `ROUTE_<NAME>_LABEL` | What the morning email calls this kind — `Site meetings`. |
| `ROUTE_<NAME>_SOURCE` | **REQUIRED per route.** The folder its recordings arrive in. |
| `ROUTE_<NAME>_OUTPUT` | **REQUIRED per route.** The folder its transcripts are written to. Two routes may share one. |
| `ROUTE_<NAME>_ARCHIVE` | Where its originals move at 60 days. Empty means this kind stays where it is, for good. |
| `ROUTE_<NAME>_ENGINE` | Empty means the service default. Set it only if this kind needs a different transcription service. |
| `ROUTE_<NAME>_ENABLED` | `false` pauses the route: its folder stops being watched, and nothing else changes. |
| `ROUTE_<NAME>_REVIEWER` | Who reviews the passages held from this folder — normally whoever records into it. **Required on every switched-on route before `GATE_MODE=on` will start.** Leave it unset and everything held from that folder goes to the service owner instead, including a staff member's own health and personal circumstances, which is the one thing the design says must not happen. |

### Holding things back for approval

See **Holding back the things that should not be written down yet** above for what these
actually do. The service ships watching and holding nothing; you change that once the
measurement in the morning email is real.

| Variable | Meaning |
| --- | --- |
| `GATE_MODE` | `off`, `shadow` or `on`. Default **`shadow`**: reads every recording, writes down what it would have held, and holds nothing. `on` actually withholds. `off` does not read for it at all and nothing about the analysis changes from the day before this existed. |
| `GATE_HELD_STORE` | The SQLite file the held words live in. Defaults to beside `LEDGER_PATH`. **It must not be inside `WORK_DIR`** — that gets cleared on a disk budget, and a held passage is the only copy of those words outside the recording. The service refuses to start if you point it there. Back this up with the ledger. |
| `GATE_REVIEW_BASE_URL` | The `https://` address of the review page. **Required before `GATE_MODE=on` will start** — without it there is nowhere to approve anything and nothing would ever be released. Each person's own link is built from this and sent in their copy of the morning email. |

`<NAME>` is the short name in capitals with hyphens as underscores, so `site-meetings`
becomes `ROUTE_SITE_MEETINGS_SOURCE`.

**A folder may only have one job.** No route may write its transcripts into any route's
recordings folder — that is a loop, and the service would transcribe its own output for as
long as nobody noticed — no two switched-on routes may watch the same folder, and no
archive folder may be a recordings or transcripts folder. The service refuses to start on
any of those and names the routes involved. It also refuses if the single-folder variables
are the same folder as each other.

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
| `ANALYSIS_API_KEY` | **REQUIRED.** `OPENAI_API_KEY` stands in for it only when the analysis pass really is calling OpenAI — that is, when `ANALYSIS_PROVIDER=openai` or `ANALYSIS_BASE_URL` names an OpenAI endpoint. On the default it is an Anthropic key and nothing else will do. |
| `ANALYSIS_PROVIDER` | `anthropic` or `openai`. Optional: left unset it is read from `ANALYSIS_BASE_URL`. |
| `ANALYSIS_BASE_URL` | The API endpoint. Default `https://api.anthropic.com`. |
| `ANALYSIS_MODEL_CHEAP` | The router model. Default `claude-haiku-4-5` on Anthropic; on an OpenAI-compatible endpoint there is no default and you name your own. |
| `ANALYSIS_MODEL_STRONG` | The reader model. Default `claude-opus-5` on Anthropic; likewise no default on OpenAI. |

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
| `WORK_DIR_MAX_BYTES` | How much scratch `WORK_DIR` may hold before the service stops starting new recordings. Default `4GiB`. Written as `4GiB`, `500MB` or a plain number of bytes. Nothing is dropped when it is reached: the queue waits and starts moving again as recordings finish. `0` means no limit, which on a small disk eventually means a full one. |
| `WORK_DIR_KEEP_FINISHED_HOURS` | How long the downloaded audio of a finished or failed recording is kept before it is cleared away. Default 48. A failure keeps its audio so a retry is cheap and so you can hear what went wrong; without an end to that, failures fill the disk. The recordings in OneDrive are never touched. |
| `POLL_INTERVAL_S` | Seconds between checks of the OneDrive change feed. Default 120. |
| `SETTLE_INTERVAL_S` | How long to wait between the two size readings that decide an upload has finished. Default 60. |
| `LEASE_SECONDS` | How long one worker's hold on a recording lasts. Must be longer than `SETTLE_INTERVAL_S`. Default 900. |
| `CONCURRENCY` | Recordings handled at once. Default 2. About **the machine** — disk, memory, ffprobe. See **Running it for a team**. |
| `ENGINE_MAX_CONCURRENT` | Transcriptions in flight at the provider at once, across every folder and every thread. Default 3. About **the provider's limits**, which are not the machine's. |
| `ENGINE_MAX_PER_MINUTE` | Transcription requests started per minute, across every folder. Default 0, meaning no per-minute limit. Set it to the number your provider allows if the log mentions being throttled. Reaching it makes the service wait, never skip. |
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
what should it be called?  ← only if it still carries the voice recorder's own name. The
   ↓                          answer is stored before the files are written, so a retry
   ↓                          writes the same subject line rather than a second document
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
