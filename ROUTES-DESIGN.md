# Routes — many folders in, many folders out

## What changes and why

Today the service watches **one** source folder and writes to **one** output folder. James records
in kinds that want handling differently — phone calls, site meetings, WhatsApp voice notes, and
whatever a future recording device drops somewhere else — and he wants to decide, per kind, where it
comes from and where its transcripts land. Several inputs may share one output; each may have its
own. Both must work.

So: a **route** is one watched folder and where its results go. The service runs N routes.

## The Route

```python
@dataclass(frozen=True)
class Route:
    name: str              # slug: lowercase, [a-z0-9-], unique. Used in cursor keys and the ledger.
    label: str             # what a person calls it: "Site meetings"
    source_folder_id: str  # watched
    output_folder_id: str  # transcript, summary and actions land here
    archive_folder_id: str # "" means this route never archives
    engine: str            # "" means the service default
    enabled: bool          # a route can be paused without losing its ledger history
```

## Configuration

```
ROUTES=calls,site-meetings,whatsapp

ROUTE_CALLS_LABEL=Phone calls
ROUTE_CALLS_SOURCE=01ABC…
ROUTE_CALLS_OUTPUT=01DEF…
ROUTE_CALLS_ARCHIVE=01GHI…
ROUTE_CALLS_ENGINE=
ROUTE_CALLS_ENABLED=true
```

The env-var stem is the slug uppercased with `-` → `_`, so `site-meetings` → `ROUTE_SITE_MEETINGS_*`.

**Backwards compatibility is required, not optional.** A `.env` written before this change has
`SOURCE_FOLDER_ID` / `OUTPUT_FOLDER_ID` / `ARCHIVE_FOLDER_ID` and no `ROUTES`. That must keep
working: synthesise exactly one route, `name="default"`, from those three. If `ROUTES` **is** set,
the three legacy variables are ignored — and if both are present, say so plainly at startup rather
than silently preferring one.

## Validation, at startup, all problems reported at once

1. At least one enabled route, or the service will not start.
2. Slugs unique, and matching `^[a-z0-9][a-z0-9-]*$`.
3. Every enabled route needs a source and an output folder.
4. **A route's output folder must not be any route's source folder.** That is a feedback loop: the
   service would read its own transcripts as recordings. This is the one validation that prevents a
   genuinely destructive misconfiguration, so it is checked across the whole set, not per route.
5. A source folder must not appear on two enabled routes — two cursors over one folder means two
   claims on one recording.
6. **Output folders MAY be shared.** He explicitly wants to be able to pool several inputs into one
   output. Do not "helpfully" forbid it.

   **Still true, and now said out loud when the routes are people.** This rule was written when a
   route meant a *kind* of recording — pooling calls and site meetings into one folder is his
   filing, and forbidding it would be the code being clever about it. A route now also carries a
   *person*, which is what `ROUTE_<NAME>_REVIEWER` names. Two routes with **different reviewers**
   sharing one output folder means each person's transcripts, summaries and proposals land where
   the others read them. That is still allowed — a shared team folder is a real thing somebody may
   want — but it is no longer silent: it is a startup notice naming the routes. A route with no
   reviewer is the service owner, who is a person too, so "one named, one not" counts as two.
7. An archive folder must not be any route's source or output folder.

## What each module does differently

**`models.py`** — add `Route`. Add `route: str` to the ledger row.

**`config.py`** — parse `routes: tuple[Route, ...]`. Keep the legacy attributes as read-only
properties derived from the first route, so nothing that reads `config.source_folder_id` breaks
before it is migrated.

**`ledger.py`** — add a `route` TEXT column, defaulting `'default'`, via the existing migration
path. **Cursor keys become per route**: `delta:<route>`, `sweep:<route>`. The load-bearing invariant
is unchanged and must stay structurally true *per route*: the cursor for route R commits in the same
transaction as R's discovered rows, and cannot advance past a file that was not recorded. Add
`counts_for_day(day, route=None)` and `unfinished(route=None)`.

**`worker.py`** — poll each enabled route in turn, each with its own cursor. One route failing must
not stop the others: catch per route, record it, carry on, and report which failed. Work items carry
their route so the pipeline knows where the outputs go.

**`pipeline.py`** — the output folder comes from the item's route, never from a global. Same for the
engine when the route overrides it.

**`sweep.py`** — sweep every enabled route, each from a zero cursor via delta (never `/children`).
Report per route.

**`archive.py`** — per route, using that route's archive folder; skip routes with none. Never move a
recording into another route's folder.

**`digest.py`** — the subject line stays one line about the whole service (`Recordings: all 23 done`).
The body gains a per-route breakdown, so "site meetings all fine, WhatsApp broken" is visible at a
glance rather than hidden inside a total.

**`__main__.py`** — `once`, `sweep`, `archive`, `backfill`, `status` all gain `--route <slug>` to act
on one route; omitted means all. `status` prints a per-route table.

## Two new commands, because "customisable" means changeable without a text editor

**`transcriber routes`** — manage routes without the full wizard.

```
transcriber routes                  # list them, with folder names resolved from the live drive
transcriber routes add              # interactive: name, label, pick source/output/archive from a menu
transcriber routes edit <slug>      # change one
transcriber routes remove <slug>    # takes it out of ROUTES; the ledger history is NEVER deleted
transcriber routes disable <slug>   # stop watching, keep everything
transcriber routes enable <slug>
```

`remove` must say plainly that history is kept and the recordings are untouched.

**`transcriber config`** — read and change single settings, so "change the model" is one line.

```
transcriber config list             # every setting and its current value, secrets masked
transcriber config get ANALYSIS_MODEL_STRONG
transcriber config set ANALYSIS_MODEL_STRONG claude-opus-5
transcriber config set --engine elevenlabs      # friendly alias for the common ones
```

`set` **validates before writing** — an unknown key, an out-of-range number, or a model id that is
not one of the documented ones is refused with the list of valid options, never written and
discovered at 06:00. It rewrites `.env` through the same `write_env_file`, preserving grouping and
the 0600 mode, and never prints a secret.

## The wizard

`transcriber setup` gains a route section replacing the three single-folder questions:

1. Show the routes it already has, if any.
2. Offer: keep these · add one · edit one · remove one · start over.
3. Adding a route walks: slug → label → pick source folder from the live listing → pick output →
   pick archive (or none) → engine (default, or override).
4. After each change, run the validation above and show any problem immediately — especially the
   output-folder-is-a-source-folder loop, in plain words: *"That would make the service read its own
   transcripts as new recordings."*

Default suggestions for a first run, since these are his actual kinds: `calls` (Phone calls),
`site-meetings` (Site meetings), `whatsapp` (WhatsApp voice notes).

## Tests that must exist

- A legacy `.env` with no `ROUTES` still starts, as exactly one route named `default`.
- Two routes, two folders, two cursors: an item discovered on one does not move the other's cursor,
  and the per-route atomicity invariant holds under an injected failure.
- The feedback-loop validation rejects output-folder-is-a-source-folder, and names both routes.
- Two routes sharing one output folder is **accepted**.
- The same source folder on two enabled routes is rejected.
- A route failing to poll does not stop the others, and is named in the report.
- Outputs land in the route's output folder, not the first route's.
- Archive uses the route's archive folder, and a route with none is skipped rather than erroring.
- `config set` refuses an unknown key and an undocumented model id, and does not write.
- `routes remove` leaves every ledger row intact.
- Ledger migration: an existing database without the `route` column upgrades, and its rows read back
  as `default`.
