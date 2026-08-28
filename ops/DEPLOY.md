# Deploying it, in order

Do these in this order. Steps 1 and 2 are where a first deploy actually goes wrong, and
neither of them is something the service can tell you about in advance.

---

## 1. The Azure app registration

**`ops/AZURE.md`, all of it.** The part people skip is the admin-consent button: application
permissions on Microsoft Graph always need a tenant administrator to approve them, and until
the API permissions page shows a green *"Granted for \<tenant\>"* every single call comes back
403 and the service stops saying the credentials were refused.

You come out of it with: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`,
`GRAPH_USER_ID`, and the three folder ids.

---

## 2. The three folders

In the OneDrive that holds the recordings, three **separate, non-nested** folders:

```
/CALLS                  the phone drops recordings here          SOURCE_FOLDER_ID
/CALLS-TRANSCRIPTS      the .md files are written here           OUTPUT_FOLDER_ID
/CALLS-ARCHIVE          originals older than 60 days move here   ARCHIVE_FOLDER_ID
```

Optionally a fourth, `/CALLS-ORPHANS`, for `ORPHAN_FOLDER_ID` — where a half-written set of
outputs is moved aside if an upload fails part-way through.

They must not be the same folder as each other and the output and archive folders must not
sit **inside** the recordings folder — the change feed walks the whole subtree, and the
service would then be watching its own output. It refuses to start if any two ids match, but
it cannot see nesting, so that one is on you.

---

## 3. Fill in the environment

Copy `.env.example` to `.env` and work through it. Every variable is explained in
`README.md`. Three that are easy to get wrong:

- **`LEDGER_PATH`** has no default on purpose — two ledgers is the same as none. Put it
  somewhere backed up.
- **`WORK_DIR`** must not be in a shared `/tmp`. It holds the raw audio of confidential
  conversations while a recording is in flight, and the transcript of any recording that
  failed. The service creates it `0700`, but a world-writable parent is still the wrong
  neighbourhood. Use `/var/cache/transcriber`.
- **`GRAPH_SECRET_EXPIRES_ON`** is optional and you want it. It is the difference between a
  countdown in the morning email and the service stopping dead in two years' time with no
  warning at all.

### The sensitivity gate

It ships **watching and holding nothing**, and that is what you deploy. Leave
`GATE_MODE=shadow`: it reads every recording, records what it *would* have held, and
withholds nothing at all. Arming it is a separate decision, taken in a week or two off the
measurement in the morning email — see **Holding back the things that should not be written
down yet** in `README.md`.

```
GATE_MODE=shadow                  # off | shadow | on. Ship shadow. Nothing is withheld.
GATE_HELD_STORE=                  # empty = beside LEDGER_PATH. MUST NOT be inside WORK_DIR.
GATE_REVIEW_BASE_URL=             # https:// address of the approval page. Needed only for `on`.
ROUTE_<NAME>_REVIEWER=            # per route: who approves what is held from that folder.
```

Three of these will refuse to start rather than fail quietly later:

- **`GATE_HELD_STORE` inside `WORK_DIR`** is refused. `WORK_DIR` is cleared on a disk budget,
  and a held passage is the only copy of those words outside the recording — it would be
  deleted without anybody deciding to.
- **`GATE_MODE=on` with no `GATE_REVIEW_BASE_URL`** is refused. There would be nowhere to
  approve anything and nothing would ever be released.
- **`GATE_MODE=on` with any switched-on route missing `ROUTE_<NAME>_REVIEWER`** is refused.
  Blank means "the service owner reviews them", which sends a staff member's own health and
  personal circumstances to the principal — the one routing the design forbids.

**Back up `GATE_HELD_STORE` with the ledger.** Once the gate is armed, a held passage exists
in exactly two places: that database and the original recording. See **Backing up**.

Three variables were added after `.env.example` was first written and may not be in your
copy — all optional, all defaulting to empty:

```
ORPHAN_FOLDER_ID=                 # where a half-written output set is moved aside
GRAPH_SECRET_EXPIRES_ON=          # YYYY-MM-DD, the date on GRAPH_CLIENT_SECRET
ENGINE_KEY_EXPIRES_ON=            # YYYY-MM-DD, if the transcription key expires
ANALYSIS_KEY_EXPIRES_ON=          # YYYY-MM-DD, if the analysis key expires
```

The file holds every credential the service has. `chmod 640`, owned by root, group-readable
by the service account. Never commit it.

---

## 4. Prove it offline, before it touches anything

```
make check       # every module compiles, then the whole test suite
make selftest    # the service proving its own parsing, state machine, quote checking and
                 # markdown format — with no credential and no network
```

Both must pass. Neither needs the internet, and neither can be affected by a wrong
credential — which is exactly why they are worth running first: if they fail, the problem is
the build, not the configuration.

---

## 5. One real pass, watched

```
transcriber status     # should print an empty ledger and no cursors set
transcriber once       # one poll, one pass
transcriber status     # should now show the recordings it found
```

If `once` finds nothing, look at what it printed. `polled 1 page(s), 14 item(s), 0 new,
14 skipped as our own output` means the output folder is pointed at the recordings folder,
or is nested inside it.

Then check the output folder in OneDrive: three files per recording, one of them without a
leading underscore.

---

## 6. Start it properly

**Systemd**, on a machine:

```
sudo cp ops/transcriber.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transcriber
journalctl -u transcriber -f
```

The unit file's header has the full set-up commands — the service account, the directories,
and the permissions on the environment file.

**Docker**, if you would rather:

```
docker build -f ops/Dockerfile -t transcriber .
docker run -d --name transcriber --restart unless-stopped \
  --env-file .env \
  -v transcriber-ledger:/var/lib/transcriber \
  -v transcriber-work:/var/cache/transcriber \
  transcriber
```

The ledger volume is not optional. It is the only proof a recording ever existed.

---

## 7. The parts outside this service

- **The external monitor** (`HEARTBEAT_URL`). Create the check with a period of one day and
  a grace of a few hours. It is told the morning was fine only when the email went out
  **and** the news in it was good. Point its alert somewhere that reaches a phone.
- **The Power Automate flow** that carries the `.md` files from OneDrive into the record.
  It is described in `kbc-site-memory/transcripts/README.md`. It triggers on a file being
  created in `OUTPUT_FOLDER_ID` and PUTs it to the repository. It holds a fine-grained GitHub
  token scoped to that one repository, *Contents: read and write*, and **that token expires
  too** — when it does, the record stops receiving transcripts while this service keeps
  reporting perfect mornings, because from here everything genuinely did work.
- **The nightly site list**, if you want unnamed recordings given a title. One cron line,
  hung off the entry that already rebuilds the record, so the list is written from a build
  that has just succeeded:

  ```
  30 4 * * *  cd /srv/kbc-site-memory && make build && \
              /srv/transcriber/ops/build-site-book.py . /var/lib/transcriber/sites.json
  ```

  Then `NAMING_SITES_FILE=/var/lib/transcriber/sites.json`. **Nothing breaks if this stops
  running**: a stale list names slightly fewer recordings and a missing one names none,
  which is the behaviour without the feature at all. The morning email prints the list's
  date every day, so a list that quietly stopped being written says so. Note the service
  only ever *reads* it — nothing here writes to the record's repository.
- **An existing pile of recordings.** `transcriber backfill` walks history newest-first in
  its own lane, yielding whenever the live path has work waiting. Start it after the live
  path has been running cleanly for a day.

---

## 8. The first morning

The email arrives at 06:00 local. Read the subject line. If it says
`Recordings: all N done`, you are finished.

If it does not arrive at all, the service is not running — that is what the email's daily
arrival is for, and it is the one failure the service cannot report on its own behalf.

---

## When you change something

```
make check && make selftest
```

Then restart. A restart is safe at any moment: stopping hands back every claim the worker
holds, so the queue is left exactly as it was found rather than with recordings stranded
behind a lease. Nothing is ever processed twice and nothing is lost across a restart — the
ledger records every step *before* the work that follows it.

## Backing up

Two files: whatever `LEDGER_PATH` points at, and — once the gate is armed —
`GATE_HELD_STORE`. (Not the site list: it is rebuilt from the record every night, so a lost
copy costs one night of plainer titles.) Both are SQLite in WAL mode and both are backed up the same way. The held
store matters as much as the ledger and for a sharper reason: a passage waiting for approval
has been cut out of the transcript, so that database and the original recording are the only
two places those words exist.

For each one: copy `<name>.sqlite`, `<name>.sqlite-wal` and `<name>.sqlite-shm` together,
or use `sqlite3 <name>.sqlite ".backup /somewhere/<name>-backup.sqlite"`, which is safe
while the service is running.

The recordings and the transcripts are in OneDrive and in the record's git history. These
two databases are the only things that live solely here.
