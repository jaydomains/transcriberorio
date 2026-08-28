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
| 2 | Make two folders in OneDrive | 2 min | you |
| 3 | Get a transcription key | 10 min | you |
| 4 | Get a key for the AI pass | 5 min | you |
| 5 | Set up the morning email | 10 min | you |
| 6 | Set up the "is it still alive" alarm | 5 min | you |
| 7 | Choose where it runs | — | decide with me |
| 8 | Run the setup wizard | 10 min | you |
| 9 | Prove it works before it touches anything | 5 min | me, with you watching |

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

## Step 2 — Make two folders in OneDrive

In the same OneDrive as `/CALLS`:

- **`/TRANSCRIBED`** — where the transcriber writes finished transcripts, summaries and action lists.
- **`/CALLS ARCHIVE`** — where recordings older than 60 days move to, once their transcripts are
  confirmed present. Nothing recent is ever touched and nothing is ever deleted.

Make them at the top level, next to `/CALLS`. Tell me the exact names if you choose different ones.

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

Cost is small — a few dollars a month at your volume.

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

## Step 6 — Set up the "is it still alive" alarm

This is the one alarm that still works when everything else is dead — including when the whole
service has been switched off, deleted, or its card has expired.

1. Go to **https://healthchecks.io** and sign up. Free.
2. **Add Check**. Name it `KBC Transcriber`.
3. Set **Period** to 1 hour and **Grace Time** to 2 hours.
4. Copy the **Ping URL** it gives you.
5. Add your phone number or email under Notifications.

The transcriber pings that URL every time it completes a cycle. If it stops pinging, healthchecks
emails you within two hours instead of you finding out four days later.

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

## Step 8 — Run the setup wizard

Don't edit any files. Run this:

```
python3 -m transcriber setup
```

It asks for everything above, one question at a time, in plain words — and **checks each answer
against the real service before moving on**. A wrong tenant id costs you thirty seconds here instead
of a day of recordings later.

What it does that saves you the fiddly parts:

- **Signs in to Microsoft and then lists your OneDrive folders**, so you pick the recordings folder
  from a numbered menu instead of hunting for a folder id.
- **Tests each API key** by making one real call, and tells you plainly if a key is wrong.
- **Sends you a test email**, so you know the morning report can reach you before you rely on it.
- **Pings the healthchecks alarm** to prove it's wired up.
- **Hides every password as you type it**, and never prints one back — not even inside an error
  message.
- Writes `.env` readable only by you, and re-running it keeps your previous answers as the defaults,
  so changing one thing later means pressing Enter through the rest.

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

## Step 9 — Prove it works before it touches anything

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
- [ ] **Step 2** — make the two folders. Two minutes.
- [ ] **Step 6** — set up the healthchecks alarm. Five minutes, free, and it's the one that catches
      everything else failing.
- [ ] Tell me whether you have an **Azure subscription**, so we can settle step 7.
- [ ] Tell me **which address** the morning email should go to.

Once step 1 is approved, the whole of steps 2–9 is one command: `python3 -m transcriber setup`.

Everything else can wait for the engine comparison.
