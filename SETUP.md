# Setting up the transcriber — step by step

Written for you, not for a developer. Work down it in order. Each step says roughly how long it
takes and whether you can do it yourself.

**Before anything: never send a password, key or secret to me in a chat message.** Chat messages get
stored. Every secret below goes straight into the place it belongs and nowhere else. When a step
says "copy this", it means copy it into your password manager or a note on your own machine. Tell me
"done" — never tell me the value.

---

## Quick map of what you're doing

| # | Step | Time | Who |
|---|---|---|---|
| 1 | Give the service permission to read your OneDrive | 20 min | **may need your IT admin** |
| 2 | Decide your folders — one set per kind of recording | 10 min | you |
| 3 | Get a transcription key | 10 min | you |
| 4 | Get a key for the AI pass | 5 min | you |
| 5 | Set up the morning email | 10 min | you |
| 6 | Set up the "is it still alive" alarm | 5 min | you |
| 7 | Choose where it runs | — | decide with me |
| 8 | Set up the approval page and say who reviews what | 15 min | you |
| 9 | Run the setup wizard | 10 min | you |
| 10 | Prove it works before it touches anything | 5 min | me, with you watching |

**Start with step 1 today.** It is the only one that might need somebody else, and everything else
waits on it. Steps 2–6 you can do in any order while step 1 is being approved.

---

## Step 1 — Give the service permission to read your OneDrive

**Why:** the transcriber has to read `/CALLS` and write transcripts back while you're asleep, with
nobody signed in. That needs its own identity in your Microsoft account, not yours.

**The catch:** the final button on this step can only be pressed by whoever administers your
Microsoft 365 account. If that's you, it's twenty minutes. If it's an outside IT company, send them
this section today — it's the only thing that can hold the whole project up.

### 1a. Create the app registration

1. Go to **https://entra.microsoft.com** and sign in.
2. Left menu → **Applications** → **App registrations** → **New registration**.
3. Name it `KBC Transcriber`.
4. Under *Supported account types*, choose **Accounts in this organizational directory only**.
5. Leave Redirect URI empty. Click **Register**.

You'll land on the app's overview page. Copy these two values — they are not secret, but you need
them:

- **Application (client) ID**
- **Directory (tenant) ID**

### 1b. Create a password for it

1. Left menu → **Certificates & secrets** → **Client secrets** → **New client secret**.
2. Description `transcriber`, expiry **24 months** (the longest offered).
3. Click **Add**.
4. **Copy the "Value" column immediately.** It is shown once and never again. If you miss it, delete
   the secret and make another — no harm done.

> ⚠️ **Put a reminder in your calendar for 23 months from now to renew this.** An expired secret is
> the single most common way a system like this dies quietly. The transcriber will also warn you in
> the morning email as the date approaches, but a calendar entry costs nothing.

### 1c. Give it permission — and this is the bit needing an admin

1. Left menu → **API permissions** → **Add a permission**.
2. Choose **Microsoft Graph** → **Application permissions** (*not* Delegated — delegated needs you
   signed in, which defeats the point).
3. Search for and tick **`Files.ReadWrite.All`**.
4. Click **Add permissions**.
5. Back on the list, click **Grant admin consent for [your organisation]** and confirm.

The permission is only live once that last button has been pressed and the status column reads
**Granted**. If the button is greyed out, you are not an admin — send whoever is these five
sub-steps.

> **You should know what you are approving.** `Files.ReadWrite.All` lets this service read and write
> **every** OneDrive in your organisation, not just yours. Your tenant has at least one other
> person's drive on it. The service is written to touch only the folders named in its settings, but
> the permission itself is broader than that.
>
> There is a tighter option if you'd rather: `Sites.Selected`, which grants access to *nothing* until
> an admin explicitly grants this one app access to one specific drive. It is more secure and about
> fifteen minutes more work. Tell me if you want that instead and I'll write the extra steps — it is
> the more correct choice, and I'd support it if your admin is willing.

**Also worth asking your admin:** your `kbc-site-memory` record already has a near-identical
registration for its `graph_pull.py` job, which needs `Files.Read.All` and `Mail.Read`. It may be
simpler to add write permission to the app that already exists than to create a second one. Ask
whether one already exists before making a new one.

---

## Step 2 — Decide your folders, and what goes where

You don't record one kind of thing, so the transcriber doesn't watch one folder. You give it a
**route** for each kind of recording: *the folder those recordings arrive in, and the folder their
transcripts should go to*. Phone calls, site meetings, WhatsApp voice notes, and whatever the next
recorder drops somewhere else.

For each kind you want handled, decide three things:

1. **Where those recordings arrive.** The folder your phone, or WhatsApp, or the recorder writes into.
2. **Where their transcripts should be written.** Its own folder, or — if you'd rather have them
   together — the same folder another kind uses. Sharing is allowed and nothing collides.
3. **Where the originals should move to once they're 60 days old**, if anywhere. This one is
   optional: leave it out and that kind of recording simply stays where it is, for good. Nothing is
   ever deleted either way, and nothing moves until its transcript has been confirmed present.

A starting point that matches how you already work:

| Kind | Arrives in | Transcripts to | Archived to |
| --- | --- | --- | --- |
| Phone calls | `/CALLS` | `/TRANSCRIBED` | `/CALLS ARCHIVE` |
| Site meetings | `/SITE MEETINGS` | `/TRANSCRIBED` (shared, on purpose) | `/SITE ARCHIVE` |
| WhatsApp voice notes | `/WHATSAPP` | `/TRANSCRIBED` | — none; they stay put |

Three rules, and the wizard checks all three while you pick the folders rather than letting you find
out later:

- **A transcripts folder must never be a recordings folder.** The service would read its own
  transcripts back in as new recordings and transcribe them again, over and over.
- **Two kinds must not arrive in the same folder.** A recording can only belong to one kind.
- **One recordings folder must not sit inside another.** OneDrive reports a folder *and everything
  underneath it*, so a kind watching `/RECORDINGS` would also pick up everything in
  `/RECORDINGS/SITE MEETINGS` and file it as the wrong kind. Keep them side by side, at the top
  level, rather than nested.

Make the folders at the top level of the same OneDrive, and tell me the names you chose. You can add
another kind later in one command — you don't have to decide all of them now.

---

## Step 3 — Get a transcription key

**Hold this one until we've done the comparison.** I want to run twenty of your own recordings —
including a few where you switch between English and Afrikaans mid-sentence, and two long site
meetings — through four engines and let you read the results. That's an afternoon and it's the only
evidence that will actually be about South African construction speech. Every published benchmark
measures English and European audio.

When you've picked, here's where the key comes from:

| Engine | Where | Rough cost at your volume |
|---|---|---|
| **OpenAI** (`gpt-transcribe` — what you originally asked for) | platform.openai.com → API keys | ~$27/month |
| **ElevenLabs** (Scribe) | elevenlabs.io → Developers → API keys | ~$22/month |
| **Azure Speech** | Same Azure account as step 1 → create a Speech resource | ~$18/month |
| **Google** | Google Cloud console | not yet priced |

Price is genuinely not a factor — they're all within about $30 a month of each other at your volume.
Choose on which one gets your Afrikaans and isiXhosa right.

**If you want to start immediately without waiting for the comparison**, get the OpenAI key. It's the
one you asked for, it's the fastest to set up, and switching later is one line in the settings file.

---

## Step 4 — Get a key for the AI pass

This is the part that reads the transcript and pulls out who promised what. Separate from
transcription.

Go to **console.anthropic.com** → **API keys** → **Create key**. Name it `kbc-transcriber`.

**About $80 a month at your volume** — call it R1,500. The range is $45 to $140 depending on
how much gets said on your recordings, and it is the largest running cost in the whole
project, bigger than transcription and the server put together.

> This line used to say "a few dollars a month". That was wrong by about twenty times, and
> it stayed wrong because the figure had no workings and no date next to it. The number
> above is worked out from the actual settings this service ships with, at published rates
> as at 24 June 2026. **Do not trust it either** — from the first morning the email tells
> you what it really cost, yesterday and month to date, priced from a list that says when it
> was last checked.

Nothing caps it. That was your call and it is written down as one: you get the number, and
the service never stops reading on your behalf. If you change your mind it is a small job.

Most of the bill is the model *writing*, not reading — its answers cost five times what the
transcript does. So if it ever needs to come down, the thing to change is how much it is
asked to write, not how many recordings you make.

---

## Step 5 — Set up the morning email

Every morning at 06:00 you get one email. The subject line carries the whole message —
*"Recordings: all 23 done"* or *"Recordings: 20 done, 3 FAILED"*. **It arrives on good days too**,
deliberately: a report that only shows up when something breaks looks identical to a system that has
died, which is how you lost four days without noticing.

You need an address to send *from*. Two options:

**Easiest — use your Microsoft 365 account.** Ask your admin to create a shared mailbox like
`transcriber@khuselabc.co.za`, or use an existing account with an app password. You'll need:
- SMTP server: `smtp.office365.com`
- Port: `587`
- The address and its app password

**Alternative — a mail-sending service.** Something like Resend or Postmark has a free tier and
avoids touching your mail setup. Slightly more setup, one less thing tangled with your business
email.

Then decide **which address receives it**. Your own, presumably.

---

## Step 5b — One email about everybody (only if more than one person is recording)

**Skip this entirely if it is just you.** Nothing below applies, and nothing about it turns
itself on.

Each person runs their own copy of this against their own OneDrive. That is the right shape
— a recording never leaves the drive of whoever made it — and it leaves one gap:

> When somebody's copy stops working, the only person who gets told is the one person whose
> record does not suffer for it. Sipho's transcriber dying is Sipho's email, and Sipho is
> on site.

So each copy drops one small file into a shared folder every morning, and whichever copy
you nominate reads all of them and sends **one** email about everybody. Its subject line
leads with the thing nobody else can see:

```
⚠ Recordings, everyone: NO WORD FROM Sipho
```

Everyone still gets their own email about their own recordings. This is in addition, and
it goes to whoever you name.

### What to do

1. **Make one folder** anywhere in your own OneDrive — call it `/TRANSCRIBER STATUS`. It
   holds a handful of tiny files and nothing else.
2. **On every copy**, set two things: `INSTANCE_NAME` to that person's name as you want it
   to read in the email (`James`, `Sipho`), and `GROUP_FOLDER_ID` to that folder.
   `GROUP_DRIVE_USER_ID` is your own account, since the folder is in your drive.
3. **On one copy only** — whoever should get the group email — also set `GROUP_ADMIN_TO` to
   their address. That single setting is what makes a copy the one that reports. Move it to
   a different copy and the job moves with it; put it on two and you get two group emails,
   which is the visible mistake rather than the silent one.

### What is in those files, and what is not

**Counts, a name, and a timestamp. Nothing else.** Here is a whole one:

```json
{ "instance": "James", "day": "2026-09-02", "arrived": 23, "done": 20,
  "failed": 3, "held_pending": 2, "spend_day_usd": 2.37, "written_at": "..." }
```

No recording names. No words from any recording. Not one line of what anybody said.

That is deliberate and it is the same rule as the approval page: **nobody reads anybody
else's held passages, including you.** A staff member who finds out the boss can read
their held words stops keeping a folder — and then the recordings are gone, which is the
whole loss this project exists to cure. A shared file listing *"Sipho, 3 stopped:
DISCIPLINARY HEARING NOTES.m4a"* would get round that sideways, so the code refuses to
write anything but numbers, in three separate places, and there is a test that reads the
file back and fails if a recording name appears in it.

The group email tells you Sipho has three stopped and hasn't checked in since Monday. What
they are is in Sipho's own email, and that is where it stays.

## Step 6 — Set up the "is it still alive" alarm

This is the one alarm that still works when everything else is dead — including when the whole
service has been switched off, deleted, or its card has expired.

1. Go to **https://healthchecks.io** and sign up. Free.
2. **Add Check**. Name it `KBC Transcriber`.
3. Set **Period** to 1 day and **Grace Time** to 3 hours.
4. Copy the **Ping URL** it gives you.
5. Add your phone number or email under Notifications.

The transcriber pings that URL once every morning, after the 06:00 email has actually gone out.
If a morning goes by without it, healthchecks tells you within three hours instead of you finding
out four days later.

(Period must be a *day*, not an hour. The ping is sent when the morning email goes out, so a
one-hour period would go red every day by mid-morning on a service that is working perfectly — and
an alarm that cries wolf every day is one you switch off, which leaves you with no alarm at all.)

---

## Step 7 — Choose where it runs

Let's decide this together once you know whether you have an Azure subscription. Briefly:

- **Azure Container Apps or a Function** — same Microsoft account as everything else, and it can use
  a managed identity so there's no password to expire. My recommendation if you have or can get an
  Azure subscription.
- **A small always-on server** (about $6/month at Hetzner or DigitalOcean) — simplest to reason
  about, works anywhere, one more thing to patch.
- **Not your office machine.** Load-shedding, an ISP outage, or a Windows update each break it
  independently and none of them will tell you.

---

## Step 8 — Set up the approval page and say who reviews what

**You can skip this today and it will still work.** The service starts up *watching*: it reads
every recording for things that should not be written down yet, notes what it would have held,
and holds nothing. Everything goes into the record exactly as it does now. This step is what you
do before you switch that from watching to actually holding — which is a decision you make in a
week or two, off a real number in the morning email, not today.

Do it now anyway if you can, because part of it needs a web address and that takes a day or two
to arrange.

### 8a. Who reviews whose passages

**A person reviews the passages held from their own recordings.** You see how many are waiting
and which site — never the words. Staff disciplinary matters are the exception and come to you,
whoever recorded them.

That is not politeness. Your people record voluntarily and can stop keeping a folder any time. If
one of them works out that you read the held text from their calls, the sensible thing for them
to do is stop recording, and then those recordings are gone — the exact loss this service was
built to cure, arriving as a people problem instead of a technical one.

So: **for every folder you set up in step 2, write down the email address of whoever records into
it.** For your own folders, that is you.

| Folder | Who records into it | Their address |
|---|---|---|
| Phone calls | | |
| Site meetings | | |
| WhatsApp voice notes | | |
| *(one row per folder)* | | |

The wizard in step 9 asks you for these, one per folder. It also refuses to switch the gate on
later if any folder is missing one — because with it blank, everything held from that folder
comes to you instead, including somebody's health and family circumstances.

### 8b. The approval page

Approving a passage takes seconds on a phone, and the link comes in the morning email. The page
needs somewhere to live:

1. It has to be **`https://`**, not `http://`. The passages travel over it.
2. It does not need to be public — behind your office VPN is better if you have one.
3. It needs a name. Something like `https://transcriber.kbc.co.za/held`.

Tell me the address once you have it and I will point the service at it. Until then, leave it
blank: nothing is being held, so there is nothing to approve.

**About the links.** Each person gets their own link, inside their own copy of the morning email.
**A link is a key** — anybody holding it can answer that person's queue — so it should not be
forwarded, and it expires on its own. A fresh one arrives with each morning's email. If somebody
loses theirs, I can mint a replacement in one command.

### 8c. What you will actually decide, in a week or two

Once it has been watching for a fortnight of ordinary work, the morning email will carry a small
table. The two lines to read are **"read by the model"**, which tells you whether the number
means anything at all, and **"that is, per day"**, which is how many times a day you would have
to tap yes or no. If that is one or two, it is a habit and we switch it on. If it is ten, we fix
what it is holding first — never the queue.

The email will not tell you it is ready unless it genuinely is. If the reading half was not
running, it says so and says not to switch it on.

---

## Step 8b — Let it name the notes you didn't get to name (optional)

**Skip this whole step if you want.** Everything works without it; you just keep naming site
notes yourself, as you do now.

The problem it solves: you record a site walk, it uploads before you get to name it, and it
lands as `Voice 260806_162219.m4a`. That is what it stays called, forever, and nothing in the
record ever says which site it was.

What it does about it: it listens for what you say at the top — *"this is a site walk of
Beach Court, general inspection"* — and titles the note the way you already name them, with
the date and time on the end: `BEACH COURT SITE WALK 060826 1622`. Day first, the way you
write it. On the ones where you forgot to announce it, it goes by which site
the conversation is mostly about instead. **It only ever touches a file still called
`Voice <numbers>_<numbers>`.**
Anything you named yourself is left completely alone, and so is a call, which your phone
already names.

It needs one thing: a list of your sites, so it can only ever propose a site you actually
have. That list comes out of the record's own nightly build.

**1. Point it at the record and build the list.** On the machine that runs the service:

```
/srv/transcriber/ops/build-site-book.py /srv/kbc-site-memory /var/lib/transcriber/sites.json
```

It should print something like `56 sites -> /var/lib/transcriber/sites.json`.

**2. Have it rebuilt each night, right after the record rebuilds.** Add one line to the same
cron entry that already rebuilds the record:

```
30 4 * * *  cd /srv/kbc-site-memory && make build && \
            /srv/transcriber/ops/build-site-book.py . /var/lib/transcriber/sites.json
```

Nothing breaks if this stops running, and nothing is ever named wrongly because of it — an
old list simply describes the sites as they were, so a new job it has never heard of gets no
name. The morning email prints the list's date whenever it has anything to say, and says so
loudly every morning if the file has gone missing altogether.

**3. Tell the service where it is.** The wizard in the next step asks. If you skipped it
there, it takes **two** settings, not one — skipping the wizard question switches the whole
thing off, so pointing at the file alone would leave it off:

```
transcriber config set NAMING_SITES_FILE /var/lib/transcriber/sites.json
transcriber config set NAMING true
```

**It starts by only telling you what it would do.** For the first few weeks the morning
email says *"this one came in without a name and I would have called it BEACH COURT"* — and
the file keeps the name it arrived with. Nothing in the record changes. You read that for a
month, and if the names look right:

```
transcriber config set NAMING_APPLY true
```

I would genuinely leave it reporting for a few weeks first. Nobody has measured how often
this fires or how often it gets it right — the first weeks are that measurement, and the
cost of waiting is a plainer title on a handful of notes.

**Two things worth knowing before you switch it on:**

- It **never renames anything in OneDrive** — not the audio, not the transcript files. The
  name goes into the transcript's subject line, which is what the record reads and what it
  shows you. If you want the audio file to match, the email tells you what to rename it to
  and you do it by hand.
- **A title cannot be corrected afterwards.** The record works out a document's identity
  partly from its subject line, so re-publishing a corrected one files a second copy rather
  than fixing the first. That is why it refuses whenever it is not certain, and why nothing
  is applied until you say so.

**When it is not sure, it says nothing and moves on.** No question, no queue, nothing
waiting for you. The recording is transcribed and filed on time exactly as it is today; it
just keeps its plain name. That is the intended outcome for most of them.

---

## Step 9 — Run the setup wizard

Don't edit any files. Run this:

```
python3 -m transcriber setup
```

It asks for everything above, one question at a time, in plain words — and **checks each answer
against the real service before moving on**. A wrong tenant id costs you thirty seconds here instead
of a day of recordings later.

What it does that saves you the fiddly parts:

- **Signs in to Microsoft and then lists your OneDrive folders**, so you pick each folder from a
  numbered menu instead of hunting for a folder id.
- **Walks you through your routes** — the kinds of recording from step 2 — one at a time: what you
  call it, which folder it arrives in, where its transcripts go, whether it gets archived. It offers
  *Phone calls*, *Site meetings* and *WhatsApp voice notes* to start from, and it tells you straight
  away if two folders would clash rather than letting the service fail at 06:00.
- **Tests each API key** by making one real call, and tells you plainly if a key is wrong.
- **Sends you a test email**, so you know the morning report can reach you before you rely on it.
- **Pings the healthchecks alarm** to prove it's wired up.
- **Hides every password as you type it**, and never prints one back — not even inside an error
  message.
- Writes `.env` readable only by you, and re-running it keeps your previous answers as the defaults,
  so changing one thing later means pressing Enter through the rest.

### Changing your routes afterwards

You never have to re-run the whole wizard to add a kind of recording or move a folder:

```
transcriber routes                    list them, with the real folder names from OneDrive
transcriber routes add                add a kind — it asks the questions and shows you the folders
transcriber routes edit calls         change one
transcriber routes disable whatsapp   stop watching that folder, keep everything else
transcriber routes remove whatsapp    take it out altogether — deletes nothing, ever
```

`disable` is the gentle one and usually the one you want: it stops watching the folder and leaves
everything else exactly as it is, including recordings part-way through. `remove` also deletes
nothing, but any recording of that kind that hasn't finished yet gets set aside for a person instead
of being transcribed — it tells you the number before it asks you to confirm.

The service reads the file once, when it starts, so restart it after a change.

Two flags worth knowing:

```
python3 -m transcriber setup --no-verify     # skip the live checks (before admin consent, or offline)
python3 -m transcriber setup --env /path/.env  # write somewhere other than ./.env
```

**Never paste the contents of `.env` into a chat, an email, or a support ticket.** It holds live
credentials. It's already in `.gitignore` so it can't be committed by accident.

When this is deployed for real, these values go into the host's own secrets store rather than a file
on disk — I'll set that up with you.

---

## Step 10 — Prove it works before it touches anything

Three checks, in this order. I'll run them with you.

```
python3 -m transcriber selftest
```
Proves the code is sane. Needs no keys, no network, touches nothing. If this fails, nothing else
matters.

```
python3 -m transcriber once --dry-run
```
Connects to OneDrive, finds what's there, and **writes nothing**. This is where we confirm the
permissions from step 1 actually work.

```
python3 -m transcriber once --limit 3
```
Processes three recordings for real. We read the three transcripts together before letting it near
the rest.

Only after those three do we turn on the loop.

---

## What happens after that

1. It runs against your existing Fireflies setup **in parallel for two weeks**. Both systems work at
   once, and the morning email shows you both counts side by side. That's how we prove the new one
   is better rather than assuming it.
2. Export your Fireflies history before cancelling anything. Fireflies storage is a lifetime cap that
   doesn't reset, so if you're near it, it may already be deleting old transcripts to make room.
3. Cancel Fireflies once the new count has been clean for two weeks.
4. Then the back catalogue, newest first — about R2,700 for everything, or R500 for the last 60 days.

---

## The short version — what to do today

- [ ] **Step 1** — start the app registration, and if you're not the admin, send section 1 to whoever
      is. This is the only thing that can hold everything else up.
- [ ] **Step 2** — decide which kinds of recording you want handled, and make their folders. Ten
      minutes, and you can add more kinds later in one command.
- [ ] **Step 6** — set up the healthchecks alarm. Five minutes, free, and it's the one that catches
      everything else failing.
- [ ] Tell me whether you have an **Azure subscription**, so we can settle step 7.
- [ ] Tell me **which address** the morning email should go to.
- [ ] **Step 8a** — write down who records into each folder, and their email address. That is
      who approves the things held back from their own calls; you see the count and the site.
- [ ] Tell me whether you can get a **web address** for the approval page. Nothing is held until
      you say so, so this is not urgent — but it takes the longest to arrange.

Once step 1 is approved, the whole of steps 2–10 is one command: `python3 -m transcriber setup`.

Everything else can wait for the engine comparison.
