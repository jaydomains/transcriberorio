"""``transcriber routes`` — manage the watched folders without the full wizard.

    transcriber routes                    list them, with folder names from the live drive
    transcriber routes add                pick a slug, a label and the folders, from a menu
    transcriber routes edit <slug>        change one
    transcriber routes remove <slug>      stop watching it; the ledger history is kept
    transcriber routes disable <slug>     pause it, keeping everything
    transcriber routes enable <slug>

A route is one watched folder and where its results go, and the service runs N of them.
They live in ``.env`` as ``ROUTES`` plus six variables per route, which is a perfectly good
way to store them and a terrible way to edit them: the folders are opaque driveItem ids,
the variable names contain the route's own name, and two of the rules are about how the
routes relate to *each other*. So this command exists.

What it will not do:

* **Write a set of routes the service cannot run.** Every change is checked with
  :func:`~transcriber.setup_wizard.route_problems` and then by loading the whole file back
  through the real :class:`~transcriber.config.Config`. If either objects, nothing is
  written and the objection is printed in plain words — above all the feedback loop, where
  a route's transcripts land in a folder something watches and the service reads its own
  output back in as recordings.
* **Delete anything.** ``remove`` takes a route out of ``ROUTES``. The recordings, the
  transcripts and the folders are untouched, and every ledger row that route ever wrote
  stays exactly where it is — which is why ``status`` and the morning email can still
  account for them afterwards.

Listing resolves folder ids to the folder's real name on the drive, because a page of
``01BSDF…`` helps nobody. When the drive cannot be reached it falls back to the id and says
so once, rather than failing: reading your own configuration must not require a network.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

from . import config as config_mod
from .config import ENGINES
from .config_cmd import comments_would_be_lost
from .models import Route, is_route_name
from .setup_wizard import (
    FEEDBACK_LOOP,
    SUGGESTED_ROUTES,
    _Ctx,
    _Style,
    _supports_colour,
    load_env_file,
    route_problems,
    shared_archive_notes,
    routes_from_values,
    routes_to_values,
    write_env_file,
)

__all__ = ["run", "add_arguments", "ACTIONS"]

EXIT_OK = 0
EXIT_FAILED = 1

ACTIONS = ("list", "add", "edit", "remove", "enable", "disable")

_ENV_HEADER = (
    "The transcriber's settings. Written by `transcriber setup`.",
    "",
    "This file holds live credentials. It is chmod 0600 and .gitignore'd —",
    "keep it that way, and never paste its contents into a chat or an email.",
    "Re-run `python3 -m transcriber setup` to change any of it.",
)

#: What a route's engine field means when it is empty, spelled out rather than left blank —
#: an empty column in a listing reads as "unset and probably wrong" when it means "the
#: normal one".
_DEFAULT_ENGINE_LABEL = "the service default"


# ---------------------------------------------------------------------------------------
# the drive, when there is one
# ---------------------------------------------------------------------------------------

class _Drive:
    """Folder names and folder listings from the live drive, cached, never fatal.

    Every method answers with something usable when Graph cannot be reached: a name falls
    back to the id, a listing falls back to empty. The first failure is remembered so that
    listing eight routes does not print eight copies of the same network error, and
    ``problem`` carries it for the caller to mention once.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self.problem = ""
        self._names: dict[str, str] = {}
        self._children: dict[str, list[tuple[str, str]]] = {}
        self._ancestors: dict[str, tuple[str, ...]] = {}

    def _fail(self, exc: Exception) -> None:
        if not self.problem:
            self.problem = f"{type(exc).__name__}: {exc}"

    def name_of(self, folder_id: str) -> str:
        """The folder's name on the drive, or '' when it cannot be looked up."""
        wanted = (folder_id or "").strip()
        if not wanted or self.client is None or self.problem:
            return self._names.get(wanted, "")
        if wanted in self._names:
            return self._names[wanted]
        try:
            self._names[wanted] = str(getattr(self.client.get_item(wanted), "name", "") or "")
        except Exception as exc:  # noqa: BLE001 - offline is a normal state for this command
            self._fail(exc)
            self._names[wanted] = ""
        return self._names[wanted]

    def ancestors(self, folder_id: str) -> tuple[str, ...]:
        """Every folder above this one, nearest first — or nothing when it cannot be asked.

        A folder id says nothing about what contains it, and OneDrive reports a folder and
        everything under it: a route whose watched folder sits inside another route's
        watched folder has its recordings claimed by that other route. So it is asked here,
        where a live drive is already being browsed to pick the ids.
        """
        wanted = (folder_id or "").strip()
        if not wanted or self.client is None:
            return ()
        if wanted in self._ancestors:
            return self._ancestors[wanted]
        chain: list[str] = []
        seen = {wanted}
        current = wanted
        for _ in range(32):   # bounded: a tree has no cycles, but a walk should not trust that
            try:
                item = self.client.get_item(current)
            except Exception as exc:  # noqa: BLE001 - offline is a normal state here
                self._fail(exc)
                break
            parent = str(getattr(item, "parent_id", "") or "").strip()
            if not parent or parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
            current = parent
        self._ancestors[wanted] = tuple(chain)
        return self._ancestors[wanted]

    def folders(self, parent_id: str | None) -> list[tuple[str, str]]:
        """(id, name) of every folder directly inside ``parent_id``, name order."""
        if self.client is None:
            return []
        key = parent_id or ""
        if key in self._children:
            return self._children[key]
        try:
            items = self.client.list_children(parent_id or None)
        except Exception as exc:  # noqa: BLE001
            self._fail(exc)
            self._children[key] = []
            return []
        found = sorted(
            ((str(i.id), str(i.name)) for i in items if not getattr(i, "is_file", True)),
            key=lambda pair: pair[1].lower(),
        )
        for folder_id, name in found:
            self._names.setdefault(folder_id, name)
        self._children[key] = found
        return found


def _drive_for(values: Mapping[str, str], *, offline: bool) -> _Drive:
    """A drive client from the ``.env`` values, or a stub that answers offline.

    Built from the values rather than from :class:`Config` on purpose: this command has to
    work on a ``.env`` that is not yet complete — that is half of what it is for — and
    refusing to list the routes because SMTP is unconfigured would be absurd.
    """
    if offline:
        drive = _Drive(None)
        drive.problem = "you asked for --offline"
        return drive
    needed = ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER_ID")
    if any(not str(values.get(name) or "").strip() for name in needed):
        drive = _Drive(None)
        drive.problem = (
            "the Microsoft settings are not filled in yet, so folder names cannot be "
            "looked up"
        )
        return drive
    try:
        from .graph import GraphClient, RetryPolicy

        client = GraphClient(
            tenant_id=str(values["GRAPH_TENANT_ID"]),
            client_id=str(values["GRAPH_CLIENT_ID"]),
            client_secret=str(values["GRAPH_CLIENT_SECRET"]),
            user_id=str(values["GRAPH_USER_ID"]),
            timeout=15.0,
            # Deliberately impatient, and nothing like the service's own policy. A folder
            # name is a convenience: somebody standing at a terminal on a laptop with no
            # route to Microsoft should see the ids a second later, not wait out six
            # retries with exponential backoff for a listing they can do without.
            retry=RetryPolicy(max_attempts=2, base_delay=0.5, max_delay=2.0),
        )
    except Exception as exc:  # noqa: BLE001
        drive = _Drive(None)
        drive.problem = f"{type(exc).__name__}: {exc}"
        return drive
    return _Drive(client)


# ---------------------------------------------------------------------------------------
# reading and writing the file
# ---------------------------------------------------------------------------------------

def _load_for_reading(env_path: str, out: Any) -> tuple[dict[str, str], list[Route]] | None:
    """The file if there is one, otherwise whatever settings this process was given."""
    if os.path.exists(env_path):
        return _load(env_path, out)
    from .config import environment_the_service_reads

    values = environment_the_service_reads()
    if not values:
        print(
            f"there is no {env_path} to read, and this process was not given any settings "
            "either. Run `python3 -m transcriber setup` to write one, or pass --env with "
            "the path to it.",
            file=out,
        )
        return None
    print(
        f"There is no {env_path}, so these are the routes THIS PROCESS was given. Pass "
        "--env to read a file instead.\n",
        file=out,
    )
    return values, list(routes_from_values(values))


def _load(env_path: str, out: Any) -> tuple[dict[str, str], list[Route]] | None:
    if not os.path.exists(env_path):
        print(
            f"there is no {env_path} to read. Run `python3 -m transcriber setup` to write "
            "one, or pass --env with the path to it.",
            file=out,
        )
        return None
    values = load_env_file(env_path)
    return values, list(routes_from_values(values))


def _declared(values: Mapping[str, str]) -> bool:
    """True when the file lists its routes, false when it is the pre-routes single-folder shape."""
    return bool(str(values.get("ROUTES") or "").strip())


def _save(env_path: str, values: dict[str, str], routes: Sequence[Route],
          drive: "_Drive | None" = None) -> list[str]:
    """Check the routes, then write them. Returns the problems; writes nothing if there are any.

    Two checks, because they answer different questions. ``route_problems`` says what is
    wrong with the routes in the words a person would use. Loading the written file back
    through the real ``Config`` says whether the *service* will start, which is the only
    definition of "valid" that matters — and doing it before the write means a refusal
    leaves the file exactly as it was.
    """
    problems = list(route_problems(routes, drive.ancestors if drive is not None else None))
    if problems:
        return problems

    candidate = routes_to_values(dict(values), list(routes))
    try:
        merged = dict(os.environ)
        merged.update(candidate)
        config_mod.Config.from_env(merged)
    except config_mod.ConfigError as exc:
        before = _existing_problems(values)
        introduced = [p for p in exc.problems if p not in before]
        if introduced:
            return introduced
    except Exception as exc:  # noqa: BLE001
        return [f"{type(exc).__name__}: {exc}"]

    lost = comments_would_be_lost(env_path)
    write_env_file(env_path, candidate, header=list(_ENV_HEADER))
    if lost:
        print("  Your own comments in that file were not kept — it is rewritten in the "
              "standard grouped layout every time, which is what keeps the 0600 mode and "
              "the grouping.")
    values.clear()
    values.update(candidate)
    return []


def _existing_problems(values: Mapping[str, str]) -> list[str]:
    """What the file was already unhappy about before this change.

    A ``.env`` that is missing its SMTP password must not become uneditable by the routes
    command: the pre-existing complaints are subtracted, so only what this change would
    break counts against it.
    """
    try:
        merged = dict(os.environ)
        merged.update(values)
        config_mod.Config.from_env(merged)
    except config_mod.ConfigError as exc:
        return list(exc.problems)
    except Exception as exc:  # noqa: BLE001
        return [f"{type(exc).__name__}: {exc}"]
    return []


def _print_problems(ctx: _Ctx, problems: Sequence[str], *, preamble: str) -> None:
    ctx.out()
    ctx.out(ctx.style.red(ctx.style.bold("  " + preamble)))
    for problem in problems:
        ctx.out(ctx.style.red(f"    • {problem}"))


# ---------------------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------------------

def _folder_line(ctx: _Ctx, drive: _Drive, label: str, folder_id: str, empty: str = "") -> None:
    if not folder_id:
        ctx.out(f"      {label:<10} {ctx.style.dim(empty or 'not set')}")
        return
    name = drive.name_of(folder_id)
    shown = name or "(name not looked up)"
    ctx.out(f"      {label:<10} {shown}  {ctx.style.dim(folder_id)}")


def cmd_list(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
             routes: list[Route]) -> int:
    drive = _drive_for(values, offline=args.offline)
    ctx.out()
    ctx.out(ctx.style.bold(f"  Routes — {args.env}"))

    if not routes:
        ctx.out()
        ctx.out("  There are no routes yet, so nothing is being watched.")
        ctx.out("  Add one with " + ctx.style.bold("python3 -m transcriber routes add") + ".")
        return EXIT_FAILED

    if not _declared(values):
        ctx.out()
        ctx.out(ctx.style.dim(
            "  This .env was written before routes existed. The service reads its one\n"
            "  watched folder as a single route called 'default'; nothing is wrong with\n"
            "  that, and it keeps working untouched. Adding or editing a route here\n"
            "  writes them out as a proper list."))

    for route in routes:
        ctx.out()
        heading = f"  {route.name}"
        if route.label and route.label != route.name:
            heading += f"  ·  {route.label}"
        state = ctx.style.green("watching") if route.enabled else ctx.style.yellow("PAUSED")
        ctx.out(ctx.style.bold(heading) + "   " + state)
        _folder_line(ctx, drive, "watches", route.source_folder_id)
        _folder_line(ctx, drive, "writes to", route.output_folder_id)
        _folder_line(
            ctx, drive, "archives", route.archive_folder_id,
            empty="never — its recordings stay where they are",
        )
        engine = route.engine or f"{_DEFAULT_ENGINE_LABEL} ({values.get('TRANSCRIBE_ENGINE') or 'openai'})"
        ctx.out(f"      {'engine':<10} {engine}")

    pooled = _pooled_outputs(routes)
    if pooled:
        ctx.out()
        for names in pooled:
            ctx.out(ctx.style.dim(
                "  " + " and ".join(names) + " write into the same folder. That is allowed "
                "on purpose."))

    for note in shared_archive_notes(routes):
        ctx.out()
        for line in _wrap_note(note):
            ctx.out(ctx.style.dim("  " + line))

    if drive.problem:
        ctx.out()
        ctx.out(ctx.style.dim(
            f"  Folder names could not be read from the drive ({drive.problem}), so the\n"
            "  ids are shown as they are. Nothing is wrong with the routes themselves."))

    problems = route_problems(routes, drive.ancestors)
    if problems:
        _print_problems(ctx, problems, preamble="These routes would stop the service starting:")

    ctx.out()
    ctx.out(ctx.style.dim(
        "  routes add · routes edit <name> · routes disable <name> · routes remove <name>"))
    return EXIT_FAILED if problems else EXIT_OK


def _wrap_note(text: str, width: int = 74) -> list[str]:
    """Wrap one plain sentence for the terminal without pulling in textwrap's defaults."""
    words = str(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _pooled_outputs(routes: Sequence[Route]) -> list[list[str]]:
    """Groups of routes sharing one output folder — reported, never forbidden."""
    by_folder: dict[str, list[str]] = {}
    for route in routes:
        if route.output_folder_id:
            by_folder.setdefault(route.output_folder_id, []).append(route.name)
    return [names for names in by_folder.values() if len(names) > 1]


# ---------------------------------------------------------------------------------------
# picking a folder
# ---------------------------------------------------------------------------------------

_UP = "\x00up"
_INTO = "\x00into"
_BY_ID = "\x00id"
_NONE = "\x00none"


def _pick_folder(
    ctx: _Ctx,
    drive: _Drive,
    question: str,
    *,
    blurb: str = "",
    current: str = "",
    allow_none: bool = False,
) -> str:
    """One folder id, chosen from the live drive where that is possible and typed where it is not.

    Nested folders are reachable: recordings rarely sit at the top of a drive, and a picker
    that only offers the root is a picker that sends somebody back to hunting for a
    driveItem id in a browser URL.
    """
    if blurb:
        ctx.out()
        ctx.out(ctx.style.dim("  " + blurb))

    if drive.client is None:
        return _ask_folder_id(ctx, question, current=current, allow_none=allow_none)

    parent: str | None = None
    trail: list[tuple[str | None, str]] = []
    while True:
        folders = drive.folders(parent)
        if not folders and parent is None and drive.problem:
            ctx.out(ctx.style.yellow(
                f"  The drive could not be listed ({drive.problem}); enter the id instead."))
            return _ask_folder_id(ctx, question, current=current, allow_none=allow_none)

        where = " / ".join(name for _id, name in trail) or "the top of the drive"
        options: list[tuple[str, str]] = [(fid, name) for fid, name in folders]
        if folders:
            options.append((_INTO, "look inside one of these"))
        if trail:
            options.append((_UP, f"back to {trail[-2][1] if len(trail) > 1 else 'the top'}"))
        options.append((
            _BY_ID,
            "type a folder id by hand" if folders
            else "there are no folders here — type an id instead",
        ))
        if allow_none:
            options.append((_NONE, "none — this route never archives"))

        chosen = ctx.choose(f"{question}  ({where})", options, current=current)
        if chosen == _UP:
            trail.pop()
            parent = trail[-1][0] if trail else None
            continue
        if chosen == _INTO:
            inside = ctx.choose("Which one do you want to look inside?", list(folders))
            name = dict(folders).get(inside, inside)
            trail.append((inside, name))
            parent = inside
            continue
        if chosen == _BY_ID:
            return _ask_folder_id(ctx, question, current=current, allow_none=allow_none)
        if chosen == _NONE:
            return ""
        ctx.out(ctx.style.green(f"  → {drive.name_of(chosen) or chosen}"))
        return chosen


def _ask_folder_id(ctx: _Ctx, question: str, *, current: str, allow_none: bool) -> str:
    help_text = (
        "The driveItem id — the long string after 'id=' in the folder's OneDrive address."
        + (" Leave it empty for none." if allow_none else "")
    )
    return ctx.ask_free(
        question + " (folder id)", default=current, required=not allow_none, help_text=help_text
    ).strip()


# ---------------------------------------------------------------------------------------
# add / edit
# ---------------------------------------------------------------------------------------

def _ask_slug(ctx: _Ctx, taken: Iterable[str], *, current: str = "") -> str:
    used = {name for name in taken if name != current}

    def check(raw: str) -> str:
        name = raw.strip().lower()
        if not is_route_name(name):
            return (
                "Short names are lowercase letters, digits and hyphens, starting with a "
                f"letter or a digit — {_suggest(raw)!r} rather than {raw!r}."
            )
        if name in used:
            return f"There is already a route called {name!r}. Each one needs its own name."
        return ""

    return ctx.ask_free(
        "A short name for this route",
        default=current,
        help_text="Lowercase, no spaces — calls, site-meetings, whatsapp. It is what the "
                  "ledger records against every recording on this route, so it is worth "
                  "choosing one you will still recognise in two years.",
        validate=check,
    ).strip().lower()


def _suggest(name: str) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in (name or "").strip().lower()).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "calls"


def _ask_engine(ctx: _Ctx, values: Mapping[str, str], current: str = "") -> str:
    default_engine = str(values.get("TRANSCRIBE_ENGINE") or "openai")
    options = [("", f"{_DEFAULT_ENGINE_LABEL} — {default_engine}")]
    options.extend((name, f"{name} — just for this route") for name in ENGINES)
    return ctx.choose(
        "Which transcription engine should this route use?", options, current=current
    )


def _ask_route(
    ctx: _Ctx, drive: _Drive, values: Mapping[str, str], *, existing: Route | None,
    taken: Iterable[str],
) -> Route:
    """The five questions a route is made of, asked in the order they make sense."""
    current = existing or Route(name="")
    name = _ask_slug(ctx, taken, current=current.name)
    label = ctx.ask_free(
        "What do you call these recordings?",
        default=current.label or _label_for(name),
        help_text="In your words — 'Site meetings', 'WhatsApp voice notes'. It is what the "
                  "morning email calls them.",
    ).strip()
    source = _pick_folder(
        ctx, drive, "Which folder do these recordings land in?",
        blurb="The folder the phone, the app or the recorder uploads into.",
        current=current.source_folder_id,
    )
    output = _pick_folder(
        ctx, drive, "Which folder should its transcripts be written to?",
        blurb="It may be the same folder another route writes to — pooling is fine. It "
              "must not be a folder any route watches: " + FEEDBACK_LOOP,
        current=current.output_folder_id,
    )
    archive = _pick_folder(
        ctx, drive, "Which folder should its recordings move to when they are old?",
        blurb="Only ever moved once the transcripts are confirmed present, and nothing is "
              "ever deleted. Choose none to leave them where they are for good.",
        current=current.archive_folder_id, allow_none=True,
    )
    engine = _ask_engine(ctx, values, current=current.engine)
    enabled = current.enabled if existing is not None else True
    return Route(
        name=name, label=label, source_folder_id=source, output_folder_id=output,
        archive_folder_id=archive, engine=engine, enabled=enabled,
    )


def _label_for(name: str) -> str:
    for slug, label in SUGGESTED_ROUTES:
        if slug == name:
            return label
    return name.replace("-", " ").capitalize()


def _converting(ctx: _Ctx, values: Mapping[str, str], routes: Sequence[Route]) -> bool:
    """Warn, once, that this edit turns a pre-routes ``.env`` into a routes one."""
    if _declared(values) or not routes:
        return True
    ctx.out()
    ctx.out(ctx.style.yellow(
        "  This .env still uses the single-folder settings from before routes existed.\n"
        "  Saving here writes them out as a list of routes instead: the folder you watch\n"
        "  now becomes a route called 'default', and nothing about what is watched, what\n"
        "  is written, or what is in the ledger changes."))
    return ctx.confirm("Carry on?", default=True)


def cmd_add(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
            routes: list[Route]) -> int:
    drive = _drive_for(values, offline=args.offline)
    if drive.problem:
        ctx.out()
        ctx.out(ctx.style.yellow(
            f"  The drive cannot be listed ({drive.problem}), so folder ids have to be\n"
            "  typed rather than picked."))
    if not _converting(ctx, values, routes):
        ctx.out("  Nothing was written.")
        return EXIT_OK

    while True:
        route = _ask_route(ctx, drive, values, existing=None,
                           taken=[r.name for r in routes])
        candidate = list(routes) + [route]
        problems = _save(args.env, values, candidate, drive)
        if not problems:
            ctx.out()
            ctx.out(ctx.style.green(f"  ✓ {route.display} is now being watched."))
            ctx.out(ctx.style.dim(
                "  The running service does not re-read .env — restart it to pick this up."))
            return EXIT_OK
        _print_problems(ctx, problems, preamble="That cannot be saved as it stands:")
        if not ctx.confirm("Go back and change the answers?", default=True):
            ctx.out("  Nothing was written.")
            return EXIT_FAILED


def cmd_edit(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
             routes: list[Route]) -> int:
    route = _find(ctx, routes, args.slug)
    if route is None:
        return EXIT_FAILED
    drive = _drive_for(values, offline=args.offline)
    if not _converting(ctx, values, routes):
        ctx.out("  Nothing was written.")
        return EXIT_OK

    ctx.out()
    ctx.out(ctx.style.dim("  Press Enter to keep an answer as it is."))
    while True:
        edited = _ask_route(ctx, drive, values, existing=route,
                            taken=[r.name for r in routes])
        candidate = [edited if r.name == route.name else r for r in routes]
        problems = _save(args.env, values, candidate, drive)
        if not problems:
            ctx.out()
            ctx.out(ctx.style.green(f"  ✓ {edited.display} saved."))
            if edited.name != route.name:
                ctx.out(
                    f"  It is now called {edited.name!r}. The {route.name!r} rows already in "
                    "the ledger keep that name — nothing was rewritten — so `status` will "
                    "show both until the old ones age out.")
                ctx.out(
                    f"  Recordings on {route.name!r} that have already finished will no "
                    "longer be archived: the archive pass only runs for routes that are "
                    "in the configuration.")
                _say_stranding(
                    ctx, route, _unfinished_in(_ledger_rows_for(values, route.name)),
                    what="Renaming this route",
                )
            ctx.out(ctx.style.dim(
                "  The running service does not re-read .env — restart it to pick this up."))
            return EXIT_OK
        _print_problems(ctx, problems, preamble="That cannot be saved as it stands:")
        if not ctx.confirm("Go back and change the answers?", default=True):
            ctx.out("  Nothing was written.")
            return EXIT_FAILED


# ---------------------------------------------------------------------------------------
# remove / enable / disable
# ---------------------------------------------------------------------------------------

def _find(ctx: _Ctx, routes: Sequence[Route], slug: str | None) -> Route | None:
    wanted = (slug or "").strip()
    if not wanted:
        ctx.out("  Which route? Give its short name, e.g. `transcriber routes edit calls`.")
        ctx.out("  `transcriber routes` lists them.")
        return None
    for route in routes:
        if route.name == wanted:
            return route
    ctx.out(f"  There is no route called {wanted!r}.")
    ctx.out("  The ones this .env describes are: "
            + (", ".join(r.name for r in routes) or "none"))
    return None


def _ledger_rows_for(values: Mapping[str, str], route: str) -> dict[str, int] | None:
    """This route's ledger rows **by state**, or None when the ledger cannot be read.

    Read rather than assumed, because "the history is kept" is a claim, and a number is a
    claim somebody can check. The breakdown is kept rather than summed away: a total says
    how much history there is, and what a person about to remove a route needs to know is
    how much of it has not finished yet. Never creates a ledger: a command about the
    ``.env`` has no business bringing a database into existence.
    """
    path = str(values.get("LEDGER_PATH") or "").strip()
    if not path or path == ":memory:" or not os.path.exists(path):
        return None
    try:
        from .ledger import Ledger

        with Ledger(path) as ledger:
            counts = ledger.stats().get("by_route", {}).get(route, {})
        return {str(state): int(n) for state, n in counts.items()}
    except Exception:  # noqa: BLE001 - a ledger we cannot read is not a reason to refuse
        return None


def _unfinished_in(counts: Mapping[str, int] | None) -> int:
    """How many of this route's recordings have not reached a terminal state."""
    from .models import State

    if not counts:
        return 0
    return sum(int(n) for state, n in counts.items() if state not in State.TERMINAL)


def _say_stranding(ctx: _Ctx, route: Route, unfinished: int, *, what: str) -> None:
    """Say, before the confirm prompt, what happens to work that is still in flight.

    Removing a route, or renaming one, leaves its unfinished recordings naming a route the
    configuration no longer has. The pipeline will not guess where their transcripts belong,
    so it stops each one for a person on the first attempt and never retries it. That is the
    right behaviour and it was not being said out loud, which made "the ledger rows are kept"
    read as "nothing is lost".
    """
    if unfinished <= 0:
        return
    ctx.out()
    ctx.out(ctx.style.yellow(ctx.style.bold(
        f"  {unfinished} of those recordings have not finished yet.")))
    ctx.out(ctx.style.yellow(
        f"  {what} stops them being transcribed. Each one will be stopped for you and"))
    ctx.out(ctx.style.yellow(
        "  marked as needing a person instead, because there would be no route left to say"))
    ctx.out(ctx.style.yellow(
        "  which folder its transcript belongs in."))
    ctx.out(ctx.style.yellow(
        f"  Let them finish first, or use `transcriber routes disable {route.name}` to stop"))
    ctx.out(ctx.style.yellow(
        "  watching the folder without abandoning them."))


def cmd_remove(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
               routes: list[Route]) -> int:
    route = _find(ctx, routes, args.slug)
    if route is None:
        return EXIT_FAILED

    counts = _ledger_rows_for(values, route.name)
    rows = None if counts is None else sum(counts.values())
    unfinished = _unfinished_in(counts)
    ctx.out()
    ctx.out(ctx.style.bold(f"  Removing {route.display} takes it out of the routes the "
                           "service watches."))
    ctx.out()
    ctx.out("  Nothing in OneDrive is touched. Not one recording is moved, renamed or")
    ctx.out("  deleted, and the folders stay exactly as they are.")
    if rows is None:
        ctx.out("  Every ledger row this route ever wrote is kept — nothing in the ledger is")
        ctx.out("  ever deleted — so the morning email and `status` can still account for them.")
    else:
        ctx.out(f"  The {rows} ledger row(s) this route recorded are kept — nothing in the")
        ctx.out("  ledger is ever deleted — so the morning email and `status` can still")
        ctx.out("  account for them.")
    ctx.out()
    ctx.out(ctx.style.dim(
        "  What stops is the watching: no new recording in that folder will be picked up."))
    ctx.out(ctx.style.dim(
        "  If that is not what you want, `routes disable` pauses it instead and keeps its\n"
        "  folders in the file, ready to switch back on."))

    _say_stranding(ctx, route, unfinished, what="Removing this route")

    if not _converting(ctx, values, routes):
        ctx.out("  Nothing was written.")
        return EXIT_OK
    # `--yes` on the command line IS the consent for this one, so it must not be routed
    # through ctx.confirm(). That returns the *default* when assuming, and the default for a
    # destructive act is deliberately No — which made `routes remove --yes` decline the very
    # removal it reads as confirming. A flag that does the opposite of what it says is worse
    # than no flag.
    if not (ctx.assume_yes or ctx.confirm(f"Remove {route.name}?", default=False)):
        ctx.out("  Nothing was written.")
        return EXIT_OK

    remaining = [r for r in routes if r.name != route.name]
    problems = _save(args.env, values, remaining)
    if problems:
        _print_problems(ctx, problems, preamble="It was not removed, and nothing was written:")
        return EXIT_FAILED
    ctx.out()
    ctx.out(ctx.style.green(f"  ✓ {route.display} is no longer watched. Its history is kept."))
    ctx.out(ctx.style.dim(
        "  The running service does not re-read .env — restart it to pick this up."))
    return EXIT_OK


def _set_enabled(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
                 routes: list[Route], *, enabled: bool) -> int:
    route = _find(ctx, routes, args.slug)
    if route is None:
        return EXIT_FAILED
    word = "watching" if enabled else "paused"
    if route.enabled == enabled:
        ctx.out(f"  {route.display} is already {word}. Nothing was written.")
        return EXIT_OK
    if not _converting(ctx, values, routes):
        ctx.out("  Nothing was written.")
        return EXIT_OK

    candidate = [replace(r, enabled=enabled) if r.name == route.name else r for r in routes]
    problems = _save(args.env, values, candidate)
    if problems:
        _print_problems(
            ctx, problems,
            preamble=("It was not switched off, and nothing was written:" if not enabled
                      else "It was not switched on, and nothing was written:"),
        )
        if not enabled and len([r for r in routes if r.enabled]) == 1:
            ctx.out()
            ctx.out("  That is the only route still switched on. With it off, nothing would "
                    "be watched\n  and the service would refuse to start. Add another route "
                    "first, or stop the service.")
        return EXIT_FAILED

    ctx.out()
    if enabled:
        ctx.out(ctx.style.green(f"  ✓ {route.display} is being watched again."))
        ctx.out("  It starts from where its cursor stopped, so anything that arrived while "
                "it was\n  paused is picked up rather than skipped.")
    else:
        ctx.out(ctx.style.yellow(f"  ✓ {route.display} is paused."))
        ctx.out("  Its folders, its cursor and every ledger row it wrote are untouched. "
                "Switch it\n  back on with `transcriber routes enable "
                f"{route.name}` and it carries on where it left off.")
    ctx.out(ctx.style.dim(
        "  The running service does not re-read .env — restart it to pick this up."))
    return EXIT_OK


def cmd_enable(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
               routes: list[Route]) -> int:
    return _set_enabled(ctx, args, values, routes, enabled=True)


def cmd_disable(ctx: _Ctx, args: argparse.Namespace, values: dict[str, str],
                routes: list[Route]) -> int:
    return _set_enabled(ctx, args, values, routes, enabled=False)


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

_HANDLERS = {
    "list": cmd_list,
    "add": cmd_add,
    "edit": cmd_edit,
    "remove": cmd_remove,
    "enable": cmd_enable,
    "disable": cmd_disable,
}


def run(args: argparse.Namespace, stream: Any = None) -> int:
    stream = stream or sys.stdout
    action = (getattr(args, "action", None) or "list").strip().lower()
    handler = _HANDLERS.get(action)
    if handler is None:  # pragma: no cover - argparse constrains this
        print(f"`routes {action}` is not something this command does: "
              + ", ".join(ACTIONS), file=stream)
        return EXIT_FAILED

    # `list` only reads, so on a deployed host with no .env it answers from the
    # process environment rather than refusing. Everything else writes the file,
    # and writing into a process environment changes nothing that outlives the
    # command, so those keep refusing with the sentence that says what to do.
    loaded = _load_for_reading(args.env, stream) if action == "list" else _load(args.env, stream)
    if loaded is None:
        return EXIT_FAILED
    values, routes = loaded

    ctx = _Ctx(
        values=values,
        style=_Style(_supports_colour(stream)),
        verify=not args.offline,
        assume_yes=args.yes,
    )
    for name in ("GRAPH_CLIENT_SECRET", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
                 "AZURE_SPEECH_KEY", "ANALYSIS_API_KEY", "SMTP_PASSWORD"):
        ctx.remember_secret(values.get(name, ""))

    try:
        return handler(ctx, args, values, routes)
    except KeyboardInterrupt:
        ctx.out(ctx.style.yellow("\n\n  Stopped. Nothing was written.\n"))
        return EXIT_FAILED


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "action", nargs="?", default="list", choices=ACTIONS,
        help="list them (the default), or add / edit / remove / enable / disable one",
    )
    parser.add_argument("slug", nargs="?", default=None,
                        help="the route's short name, for everything but list and add")
    parser.add_argument("--env", default=".env", help="path to the .env (default: .env)")
    parser.add_argument(
        "--offline", action="store_true",
        help="do not contact the drive: show folder ids rather than names, and type ids "
             "rather than picking them",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="do not stop to confirm — including the confirmation on `remove`",
    )
    return parser
