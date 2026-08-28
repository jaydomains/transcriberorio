"""``transcriber setup`` — the interactive wizard that writes ``.env``.

The person deploying this is a building consultant, not an engineer. Handing him a file of
forty variables and a comment each is a way of saying "you work it out". So this asks one
question at a time, in plain words, and — the part that actually matters — **checks each
answer against the real service before moving on**. A wrong tenant id found here costs
thirty seconds; found at 06:00 on a Tuesday it costs a day of recordings.

Section 2 is the routes: one per kind of recording he makes, each with the folder it
arrives in and the folder its transcripts land in. Nobody is asked to know a driveItem id —
folders are chosen from the live drive by name — and nobody is asked to invent a structure
either: a first run offers his three actual kinds, ready to accept. Every change is checked
against the whole set at once, because the misconfiguration that matters is between two
routes rather than inside one: transcripts written into a watched folder come back round as
new recordings, and the same folder watched twice means two claims on one recording.

Two rules the whole module obeys:

* **A secret is never echoed, never logged, never written anywhere but ``.env``.** Typed
  input is read with :func:`getpass.getpass`, existing values are shown only as their last
  four characters, and every failure message is scrubbed before it is printed.
* **A failed check is never fatal by itself.** He may be setting this up before the admin
  has granted consent, or on a laptop with no route to SMTP. Every check offers "carry on
  anyway", because a wizard that refuses to finish is a wizard that gets abandoned
  half-done — and a half-written ``.env`` is worse than none.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from . import config as config_mod
from .config import nested_folder_problems
from .models import DEFAULT_ROUTE, Route, is_route_name, route_env_var

__all__ = [
    "run_setup",
    "load_env_file",
    "write_env_file",
    "routes_from_values",
    "routes_to_values",
    "route_problems",
    "shared_archive_notes",
    "SUGGESTED_ROUTES",
    "FEEDBACK_LOOP",
]


# ---------------------------------------------------------------------------------------
# terminal helpers
# ---------------------------------------------------------------------------------------

def _supports_colour(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and os.environ.get("TERM") != "dumb"


class _Style:
    def __init__(self, on: bool) -> None:
        self.on = on

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)


def mask(value: str) -> str:
    """Show enough of a secret to recognise it, never enough to use it."""
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return "•" * 8 + value[-4:]


# ---------------------------------------------------------------------------------------
# .env read / write
# ---------------------------------------------------------------------------------------

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_env_file(path: str) -> dict[str, str]:
    """Parse a ``.env``. Tolerant on purpose — a hand-edited file must still load."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return out
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            # Undo the escaping _quote() applies. Without this a client secret containing a
            # quote or a backslash round-trips to something subtly different, and the only
            # symptom is an authentication failure that names nothing useful.
            value = value[1:-1]
            if value and "\\" in value:
                value = re.sub(r"\\(.)", r"\1", value)
        else:  # strip a trailing comment only when the value is not quoted
            value = value.split("  #", 1)[0].strip()
        out[key] = value
    return out


def _quote(value: str) -> str:
    if value == "" or re.search(r"[\s#\"'$`\\]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def write_env_file(path: str, values: dict[str, str], *, header: Sequence[str] = ()) -> None:
    """Write ``.env`` at 0600, and only ever at 0600.

    Created with :func:`os.open` rather than written then chmod'd — between those two calls
    a world-readable file holding a client secret exists on disk, and on a shared host that
    window is the whole vulnerability.
    """
    lines: list[str] = []
    for line in header:
        lines.append(f"# {line}" if line else "#")
    if header:
        lines.append("")

    written: set[str] = set()
    for group, members in _effective_groups(values):
        present = [v for v in members if v in values and v not in written]
        if not present:
            continue
        lines.append(f"# --- {group} " + "-" * max(0, 70 - len(group)))
        for name in present:
            lines.append(f"{name}={_quote(values[name])}")
            written.add(name)
        lines.append("")
    extra = sorted(k for k in values if k not in written)
    if extra:
        lines.append("# --- other " + "-" * 64)
        for name in extra:
            lines.append(f"{name}={_quote(values[name])}")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)


#: The heading the routes are written under. Named once, because
#: :func:`_effective_groups` finds the group by this string in order to splice each route's
#: own variables in after ``ROUTES``.
ROUTE_GROUP = "the routes — one per kind of recording"

_GROUPS: list[tuple[str, list[str]]] = [
    ("Microsoft / OneDrive", [
        "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_USER_ID",
        "GRAPH_SECRET_EXPIRES_ON", "ORPHAN_FOLDER_ID",
    ]),
    # ROUTES first, then each route's own six variables in the order they were asked for,
    # then the three single-folder settings a .env written before routes existed uses. The
    # per-route names depend on the route names, so they are spliced in at write time.
    (ROUTE_GROUP, [
        "ROUTES",
        "SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID",
    ]),
    ("transcription", [
        "TRANSCRIBE_ENGINE", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
        "AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION", "ENGINE_BASE_URL",
        "ENGINE_KEY_EXPIRES_ON",
    ]),
    ("the analysis pass", [
        "ANALYSIS_PROVIDER", "ANALYSIS_API_KEY", "ANALYSIS_BASE_URL",
        "ANALYSIS_MODEL_CHEAP", "ANALYSIS_MODEL_STRONG", "ANALYSIS_KEY_EXPIRES_ON",
    ]),
    ("the morning email", [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO",
        "DIGEST_HOUR",
    ]),
    ("staying alive", ["HEARTBEAT_URL"]),
    ("local", ["LEDGER_PATH", "WORK_DIR", "LOG_FORMAT"]),
]

#: Written masked into any transcript of the run, and never printed back.
SECRET_VARS = {
    "GRAPH_CLIENT_SECRET", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY", "ANALYSIS_API_KEY", "SMTP_PASSWORD",
}


def _route_names_in(values: Mapping[str, str]) -> list[str]:
    """The route names ``ROUTES`` lists, in the order it lists them, without duplicates."""
    raw = str(values.get("ROUTES") or "").strip()
    out: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def _effective_groups(values: Mapping[str, str]) -> list[tuple[str, list[str]]]:
    """``_GROUPS``, with each route's own variables spliced into the routes group.

    ``ROUTE_SITE_MEETINGS_SOURCE`` cannot be listed in a static table because the name of
    the route is half of it. Without this the route settings fall through to the "other"
    heading at the bottom, sorted alphabetically, which scatters a route's six lines across
    the file and puts the folders of one kind of recording next to the folders of another.
    """
    route_vars: list[str] = []
    for name in _route_names_in(values):
        route_vars.extend(route_env_var(name, suffix) for suffix in config_mod.ROUTE_SUFFIXES)
    # Anything ROUTE_-shaped that no listed route claims: a route taken out of ROUTES by
    # hand, most likely. Keep it with the routes rather than in "other", so the file still
    # reads as one section per subject.
    known = set(route_vars)
    orphans = sorted(k for k in values if k.startswith("ROUTE_") and k not in known)

    groups: list[tuple[str, list[str]]] = []
    for group, members in _GROUPS:
        if group == ROUTE_GROUP:
            head = [m for m in members if m == "ROUTES"]
            tail = [m for m in members if m != "ROUTES"]
            groups.append((group, head + route_vars + orphans + tail))
        else:
            groups.append((group, list(members)))
    return groups


# ---------------------------------------------------------------------------------------
# routes — reading them out of a .env, writing them back, and saying what is wrong
# ---------------------------------------------------------------------------------------

#: His three actual kinds of recording, offered on a first run so that the answer to "what
#: routes do you want?" is a single Enter rather than an invitation to invent a structure.
SUGGESTED_ROUTES: tuple[tuple[str, str], ...] = (
    ("calls", "Phone calls"),
    ("site-meetings", "Site meetings"),
    ("whatsapp", "WhatsApp voice notes"),
)

#: The one sentence that has to be said when a route's transcripts would land in a folder
#: something watches. Every other misconfiguration here costs a restart; this one has the
#: service transcribing its own transcripts, for as long as nobody notices — so it is
#: spelled out in words rather than described as a rule number.
FEEDBACK_LOOP = "That would make the service read its own transcripts as new recordings."


def routes_from_values(values: Mapping[str, str]) -> list[Route]:
    """The routes a set of ``.env`` values describes, in the order they are listed.

    A file with no ``ROUTES`` but the older single-folder settings is one route called
    ``default`` — the same reading :mod:`transcriber.config` gives it, so re-running the
    wizard over a ``.env`` written before routes existed shows him what he already has
    instead of an empty list and a question he has answered before.
    """
    names = _route_names_in(values)
    if names:
        routes: list[Route] = []
        for name in names:
            def var(suffix: str, _name: str = name) -> str:
                return str(values.get(route_env_var(_name, suffix)) or "").strip()

            routes.append(
                Route(
                    name=name,
                    label=var("LABEL"),
                    source_folder_id=var("SOURCE"),
                    output_folder_id=var("OUTPUT"),
                    archive_folder_id=var("ARCHIVE"),
                    engine=var("ENGINE").lower(),
                    enabled=var("ENABLED").lower() not in ("0", "false", "no", "off"),
                )
            )
        return routes

    legacy = [str(values.get(key) or "").strip() for key in
              ("SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID")]
    if any(legacy):
        return [
            Route(
                name=DEFAULT_ROUTE,
                label="Recordings",
                source_folder_id=legacy[0],
                output_folder_id=legacy[1],
                archive_folder_id=legacy[2],
            )
        ]
    return []


def routes_to_values(values: dict[str, str], routes: Sequence[Route]) -> dict[str, str]:
    """Write the routes into the ``.env`` values, and take out what they replace.

    Every ``ROUTE_*`` variable is cleared first, so a route he removed does not leave its
    folders behind for a later hand-edit of ``ROUTES`` to resurrect. The three
    single-folder settings go too: once ``ROUTES`` is set the service ignores them
    completely, and a file that still lists them is a file that invites somebody to edit
    the setting that does nothing.
    """
    # Who reviews each route's held passages is a route setting, but it is not one the
    # folder pickers ask for — it is set with `transcriber config` or by hand. Clearing
    # every ROUTE_ variable would delete it on the next wizard run, and a reviewer that
    # quietly reverts to the service owner is exactly the kind of silent change this
    # service exists to stop, so it is carried across for the routes that survive.
    reviewers = {
        key: value for key, value in values.items()
        if key.startswith("ROUTE_") and key.endswith("_REVIEWER") and str(value or "").strip()
    }
    for key in [k for k in values if k.startswith("ROUTE_")]:
        del values[key]
    for key in ("SOURCE_FOLDER_ID", "OUTPUT_FOLDER_ID", "ARCHIVE_FOLDER_ID"):
        values.pop(key, None)

    values["ROUTES"] = ",".join(route.name for route in routes)
    for route in routes:
        values[route_env_var(route.name, "LABEL")] = route.label
        values[route_env_var(route.name, "SOURCE")] = route.source_folder_id
        values[route_env_var(route.name, "OUTPUT")] = route.output_folder_id
        values[route_env_var(route.name, "ARCHIVE")] = route.archive_folder_id
        values[route_env_var(route.name, "ENGINE")] = route.engine
        values[route_env_var(route.name, "ENABLED")] = "true" if route.enabled else "false"
        kept = reviewers.get(route_env_var(route.name, "REVIEWER"))
        if kept:
            values[route_env_var(route.name, "REVIEWER")] = kept
    return values


def _phrase(route: Route) -> str:
    """How a route is named in a sentence somebody reads: ``Phone calls (calls)``."""
    label = (route.label or "").strip()
    return f"{label} ({route.name})" if label else route.name


def route_problems(routes: Sequence[Route], ancestors_of: Any = None) -> list[str]:
    """Everything wrong with a set of routes, in the words he would use.

    ``ancestors_of(folder_id) -> ancestor ids, nearest first`` adds the checks that folder
    ids alone cannot answer — one route's watched folder sitting inside another's, an
    archive folder inside a watched folder. Pass it whenever a live drive is at hand, which
    is exactly where the ids are chosen; omitted, those checks are simply not made.

    This is the wizard's own copy of the rules so that a problem can be shown the moment it
    is created rather than at the end of a twenty-question run. It is deliberately no more
    permissive than :func:`transcriber.config._validate_routes`, and it is not the
    authority: what the wizard writes is loaded back through the real ``Config`` before it
    claims to have finished, so the two cannot quietly disagree about whether the service
    will start.
    """
    problems: list[str] = []
    if not routes:
        problems.append(
            "There are no routes yet, so nothing would be watched and nothing transcribed. "
            "Add at least one."
        )

    seen: set[str] = set()
    for route in routes:
        if not is_route_name(route.name):
            problems.append(
                f"{route.name!r} will not do as a short name — lowercase letters, digits and "
                "hyphens only, starting with a letter or a digit, like site-meetings."
            )
        if route.name in seen:
            problems.append(
                f"There are two routes called {route.name!r}. The short name is what the "
                "ledger records against every recording, so each route needs its own."
            )
        seen.add(route.name)

    enabled = [r for r in routes if r.enabled]
    if routes and not enabled:
        problems.append(
            "Every route is paused, so nothing would be watched. Switch at least one back on."
        )

    for route in enabled:
        if not route.source_folder_id:
            problems.append(f"{_phrase(route)} has no folder to watch for recordings.")
        if not route.output_folder_id:
            problems.append(f"{_phrase(route)} has nowhere to write its transcripts.")

    # The feedback loop, checked across every route whether it is paused or not: a paused
    # route is a folder somebody switches back on later without re-reading the whole file.
    for writer in routes:
        if not writer.output_folder_id:
            continue
        for watcher in routes:
            if writer.output_folder_id != watcher.source_folder_id or not watcher.source_folder_id:
                continue
            if writer.name == watcher.name:
                problems.append(
                    f"{_phrase(writer)} writes its transcripts into the very folder it "
                    f"watches for recordings. {FEEDBACK_LOOP}"
                )
            else:
                problems.append(
                    f"{_phrase(writer)} writes its transcripts into the folder "
                    f"{_phrase(watcher)} watches for recordings. {FEEDBACK_LOOP}"
                )

    # One folder, one route. Two cursors over one folder is two claims on one recording.
    for index, first in enumerate(enabled):
        if not first.source_folder_id:
            continue
        for second in enabled[index + 1:]:
            if first.source_folder_id == second.source_folder_id:
                problems.append(
                    f"{_phrase(first)} and {_phrase(second)} watch the same folder. A "
                    "recording can only belong to one of them, and whichever saw it first "
                    "would own it while the other moved past it as though it were handled."
                )

    # Sharing an output folder is allowed on purpose — pooling several kinds of recording
    # into one transcripts folder is a thing he asked for, so it is not checked here.

    for archiver in routes:
        if not archiver.archives:
            continue
        for other in routes:
            if other.source_folder_id and archiver.archive_folder_id == other.source_folder_id:
                where = (
                    "the very folder it watches"
                    if archiver.name == other.name
                    else f"the folder {_phrase(other)} watches"
                )
                problems.append(
                    f"{_phrase(archiver)} archives old recordings into {where} for "
                    "recordings, so everything it filed away would be discovered all over "
                    "again the moment it moved."
                )
            if other.output_folder_id and archiver.archive_folder_id == other.output_folder_id:
                whose = (
                    "its own transcripts"
                    if archiver.name == other.name
                    else f"the transcripts of {_phrase(other)}"
                )
                problems.append(
                    f"{_phrase(archiver)} archives old recordings into the folder that "
                    f"holds {whose}. The archive is meant to be the untouched originals, "
                    "and mixing the two makes it neither."
                )

    # And the overlaps an id cannot show, when there is a live drive to ask.
    if ancestors_of is not None:
        problems.extend(nested_folder_problems(routes, ancestors_of))

    return list(dict.fromkeys(problems))


def shared_archive_notes(routes: Sequence[Route]) -> list[str]:
    """Routes filing their old recordings into one archive folder — said, never refused.

    Sharing a folder is his to choose, and pooling is a thing he asked for, so this is not a
    problem and does not stop anything starting. But transcripts and originals are not the
    same case: this service names a transcript and gives it a timestamp prefix, so two of
    ours cannot collide, while an original keeps the name the phone or WhatsApp gave it. Two
    of those arriving in one archive folder with the same name means the second one cannot be
    moved — reported plainly at the time rather than discovered as a failed archive months
    later.
    """
    by_folder: dict[str, list[Route]] = {}
    for route in routes:
        if route.archives:
            by_folder.setdefault(route.archive_folder_id, []).append(route)
    notes: list[str] = []
    for sharing in by_folder.values():
        if len(sharing) < 2:
            continue
        names = " and ".join(_phrase(r) for r in sharing)
        notes.append(
            f"{names} file their old recordings into the same archive folder. That is "
            "allowed. Worth knowing: originals keep the name the phone gave them, so if two "
            "of them ever have the same name the second cannot be moved — it will be "
            "reported in the morning email rather than replacing the first."
        )
    return notes


# ---------------------------------------------------------------------------------------
# the wizard
# ---------------------------------------------------------------------------------------

#: What to say when stdin runs out mid-question. Every prompt in this module ends this way
#: rather than looping on an input that can never arrive: a wizard spinning silently against
#: a closed stdin looks exactly like a wizard that has hung.
_EOF_STOP = (
    "\n  Input ended before that question was answered. Run this in a terminal, or pipe "
    "an answer for every question in order.\n"
)


@dataclass
class _Ctx:
    values: dict[str, str]
    style: _Style
    verify: bool = True
    assume_yes: bool = False
    notes: list[str] = field(default_factory=list)
    scrub: list[str] = field(default_factory=list)

    def out(self, text: str = "") -> None:
        print(self._clean(text))

    def _clean(self, text: str) -> str:
        for secret in self.scrub:
            if secret and len(secret) >= 6:
                text = text.replace(secret, "••••")
        return text

    def remember_secret(self, value: str) -> None:
        if value and value not in self.scrub:
            self.scrub.append(value)

    # -- prompts ------------------------------------------------------------------------

    def ask(
        self,
        name: str,
        question: str,
        *,
        secret: bool = False,
        default: str = "",
        required: bool = True,
        help_text: str = "",
        validate: Callable[[str], str] | None = None,
    ) -> str:
        current = self.values.get(name, "") or default
        shown = mask(current) if (secret and current) else current
        suffix = f" [{self.style.dim(shown)}]" if shown else ""
        while True:
            self.out()
            self.out(self.style.bold(question) + suffix)
            if help_text:
                self.out(self.style.dim("  " + help_text))
            try:
                if secret:
                    raw = getpass.getpass("  > ").strip()
                else:
                    raw = input("  > ").strip()
            except EOFError:
                if not current and required:
                    raise SystemExit(_EOF_STOP)
                raw = ""
            if not raw and current:
                raw = current
            if not raw:
                if not required:
                    self.values[name] = ""
                    return ""
                self.out(self.style.red("  That one is needed. Try again, or press Ctrl-C to stop."))
                continue
            if validate:
                problem = validate(raw)
                if problem:
                    self.out(self.style.red("  " + problem))
                    continue
            if secret:
                self.remember_secret(raw)
            self.values[name] = raw
            return raw

    def ask_free(
        self,
        question: str,
        *,
        default: str = "",
        required: bool = True,
        help_text: str = "",
        validate: Callable[[str], str] | None = None,
    ) -> str:
        """Ask for something that is not an environment variable of its own.

        A route's short name and its label are answers, not settings: they end up inside
        ``ROUTES`` and ``ROUTE_<NAME>_LABEL``, whose names are not known until the answer
        is given. Asked through a scratch key that is removed again, so a half-answered
        route can never leave a stray variable in the written file.
        """
        scratch = "__ASK__"
        self.values.pop(scratch, None)
        try:
            return self.ask(
                scratch, question, default=default, required=required,
                help_text=help_text, validate=validate,
            )
        finally:
            self.values.pop(scratch, None)

    def choose(self, question: str, options: Sequence[tuple[str, str]], *, current: str = "") -> str:
        self.out()
        self.out(self.style.bold(question))
        for i, (value, label) in enumerate(options, 1):
            marker = self.style.green(" (current)") if value == current else ""
            self.out(f"  {i}. {label}{marker}")
        offered = [value for value, _ in options]
        while True:
            try:
                raw = input("  > ").strip()
            except EOFError:
                # No more input will ever arrive. Take the default if there is one; say so
                # and stop if there is not, rather than re-asking a question nobody can hear.
                if current or current in offered:
                    return current
                raise SystemExit(_EOF_STOP) from None
            if not raw and current:
                return current
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1][0]
            for value, _ in options:
                if raw.lower() == value.lower():
                    return value
            self.out(self.style.red(f"  Pick a number from 1 to {len(options)}."))

    def confirm(self, question: str, *, default: bool = True) -> bool:
        if self.assume_yes:
            return default
        hint = "Y/n" if default else "y/N"
        while True:
            try:
                raw = input(f"  {question} [{hint}] ").strip().lower()
            except EOFError:
                return default
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False

    # -- check reporting ----------------------------------------------------------------

    def check(self, label: str, fn: Callable[[], str]) -> bool:
        """Run one live check. Returns False only if the user chooses to stop."""
        if not self.verify:
            self.out(self.style.dim(f"  (skipped: {label})"))
            return True
        self.out(self.style.dim(f"  checking {label} …"))
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - any failure is reportable, none is fatal
            msg = self._clean(f"{type(exc).__name__}: {exc}")
            self.out(self.style.red(f"  ✗ {label} — {msg}"))
            self.notes.append(f"{label} did not pass: {msg}")
            return self.confirm("Carry on anyway and fix it later?", default=True)
        self.out(self.style.green(f"  ✓ {label}") + (f" — {self._clean(detail)}" if detail else ""))
        return True


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    method: str = "GET",
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    try:
        doc = json.loads(body.decode("utf-8", "replace")) if body else {}
    except ValueError:
        doc = {"_raw": body[:400].decode("utf-8", "replace")}
    return status, doc if isinstance(doc, dict) else {"_list": doc}


def _api_error(doc: dict[str, Any]) -> str:
    for path in (("error", "message"), ("error", "code"), ("message",), ("error_description",)):
        node: Any = doc
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, str) and node:
            return node[:300]
    return json.dumps(doc)[:300]


# ---------------------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------------------

def _section(ctx: _Ctx, number: int, title: str, blurb: str = "") -> None:
    ctx.out()
    ctx.out(ctx.style.cyan("─" * 74))
    ctx.out(ctx.style.cyan(ctx.style.bold(f" {number}. {title}")))
    if blurb:
        ctx.out(ctx.style.dim(" " + blurb))
    ctx.out(ctx.style.cyan("─" * 74))


def _iso_date_or_blank(raw: str) -> str:
    if not raw:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return ""
    return "Write it as YYYY-MM-DD, for example 2028-08-27 — or leave it empty."


def _microsoft(ctx: _Ctx) -> Any:
    """Section 1, and the live client the folder questions in section 2 are asked with."""
    _section(
        ctx, 1, "Microsoft / OneDrive",
        "From the app registration in SETUP.md step 1. Nothing works without this.",
    )
    ctx.ask("GRAPH_TENANT_ID", "Directory (tenant) ID",
            help_text="On the app registration's Overview page. Not a secret.")
    ctx.ask("GRAPH_CLIENT_ID", "Application (client) ID",
            help_text="Same page, just above it. Not a secret.")
    ctx.ask("GRAPH_CLIENT_SECRET", "Client secret VALUE", secret=True,
            help_text="The Value column, not the Secret ID. Typing is hidden.")
    ctx.ask("GRAPH_USER_ID", "Whose OneDrive holds the recordings?",
            help_text="The sign-in address, e.g. james@yourcompany.co.za")
    ctx.ask("GRAPH_SECRET_EXPIRES_ON", "When does that secret expire? (YYYY-MM-DD)",
            required=False, validate=_iso_date_or_blank,
            help_text="Optional but worth it — the morning email counts down to it. "
                      "An expired secret is the commonest way a service like this dies quietly.")

    client = _graph_client(ctx)
    if client is None:
        # --no-verify, or the client would not build. Either way there is no drive to list,
        # so the folder ids have to be typed. Skipping them is how the wizard used to finish
        # "successfully" and still leave a .env the service refuses to start from.
        return None

    if not ctx.check("Microsoft sign-in and drive access", lambda: _probe_graph(client)):
        return None
    return client


def _graph_client(ctx: _Ctx) -> Any:
    if not ctx.verify:
        return None
    try:
        from .graph import GraphClient
    except Exception:  # pragma: no cover - import guard only
        return None

    class _Shim:
        tenant_id = ctx.values.get("GRAPH_TENANT_ID", "")
        client_id = ctx.values.get("GRAPH_CLIENT_ID", "")
        client_secret = ctx.values.get("GRAPH_CLIENT_SECRET", "")
        user_id = ctx.values.get("GRAPH_USER_ID", "")
        drive_id = ""

    try:
        return GraphClient.from_config(_Shim())
    except Exception as exc:  # noqa: BLE001
        ctx.out(ctx.style.red(f"  ✗ could not build the client — {exc}"))
        return None


def _probe_graph(client: Any) -> str:
    items = client.list_children(None)
    folders = [i for i in items if not i.is_file]
    return f"{len(folders)} folders visible at the top of the drive"


# ---------------------------------------------------------------------------------------
# section 2 — the routes
# ---------------------------------------------------------------------------------------

class _Folders:
    """The drive's folders, listed once and remembered, for every folder question asked.

    A driveItem id is forty characters nobody can recognise, so nothing in this wizard ever
    asks him to know one: folders are chosen from a live listing and shown back by name.
    The listing is per level and cached, because the same set of routes asks about the same
    folders several times and re-listing a drive between two questions is how a wizard
    starts to feel slow.

    With no live client — ``--no-verify``, or consent not granted yet — the very same
    method asks for an id by hand instead. It does not skip the question. A folder that was
    never asked for is a ``.env`` the service refuses to start from, which is exactly the
    failure this wizard exists to prevent.
    """

    def __init__(self, client: Any = None) -> None:
        self.client = client
        self._children: dict[str, list[Any]] = {}
        self._names: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._ancestors: dict[str, tuple[str, ...]] = {}
        self._warned = False

    @property
    def live(self) -> bool:
        return self.client is not None

    def children(self, folder_id: str = "") -> list[Any]:
        """The folders inside one folder, sorted by name.

        Never raises — a question he cannot answer is worse than a listing he cannot
        see — but it never swallows the reason either: why a folder came back empty is
        kept and printed with the empty list, so "no folders in here" and "that folder
        refused the request" are never the same thing on screen.
        """
        if folder_id in self._children:
            return self._children[folder_id]
        items: list[Any] = []
        if self.client is not None:
            try:
                raw = self.client.list_children(folder_id or None)
            except Exception as exc:  # noqa: BLE001 - shown, not hidden; see docstring
                self._errors[folder_id] = f"{type(exc).__name__}: {exc}"
                raw = []
            items = sorted(
                (i for i in raw
                 if getattr(i, "is_folder", False) and not getattr(i, "is_deleted", False)),
                key=lambda i: str(getattr(i, "name", "")).lower(),
            )
            for item in items:
                self._names[item.id] = item.name
        self._children[folder_id] = items
        return items

    def ancestors(self, folder_id: str) -> tuple[str, ...]:
        """Every folder above this one on the drive, nearest first. Empty when unknown.

        A folder id says nothing about what contains it, and containment is the difference
        between two routes that happen to sit near each other and two routes where one is
        quietly reading the other's recordings — OneDrive reports a folder and everything
        under it. So it is asked here, at the keyboard, where the ids are being chosen and
        there is a live drive to ask.

        Never raises and never asks twice: a chain that cannot be walked comes back empty,
        which the checks read as "not known" rather than as "no overlap".
        """
        wanted = (folder_id or "").strip()
        if not wanted or self.client is None:
            return ()
        if wanted in self._ancestors:
            return self._ancestors[wanted]
        chain: list[str] = []
        seen = {wanted}
        current = wanted
        # Bounded: a cycle cannot happen in a drive tree, but a walk that trusted the drive
        # to say so would hang the wizard if one ever did.
        for _ in range(32):
            try:
                item = self.client.get_item(current)
            except Exception:  # noqa: BLE001 - an unanswerable question is not a problem
                break
            parent = str(getattr(item, "parent_id", "") or "").strip()
            if not parent or parent in seen:
                break
            chain.append(parent)
            seen.add(parent)
            current = parent
        self._ancestors[wanted] = tuple(chain)
        return self._ancestors[wanted]

    def describe(self, folder_id: str) -> str:
        """A folder's name if it can be had, otherwise its id — never nothing at all."""
        if not folder_id:
            return "not chosen yet"
        if folder_id not in self._names and self.client is not None:
            try:
                self._names[folder_id] = str(self.client.get_item(folder_id).name or "")
            except Exception:  # noqa: BLE001 - an id we cannot resolve is still a valid id
                self._names[folder_id] = ""
        return self._names.get(folder_id) or folder_id

    def pick(
        self,
        ctx: _Ctx,
        question: str,
        blurb: str = "",
        *,
        current: str = "",
        allow_none: bool = False,
    ) -> str:
        if not self.live:
            return self._ask_by_hand(ctx, question, blurb, current=current, allow_none=allow_none)

        trail: list[Any] = []
        while True:
            here = trail[-1].id if trail else ""
            items = self.children(here)
            ctx.out()
            ctx.out(ctx.style.bold(question))
            if blurb:
                ctx.out(ctx.style.dim("  " + blurb))
            where = " / ".join(str(i.name) for i in trail) or "the top of the drive"
            ctx.out(ctx.style.dim(f"  in {where}:"))
            if not items:
                failure = self._errors.get(here, "")
                ctx.out(ctx.style.yellow(
                    f"    (that folder could not be listed — {failure})" if failure
                    else "    (no folders in here)"))
            for number, item in enumerate(items, 1):
                mark = ctx.style.green("   ← the one set now") if item.id == current else ""
                ctx.out(f"    {number}. {item.name}{mark}")
            if items:
                ctx.out(ctx.style.dim("    o 2    look inside folder 2"))
            if trail:
                ctx.out(ctx.style.dim("    u      back up a level"))
            ctx.out(ctx.style.dim("    h      type a folder id by hand"))
            if allow_none:
                ctx.out(ctx.style.dim("    n      no folder — leave these recordings where they are"))
            if current:
                ctx.out(ctx.style.dim(f"    Enter  keep {self.describe(current)}"))

            try:
                raw = input("  > ").strip()
            except EOFError:
                if current or allow_none:
                    return current
                raise SystemExit(_EOF_STOP) from None

            low = raw.lower()
            if not raw:
                if current:
                    return current
                ctx.out(ctx.style.red(_no_folder_here(items)))
                continue
            if allow_none and low in ("n", "no", "none"):
                return ""
            if trail and low in ("u", "up"):
                trail.pop()
                continue
            if low in ("h", "id", "hand"):
                typed = self._ask_by_hand(
                    ctx, question, blurb, current=current, allow_none=allow_none)
                if typed or allow_none:
                    return typed
                continue
            if items and low.startswith("o"):
                rest = low[1:].strip()
                if rest.isdigit() and 1 <= int(rest) <= len(items):
                    trail.append(items[int(rest) - 1])
                    continue
                ctx.out(ctx.style.red("  Say which one to look inside, like o 2."))
                continue
            if raw.isdigit() and 1 <= int(raw) <= len(items):
                chosen = items[int(raw) - 1]
                ctx.out(ctx.style.green(f"  → {chosen.name}"))
                return str(chosen.id)
            ctx.out(ctx.style.red(_no_folder_here(items)))

    def _ask_by_hand(
        self, ctx: _Ctx, question: str, blurb: str, *, current: str, allow_none: bool
    ) -> str:
        if not self.live and not self._warned:
            self._warned = True
            ctx.out()
            ctx.out(ctx.style.yellow(
                "  Without a live connection the folders cannot be listed for you, so their\n"
                "  ids have to be typed. Open the folder in OneDrive in a browser and the id\n"
                "  is the id= part of the address. Re-run without --no-verify once the app\n"
                "  registration is consented and you can pick them from a list instead."))
        help_text = blurb
        if allow_none:
            help_text = (blurb + "  Leave it empty for none.").strip()
        return ctx.ask_free(
            question + "  (folder id)", default=current, required=not allow_none,
            help_text=help_text,
        )


def _no_folder_here(items: Sequence[Any]) -> str:
    """What to say when the answer given is not one of the folders on offer."""
    if not items:
        return ("  There are no folders here. Go back up with u, or type a folder id by "
                "hand with h.")
    return f"  Choose a number from 1 to {len(items)}, or one of the letters above."


def _show_routes(ctx: _Ctx, routes: Sequence[Route], folders: _Folders) -> None:
    ctx.out()
    if not routes:
        ctx.out(ctx.style.yellow("  No routes yet — nothing would be watched."))
        return
    for route in routes:
        head = ctx.style.bold(f"  {route.display}") + ctx.style.dim(f"   {route.name}")
        if not route.enabled:
            head += ctx.style.yellow("   paused")
        ctx.out(head)
        ctx.out(f"      recordings arrive in   {folders.describe(route.source_folder_id)}")
        ctx.out(f"      transcripts go to      {folders.describe(route.output_folder_id)}")
        if route.archives:
            ctx.out(f"      archived after 60 days {folders.describe(route.archive_folder_id)}")
        else:
            ctx.out("      archived after 60 days no — recordings stay where they are")
        if route.engine:
            ctx.out(f"      transcribed by         {route.engine}")


def _report_routes(
    ctx: _Ctx, routes: Sequence[Route], folders: "_Folders | None" = None
) -> list[str]:
    """Say what is wrong with these routes, now, rather than at the end of the wizard."""
    problems = route_problems(routes, folders.ancestors if folders is not None else None)
    if not problems:
        return problems
    ctx.out()
    ctx.out(ctx.style.red(ctx.style.bold("  That is not something the service can run:")))
    for problem in problems:
        ctx.out(ctx.style.red(f"    • {problem}"))
    return problems


def _engine_options(ctx: _Ctx) -> list[tuple[str, str]]:
    chosen = ctx.values.get("TRANSCRIBE_ENGINE", "")
    default = f"the same as everything else ({chosen})" if chosen else \
        "the same as everything else — you choose which in the next section"
    return [("", default)] + [(name, label) for name, label in _ENGINES]


def _suggest_slug(label: str) -> str:
    """A usable short name out of what he called it: ``Site meetings`` -> ``site-meetings``."""
    cleaned = "".join(c if c.isalnum() else "-" for c in (label or "").strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def _ask_route_folders(ctx: _Ctx, folders: _Folders, route: Route) -> Route:
    """Source, then output, then archive — the three folders one kind of recording uses."""
    what = route.display
    source = folders.pick(
        ctx, f"{what}: which folder do they arrive in?",
        "The folder your phone, or WhatsApp, or the recorder uploads into.",
        current=route.source_folder_id,
    )
    output = folders.pick(
        ctx, f"{what}: where should their transcripts be written?",
        "Not the folder above. Several kinds may share one transcripts folder if you want "
        "them together.",
        current=route.output_folder_id,
    )
    archive = folders.pick(
        ctx, f"{what}: where should they move to once they are 60 days old?",
        "Only ever once their transcripts are confirmed present, and nothing is ever "
        "deleted. There need not be one — they can stay where they are for good.",
        current=route.archive_folder_id, allow_none=True,
    )
    return replace(
        route, source_folder_id=source, output_folder_id=output, archive_folder_id=archive,
    )


def _settle_route(ctx: _Ctx, others: Sequence[Route], route: Route, folders: _Folders) -> Route:
    """Keep asking about this route's folders until the whole set is sound, or he says stop.

    A problem here is shown against the set the route is joining, not against the route on
    its own, because the two failures that matter — writing transcripts into a watched
    folder, and two routes watching one folder — only exist between routes.
    """
    while True:
        problems = route_problems([*others, route], folders.ancestors)
        if not problems:
            return route
        _report_routes(ctx, [*others, route], folders)
        if not ctx.confirm("Choose different folders for this one?", default=True):
            ctx.notes.append(
                f"{_phrase(route)} is not usable as it stands: {problems[0]}"
            )
            return route
        route = _ask_route_folders(ctx, folders, route)


def _add_route(ctx: _Ctx, routes: Sequence[Route], folders: _Folders) -> Route:
    taken = {r.name for r in routes}

    def check_slug(raw: str) -> str:
        if not is_route_name(raw):
            return ("Lowercase letters, digits and hyphens only, starting with a letter or "
                    "a digit — calls, site-meetings, whatsapp.")
        if raw in taken:
            return f"There is already a route called {raw}. Give this one another name."
        return ""

    label = ctx.ask_free(
        "What do you call this kind of recording?",
        help_text="In plain words — Site meetings. This is what the morning email calls them.",
    )
    slug = ctx.ask_free(
        "And a short name for it, with no spaces?",
        default=_suggest_slug(label),
        help_text="Press Enter to take the one offered. It is written into the ledger "
                  "beside every recording this route handles, so it stays as it is "
                  "afterwards.",
        validate=check_slug,
    )
    route = Route(name=slug, label=label)
    route = _ask_route_folders(ctx, folders, route)
    route = replace(route, engine=ctx.choose(
        f"{label}: which service should transcribe them?",
        _engine_options(ctx), current=route.engine,
    ))
    _ask_route_reviewer(ctx, route)
    return _settle_route(ctx, routes, route, folders)


def _ask_route_reviewer(ctx: _Ctx, route: Route) -> None:
    """Who approves the passages held back from this folder.

    Asked here rather than left to a hand-edit, because the answer to "nobody said" is not
    "nobody sees it" — it is "the service owner sees all of it", and that is the one routing
    the design says must not happen by default. A staff member reviews their own held
    passages; only a staff disciplinary matter is the principal's. Staff record voluntarily,
    and one who works out that the boss reads the held text from their calls stops keeping a
    folder — which loses the recordings entirely.

    Written straight into the values rather than onto :class:`Route`, because it is an
    address: the ledger carries a route on every row and no address may ever go there.
    """
    variable = route_env_var(route.name, "REVIEWER")
    current = str(ctx.values.get(variable, "") or "").strip()

    def check(raw: str) -> str:
        text = raw.strip()
        if not text:
            return ""
        if text.count("@") != 1 or " " in text:
            return "That does not look like an email address."
        _local, _at, domain = text.partition("@")
        if "." not in domain or domain.startswith(".") or domain.endswith("."):
            return "That does not look like an email address."
        return ""

    answer = ctx.ask_free(
        f"{route.label}: who approves anything held back from these recordings?",
        default=current,
        required=False,
        help_text=(
            "The email address of whoever records into this folder — usually themselves. "
            "They see their own held passages; you see only how many are waiting and which "
            "site, except for staff disciplinary matters, which come to you. Leave it empty "
            "and everything held here comes to the service owner instead, including "
            "somebody's health and family circumstances — and the service will refuse to "
            "start once holding is switched on."
        ),
        validate=check,
    ).strip()
    if answer:
        ctx.values[variable] = answer
        ctx.remember_secret(answer)
    else:
        ctx.values.pop(variable, None)
        ctx.notes.append(
            f"No one is named to approve the passages held back from {_phrase(route)}. "
            f"Nothing is being held yet, so nothing is going anywhere — but set "
            f"{variable} before switching holding on."
        )


def _edit_route(ctx: _Ctx, routes: list[Route], folders: _Folders) -> list[Route]:
    index = _choose_route(ctx, routes, "Which one do you want to change?")
    if index is None:
        return routes
    route = routes[index]
    others = [r for i, r in enumerate(routes) if i != index]
    ctx.out()
    ctx.out(ctx.style.dim(
        f"  The short name stays as {route.name}: it is what the ledger has recorded "
        "against every\n  recording this route has already handled."))
    while True:
        _show_routes(ctx, [route], folders)
        what = ctx.choose(
            f"What about {route.display} do you want to change?",
            [
                ("done", "nothing more — go back"),
                ("label", "what it is called"),
                ("folders", "its folders"),
                ("engine", "which service transcribes it"),
                ("reviewer", "who approves anything held back from it"),
                ("enabled", "switch it back on" if not route.enabled
                            else "pause it — stop watching, keep everything"),
            ],
            current="done",
        )
        if what == "done":
            break
        if what == "label":
            route = replace(route, label=ctx.ask_free(
                "What should it be called?", default=route.label,
                help_text="Only the name people read. Nothing else changes."))
        elif what == "folders":
            route = _ask_route_folders(ctx, folders, route)
        elif what == "reviewer":
            _ask_route_reviewer(ctx, route)
        elif what == "engine":
            route = replace(route, engine=ctx.choose(
                f"{route.display}: which service should transcribe them?",
                _engine_options(ctx), current=route.engine))
        elif what == "enabled":
            route = replace(route, enabled=not route.enabled)
            ctx.out(ctx.style.green(
                f"  → {route.display} is " + ("watched again."
                if route.enabled else
                "paused. Its folder is no longer watched; everything it has already "
                "processed is kept.")))
        _report_routes(ctx, [*others, route], folders)
    # No settle loop here on purpose: the edit menu he has just left is itself the way to
    # change the folders again, and a problem like "every route is paused" is not one that
    # asking about folders can fix. Anything still wrong is printed by the routes menu he
    # returns to, and recorded as a note if he leaves it that way.
    updated = list(routes)
    updated[index] = route
    return updated


def _remove_route(ctx: _Ctx, routes: list[Route], folders: _Folders) -> list[Route]:
    index = _choose_route(ctx, routes, "Which one do you want to take out?")
    if index is None:
        return routes
    route = routes[index]
    ctx.out()
    ctx.out(ctx.style.dim(
        f"  Taking {route.display} out stops the service watching that folder, and nothing\n"
        "  else. The recordings in OneDrive are not touched, and the ledger keeps every row\n"
        "  it ever wrote for them — you can look up anything it processed afterwards.\n"
        "  If you only want it to stop for a while, change it instead and pause it: that\n"
        "  keeps its folders in the file so switching it back on is one answer."))
    if not ctx.confirm(f"Take {route.display} out?", default=False):
        return routes
    ctx.out(ctx.style.green(f"  → {route.display} taken out. Nothing was deleted."))
    return [r for i, r in enumerate(routes) if i != index]


def _choose_route(ctx: _Ctx, routes: Sequence[Route], question: str) -> int | None:
    if not routes:
        ctx.out(ctx.style.yellow("  There are no routes to choose from yet."))
        return None
    options = [(str(i + 1), f"{r.display}  ({r.name})") for i, r in enumerate(routes)]
    options.append(("cancel", "never mind"))
    picked = ctx.choose(question, options, current="cancel")
    if picked == "cancel":
        return None
    return int(picked) - 1


def _first_routes(ctx: _Ctx, folders: _Folders) -> list[Route]:
    """The first run, where the answer to "what routes?" should be one keystroke.

    He records three kinds of thing and has said so, so the wizard offers those three by
    name rather than asking him to invent a structure. The engine is left at the service
    default for all three: that is what an override is *for*, and asking three extra
    questions on a first run to say "the same as everything else" three times is the kind
    of thoroughness that gets a wizard abandoned half-done.
    """
    ctx.out()
    ctx.out(ctx.style.bold("  You have not set up any folders yet."))
    ctx.out(ctx.style.dim(
        "  A route is one folder recordings arrive in, and the folder their transcripts go\n"
        "  to. The service can run as many as you like — one per kind of recording, so a\n"
        "  site meeting and a phone call need not land in the same place."))
    ctx.out()
    for _slug, label in SUGGESTED_ROUTES:
        ctx.out(ctx.style.dim(f"    • {label}"))
    ctx.out()
    if not ctx.confirm("Set up those three now?", default=True):
        return [_add_route(ctx, [], folders)]

    shared_output = ""
    shared_archive = ""
    pooled = ctx.confirm(
        "Should all three write their transcripts into one folder?", default=False)
    if pooled:
        shared_output = folders.pick(
            ctx, "Which folder should all the transcripts go to?",
            "Every kind writes here. Each one is still tracked separately, and the morning "
            "email still reports them apart.")
        shared_archive = folders.pick(
            ctx, "Where should recordings move to once they are 60 days old?",
            "The same archive for all three. Choose n to leave them where they are.",
            allow_none=True)

    routes: list[Route] = []
    for slug, label in SUGGESTED_ROUTES:
        route = Route(name=slug, label=label,
                      output_folder_id=shared_output, archive_folder_id=shared_archive)
        if pooled:
            route = replace(route, source_folder_id=folders.pick(
                ctx, f"{label}: which folder do they arrive in?",
                "The folder your phone, or WhatsApp, or the recorder uploads into.",
                current=route.source_folder_id))
        else:
            route = _ask_route_folders(ctx, folders, route)
        routes.append(_settle_route(ctx, routes, route, folders))
    return routes


def _routes(ctx: _Ctx, client: Any) -> None:
    _section(
        ctx, 2, "Your folders",
        "One route per kind of recording: where it arrives, and where its transcripts go.",
    )
    folders = _Folders(client)
    routes = routes_from_values(ctx.values)
    if routes:
        ctx.out()
        ctx.out(ctx.style.dim("  These are the routes this file already has:"))
    else:
        routes = _first_routes(ctx, folders)

    while True:
        _show_routes(ctx, routes, folders)
        _report_routes(ctx, routes, folders)
        what = ctx.choose(
            "What now?",
            [
                ("keep", "keep these as they are"),
                ("add", "add another kind of recording"),
                ("edit", "change one of them"),
                ("remove", "take one out"),
                ("restart", "start the folders over from nothing"),
            ],
            current="keep",
        )
        if what == "keep":
            break
        if what == "add":
            routes.append(_add_route(ctx, routes, folders))
        elif what == "edit":
            routes = _edit_route(ctx, routes, folders)
        elif what == "remove":
            routes = _remove_route(ctx, routes, folders)
        elif what == "restart":
            routes = _first_routes(ctx, folders)

    for problem in route_problems(routes, folders.ancestors):
        ctx.notes.append(problem)
    for note in shared_archive_notes(routes):
        ctx.notes.append(note)
    routes_to_values(ctx.values, routes)


def _engines_in_use(ctx: _Ctx) -> list[str]:
    """Every engine some part of this configuration needs a key for.

    The service default, plus any engine a route overrides it with. Asking only for the
    default's key is how a route set to ElevenLabs ends up with no ElevenLabs key and fails
    on its first recording — and the run that dropped "unused" keys at the end was deleting
    exactly the key that route needed.
    """
    engines = [ctx.values.get("TRANSCRIBE_ENGINE", "").strip().lower()]
    for route in routes_from_values(ctx.values):
        if route.enabled and route.engine:
            engines.append(route.engine.strip().lower())
    return list(dict.fromkeys(e for e in engines if e))


def _routes_using(ctx: _Ctx, engine: str) -> list[Route]:
    return [r for r in routes_from_values(ctx.values)
            if r.enabled and r.engine.strip().lower() == engine]


_ENGINES = [
    ("openai", "OpenAI — what you asked for; strong multilingual, splits files over 25 MB"),
    ("elevenlabs", "ElevenLabs Scribe — takes whole files, claims all four SA languages"),
    ("azure", "Azure Speech — stays inside your tenant, but has no isiXhosa"),
]


def _transcription(ctx: _Ctx) -> None:
    _section(ctx, 3, "Transcription",
             "Which service turns the audio into words, for every route that has not "
             "asked for a different one.")
    engine = ctx.choose("Which transcription engine?", _ENGINES,
                        current=ctx.values.get("TRANSCRIBE_ENGINE", "openai"))
    ctx.values["TRANSCRIBE_ENGINE"] = engine

    # The default is not necessarily the only one: a route may transcribe with something
    # else, and that engine needs its own key or that route fails on its first recording.
    for name in _engines_in_use(ctx):
        borrowers = _routes_using(ctx, name)
        if borrowers and name != engine:
            ctx.out()
            ctx.out(ctx.style.dim(
                "  " + ", ".join(r.display for r in borrowers)
                + f" transcribe with {name}, so it needs its own key too."))
        if name == "openai":
            ctx.ask("OPENAI_API_KEY", "OpenAI API key", secret=True,
                    help_text="platform.openai.com → API keys. Starts sk-.")
            ctx.check("the OpenAI key", lambda: _probe_openai(ctx.values["OPENAI_API_KEY"]))
        elif name == "elevenlabs":
            ctx.ask("ELEVENLABS_API_KEY", "ElevenLabs API key", secret=True,
                    help_text="elevenlabs.io → Developers → API keys.")
            ctx.check("the ElevenLabs key",
                      lambda: _probe_elevenlabs(ctx.values["ELEVENLABS_API_KEY"]))
        elif name == "azure":
            ctx.ask("AZURE_SPEECH_KEY", "Azure Speech key", secret=True)
            ctx.ask("AZURE_SPEECH_REGION", "Azure Speech region",
                    help_text="e.g. southafricanorth, or westeurope.")


def _probe_openai(key: str) -> str:
    status, doc = _http_json("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {key}"})
    if status != 200:
        raise RuntimeError(_api_error(doc))
    n = len(doc.get("data") or [])
    return f"key accepted, {n} models available"


def _probe_elevenlabs(key: str) -> str:
    status, doc = _http_json("https://api.elevenlabs.io/v1/user",
                             headers={"xi-api-key": key})
    if status != 200:
        raise RuntimeError(_api_error(doc))
    return "key accepted"


#: Model ids from the bundled claude-api reference, not from memory. Priced per million
#: tokens. The router runs on every recording so nothing is skipped on a guess; the strong
#: model reads only the substantive ones.
_ANALYSIS_TIERS = [
    ("balanced", "Haiku 4.5 sorts everything, Opus 5 reads what matters  (recommended)",
     "claude-haiku-4-5", "claude-opus-5"),
    ("thorough", "Opus 5 does both passes — most careful, costs more",
     "claude-opus-5", "claude-opus-5"),
    ("light", "Haiku 4.5 does both passes — cheapest, less careful on long meetings",
     "claude-haiku-4-5", "claude-haiku-4-5"),
]


def _analysis(ctx: _Ctx) -> None:
    _section(
        ctx, 4, "The AI pass",
        "Reads each transcript and pulls out who promised what. Separate from transcription.",
    )
    ctx.values["ANALYSIS_PROVIDER"] = "anthropic"
    ctx.values["ANALYSIS_BASE_URL"] = "https://api.anthropic.com"

    ctx.ask("ANALYSIS_API_KEY", "Anthropic API key", secret=True,
            help_text="console.anthropic.com → API keys. Starts sk-ant-.")

    current = ctx.values.get("ANALYSIS_MODEL_CHEAP", "")
    tier_now = ""
    for key, _label, cheap, strong in _ANALYSIS_TIERS:
        if cheap == current and strong == ctx.values.get("ANALYSIS_MODEL_STRONG", ""):
            tier_now = key
    tier = ctx.choose(
        "How careful should the AI pass be?",
        [(k, label) for k, label, _c, _s in _ANALYSIS_TIERS],
        current=tier_now or "balanced",
    )
    for key, _label, cheap, strong in _ANALYSIS_TIERS:
        if key == tier:
            ctx.values["ANALYSIS_MODEL_CHEAP"] = cheap
            ctx.values["ANALYSIS_MODEL_STRONG"] = strong

    ctx.check("the Anthropic key", lambda: _probe_anthropic(
        ctx.values["ANALYSIS_API_KEY"], ctx.values["ANALYSIS_MODEL_CHEAP"]))


def _probe_anthropic(key: str, model: str) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
    }).encode()
    status, doc = _http_json(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        data=body,
        method="POST",
    )
    if status != 200:
        raise RuntimeError(_api_error(doc))
    blocks = doc.get("content") or []
    said = ""
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            said = str(block.get("text", "")).strip()
            break
    return f"{doc.get('model', model)} answered {said[:24]!r}"


def _email(ctx: _Ctx) -> None:
    _section(
        ctx, 5, "The morning email",
        "One message at 06:00 — sent on good days too, so silence always means something is wrong.",
    )
    ctx.ask("SMTP_HOST", "Mail server", default="smtp.office365.com",
            help_text="smtp.office365.com for Microsoft 365.")
    ctx.ask("SMTP_PORT", "Port", default="587",
            validate=lambda v: "" if v.isdigit() else "Numbers only — usually 587.")
    ctx.ask("SMTP_USER", "Sign-in address for that mailbox")
    ctx.ask("SMTP_PASSWORD", "Its password (an app password, if the account has 2FA)", secret=True)
    ctx.ask("SMTP_FROM", "Send the report FROM", default=ctx.values.get("SMTP_USER", ""))
    ctx.ask("SMTP_TO", "Send the report TO",
            help_text="Your own address. Several are fine, separated by commas.")
    ctx.ask("DIGEST_HOUR", "What hour should it arrive? (0-23)", default="6",
            validate=lambda v: "" if v.isdigit() and 0 <= int(v) <= 23 else "A number from 0 to 23.")

    if ctx.verify and ctx.confirm("Send a test email now?", default=True):
        ctx.check("the morning email", lambda: _probe_smtp(ctx))


def _probe_smtp(ctx: _Ctx) -> str:
    import smtplib
    from email.message import EmailMessage

    host = ctx.values["SMTP_HOST"]
    port = int(ctx.values.get("SMTP_PORT", "587"))
    recipients = [a.strip() for a in ctx.values["SMTP_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = "Recordings: setup test"
    msg["From"] = ctx.values["SMTP_FROM"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "This is the transcriber's setup test.\n\n"
        "If you can read this, the morning report can reach you. From now on it arrives "
        "every day at the hour you chose — including on days when nothing went wrong, "
        "because a report that only turns up when something breaks looks exactly like a "
        "service that has died.\n"
    )

    if port == 465:
        server: Any = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    try:
        if port != 465:
            server.starttls(context=ssl.create_default_context())
        server.login(ctx.values["SMTP_USER"], ctx.values["SMTP_PASSWORD"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass
    return f"sent to {', '.join(recipients)} — go and look"


def _heartbeat(ctx: _Ctx) -> None:
    _section(
        ctx, 6, "Staying alive",
        "The one alarm that still works when the service itself is dead.",
    )
    ctx.out()
    ctx.out(ctx.style.dim(
        "  Sign up free at healthchecks.io, add a check, and paste its ping URL. If the\n"
        "  transcriber stops pinging it, healthchecks emails you — which is how you find\n"
        "  out in two hours instead of four days."))
    url = ctx.ask(
        "HEARTBEAT_URL", "Ping URL",
        help_text="Required, deliberately — it is the only alarm that still works when the "
                  "service itself is dead.",
        validate=lambda v: "" if v.startswith("https://") else "Must start with https://")
    if url and ctx.verify:
        ctx.check("the heartbeat", lambda: _probe_heartbeat(url))


def _probe_heartbeat(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as resp:
        resp.read()
        return f"pinged, {resp.status} — the check should now show as up"


def _local(ctx: _Ctx) -> None:
    _section(ctx, 7, "Where it keeps its notes", "Sensible defaults; press Enter through these.")
    ctx.ask("LEDGER_PATH", "Where should the ledger live?", default="./transcriber.db",
            help_text="The permanent record of every recording and what happened to it. "
                      "Back this up — it is the memory that stops a loss going unnoticed.")
    ctx.ask("WORK_DIR", "Scratch folder for downloads", default="./work")


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

_BANNER = """
  This asks for everything the transcriber needs, checks each answer against the real
  service, and writes it to .env — so a wrong value costs you thirty seconds now instead
  of a day of recordings later.

  Press Enter to keep an existing answer. Ctrl-C stops without writing anything.
  Nothing you type is printed back, logged, or sent anywhere but that one file.
"""


def run_setup(
    *,
    env_path: str = ".env",
    verify: bool = True,
    assume_yes: bool = False,
    stream: Any = None,
) -> int:
    stream = stream or sys.stdout
    style = _Style(_supports_colour(stream))
    existing = load_env_file(env_path)
    ctx = _Ctx(values=dict(existing), style=style, verify=verify, assume_yes=assume_yes)
    for name in SECRET_VARS:
        ctx.remember_secret(existing.get(name, ""))

    ctx.out(style.bold("\n  Setting up the transcriber"))
    ctx.out(style.dim(_BANNER.rstrip()))
    if existing:
        ctx.out(style.green(f"  Found an existing {env_path} — its answers are the defaults."))
    if not verify:
        ctx.out(style.yellow("  Running with --no-verify: nothing will be checked against a real service."))

    try:
        client = _microsoft(ctx)
        _routes(ctx, client)
        _transcription(ctx)
        _analysis(ctx)
        _email(ctx)
        _heartbeat(ctx)
        _local(ctx)
    except KeyboardInterrupt:
        ctx.out(style.yellow("\n\n  Stopped. Nothing was written.\n"))
        return 130

    # Drop keys belonging to engines nothing uses, so a stale key cannot be picked up later
    # by a config change nobody remembers making. "Uses" means the service default *or* any
    # route that overrides it: deleting a key on the strength of the default alone is how a
    # route set to ElevenLabs loses the key it was asked for one question earlier.
    in_use = set(_engines_in_use(ctx))
    for name, owner in (("OPENAI_API_KEY", "openai"),
                        ("ELEVENLABS_API_KEY", "elevenlabs"),
                        ("AZURE_SPEECH_KEY", "azure")):
        if owner not in in_use:
            ctx.values.pop(name, None)
    if "azure" not in in_use:
        ctx.values.pop("AZURE_SPEECH_REGION", None)

    ctx.out()
    ctx.out(style.cyan("─" * 74))
    ctx.out(style.bold("  Writing " + env_path))
    write_env_file(
        env_path, ctx.values,
        header=[
            "The transcriber's settings. Written by `transcriber setup`.",
            "",
            "This file holds live credentials. It is chmod 0600 and .gitignore'd —",
            "keep it that way, and never paste its contents into a chat or an email.",
            "Re-run `python3 -m transcriber setup` to change any of it.",
        ],
    )
    ctx.out(style.green(f"  ✓ written, readable only by you (0600)"))

    problems = _validate(env_path, ctx)
    ctx.out()
    ctx.out(style.cyan("─" * 74))
    if ctx.notes:
        ctx.out(style.yellow(style.bold("  Worth going back to:")))
        for note in ctx.notes:
            ctx.out(style.yellow(f"    • {note}"))
        ctx.out()
    if problems:
        ctx.out(style.red(style.bold("  The settings are not complete yet:")))
        for problem in problems:
            ctx.out(style.red(f"    • {problem}"))
        ctx.out()
        ctx.out("  Re-run " + style.bold("python3 -m transcriber setup") + " to fill in the rest.")
        return 1

    ctx.out(style.green(style.bold("  Ready. It will watch:")))
    for route in routes_from_values(ctx.values):
        state = "" if route.enabled else "   (paused)"
        ctx.out(f"    • {route.display}{state}")
    ctx.out()
    ctx.out(style.green(style.bold("  Three commands, in this order:")))
    ctx.out()
    ctx.out("    " + style.bold("python3 -m transcriber selftest"))
    ctx.out(style.dim("      Proves the code is sane. Touches nothing."))
    ctx.out("    " + style.bold("python3 -m transcriber once --dry-run"))
    ctx.out(style.dim("      Looks at your OneDrive and writes nothing. Confirms the permissions."))
    ctx.out("    " + style.bold("python3 -m transcriber once --limit 3"))
    ctx.out(style.dim("      Does three recordings for real. Read them before letting it near the rest."))
    ctx.out()
    ctx.out("  Then " + style.bold("python3 -m transcriber run") + " to start the loop.")
    ctx.out()
    return 0


def _validate(env_path: str, ctx: _Ctx) -> list[str]:
    """Load what we just wrote through the real Config, so the wizard cannot disagree with it."""
    try:
        merged = dict(os.environ)
        merged.update(load_env_file(env_path))
        config_mod.Config.from_env(merged)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        lines = [ln.strip(" -•\t") for ln in text.splitlines() if ln.strip()]
        return [ln for ln in lines if ln][:20] or [text[:300]]
    return []
